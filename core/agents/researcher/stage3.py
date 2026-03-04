"""3단계: GEO 인용 수집 — ChatGPT, Perplexity, Claude, Gemini."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from core.agents.researcher.parser import (
    _extract_domain,
    _extract_pplx_context,
    _keyword_to_question,
)
from core.schemas import Stage3Output

logger = logging.getLogger(__name__)

OWN_DOMAIN = "wishket.com"

# 도구 타입 힌트
SafeToolCall = Callable[..., Awaitable]


async def stage3_geo(
    reps: list[str],
    *,
    safe_tool_call: SafeToolCall,
    ai_search_fn: Callable,
    perplexity_search_fn: Callable,
    geo_claude_fn: Callable,
    geo_gemini_fn: Callable,
) -> Stage3Output:
    """대표 키워드별 4개 AI 서비스 GEO 인용 수집."""
    logger.info("3단계 시작: representatives=%d", len(reps))
    if not reps:
        return Stage3Output()

    tasks = [
        _geo_for_keyword(
            kw, _keyword_to_question(kw),
            safe_tool_call=safe_tool_call,
            ai_search_fn=ai_search_fn,
            perplexity_search_fn=perplexity_search_fn,
            geo_claude_fn=geo_claude_fn,
            geo_gemini_fn=geo_gemini_fn,
        )
        for kw in reps
    ]
    geo_results = await asyncio.gather(*tasks)

    output = Stage3Output()
    for kw, citations in geo_results:
        output.citations[kw] = citations
    return output


async def _geo_for_keyword(
    keyword: str,
    question: str,
    *,
    safe_tool_call: SafeToolCall,
    ai_search_fn: Callable,
    perplexity_search_fn: Callable,
    geo_claude_fn: Callable,
    geo_gemini_fn: Callable,
) -> tuple[str, list[dict]]:
    """단일 키워드에 대해 4개 AI 서비스 병렬 GEO 인용 수집."""
    ai_raw, pplx_raw, claude_raw, gemini_raw = await asyncio.gather(
        safe_tool_call(
            f"ai_search({question})", ai_search_fn(question), "{}",
        ),
        safe_tool_call(
            f"perplexity({question})",
            perplexity_search_fn(question),
            "{}",
        ),
        safe_tool_call(
            f"geo_claude({question})",
            geo_claude_fn(question),
            "{}",
        ),
        safe_tool_call(
            f"geo_gemini({question})",
            geo_gemini_fn(question),
            "{}",
        ),
    )

    citations: list[dict] = []
    seen_urls: set[str] = set()

    # ChatGPT (ai_search) 파싱
    if ai_raw:
        try:
            data = json.loads(ai_raw)
            for detail in data.get("citation_details", []):
                url = detail.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    domain = _extract_domain(url)
                    # context_snippet 우선, fallback title
                    ctx = (
                        detail.get("context_snippet")
                        or detail.get("title", "")
                    )
                    citations.append({
                        "url": url,
                        "domain": domain,
                        "context_summary": ctx,
                        "source": "chatgpt",
                        "is_own_domain": OWN_DOMAIN in domain,
                        "is_competitor": False,
                    })
        except (json.JSONDecodeError, TypeError):
            pass

    # Perplexity 파싱
    if pplx_raw:
        try:
            data = json.loads(pplx_raw)
            pplx_answer = data.get("answer", "")
            pplx_urls = data.get("citations", [])
            for idx, url in enumerate(pplx_urls):
                if isinstance(url, str) and url and url not in seen_urls:
                    seen_urls.add(url)
                    domain = _extract_domain(url)
                    # 답변 텍스트에서 [N] 인용 주변 문맥 추출
                    ctx = _extract_pplx_context(pplx_answer, idx + 1)
                    citations.append({
                        "url": url,
                        "domain": domain,
                        "context_summary": ctx,
                        "source": "perplexity",
                        "is_own_domain": OWN_DOMAIN in domain,
                        "is_competitor": False,
                    })
        except (json.JSONDecodeError, TypeError):
            pass

    # Claude 파싱 (Playwright 브라우저 기반)
    if claude_raw:
        try:
            data = json.loads(claude_raw)
            for cite in data.get("citations", []):
                url = cite.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    domain = _extract_domain(url)
                    citations.append({
                        "url": url,
                        "domain": domain,
                        "context_summary": cite.get("context_summary", ""),
                        "source": "claude",
                        "is_own_domain": OWN_DOMAIN in domain,
                        "is_competitor": False,
                    })
        except (json.JSONDecodeError, TypeError):
            pass

    # Gemini 파싱 (Playwright 브라우저 기반)
    if gemini_raw:
        try:
            data = json.loads(gemini_raw)
            for cite in data.get("citations", []):
                url = cite.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    domain = _extract_domain(url)
                    citations.append({
                        "url": url,
                        "domain": domain,
                        "context_summary": cite.get("context_summary", ""),
                        "source": "gemini",
                        "is_own_domain": OWN_DOMAIN in domain,
                        "is_competitor": False,
                    })
        except (json.JSONDecodeError, TypeError):
            pass

    return keyword, citations
