"""슬랙 봇 진입점.

실행:
    python -m interfaces.slack.app
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger(__name__)

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .config import SlackConfig
from . import handlers, scheduler as scheduler_mod


async def main() -> None:
    config = SlackConfig.from_env()

    app = AsyncApp(token=config.bot_token)
    handlers.register(app, config)

    # APScheduler: 매월 1~7일 평일 10:30 정규 요청
    sched = scheduler_mod.setup_scheduler(app.client, config.channel_id)
    sched.start()
    logger.info(
        "스케줄러 시작 — 매월 1~7일 평일 10:30 체크 (채널: %s)", config.channel_id
    )

    socket_handler = AsyncSocketModeHandler(app, config.app_token)
    logger.info("슬랙 봇 시작 (Socket Mode, client=%s)", config.client_name)
    await socket_handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
