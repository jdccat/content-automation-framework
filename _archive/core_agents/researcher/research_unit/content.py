"""리서치 유닛: 상위 콘텐츠 수집 + H2 갭 분석."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable

from core.agents.researcher.parser import (
    _chunk,
    _extract_items_from_serp,
    _is_relevant_serp_item,
    _map_content_type,
    _parse_web_fetch_result,
)

logger = logging.getLogger(__name__)

SafeToolCall = Callable[..., Awaitable]


async def prefetch_serp(
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


async def collect_google_content(
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


async def collect_naver_content(
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


# -- H2 주제 목록 (데이터 조립, 도구 호출 없음) --

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

_KOREAN_ENGLISH_RE = re.compile(
    r"[\uAC00-\uD7A3a-zA-Z0-9\s\-_.,!?:;()\[\]{}\"'/%&#+@~·•…→←↑↓]"
)


def _is_noise_h2(text: str) -> bool:
    """H2 텍스트가 UI 노이즈인지 판별."""
    t = text.strip()
    if len(t) < 2 or len(t) > 80:
        return True
    t_lower = t.lower()
    if any(pat.match(t_lower) for pat in _H2_BLACKLIST_RE):
        return True
    if re.match(r'^[\d\s\W]+$', t):
        return True
    if len(t) >= 4:
        non_kr_en = len(_KOREAN_ENGLISH_RE.sub("", t))
        if non_kr_en / len(t) > 0.4:
            return True
    return False


_MAX_H2_PER_PAGE = 10
_MAX_H2_PER_CLUSTER = 30


def extract_h2_topics(
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
