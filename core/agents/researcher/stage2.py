"""2단계: 검증 수집 — 구글/네이버 병렬."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable

from core.agents.researcher.parser import (
    _chunk,
    _extract_items_from_serp,
    _extract_serp_features,
    _is_relevant_serp_item,
    _map_content_type,
    _normalize_keyword,
    _parse_web_fetch_result,
)
from core.schemas import Stage2Output

logger = logging.getLogger(__name__)

SafeToolCall = Callable[..., Awaitable]


async def stage2_validation(
    reps: list[str],
    *,
    all_keywords: list[str] | None = None,
    paa_questions: dict[str, list[str]] | None = None,
    stage1_volumes: dict[str, int] | None = None,
    stage1_volumes_pc: dict[str, int] | None = None,
    stage1_volumes_mobile: dict[str, int] | None = None,
    safe_tool_call: SafeToolCall,
    google_search_fn: Callable,
    naver_keyword_volume_fn: Callable,
    google_keyword_trend_fn: Callable,
    naver_keyword_trend_fn: Callable,
    naver_blog_search_fn: Callable,
    web_fetch_fn: Callable,
    naver_serp_features_fn: Callable,
) -> Stage2Output:
    """대표 키워드 리스트에 대해 2단계 검증 수집을 수행한다."""
    logger.info("2단계 시작: representatives=%d", len(reps))
    if not reps:
        return Stage2Output()

    # 구글 SERP 사전 수집 (1회)
    serp_cache = await _prefetch_serp(reps, safe_tool_call, google_search_fn)

    # 2-1 검색량 + 2-2 상위 콘텐츠 (구글/네이버 병렬)
    vol, google_meta, naver_meta = await asyncio.gather(
        _stage2a_volume(
            reps,
            all_keywords=all_keywords,
            stage1_volumes=stage1_volumes,
            stage1_volumes_pc=stage1_volumes_pc,
            stage1_volumes_mobile=stage1_volumes_mobile,
            safe_tool_call=safe_tool_call,
            naver_keyword_volume_fn=naver_keyword_volume_fn,
            google_keyword_trend_fn=google_keyword_trend_fn,
            naver_keyword_trend_fn=naver_keyword_trend_fn,
        ),
        _stage2c_google_content(
            reps, serp_cache,
            safe_tool_call=safe_tool_call,
            web_fetch_fn=web_fetch_fn,
        ),
        _stage2c_naver_content(
            reps,
            safe_tool_call=safe_tool_call,
            naver_blog_search_fn=naver_blog_search_fn,
        ),
    )

    h2_topics = _stage2d_content_gap(google_meta)
    google_features = _stage2e_google_serp_features(
        reps, serp_cache, paa_questions=paa_questions or {},
    )
    naver_features = await _stage2e_naver_serp_features(
        reps,
        safe_tool_call=safe_tool_call,
        naver_serp_features_fn=naver_serp_features_fn,
    )

    logger.info("2단계 완료")
    return Stage2Output(
        volumes=vol,
        google_content_metas=google_meta,
        naver_content_metas=naver_meta,
        h2_topics=h2_topics,
        google_serp_features=google_features,
        naver_serp_features=naver_features,
    )


async def _prefetch_serp(
    reps: list[str],
    safe_tool_call: SafeToolCall,
    google_search_fn: Callable,
) -> dict[str, str]:
    """대표 키워드별 google_search 1회 호출, 캐시 dict 반환."""
    tasks = [
        safe_tool_call(
            f"google_search({kw})", google_search_fn(kw, 10), "{}",
        )
        for kw in reps
    ]
    results = await asyncio.gather(*tasks)
    return {kw: raw or "{}" for kw, raw in zip(reps, results)}


# -- 2-1: 검색량 + 추이 (구글/네이버 분리, PC/모바일) --

async def _stage2a_volume(
    reps: list[str],
    *,
    all_keywords: list[str] | None = None,
    stage1_volumes: dict[str, int] | None = None,
    stage1_volumes_pc: dict[str, int] | None = None,
    stage1_volumes_mobile: dict[str, int] | None = None,
    safe_tool_call: SafeToolCall,
    naver_keyword_volume_fn: Callable,
    google_keyword_trend_fn: Callable,
    naver_keyword_trend_fn: Callable,
) -> dict[str, dict]:
    # 볼륨 조회 대상: 전체 클러스터 키워드 (없으면 대표만)
    volume_targets = all_keywords if all_keywords else reps
    logger.info("2a 볼륨 조회 대상: %d개 (대표 %d개)", len(volume_targets), len(reps))

    s1_vol = stage1_volumes or {}
    s1_pc = stage1_volumes_pc or {}
    s1_mob = stage1_volumes_mobile or {}

    result: dict[str, dict] = {}
    # 볼륨 대상 전부 초기화 — Stage 1 볼륨으로 프리필
    for kw in volume_targets:
        nk = _normalize_keyword(kw)
        result[kw] = {
            "naver_volume": s1_vol.get(nk, 0),
            "naver_volume_pc": s1_pc.get(nk, 0),
            "naver_volume_mobile": s1_mob.get(nk, 0),
            "google_trend_avg": 0.0,
            "naver_trend_avg": 0.0,
            "google_direction": "stable",
            "naver_direction": "stable",
        }
    # 대표 키워드도 반드시 포함
    for kw in reps:
        if kw not in result:
            nk = _normalize_keyword(kw)
            result[kw] = {
                "naver_volume": s1_vol.get(nk, 0),
                "naver_volume_pc": s1_pc.get(nk, 0),
                "naver_volume_mobile": s1_mob.get(nk, 0),
                "google_trend_avg": 0.0,
                "naver_trend_avg": 0.0,
                "google_direction": "stable",
                "naver_direction": "stable",
            }

    # normalized → original key 역매핑 (볼륨 결과 매칭용)
    norm_to_keys: dict[str, list[str]] = {}
    for kw in result:
        n = _normalize_keyword(kw)
        norm_to_keys.setdefault(n, []).append(kw)

    # Stage 1에서 이미 볼륨 있는 키워드 제외 → API 호출량 절감
    need_query = [kw for kw in volume_targets if result[kw]["naver_volume"] == 0]

    # 띄어쓰기 제거 변형 추가 — "외주 개발 비용" → "외주개발비용"도 함께 조회
    nospace_to_orig: dict[str, list[str]] = {}
    query_set: list[str] = []
    seen_q: set[str] = set()
    for kw in need_query:
        nk = _normalize_keyword(kw)
        if nk not in seen_q:
            query_set.append(kw)
            seen_q.add(nk)
        nospace = nk.replace(" ", "")
        if nospace != nk and nospace not in seen_q:
            query_set.append(nospace)
            seen_q.add(nospace)
            nospace_to_orig.setdefault(nospace, []).append(kw)

    logger.info(
        "2a searchad 쿼리 대상: %d개 (+변형 %d개, Stage1 프리필: %d개 스킵)",
        len(need_query), len(query_set) - len(need_query),
        len(volume_targets) - len(need_query),
    )

    # naver_searchad — 순차 (429 방지, 배치 간 1초 대기), PC/모바일 분리
    searchad_results = []
    for i, batch in enumerate(_chunk(query_set, 5)):
        if i > 0:
            await asyncio.sleep(1)
        raw = await safe_tool_call(
            f"naver_searchad({batch})",
            naver_keyword_volume_fn(batch),
            "{}",
        )
        searchad_results.append(raw)
    for raw in searchad_results:
        if not raw:
            continue
        try:
            data = json.loads(raw)
            for item in data.get("input_keywords", []):
                kw_raw = item.get("keyword", "")
                n = _normalize_keyword(kw_raw)
                vol_total = item.get("monthly_total", 0)
                vol_pc = item.get("monthly_pc", 0)
                vol_mob = item.get("monthly_mobile", 0)
                # 정확 매칭
                for key in norm_to_keys.get(n, []):
                    result[key]["naver_volume"] = vol_total
                    result[key]["naver_volume_pc"] = vol_pc
                    result[key]["naver_volume_mobile"] = vol_mob
                # 띄어쓰기 제거 변형 → 원본에 전파
                for orig_kw in nospace_to_orig.get(n, []):
                    if result.get(orig_kw, {}).get("naver_volume", 0) == 0:
                        result[orig_kw]["naver_volume"] = vol_total
                        result[orig_kw]["naver_volume_pc"] = vol_pc
                        result[orig_kw]["naver_volume_mobile"] = vol_mob
        except (json.JSONDecodeError, TypeError):
            pass

    # google_trends — 순차 (429 방지, 배치 간 5초 대기)
    batches = _chunk(reps, 5)
    for i, batch in enumerate(batches):
        if i > 0:
            await asyncio.sleep(5)
        raw = await safe_tool_call(
            f"google_trends({batch})",
            google_keyword_trend_fn(batch),
            "{}",
        )
        if raw:
            try:
                data = json.loads(raw)
                for kw, td in data.get("trends", {}).items():
                    if kw in result:
                        result[kw]["google_trend_avg"] = td.get(
                            "average", 0.0,
                        )
                        result[kw]["google_direction"] = td.get(
                            "direction", "stable",
                        )
                        result[kw]["google_trend_series"] = td.get(
                            "series", [],
                        )
            except (json.JSONDecodeError, TypeError):
                pass

    # naver_datalab — 병렬 (배치 5)
    datalab_tasks = [
        safe_tool_call(
            f"naver_datalab({batch})",
            naver_keyword_trend_fn(batch),
            "{}",
        )
        for batch in _chunk(reps, 5)
    ]
    datalab_results = await asyncio.gather(*datalab_tasks)
    for raw in datalab_results:
        if not raw:
            continue
        try:
            data = json.loads(raw)
            for kw, td in data.items():
                if kw in result:
                    result[kw]["naver_trend_avg"] = td.get("average", 0.0)
                    result[kw]["naver_direction"] = td.get(
                        "direction", "stable",
                    )
                    result[kw]["naver_trend_series"] = td.get(
                        "series", [],
                    )
        except (json.JSONDecodeError, TypeError):
            pass

    return result


# -- 2-2: 구글 상위 콘텐츠 분석 --

async def _stage2c_google_content(
    reps: list[str],
    serp_cache: dict[str, str],
    *,
    safe_tool_call: SafeToolCall,
    web_fetch_fn: Callable,
) -> dict[str, list[dict]]:
    """구글 상위 10개 콘텐츠 메타 수집."""
    result: dict[str, list[dict]] = {}

    for kw in reps:
        raw_serp = serp_cache.get(kw, "{}")
        all_items = _extract_items_from_serp(raw_serp)

        # hallucination 필터: 키워드 토큰과 겹침 없는 결과 제거
        items = [
            it for it in all_items
            if _is_relevant_serp_item(kw, it)
        ]
        filtered_count = len(all_items) - len(items)
        if filtered_count:
            logger.info(
                "구글 SERP 관련성 필터: '%s' — %d/%d건 제거",
                kw, filtered_count, len(all_items),
            )

        fetch_tasks = [
            safe_tool_call(
                f"web_fetch({item.get('link', '')})",
                web_fetch_fn(item.get("link", ""), 500),
                "",
            )
            for item in items[:10]
            if item.get("link")
        ]
        fetch_results = await asyncio.gather(*fetch_tasks)

        metas: list[dict] = []
        for rank, item in enumerate(items[:10], 1):
            fetched = (
                fetch_results[rank - 1]
                if rank - 1 < len(fetch_results)
                else ""
            )
            parsed = _parse_web_fetch_result(fetched or "")
            metas.append({
                "rank": rank,
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "h2_structure": parsed.get("h2_structure", []),
                "publish_date": parsed.get("publish_date"),
                "content_type": _map_content_type(
                    item.get("content_type", "website"),
                ),
                "platform": "google",
            })
        result[kw] = metas
    return result


# -- 2-2: 네이버 상위 콘텐츠 분석 --

async def _stage2c_naver_content(
    reps: list[str],
    *,
    safe_tool_call: SafeToolCall,
    naver_blog_search_fn: Callable,
) -> dict[str, list[dict]]:
    """네이버 상위 10개 콘텐츠 메타 수집 (배치 5건 + 1초 대기)."""
    result: dict[str, list[dict]] = {}
    naver_results: list[str] = []

    for batch in _chunk(reps, 5):
        if naver_results:
            await asyncio.sleep(1)
        batch_tasks = [
            safe_tool_call(
                f"naver_search({kw})",
                naver_blog_search_fn(kw, 10),
                "{}",
            )
            for kw in batch
        ]
        batch_results = await asyncio.gather(*batch_tasks)
        naver_results.extend(batch_results)

    for kw, raw in zip(reps, naver_results):
        metas: list[dict] = []
        if raw:
            try:
                data = json.loads(raw)
                for rank, item in enumerate(data.get("items", [])[:10], 1):
                    metas.append({
                        "rank": rank,
                        "title": item.get("title", ""),
                        "url": item.get("link", ""),
                        "h2_structure": [],
                        "publish_date": item.get("postdate"),
                        "content_type": "blog",
                        "platform": "naver",
                        "exposure_area": "블로그",
                    })
            except (json.JSONDecodeError, TypeError):
                pass
        result[kw] = metas
    return result


# -- 2d: H2 주제 목록 (데이터 조립, 도구 호출 없음) --

_H2_BLACKLIST_PATTERNS = [
    # 네비게이션/UI (한국어)
    r"^(인기|추천|관련|최신|다른)\s*(글|포스트|게시물|콘텐츠|기사|뉴스)",
    r"^(댓글|코멘트|답글|리뷰)\s",
    r"^(공유하기|구독|뉴스레터|이메일)",
    r"^(카테고리|태그|목차|사이드바)",
    r"^(최근\s*글|인기\s*글|많이\s*본|조회수)",
    r"^(이전\s*글|다음\s*글|관련\s*글|추천\s*글)",
    # 네비게이션/UI (영어)
    r"^(footer|header|nav|sidebar|menu|navigation|breadcrumb)",
    r"^(about|contact|privacy|terms|copyright|disclaimer|sitemap)",
    r"^(search|sign\s*in|log\s*in|sign\s*up|register|subscribe)",
    r"^(table\s*of\s*contents|toc|share|social)",
    # 개발 문서 UI
    r"^(additional\s*resources|see\s*also|related\s*topics|further\s*reading)",
    r"^(이\s*문서의?\s*내용|추가\s*리소스|피드백|참고\s*항목|관련\s*항목)",
    r"^(prerequisites|requirements|getting\s*started|installation)",
    r"^(changelog|release\s*notes|version\s*history|what'?s\s*new)",
    r"^(api\s*reference|configuration|parameters|options|properties)$",
    # 광고/프로모션
    r"^(광고|ad|sponsored|프로모션|할인|쿠폰|이벤트\s*안내)",
    r"^(무료\s*(상담|견적|체험)|지금\s*바로|문의\s*하기)",
    # 앱스토어/제품 페이지
    r"^(앱\s*정보|app\s*info|ratings|reviews?\s*&?\s*ratings)",
    r"^(data\s*safety|permissions|what'?s\s*new|version)",
    # 지역/위치 네비게이션
    r"^(africa|americas|asia|europe|oceania|middle\s*east)",
    # 잡포스팅/채용 UI
    r"^(포지션|채용|지원\s*하기|연봉|근무\s*조건|경력|기술\s*스택)",
    r"^(my\s*홈|메시지|채용\s*공고|프로필|bp\s*포인트|이력서)",
    r"^(apply|job\s*details|qualifications|responsibilities|benefits)",
    # 이미지/미디어 사이트
    r"^(관련\s*무료\s*이미지|similar\s*images|related\s*(photos|images))",
    r"^(다른\s*앨범|더\s*보기|more\s*from|see\s*more)",
    # 사이트 공통 네비게이션/메뉴
    r"^(메인\s*메뉴|전체\s*메뉴|회사\s*소개|고객\s*센터|파트너)",
    r"^(공지\s*사항|전체\s*방문자|최근\s*댓글|티스토리\s*툴바)",
    r"^(용어\s*집|서비스\s*소개|솔루션|제품\s*소개)$",
]
_H2_BLACKLIST_RE = [re.compile(pat, re.IGNORECASE) for pat in _H2_BLACKLIST_PATTERNS]

# 한국어/영어/숫자/공통 기호 외 문자 비율로 외국어 감지
_KOREAN_ENGLISH_RE = re.compile(r"[\uAC00-\uD7A3a-zA-Z0-9\s\-_.,!?:;()\[\]{}\"'/%&#+@~·•…→←↑↓]")


def _is_noise_h2(text: str) -> bool:
    """H2 텍스트가 UI 노이즈인지 판별."""
    t = text.strip()
    if len(t) < 2 or len(t) > 80:
        return True
    t_lower = t.lower()
    if any(pat.match(t_lower) for pat in _H2_BLACKLIST_RE):
        return True
    # 숫자만, 특수문자만
    if re.match(r'^[\d\s\W]+$', t):
        return True
    # 외국어 비율 감지 (한국어/영어 외 문자 40%+ → 노이즈)
    if len(t) >= 4:
        non_kr_en = len(_KOREAN_ENGLISH_RE.sub("", t))
        if non_kr_en / len(t) > 0.4:
            return True
    return False


_MAX_H2_PER_PAGE = 10    # 단일 페이지에서 가져올 h2 최대 수
_MAX_H2_PER_CLUSTER = 30  # 클러스터당 h2 최대 수


def _stage2d_content_gap(
    google_content_metas: dict[str, list[dict]],
) -> dict[str, list[str]]:
    """구글 상위 콘텐츠에서 H2 주제 목록 추출 (노이즈 필터 + 개수 제한)."""
    h2_topics: dict[str, list[str]] = {}
    for kw, metas in google_content_metas.items():
        seen: set[str] = set()
        deduped: list[str] = []
        for meta in metas:
            page_count = 0
            for h2 in meta.get("h2_structure", []):
                if page_count >= _MAX_H2_PER_PAGE:
                    break
                if h2 not in seen and not _is_noise_h2(h2):
                    seen.add(h2)
                    deduped.append(h2)
                    page_count += 1
            if len(deduped) >= _MAX_H2_PER_CLUSTER:
                break
        h2_topics[kw] = deduped[:_MAX_H2_PER_CLUSTER]
    return h2_topics


# -- 2-3: 구글 SERP 피처 --

def _stage2e_google_serp_features(
    reps: list[str],
    serp_cache: dict[str, str],
    *,
    paa_questions: dict[str, list[str]] | None = None,
) -> dict[str, dict]:
    """구글 SERP 피처: AI Overview, Featured Snippet, PAA."""
    paa_qs = paa_questions or {}
    result: dict[str, dict] = {}
    for kw in reps:
        raw = serp_cache.get(kw, "{}")
        sf = _extract_serp_features(raw)
        # PAA 질문: Stage 1 수집 데이터 주입 (google_paa 도구 결과)
        kw_paa = paa_qs.get(kw, sf.get("paa_questions", []))
        result[kw] = {
            "ai_overview": sf.get("ai_overview", False),
            "featured_snippet_exists": sf.get("featured_snippet_exists", False),
            "featured_snippet_url": sf.get("featured_snippet_url"),
            "paa_questions": kw_paa,
        }
    return result


# -- 2-3: 네이버 SERP 피처 --

async def _stage2e_naver_serp_features(
    reps: list[str],
    *,
    safe_tool_call: SafeToolCall,
    naver_serp_features_fn: Callable,
) -> dict[str, dict]:
    """네이버 SERP 피처: 지식스니펫, 스마트블록 수집."""
    result: dict[str, dict] = {}
    tasks = [
        safe_tool_call(
            f"naver_serp({kw})",
            naver_serp_features_fn(kw),
            json.dumps({
                "keyword": kw,
                "knowledge_snippet": False,
                "smart_block": False,
                "smart_block_components": [],
            }),
        )
        for kw in reps
    ]
    raw_results = await asyncio.gather(*tasks)
    for kw, raw in zip(reps, raw_results):
        try:
            data = json.loads(raw)
            result[kw] = {
                "knowledge_snippet": data.get("knowledge_snippet", False),
                "smart_block": data.get("smart_block", False),
                "smart_block_components": data.get(
                    "smart_block_components", [],
                ),
            }
        except (json.JSONDecodeError, TypeError):
            result[kw] = {
                "knowledge_snippet": False,
                "smart_block": False,
                "smart_block_components": [],
            }
    return result
