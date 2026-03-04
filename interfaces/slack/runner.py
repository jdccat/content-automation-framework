"""파이프라인 비동기 실행 + 슬랙 진행 보고."""

from __future__ import annotations

import calendar as cal_mod
import datetime as dt_mod
import json
import logging
from datetime import date
from pathlib import Path

from core.agents.planner.agent import PlannerAgent
from core.agents.researcher.agent import ResearcherAgent
from core.schemas import ContentPlan, PlannerInput, PublishedContent, ResearchResult

from . import formatter
from .config import SlackConfig
from .parser import PipelineParams

logger = logging.getLogger(__name__)

MAX_RETRIES = 2


class PipelineRunner:
    def __init__(self, config: SlackConfig) -> None:
        self._config = config

    async def run(
        self,
        params: PipelineParams,
        slack_client,
        channel: str,
        thread_ts: str,
    ) -> None:
        """리서처 → 플래너 순차 실행. 단계별 슬랙 메시지 업데이트."""
        await _post(slack_client, channel, thread_ts,
                    formatter.progress(":hourglass_flowing_sand:", "분석을 시작합니다."))

        # ── ResearcherAgent ──────────────────────────────────────
        research_result: ResearchResult | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                research_result = await self._run_researcher(
                    params, slack_client, channel, thread_ts
                )
                break
            except Exception as exc:
                logger.exception("리서처 실패 attempt=%d", attempt)
                if attempt < MAX_RETRIES:
                    await _post(slack_client, channel, thread_ts,
                                formatter.progress(
                                    ":warning:",
                                    f"리서처 오류, 재시도 중... ({attempt}/{MAX_RETRIES})\n`{exc}`",
                                ))
                else:
                    await _post(slack_client, channel, thread_ts,
                                formatter.progress(":x:", f"리서처 실패 — 파이프라인 중단\n`{exc}`"))
                    return

        # ── PlannerAgent ─────────────────────────────────────────
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                plan, output_paths = await self._run_planner(
                    params, research_result, slack_client, channel, thread_ts  # type: ignore[arg-type]
                )
                break
            except Exception as exc:
                logger.exception("플래너 실패 attempt=%d", attempt)
                if attempt < MAX_RETRIES:
                    await _post(slack_client, channel, thread_ts,
                                formatter.progress(
                                    ":warning:",
                                    f"플래너 오류, 재시도 중... ({attempt}/{MAX_RETRIES})\n`{exc}`",
                                ))
                else:
                    await _post(slack_client, channel, thread_ts,
                                formatter.progress(":x:", f"플래너 실패 — 파이프라인 중단\n`{exc}`"))
                    return

        await slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=formatter.summary_blocks(plan, output_paths),
            text=f"콘텐츠 계획 완료 — {plan.target_month} {len(plan.content_pieces)}건",
        )

    # ── 내부 단계 ────────────────────────────────────────────────

    async def _run_researcher(
        self,
        params: PipelineParams,
        slack_client,
        channel: str,
        thread_ts: str,
    ) -> ResearchResult:
        await _post(slack_client, channel, thread_ts,
                    formatter.progress(":mag:", "리서처: 키워드 분석 중..."))

        input_text = (
            f"질문 의도 : {params.intent}\n"
            "질문 형태\n"
            + "\n".join(params.questions)
            + f"\n콘텐츠 방향성 : {params.content_direction}"
        )

        result = await ResearcherAgent().run(input_text)

        cluster_count = len(result.clusters)
        focus_count = sum(1 for c in result.clusters if c.is_focus)
        await _post(slack_client, channel, thread_ts,
                    formatter.progress(
                        ":white_check_mark:",
                        f"리서처 완료 — 클러스터 {cluster_count}개 (포커스 {focus_count}개)",
                    ))
        return result

    async def _run_planner(
        self,
        params: PipelineParams,
        research_result: ResearchResult,
        slack_client,
        channel: str,
        thread_ts: str,
    ) -> tuple[ContentPlan, dict[str, str]]:
        await _post(slack_client, channel, thread_ts,
                    formatter.progress(":memo:", "플래너: 콘텐츠 전략 수립 중..."))

        published = _load_published(self._config.published_db)
        planner_input = PlannerInput(
            intent=[params.intent],
            questions=params.questions,
            content_direction=[params.content_direction],
            research_result=research_result,
            published_contents=published,
            target_month=params.target_month,
            client_name=self._config.client_name,
        )

        plan = await PlannerAgent(client_name=self._config.client_name).run(planner_input)
        output_paths = _save_output(plan, params.target_month, self._config.client_name)

        # Google Sheets 업로드 (설정된 경우)
        if self._config.google_creds_json and self._config.google_drive_folder_id:
            await _post(slack_client, channel, thread_ts,
                        formatter.progress(":bar_chart:", "Google Sheets에 기록 중..."))
            try:
                from interfaces.google_drive.uploader import upload_to_sheets
                sheets_url = await upload_to_sheets(
                    plan=plan,
                    folder_id=self._config.google_drive_folder_id,
                    creds_json_path=self._config.google_creds_json,
                )
                output_paths["sheets"] = sheets_url
                logger.info("Google Sheets 업로드 완료: %s", sheets_url)
            except Exception as exc:
                logger.warning("Google Sheets 업로드 실패: %s", exc)
                output_paths["sheets_error"] = str(exc)

        await _post(slack_client, channel, thread_ts,
                    formatter.progress(
                        ":white_check_mark:",
                        f"플래너 완료 — 콘텐츠 {len(plan.content_pieces)}건 기획",
                    ))
        return plan, output_paths


# ── 모듈 레벨 헬퍼 ────────────────────────────────────────────────


async def _post(slack_client, channel: str, thread_ts: str, text: str) -> None:
    await slack_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=text,
    )


def _load_published(path: str) -> list[PublishedContent]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return [PublishedContent.model_validate(p) for p in raw]
    except FileNotFoundError:
        logger.warning("기발행 DB 없음: %s — 빈 리스트로 진행", path)
        return []


def _save_output(plan: ContentPlan, target_month: str, client_name: str) -> dict[str, str]:
    out_dir = Path("output/planner")
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{target_month}_{client_name}_plan.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(plan.model_dump(), f, ensure_ascii=False, indent=2)

    paths: dict[str, str] = {"json": str(json_path)}
    if plan.planning_document:
        md_path = out_dir / f"{target_month}_{client_name}_plan.md"
        md_path.write_text(plan.planning_document, encoding="utf-8")
        paths["md"] = str(md_path)

    return paths
