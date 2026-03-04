"""슬랙 이벤트 핸들러.

감지하는 이벤트/액션:
  1. /content 슬래시 커맨드 → 입력 모달 오픈
  2. content_input_modal 제출 → 파이프라인 실행
  3. app_mention  — 채널에서 @봇 멘션 (텍스트 직접 입력 fallback)
  4. message (im) — DM 직접 입력 (텍스트 직접 입력 fallback)
  5. message      — 정규 요청 스레드에 달린 응답
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from slack_bolt.async_app import AsyncApp

from . import formatter, state
from .config import SlackConfig
from . import parser
from .parser import PipelineParams, _next_month
from .runner import PipelineRunner

logger = logging.getLogger(__name__)


def register(app: AsyncApp, config: SlackConfig) -> None:
    runner = PipelineRunner(config)

    # ── 공통: 모달 오픈 헬퍼 ────────────────────────────────────────

    async def _open_modal(trigger_id: str, channel_id: str, user_id: str, client) -> None:
        private_metadata = json.dumps({"channel_id": channel_id, "user_id": user_id})
        await client.views_open(
            trigger_id=trigger_id,
            view=formatter.build_modal(
                private_metadata=private_metadata,
                initial_month=_next_month(),
            ),
        )

    # ── 슬래시 커맨드: /content ───────────────────────────────────

    @app.command("/content")
    async def handle_content_command(ack, body, client):
        """/content 입력 시 드롭다운 모달 오픈."""
        await ack()
        await _open_modal(body["trigger_id"], body["channel_id"], body["user_id"], client)

    # ── 버튼 클릭: "입력하기" ────────────────────────────────────

    @app.action("open_content_modal")
    async def handle_open_modal_button(ack, body, client):
        """정규 요청 메시지 또는 멘션 응답의 '입력하기' 버튼 클릭."""
        await ack()
        channel_id = body["channel"]["id"]
        user_id = body["user"]["id"]
        await _open_modal(body["trigger_id"], channel_id, user_id, client)

    # ── 모달 제출 ────────────────────────────────────────────────

    @app.view("content_input_modal")
    async def handle_modal_submit(ack, body, view, client):
        """모달 제출 → 값 추출 → 파이프라인 실행."""
        await ack()

        values = view["state"]["values"]
        intent: str = (
            values["intent_block"]["intent_select"]["selected_option"]["value"]
        )
        questions_raw: str = (
            values["questions_block"]["questions_input"]["value"] or ""
        )
        direction: str = (
            values["direction_block"]["direction_select"]["selected_option"]["value"]
        )
        month_raw: str = (
            (values.get("month_block") or {})
            .get("month_input", {})
            .get("value") or ""
        )

        questions = [q.strip() for q in questions_raw.splitlines() if q.strip()]
        m = re.search(r"\d{4}-\d{2}", month_raw)
        target_month = m.group(0) if m else _next_month()

        params = PipelineParams(
            intent=intent,
            questions=questions,
            content_direction=direction,
            target_month=target_month,
        )

        # 채널 및 유저 정보 복원
        meta = json.loads(view.get("private_metadata") or "{}")
        channel = meta.get("channel_id") or config.channel_id
        user_id = meta.get("user_id", "")

        # 채널에 시작 메시지 게시 → thread_ts 확보
        resp = await client.chat_postMessage(
            channel=channel,
            text=f"<@{user_id}> 콘텐츠 계획 분석을 시작합니다.",
        )
        thread_ts: str = resp["ts"]

        asyncio.create_task(runner.run(params, client, channel, thread_ts))

    # ── @멘션 → 버튼 메시지 응답 ────────────────────────────────

    @app.event("app_mention")
    async def handle_mention(event, client):
        """@멘션 시 '입력하기' 버튼이 달린 메시지로 응답."""
        channel: str = event["channel"]
        ts: str = event["ts"]
        await client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            blocks=formatter.build_mention_reply(),
            text="콘텐츠 계획을 입력하시겠어요?",
        )

    # ── DM / 정규 요청 스레드 응답 ────────────────────────────────

    @app.event("message")
    async def handle_message(event, client):
        if event.get("subtype") or event.get("bot_id"):
            return

        channel_type: str = event.get("channel_type", "")
        channel: str = event["channel"]
        ts: str = event["ts"]
        thread_ts: str | None = event.get("thread_ts")
        text: str = event.get("text", "").strip()

        if not text:
            return

        # 케이스 1: 정규 요청 스레드 응답
        if thread_ts and thread_ts in state.scheduled_thread_ts:
            result = parser.parse(text)
            if isinstance(result, parser.ParseError):
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=result.message,
                )
                return
            asyncio.create_task(runner.run(result, client, channel, thread_ts))
            return

        # 케이스 2: DM 최상위 메시지
        if channel_type == "im" and not thread_ts:
            result = parser.parse(text)
            if isinstance(result, parser.ParseError):
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=ts,
                    text=result.message,
                )
                return
            asyncio.create_task(runner.run(result, client, channel, ts))
