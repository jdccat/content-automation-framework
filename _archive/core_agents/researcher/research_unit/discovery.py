"""리서치 유닛: 관련 검색어 + PAA 수집."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

SafeToolCall = Callable[..., Awaitable]


async def collect_related(
    keywords: list[str],
    *,
    safe_tool_call: SafeToolCall,
    search_suggestions_fn: Callable | None = None,
    google_related_fn: Callable | None = None,
) -> dict[str, list[str]]:
    """키워드별 관련 검색어 수집 (autocomplete + google_related)."""
    result: dict[str, list[str]] = {}
    if not keywords:
        return result

    tasks = []
    task_keys: list[tuple[str, str]] = []  # (keyword, source)

    if search_suggestions_fn:
        for kw in keywords:
            tasks.append(
                safe_tool_call(
                    f"autocomplete({kw})", search_suggestions_fn(kw), "{}",
                )
            )
            task_keys.append((kw, "autocomplete"))

    if google_related_fn:
        for kw in keywords:
            tasks.append(
                safe_tool_call(
                    f"google_related({kw})", google_related_fn(kw), "{}",
                )
            )
            task_keys.append((kw, "related"))

    if not tasks:
        return result

    raw_results = await asyncio.gather(*tasks)

    for (kw, source), raw in zip(task_keys, raw_results):
        if not raw:
            continue
        try:
            data = json.loads(raw)
            related: list[str] = []
            if source == "autocomplete":
                related.extend(data.get("naver", []))
                related.extend(data.get("google", []))
            elif source == "related":
                related.extend(data.get("related_searches", []))
            if related:
                existing = result.get(kw, [])
                existing.extend(related)
                result[kw] = existing
        except (json.JSONDecodeError, TypeError):
            pass

    # 중복 제거
    for kw in result:
        result[kw] = list(dict.fromkeys(result[kw]))

    return result


async def collect_paa(
    keywords: list[str],
    *,
    safe_tool_call: SafeToolCall,
    google_paa_fn: Callable | None = None,
) -> dict[str, list[str]]:
    """키워드별 PAA(People Also Ask) 질문 수집."""
    result: dict[str, list[str]] = {}
    if not keywords or not google_paa_fn:
        return result

    tasks = [
        safe_tool_call(
            f"google_paa({kw})", google_paa_fn(kw), "{}",
        )
        for kw in keywords
    ]
    raw_results = await asyncio.gather(*tasks)

    for kw, raw in zip(keywords, raw_results):
        if not raw:
            continue
        try:
            data = json.loads(raw)
            questions = data.get("questions", [])
            if questions:
                result[kw] = questions
        except (json.JSONDecodeError, TypeError):
            pass

    return result
