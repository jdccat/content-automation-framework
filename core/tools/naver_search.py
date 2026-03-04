"""네이버 블로그 검색 API 도구."""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

BLOG_SEARCH_URL = "https://openapi.naver.com/v1/search/blog"


def _strip_html(text: str) -> str:
    """네이버 API 응답에서 HTML 태그를 제거한다."""
    return re.sub(r"<[^>]+>", "", text)


async def naver_blog_search(query: str, display: int = 5) -> str:
    """네이버 블로그 검색 API로 경쟁 콘텐츠 URL을 탐색한다.

    Args:
        query: 검색 키워드.
        display: 검색 결과 개수 (1~100, 기본 5).

    Returns:
        JSON 문자열 — 블로그 검색 결과 목록.
    """
    client_id = os.environ.get("NAVER_CLIENT_ID", "")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return json.dumps(
            {"error": "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 설정되지 않았습니다."},
            ensure_ascii=False,
        )

    display = max(1, min(display, 100))

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    params = {
        "query": query,
        "display": display,
        "sort": "sim",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(BLOG_SEARCH_URL, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    items = [
        {
            "title": _strip_html(item.get("title", "")),
            "link": item.get("link", ""),
            "description": _strip_html(item.get("description", "")),
            "postdate": item.get("postdate", ""),
            "blogger_name": item.get("bloggername", ""),
        }
        for item in data.get("items", [])
    ]

    result = {
        "query": query,
        "total": data.get("total", 0),
        "items": items,
    }

    logger.info("네이버 블로그 검색 완료: query=%s, %d건", query, len(items))
    return json.dumps(result, ensure_ascii=False)
