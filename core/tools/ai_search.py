"""OpenAI web_search_preview 기반 AI 큐레이팅 검색 도구.

Perplexity와 동일한 가치(AI 요약 + 인용 URL)를 OpenAI API로 제공한다.
기존 OPENAI_API_KEY만 있으면 동작하며 별도 API 키 불필요.
"""

from __future__ import annotations

import json
import logging
import os

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


async def ai_search(query: str, num: int = 5) -> str:
    """OpenAI web_search_preview로 AI가 선별한 인용 URL과 요약을 반환한다.

    Args:
        query: 검색 키워드.
        num: 반환할 인용 URL 최대 개수 (기본 5).

    Returns:
        JSON 문자열 — 응답 요약 + citations 리스트.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return json.dumps(
            {"error": "OPENAI_API_KEY 환경변수가 설정되지 않았습니다."},
            ensure_ascii=False,
        )

    if not query or not query.strip():
        return json.dumps(
            {"error": "검색어가 비어 있습니다."},
            ensure_ascii=False,
        )

    client = AsyncOpenAI(api_key=api_key)

    prompt = (
        f"다음 주제에 대해 한국어로 웹 검색하고, "
        f"다양한 소스를 인용하여 종합적으로 답변하세요: {query}\n\n"
        "가능한 한 많은 출처를 참조하세요."
    )

    response = await client.responses.create(
        model="gpt-4.1-mini",
        tools=[{"type": "web_search_preview"}],
        input=prompt,
    )

    _MAX_ANSWER_CHARS = 800
    full_answer = response.output_text or ""
    answer = full_answer[:_MAX_ANSWER_CHARS]

    # 응답 annotations에서 인용 URL + 문맥 스니펫 추출
    citations: list[dict] = []
    seen_urls: set[str] = set()
    for item in response.output:
        content = getattr(item, "content", None)
        if not content:
            continue
        for part in content:
            annotations = getattr(part, "annotations", None)
            if not annotations:
                continue
            for ann in annotations:
                url = getattr(ann, "url", None)
                title = getattr(ann, "title", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    # answer 텍스트에서 인용 위치 기반 문맥 추출
                    snippet = ""
                    start = getattr(ann, "start_index", None)
                    end = getattr(ann, "end_index", None)
                    if start is not None and end is not None and full_answer:
                        ctx_start = max(0, full_answer.rfind(".", 0, start) + 1)
                        ctx_end = full_answer.find(".", end)
                        if ctx_end < 0:
                            ctx_end = min(len(full_answer), end + 120)
                        else:
                            ctx_end += 1
                        snippet = full_answer[ctx_start:ctx_end].strip()[:200]
                    citations.append({
                        "url": url,
                        "title": title,
                        "context_snippet": snippet,
                    })

    result = {
        "query": query,
        "answer": answer,
        "citations": [c["url"] for c in citations[:num]],
        "citation_details": citations[:num],
        "total": len(citations),
    }

    logger.info(
        "AI 검색 완료: query=%s, %d개 인용",
        query,
        len(citations),
    )
    return json.dumps(result, ensure_ascii=False)
