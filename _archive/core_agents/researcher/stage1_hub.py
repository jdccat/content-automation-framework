"""Stage 1 허브 키워드 리서치 — 메인 키워드 중심 Top-down 파이프라인.

Pipeline:
  A. 시드별 키워드 확장 + PAA 분리
  B. 메인 키워드 친화도 필터 + LLM 도메인 필터
  C. 클러스터링 (중복 제거) + 대표 선정
  D. 대표 키워드 → research_unit 1회 호출
  E. 시드별 결과 분배 + 멤버 볼륨 전파 + PAA 병합
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from core.agents.researcher.parser import _normalize_keyword
from core.agents.researcher.research_unit.runner import run_research_unit
from core.agents.researcher.stage1 import (
    _domain_filter,
    _rule_based_prefilter,
    _stage1a_collect_keywords,
    _stage1a_longtail_expansion,
    _stage1d_llm_clustering,
    _stage1e_llm_representative,
    deduplicate_keywords,
)
from core.schemas import (
    ClusterDraft,
    HubResearchData,
    PROFILE_FULL,
    PROFILE_UMBRELLA,
    ResearchProfile,
    ResearchUnitOutput,
    SeedQuestion,
)

logger = logging.getLogger(__name__)

SafeToolCall = Callable[..., Awaitable]
LlmCall = Callable[..., Awaitable[str]]

# ── 상수 ──────────────────────────────────────────────────────

DEFAULT_MAX_KEYWORDS = 80
DEFAULT_MAX_REPRESENTATIVES = 15

# PAA 질문 감지 패턴 (한국어 질문 어미 + 물음표)
_PAA_RE = re.compile(
    r"(?:인가요|[ㄴ는]가요|나요|까요|세요|ㅂ니까|습니까|ㄹ까요)\s*\??$"
    r"|\?$"
)


# ── 비활성화 도구 ────────────────────────────────────────────────


_noop_future: asyncio.Future | None = None


def _noop_tool_fn(*args, **kwargs):
    """비활성화된 도구 — 빈 결과 반환."""
    global _noop_future  # noqa: PLW0603
    if _noop_future is None or _noop_future.done():
        loop = asyncio.get_event_loop()
        _noop_future = loop.create_future()
        _noop_future.set_result("{}")
    return _noop_future


# ── LLM 시드 추출 ────────────────────────────────────────────────

_SEED_EXTRACT_SYSTEM = """\
당신은 SEO 키워드 전문가입니다. 사용자 질문에서 검색 도구에 입력할 시드 키워드를 추출합니다.

## 규칙

1. **main_keyword**: 이 질문의 핵심 검색어 1개. 사람이 실제로 검색할 자연스러운 구문.
   - "앱 개발 견적이 업체마다 다른 이유" → "앱 개발 견적"
   - "ERP 외주 개발 업체를 고를 때 기준" → "ERP 외주 개발 업체"
   - 조사·어미·수식어 제거. 2~4토큰 명사 구문.

2. **seeds**: 검색 도구 확장용 시드 키워드 3~8개.
   - main_keyword 포함 필수.
   - 질문에서 파생 가능한 관련 검색어 (비용, 후기, 비교 등).
   - 각각 2~4토큰 명사 구문. 실제 검색 가능한 자연어.

3. 제외: 1토큰 일반명사(방법, 기준, 이유), 동사, 조사, 질문 어미.

## 입력 형식
질문: {question}
의도: {intent}
방향성: {direction}

