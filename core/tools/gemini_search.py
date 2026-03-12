"""Google Gemini API 기반 GEO 인용 수집 도구.

ai_search.py(OpenAI), claude_search.py(Anthropic)와 동일한 인터페이스.
GOOGLE_API_KEY만 있으면 동작. Gemini + Google Search grounding 사용.
Fail-fast: 1회 시도 후 실패 시 즉시 에러 반환 (재시도 없음).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

_MAX_RETRIES = 0  # fail-fast: 재시도 없이 1회 시도 후 즉시 반환


async def gemini_search(query: str, num: int = 5) -> str:
    """Gemini API + Google Search grounding으로 AI 요약 + 인용 URL 반환.

    Args:
        query: 검색 쿼리 (자연어).
        num: 반환할 인용 URL 최대 개수 (기본 5).

    Returns:
        JSON 문자열 — 응답 요약 + citations 리스트.
    """
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return json.dumps(
            {"error": "GOOGLE_API_KEY 또는 GEMINI_API_KEY 환경변수가 설정되지 않았습니다."},
            ensure_ascii=False,
        )

    if not query or not query.strip():
        return json.dumps(
            {"error": "검색어가 비어 있습니다."},
            ensure_ascii=False,
        )

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return json.dumps(
            {"error": "google-genai 패키지가 설치되지 않았습니다. pip install google-genai"},
            ensure_ascii=False,
        )

    client = genai.Client(api_key=api_key)
    _MAX_ANSWER_CHARS = 800

    prompt = (
        f"다음 주제에 대해 웹 검색을 수행하고, "
        f"다양한 소스를 인용하여 한국어로 종합적으로 답변하세요: {query}\n\n"
        "가능한 한 많은 출처를 참조하세요."
    )

    response = None
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
    except Exception as e:
        logger.warning("Gemini search 실패 (fail-fast): %s", e)
        return json.dumps(
            {"error": f"Gemini API 호출 실패: {e}", "query": query},
            ensure_ascii=False,
        )

    if response is None:
        return json.dumps({"error": "Gemini API 응답 없음", "query": query}, ensure_ascii=False)

    # 텍스트 답변 추출
    full_answer = ""
    try:
        if response.text:
            full_answer = response.text
    except Exception:
        for candidate in getattr(response, "candidates", []):
            for part in getattr(candidate.content, "parts", []):
                if hasattr(part, "text") and part.text:
                    full_answer += part.text

    # grounding metadata에서 인용 URL 추출
    citations: list[dict] = []
    seen_urls: set[str] = set()

    for candidate in getattr(response, "candidates", []):
        grounding = getattr(candidate, "grounding_metadata", None)
        if not grounding:
            continue

        # grounding_chunks: 개별 인용 소스
        for chunk in getattr(grounding, "grounding_chunks", []):
            web = getattr(chunk, "web", None)
            if not web:
                continue
            url = getattr(web, "uri", "") or ""
            title = getattr(web, "title", "") or ""
            if url and url not in seen_urls:
                seen_urls.add(url)
                citations.append({
                    "url": url,
                    "title": title,
                    "context_snippet": "",
                })

        # grounding_supports: 텍스트 구간별 인용 매핑
        for support in getattr(grounding, "grounding_supports", []):
            snippet = getattr(support, "segment", None)
            snippet_text = getattr(snippet, "text", "") if snippet else ""
            for idx in getattr(support, "grounding_chunk_indices", []):
                if idx < len(citations) and snippet_text:
                    existing = citations[idx].get("context_snippet", "")
                    if not existing:
                        citations[idx]["context_snippet"] = snippet_text[:200]

    answer = full_answer[:_MAX_ANSWER_CHARS]

    result = {
        "query": query,
        "answer": answer,
        "citations": [c["url"] for c in citations[:num]],
        "citation_details": citations[:num],
        "total": len(citations),
    }

    logger.info(
        "Gemini 검색 완료: query=%s, %d개 인용",
        query,
        len(citations),
    )
    return json.dumps(result, ensure_ascii=False)
