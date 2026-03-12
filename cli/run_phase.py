"""Phase별 CLI 러너 — Slack 없이 개별 Phase를 실행하고 스냅샷 저장.

Usage:
  python cli/run_phase.py init "입력텍스트"     # Phase 0: 입력→세션→스냅샷
  python cli/run_phase.py researcher 0          # Phase 1: 질문[0] seed+fanout
  python cli/run_phase.py designer 0            # Phase 2: 질문[0] 설계
  python cli/run_phase.py gate                  # Phase 3: 완료 게이트
  python cli/run_phase.py planner               # Phase 4: 플래닝+대시보드
  python cli/run_phase.py status                # 현재 런 상태
  python cli/run_phase.py list                  # 전체 런 목록
  python cli/run_phase.py snapshot <name>       # 특정 스냅샷 JSON 출력
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# Project root
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# content_strategist import
_AGENTS_DIR = str(Path(_PROJECT_ROOT) / ".claude" / "agents")
if _AGENTS_DIR not in sys.path:
    sys.path.insert(0, _AGENTS_DIR)

from interfaces.slack.parser import PipelineParams, ParseError, parse, parse_json
from content_strategist.snapshot import SnapshotManager
from content_strategist.state import PipelineSession
from content_strategist import validator
from content_strategist.orchestrator import AGENT_TIMEOUT, AGENT_BUDGET, DEFAULT_TIMEOUT, DEFAULT_BUDGET


SNAPSHOTS_ROOT = "snapshots"


# ── run_id 생성 ─────────────────────────────────────────────────

def _generate_run_id() -> str:
    now = datetime.now()
    return now.strftime("%Y%m%d_%H%M%S")


# ── 세션 persist / restore ───────────────────────────────────────

def _session_from_manifest(manifest: dict) -> PipelineSession:
    """manifest dict → PipelineSession 복원."""
    return PipelineSession(
        run_id=manifest["run_id"],
        intent=manifest.get("intent", ""),
        content_direction=manifest.get("content_direction", ""),
        target_month=manifest.get("target_month", ""),
        questions=manifest.get("questions", []),
        question_tags=manifest.get("question_tags", []),
        researcher_outputs=manifest.get("researcher_outputs", []),
        designer_outputs=manifest.get("designer_outputs", []),
        schedule_output=manifest.get("schedule_output", ""),
        dashboard_path=manifest.get("dashboard_path", ""),
        dashboard_url=manifest.get("dashboard_url", ""),
        processing=manifest.get("processing", False),
        current_phase=manifest.get("current_phase", "input"),
        feedback_pending={int(k): v for k, v in manifest.get("feedback_pending", {}).items()},
        created_at=manifest.get("created_at", datetime.now().isoformat()),
    )


def _get_active_mgr_and_session() -> tuple[SnapshotManager, PipelineSession]:
    """활성 런의 SnapshotManager + PipelineSession 복원."""
    run_id = SnapshotManager.get_active_run_id(SNAPSHOTS_ROOT)
    if not run_id:
        print("ERROR: 활성 런이 없습니다. 'init'으로 먼저 시작하세요.", file=sys.stderr)
        sys.exit(1)

    mgr = SnapshotManager(run_id, SNAPSHOTS_ROOT)
    manifest = mgr.load_manifest()
    if not manifest:
        print(f"ERROR: manifest.json을 찾을 수 없습니다: {run_id}", file=sys.stderr)
        sys.exit(1)

    session = _session_from_manifest(manifest)
    return mgr, session


# ── claude agent 호출 ────────────────────────────────────────────

async def _call_claude_agent(agent_name: str, input_text: str) -> str:
    """claude CLI로 서브에이전트 호출. 에이전트별 타임아웃/예산 적용."""
    claude_bin = shutil.which("claude") or "/opt/homebrew/bin/claude"
    timeout = AGENT_TIMEOUT.get(agent_name, DEFAULT_TIMEOUT)
    budget = AGENT_BUDGET.get(agent_name, DEFAULT_BUDGET)

    print(f"  에이전트 호출: {agent_name} (timeout={timeout}s, budget=${budget})")

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
        cwd=_PROJECT_ROOT,
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


# ── Phase 0: init ────────────────────────────────────────────────

def cmd_init(input_text: str) -> None:
    """입력 파싱 → 세션 생성 → phase0_input.json + manifest.json.

    입력이 JSON이면 parse_json(), 아니면 텍스트 parse() 사용.
    """
    # JSON 시도
    try:
        json_data = json.loads(input_text)
        if isinstance(json_data, dict):
            result = parse_json(json_data)
        else:
            result = parse(input_text)
    except (json.JSONDecodeError, ValueError):
        result = parse(input_text)

    if isinstance(result, ParseError):
        print(f"ERROR: {result.message}", file=sys.stderr)
        sys.exit(1)

    params: PipelineParams = result
    run_id = _generate_run_id()
    mgr = SnapshotManager(run_id, SNAPSHOTS_ROOT)

    session = PipelineSession(
        run_id=run_id,
        intent=params.intent,
        content_direction=params.content_direction,
        target_month=params.target_month,
        questions=params.questions,
        question_tags=params.question_tags,
    )
    session.current_phase = "input"

    # phase0_input snapshot
    mgr.capture("phase0_input", {
        "phase": "input",
        "step": 0,
        "input": {
            "intent": params.intent,
            "content_direction": params.content_direction,
            "target_month": params.target_month,
            "questions": params.questions,
            "question_tags": params.question_tags,
        },
        "output": {
            "question_count": len(params.questions),
            "pipeline_mode": params.pipeline_mode,
        },
        "validation": {
            "passed": True,
            "checks": ["parse: OK", f"questions: {len(params.questions)}"],
            "errors": [],
        },
    })

    # manifest
    mgr.save_manifest(session)

    # set active
    SnapshotManager.set_active_run_id(run_id, SNAPSHOTS_ROOT)

    print(run_id)


# ── Phase 1: researcher ─────────────────────────────────────────

def cmd_researcher(question_index: int) -> None:
    """질문[N] researcher 실행 → seed + fanout 스냅샷."""
    mgr, session = _get_active_mgr_and_session()

    if question_index < 0 or question_index >= len(session.questions):
        print(f"ERROR: 질문 인덱스 범위 초과 (0~{len(session.questions)-1})", file=sys.stderr)
        sys.exit(1)

    question = session.questions[question_index]
    step = question_index + 1
    session.current_phase = "research"

    print(f"[Phase 1] 리서치 시작: 질문 {step}/{len(session.questions)}")
    print(f"  질문: {question[:60]}...")

    # researcher 실행
    researcher_dir = Path("output/claude_researcher")
    researcher_dir.mkdir(parents=True, exist_ok=True)
    before = set(researcher_dir.glob("seed_*.json"))

    t0 = time.time()
    asyncio.run(_call_claude_agent("researcher", question))
    duration = time.time() - t0

    after = set(researcher_dir.glob("seed_*.json"))
    new_files = sorted(after - before, key=lambda p: p.stat().st_mtime)
    if not new_files:
        print("ERROR: researcher 출력 파일 미생성", file=sys.stderr)
        sys.exit(1)
    researcher_output = str(new_files[-1])

    # 검증
    r_ok, r_details = validator.verify_researcher(researcher_output)
    print(f"  검증: {'PASS' if r_ok else 'FAIL'}")
    for d in r_details:
        print(f"    - {d}")

    if not r_ok:
        # 1회 재시도
        print("  재시도 중...")
        before2 = set(researcher_dir.glob("seed_*.json"))
        t1 = time.time()
        asyncio.run(_call_claude_agent("researcher", question))
        duration += time.time() - t1
        after2 = set(researcher_dir.glob("seed_*.json"))
        new_files2 = sorted(after2 - before2, key=lambda p: p.stat().st_mtime)
        if not new_files2:
            print("ERROR: researcher 재시도 출력 파일 미생성", file=sys.stderr)
            sys.exit(1)
        researcher_output = str(new_files2[-1])
        r_ok, r_details = validator.verify_researcher(researcher_output)
        if not r_ok:
            print(f"ERROR: researcher 재시도 검증 실패: {', '.join(r_details)}", file=sys.stderr)
            sys.exit(1)

    # seed keyword 추출
    try:
        with open(researcher_output, encoding="utf-8") as f:
            rdata = json.load(f)
        seed_keyword = rdata.get("seed", {}).get("keyword", "")
    except Exception:
        seed_keyword = ""

    # 스냅샷: seed + fanout
    mgr.capture_seed(step, question, researcher_output, (r_ok, r_details), duration)
    mgr.capture_fanout(step, seed_keyword, researcher_output, (r_ok, r_details), duration)

    # 세션 업데이트
    while len(session.researcher_outputs) <= question_index:
        session.researcher_outputs.append("")
    session.researcher_outputs[question_index] = researcher_output

    mgr.save_manifest(session)

    print(f"  완료: {researcher_output} ({duration:.1f}s)")
    print(f"  스냅샷: q{step:03d}_seed.json, q{step:03d}_fanout.json, q{step:03d}_researcher_full.json")


# ── Phase 2: designer ───────────────────────────────────────────

def cmd_designer(question_index: int) -> None:
    """질문[N] designer 실행 → designer 스냅샷."""
    mgr, session = _get_active_mgr_and_session()

    if question_index < 0 or question_index >= len(session.questions):
        print(f"ERROR: 질문 인덱스 범위 초과 (0~{len(session.questions)-1})", file=sys.stderr)
        sys.exit(1)

    step = question_index + 1

    # researcher 결과 확인
    if question_index >= len(session.researcher_outputs) or not session.researcher_outputs[question_index]:
        print(f"ERROR: 질문 {step}의 researcher 결과가 없습니다. 먼저 'researcher {question_index}'를 실행하세요.", file=sys.stderr)
        sys.exit(1)

    researcher_output = session.researcher_outputs[question_index]
    session.current_phase = "design"

    print(f"[Phase 2] 콘텐츠 설계 시작: 질문 {step}/{len(session.questions)}")

    # 포맷 호환성 검증
    compat_ok, compat_details = validator.check_format_compat(researcher_output, "designer")
    if not compat_ok:
        print(f"ERROR: 포맷 호환 실패: {', '.join(compat_details)}", file=sys.stderr)
        sys.exit(1)

    # designer 실행
    designer_dir = Path("output/claude_content_designer")
    designer_dir.mkdir(parents=True, exist_ok=True)
    before_d = set(designer_dir.glob("plan_*.json"))

    designer_input = f"리서처 결과: {researcher_output}"
    t0 = time.time()
    asyncio.run(_call_claude_agent("content-designer", designer_input))
    duration = time.time() - t0

    after_d = set(designer_dir.glob("plan_*.json"))
    new_plans = sorted(after_d - before_d, key=lambda p: p.stat().st_mtime)
    if not new_plans:
        print("ERROR: content-designer 출력 파일 미생성", file=sys.stderr)
        sys.exit(1)
    designer_output = str(new_plans[-1])

    # 검증
    d_ok, d_details = validator.verify_designer(designer_output)
    print(f"  검증: {'PASS' if d_ok else 'FAIL'}")
    for d in d_details:
        print(f"    - {d}")

    if not d_ok:
        print(f"ERROR: designer 검증 실패: {', '.join(d_details)}", file=sys.stderr)
        sys.exit(1)

    # 스냅샷
    mgr.capture_designer(step, researcher_output, designer_output, (d_ok, d_details), duration)

    # 세션 업데이트
    while len(session.designer_outputs) <= question_index:
        session.designer_outputs.append("")
    session.designer_outputs[question_index] = designer_output

    mgr.save_manifest(session)

    print(f"  완료: {designer_output} ({duration:.1f}s)")
    print(f"  스냅샷: q{step:03d}_designer.json, q{step:03d}_designer_full.json")


# ── Phase 3: gate ────────────────────────────────────────────────

def cmd_gate() -> None:
    """완료 게이트 — 성공 쌍 >= 2 확인."""
    mgr, session = _get_active_mgr_and_session()

    total = len(session.questions)
    success_count = len([d for d in session.designer_outputs if d])
    passed = success_count >= 2

    session.current_phase = "gate"
    mgr.capture_gate(success_count, total, passed)
    mgr.save_manifest(session)

    status = "PASS" if passed else "FAIL"
    print(f"[Phase 3] 완료 게이트: {status}")
    print(f"  성공: {success_count}/{total} (최소 2개 필요)")

    if not passed:
        sys.exit(1)


# ── Phase 4: planner ─────────────────────────────────────────────

def cmd_planner() -> None:
    """플래닝 + 대시보드 생성."""
    mgr, session = _get_active_mgr_and_session()

    # 게이트 확인
    gate = mgr.load("phase3_gate")
    if not gate or not gate.get("output", {}).get("passed"):
        print("ERROR: Phase 3 게이트를 먼저 통과해야 합니다.", file=sys.stderr)
        sys.exit(1)

    session.current_phase = "planning"
    print("[Phase 4] 플래닝 + 대시보드 생성 시작")

    # 포맷 호환성 검증
    for dp in session.designer_outputs:
        if not dp:
            continue
        compat_ok, compat_details = validator.check_format_compat(dp, "planner")
        if not compat_ok:
            print(f"ERROR: planner 포맷 호환 실패: {', '.join(compat_details)}", file=sys.stderr)
            sys.exit(1)

    plan_list = "\n".join(f"- {p}" for p in session.designer_outputs if p)
    planner_input = (
        f"대상 월: {session.target_month}\n"
        f"콘텐츠 기획:\n{plan_list}"
    )

    t0 = time.time()
    asyncio.run(_call_claude_agent("content-planner", planner_input))
    duration = time.time() - t0

    # 최신 schedule 파일
    sched_dir = Path("output/claude_content_scheduler")
    schedules = sorted(
        sched_dir.glob("schedule_*.json"),
        key=lambda p: p.stat().st_mtime,
    ) if sched_dir.exists() else []
    schedule_path = str(schedules[-1]) if schedules else ""
    session.schedule_output = schedule_path

    # schedule 검증
    s_ok, s_details = (True, []) if not schedule_path else validator.verify_schedule(schedule_path)
    print(f"  Schedule 검증: {'PASS' if s_ok else 'FAIL'}")

    # 최신 dashboard
    docs_dir = Path("docs")
    dashboards = sorted(
        docs_dir.glob(f"{session.target_month}_*.html"),
        key=lambda p: p.stat().st_mtime,
    ) if docs_dir.exists() else []
    dashboard_path = str(dashboards[-1]) if dashboards else ""
    session.dashboard_path = dashboard_path

    # 스냅샷
    mgr.capture_planner(schedule_path, dashboard_path, (s_ok, s_details), duration)
    mgr.save_manifest(session)

    print(f"  완료: schedule={schedule_path}, dashboard={dashboard_path} ({duration:.1f}s)")
    print(f"  스냅샷: phase4_planner.json, phase4_schedule_full.json")


# ── status ───────────────────────────────────────────────────────

def cmd_status() -> None:
    """현재 런 상태 출력."""
    run_id = SnapshotManager.get_active_run_id(SNAPSHOTS_ROOT)
    if not run_id:
        print("활성 런이 없습니다.")
        return

    mgr = SnapshotManager(run_id, SNAPSHOTS_ROOT)
    manifest = mgr.load_manifest()
    if not manifest:
        print(f"manifest.json을 찾을 수 없습니다: {run_id}")
        return

    print(f"Run ID: {manifest['run_id']}")
    print(f"Phase:  {manifest.get('current_phase', 'unknown')}")
    print(f"생성:   {manifest.get('created_at', '')}")
    print(f"갱신:   {manifest.get('updated_at', '')}")
    print(f"의도:   {manifest.get('intent', '')}")
    print(f"방향성: {manifest.get('content_direction', '')}")
    print(f"대상 월: {manifest.get('target_month', '')}")
    print()

    questions = manifest.get("questions", [])
    r_outputs = manifest.get("researcher_outputs", [])
    d_outputs = manifest.get("designer_outputs", [])

    print(f"질문 ({len(questions)}개):")
    for i, q in enumerate(questions):
        r_status = "OK" if i < len(r_outputs) and r_outputs[i] else "--"
        d_status = "OK" if i < len(d_outputs) and d_outputs[i] else "--"
        print(f"  [{i}] {q[:50]}...")
        print(f"      researcher: {r_status}  designer: {d_status}")
    print()

    snapshots = manifest.get("snapshots", [])
    print(f"스냅샷 ({len(snapshots)}개):")
    for s in snapshots:
        print(f"  - {s}")


# ── list ─────────────────────────────────────────────────────────

def cmd_list() -> None:
    """전체 런 목록 출력."""
    runs = SnapshotManager.list_runs(SNAPSHOTS_ROOT)
    if not runs:
        print("런 없음.")
        return

    active = SnapshotManager.get_active_run_id(SNAPSHOTS_ROOT)
    for r in runs:
        marker = " *" if r["run_id"] == active else ""
        print(f"  {r['run_id']}{marker}  phase={r['current_phase']}  "
              f"questions={r['question_count']}  snapshots={r['snapshots']}")


# ── snapshot ─────────────────────────────────────────────────────

def cmd_snapshot(name: str) -> None:
    """특정 스냅샷 JSON 출력."""
    mgr, _ = _get_active_mgr_and_session()
    data = mgr.load(name)
    if not data:
        print(f"ERROR: 스냅샷을 찾을 수 없습니다: {name}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ── main ─────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "init":
        if len(sys.argv) < 3:
            print("Usage: python cli/run_phase.py init \"입력텍스트\"", file=sys.stderr)
            sys.exit(1)
        cmd_init(sys.argv[2])

    elif cmd == "researcher":
        if len(sys.argv) < 3:
            print("Usage: python cli/run_phase.py researcher <question_index>", file=sys.stderr)
            sys.exit(1)
        cmd_researcher(int(sys.argv[2]))

    elif cmd == "designer":
        if len(sys.argv) < 3:
            print("Usage: python cli/run_phase.py designer <question_index>", file=sys.stderr)
            sys.exit(1)
        cmd_designer(int(sys.argv[2]))

    elif cmd == "gate":
        cmd_gate()

    elif cmd == "planner":
        cmd_planner()

    elif cmd == "status":
        cmd_status()

    elif cmd == "list":
        cmd_list()

    elif cmd == "snapshot":
        if len(sys.argv) < 3:
            print("Usage: python cli/run_phase.py snapshot <name>", file=sys.stderr)
            sys.exit(1)
        cmd_snapshot(sys.argv[2])

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    # 백그라운드 실행 시에도 즉시 출력
    import functools
    print = functools.partial(print, flush=True)  # type: ignore[assignment]
    main()
