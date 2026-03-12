"""리서치 유닛: SERP 피처 수집 (구글/네이버)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from core.agents.researcher.parser import _extract_serp_features

logger = logging.getLogger(__name__)

SafeToolCall = Callable[..., Awaitable]


def collect_google_serp_features(
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
        kw_paa = paa_qs.get(kw, sf.get("paa_questions", []))
        result[kw] = {
            "ai_overview": sf.get("ai_overview", False),
            "featured_snippet_exists": sf.get("featured_snippet_exists", False),
            "featured_snippet_url": sf.get("featured_snippet_url"),
            "paa_questions": kw_paa,
        }
    return result


async def collect_naver_serp_features(
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
                "smart_block_components": data.get("smart_block_components", []),
            }
        except (json.JSONDecodeError, TypeError):
            result[kw] = {
                "knowledge_snippet": False,
                "smart_block": False,
                "smart_block_components": [],
            }
    return result
