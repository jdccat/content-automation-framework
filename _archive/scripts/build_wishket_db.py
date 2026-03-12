"""위시켓 블로그 기발행 콘텐츠 DB 크롤러.

사용법:
    python build_wishket_db.py                              # 전체 수집
    python build_wishket_db.py --output path/to/out.json    # 경로 지정
    python build_wishket_db.py --limit 50                   # 테스트용 상한
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://blog.wishket.com"
CATEGORY_DIRS = ["/dir/development", "/dir/freelancers", "/dir/ai", "/dir/services", "/dir/it"]
SEMAPHORE_LIMIT = 5
REQUEST_DELAY = 0.5  # seconds between requests per worker
REQUEST_TIMEOUT = 20.0

DOMAIN_KWS = [
    "ERP",
    "앱 개발",
    "외주 개발",
    "프리랜서",
    "견적",
    "계약서",
    "AI 개발",
    "MVP",
    "IT 아웃소싱",
    "스타트업",
    "SI",
    "웹 개발",
    "UI/UX",
    "디자인",
    "백엔드",
    "프론트엔드",
    "클라우드",
    "데이터",
    "보안",
    "DevOps",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


# ── 휴리스틱 함수 ────────────────────────────────────────────────────────────


def _classify_content_type(title: str, h2s: list[str]) -> str:
    if re.search(r"\d+가지|\d+개|\d+단계|\d+가지|\d+선", title):
        return "listicle"
    if re.search(r"비교|vs\.|VS|vs ", title, re.IGNORECASE):
        return "comparison"
    if re.search(r"이란|란[?\s]|란$|뜻|의미", title):
        return "definition"
    if re.search(r"방법|가이드|하는 법|절차|따라하기|튜토리얼", title):
        return "guide"
    return "other"


def _extract_main_keyword(title: str) -> str:
    for kw in DOMAIN_KWS:
        if kw in title:
            return kw
    return title[:10]


# ── HTML 파싱 ────────────────────────────────────────────────────────────────


def _parse_post(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    # 제목
    title_tag = soup.select_one(".heading-3") or soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else ""

    # 발행일 — .day.large 우선, meta 태그 fallback
    publish_date = ""
    day_tag = soup.select_one(".day.large")
    if day_tag:
        raw_date = day_tag.get_text(strip=True)
        # YYYY-MM-DD 형식 추출
        m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw_date)
        if m:
            publish_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if not publish_date:
        meta_dt = soup.find("meta", property="article:published_time")
        if meta_dt and meta_dt.get("content"):
            publish_date = str(meta_dt["content"])[:10]

    # 카테고리
    cat_tag = soup.select_one('[fs-cmsfilter-field="category"].blog-detail-date')
    if not cat_tag:
        cat_tag = soup.select_one('[fs-cmsfilter-field="category"]')
    category = cat_tag.get_text(strip=True) if cat_tag else ""

    # 본문 영역
    body_tag = soup.select_one(".blog-post.w-richtext")

    # H2/H3
    h2_sections: list[str] = []
    if body_tag:
        for tag in body_tag.select("h2, h3"):
            text = tag.get_text(strip=True)
            if text:
                h2_sections.append(text)

    # 단어 수
    word_count = 0
    if body_tag:
        body_text = body_tag.get_text(separator=" ")
        word_count = len(body_text.split())

    # TL;DR excerpt
    excerpt = ""
    tldr_tag = soup.select_one(".tldr-container .text-block-62")
    if tldr_tag and "w-condition-invisible" not in (tldr_tag.get("class") or []):
        excerpt = tldr_tag.get_text(strip=True)
    if not excerpt:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            excerpt = str(meta_desc["content"])
        else:
            og_desc = soup.find("meta", property="og:description")
            if og_desc and og_desc.get("content"):
                excerpt = str(og_desc["content"])

    content_type = _classify_content_type(title, h2_sections)
    main_keyword = _extract_main_keyword(title)

    return {
        "url": url,
        "title": title,
        "main_keyword": main_keyword,
        "publish_date": publish_date or None,
        "funnel": "unclassified",
        "h2_sections": h2_sections,
        "excerpt": excerpt,
        "word_count": word_count,
        "content_type": content_type,
        "category": category,
        "ctr": None,
        "search_rank": None,
        "avg_time_on_page": None,
    }


def _collect_blog_links(html: str) -> set[str]:
    """HTML에서 /blog/ 경로 링크를 수집한다."""
    soup = BeautifulSoup(html, "lxml")
    urls: set[str] = set()
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if href.startswith("/blog/") and len(href) > 7:
            urls.add(BASE_URL + href.rstrip("/"))
        elif href.startswith(BASE_URL + "/blog/"):
            urls.add(href.rstrip("/"))
    return urls


# ── 비동기 크롤러 ────────────────────────────────────────────────────────────


async def _fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        return r.text
    except Exception as exc:
        logger.warning("fetch 실패 %s: %s", url, exc)
        return None


async def _collect_urls(client: httpx.AsyncClient) -> set[str]:
    """Step 1+2: 메인 + 카테고리 페이지에서 포스트 URL 수집."""
    all_urls: set[str] = set()

    # 메인 페이지
    html = await _fetch(client, BASE_URL + "/")
    if html:
        all_urls.update(_collect_blog_links(html))
        # 카테고리 디렉토리 링크도 수집
        soup = BeautifulSoup(html, "lxml")
        extra_dirs: list[str] = []
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            if href.startswith("/dir/") and href not in CATEGORY_DIRS:
                extra_dirs.append(href)
    else:
        extra_dirs = []

    # 카테고리 페이지 순회
    dirs_to_visit = CATEGORY_DIRS + extra_dirs
    for dir_path in dirs_to_visit:
        # 첫 페이지
        dir_url = BASE_URL + dir_path
        html = await _fetch(client, dir_url)
        if not html:
            continue
        found = _collect_blog_links(html)
        all_urls.update(found)
        logger.info("  카테고리 %s: %d개 발견 (누계 %d)", dir_path, len(found), len(all_urls))

        # Finsweet CMS offset 페이지네이션 시도
        offset = 20
        prev_count = len(found)
        while True:
            paged_html = await _fetch(client, f"{dir_url}?offset={offset}&per-page=20")
            if not paged_html:
                break
            paged_found = _collect_blog_links(paged_html)
            # 이전 페이지와 같거나 새 URL이 없으면 종료
            new_urls = paged_found - all_urls
            if not new_urls or len(paged_found) == prev_count:
                break
            all_urls.update(new_urls)
            logger.info(
                "  카테고리 %s offset=%d: +%d개 (누계 %d)",
                dir_path,
                offset,
                len(new_urls),
                len(all_urls),
            )
            prev_count = len(paged_found)
            offset += 20
            await asyncio.sleep(REQUEST_DELAY)

        await asyncio.sleep(REQUEST_DELAY)

    return all_urls


async def _fetch_post(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    url: str,
) -> dict | None:
    async with sem:
        html = await _fetch(client, url)
        await asyncio.sleep(REQUEST_DELAY)
        if not html:
            return None
        return _parse_post(url, html)


async def _run(output_path: Path, limit: int | None) -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        logger.info("Step 1+2: 포스트 URL 수집 중...")
        urls = await _collect_urls(client)
        logger.info("수집된 포스트 URL: %d개", len(urls))

        sorted_urls = sorted(urls)
        if limit:
            sorted_urls = sorted_urls[:limit]
            logger.info("--limit %d 적용: %d개로 제한", limit, len(sorted_urls))

        logger.info("Step 3: 포스트 개별 크롤링 시작...")
        sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
        tasks = [_fetch_post(sem, client, url) for url in sorted_urls]
        results_raw = await asyncio.gather(*tasks)

    results = [r for r in results_raw if r is not None]
    logger.info("크롤링 완료: %d / %d개", len(results), len(sorted_urls))

    # 통계 출력
    total = len(results)
    if total:
        kw_filled = sum(1 for r in results if r["main_keyword"] and len(r["main_keyword"]) > 3)
        h2_filled = sum(1 for r in results if r["h2_sections"])
        wc_filled = sum(1 for r in results if r["word_count"] > 0)
        type_not_other = sum(1 for r in results if r["content_type"] != "other")
        logger.info(
            "통계 — main_keyword: %.0f%%, h2_sections: %.0f%%, word_count>0: %.0f%%, type!=other: %.0f%%",
            kw_filled / total * 100,
            h2_filled / total * 100,
            wc_filled / total * 100,
            type_not_other / total * 100,
        )

    # Step 4: 저장
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    logger.info("저장 완료: %s (%d개)", output_path, len(results))

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="위시켓 블로그 기발행 콘텐츠 DB 크롤러")
    parser.add_argument(
        "--output",
        default="data/wishket_published.json",
        help="출력 JSON 경로 (기본: data/wishket_published.json)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="수집 포스트 상한 (테스트용)",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    results = asyncio.run(_run(output_path, args.limit))
    print(f"\n수집 완료: {len(results)}개 → {output_path}")


if __name__ == "__main__":
    main()
