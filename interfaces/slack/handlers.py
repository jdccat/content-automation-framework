"""슬랙 이벤트 핸들러 (thin wrapper).

이벤트만 받아서 content-strategist 모듈로 위임.

감지하는 이벤트/액션:
  1. /content 슬래시 커맨드 → 스레드 생성 + 모달 오픈
  2. open_content_modal 버튼 → 스레드 생성 + 모달 오픈
  3. content_input_modal 제출 → 질문 저장
  4. content_input_modal 닫기 → 수집 완료 요약 + 리서치 시작 버튼
  5. pipeline_next_question 버튼 → 질문 추가 모달 오픈
  6. pipeline_start_research 버튼 → 순차 리서치 실행
  7. pipeline_finish 버튼 → 플래닝 트리거
  8. app_mention → 입력하기 버튼 응답
  9. message → 파이프라인 스레드 텍스트 입력 시 모달 안내
  10. feedback_approve_* → 콘텐츠 승인
  11. feedback_seed_change_* → 시드 변경 모달
  12. feedback_meta_change_* → 메타 변경 모달
  13. feedback_seed_modal 제출 → 시드 변경 재실행
  14. feedback_meta_modal 제출 → 메타 변경 재실행
  15. feedback_process_issues → GitHub Issues 배치 처리
  16. feedback_complete → 피드백 완료, 세션 정리
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import uuid
from pathlib import Path

from slack_bolt.async_app import AsyncApp

# content-strategist 패키지 import
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "agents"))
from content_strategist import formatter, state  # noqa: E402
from content_strategist.orchestrator import ContentStrategist  # noqa: E402
from content_strategist.state import PipelineSession  # noqa: E402

from .config import SlackConfig  # noqa: E402
from .parser import _next_month  # noqa: E402

logger = logging.getLogger(__name__)


def register(app: AsyncApp, config: SlackConfig) -> None:
    strategist = ContentStrategist(config)

    # ── 공통: 스레드 생성 + 세션 생성 + 모달 오픈 ──────────────────

    async def _start_session_and_open_modal(
        trigger_id: str, channel_id: str, user_id: str, client,
    ) -> None:
        """스레드 메시지 게시 → 세션 생성 → 질문 모달 오픈."""
        resp = await client.chat_postMessage(
            channel=channel_id,
            text=f"<@{user_id}> {formatter.pipeline_session_start()}",
        )
        thread_ts: str = resp["ts"]

        run_id = uuid.uuid4().hex[:12]
        session = PipelineSession(
            run_id=run_id,
            intent="",
            content_direction="",
            target_month=_next_month(),
        )
        state.pipeline_sessions[thread_ts] = session

        private_metadata = json.dumps({
            "channel_id": channel_id,
            "user_id": user_id,
            "thread_ts": thread_ts,
        })
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
        """/content → 스레드 생성 + 모달 오픈."""
        await ack()
        await _start_session_and_open_modal(
            body["trigger_id"], body["channel_id"], body["user_id"], client,
        )

    # ── 버튼 클릭: "입력하기" (새 세션) ─────────────────────────────

    @app.action("open_content_modal")
    async def handle_open_modal_button(ack, body, client):
        """입력하기 버튼 → 스레드 생성 + 모달 오픈."""
        await ack()
        channel_id = body["channel"]["id"]
        user_id = body["user"]["id"]
        await _start_session_and_open_modal(
            body["trigger_id"], channel_id, user_id, client,
        )

    # ── 버튼 클릭: "질문 추가하기" (기존 세션) ────────────────────────

    @app.action("pipeline_next_question")
    async def handle_next_question(ack, body, client):
        """질문 추가 버튼 → 기존 세션 스레드에 모달 오픈."""
        await ack()
        channel = body["channel"]["id"]
        user_id = body["user"]["id"]
        msg = body["message"]
        thread_ts = msg.get("thread_ts") or msg["ts"]

        session = state.pipeline_sessions.get(thread_ts)
        if not session:
            return

        if session.processing:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=":hourglass: 리서치 진행 중입니다. 완료 후 다시 눌러주세요.",
            )
            return

        private_metadata = json.dumps({
            "channel_id": channel,
            "user_id": user_id,
            "thread_ts": thread_ts,
        })
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=formatter.build_modal(
                private_metadata=private_metadata,
                initial_month=session.target_month,
                initial_intent=session.intent,
                initial_direction=session.content_direction,
            ),
        )

    # ── 모달 제출 → 질문 저장만 ──────────────────────────────────

    @app.view("content_input_modal")
    async def handle_modal_submit(ack, body, view, client):
        """모달 제출 → 질문을 세션에 저장 + 스레드에 등록 메시지."""
        values = view["state"]["values"]
        question: str = (
            values["questions_block"]["questions_input"]["value"] or ""
        ).strip()
        intent: str = (
            values["intent_block"]["intent_select"]["selected_option"]["value"]
        )
        direction: str = (
            values["direction_block"]["direction_select"]["selected_option"]["value"]
        )
        month_raw: str = (
            (values.get("month_block") or {})
            .get("month_input", {})
            .get("value") or ""
        )

        meta = json.loads(view.get("private_metadata") or "{}")
        channel = meta.get("channel_id") or config.channel_id
        thread_ts = meta.get("thread_ts")

        if not thread_ts or thread_ts not in state.pipeline_sessions:
            await ack(response_action="clear")
            return

        if not question:
            await ack()
            return

        session = state.pipeline_sessions[thread_ts]

        if session.processing:
            await ack(response_action="errors", errors={
                "questions_block": "리서치 진행 중에는 질문을 추가할 수 없습니다.",
            })
            return

        # 세션 메타 업데이트
        session.intent = intent
        session.content_direction = direction
        m = re.search(r"\d{4}-\d{2}", month_raw)
        if m:
            session.target_month = m.group(0)

        # 질문 저장
        session.questions.append(question)
        n = len(session.questions)

        # 모달을 즉시 빈 폼으로 리프레시 (의도·방향성 유지)
        new_metadata = json.dumps({
            "channel_id": channel,
            "user_id": meta.get("user_id", ""),
            "thread_ts": thread_ts,
        })
        await ack(
            response_action="update",
            view=formatter.build_modal(
                private_metadata=new_metadata,
                initial_month=session.target_month,
                initial_intent=intent,
                initial_direction=direction,
            ),
        )

        # 스레드에 등록 확인 메시지
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":pencil: 질문 {n} 등록: _{question[:50]}_",
        )

    # ── 모달 닫기 → 수집 완료 요약 ──────────────────────────────

    @app.view_closed("content_input_modal")
    async def handle_modal_close(ack, body, view, client):
        """모달 닫기(입력 완료) → 수집된 질문 요약 + 리서치 시작 버튼."""
        await ack()
        meta = json.loads(view.get("private_metadata") or "{}")
        channel = meta.get("channel_id")
        thread_ts = meta.get("thread_ts")

        if not thread_ts or thread_ts not in state.pipeline_sessions:
            return

        session = state.pipeline_sessions[thread_ts]

        if not session.questions:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=":information_source: 등록된 질문이 없습니다. 입력하기 버튼으로 다시 시작해 주세요.",
            )
            state.pipeline_sessions.pop(thread_ts, None)
            return

        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=formatter.pipeline_questions_collected_blocks(session.questions),
            text=f"질문 {len(session.questions)}개 등록 완료",
        )

    # ── 리서치 시작 버튼 → 순차 처리 ───────────────────────────────

    @app.action("pipeline_start_research")
    async def handle_start_research(ack, body, client):
        """리서치 시작 버튼 → 순차 리서치 + 콘텐츠 설계 실행."""
        await ack()
        channel = body["channel"]["id"]
        msg = body["message"]
        thread_ts = msg.get("thread_ts") or msg["ts"]
        session = state.pipeline_sessions.get(thread_ts)
        if not session:
            return

        if session.processing:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=":hourglass: 이미 리서치가 진행 중입니다.",
            )
            return

        asyncio.create_task(
            strategist.run_all_questions(session, client, channel, thread_ts)
        )

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

    # ── pipeline_finish 버튼 액션 ─────────────────────────────────

    @app.action("pipeline_finish")
    async def handle_pipeline_finish(ack, body, client):
        """종료 버튼 → 플래닝 실행."""
        await ack()
        channel = body["channel"]["id"]
        msg = body["message"]
        thread_ts = msg.get("thread_ts") or msg["ts"]
        session = state.pipeline_sessions.get(thread_ts)
        if not session:
            return

        if session.processing:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=":hourglass: 리서치 진행 중입니다. 완료 후 다시 눌러주세요.",
            )
            return

        if len(session.designer_outputs) < 2:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":warning: 최소 2개 질문이 필요합니다. (현재 {len(session.designer_outputs)}개)",
            )
            return

        asyncio.create_task(
            strategist.run_planner(session, client, channel, thread_ts)
        )

    # ── 피드백: 승인 ─────────────────────────────────────────────

    @app.action(re.compile(r"^feedback_approve_\d+$"))
    async def handle_feedback_approve(ack, body, client):
        """콘텐츠 승인. feedback_pending에서 제거."""
        await ack()
        action_id = body["actions"][0]["action_id"]
        idx = int(action_id.split("_")[-1])
        channel = body["channel"]["id"]
        msg = body["message"]
        thread_ts = msg.get("thread_ts") or msg["ts"]

        session = state.pipeline_sessions.get(thread_ts)
        if session:
            session.feedback_pending.pop(idx, None)

        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":white_check_mark: 콘텐츠 {idx + 1} 승인 완료",
        )

    # ── 피드백: 시드 변경 모달 ───────────────────────────────────

    @app.action(re.compile(r"^feedback_seed_change_\d+$"))
    async def handle_feedback_seed_change(ack, body, client):
        """시드 변경 모달 오픈."""
        await ack()
        action_id = body["actions"][0]["action_id"]
        idx = int(action_id.split("_")[-1])
        channel = body["channel"]["id"]
        msg = body["message"]
        thread_ts = msg.get("thread_ts") or msg["ts"]

        session = state.pipeline_sessions.get(thread_ts)
        if not session or not session.schedule_output:
            return

        try:
            with open(session.schedule_output, encoding="utf-8") as f:
                schedule_data = json.load(f)
            items = schedule_data.get("schedule", [])
            if idx >= len(items):
                return
            item = items[idx]
        except Exception:
            return

        private_metadata = json.dumps({
            "channel_id": channel,
            "thread_ts": thread_ts,
            "question_index": idx,
        })
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=formatter.feedback_seed_modal(item, private_metadata),
        )

    # ── 피드백: 메타 변경 모달 ───────────────────────────────────

    @app.action(re.compile(r"^feedback_meta_change_\d+$"))
    async def handle_feedback_meta_change(ack, body, client):
        """메타 변경 모달 오픈."""
        await ack()
        action_id = body["actions"][0]["action_id"]
        idx = int(action_id.split("_")[-1])
        channel = body["channel"]["id"]
        msg = body["message"]
        thread_ts = msg.get("thread_ts") or msg["ts"]

        session = state.pipeline_sessions.get(thread_ts)
        if not session or not session.schedule_output:
            return

        try:
            with open(session.schedule_output, encoding="utf-8") as f:
                schedule_data = json.load(f)
            items = schedule_data.get("schedule", [])
            if idx >= len(items):
                return
            item = items[idx]
        except Exception:
            return

        private_metadata = json.dumps({
            "channel_id": channel,
            "thread_ts": thread_ts,
            "question_index": idx,
        })
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=formatter.feedback_meta_modal(item, private_metadata),
        )

    # ── 피드백: 시드 변경 모달 제출 ──────────────────────────────

    @app.view("feedback_seed_modal")
    async def handle_feedback_seed_submit(ack, body, view, client):
        """시드 변경 제출 → strategist.run_feedback_rerun("seed_change")."""
        await ack()
        meta = json.loads(view.get("private_metadata") or "{}")
        channel = meta.get("channel_id")
        thread_ts = meta.get("thread_ts")
        idx = meta.get("question_index", 0)

        session = state.pipeline_sessions.get(thread_ts)
        if not session:
            return

        values = view["state"]["values"]
        new_question = (
            values["new_question_block"]["new_question_input"]["value"] or ""
        ).strip()

        asyncio.create_task(
            strategist.run_feedback_rerun(
                session, client, channel, thread_ts,
                question_index=idx,
                feedback_type="seed_change",
                new_input=new_question,
            )
        )

    # ── 피드백: 메타 변경 모달 제출 ──────────────────────────────

    @app.view("feedback_meta_modal")
    async def handle_feedback_meta_submit(ack, body, view, client):
        """메타 변경 제출 → strategist.run_feedback_rerun("meta_change")."""
        await ack()
        meta = json.loads(view.get("private_metadata") or "{}")
        channel = meta.get("channel_id")
        thread_ts = meta.get("thread_ts")
        idx = meta.get("question_index", 0)

        session = state.pipeline_sessions.get(thread_ts)
        if not session:
            return

        values = view["state"]["values"]
        change_instruction = (
            values["meta_change_block"]["meta_change_input"]["value"] or ""
        ).strip()

        asyncio.create_task(
            strategist.run_feedback_rerun(
                session, client, channel, thread_ts,
                question_index=idx,
                feedback_type="meta_change",
                new_input=change_instruction,
            )
        )

    # ── 피드백: GitHub Issues 배치 처리 ──────────────────────────

    @app.action("feedback_process_issues")
    async def handle_process_github_issues(ack, body, client):
        """GitHub Issues 배치 처리 트리거."""
        await ack()
        channel = body["channel"]["id"]
        msg = body["message"]
        thread_ts = msg.get("thread_ts") or msg["ts"]

        session = state.pipeline_sessions.get(thread_ts)
        if not session:
            return

        asyncio.create_task(
            strategist.collect_and_process_github_feedback(
                session, client, channel, thread_ts,
            )
        )

    # ── 피드백: 완료 ─────────────────────────────────────────────

    @app.action("feedback_complete")
    async def handle_feedback_complete(ack, body, client):
        """피드백 완료. 세션 정리."""
        await ack()
        channel = body["channel"]["id"]
        msg = body["message"]
        thread_ts = msg.get("thread_ts") or msg["ts"]

        session = state.pipeline_sessions.get(thread_ts)
        if session:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=formatter.feedback_complete_blocks([]),
                text="피드백 완료",
            )
            state.pipeline_sessions.pop(thread_ts, None)

    # ── 메시지 이벤트 ─────────────────────────────────────────────

    @app.event("message")
    async def handle_message(event, client):
        if event.get("subtype") or event.get("bot_id"):
            return

        channel: str = event["channel"]
        thread_ts: str | None = event.get("thread_ts")
        text: str = event.get("text", "").strip()

        if not text:
            return

        # 파이프라인 세션 스레드에서 텍스트 입력 → 모달 안내
        if thread_ts and thread_ts in state.pipeline_sessions:
            session = state.pipeline_sessions[thread_ts]
            if session.processing:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=":hourglass: 리서치 진행 중입니다. 완료 후 다시 입력해 주세요.",
                )
                return
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "아래 버튼으로 질문을 입력해 주세요.",
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "pipeline_next_question",
                                "text": {"type": "plain_text", "text": "질문 입력하기"},
                                "style": "primary",
                            }
                        ],
                    },
                ],
                text="아래 버튼으로 질문을 입력해 주세요.",
            )
            return
