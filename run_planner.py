"""플래너 에이전트 실행 스크립트.

사용법:
  # 전체 실행
  python run_planner.py --input snapshots/researcher/2026-03-01.json

  # N단계까지만 실행 후 중간 결과 확인
  python run_planner.py --input snapshots/researcher/2026-03-01.json --stage 1

  # 발행 월·클라이언트·기발행 DB 지정
  python run_planner.py --input snapshots/researcher/2026-03-01.json \\
      --month 2026-03 --client wishket --published data/published.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

from core.agents.planner.agent import PlannerAgent
from core.schemas import ContentPlan, PlannerInput, PublishedContent, ResearchResult


# ── 헬퍼 ─────────────────────────────────────────────────────────


def _next_month() -> str:
    """오늘 기준 다음 달을 'YYYY-MM' 형식으로 반환."""
    import calendar as cal_mod

    today = date.today()
    last_day = cal_mod.monthrange(today.year, today.month)[1]
    import datetime
    next_first = date(today.year, today.month, last_day) + datetime.timedelta(days=1)
    return next_first.strftime("%Y-%m")


def _load_research_result(path: str) -> ResearchResult:
    """리서처 스냅샷 JSON → ResearchResult."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return ResearchResult.model_validate(raw)


def _load_published(path: str) -> list[PublishedContent]:
    """기발행 콘텐츠 DB JSON → list[PublishedContent].

    파일 형식: PublishedContent 호환 dict의 JSON 배열.
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return [PublishedContent.model_validate(p) for p in raw]


def _build_planner_input(
    result: ResearchResult,
    published: list[PublishedContent],
    target_month: str,
    client_name: str,
) -> PlannerInput:
    """ResearchResult + CLI 인수로 PlannerInput을 구성한다.

    ResearchResult 필드 매핑:
      intent            → str → list[str]으로 래핑
      source_questions  → list[str] (카테고리 원본 질문)
      content_direction → str → list[str]으로 래핑
    """
    intent = [result.intent] if result.intent else []
    content_direction = [result.content_direction] if result.content_direction else []

    return PlannerInput(
        intent=intent,
        questions=result.source_questions,
        content_direction=content_direction,
        research_result=result,
        published_contents=published,
        target_month=target_month,
        client_name=client_name,
    )


def _print_partial_summary(result: dict, stage: int) -> None:
    """중간 단계 결과 요약을 stderr에 출력한다."""
    print(f"\n=== Stage {stage} 결과 ===", file=sys.stderr)

    if stage == 1:
        questions = result.get("derived_questions", [])
        print(f"파생 질문 수: {len(questions)}개", file=sys.stderr)
        # 카테고리별 분포
        by_cat: dict[str, int] = {}
        for q in questions:
            cat = q.get("category", "(미분류)")
            by_cat[cat] = by_cat.get(cat, 0) + 1
        print("카테고리별 분포:", file=sys.stderr)
        for cat, cnt in by_cat.items():
            print(f"  [{cat[:40]}] {cnt}개", file=sys.stderr)

    elif stage == 2:
        questions = result.get("derived_questions", [])
        print(f"파생 질문 수: {len(questions)}개", file=sys.stderr)
        funnel_counts: dict[str, int] = {}
        for q in questions:
            f = q.get("funnel", "unclassified")
            funnel_counts[f] = funnel_counts.get(f, 0) + 1
        print("퍼널 분포:", file=sys.stderr)
        for f, cnt in funnel_counts.items():
            print(f"  {f}: {cnt}개", file=sys.stderr)

    elif stage == 3:
        questions = result.get("derived_questions", [])
        candidates = result.get("update_candidates", [])
        verdicts: dict[str, int] = {}
        for q in questions:
            dr = q.get("duplicate_result") or {}
            v = dr.get("verdict", "new")
            verdicts[v] = verdicts.get(v, 0) + 1
        print(f"파생 질문 수: {len(questions)}개", file=sys.stderr)
        print("중복 판정 결과:", file=sys.stderr)
        for v, cnt in verdicts.items():
            print(f"  {v}: {cnt}개", file=sys.stderr)
        print(f"업데이트 후보: {len(candidates)}개", file=sys.stderr)

    elif stage == 4:
        selected = result.get("selected", [])
        all_q = result.get("all_questions", [])
        print(f"전체 질문: {len(all_q)}개 → 선발: {len(selected)}개", file=sys.stderr)
        by_cat: dict[str, int] = {}
        for q in selected:
            cat = q.get("category", "(미분류)")
            by_cat[cat] = by_cat.get(cat, 0) + 1
        print("카테고리별 선발:", file=sys.stderr)
        for cat, cnt in by_cat.items():
            print(f"  [{cat[:40]}] {cnt}개", file=sys.stderr)

    elif stage == 5:
        selected = result.get("selected", [])
        dist = result.get("funnel_distribution", {})
        print(f"최종 선발: {len(selected)}개", file=sys.stderr)
        print(f"퍼널 분포: {dist}", file=sys.stderr)

    elif stage == 6:
        pieces = result.get("content_pieces", [])
        print(f"콘텐츠 피스 수: {len(pieces)}개", file=sys.stderr)
        for p in pieces:
            print(
                f"  [{p.get('content_id')}] {p.get('question', '')[:40]}"
                f" | {p.get('funnel')} | {p.get('geo_type')}",
                file=sys.stderr,
            )

    snapshot_hint = f"snapshots/planner/{{오늘날짜}}_stage{stage}.json"
    print(f"스냅샷: {snapshot_hint}", file=sys.stderr)


def _print_final_summary(plan: ContentPlan) -> None:
    """최종 ContentPlan 요약을 stderr에 출력한다."""
    print("\n=== 플래너 완료 ===", file=sys.stderr)
    print(f"대상 월: {plan.target_month}", file=sys.stderr)
    print(f"클라이언트: {plan.client_name}", file=sys.stderr)
    print(f"선발 콘텐츠: {len(plan.content_pieces)}개", file=sys.stderr)
    dist = plan.funnel_distribution
    print(
        f"퍼널 분포: 인지={dist.awareness} / 고려={dist.consideration} / 전환={dist.conversion}",
        file=sys.stderr,
    )
    print(f"발행 일정: {len(plan.calendar)}건", file=sys.stderr)
    print(f"업데이트 후보: {len(plan.update_candidates)}개", file=sys.stderr)


def _save_output(plan: ContentPlan, target_month: str, client_name: str) -> None:
    """ContentPlan을 output/planner/ 에 JSON + Markdown + HTML 대시보드로 저장한다."""
    out_dir = Path("output/planner")
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{target_month}_{client_name}_plan.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(plan.model_dump(), f, ensure_ascii=False, indent=2)
    print(f"결과 저장: {json_path}", file=sys.stderr)

    if plan.planning_document:
        md_path = out_dir / f"{target_month}_{client_name}_plan.md"
        md_path.write_text(plan.planning_document, encoding="utf-8")
        print(f"기획 문서: {md_path}", file=sys.stderr)

    # HTML 대시보드 생성 (docs/ → GitHub Pages 배포용)
    try:
        from core.dashboard import generate as generate_dashboard

        dashboard_path = generate_dashboard(json_path, out_dir="docs")
        print(f"대시보드: {dashboard_path}", file=sys.stderr)
    except Exception as exc:
        print(f"[경고] 대시보드 생성 실패: {exc}", file=sys.stderr)


# ── 메인 ─────────────────────────────────────────────────────────


async def main(args: argparse.Namespace) -> None:
    # --resume: Phase 0에서는 미구현
    if args.resume:
        print(
            "[오류] --resume은 아직 미구현입니다. --input으로 처음부터 실행하세요.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 1. ResearchResult 로드 (start_at_stage > 1이면 스킵 가능)
    if args.input:
        result = _load_research_result(args.input)
    elif args.start_at_stage > 1:
        # 스냅샷 재개 시 ResearchResult 없어도 됨 — 최소 플레이스홀더 사용
        from core.schemas import ResearchResult as _RR
        result = _RR(
            run_date="",
            main_keyword="(재개 — 리서처 결과 없음)",
            entry_moment="",
            intent="",
            source_questions=[],
            content_direction="",
            clusters=[],
            orphan_keywords=[],
        )
    else:
        raise RuntimeError("--input 이 필요합니다.")

    # 2. 기발행 콘텐츠 로드
    published: list[PublishedContent] = []
    if args.published:
        published = _load_published(args.published)

    # 3. 발행 대상 월 결정
    target_month = args.month or _next_month()

    # 4. PlannerInput 구성
    planner_input = _build_planner_input(result, published, target_month, args.client)

    # 5. 에이전트 실행
    agent = PlannerAgent(client_name=args.client)
    run_result = await agent.run(
        planner_input,
        stop_at_stage=args.stage,
        start_at_stage=args.start_at_stage,
        snapshot_dir=args.snapshot_dir,
    )

    # 6. 결과 처리
    if args.stage < 7:
        _print_partial_summary(run_result, args.stage)  # type: ignore[arg-type]
    else:
        plan = run_result  # type: ignore[assignment]
        _save_output(plan, target_month, args.client)
        _print_final_summary(plan)

        # Google Sheets 업로드 (환경변수 설정된 경우)
        creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        if creds_json and folder_id:
            print("Google Sheets 업로드 중...", file=sys.stderr)
            try:
                from interfaces.google_drive.uploader import upload_to_sheets
                sheets_url = await upload_to_sheets(
                    plan=plan,
                    folder_id=folder_id,
                    creds_json_path=creds_json,
                )
                print(f"Google Sheets 업로드 완료: {sheets_url}", file=sys.stderr)
            except Exception as exc:
                print(f"[경고] Google Sheets 업로드 실패: {exc}", file=sys.stderr)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="플래너 에이전트 실행 스크립트",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input",
        help="리서처 스냅샷 JSON 경로 (ResearchResult 형식)",
    )
    parser.add_argument(
        "--stage",
        type=int,
        default=7,
        choices=range(0, 8),
        metavar="N",
        help="N단계까지만 실행 후 중단 (0~7, 기본값: 7)",
    )
    parser.add_argument(
        "--start-at-stage",
        type=int,
        default=1,
        dest="start_at_stage",
        metavar="N",
        help="N단계부터 재개 (기본값: 1, 이전 스냅샷 자동 로드)",
    )
    parser.add_argument(
        "--snapshot-dir",
        default="snapshots/planner",
        dest="snapshot_dir",
        help="스냅샷 디렉토리 (기본값: snapshots/planner)",
    )
    parser.add_argument(
        "--resume",
        help="[미구현] 플래너 중간 스냅샷 경로 — Phase 5 이후 지원 예정",
    )
    parser.add_argument(
        "--month",
        help="발행 대상 월 YYYY-MM (기본값: 다음 달)",
    )
    parser.add_argument(
        "--client",
        default="wishket",
        help="클라이언트 이름 (기본값: wishket)",
    )
    parser.add_argument(
        "--published",
        help="기발행 콘텐츠 DB JSON 경로 (PublishedContent 배열, 없으면 빈 리스트)",
    )

    args = parser.parse_args()

    # --input 또는 --resume 중 하나는 필수 (단, --start-at-stage > 1이면 --input 없어도 됨)
    if not args.input and not args.resume and args.start_at_stage <= 1:
        parser.error("--input 또는 --resume 중 하나를 지정해야 합니다.")

    return args


if __name__ == "__main__":
    asyncio.run(main(_parse_args()))
