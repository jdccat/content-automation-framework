"""단계별 파이프라인 실행 CLI.

사용법:
  python run_stage.py stage1                     # Stage 1 전체 (표준 입력 사용)
  python run_stage.py stage1 --input input.txt   # Stage 1 전체 (파일 입력)
  python run_stage.py stage1a                    # 1a+1b: 도구 수집만
  python run_stage.py stage1d --date 2026-02-26  # 1d: pool → 클러스터링
  python run_stage.py stage1e --date 2026-02-26  # 1e: 대표 키워드 선정
  python run_stage.py stage1f --date 2026-02-26  # 1f: 아카이브 비교
  python run_stage.py stage1g --date 2026-02-26  # 1g: 포커스 선정
  python run_stage.py stage2 --date 2026-02-26   # Stage 1 스냅샷 → Stage 2만
  python run_stage.py stage3 --date 2026-02-26   # Stage 1+2 스냅샷 → Stage 3만
  python run_stage.py assemble --date 2026-02-26 # 전체 스냅샷 → 결과 조립
  python run_stage.py all                        # 전체 파이프라인
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

SAMPLE_INPUT = """\
질문 의도 : 비교 판단
질문 형태
ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?
앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?
외주 개발 프로젝트를 진행할 때 가장 자주 발생하는 문제는 무엇이며, 어떻게 해결할 수 있나요?
콘텐츠 방향성 : 판단 기준 제시\
"""


def _read_input(args) -> str:
    """입력 텍스트 로드: --input 파일 > SAMPLE_INPUT."""
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            return f.read().strip()
    return SAMPLE_INPUT


def _print_summary(result) -> None:
    """ResearchResult 요약 출력."""
    print(f"\n=== 요약 ===", file=sys.stderr)
    print(f"메인 키워드: {result.main_keyword}", file=sys.stderr)
    print(f"클러스터 수: {len(result.clusters)}", file=sys.stderr)
    focus_count = sum(1 for c in result.clusters if c.is_focus)
    print(f"포커스 클러스터: {focus_count}", file=sys.stderr)
    total_kw = sum(len(c.keywords) for c in result.clusters)
    print(f"총 키워드: {total_kw}", file=sys.stderr)
    vol_kw = sum(
        1 for c in result.clusters for k in c.keywords
        if k.monthly_volume_naver > 0 or k.monthly_volume_google > 0
    )
    print(f"볼륨 보유 키워드: {vol_kw}", file=sys.stderr)
    print(f"고립 키워드: {len(result.orphan_keywords)}개", file=sys.stderr)
    for c in result.clusters:
        label = "★" if c.is_focus else " "
        print(
            f"  {label} [{c.cluster_id}] {c.representative_keyword} "
            f"(kw={len(c.keywords)}, naver_vol={c.total_volume_naver}, "
            f"geo={len(c.geo_citations)})",
            file=sys.stderr,
        )


async def cmd_stage1(args) -> None:
    """Stage 1: 입력 파싱 → 키워드 확장+클러스터링."""
    from core.agents.researcher.agent import ResearcherAgent
    from core.agents.researcher.snapshot import save_snapshot

    input_text = _read_input(args)
    run_date = args.date
    snap_dir = args.snapshot_dir

    agent = ResearcherAgent()
    parsed = agent._parse_input(input_text)
    save_snapshot("input", parsed, run_date, snap_dir)

    seeds = list(dict.fromkeys(parsed.extracted_seeds)) if parsed.extracted_seeds else [parsed.main_keyword]
    all_seeds = seeds

    # 시드 필터
    if parsed.extracted_seeds and agent._config.get("seed_filter", {}).get("enabled"):
        seeds = await agent._filter_seeds(
            seeds, parsed.questions, parsed.intent, parsed.direction,
        )

    stage1 = await agent._stage1_expansion(
        parsed.main_keyword, seeds, parsed.questions,
        intent=parsed.intent, direction=parsed.direction,
        all_seeds=all_seeds,
        snapshot_dir=snap_dir, run_date=run_date,
    )
    save_snapshot("stage1_clusters", stage1, run_date, snap_dir)
    print(f"Stage 1 완료 — 클러스터: {len(stage1.cluster_drafts)}, "
          f"포커스: {sum(1 for cd in stage1.cluster_drafts if cd.is_focus)}",
          file=sys.stderr)


async def cmd_stage1a(args) -> None:
    """Stage 1a+1b: 도구 수집 + 롱테일 + 고객 언어만 실행."""
    from core.agents.researcher.agent import ResearcherAgent
    from core.agents.researcher.snapshot import save_snapshot

    input_text = _read_input(args)
    run_date = args.date
    snap_dir = args.snapshot_dir

    agent = ResearcherAgent()
    parsed = agent._parse_input(input_text)
    save_snapshot("input", parsed, run_date, snap_dir)

    seeds = list(dict.fromkeys(parsed.extracted_seeds)) if parsed.extracted_seeds else [parsed.main_keyword]
    all_seeds = seeds

    if parsed.extracted_seeds and agent._config.get("seed_filter", {}).get("enabled"):
        seeds = await agent._filter_seeds(
            seeds, parsed.questions, parsed.intent, parsed.direction,
        )

    # 1a+1b만 실행 (snapshot_dir 전달하면 stage1_keywords 자동 저장)
    # stage1_expansion 내부에서 pool 스냅샷까지 저장 후 중단할 수 없으므로
    # 직접 서브함수 호출
    from core.agents.researcher import stage1 as stage1_mod
    from core.tools.autocomplete import search_suggestions
    from core.tools.google_related import google_related_searches
    from core.tools.google_paa import google_paa
    from core.tools.naver_searchad import naver_keyword_volume

    pool = await stage1_mod._stage1a_collect_keywords(
        seeds, parsed.intent, parsed.direction,
        all_seeds=all_seeds,
        safe_tool_call=agent._safe_tool_call,
        search_suggestions_fn=search_suggestions,
        google_related_fn=google_related_searches,
        google_paa_fn=google_paa,
        naver_keyword_volume_fn=naver_keyword_volume,
        llm_call_fn=agent._llm_call,
        config=agent._config,
    )
    await stage1_mod._stage1a_longtail_expansion(
        pool, seeds,
        config=agent._config,
        safe_tool_call=agent._safe_tool_call,
        search_suggestions_fn=search_suggestions,
    )
    internal_kws = stage1_mod.stage1b_customer_language("", agent._config)
    pool.internal_data.extend(internal_kws)

    save_snapshot("stage1_keywords", pool, run_date, snap_dir)
    total = len(pool.google) + len(pool.naver) + len(pool.keyword_tool) + len(pool.internal_data) + len(pool.paa)
    print(f"Stage 1a 완료 — 원시 키워드: {total}, 볼륨: {len(pool.volumes)}개",
          file=sys.stderr)


async def cmd_stage1d(args) -> None:
    """Stage 1d: pool 스냅샷 → 중복제거 + 관련성필터 + LLM 클러스터링."""
    from core.agents.researcher.agent import ResearcherAgent
    from core.agents.researcher.snapshot import (
        load_input, load_pool, save_deduped, save_stage1_sub,
    )
    from core.agents.researcher import stage1 as stage1_mod

    run_date = args.date
    snap_dir = args.snapshot_dir

    parsed = load_input(run_date, snap_dir)
    pool = load_pool(run_date, snap_dir)
    print(f"스냅샷 로드: input + stage1_keywords ({run_date})", file=sys.stderr)

    agent = ResearcherAgent()
    seeds = list(dict.fromkeys(parsed.extracted_seeds)) if parsed.extracted_seeds else [parsed.main_keyword]

    deduped = stage1_mod.deduplicate_keywords(pool)
    if deduped and agent._config.get("relevance_filter", {}).get("enabled", True):
        deduped = await stage1_mod._filter_relevance(
            deduped, parsed.questions or [], parsed.intent, parsed.direction, seeds,
            llm_call_fn=agent._llm_call,
            config=agent._config,
        )
    save_deduped(deduped, pool, run_date, snap_dir)

    cluster_drafts, orphan_keywords = await stage1_mod._stage1d_llm_clustering(
        deduped, llm_call_fn=agent._llm_call, config=agent._config,
    )
    save_stage1_sub("stage1d_clusters", cluster_drafts, orphan_keywords, pool, run_date, snap_dir)
    print(f"Stage 1d 완료 — 클러스터: {len(cluster_drafts)}, 고립: {len(orphan_keywords)}",
          file=sys.stderr)


async def cmd_stage1e(args) -> None:
    """Stage 1e: 1d 스냅샷 → LLM 대표 키워드 선정."""
    from core.agents.researcher.agent import ResearcherAgent
    from core.agents.researcher.snapshot import load_pool, load_stage1_sub, save_stage1_sub
    from core.agents.researcher import stage1 as stage1_mod

    run_date = args.date
    snap_dir = args.snapshot_dir

    prev = load_stage1_sub("stage1d_clusters", run_date, snap_dir)
    pool = load_pool(run_date, snap_dir)
    print(f"스냅샷 로드: stage1d_clusters + pool ({run_date})", file=sys.stderr)

    agent = ResearcherAgent()
    await stage1_mod._stage1e_llm_representative(
        prev.cluster_drafts, prev.volumes,
        llm_call_fn=agent._llm_call, config=agent._config,
    )
    save_stage1_sub("stage1e_clusters", prev.cluster_drafts, prev.orphan_keywords, pool, run_date, snap_dir)
    reps = [cd.representative for cd in prev.cluster_drafts if cd.representative]
    print(f"Stage 1e 완료 — 대표 키워드: {len(reps)}개", file=sys.stderr)


async def cmd_stage1f(args) -> None:
    """Stage 1f: 1e 스냅샷 → 아카이브 비교."""
    from core.agents.researcher.agent import ResearcherAgent
    from core.agents.researcher.snapshot import load_pool, load_stage1_sub, save_stage1_sub
    from core.agents.researcher import stage1 as stage1_mod

    run_date = args.date
    snap_dir = args.snapshot_dir

    prev = load_stage1_sub("stage1e_clusters", run_date, snap_dir)
    pool = load_pool(run_date, snap_dir)
    print(f"스냅샷 로드: stage1e_clusters + pool ({run_date})", file=sys.stderr)

    agent = ResearcherAgent()
    await stage1_mod._stage1f_archive_comparison(
        prev.cluster_drafts,
        llm_call_fn=agent._llm_call,
        config=agent._config,
        load_archive_reps_fn=agent._load_archive_reps,
        load_archive_clusters_fn=agent._load_archive_clusters,
    )
    save_stage1_sub("stage1f_clusters", prev.cluster_drafts, prev.orphan_keywords, pool, run_date, snap_dir)
    verdicts = {}
    for cd in prev.cluster_drafts:
        v = cd.archive_verdict or "unknown"
        verdicts[v] = verdicts.get(v, 0) + 1
    print(f"Stage 1f 완료 — 아카이브 판정: {verdicts}", file=sys.stderr)


async def cmd_stage1g(args) -> None:
    """Stage 1g: 1f 스냅샷 → 포커스 선정 → stage1_clusters 최종 저장."""
    from core.agents.researcher.agent import ResearcherAgent
    from core.agents.researcher.snapshot import load_input, load_pool, load_stage1_sub, save_snapshot
    from core.agents.researcher import stage1 as stage1_mod
    from core.schemas import Stage1Output

    run_date = args.date
    snap_dir = args.snapshot_dir

    parsed = load_input(run_date, snap_dir)
    prev = load_stage1_sub("stage1f_clusters", run_date, snap_dir)
    pool = load_pool(run_date, snap_dir)
    print(f"스냅샷 로드: input + stage1f_clusters + pool ({run_date})", file=sys.stderr)

    agent = ResearcherAgent()
    await stage1_mod._stage1g_focus_selection(
        prev.cluster_drafts, parsed.questions or [], prev.volumes,
        llm_call_fn=agent._llm_call, config=agent._config,
    )
    final = Stage1Output(
        cluster_drafts=prev.cluster_drafts,
        orphan_keywords=prev.orphan_keywords,
        paa_questions=pool.paa_questions,
        volumes=pool.volumes,
        volumes_pc=pool.volumes_pc,
        volumes_mobile=pool.volumes_mobile,
    )
    save_snapshot("stage1_clusters", final, run_date, snap_dir)
    focus = sum(1 for cd in final.cluster_drafts if cd.is_focus)
    print(f"Stage 1g 완료 — 포커스: {focus}/{len(final.cluster_drafts)}",
          file=sys.stderr)


async def cmd_stage2(args) -> None:
    """Stage 2: Stage 1 스냅샷 로드 → 검증 수집."""
    from core.agents.researcher.agent import ResearcherAgent
    from core.agents.researcher.snapshot import load_input, load_stage1, save_snapshot

    run_date = args.date
    snap_dir = args.snapshot_dir

    parsed = load_input(run_date, snap_dir)
    stage1 = load_stage1(run_date, snap_dir)
    print(f"스냅샷 로드 완료: input + stage1 ({run_date})", file=sys.stderr)

    agent = ResearcherAgent()

    focus_reps = [
        cd.representative for cd in stage1.cluster_drafts
        if cd.representative and cd.is_focus
    ]
    focus_all_keywords: list[str] = []
    for cd in stage1.cluster_drafts:
        if cd.is_focus:
            focus_all_keywords.extend(kw for kw, _ in cd.keywords)
    focus_all_keywords = list(dict.fromkeys(focus_all_keywords))

    rep_paa_questions: dict[str, list[str]] = {}
    for cd in stage1.cluster_drafts:
        if cd.is_focus and cd.representative:
            qs: list[str] = []
            for kw, _ in cd.keywords:
                qs.extend(stage1.paa_questions.get(kw, []))
            rep_paa_questions[cd.representative] = list(dict.fromkeys(qs))

    stage2 = await agent._stage2_validation(
        focus_reps,
        all_keywords=focus_all_keywords,
        paa_questions=rep_paa_questions,
        stage1_volumes=stage1.volumes,
        stage1_volumes_pc=stage1.volumes_pc,
        stage1_volumes_mobile=stage1.volumes_mobile,
    )
    save_snapshot("stage2_serp", stage2, run_date, snap_dir)
    print(f"Stage 2 완료 — 볼륨 키: {len(stage2.volumes)}", file=sys.stderr)


async def cmd_stage3(args) -> None:
    """Stage 3: Stage 1 스냅샷 → focus reps 추출 → GEO 수집."""
    from core.agents.researcher.agent import ResearcherAgent
    from core.agents.researcher.snapshot import load_stage1, save_snapshot

    run_date = args.date
    snap_dir = args.snapshot_dir

    stage1 = load_stage1(run_date, snap_dir)
    print(f"스냅샷 로드 완료: stage1 ({run_date})", file=sys.stderr)

    focus_reps = [
        cd.representative for cd in stage1.cluster_drafts
        if cd.representative and cd.is_focus
    ]

    agent = ResearcherAgent()
    stage3 = await agent._stage3_geo(focus_reps)
    save_snapshot("stage3_geo", stage3, run_date, snap_dir)
    print(f"Stage 3 완료 — GEO 키: {len(stage3.citations)}", file=sys.stderr)


async def cmd_assemble(args) -> None:
    """전체 스냅샷 → ResearchResult 조립."""
    from core.agents.researcher.agent import ResearcherAgent
    from core.agents.researcher.snapshot import (
        load_input, load_stage1, load_stage2, load_stage3,
    )

    run_date = args.date
    snap_dir = args.snapshot_dir

    parsed = load_input(run_date, snap_dir)
    stage1 = load_stage1(run_date, snap_dir)
    stage2 = load_stage2(run_date, snap_dir)
    stage3 = load_stage3(run_date, snap_dir)
    print(f"스냅샷 로드 완료: input + stage1~3 ({run_date})", file=sys.stderr)

    agent = ResearcherAgent()
    result = agent._assemble_result(parsed, stage1, stage2, stage3)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = f"output/researcher_v4_result_{ts}.json"
    from pathlib import Path
    Path("output").mkdir(exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result.model_dump(), f, ensure_ascii=False, indent=2)
    print(f"결과 저장: {output_path}", file=sys.stderr)
    _print_summary(result)


async def cmd_all(args) -> None:
    """전체 파이프라인 실행."""
    from core.agents.researcher.agent import ResearcherAgent

    input_text = _read_input(args)
    agent = ResearcherAgent()
    result = await agent.run(input_text, snapshot_dir=args.snapshot_dir)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = f"output/researcher_v4_result_{ts}.json"
    from pathlib import Path
    Path("output").mkdir(exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result.model_dump(), f, ensure_ascii=False, indent=2)
    print(f"결과 저장: {output_path}", file=sys.stderr)
    _print_summary(result)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="리서처 파이프라인 단계별 실행",
    )
    parser.add_argument(
        "stage",
        choices=[
            "stage1", "stage1a", "stage1d", "stage1e", "stage1f", "stage1g",
            "stage2", "stage3", "assemble", "all",
        ],
        help="실행할 단계 (stage1a/d/e/f/g: Stage 1 서브스텝)",
    )
    parser.add_argument(
        "--date",
        default=str(date.today()),
        help="스냅샷 날짜 (기본: 오늘, YYYY-MM-DD)",
    )
    parser.add_argument(
        "--input",
        default="",
        help="Stage 1용 입력 텍스트 파일 경로",
    )
    parser.add_argument(
        "--snapshot-dir",
        default="snapshots",
        help="스냅샷 디렉토리 (기본: snapshots)",
    )
    args = parser.parse_args()

    handlers = {
        "stage1": cmd_stage1,
        "stage1a": cmd_stage1a,
        "stage1d": cmd_stage1d,
        "stage1e": cmd_stage1e,
        "stage1f": cmd_stage1f,
        "stage1g": cmd_stage1g,
        "stage2": cmd_stage2,
        "stage3": cmd_stage3,
        "assemble": cmd_assemble,
        "all": cmd_all,
    }
    asyncio.run(handlers[args.stage](args))


if __name__ == "__main__":
    main()
