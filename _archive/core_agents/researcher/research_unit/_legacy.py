"""레거시 호환 래퍼 — agent.py run_legacy()용 시그니처 보존."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from core.schemas import Stage2Output, Stage3Output

from core.agents.researcher.research_unit.content import (
    collect_google_content,
    collect_naver_content,
    extract_h2_topics,
    prefetch_serp,
)
from core.agents.researcher.research_unit.geo import (
    stage3_geo as _stage3_geo_impl,
)
from core.agents.researcher.research_unit.serp import (
    collect_google_serp_features,
    collect_naver_serp_features,
)
from core.agents.researcher.research_unit.volume import collect_volumes

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
    """레거시 호환 래퍼 — 기존 stage2_validation() 시그니처 유지."""
    logger.info("2단계 시작: representatives=%d", len(reps))
    if not reps:
        return Stage2Output()

    # 구글 SERP 사전 수집
    serp_cache = await prefetch_serp(reps, safe_tool_call, google_search_fn)

    # 볼륨 + 구글 콘텐츠 + 네이버 콘텐츠 병렬
    vol, google_meta, naver_meta = await asyncio.gather(
        collect_volumes(
            reps,
            all_keywords=all_keywords,
            existing_volumes=stage1_volumes,
            existing_volumes_pc=stage1_volumes_pc,
            existing_volumes_mobile=stage1_volumes_mobile,
            safe_tool_call=safe_tool_call,
            naver_keyword_volume_fn=naver_keyword_volume_fn,
            google_keyword_trend_fn=google_keyword_trend_fn,
            naver_keyword_trend_fn=naver_keyword_trend_fn,
        ),
        collect_google_content(
            reps, serp_cache,
            safe_tool_call=safe_tool_call,
            web_fetch_fn=web_fetch_fn,
        ),
        collect_naver_content(
            reps,
            safe_tool_call=safe_tool_call,
            naver_blog_search_fn=naver_blog_search_fn,
        ),
    )

    h2_topics = extract_h2_topics(google_meta)
    google_features = collect_google_serp_features(
        reps, serp_cache, paa_questions=paa_questions or {},
    )
    naver_features = await collect_naver_serp_features(
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


async def stage3_geo(
    reps: list[str],
    *,
    safe_tool_call: SafeToolCall,
    ai_search_fn: Callable,
    perplexity_search_fn: Callable,
    geo_claude_fn: Callable,
    geo_gemini_fn: Callable,
) -> Stage3Output:
    """레거시 호환 래퍼 — 기존 stage3_geo() 시그니처 유지."""
    return await _stage3_geo_impl(
        reps,
        safe_tool_call=safe_tool_call,
        ai_search_fn=ai_search_fn,
        perplexity_search_fn=perplexity_search_fn,
        geo_claude_fn=geo_claude_fn,
        geo_gemini_fn=geo_gemini_fn,
    )
