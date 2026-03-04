"""웹 페이지 콘텐츠 가져오기 도구."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from urllib.parse import urlparse

import httpx

# 동시 크롤링 제한 (호스트별 rate limit 방지)
_FETCH_SEMAPHORE = asyncio.Semaphore(4)

logger = logging.getLogger(__name__)

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)
_DEFAULT_UA = "Mozilla/5.0 (compatible; WishketBot/1.0)"


def _to_naver_mobile_url(url: str) -> str:
    """blog.naver.com URL을 m.blog.naver.com으로 변환한다."""
    parsed = urlparse(url)
    if parsed.hostname == "blog.naver.com":
        return url.replace("://blog.naver.com", "://m.blog.naver.com", 1)
    return url


def _is_naver_blog(url: str) -> bool:
    hostname = urlparse(url).hostname or ""
    return hostname in ("blog.naver.com", "m.blog.naver.com")


def _extract_naver_headings(html: str) -> list[str]:
    """네이버 블로그 스마트에디터의 소제목을 추출한다.

    네이버 블로그는 <h2> 대신 se-fs-fs{size} 클래스의 큰 폰트 span을
    소제목으로 사용한다. 본문 내 최빈 폰트보다 큰 사이즈를 소제목으로 판별.
    """
    # 본문 내 폰트 사이즈 분포 파악
    fs_matches = re.findall(r"se-fs-fs(\d+)", html)
    if not fs_matches:
        return []

    freq = Counter(int(s) for s in fs_matches)
    body_size = freq.most_common(1)[0][0]  # 가장 많이 쓰인 폰트 = 본문

    # 본문 폰트보다 큰 사이즈의 span 추출
    headings: list[str] = []
    pattern = r'<span[^>]*class="[^"]*se-fs-fs(\d+)[^"]*"[^>]*>(.*?)</span>'
    for match in re.finditer(pattern, html, flags=re.DOTALL):
        size = int(match.group(1))
        if size <= body_size:
            continue
        text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        if text and text != "\u200b" and len(text) > 1:
            headings.append(text)

    return headings


_ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml")

# 발행일 추출 패턴 (meta tag 기반)
_DATE_META_PATTERNS: list[re.Pattern[str]] = [
    # Open Graph / article:published_time
    re.compile(
        r'<meta[^>]+property\s*=\s*["\']article:published_time["\'][^>]+content\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]+property\s*=\s*["\']article:published_time["\']',
        re.IGNORECASE,
    ),
    # Schema.org datePublished
    re.compile(
        r'"datePublished"\s*:\s*"([^"]+)"',
    ),
    # <time datetime="...">
    re.compile(
        r'<time[^>]+datetime\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    # meta name="date"
    re.compile(
        r'<meta[^>]+name\s*=\s*["\']date["\'][^>]+content\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
]

_DATE_NORMALIZE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _extract_publish_date(html: str) -> str | None:
    """HTML에서 발행일을 추출한다. YYYY-MM-DD 형식으로 반환."""
    for pattern in _DATE_META_PATTERNS:
        m = pattern.search(html)
        if m:
            raw = m.group(1).strip()
            # YYYY-MM-DD 부분만 추출
            dm = _DATE_NORMALIZE_RE.match(raw)
            if dm:
                return dm.group(0)
            # YYYYMMDD → YYYY-MM-DD
            if re.match(r"^\d{8}$", raw):
                return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


async def web_fetch(url: str, max_chars: int = 5000) -> str:
    """URL의 웹 페이지를 가져와 H2 구조, 글자 수, 본문 텍스트를 반환한다.

    Args:
        url: 가져올 웹 페이지 URL.
        max_chars: 반환할 본문 최대 글자 수 (기본 5000).

    Returns:
        페이지 분석 결과 문자열 (H2 구조, 글자 수, 본문 앞 max_chars자).
    """
    naver_blog = _is_naver_blog(url)
    if naver_blog:
        url = _to_naver_mobile_url(url)

    headers = {
        "User-Agent": _MOBILE_UA if naver_blog else _DEFAULT_UA,
    }

    async with _FETCH_SEMAPHORE:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # HEAD 요청으로 Content-Type 확인 (PDF/이미지 등 제외)
            try:
                head_resp = await client.head(url, headers=headers)
                content_type = head_resp.headers.get("content-type", "")
                if not any(ct in content_type for ct in _ALLOWED_CONTENT_TYPES):
                    return (
                        f"URL: {url}\n"
                        f"H2 구조: []\n"
                        f"글자 수: 0\n"
                        f"본문: [스킵: content-type={content_type}]"
                    )
            except httpx.HTTPError:
                pass  # HEAD 실패 시 GET으로 진행

            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text

    # 발행일 추출 (HTML 파싱 전)
    pub_date = _extract_publish_date(html)

    # H2 추출
    if naver_blog:
        h2_list = _extract_naver_headings(html)
    else:
        h2_matches = re.findall(
            r"<h2[^>]*>(.*?)</h2>", html, flags=re.IGNORECASE | re.DOTALL
        )
        h2_list = [re.sub(r"<[^>]+>", "", h).strip() for h in h2_matches]

    # HTML → 텍스트
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    result = (
        f"URL: {url}\n"
        f"H2 구조: {json.dumps(h2_list, ensure_ascii=False)}\n"
        f"발행일: {pub_date or ''}\n"
        f"글자 수: {len(text)}\n"
        f"본문 (앞 {max_chars}자):\n{text[:max_chars]}"
    )

    logger.info("웹 페이지 가져오기 완료: %s (%d자)", url, len(text))
    return result
