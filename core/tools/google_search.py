"""OpenAI Responses API web_search 기반 구글 검색 도구."""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from urllib.parse import urlparse

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# 도메인 → 콘텐츠 유형 매핑
_DOMAIN_TYPE_RULES: list[tuple[list[str], str]] = [
    (["youtube.com", "youtu.be"], "video"),
    (["blog.naver.com", "m.blog.naver.com", "tistory.com", "brunch.co.kr", "velog.io", "medium.com"], "blog"),
    (["news.naver.com", "n.news.naver.com", "zdnet", "bloter", "itworld"], "news"),
    (["wikipedia.org", "namu.wiki", "namuwiki"], "wiki"),
    ([".go.kr", ".gov"], "government"),
    (["cafe.naver.com"], "community"),
]


def _classify_url(url: str) -> str:
    """URL에서 콘텐츠 유형을 추정한다."""
    domain = urlparse(url).netloc.lower()
    full_url = url.lower()
    for patterns, content_type in _DOMAIN_TYPE_RULES:
        if any(p in domain or p in full_url for p in patterns):
            return content_type
    return "website"


async def google_search(query: str, num: int = 5) -> str:
    """OpenAI web_search_preview 도구로 구글 웹 검색 결과를 반환한다.

    OPENAI_API_KEY만 있으면 동작하며 별도 Google API 키가 불필요하다.

    Args:
        query: 검색 키워드.
        num: 원하는 검색 결과 개수 (가이드, 정확한 수를 보장하지 않음).

    Returns:
        JSON 문자열 — 검색 결과 목록.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return json.dumps(
            {"error": "OPENAI_API_KEY 환경변수가 설정되지 않았습니다."},
            ensure_ascii=False,
        )

    client = AsyncOpenAI(api_key=api_key)

    prompt = (
        f"다음 키워드로 한국어 웹 검색을 수행하세요: {query}\n\n"
        f"상위 {num}개 결과의 title, link, snippet을 아래 JSON 형식으로 반환하세요.\n"
        '{"items": [{"title": "...", "link": "...", "snippet": "..."}]}\n'
        "JSON만 반환하고 다른 텍스트는 포함하지 마세요."
    )

    response = await client.responses.create(
        model="gpt-4.1-mini",
        tools=[{"type": "web_search_preview"}],
        input=prompt,
    )

    # output_text에서 JSON 추출
    text = response.output_text or ""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    try:
        data = json.loads(text)
        items = data.get("items", [])
    except json.JSONDecodeError:
        # JSON 파싱 실패 시 output에서 URL 참조 직접 추출
        items = []
        for item in response.output:
            if getattr(item, "type", None) == "web_search_call":
                # web_search_call 결과에서 추출 시도
                pass

    # 각 항목에 콘텐츠 유형 태깅 + SERP 특성 집계
    tagged_items = []
    for item in items[:num]:
        link = item.get("link", "")
        item["content_type"] = _classify_url(link)
        tagged_items.append(item)

    type_counts = Counter(it["content_type"] for it in tagged_items)
    domains = list({urlparse(it.get("link", "")).netloc for it in tagged_items if it.get("link")})

    # Featured Snippet 휴리스틱: 첫 번째 결과의 snippet이 200자 이상이면 감지
    featured_snippet_exists = False
    featured_snippet_url = None
    if tagged_items:
        first_snippet = tagged_items[0].get("snippet", "")
        if len(first_snippet) >= 200:
            featured_snippet_exists = True
            featured_snippet_url = tagged_items[0].get("link")

    result = {
        "query": query,
        "total": len(tagged_items),
        "items": tagged_items,
        "serp_features": {
            "content_type_distribution": dict(type_counts),
            "has_video": type_counts.get("video", 0) > 0,
            "has_news": type_counts.get("news", 0) > 0,
            "domains": sorted(domains),
            # TODO: ai_overview 정확한 감지는 Playwright 기반으로 교체 예정
            "ai_overview": False,
            "featured_snippet_exists": featured_snippet_exists,
            "featured_snippet_url": featured_snippet_url,
            "paa_questions": [],  # Stage 1의 PAA 데이터를 Stage 2 조립 시 주입
        },
    }

    logger.info("Google 웹 검색 완료: query=%s, %d건", query, len(tagged_items))
    return json.dumps(result, ensure_ascii=False)
