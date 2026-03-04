"""Perplexity API 기반 검색 도구 — AI 큐레이팅 인용 URL 수집."""

from __future__ import annotations

import json
import logging
import os

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


async def perplexity_search(query: str, num: int = 5) -> str:
    """Perplexity sonar 모델로 검색하고 AI가 선별한 인용 URL을 반환한다.

    Args:
        query: 검색 키워드.
        num: 반환할 인용 URL 최대 개수 (기본 5).

    Returns:
        JSON 문자열 — 응답 요약 + citations 리스트.
    """
    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key:
        return json.dumps(
            {"error": "PERPLEXITY_API_KEY 환경변수가 설정되지 않았습니다."},
            ensure_ascii=False,
        )

    if not query or not query.strip():
        return json.dumps(
            {"error": "검색어가 비어 있습니다."},
            ensure_ascii=False,
        )

    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://api.perplexity.ai",
    )

    response = await client.chat.completions.create(
        model="sonar",
        messages=[
            {
                "role": "system",
                "content": (
                    "한국어로 답변하세요. 검색 결과를 요약하고 "
                    "관련 소스를 최대한 많이 인용하세요."
                ),
            },
            {"role": "user", "content": query},
        ],
    )

    answer = response.choices[0].message.content or ""
    citations: list[str] = getattr(response, "citations", []) or []

    result = {
        "query": query,
        "answer": answer,
        "citations": citations[:num],
        "total": len(citations),
    }

    logger.info(
        "Perplexity 검색 완료: query=%s, %d개 인용",
        query,
        len(citations),
    )
    return json.dumps(result, ensure_ascii=False)
