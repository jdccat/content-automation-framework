"""네이버 + 구글 자동완성 검색어 제안 도구.

인증 불필요. 사람들이 실제로 검색창에 입력하는 쿼리를 수집한다.
"""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger(__name__)

NAVER_AC_URL = "https://ac.search.naver.com/nx/ac"
GOOGLE_AC_URL = "https://suggestqueries.google.com/complete/search"


async def search_suggestions(keyword: str) -> str:
    """키워드의 네이버 + 구글 자동완성 제안을 수집한다.

    Args:
        keyword: 자동완성 제안을 조회할 검색어.

    Returns:
        JSON — {naver: [제안 목록], google: [제안 목록]}.
    """
    if not keyword or not keyword.strip():
        return json.dumps({"error": "키워드를 입력하세요."})

    keyword = keyword.strip()
    naver_suggestions = await _naver_autocomplete(keyword)
    google_suggestions = await _google_autocomplete(keyword)

    result = {
        "keyword": keyword,
        "naver": naver_suggestions,
        "google": google_suggestions,
    }
    logger.info(
        "자동완성 조회: %s → 네이버 %d개, 구글 %d개",
        keyword, len(naver_suggestions), len(google_suggestions),
    )
    return json.dumps(result, ensure_ascii=False)


async def _naver_autocomplete(keyword: str) -> list[str]:
    """네이버 자동완성 API 호출."""
    params = {
        "q": keyword,
        "con": "1",
        "frm": "nv",
        "ans": "2",
        "r_format": "json",
        "r_enc": "UTF-8",
        "r_unicode": "0",
        "t_koreng": "1",
        "run": "2",
        "rev": "4",
        "q_enc": "UTF-8",
        "st": "100",
    }
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        async with httpx.AsyncClient(timeout=5, headers=headers) as client:
            resp = await client.get(NAVER_AC_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        suggestions = []
        for group in data.get("items", []):
            if not isinstance(group, list):
                continue
            for entry in group:
                if isinstance(entry, list) and len(entry) >= 1:
                    text = str(entry[0])
                elif isinstance(entry, str):
                    text = entry
                else:
                    continue
                if text and text != keyword:
                    suggestions.append(text)
        return suggestions[:10]
    except Exception as e:
        logger.warning("네이버 자동완성 조회 실패: %s", e)
        return []


async def _google_autocomplete(keyword: str) -> list[str]:
    """구글 자동완성 API 호출."""
    params = {
        "client": "chrome",
        "q": keyword,
        "hl": "ko",
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(GOOGLE_AC_URL, params=params)
            resp.raise_for_status()
            # Google Suggest 응답 인코딩: EUC-KR → UTF-8 → latin-1 순으로 시도
            raw = resp.content
            for enc in ("euc-kr", "utf-8", "latin-1"):
                try:
                    raw_text = raw.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                raw_text = raw.decode("latin-1")
            data = json.loads(raw_text)

        # 응답 형식: ["query", ["suggestion1", "suggestion2", ...], ...]
        if isinstance(data, list) and len(data) >= 2:
            raw = data[1]
            if isinstance(raw, list):
                return [s for s in raw if isinstance(s, str) and s != keyword][:10]
        return []
    except Exception as e:
        logger.warning("구글 자동완성 조회 실패: %s", e)
        return []
