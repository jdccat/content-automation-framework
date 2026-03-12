"""슬랙 봇 진입점.

실행:
    python -m interfaces.slack.app
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger(__name__)

# content-strategist 패키지 경로 추가
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "agents"))
from content_strategist import formatter  # noqa: E402

from slack_bolt.async_app import AsyncApp  # noqa: E402
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler  # noqa: E402

from .config import SlackConfig  # noqa: E402
from . import handlers, scheduler as scheduler_mod  # noqa: E402


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

    # 봇 시작 시 채널에 입력 버튼 게시
    try:
        await app.client.chat_postMessage(
            channel=config.channel_id,
            blocks=formatter.build_mention_reply(),
            text="콘텐츠 계획을 입력하시겠어요?",
        )
        logger.info("시작 메시지 게시 완료 (채널: %s)", config.channel_id)
    except Exception as exc:
        logger.warning("시작 메시지 게시 실패: %s", exc)

    socket_handler = AsyncSocketModeHandler(app, config.app_token)
    logger.info("슬랙 봇 시작 (Socket Mode, client=%s)", config.client_name)
    await socket_handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
