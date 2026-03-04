"""Playwright 기반 AI 서비스 GEO 인용 수집.

지원 서비스:
- Claude: claude.ai (초기 1회 수동 로그인 필요)
- Gemini: gemini.google.com (초기 1회 수동 로그인 필요)

ChatGPT → ai_search.py (API), Perplexity → perplexity_search.py (API) 사용.

사전 설정:
  pip install playwright
  playwright install chromium

초기 로그인 (headed 브라우저):
  python -m core.tools.geo_browser --login claude
  python -m core.tools.geo_browser --login gemini
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 브라우저 프로필 저장 경로
_PROFILE_DIR = Path(__file__).parent.parent.parent / ".browser_profiles"

# 전역 동시 브라우저 수 제한
_BROWSER_SEM = asyncio.Semaphore(2)

# 인용 URL로 사용하지 않을 도메인
_SKIP_DOMAINS = frozenset({
    "claude.ai",
    "gemini.google.com",
    "accounts.google.com",
    "myaccount.google.com",
    "support.google.com",
    "policies.google.com",
    "www.google.com",
    "login.microsoftonline.com",
    "anthropic.com",
    "www.anthropic.com",
})


def _is_citation_url(url: str) -> bool:
    """외부 인용으로 유효한 URL인지 확인."""
    if not url or not url.startswith("http"):
        return False
    host = urlparse(url).hostname or ""
    return host not in _SKIP_DOMAINS and "." in host and len(url) > 15


def _profile_path(service: str) -> str:
    path = _PROFILE_DIR / service
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _has_profile(service: str) -> bool:
    """로그인 프로필이 존재하는지 확인."""
    path = _PROFILE_DIR / service
    return path.exists() and any(path.iterdir())


async def _launch_context(pw, service: str, headless: bool = True):
    """persistent context 생성 (쿠키/로그인 상태 유지)."""
    return await pw.chromium.launch_persistent_context(
        _profile_path(service),
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        viewport={"width": 1280, "height": 800},
    )


async def _extract_links(page) -> list[dict]:
    """페이지에서 외부 인용 링크를 추출."""
    citations: list[dict] = []
    seen: set[str] = set()
    try:
        links = await page.locator("a[href]").all()
        for a in links:
            try:
                href = await a.get_attribute("href") or ""
                if _is_citation_url(href) and href not in seen:
                    seen.add(href)
                    title = ""
                    try:
                        if await a.is_visible():
                            title = (await a.inner_text(timeout=1000)).strip()[:100]
                    except Exception:
                        pass
                    citations.append({"url": href, "title": title})
            except Exception:
                continue
    except Exception:
        pass
    return citations


def _empty_result(query: str) -> str:
    return json.dumps(
        {"query": query, "answer": "", "citations": [], "citation_details": []},
        ensure_ascii=False,
    )


# ── Claude ───────────────────────────────────────────────────

async def geo_claude_browser(query: str) -> str:
    """Claude.ai 브라우저로 GEO 인용 URL을 수집한다.

    초기 1회 수동 로그인 필요:
      python -m core.tools.geo_browser --login claude

    Returns:
        JSON: {"query", "answer", "citations": [{"url", "context_summary"}]}
    """
    if not query or not query.strip():
        return _empty_result(query)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("geo_claude: playwright 미설치")
        return _empty_result(query)

    if not _has_profile("claude"):
        logger.warning(
            "geo_claude: 로그인 필요 — python -m core.tools.geo_browser --login claude"
        )
        return _empty_result(query)

    async with _BROWSER_SEM:
        try:
            async with async_playwright() as pw:
                ctx = await _launch_context(pw, "claude")
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                await page.goto(
                    "https://claude.ai/new",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                await page.wait_for_timeout(3000)

                # 메시지 입력 — contenteditable 영역 또는 textarea
                editor = page.locator(
                    "[contenteditable='true'], textarea"
                ).first
                await editor.click()
                await page.keyboard.type(query, delay=30)
                await page.wait_for_timeout(500)
                await page.keyboard.press("Enter")

                # 응답 대기 (최대 60초)
                # 스트리밍 완료 감지: 전송 버튼 재활성화 대기
                try:
                    await page.wait_for_function(
                        """() => {
                            const btns = document.querySelectorAll('button[aria-label]');
                            for (const b of btns) {
                                if (b.textContent.includes('Send') ||
                                    b.getAttribute('aria-label')?.includes('Send')) {
                                    return !b.disabled;
                                }
                            }
                            // 스트리밍 인디케이터 사라짐 확인
                            return !document.querySelector('[data-is-streaming="true"]');
                        }""",
                        timeout=60000,
                    )
                except Exception:
                    # 타임아웃이면 현재 상태로 진행
                    await page.wait_for_timeout(15000)

                # 응답 텍스트 추출
                answer = ""
                try:
                    # Claude 응답 영역 — 여러 셀렉터 시도
                    for sel in [
                        "[data-is-streaming='false'] .prose",
                        ".prose",
                        "[class*='message'] [class*='content']",
                    ]:
                        el = page.locator(sel).last
                        if await el.count() > 0:
                            answer = (await el.inner_text(timeout=5000))[:800]
                            if answer.strip():
                                break
                except Exception:
                    pass

                citations_raw = await _extract_links(page)

                await ctx.close()

                # agent.py 파서와 호환되는 형식
                citations = [
                    {"url": c["url"], "context_summary": c["title"]}
                    for c in citations_raw[:10]
                ]

                result = {
                    "query": query,
                    "answer": answer,
                    "citations": citations,
                }
                logger.info("Claude 브라우저: %d개 인용", len(citations))
                return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.warning("geo_claude 실패: %s", e)
            return _empty_result(query)


# ── Gemini ───────────────────────────────────────────────────

async def geo_gemini_browser(query: str) -> str:
    """Gemini 브라우저로 GEO 인용 URL을 수집한다.

    초기 1회 수동 로그인 필요:
      python -m core.tools.geo_browser --login gemini

    Returns:
        JSON: {"query", "answer", "citations": [{"url", "context_summary"}]}
    """
    if not query or not query.strip():
        return _empty_result(query)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("geo_gemini: playwright 미설치")
        return _empty_result(query)

    if not _has_profile("gemini"):
        logger.warning(
            "geo_gemini: 로그인 필요 — python -m core.tools.geo_browser --login gemini"
        )
        return _empty_result(query)

    async with _BROWSER_SEM:
        try:
            async with async_playwright() as pw:
                ctx = await _launch_context(pw, "gemini")
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                await page.goto(
                    "https://gemini.google.com/",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                await page.wait_for_timeout(3000)

                # 입력 — rich text editor 또는 textarea
                input_el = page.locator(
                    "rich-textarea .ql-editor, textarea, [contenteditable='true']"
                ).first
                await input_el.click()
                await page.keyboard.type(query, delay=30)
                await page.wait_for_timeout(500)

                # 전송 버튼 클릭 (Enter가 줄바꿈일 수 있으므로)
                send_btn = page.locator(
                    "button.send-button, button[aria-label*='Send'], "
                    "button[aria-label*='전송'], button[mattooltip*='Send']"
                ).first
                try:
                    await send_btn.click(timeout=3000)
                except Exception:
                    await page.keyboard.press("Enter")

                # 응답 대기 — 로딩 인디케이터 사라짐 확인
                try:
                    await page.wait_for_function(
                        """() => {
                            const loading = document.querySelector(
                                '.loading-indicator, .thinking-indicator, '
                                + '[class*="loading"], mat-progress-bar'
                            );
                            return !loading || loading.offsetParent === null;
                        }""",
                        timeout=60000,
                    )
                    await page.wait_for_timeout(3000)  # 렌더링 완료 여유
                except Exception:
                    await page.wait_for_timeout(20000)

                # 응답 텍스트 추출
                answer = ""
                try:
                    for sel in [
                        "model-response .response-container",
                        "model-response",
                        "message-content",
                        "[class*='response-content']",
                        ".model-response-text",
                    ]:
                        el = page.locator(sel).last
                        if await el.count() > 0:
                            answer = (await el.inner_text(timeout=5000))[:800]
                            if answer.strip():
                                break
                except Exception:
                    pass

                citations_raw = await _extract_links(page)

                await ctx.close()

                citations = [
                    {"url": c["url"], "context_summary": c["title"]}
                    for c in citations_raw[:10]
                ]

                result = {
                    "query": query,
                    "answer": answer,
                    "citations": citations,
                }
                logger.info("Gemini 브라우저: %d개 인용", len(citations))
                return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.warning("geo_gemini 실패: %s", e)
            return _empty_result(query)


# ── CLI: 초기 수동 로그인 ────────────────────────────────────

async def _manual_login(service: str) -> None:
    """수동 로그인을 위해 headed 브라우저를 실행한다."""
    from playwright.async_api import async_playwright

    urls = {
        "claude": "https://claude.ai/login",
        "gemini": "https://accounts.google.com/signin",
    }
    url = urls.get(service)
    if not url:
        print(f"지원하지 않는 서비스: {service} (claude, gemini)")
        return

    print(f"\n  {service} 로그인 브라우저를 엽니다.")
    print("  로그인 완료 후 터미널에서 Enter를 누르세요.\n")

    async with async_playwright() as pw:
        ctx = await _launch_context(pw, service, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url)
        input("  로그인 완료 후 Enter → ")
        await ctx.close()
        print(f"  {service} 로그인 상태 저장: {_profile_path(service)}")


if __name__ == "__main__":
    import sys

    if "--login" in sys.argv:
        idx = sys.argv.index("--login")
        svc = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if svc in ("claude", "gemini"):
            asyncio.run(_manual_login(svc))
        else:
            print("사용법: python -m core.tools.geo_browser --login <claude|gemini>")
    else:
        print("사용법: python -m core.tools.geo_browser --login <claude|gemini>")
