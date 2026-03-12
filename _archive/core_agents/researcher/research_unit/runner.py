"""리서치 유닛 오케스트레이터 — 프로파일 기반 수집 분기."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Awaitable, Callable

from core.schemas import ResearchProfile, ResearchUnitOutput

from core.agents.researcher.research_unit.content import (
    collect_google_content,
    collect_naver_content,
    extract_h2_topics,
    prefetch_serp,
)
from core.agents.researcher.research_unit.discovery import (
    collect_paa,
    collect_related,
)
from core.agents.researcher.research_unit.geo import collect_geo
from core.agents.researcher.research_unit.serp import (
    collect_google_serp_features,
    collect_naver_serp_features,
)
from core.agents.researcher.research_unit.volume import collect_volumes

logger = logging.getLogger(__name__)

SafeToolCall = Callable[..., Awaitable]


def _snap(name: str, data: dict, run_date: str, snapshot_dir: str) -> None:
    """단계별 스냅샷 저장 (snapshot_dir 미지정 시 no-op)."""
    if not snapshot_dir or not run_date:
        return
    d = Path(snapshot_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{run_date}_ru_{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("리서치 유닛 스냅샷: %s", path)


async def run_research_unit(
    keywords: list[str],
    profile: ResearchProfile,
    *,
    safe_tool_call: SafeToolCall,
    google_search_fn: Callable,
    naver_keyword_volume_fn: Callable,
    google_keyword_trend_fn: Callable,
    naver_keyword_trend_fn: Callable,
    naver_blog_search_fn: Callable,
    web_fetch_fn: Callable,
    naver_serp_features_fn: Callable,
    # 프로파일 선택적
    search_suggestions_fn: Callable | None = None,
    google_related_fn: Callable | None = None,
    google_paa_fn: Callable | None = None,
    ai_search_fn: Callable | None = None,
    perplexity_search_fn: Callable | None = None,
    geo_claude_fn: Callable | None = None,
    geo_gemini_fn: Callable | None = None,
    existing_volumes: dict[str, int] | None = None,
    existing_volumes_pc: dict[str, int] | None = None,
    existing_volumes_mobile: dict[str, int] | None = None,
    snapshot_dir: str = "",
    run_date: str = "",
) -> ResearchUnitOutput:
    """프로파일 플래그에 따라 수집 모듈을 조합하여 실행."""
    if not keywords:
        return ResearchUnitOutput()

    logger.info(
        "리서치 유닛 시작: keywords=%d, profile=%s",
        len(keywords), profile,
    )
    output = ResearchUnitOutput()

    # 1. SERP 사전 수집 (content 또는 serp_features 필요 시)
    serp_cache: dict[str, str] = {}
    if profile.content or profile.serp_features:
        serp_cache = await prefetch_serp(
            keywords, safe_tool_call, google_search_fn,
        )
        _snap("1_serp_cache", {
            "keywords": keywords,
            "serp_cache_keys": list(serp_cache.keys()),
            "serp_cache": serp_cache,
        }, run_date, snapshot_dir)

    # 2. 병렬 수집 태스크 조립
    parallel_tasks: dict[str, asyncio.Task] = {}

    if profile.volumes:
        parallel_tasks["volumes"] = asyncio.ensure_future(
            collect_volumes(
                keywords,
                existing_volumes=existing_volumes,
                existing_volumes_pc=existing_volumes_pc,
                existing_volumes_mobile=existing_volumes_mobile,
                safe_tool_call=safe_tool_call,
                naver_keyword_volume_fn=naver_keyword_volume_fn,
                google_keyword_trend_fn=google_keyword_trend_fn,
                naver_keyword_trend_fn=naver_keyword_trend_fn,
            )
        )

    if profile.content:
        parallel_tasks["google_content"] = asyncio.ensure_future(
            collect_google_content(
                keywords, serp_cache,
                safe_tool_call=safe_tool_call,
                web_fetch_fn=web_fetch_fn,
            )
        )
        parallel_tasks["naver_content"] = asyncio.ensure_future(
            collect_naver_content(
                keywords,
                safe_tool_call=safe_tool_call,
                naver_blog_search_fn=naver_blog_search_fn,
            )
        )

    if profile.related_keywords:
        parallel_tasks["related"] = asyncio.ensure_future(
            collect_related(
                keywords,
                safe_tool_call=safe_tool_call,
                search_suggestions_fn=search_suggestions_fn,
                google_related_fn=google_related_fn,
            )
        )

    if profile.paa:
        parallel_tasks["paa"] = asyncio.ensure_future(
            collect_paa(
                keywords,
                safe_tool_call=safe_tool_call,
                google_paa_fn=google_paa_fn,
            )
        )

    if profile.geo and ai_search_fn and perplexity_search_fn and geo_claude_fn and geo_gemini_fn:
        parallel_tasks["geo"] = asyncio.ensure_future(
            collect_geo(
                keywords,
                safe_tool_call=safe_tool_call,
                ai_search_fn=ai_search_fn,
                perplexity_search_fn=perplexity_search_fn,
                geo_claude_fn=geo_claude_fn,
                geo_gemini_fn=geo_gemini_fn,
            )
        )

    # 모든 태스크 대기
    if parallel_tasks:
        await asyncio.gather(*parallel_tasks.values())

    # 3. 결과 조립
    if "volumes" in parallel_tasks:
        output.volumes = parallel_tasks["volumes"].result()

    if "google_content" in parallel_tasks:
        output.google_content_metas = parallel_tasks["google_content"].result()

    if "naver_content" in parallel_tasks:
        output.naver_content_metas = parallel_tasks["naver_content"].result()

    if "related" in parallel_tasks:
        output.related_keywords = parallel_tasks["related"].result()

    if "paa" in parallel_tasks:
        output.paa_questions = parallel_tasks["paa"].result()

    if "geo" in parallel_tasks:
        output.geo_citations = parallel_tasks["geo"].result()

    # 스냅샷 2: 병렬 수집 완료
    _snap("2_parallel", asdict(output), run_date, snapshot_dir)

    # 4. 동기 후처리
    if profile.content and output.google_content_metas:
        output.h2_topics = extract_h2_topics(output.google_content_metas)

    if profile.serp_features:
        output.google_serp_features = collect_google_serp_features(
            keywords, serp_cache,
        )
        output.naver_serp_features = await collect_naver_serp_features(
            keywords,
            safe_tool_call=safe_tool_call,
            naver_serp_features_fn=naver_serp_features_fn,
        )

    # 스냅샷 3: 최종 출력
    _snap("3_final", asdict(output), run_date, snapshot_dir)

    logger.info("리서치 유닛 완료")
    return output
