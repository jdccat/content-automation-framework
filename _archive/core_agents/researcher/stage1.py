"""1단계: 키워드 확장 및 클러스터링."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from core.agents.researcher.archive import (
    load_archive_clusters,
    load_archive_reps,
)
from core.agents.researcher.parser import (
    _chunk,
    _normalize_keyword,
)
from core.agents.researcher.prompts import load_prompt
from core.schemas import (
    ClusterDraft,
    RawKeywordPool,
    Stage1Output,
)

logger = logging.getLogger(__name__)

SafeToolCall = Callable[..., Awaitable]
LlmCall = Callable[..., Awaitable[str]]


async def safe_tool_call(label: str, coro, default=None):
    """NotImplementedError/일반 에러 격리. 파이프라인은 계속 진행."""
    try:
        return await coro
    except NotImplementedError:
        logger.info("[%s] 미구현 — 스킵", label)
        return default
    except Exception as e:
        logger.warning("[%s] 실패: %s", label, e)
        return default


async def llm_call(
    label: str, system: str, user: str,
    config: dict,
    model: str = "", max_tokens: int = 4096,
    retries: int = 2,
) -> str:
    """OpenAI Chat Completions 호출. 빈 응답/실패 시 최대 retries회 재시도."""
    from openai import AsyncOpenAI

    model = model or config.get("models", {}).get("main", "gpt-5.2")
    client = AsyncOpenAI()
    kwargs: dict = {
        "model": model,
        "temperature": 0,
        "max_completion_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if "JSON" in system or "json" in system:
        kwargs["response_format"] = {"type": "json_object"}

    last_error: str = ""
    for attempt in range(1, retries + 2):  # 1회 시도 + retries회 재시도
        try:
            resp = await client.chat.completions.create(**kwargs)
            choice = resp.choices[0]
            content = choice.message.content or ""
            if choice.finish_reason == "length":
                logger.warning("[%s] 응답이 max_tokens에서 잘림 (truncated)", label)
            if content:
                return content
            # 빈 응답 — 재시도 대상
            last_error = f"빈 응답 (finish_reason={choice.finish_reason})"
            logger.warning("[%s] 시도 %d/%d 빈 응답 — %s",
                           label, attempt, retries + 1, last_error)
        except Exception as e:
            last_error = str(e)
            logger.warning("[%s] 시도 %d/%d 실패: %s",
                           label, attempt, retries + 1, e)
        if attempt <= retries:
            await asyncio.sleep(2 * attempt)  # 2초, 4초 백오프

    logger.error("[%s] %d회 시도 모두 실패: %s", label, retries + 1, last_error)
    return ""


# ── 1단계 진입점 ────────────────────────────────────────────────


async def stage1_expansion(
    main_kw: str,
    seeds: list[str],
    questions: list[str] | None = None,
    *,
    client: str = "",
    intent: str = "",
    direction: str = "",
    all_seeds: list[str] | None = None,
    config: dict,
    safe_tool_call_fn: SafeToolCall,
    llm_call_fn: LlmCall,
    load_archive_reps_fn: Callable[[], list[str]],
    load_archive_clusters_fn: Callable[[], dict[str, list[str]]],
    search_suggestions_fn: Callable,
    google_related_fn: Callable,
    google_paa_fn: Callable,
    naver_keyword_volume_fn: Callable,
    snapshot_dir: str = "",
    run_date: str = "",
) -> Stage1Output:
    logger.info("1단계 시작: seeds=%d", len(seeds))

    # 1-2: 플랫폼별 키워드 확장
    pool = await _stage1a_collect_keywords(
        seeds, intent, direction,
        all_seeds=all_seeds,
        safe_tool_call=safe_tool_call_fn,
        search_suggestions_fn=search_suggestions_fn,
        google_related_fn=google_related_fn,
        google_paa_fn=google_paa_fn,
        naver_keyword_volume_fn=naver_keyword_volume_fn,
        llm_call_fn=llm_call_fn,
        config=config,
    )

    # 1-2 롱테일 확장
    await _stage1a_longtail_expansion(
        pool, seeds,
        config=config,
        safe_tool_call=safe_tool_call_fn,
        search_suggestions_fn=search_suggestions_fn,
    )

    # 1-3: 내부 고객 언어 대조
    internal_kws = stage1b_customer_language(client, config)
    pool.internal_data.extend(internal_kws)

    # 스냅샷: 키워드 풀 (도구 수집 결과)
    if snapshot_dir and run_date:
        from core.agents.researcher.snapshot import (
            save_snapshot, save_deduped, save_stage1_sub,
        )
        save_snapshot("stage1_keywords", pool, run_date, snapshot_dir)

    # 1-4: 키워드 정리
    deduped = deduplicate_keywords(pool)
    if not deduped:
        logger.warning("1단계: 수집된 키워드 없음")
        return Stage1Output(
            paa_questions=pool.paa_questions,
            volumes=pool.volumes,
            volumes_pc=pool.volumes_pc,
            volumes_mobile=pool.volumes_mobile,
        )

    # 1-4b: 규칙 기반 사전 필터 → 도메인 필터
    deduped = _rule_based_prefilter(deduped, seeds)
    if deduped:
        deduped = await _domain_filter(
            deduped, seeds, intent, direction,
            llm_call_fn=llm_call_fn, config=config,
        )

    # 스냅샷: 중복 제거 + 관련성 필터 후
    if snapshot_dir and run_date:
        save_deduped(deduped, pool, run_date, snapshot_dir)

    # 클러스터링
    cluster_drafts, orphan_keywords = await _stage1d_llm_clustering(
        deduped, llm_call_fn=llm_call_fn, config=config,
    )

    # 스냅샷: 1d 클러스터링 직후
    if snapshot_dir and run_date:
        save_stage1_sub(
            "stage1d_clusters", cluster_drafts, orphan_keywords,
            pool, run_date, snapshot_dir,
        )

    # 대표 키워드 선정
    await _stage1e_llm_representative(
        cluster_drafts, pool.volumes,
        llm_call_fn=llm_call_fn, config=config,
    )

    # 스냅샷: 1e 대표 선정 후
    if snapshot_dir and run_date:
        save_stage1_sub(
            "stage1e_clusters", cluster_drafts, orphan_keywords,
            pool, run_date, snapshot_dir,
        )

    # 아카이브 비교
    await _stage1f_archive_comparison(
        cluster_drafts,
        llm_call_fn=llm_call_fn,
        config=config,
        load_archive_reps_fn=load_archive_reps_fn,
        load_archive_clusters_fn=load_archive_clusters_fn,
    )

    # 스냅샷: 1f 아카이브 비교 후
    if snapshot_dir and run_date:
        save_stage1_sub(
            "stage1f_clusters", cluster_drafts, orphan_keywords,
            pool, run_date, snapshot_dir,
        )

    # 1-5: 포커스 클러스터 선정
    await _stage1g_focus_selection(
        cluster_drafts, questions or [], pool.volumes,
        llm_call_fn=llm_call_fn, config=config,
    )

    logger.info(
        "1단계 완료: clusters=%d (focus=%d), orphans=%d",
        len(cluster_drafts),
        sum(1 for cd in cluster_drafts if cd.is_focus),
        len(orphan_keywords),
    )
    return Stage1Output(
        cluster_drafts=cluster_drafts,
        orphan_keywords=orphan_keywords,
        paa_questions=pool.paa_questions,
        volumes=pool.volumes,
        volumes_pc=pool.volumes_pc,
        volumes_mobile=pool.volumes_mobile,
    )


# -- 1a: 병렬 도구 수집 --

async def _stage1a_collect_keywords(
    seeds: list[str], intent: str = "", direction: str = "",
    *, all_seeds: list[str] | None = None,
    safe_tool_call: SafeToolCall,
    search_suggestions_fn: Callable,
    google_related_fn: Callable,
    google_paa_fn: Callable,
    naver_keyword_volume_fn: Callable,
    llm_call_fn: LlmCall,
    config: dict,
) -> RawKeywordPool:
    pool = RawKeywordPool()

    # 자동완성용 시드: 2토큰 이상 키워드만 사용 (단일토큰 노이즈 방지)
    ac_seeds = list(dict.fromkeys(
        s for s in seeds if len(s.split()) >= 2
    ))
    if not ac_seeds:
        ac_seeds = seeds[:5]
    logger.info("자동완성 시드 (2토큰+): %d/%d", len(ac_seeds), len(seeds))

    ac_tasks = [
        safe_tool_call(
            f"autocomplete({s})", search_suggestions_fn(s), "{}",
        )
        for s in ac_seeds
    ]
    related_tasks = [
        safe_tool_call(
            f"google_related({s})", google_related_fn(s), "{}",
        )
        for s in seeds
    ]
    paa_tasks = [
        safe_tool_call(
            f"google_paa({s})", google_paa_fn(s), "{}",
        )
        for s in seeds
    ]

    # naver_searchad: 5개씩 배치 병렬
    searchad_tasks = [
        safe_tool_call(
            f"naver_searchad({batch})",
            naver_keyword_volume_fn(batch),
            "{}",
        )
        for batch in _chunk(seeds, 5)
    ]

    all_results = await asyncio.gather(
        *ac_tasks, *related_tasks, *paa_tasks, *searchad_tasks,
    )

    n_ac = len(ac_seeds)
    n_seeds = len(seeds)
    offset = 0
    ac_results = all_results[offset : offset + n_ac]; offset += n_ac
    related_results = all_results[offset : offset + n_seeds]; offset += n_seeds
    paa_results = all_results[offset : offset + n_seeds]; offset += n_seeds
    searchad_results = all_results[offset :]

    # autocomplete 파싱 — 플랫폼별 분리
    for raw in ac_results:
        if not raw:
            continue
        try:
            data = json.loads(raw)
            pool.naver.extend(data.get("naver", []))
            pool.google.extend(data.get("google", []))
        except (json.JSONDecodeError, TypeError):
            pass

    # google_related 파싱
    for raw in related_results:
        if not raw:
            continue
        try:
            data = json.loads(raw)
            pool.google.extend(data.get("related_searches", []))
        except (json.JSONDecodeError, TypeError):
            pass

    # google_paa 파싱 — paa 소스로 별도 태깅
    for i, raw in enumerate(paa_results):
        if not raw:
            continue
        try:
            data = json.loads(raw)
            questions = data.get("questions", [])
            kw = data.get("keyword", seeds[i] if i < len(seeds) else "")
            if questions:
                pool.paa_questions[kw] = questions
                pool.paa.extend(questions)
        except (json.JSONDecodeError, TypeError):
            pass

    # naver_searchad 파싱 — PC/모바일 분리
    for raw in searchad_results:
        if not raw:
            continue
        try:
            data = json.loads(raw)
            for item in data.get("input_keywords", []):
                kw = item.get("keyword", "")
                vol = item.get("monthly_total", 0)
                pc = item.get("monthly_pc", 0)
                mobile = item.get("monthly_mobile", 0)
                if kw:
                    nk = _normalize_keyword(kw)
                    pool.volumes[nk] = vol
                    pool.volumes_pc[nk] = pc
                    pool.volumes_mobile[nk] = mobile
            for item in data.get("related_keywords", []):
                kw = item.get("keyword", "")
                vol = item.get("monthly_total", 0)
                pc = item.get("monthly_pc", 0)
                mobile = item.get("monthly_mobile", 0)
                if kw:
                    pool.related_from_searchad.append(kw)
                    nk = _normalize_keyword(kw)
                    pool.volumes[nk] = vol
                    pool.volumes_pc[nk] = pc
                    pool.volumes_mobile[nk] = mobile
        except (json.JSONDecodeError, TypeError):
            pass

    # searchad 연관 키워드 → 필터 없이 keyword_tool에 추가
    if pool.related_from_searchad:
        pool.keyword_tool.extend(pool.related_from_searchad)

    # 시드 자체도 키워드 풀에 추가 (구글 소스 기본)
    pool.google.extend(seeds)
    return pool


# -- 1a-lt: 롱테일 확장 (2토큰 → autocomplete 재투입 → 3토큰+) --

async def _stage1a_longtail_expansion(
    pool: RawKeywordPool, seeds: list[str],
    *, config: dict,
    safe_tool_call: SafeToolCall,
    search_suggestions_fn: Callable,
) -> None:
    """수집된 2토큰 키워드를 autocomplete에 재투입하여 3토큰+ 롱테일 추가."""
    max_n = config.get("longtail", {}).get("max_second_pass", 10)
    if max_n <= 0:
        return

    seed_set = {_normalize_keyword(s) for s in seeds}
    all_kws = pool.google + pool.naver + pool.keyword_tool
    existing = {_normalize_keyword(kw) for kw in all_kws}

    # 2토큰 키워드 중 시드와 겹치지 않는 것 선별
    candidates = [
        kw for kw in all_kws
        if len(kw.split()) == 2
        and _normalize_keyword(kw) not in seed_set
    ]
    # 중복 제거 후 상위 N개
    seen: set[str] = set()
    unique: list[str] = []
    for kw in candidates:
        n = _normalize_keyword(kw)
        if n not in seen:
            seen.add(n)
            unique.append(kw)
    unique = unique[:max_n]

    if not unique:
        return

    logger.info("롱테일 확장: %d개 2토큰 키워드 재투입", len(unique))
    tasks = [
        safe_tool_call(
            f"autocomplete_lt({kw})", search_suggestions_fn(kw), "{}",
        )
        for kw in unique
    ]
    results = await asyncio.gather(*tasks)

    added = 0
    for raw in results:
        if not raw:
            continue
        try:
            data = json.loads(raw)
            for platform in ("naver", "google"):
                for suggestion in data.get(platform, []):
                    if len(suggestion.split()) >= 3:
                        n = _normalize_keyword(suggestion)
                        if n not in existing:
                            existing.add(n)
                            target = pool.naver if platform == "naver" else pool.google
                            target.append(suggestion)
                            added += 1
        except (json.JSONDecodeError, TypeError):
            pass

    logger.info("롱테일 확장 결과: +%d개 (3토큰+)", added)


# -- 1b: 내부 고객 언어 대조 --

def stage1b_customer_language(client: str, config: dict) -> list[str]:
    """clients/{client}/context.yaml의 customer_language 섹션 로드."""
    if not client:
        return []

    context_template = config.get("context_path", "")
    if not context_template:
        return []

    context_path = Path(context_template.format(client=client))
    if not context_path.is_file():
        logger.info("고객 context 파일 없음: %s", context_path)
        return []

    try:
        with open(context_path) as f:
            data = yaml.safe_load(f) or {}
        cl = data.get("customer_language", {})
        keywords: list[str] = []
        if isinstance(cl, dict):
            for value in cl.values():
                if isinstance(value, list):
                    keywords.extend(str(v) for v in value)
                elif isinstance(value, str):
                    keywords.append(value)
        elif isinstance(cl, list):
            keywords.extend(str(v) for v in cl)
        logger.info("고객 언어 로드: %s → %d개", client, len(keywords))
        return keywords
    except (OSError, yaml.YAMLError) as e:
        logger.warning("고객 context 로드 실패: %s", e)
        return []


# -- 중복 제거 --

def deduplicate_keywords(
    pool: RawKeywordPool,
) -> list[tuple[str, str]]:
    """키워드 풀 정제 → [(normalized_kw, source)] 중복 제거."""
    seen: dict[str, str] = {}
    for kw in pool.google:
        n = _normalize_keyword(kw)
        if n and n not in seen:
            seen[n] = "google"
    for kw in pool.naver:
        n = _normalize_keyword(kw)
        if n and n not in seen:
            seen[n] = "naver"
    for kw in pool.keyword_tool:
        n = _normalize_keyword(kw)
        if n and n not in seen:
            seen[n] = "keyword_tool"
    for kw in pool.paa:
        n = _normalize_keyword(kw)
        if n and n not in seen:
            seen[n] = "google"  # PAA는 구글 소스로 분류
    for kw in pool.internal_data:
        n = _normalize_keyword(kw)
        if n and n not in seen:
            seen[n] = "internal_data"
    return list(seen.items())


# -- 1d: LLM 의미 기반 클러스터링 --

async def _stage1d_llm_clustering(
    deduped: list[tuple[str, str]],
    *, llm_call_fn: LlmCall, config: dict,
) -> tuple[list[ClusterDraft], list[str]]:
    """LLM에 전체 키워드 목록을 전달하여 의미 의도 기반 클러스터링."""
    kw_list = [kw for kw, _ in deduped]
    source_map = dict(deduped)

    system_prompt = load_prompt("1d_clustering")
    user_prompt = json.dumps(
        {"keywords": kw_list},
        ensure_ascii=False,
    )
    response_format = (
        '{"clusters": [{"keywords": ["kw1", "kw2", "kw3"], '
        '"shared_intent": "공유 검색 의도"}], "orphans": ["kw4"]}'
    )
    user_prompt += f"\n\n응답 형식: {response_format}"

    main_model = config.get("models", {}).get("main", "gpt-5.2")
    raw = await llm_call_fn(
        "1d_clustering", system_prompt, user_prompt,
        model=main_model, max_tokens=16384,
    )

    # 파싱
    min_members = 3
    cluster_drafts: list[ClusterDraft] = []
    orphans: list[str] = []
    try:
        # JSON 블록 추출 (```json ... ``` 또는 순수 JSON)
        json_match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        json_str = json_match.group(1) if json_match else raw
        data = json.loads(json_str)
        for idx, group in enumerate(data.get("clusters", [])):
            members = group.get("keywords", [])
            if len(members) < min_members:
                orphans.extend(members)
                continue
            cd = ClusterDraft(
                cluster_id=f"c{idx:03d}",
                keywords=[
                    (kw, source_map.get(kw, "search_tool"))
                    for kw in members
                    if kw in source_map
                ],
                shared_intent=group.get("shared_intent", ""),
            )
            if cd.keywords:
                cluster_drafts.append(cd)
        orphans.extend(data.get("orphans", []))
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("[1d_clustering] LLM 응답 파싱 실패 — 전부 고립 처리: %s", e)
        logger.debug("[1d_clustering] raw 응답 길이=%d, 처음500자=%s", len(raw), raw[:500])
        orphans = kw_list

    # 후처리: 25개 초과 클러스터 → 초과분을 orphans로 이동
    max_cluster_size = config.get("clustering", {}).get("max_cluster_size", 25)
    for cd in cluster_drafts:
        if len(cd.keywords) > max_cluster_size:
            logger.info(
                "[1d_clustering] 클러스터 %s 과팽창: %d개 → %d개로 축소",
                cd.cluster_id, len(cd.keywords), max_cluster_size,
            )
            cd.keywords.sort(
                key=lambda item: -(len(item[0])),  # 긴 키워드(구체적) 우선
            )
            excess = cd.keywords[max_cluster_size:]
            cd.keywords = cd.keywords[:max_cluster_size]
            orphans.extend(kw for kw, _ in excess)

    return cluster_drafts, orphans


# -- 1e: LLM 대표 키워드 선정 --

async def _stage1e_llm_representative(
    cluster_drafts: list[ClusterDraft], volumes: dict[str, int],
    *, llm_call_fn: LlmCall, config: dict,
) -> None:
    """LLM으로 각 클러스터 대표 키워드 선정 (in-place 업데이트)."""
    if not cluster_drafts:
        return

    system_prompt = load_prompt("1e_representative")
    clusters_input = [
        {
            "id": cd.cluster_id,
            "keywords": [
                {"keyword": kw, "volume": volumes.get(_normalize_keyword(kw), 0)}
                for kw, _ in cd.keywords
            ],
        }
        for cd in cluster_drafts
    ]
    user_prompt = json.dumps(
        {"clusters": clusters_input}, ensure_ascii=False,
    )
    response_format = '[{"id": "c000", "representative": "...", "rationale": "..."}]'
    user_prompt += f"\n\n응답 형식: {response_format}"

    mini_model = config.get("models", {}).get("mini", "gpt-4.1-mini")
    raw = await llm_call_fn("1e_representative", system_prompt, user_prompt, model=mini_model)

    # 파싱
    id_map = {cd.cluster_id: cd for cd in cluster_drafts}
    try:
        json_match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        json_str = json_match.group(1) if json_match else raw
        results = json.loads(json_str)
        if isinstance(results, list):
            for item in results:
                cid = item.get("id", "")
                rep = item.get("representative", "")
                rationale = item.get("rationale", "")
                if cid in id_map and rep:
                    id_map[cid].representative = rep
                    id_map[cid].representative_rationale = rationale
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("[1e_representative] LLM 응답 파싱 실패 — 검색량 기반 fallback")

    # Fallback: LLM이 대표를 설정하지 못한 클러스터에 검색량 기반 선정
    for cd in cluster_drafts:
        if not cd.representative and cd.keywords:
            members = [kw for kw, _ in cd.keywords]
            cd.representative = max(
                members,
                key=lambda kw: (
                    volumes.get(_normalize_keyword(kw), 0),
                    -len(kw),
                ),
            )


# -- 1f: 아카이브 비교 --

async def _stage1f_archive_comparison(
    cluster_drafts: list[ClusterDraft],
    *,
    llm_call_fn: LlmCall,
    config: dict,
    load_archive_reps_fn: Callable[[], list[str]],
    load_archive_clusters_fn: Callable[[], dict[str, list[str]]],
) -> None:
    """신규 대표 키워드를 아카이브 대표와 LLM으로 비교 (in-place 업데이트)."""
    archive_reps = load_archive_reps_fn()
    if not archive_reps or not cluster_drafts:
        for cd in cluster_drafts:
            cd.archive_verdict = "new"
        return

    new_clusters = []
    for cd in cluster_drafts:
        if cd.representative:
            new_clusters.append({
                "representative": cd.representative,
                "keywords": [kw for kw, _ in cd.keywords],
            })
    if not new_clusters:
        return

    # 아카이브 클러스터 데이터 (소속 키워드 포함)
    archive_clusters = load_archive_clusters_fn()
    archive_input = [
        {"representative": rep, "keywords": archive_clusters.get(rep, [rep])}
        for rep in archive_reps
    ]

    system_prompt = load_prompt("1f_archive")
    user_prompt = json.dumps(
        {"new_clusters": new_clusters, "archive_clusters": archive_input},
        ensure_ascii=False,
    )
    response_format = '[{"new_rep": "...", "verdict": "new|merge|duplicate", "matched_archive_rep": "..."}]'
    user_prompt += f"\n\n응답 형식: {response_format}"

    mini_model = config.get("models", {}).get("mini", "gpt-4.1-mini")
    raw = await llm_call_fn("1f_archive", system_prompt, user_prompt, model=mini_model)

    rep_to_cd = {cd.representative: cd for cd in cluster_drafts}
    try:
        json_match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        json_str = json_match.group(1) if json_match else raw
        results = json.loads(json_str)
        if isinstance(results, list):
            for item in results:
                nr = item.get("new_rep", "")
                verdict = item.get("verdict", "new")
                matched = item.get("matched_archive_rep", "")
                if nr in rep_to_cd:
                    rep_to_cd[nr].archive_verdict = verdict
                    rep_to_cd[nr].matched_archive_representative = matched
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("[1f_archive] LLM 응답 파싱 실패 — 전부 new 처리")

    # Fallback: 판정 안 된 것은 전부 "new"
    for cd in cluster_drafts:
        if not cd.archive_verdict:
            cd.archive_verdict = "new"

    # merge 판정 시 아카이브 키워드를 현재 클러스터에 실제 병합
    if archive_clusters:
        for cd in cluster_drafts:
            if cd.archive_verdict == "merge" and cd.matched_archive_representative:
                arch_kws = archive_clusters.get(
                    cd.matched_archive_representative, [],
                )
                existing = {kw for kw, _ in cd.keywords}
                for akw in arch_kws:
                    if akw not in existing:
                        cd.keywords.append((akw, "archive"))
                        existing.add(akw)
                if arch_kws:
                    logger.info(
                        "아카이브 병합: %s ← %s (+%d kws)",
                        cd.representative,
                        cd.matched_archive_representative,
                        len(arch_kws),
                    )


# -- 1g: 포커스 클러스터 선정 --

async def _stage1g_focus_selection(
    cluster_drafts: list[ClusterDraft],
    questions: list[str],
    volumes: dict[str, int] | None = None,
    *,
    llm_call_fn: LlmCall,
    config: dict,
) -> None:
    """콘텐츠 제작에 적합한 검색 키워드를 가진 클러스터만 포커스로 선정 (in-place)."""
    if not cluster_drafts or not questions:
        # 질문 없으면 전부 포커스 (폴백)
        return

    system_prompt = load_prompt("1g_focus")
    clusters_input = [
        {
            "id": cd.cluster_id,
            "representative": cd.representative,
            "keywords": [kw for kw, _ in cd.keywords][:10],
        }
        for cd in cluster_drafts
    ]
    user_prompt = json.dumps(
        {"questions": questions, "clusters": clusters_input},
        ensure_ascii=False,
    )
    response_format = '[{"id": "c000", "focus": true, "reason": "비용 비교는 업체 선정의 핵심 기준"}]'
    user_prompt += f"\n\n응답 형식: {response_format}"

    main_model = config.get("models", {}).get("main", "gpt-5.2")
    raw = await llm_call_fn("1g_focus", system_prompt, user_prompt, model=main_model)

    id_map = {cd.cluster_id: cd for cd in cluster_drafts}
    try:
        json_match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        json_str = json_match.group(1) if json_match else raw
        results = json.loads(json_str)
        if isinstance(results, list):
            for item in results:
                cid = item.get("id", "")
                focus = item.get("focus", True)
                reason = item.get("reason", "")
                if cid in id_map:
                    id_map[cid].is_focus = bool(focus)
                    label = "포커스" if focus else "제외"
                    logger.info(
                        "[1g_focus] %s %s (%s): %s",
                        label, id_map[cid].representative, cid, reason,
                    )
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("[1g_focus] LLM 응답 파싱 실패 — 전부 포커스 유지")

    focus_count = sum(1 for cd in cluster_drafts if cd.is_focus)

    # 포커스 비율 상한 적용 — 전부 focus일 때 볼륨 하위 클러스터 제외
    max_ratio = config.get("focus", {}).get("max_ratio", 0.70)
    max_focus = max(1, int(len(cluster_drafts) * max_ratio))
    if focus_count > max_focus:
        vols = volumes or {}
        focused = [cd for cd in cluster_drafts if cd.is_focus]
        focused.sort(
            key=lambda cd: sum(
                vols.get(_normalize_keyword(kw), 0)
                for kw, _ in cd.keywords
            ),
            reverse=True,
        )
        for cd in focused[max_focus:]:
            cd.is_focus = False
            logger.info(
                "[1g_focus] 비율 상한 초과로 제외: %s (%s)",
                cd.representative, cd.cluster_id,
            )
        focus_count = max_focus

    logger.info(
        "포커스 선정: %d/%d 클러스터",
        focus_count, len(cluster_drafts),
    )


# -- 필터 함수들 --


def _rule_based_prefilter(
    deduped: list[tuple[str, str]], seeds: list[str],
) -> list[tuple[str, str]]:
    """규칙 기반 사전 필터 (LLM 호출 없음).

    - keyword_tool 출처: 시드와 토큰 겹침 2개 이상 + 2어절 이상만 통과
    - 나머지 출처: 느슨하게 통과
    - 정규화 후 동일 키워드 중복 압축
    """
    if not deduped:
        return deduped

    seed_tokens: set[str] = set()
    for s in seeds:
        for tok in _normalize_keyword(s).split():
            if len(tok) >= 2:
                seed_tokens.add(tok)

    before = len(deduped)
    filtered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kw, src in deduped:
        nk = _normalize_keyword(kw)
        if nk in seen:
            continue
        if src == "keyword_tool":
            tokens = nk.split()
            if len(tokens) < 2:
                continue
            overlap = sum(1 for t in tokens if t in seed_tokens)
            if overlap < 2:
                continue
        seen.add(nk)
        filtered.append((kw, src))

    logger.info("규칙 필터: %d → %d", before, len(filtered))
    return filtered


async def _domain_filter(
    deduped: list[tuple[str, str]], seeds: list[str],
    intent: str, direction: str,
    *, llm_call_fn: LlmCall, config: dict,
) -> list[tuple[str, str]]:
    """도메인 필터 (mini 모델 1회) — 도메인과 무관한 키워드만 제거.

    '질문에 답하는 데 필요한가'는 판단하지 않는다 — 그건 포커스 선정의 역할.
    """
    if not deduped:
        return deduped
    before = len(deduped)
    kw_source_map = {kw: src for kw, src in deduped}
    kw_list = [kw for kw, _ in deduped]

    batch_size = 300
    filtered_all: list[tuple[str, str]] = []
    mini = config.get("models", {}).get("mini", "gpt-4.1-mini")

    for batch in _chunk(kw_list, batch_size):
        system = load_prompt(
            "domain_filter",
            seed_list=", ".join(seeds[:10]),
            intent=intent,
            direction=direction,
        )
        user = json.dumps(batch, ensure_ascii=False)
        raw = await llm_call_fn(
            "domain_filter", system, user,
            model=mini, max_tokens=4096,
        )
        try:
            result = json.loads(raw)
            if isinstance(result, dict):
                for v in result.values():
                    if isinstance(v, list):
                        result = v
                        break
            if isinstance(result, list):
                for kw in result:
                    src = kw_source_map.get(kw)
                    if src is not None:
                        filtered_all.append((kw, src))
            else:
                filtered_all.extend([(kw, kw_source_map[kw]) for kw in batch])
        except (json.JSONDecodeError, TypeError):
            filtered_all.extend([(kw, kw_source_map[kw]) for kw in batch])

    logger.info("도메인 필터: %d → %d", before, len(filtered_all))
    return filtered_all