## 출력 형식 (JSON만, 설명 없음)
{{"main_keyword": "...", "seeds": ["...", "..."]}}"""


async def _llm_extract_seeds(
    question: str,
    intent: str,
    direction: str,
    *,
    llm_call_fn: LlmCall,
) -> tuple[str, list[str]]:
    """LLM으로 질문에서 시드 키워드 + 메인 키워드 추출.

    Returns:
        (main_keyword, seeds)
    """
    system = _SEED_EXTRACT_SYSTEM.format(
        question=question, intent=intent, direction=direction,
    )
    user_msg = question

    raw = await llm_call_fn("seed_extract", system, user_msg, max_tokens=512)
    if not raw:
        logger.warning("시드 추출 LLM 응답 비어있음 — 폴백")
        return "", []

    try:
        data = json.loads(raw)
        main_kw = str(data.get("main_keyword", "")).strip()
        seeds = [str(s).strip() for s in data.get("seeds", []) if str(s).strip()]

        # main_keyword가 seeds에 없으면 선두에 추가
        if main_kw and main_kw not in seeds:
            seeds.insert(0, main_kw)

        logger.info("LLM 시드 추출: main=%s, seeds=%s", main_kw, seeds)
        return main_kw, seeds
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        logger.warning("시드 추출 파싱 실패: %s — 폴백", e)
        return "", []


# ── 고립 재그룹핑 ──────────────────────────────────────────────


def _fuzzy_token_overlap(set1: set[str], set2: set[str]) -> bool:
    """두 토큰 집합 간 퍼지 겹침 — 부분 문자열 포함 (2글자+)."""
    for t1 in set1:
        for t2 in set2:
            if len(t1) >= 2 and len(t2) >= 2:
                if t1 in t2 or t2 in t1:
                    return True
    return False


def _regroup_orphans(
    clusters: list[ClusterDraft],
    orphans: list[str],
    main_keyword: str,
) -> tuple[list[ClusterDraft], list[str]]:
    """고립 키워드 후처리 — 재그룹핑 + 기존 클러스터 병합.

    1. 메인 토큰 제외한 고유 토큰으로 고립 간 퍼지 매칭 → 미니 클러스터 생성
    2. 매칭 안 된 고립 → 기존 클러스터 중 고유 토큰 겹침 가장 높은 곳에 병합
    3. 어디에도 못 붙으면 고립 유지

    Returns:
        (updated_clusters, remaining_orphans)
    """
    if not orphans:
        return clusters, []

    main_tokens = set(_normalize_keyword(main_keyword).split())

    # 각 고립의 고유 토큰 (메인 토큰 제외)
    orphan_distinctive: dict[str, set[str]] = {}
    for kw in orphans:
        tokens = set(_normalize_keyword(kw).split()) - main_tokens
        orphan_distinctive[kw] = tokens

    # ── 1. 고립 간 퍼지 매칭으로 그룹핑 ─────────────
    used: set[int] = set()
    new_groups: list[list[str]] = []

    for i, kw1 in enumerate(orphans):
        if i in used or not orphan_distinctive[kw1]:
            continue
        group = [kw1]
        used.add(i)
        for j, kw2 in enumerate(orphans):
            if j in used or not orphan_distinctive[kw2]:
                continue
            if _fuzzy_token_overlap(orphan_distinctive[kw1], orphan_distinctive[kw2]):
                group.append(kw2)
                used.add(j)
        if len(group) >= 2:
            new_groups.append(group)
        else:
            used.discard(i)

    # 미니 클러스터 생성
    for group in new_groups:
        cid = f"c{len(clusters):03d}"
        kw_tuples = [(kw, "orphan") for kw in group]
        cd = ClusterDraft(cluster_id=cid, keywords=kw_tuples, representative=group[0])
        clusters.append(cd)
        logger.info("고립 재그룹핑: %s → %s (%d개)", cid, group, len(group))

    # ── 2. 남은 고립 → 기존 클러스터에 병합 시도 ─────
    ungrouped = [kw for i, kw in enumerate(orphans) if i not in used]
    remaining: list[str] = []

    for kw in ungrouped:
        kw_dist = orphan_distinctive[kw]
        if not kw_dist:
            remaining.append(kw)
            continue

        best_cluster: ClusterDraft | None = None
        best_score = 0
        for cd in clusters:
            cluster_dist: set[str] = set()
            for member, _ in cd.keywords:
                cluster_dist.update(
                    set(_normalize_keyword(member).split()) - main_tokens
                )
            if _fuzzy_token_overlap(kw_dist, cluster_dist):
                # 정확 겹침 수 계산
                score = len(kw_dist & cluster_dist)
                if score > best_score:
                    best_score = score
                    best_cluster = cd

        if best_cluster:
            best_cluster.keywords.append((kw, "orphan"))
            logger.info(
                "고립 병합: %s → %s", kw, best_cluster.cluster_id,
            )
        else:
            remaining.append(kw)

    return clusters, remaining


# ── 우산 키워드 분리 ──────────────────────────────────────────


def _separate_umbrella(
    reps: list[str],
    main_keyword: str,
) -> tuple[list[str], list[str]]:
    """메인 키워드의 토큰 부분집합인 우산 키워드 분리.

    "ERP 외주 개발 업체" 기준:
      "erp 외주" → 부분집합 → 우산 (리서치 제외, 볼륨만)
      "erp 외주 가격" → 비부분집합 → 리서치 대상

    Returns:
        (research_reps, umbrella_kws)
    """
    main_tokens = set(_normalize_keyword(main_keyword).split())
    research: list[str] = []
    umbrella: list[str] = []

    for r in reps:
        r_tokens = set(_normalize_keyword(r).split())
        if r_tokens.issubset(main_tokens) and len(r_tokens) < len(main_tokens):
            umbrella.append(r)
        else:
            research.append(r)

    if umbrella:
        logger.info(
            "우산 키워드 분리: %s (볼륨만, 리서치 제외)", umbrella,
        )

    return research, umbrella


# ── 메인 키워드 친화도 필터 ─────────────────────────────────────


def _affinity_filter(
    keywords: list[tuple[str, str]],
    main_keyword: str,
    *,
    min_overlap_ratio: float = 0.5,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """메인 키워드 헤드 구문 + 토큰 겹침 기반 친화도 필터.

    1. 메인 키워드 코어 토큰 추출
    2. 헤드 구문 = 상위 절반 토큰 (최소 1, 최대 3) — 도메인 범위 한정
    3. 헤드 구문 토큰 전부 포함 AND 겹침 비율 ≥ threshold → 통과

    예시: 메인 "ERP 외주 개발 업체" → 헤드 {"erp", "외주"}
      "erp 외주 가격" → 헤드 포함 + 2/4=50% → ✅
      "외주 개발 비용" → "erp" 미포함 → ❌
      "erp 웹" → "외주" 미포함 → ❌

    Returns:
        (passed, dropped)
    """
    core_tokens = _normalize_keyword(main_keyword).split()
    if not core_tokens:
        return keywords, []

    # 헤드 구문: 메인 키워드 상위 절반 (최소 1, 최대 3)
    head_len = max(1, min(3, -(-len(core_tokens) // 2)))  # ceil division
    head_tokens = set(core_tokens[:head_len])

    passed: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []

    for kw, src in keywords:
        nk_tokens = set(_normalize_keyword(kw).split())

        # 겹침 비율
        overlap = sum(1 for t in core_tokens if t in nk_tokens)
        ratio = overlap / len(core_tokens)

        # 헤드 구문 포함 여부
        has_head = head_tokens.issubset(nk_tokens)

        if has_head and ratio >= min_overlap_ratio:
            passed.append((kw, src))
        else:
            dropped.append((kw, src))

    logger.info(
        "친화도 필터: %d → %d (메인=%s, 헤드=%s)",
        len(keywords), len(passed), main_keyword, sorted(head_tokens),
    )
    return passed, dropped


# ── 시드 사전 필터 ─────────────────────────────────────────────


def _filter_seeds(seeds: list[str], question: str) -> tuple[list[str], list[str]]:
    """시드 확장 전 필터링 — 1토큰 시드 제거.

    2토큰+ 시드가 존재하면 1토큰 시드를 제거한다.
    모든 시드가 1토큰이면 원본 질문을 시드로 사용.

    Returns:
        (filtered_seeds, removed_seeds)
    """
    multi_token = [s for s in seeds if len(s.split()) >= 2]
    single_token = [s for s in seeds if len(s.split()) < 2]

    if multi_token:
        return multi_token, single_token

    # 모든 시드가 1토큰 → 질문 전체를 시드로
    return ([question] if question else seeds), single_token


# ── PAA 분리 ──────────────────────────────────────────────────


def _separate_paa(
    deduped: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """키워드 목록에서 PAA 질문 형태를 분리.

    Returns:
        (seo_keywords, paa_items) — 각각 (keyword, source) 튜플 리스트.
    """
    seo: list[tuple[str, str]] = []
    paa: list[tuple[str, str]] = []
    for kw, src in deduped:
        if _PAA_RE.search(kw):
            paa.append((kw, src))
        else:
            seo.append((kw, src))
    return seo, paa


# ── 키워드 캡 ─────────────────────────────────────────────────


def _cap_keywords(
    deduped: list[tuple[str, str]],
    seeds: list[str],
    max_kw: int,
) -> list[tuple[str, str]]:
    """키워드 수 제한. 시드 토큰 겹침 높은 키워드 우선 보존."""
    if len(deduped) <= max_kw:
        return deduped

    seed_tokens: set[str] = set()
    for s in seeds:
        for tok in _normalize_keyword(s).split():
            if len(tok) >= 2:
                seed_tokens.add(tok)

    def _relevance(item: tuple[str, str]) -> int:
        tokens = _normalize_keyword(item[0]).split()
        return sum(1 for t in tokens if t in seed_tokens)

    scored = sorted(deduped, key=_relevance, reverse=True)
    logger.info("키워드 캡 적용: %d → %d", len(deduped), max_kw)
    return scored[:max_kw]


# ── 대표 캡 ──────────────────────────────────────────────────


def _cap_representatives(
    clusters: list[ClusterDraft],
    orphans: list[str],
    volumes: dict[str, int],
    max_reps: int,
) -> list[str]:
    """클러스터 대표 + 볼륨 있는 고립 키워드를 max_reps 이내로 제한."""
    reps: list[str] = [cd.representative for cd in clusters if cd.representative]

    # 고립 키워드 중 볼륨 있는 것만 추가 (남는 슬롯만큼)
    remaining = max(0, max_reps - len(reps))
    if remaining and orphans:
        orphans_scored = sorted(
            orphans,
            key=lambda kw: volumes.get(_normalize_keyword(kw), 0),
            reverse=True,
        )
        reps.extend(orphans_scored[:remaining])

    # 전체 캡
    if len(reps) > max_reps:
        reps.sort(
            key=lambda kw: volumes.get(_normalize_keyword(kw), 0),
            reverse=True,
        )
        reps = reps[:max_reps]
        logger.info("대표 캡 적용: → %d", max_reps)

    return reps


# ── 스냅샷 헬퍼 ───────────────────────────────────────────────


def _save_hub_snapshot(
    name: str, data: dict, run_date: str, snapshot_dir: str,
) -> Path:
    """허브 스냅샷 저장."""
    d = Path(snapshot_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{run_date}_{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("스냅샷 저장: %s", path)
    return path


# ── 허브 리서치 메인 ──────────────────────────────────────────


async def stage1_hub_research(
    seed_questions: list[SeedQuestion],
    *,
    config: dict,
    safe_tool_call: SafeToolCall,
    llm_call_fn: LlmCall,
    search_suggestions_fn: Callable,
    google_related_fn: Callable,
    google_paa_fn: Callable,
    naver_keyword_volume_fn: Callable,
    google_search_fn: Callable,
    google_keyword_trend_fn: Callable,
    naver_keyword_trend_fn: Callable,
    naver_blog_search_fn: Callable,
    web_fetch_fn: Callable,
    naver_serp_features_fn: Callable,
    ai_search_fn: Callable | None = None,
    perplexity_search_fn: Callable | None = None,
    geo_claude_fn: Callable | None = None,
    geo_gemini_fn: Callable | None = None,
    profile: ResearchProfile | None = None,
    snapshot_dir: str = "",
    run_date: str = "",
) -> list[HubResearchData]:
    """시드 질문별 허브 키워드 리서치 — 메인 키워드 중심 Top-down."""
    if not seed_questions:
        return []

    profile = profile or PROFILE_FULL
    hub_cfg = config.get("hub", {})
    max_kw = hub_cfg.get("max_keywords", DEFAULT_MAX_KEYWORDS)
    max_reps = hub_cfg.get("max_representatives", DEFAULT_MAX_REPRESENTATIVES)
    affinity_cfg = hub_cfg.get("affinity", {})
    min_overlap = affinity_cfg.get("min_overlap_ratio", 0.5)

    logger.info(
        "Stage 1 허브 리서치 시작: seeds=%d, max_kw=%d, max_reps=%d",
        len(seed_questions), max_kw, max_reps,
    )

    # ── A. 시드별 키워드 확장 (PAA는 research_unit에서 수집) ──

    seeds_snapshot: list[dict] = []
    seed_seo_map: dict[str, list[tuple[str, str]]] = {}
    seed_paa_map: dict[str, list[str]] = {}
    seed_volumes_map: dict[str, dict[str, int]] = {}
    seed_filtered_map: dict[str, list[str]] = {}
    seed_main_kw_map: dict[str, str] = {}

    for sq in seed_questions:
        intent = sq.intent[0] if sq.intent else ""
        direction = sq.content_direction[0] if sq.content_direction else ""

        # A-0: LLM 시드 추출 (규칙 기반 폴백)
        main_kw, llm_seeds = await _llm_extract_seeds(
            sq.question, intent, direction,
            llm_call_fn=llm_call_fn,
        )

        if llm_seeds:
            filtered_seeds = llm_seeds
        else:
            # 폴백: 규칙 기반 추출
            from core.agents.researcher.parser import _extract_keywords_from_question

            raw_seeds = _extract_keywords_from_question(sq.question)
            if not raw_seeds:
                raw_seeds = [sq.question]
            filtered_seeds, _ = _filter_seeds(raw_seeds, sq.question)
            if not main_kw:
                main_kw = filtered_seeds[0] if filtered_seeds else ""

        seed_main_kw_map[sq.seed_id] = main_kw

        # A-1: 도구 기반 확장 (PAA 비활성 — research_unit에서 수집)
        pool = await _stage1a_collect_keywords(
            filtered_seeds, intent, direction,
            safe_tool_call=safe_tool_call,
            search_suggestions_fn=search_suggestions_fn,
            google_related_fn=google_related_fn,
            google_paa_fn=_noop_tool_fn,
            naver_keyword_volume_fn=naver_keyword_volume_fn,
            llm_call_fn=llm_call_fn,
            config=config,
        )

        # A-2: 롱테일 확장
        await _stage1a_longtail_expansion(
            pool, filtered_seeds,
            config=config,
            safe_tool_call=safe_tool_call,
            search_suggestions_fn=search_suggestions_fn,
        )

        # A-3: 중복 제거 + 규칙 필터
        deduped = deduplicate_keywords(pool)
        deduped = _rule_based_prefilter(deduped, filtered_seeds)

        # A-4: 질문형 키워드 분리 (클러스터링 오염 방지)
        seo_deduped, paa_items = _separate_paa(deduped)

        seed_seo_map[sq.seed_id] = seo_deduped
        seed_paa_map[sq.seed_id] = [kw for kw, _ in paa_items]
        seed_volumes_map[sq.seed_id] = dict(pool.volumes)
        seed_filtered_map[sq.seed_id] = filtered_seeds

        logger.info(
            "[%s] main=%s | 시드: %d | SEO %d + 질문형 %d",
            sq.seed_id, main_kw,
            len(filtered_seeds),
            len(seo_deduped), len(paa_items),
        )
        seeds_snapshot.append({
            "seed_id": sq.seed_id,
            "question": sq.question,
            "main_keyword": main_kw,
            "seeds": filtered_seeds,
        })

    # 스냅샷 0: 시드 + 메인 키워드
    if snapshot_dir and run_date:
        _save_hub_snapshot("hub_0_seeds", {
            "seed_count": len(seed_questions),
            "seeds": seeds_snapshot,
        }, run_date, snapshot_dir)

    # ── B. 친화도 필터 + LLM 도메인 필터 + 캡 ────────────────

    seed_affinity_map: dict[str, dict] = {}  # 스냅샷용

    for sq in seed_questions:
        seo_deduped = seed_seo_map.get(sq.seed_id, [])
        filtered_seeds = seed_filtered_map.get(sq.seed_id, [])
        main_kw = seed_main_kw_map.get(sq.seed_id, "")

        if not seo_deduped:
            seed_affinity_map[sq.seed_id] = {
                "before": 0, "after": 0, "dropped": 0,
            }
            continue

        # B-1: 메인 키워드 친화도 필터
        affinity_passed, affinity_dropped = _affinity_filter(
            seo_deduped, main_kw,
            min_overlap_ratio=min_overlap,
        )

        seed_affinity_map[sq.seed_id] = {
            "before": len(seo_deduped),
            "after_affinity": len(affinity_passed),
            "dropped_count": len(affinity_dropped),
            "dropped_samples": [kw for kw, _ in affinity_dropped[:20]],
        }

        intent = sq.intent[0] if sq.intent else ""
        direction = sq.content_direction[0] if sq.content_direction else ""

        # B-2: LLM 도메인 필터 (잔여 노이즈 정리)
        if affinity_passed:
            affinity_passed = await _domain_filter(
                affinity_passed, filtered_seeds, intent, direction,
                llm_call_fn=llm_call_fn, config=config,
            )
            logger.info(
                "[%s] LLM 도메인 필터 후: %d개", sq.seed_id, len(affinity_passed),
            )

        # B-3: 키워드 캡
        affinity_passed = _cap_keywords(
            affinity_passed,
            filtered_seeds + ([main_kw] if main_kw else []),
            max_kw,
        )

        seed_seo_map[sq.seed_id] = affinity_passed

    # 스냅샷 1: 키워드 확장 + 친화도 필터 결과
    if snapshot_dir and run_date:
        kw_snap: dict = {"per_seed": {}}
        for sq in seed_questions:
            seo = seed_seo_map.get(sq.seed_id, [])
            paa = seed_paa_map.get(sq.seed_id, [])
            aff = seed_affinity_map.get(sq.seed_id, {})
            kw_snap["per_seed"][sq.seed_id] = {
                "question": sq.question,
                "main_keyword": seed_main_kw_map.get(sq.seed_id, ""),
                "affinity": aff,
                "seo_keywords": [kw for kw, _ in seo],
                "seo_count": len(seo),
                "paa_questions": paa,
                "paa_count": len(paa),
            }
        _save_hub_snapshot("hub_1_keywords", kw_snap, run_date, snapshot_dir)

    # ── C. 클러스터링 (중복 제거) + 대표 선정 ────────────────

    seed_cluster_map: dict[str, tuple[list[ClusterDraft], list[str]]] = {}
    seed_reps_map: dict[str, list[str]] = {}
    seed_umbrella_map: dict[str, list[str]] = {}
    all_representatives: list[str] = []

    for sq in seed_questions:
        seo_deduped = seed_seo_map.get(sq.seed_id, [])
        if not seo_deduped:
            seed_cluster_map[sq.seed_id] = ([], [])
            seed_reps_map[sq.seed_id] = []
            continue

        # C-1: LLM 의미 기반 클러스터링
        clusters, orphans = await _stage1d_llm_clustering(
            seo_deduped, llm_call_fn=llm_call_fn, config=config,
        )
        logger.info(
            "[%s] 클러스터링: %d 클러스터, %d 고립",
            sq.seed_id, len(clusters), len(orphans),
        )

        # C-1b: 고립 키워드 재그룹핑 (퍼지 매칭으로 병합)
        main_kw = seed_main_kw_map.get(sq.seed_id, "")
        clusters, orphans = _regroup_orphans(clusters, orphans, main_kw)
        if orphans:
            logger.info("[%s] 재그룹핑 후 잔여 고립: %d", sq.seed_id, len(orphans))

        # C-2: 대표 키워드 선정
        volumes = seed_volumes_map.get(sq.seed_id, {})
        await _stage1e_llm_representative(
            clusters, volumes, llm_call_fn=llm_call_fn, config=config,
        )

        # C-3: 대표 캡
        reps = _cap_representatives(clusters, orphans, volumes, max_reps)

        # C-4: 우산 키워드 분리 (메인 키워드 부분집합 → 별도 프로파일로 수집)
        research_reps, umbrella_kws = _separate_umbrella(reps, main_kw)

        seed_cluster_map[sq.seed_id] = (clusters, orphans)
        seed_reps_map[sq.seed_id] = research_reps
        seed_umbrella_map[sq.seed_id] = umbrella_kws
        all_representatives.extend(research_reps)

    # 대표 중복 제거 (시드 간)
    seen_reps: set[str] = set()
    unique_reps: list[str] = []
    for r in all_representatives:
        nr = _normalize_keyword(r)
        if nr not in seen_reps:
            seen_reps.add(nr)
            unique_reps.append(r)

    logger.info("전체 대표 키워드: %d개", len(unique_reps))

    # 스냅샷 2: 클러스터링 결과
    if snapshot_dir and run_date:
        cl_snap: dict = {
            "total_representatives": len(unique_reps),
            "representatives": unique_reps,
            "per_seed": {},
        }
        for sq in seed_questions:
            clusters, orphans = seed_cluster_map.get(sq.seed_id, ([], []))
            cl_snap["per_seed"][sq.seed_id] = {
                "question": sq.question,
                "main_keyword": seed_main_kw_map.get(sq.seed_id, ""),
                "representatives": seed_reps_map.get(sq.seed_id, []),
                "umbrella_keywords": seed_umbrella_map.get(sq.seed_id, []),
                "clusters": [
                    {
                        "id": cd.cluster_id,
                        "representative": cd.representative,
                        "shared_intent": cd.shared_intent,
                        "members": [kw for kw, _ in cd.keywords],
                    }
                    for cd in clusters
                ],
                "orphans": orphans,
            }
        _save_hub_snapshot("hub_2_clusters", cl_snap, run_date, snapshot_dir)

    if not unique_reps:
        return [
            HubResearchData(seed_id=sq.seed_id, seed_question=sq.question)
            for sq in seed_questions
        ]

    # ── D. research_unit 호출 (대표 + 우산) ──────────────────

    # D-1: 공통 도구 인자
    _ru_tools = dict(
        safe_tool_call=safe_tool_call,
        google_search_fn=google_search_fn,
        naver_keyword_volume_fn=naver_keyword_volume_fn,
        google_keyword_trend_fn=google_keyword_trend_fn,
        naver_keyword_trend_fn=naver_keyword_trend_fn,
        naver_blog_search_fn=naver_blog_search_fn,
        web_fetch_fn=web_fetch_fn,
        naver_serp_features_fn=naver_serp_features_fn,
        search_suggestions_fn=search_suggestions_fn,
        google_related_fn=google_related_fn,
        google_paa_fn=google_paa_fn,
        ai_search_fn=ai_search_fn,
        perplexity_search_fn=perplexity_search_fn,
        geo_claude_fn=geo_claude_fn,
        geo_gemini_fn=geo_gemini_fn,
    )

    # D-2: 대표 키워드 → 메인 프로파일
    research = await run_research_unit(
        unique_reps, profile,
        **_ru_tools,
        snapshot_dir=snapshot_dir,
        run_date=run_date,
    )

    # D-3: 우산 키워드 → PROFILE_UMBRELLA (volumes + serp + paa)
    #       메인 프로파일과 교집합하여 비활성 모듈은 우산에서도 스킵
    all_umbrella: list[str] = []
    for sq in seed_questions:
        all_umbrella.extend(seed_umbrella_map.get(sq.seed_id, []))

    umbrella_research = ResearchUnitOutput()
    umb_profile = ResearchProfile(
        volumes=profile.volumes and PROFILE_UMBRELLA.volumes,
        related_keywords=profile.related_keywords and PROFILE_UMBRELLA.related_keywords,
        paa=profile.paa and PROFILE_UMBRELLA.paa,
        content=profile.content and PROFILE_UMBRELLA.content,
        serp_features=profile.serp_features and PROFILE_UMBRELLA.serp_features,
        geo=profile.geo and PROFILE_UMBRELLA.geo,
    )
    umb_any = umb_profile.volumes or umb_profile.serp_features or umb_profile.paa
    if all_umbrella and umb_any:
        logger.info("우산 키워드 리서치: %d개 → %s", len(all_umbrella), umb_profile)
        umbrella_research = await run_research_unit(
            all_umbrella, umb_profile,
            **_ru_tools,
        )

    # ── E. 시드별 결과 분배 + 볼륨 전파 + 우산 병합 ──────────

    result: list[HubResearchData] = []
    for sq in seed_questions:
        clusters, orphans = seed_cluster_map.get(sq.seed_id, ([], []))
        reps = seed_reps_map.get(sq.seed_id, [])
        umbrella_kws = seed_umbrella_map.get(sq.seed_id, [])

        # 이 시드의 모든 키워드 (클러스터 멤버 + 고립 + 우산)
        all_kws: list[str] = []
        for cd in clusters:
            for kw, _ in cd.keywords:
                all_kws.append(kw)
        all_kws.extend(orphans)
        all_kws.extend(umbrella_kws)

        # 대표 기준으로 리서치 슬라이스
        sliced = _slice_research_by_keywords(research, reps)

        # 우산 키워드 리서치 병합
        if umbrella_kws:
            umb_sliced = _slice_research_by_keywords(umbrella_research, umbrella_kws)
            sliced.volumes.update(umb_sliced.volumes)
            sliced.paa_questions.update(umb_sliced.paa_questions)
            sliced.google_serp_features.update(umb_sliced.google_serp_features)
            sliced.naver_serp_features.update(umb_sliced.naver_serp_features)

        # 멤버 키워드에 볼륨 데이터 전파 (확장 단계에서 수집된 것)
        expansion_vols = seed_volumes_map.get(sq.seed_id, {})
        for kw in all_kws:
            nk = _normalize_keyword(kw)
            if nk not in sliced.volumes and nk in expansion_vols:
                sliced.volumes[nk] = expansion_vols[nk]

        result.append(HubResearchData(
            seed_id=sq.seed_id,
            seed_question=sq.question,
            keywords=all_kws,
            research=sliced,
        ))

    logger.info("Stage 1 허브 리서치 완료: %d 시드", len(result))
    return result


# ── 리서치 슬라이서 ───────────────────────────────────────────


def _slice_research_by_keywords(
    research: ResearchUnitOutput,
    keywords: list[str],
) -> ResearchUnitOutput:
    """전체 리서치 출력에서 지정된 키워드에 해당하는 데이터만 추출."""
    norm_set = {_normalize_keyword(kw) for kw in keywords}

    def _match(key: str) -> bool:
        return _normalize_keyword(key) in norm_set

    return ResearchUnitOutput(
        volumes={k: v for k, v in research.volumes.items() if _match(k)},
        related_keywords={k: v for k, v in research.related_keywords.items() if _match(k)},
        paa_questions={k: v for k, v in research.paa_questions.items() if _match(k)},
        google_content_metas={k: v for k, v in research.google_content_metas.items() if _match(k)},
        naver_content_metas={k: v for k, v in research.naver_content_metas.items() if _match(k)},
        h2_topics={k: v for k, v in research.h2_topics.items() if _match(k)},
        google_serp_features={k: v for k, v in research.google_serp_features.items() if _match(k)},
        naver_serp_features={k: v for k, v in research.naver_serp_features.items() if _match(k)},
        geo_citations={k: v for k, v in research.geo_citations.items() if _match(k)},
    )
