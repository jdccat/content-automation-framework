"""Anthropic Claude API web_search 기반 GEO 인용 수집 도구.

ai_search.py(OpenAI)와 동일한 가치: AI 요약 + 인용 URL.
ANTHROPIC_API_KEY만 있으면 동작.
Fail-fast: 1회 시도 후 실패 시 즉시 에러 반환 (재시도 없음).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import anthropic

logger = logging.getLogger(__name__)

_MAX_RETRIES = 0  # fail-fast: 재시도 없이 1회 시도 후 즉시 반환


async def claude_search(query: str, num: int = 5) -> str:
    """Claude web_search로 AI가 선별한 인용 URL과 요약을 반환한다.

    Args:
        query: 검색 쿼리 (자연어).
        num: 반환할 인용 URL 최대 개수 (기본 5).

    Returns:
        JSON 문자열 — 응답 요약 + citations 리스트.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return json.dumps(
            {"error": "ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다."},
            ensure_ascii=False,
        )

    if not query or not query.strip():
        return json.dumps(
            {"error": "검색어가 비어 있습니다."},
            ensure_ascii=False,
        )

    client = anthropic.AsyncAnthropic(api_key=api_key)

    prompt = (
        f"다음 주제에 대해 웹 검색을 수행하고, "
        f"다양한 소스를 인용하여 한국어로 종합적으로 답변하세요: {query}\n\n"
        "가능한 한 많은 출처를 참조하세요."
    )

    _MAX_ANSWER_CHARS = 800

    response = None
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning("Claude search 실패 (fail-fast): %s", e)
        return json.dumps(
            {"error": f"Claude API 호출 실패: {e}", "query": query},
            ensure_ascii=False,
        )

    # 응답에서 텍스트 + 인용 URL 추출
    full_answer = ""
    citations: list[dict] = []
    seen_urls: set[str] = set()

    for block in response.content:
        # 텍스트 블록에서 답변 추출
        if block.type == "text":
            full_answer += block.text
        # web_search_tool_result 블록에서 검색 결과 추출
        elif block.type == "web_search_tool_result":
            for search_result in getattr(block, "content", []):
                if getattr(search_result, "type", "") == "web_search_result":
                    url = getattr(search_result, "url", "")
                    title = getattr(search_result, "title", "")
                    snippet = getattr(search_result, "page_content", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        citations.append({
                            "url": url,
                            "title": title,
                            "context_snippet": snippet[:200] if snippet else "",
                        })

    answer = full_answer[:_MAX_ANSWER_CHARS]

    result = {
        "query": query,
        "answer": answer,
        "citations": [c["url"] for c in citations[:num]],
        "citation_details": citations[:num],
        "total": len(citations),
    }

    logger.info(
        "Claude 검색 완료: query=%s, %d개 인용",
        query,
        len(citations),
    )
    return json.dumps(result, ensure_ascii=False)
