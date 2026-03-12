"""콘텐츠 전략가 오케스트레이터 — 파이프라인 실행 로직."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from . import feedback, formatter, state, validator
from .snapshot import SnapshotManager
from .state import PipelineSession

logger = logging.getLogger(__name__)

# ── 에이전트별 하드 리밋 ──────────────────────────────────────────
# 로그 기반 추정: researcher 10-15분, designer 5-8분, planner 3-5분
# 타임아웃은 정상 범위의 ~2배로 설정

AGENT_TIMEOUT: dict[str, int] = {
    "researcher": 1800,       # 30분 (실측 7-20분, 어셈블리 포함)
    "content-designer": 600,  # 10분 (정상 5-8분)
    "content-planner": 300,   #  5분 (정상 3-5분)
}

AGENT_BUDGET: dict[str, str] = {
    "researcher": "10",
    "content-designer": "8",
    "content-planner": "5",
}

DEFAULT_TIMEOUT = 1200   # 알 수 없는 에이전트용 기본값
DEFAULT_BUDGET = "10"


class ContentStrategist:
    """콘텐츠 전략가 오케스트레이터."""

    def __init__(self, config, snapshot_mgr: SnapshotManager | None = None) -> None:
        self._config = config
        self.snapshot_mgr = snapshot_mgr

    # ── Phase 1+2: 질문별 리서치 + 설계 ──────────────────────────

    async def run_all_questions(
        self,
        session: PipelineSession,
        slack_client,
        channel: str,
        thread_ts: str,
    ) -> None:
        """세션에 수집된 질문 전체를 순차 실행 (researcher → designer)."""
        session.processing = True
        session.current_phase = "research"
        total = len(session.questions)

        # Phase 0: 액션 플랜
        await slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=formatter.action_plan_blocks(
                session.questions, session.intent,
                session.content_direction, session.target_month,
            ),
            text=f"액션 플랜: 질문 {total}개 처리",
        )

        await _post(slack_client, channel, thread_ts,
                    formatter.progress(":rocket:", f"질문 {total}개 순차 리서치를 시작합니다."))

        if self.snapshot_mgr:
            self.snapshot_mgr.save_manifest(session)

        try:
            for i, question in enumerate(session.questions, 1):
                await self._run_single_question(
                    session, i, total, question, slack_client, channel, thread_ts,
                )
        finally:
            session.processing = False

        # Phase 3: 완료 게이트 — 성공 쌍 2개 이상 확인
        success_count = len(session.designer_outputs)

        if self.snapshot_mgr:
            self.snapshot_mgr.capture_gate(success_count, total, success_count >= 2)
            self.snapshot_mgr.save_manifest(session)

        if success_count < 2:
            await _post(slack_client, channel, thread_ts,
                        formatter.progress(
                            ":x:",
                            f"완료 게이트 미달: 성공 {success_count}/{total}개 (최소 2개 필요). 파이프라인 중단.",
                        ))
            return

        # 스케줄링 버튼
        finish_btn: dict = {
            "type": "button",
            "action_id": "pipeline_finish",
            "text": {"type": "plain_text", "text": "스케줄링 시작"},
            "style": "primary",
        }
        await slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":white_check_mark: *질문 {success_count}/{total}개 처리 완료*",
                    },
                },
                {"type": "actions", "elements": [finish_btn]},
            ],
            text=f"질문 처리 완료 — 스케줄링을 시작하세요.",
        )

    async def _run_single_question(
        self,
        session: PipelineSession,
        n: int,
        total: int,
        question: str,
        slack_client,
        channel: str,
        thread_ts: str,
    ) -> None:
        """질문 1개 → researcher + 검증 + designer + 검증."""
        try:
            await _post(slack_client, channel, thread_ts,
                        formatter.progress(":mag:", f"[{n}/{total}] 리서치 중... _{question[:40]}_"))

            # ── Researcher 실행 ──
            researcher_dir = Path("output/claude_researcher")
            researcher_dir.mkdir(parents=True, exist_ok=True)
            before = set(researcher_dir.glob("seed_*.json"))

            _t0 = time.time()
            await self._call_claude_agent("researcher", question)
            _researcher_duration = time.time() - _t0

            after = set(researcher_dir.glob("seed_*.json"))
            new_files = sorted(after - before, key=lambda p: p.stat().st_mtime)
            if not new_files:
                raise RuntimeError("researcher 출력 파일 미생성")
            researcher_output = str(new_files[-1])

            # ── Researcher 검증 게이트 ──
            r_ok, r_details = validator.verify_researcher(researcher_output)
            await slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=formatter.verification_blocks("Researcher", r_ok, r_details),
                text=f"Researcher 검증 {'통과' if r_ok else '실패'}",
            )

            if not r_ok:
                # 1회 재시도
                await _post(slack_client, channel, thread_ts,
                            formatter.progress(":arrows_counterclockwise:", f"[{n}/{total}] 리서치 재시도..."))
                before2 = set(researcher_dir.glob("seed_*.json"))
                await self._call_claude_agent("researcher", question)
                after2 = set(researcher_dir.glob("seed_*.json"))
                new_files2 = sorted(after2 - before2, key=lambda p: p.stat().st_mtime)
                if not new_files2:
                    raise RuntimeError("researcher 재시도 출력 파일 미생성")
                researcher_output = str(new_files2[-1])
                r_ok2, r_details2 = validator.verify_researcher(researcher_output)
                if not r_ok2:
                    raise RuntimeError(f"researcher 재시도 검증 실패: {', '.join(r_details2)}")

            session.researcher_outputs.append(researcher_output)

            # ── Researcher 스냅샷 ──
            if self.snapshot_mgr:
                try:
                    import json as _json
                    with open(researcher_output, encoding="utf-8") as _f:
                        _rdata = _json.load(_f)
                    _seed_kw = _rdata.get("seed", {}).get("keyword", "")
                except Exception:
                    _seed_kw = ""
                self.snapshot_mgr.capture_seed(n, question, researcher_output, (r_ok, r_details), _researcher_duration)
                self.snapshot_mgr.capture_fanout(n, _seed_kw, researcher_output, (r_ok, r_details), _researcher_duration)
                self.snapshot_mgr.save_manifest(session)

            # ── 포맷 호환성 검증 (researcher → designer) ──
            compat_ok, compat_details = validator.check_format_compat(researcher_output, "designer")
            if not compat_ok:
                await slack_client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    blocks=formatter.verification_blocks("포맷 호환", compat_ok, compat_details),
                    text="포맷 호환 검증 실패",
                )
                raise RuntimeError(f"포맷 호환 실패: {', '.join(compat_details)}")

            # ── Content Designer 실행 ──
            session.current_phase = "design"
            await _post(slack_client, channel, thread_ts,
                        formatter.progress(":art:", f"[{n}/{total}] 콘텐츠 설계 중..."))

            designer_dir = Path("output/claude_content_designer")
            designer_dir.mkdir(parents=True, exist_ok=True)
            before_d = set(designer_dir.glob("plan_*.json"))
            designer_input = f"리서처 결과: {researcher_output}"
            _t1 = time.time()
            await self._call_claude_agent("content-designer", designer_input)
            _designer_duration = time.time() - _t1
            after_d = set(designer_dir.glob("plan_*.json"))
            new_plans = sorted(after_d - before_d, key=lambda p: p.stat().st_mtime)
            if not new_plans:
                raise RuntimeError("content-designer 출력 파일 미생성")
            designer_output = str(new_plans[-1])

            # ── Designer 검증 게이트 ──
            d_ok, d_details = validator.verify_designer(designer_output)
            await slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=formatter.verification_blocks("Designer", d_ok, d_details),
                text=f"Designer 검증 {'통과' if d_ok else '실패'}",
            )
            if not d_ok:
                raise RuntimeError(f"designer 검증 실패: {', '.join(d_details)}")

            session.designer_outputs.append(designer_output)

            # ── Designer 스냅샷 ──
            if self.snapshot_mgr:
                self.snapshot_mgr.capture_designer(n, researcher_output, designer_output, (d_ok, d_details), _designer_duration)
                self.snapshot_mgr.save_manifest(session)

            await _post(slack_client, channel, thread_ts,
                        formatter.progress(":white_check_mark:", f"[{n}/{total}] 완료"))

        except Exception as exc:
            logger.exception("질문 %d 처리 실패", n)
            await _post(slack_client, channel, thread_ts,
                        formatter.progress(":x:", f"[{n}/{total}] 처리 실패: `{exc}`"))

    # ── Phase 4: 플래닝 ──────────────────────────────────────────

    async def run_planner(
        self,
        session: PipelineSession,
        slack_client,
        channel: str,
        thread_ts: str,
    ) -> None:
        """content-planner 호출 + 검증 + 대시보드 배포 + 피드백 카드 게시."""
        session.current_phase = "planning"
        await _post(slack_client, channel, thread_ts,
                    formatter.progress(":calendar:", "플래닝 + 대시보드 생성 중..."))

        try:
            # 포맷 호환성 검증 (designer → planner)
            for dp in session.designer_outputs:
                compat_ok, compat_details = validator.check_format_compat(dp, "planner")
                if not compat_ok:
                    await slack_client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        blocks=formatter.verification_blocks("포맷 호환 (planner)", compat_ok, compat_details),
                        text="포맷 호환 검증 실패",
                    )
                    raise RuntimeError(f"planner 포맷 호환 실패: {', '.join(compat_details)}")

            plan_list = "\n".join(f"- {p}" for p in session.designer_outputs)
            planner_input = (
                f"대상 월: {session.target_month}\n"
                f"콘텐츠 기획:\n{plan_list}"
            )
            _tp = time.time()
            await self._call_claude_agent("content-planner", planner_input)
            _planner_duration = time.time() - _tp

            # 최신 schedule 파일 찾기
            sched_dir = Path("output/claude_content_scheduler")
            schedules = sorted(
                sched_dir.glob("schedule_*.json"),
                key=lambda p: p.stat().st_mtime,
            ) if sched_dir.exists() else []
            schedule_path = str(schedules[-1]) if schedules else ""
            session.schedule_output = schedule_path

            # Schedule 검증 게이트
            s_ok, s_details = True, []
            if schedule_path:
                s_ok, s_details = validator.verify_schedule(schedule_path)
                await slack_client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    blocks=formatter.verification_blocks("Schedule", s_ok, s_details),
                    text=f"Schedule 검증 {'통과' if s_ok else '실패'}",
                )

            # 최신 dashboard 파일 찾기
            docs_dir = Path("docs")
            dashboards = sorted(
                docs_dir.glob(f"{session.target_month}_*.html"),
                key=lambda p: p.stat().st_mtime,
            ) if docs_dir.exists() else []
            dashboard_path = str(dashboards[-1]) if dashboards else ""
            session.dashboard_path = dashboard_path

            # ── Planner 스냅샷 ──
            if self.snapshot_mgr:
                self.snapshot_mgr.capture_planner(schedule_path, dashboard_path, (s_ok, s_details), _planner_duration)
                self.snapshot_mgr.save_manifest(session)

            # 대시보드 GitHub Pages push
            dashboard_url = ""
            if dashboard_path:
                await _post(slack_client, channel, thread_ts,
                            formatter.progress(":rocket:", "대시보드 GitHub Pages 배포 중..."))
                pushed = await _push_dashboard(
                    dashboard_path, session.target_month, self._config.client_name,
                )
                if pushed:
                    pages_base = await _get_pages_url()
                    if pages_base:
                        filename = Path(dashboard_path).name
                        dashboard_url = f"{pages_base}{filename}"
                        session.dashboard_url = dashboard_url

            # Phase 5: 완료 안내
            await slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=formatter.pipeline_summary_blocks(
                    session.target_month, schedule_path, dashboard_path,
                    session.run_id, dashboard_url=dashboard_url,
                ),
                text=f"풀 파이프라인 완료 — {session.target_month}",
            )

            # 피드백 카드 게시
            await self._post_feedback_cards(session, slack_client, channel, thread_ts)

        except Exception as exc:
            logger.exception("플래너 실패")
            await _post(slack_client, channel, thread_ts,
                        formatter.progress(":x:", f"플래너 실패: `{exc}`"))

        # 세션은 삭제하지 않음 (피드백 대기)

    # ── Phase 6: 피드백 재실행 ───────────────────────────────────

    async def run_feedback_rerun(
        self,
        session: PipelineSession,
        slack_client,
        channel: str,
        thread_ts: str,
        question_index: int,
        feedback_type: str,
        new_input: str = "",
    ) -> None:
        """부분 재실행: seed_change → researcher부터, meta_change → designer부터."""
        session.processing = True
        try:
            if feedback_type == "seed_change":
                await _post(slack_client, channel, thread_ts,
                            formatter.progress(":arrows_counterclockwise:",
                                              f"시드 변경 재실행 (질문 {question_index + 1})"))

                # researcher 재호출
                question = new_input or session.questions[question_index]
                researcher_dir = Path("output/claude_researcher")
                before = set(researcher_dir.glob("seed_*.json"))
                await self._call_claude_agent("researcher", question)
                after = set(researcher_dir.glob("seed_*.json"))
                new_files = sorted(after - before, key=lambda p: p.stat().st_mtime)
                if not new_files:
                    raise RuntimeError("researcher 재실행 출력 파일 미생성")
                researcher_output = str(new_files[-1])

                r_ok, r_details = validator.verify_researcher(researcher_output)
                await slack_client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    blocks=formatter.verification_blocks("Researcher 재실행", r_ok, r_details),
                    text=f"Researcher 재실행 검증 {'통과' if r_ok else '실패'}",
                )
                if not r_ok:
                    raise RuntimeError(f"researcher 재실행 검증 실패: {', '.join(r_details)}")

                # 인덱스 업데이트
                if question_index < len(session.researcher_outputs):
                    session.researcher_outputs[question_index] = researcher_output
                else:
                    session.researcher_outputs.append(researcher_output)

                # designer 재호출
                designer_dir = Path("output/claude_content_designer")
                before_d = set(designer_dir.glob("plan_*.json"))
                await self._call_claude_agent("content-designer", f"리서처 결과: {researcher_output}")
                after_d = set(designer_dir.glob("plan_*.json"))
                new_plans = sorted(after_d - before_d, key=lambda p: p.stat().st_mtime)
                if not new_plans:
                    raise RuntimeError("content-designer 재실행 출력 파일 미생성")
                designer_output = str(new_plans[-1])

                if question_index < len(session.designer_outputs):
                    session.designer_outputs[question_index] = designer_output
                else:
                    session.designer_outputs.append(designer_output)

            elif feedback_type == "meta_change":
                await _post(slack_client, channel, thread_ts,
                            formatter.progress(":arrows_counterclockwise:",
                                              f"메타 변경 재실행 (질문 {question_index + 1})"))

                # 기존 researcher 결과 유지, designer만 재호출
                researcher_output = session.researcher_outputs[question_index]
                designer_dir = Path("output/claude_content_designer")
                before_d = set(designer_dir.glob("plan_*.json"))
                designer_input = f"리서처 결과: {researcher_output}\n변경 지시: {new_input}"
                await self._call_claude_agent("content-designer", designer_input)
                after_d = set(designer_dir.glob("plan_*.json"))
                new_plans = sorted(after_d - before_d, key=lambda p: p.stat().st_mtime)
                if not new_plans:
                    raise RuntimeError("content-designer 재실행 출력 파일 미생성")
                designer_output = str(new_plans[-1])
                session.designer_outputs[question_index] = designer_output

            # planner 재실행 (전체 designer_outputs)
            await _post(slack_client, channel, thread_ts,
                        formatter.progress(":calendar:", "플래닝 재실행 중..."))

            plan_list = "\n".join(f"- {p}" for p in session.designer_outputs)
            planner_input = (
                f"대상 월: {session.target_month}\n"
                f"콘텐츠 기획:\n{plan_list}"
            )
            await self._call_claude_agent("content-planner", planner_input)

            # 대시보드 재배포
            docs_dir = Path("docs")
            dashboards = sorted(
                docs_dir.glob(f"{session.target_month}_*.html"),
                key=lambda p: p.stat().st_mtime,
            ) if docs_dir.exists() else []
            if dashboards:
                dashboard_path = str(dashboards[-1])
                session.dashboard_path = dashboard_path
                pushed = await _push_dashboard(
                    dashboard_path, session.target_month, self._config.client_name,
                )
                if pushed:
                    pages_base = await _get_pages_url()
                    if pages_base:
                        session.dashboard_url = f"{pages_base}{Path(dashboard_path).name}"

            await _post(slack_client, channel, thread_ts,
                        formatter.progress(":white_check_mark:", "피드백 반영 완료"))

            # feedback_pending에서 제거
            session.feedback_pending.pop(question_index, None)

        except Exception as exc:
            logger.exception("피드백 재실행 실패")
            await _post(slack_client, channel, thread_ts,
                        formatter.progress(":x:", f"피드백 재실행 실패: `{exc}`"))
        finally:
            session.processing = False

    async def collect_and_process_github_feedback(
        self,
        session: PipelineSession,
        slack_client,
        channel: str,
        thread_ts: str,
    ) -> None:
        """GitHub Issues에서 현재 run_id 피드백 수집 → 순차 처리."""
        await _post(slack_client, channel, thread_ts,
                    formatter.progress(":github:", "GitHub Issues 피드백 수집 중..."))

        issues = await feedback.collect_github_issues(session.run_id)
        if not issues:
            await _post(slack_client, channel, thread_ts,
                        formatter.progress(":information_source:", "처리할 GitHub 피드백이 없습니다."))
            return

        await _post(slack_client, channel, thread_ts,
                    formatter.progress(":clipboard:", f"GitHub 피드백 {len(issues)}건 발견. 순차 처리 시작."))

        changed_items: list[dict] = []
        for issue in issues:
            idx = issue["question_index"]
            ft = issue["feedback_type"]
            if idx < 0 or ft == "other":
                await feedback.close_github_issue(
                    issue["number"], "자동 처리 불가 — 수동 확인 필요",
                )
                continue

            await self.run_feedback_rerun(
                session, slack_client, channel, thread_ts,
                question_index=idx,
                feedback_type=ft,
                new_input=issue.get("body", ""),
            )

            await feedback.close_github_issue(
                issue["number"],
                f"피드백 자동 처리 완료 (Run: {session.run_id})",
            )
            changed_items.append({
                "keyword": issue["title"],
                "change_type": ft,
            })

        await slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=formatter.feedback_complete_blocks(changed_items),
            text="GitHub 피드백 처리 완료",
        )

    # ── 내부 헬퍼 ────────────────────────────────────────────────

    async def _call_claude_agent(self, agent_name: str, input_text: str) -> str:
        """claude CLI로 서브에이전트 호출. stdout 반환.

        에이전트별 타임아웃/예산 리밋 적용:
          researcher     — 1200s / $10
          content-designer — 600s / $8
          content-planner  — 300s / $5
        """
        import shutil
        claude_bin = shutil.which("claude") or "/opt/homebrew/bin/claude"
        timeout = AGENT_TIMEOUT.get(agent_name, DEFAULT_TIMEOUT)
        budget = AGENT_BUDGET.get(agent_name, DEFAULT_BUDGET)

        logger.info("에이전트 호출: %s (timeout=%ds, budget=$%s)", agent_name, timeout, budget)

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "-p",
            "--agent", agent_name,
            "--dangerously-skip-permissions",
            "--max-budget-usd", budget,
            input_text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(Path.cwd()),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"claude --agent {agent_name} 타임아웃 ({timeout}s 초과). "
                "무한 루프 또는 외부 도구 지연 가능성."
            )
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"claude --agent {agent_name} 실패: {err}")
        return stdout.decode("utf-8", errors="replace")

    async def _post_feedback_cards(
        self,
        session: PipelineSession,
        slack_client,
        channel: str,
        thread_ts: str,
    ) -> None:
        """스케줄의 각 콘텐츠별 피드백 버튼 카드 게시."""
        session.current_phase = "feedback"

        if not session.schedule_output:
            return

        try:
            with open(session.schedule_output, encoding="utf-8") as f:
                schedule_data = json.load(f)
        except Exception:
            return

        schedule_items = schedule_data.get("schedule", [])
        for i, item in enumerate(schedule_items):
            blocks = formatter.feedback_content_card(item, i)
            await slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=blocks,
                text=f"피드백: {item.get('keyword', '')}",
            )

        # GitHub Issues 처리 버튼
        await slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=formatter.feedback_issues_button(session.run_id),
            text="GitHub Issues 피드백 처리",
        )


# ── 모듈 레벨 헬퍼 ────────────────────────────────────────────────


async def _push_dashboard(
    dashboard_path: str, target_month: str, client_name: str,
) -> bool:
    """대시보드 파일을 git commit + push. 성공 시 True, 실패 시 False (경고만)."""
    try:
        db_file = Path(dashboard_path).name
        add_targets = [f"docs/{db_file}", "docs/index.html", "docs/.nojekyll"]
        add_cmd = ["git", "add"] + add_targets

        proc = await asyncio.create_subprocess_exec(
            *add_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        diff_proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--cached", "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await diff_proc.communicate()
        if diff_proc.returncode == 0:
            logger.info("대시보드 변경 사항 없음 — push 건너뜀")
            return False

        commit_msg = f"dashboard: {target_month} {client_name}"
        commit_proc = await asyncio.create_subprocess_exec(
            "git", "commit", "-m", commit_msg,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, commit_err = await commit_proc.communicate()
        if commit_proc.returncode != 0:
            logger.warning("대시보드 commit 실패: %s", commit_err.decode(errors="replace"))
            return False

        push_proc = await asyncio.create_subprocess_exec(
            "git", "push",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, push_err = await push_proc.communicate()
        if push_proc.returncode != 0:
            logger.warning("대시보드 push 실패: %s", push_err.decode(errors="replace"))
            return False

        logger.info("대시보드 push 완료: %s", dashboard_path)
        return True
    except Exception as exc:
        logger.warning("대시보드 push 중 예외: %s", exc)
        return False


async def _get_pages_url() -> str:
    """git remote에서 GitHub Pages base URL 추출. 실패 시 빈 문자열."""
    import re as re_mod
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "remote", "get-url", "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        remote_url = stdout.decode(errors="replace").strip()

        m = re_mod.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote_url)
        if not m:
            return ""
        owner, repo = m.group(1), m.group(2)
        return f"https://{owner}.github.io/{repo}/"
    except Exception:
        return ""


async def _post(slack_client, channel: str, thread_ts: str, text: str) -> None:
    await slack_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=text,
    )
