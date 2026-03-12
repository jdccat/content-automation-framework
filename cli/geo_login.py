"""GEO 서비스 브라우저 로그인 헬퍼.

각 서비스에 대해 headed 브라우저를 열고, 로그인 완료까지 대기한 후 프로필을 저장합니다.

Usage:
    .venv/bin/python experiments/claude_researcher/geo_login.py claude
    .venv/bin/python experiments/claude_researcher/geo_login.py gemini
    .venv/bin/python experiments/claude_researcher/geo_login.py all
"""

import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_PROFILE_DIR = _PROJECT_ROOT / ".browser_profiles"

SERVICES = {
    "claude": "https://claude.ai/login",
    "gemini": "https://accounts.google.com/signin",
}

# 로그인 완료 판별 URL 패턴
LOGIN_SUCCESS = {
    "claude": "claude.ai/new",
    "gemini": "gemini.google.com",
}


async def login(service: str, timeout: int = 180) -> bool:
    """headed 브라우저를 열고 로그인 완료를 자동 감지한다."""
    from playwright.async_api import async_playwright

    url = SERVICES.get(service)
    if not url:
        print(f"Unknown service: {service}. Available: {', '.join(SERVICES)}")
        return False

    profile_path = _PROFILE_DIR / service
    profile_path.mkdir(parents=True, exist_ok=True)

    print(f"\n  [{service}] 브라우저를 엽니다. 로그인을 완료해주세요.")
    print(f"  최대 {timeout}초 대기합니다.\n")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile_path),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url)

        success_pattern = LOGIN_SUCCESS[service]
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(3)
            elapsed += 3
            current = page.url
            if success_pattern in current:
                print(f"  [{service}] 로그인 성공 감지: {current}")
                await asyncio.sleep(2)  # 쿠키 저장 대기
                break
        else:
            print(f"  [{service}] 타임아웃 ({timeout}초). 현재 URL: {page.url}")
            print(f"  프로필은 저장됩니다. 로그인이 완료되었다면 정상 작동합니다.")

        await ctx.close()
        print(f"  [{service}] 프로필 저장 완료: {profile_path}\n")
        return True


async def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "all"

    if target == "all":
        for svc in SERVICES:
            await login(svc)
    elif target in SERVICES:
        await login(target)
    else:
        print(f"Usage: geo_login.py [{'|'.join(SERVICES)}|all]")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
