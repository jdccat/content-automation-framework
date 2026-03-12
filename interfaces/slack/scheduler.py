"""첫 영업일 계산 + APScheduler 설정.

정규 트리거: 매월 1~7일 평일 10:30.
실제 첫 영업일 여부는 job 내부에서 재확인한다.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# content-strategist 패키지 경로 추가
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "agents"))
from content_strategist import formatter, state  # noqa: E402

logger = logging.getLogger(__name__)


def get_first_business_day(year: int, month: int) -> date:
    """해당 월의 첫 번째 평일(월~금) 반환."""
    d = date(year, month, 1)
    while d.weekday() >= 5:  # 5=토, 6=일
        d += timedelta(days=1)
    return d


def is_first_business_day_today() -> bool:
    today = date.today()
    return today == get_first_business_day(today.year, today.month)


def _next_month_str() -> str:
    import calendar as cal_mod
    today = date.today()
    last_day = cal_mod.monthrange(today.year, today.month)[1]
    next_first = date(today.year, today.month, last_day) + timedelta(days=1)
    return next_first.strftime("%Y-%m")


async def _scheduled_job(slack_client, channel_id: str) -> None:
    """10:30 실행. 오늘이 첫 영업일이 아니면 무시."""
    if not is_first_business_day_today():
        return

    target_month = _next_month_str()
    logger.info("정규 트리거 — 채널 %s에 입력 요청 게시 (대상 월: %s)", channel_id, target_month)

    resp = await slack_client.chat_postMessage(
        channel=channel_id,
        blocks=formatter.build_input_request(target_month),
        text=f"{target_month} 콘텐츠 계획 입력 요청",
    )
    # 게시된 메시지 ts를 핸들러가 감지할 수 있도록 state에 등록
    if resp and resp.get("ok"):
        state.scheduled_thread_ts.add(resp["ts"])
        logger.info("정규 요청 ts 등록: %s", resp["ts"])


def setup_scheduler(slack_client, channel_id: str) -> AsyncIOScheduler:
    """APScheduler 반환. app.py에서 start() 호출."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _scheduled_job,
        # 매월 1~7일 평일 10:30 — job 내부에서 첫 영업일 여부 재확인
        CronTrigger(day="1-7", day_of_week="mon-fri", hour=10, minute=30),
        args=[slack_client, channel_id],
        id="monthly_content_request",
        replace_existing=True,
    )
    return scheduler
