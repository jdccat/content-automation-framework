"""허브 시드 필터 + 키워드 확장 + 클러스터링 스냅샷 확인용.

research_unit은 모든 수집 비활성화하여 스킵.
"""
import asyncio
import json
import logging
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

from core.agents.researcher.agent import ResearcherAgent
from core.schemas import ResearchProfile

DATA = {
    "questions": [
        {
            "question": "ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?",
            "intent": "비교 판단",
            "direction": "판단 기준 제시",
        },
    ],
    "target_month": "2026-04",
}

# 스냅샷 전용 (수집 비활성)
PROFILE_SNAPSHOT_ONLY = ResearchProfile(
    volumes=False, content=False, serp_features=False,
    geo=False, related_keywords=False, paa=False,
)

# 풀 프로파일 (GEO 제외 — 키 미설정)
from core.schemas import PROFILE_FULL
PROFILE_RUN = ResearchProfile(
    volumes=True, content=True, serp_features=True,
    geo=False, related_keywords=True, paa=True,
)


async def main():
    run_date = str(date.today())
    output_dir = f"output/researcher/{run_date}_hub_snapshot"
    snapshot_dir = f"{output_dir}/snapshots"

    agent = ResearcherAgent()
    results = await agent.run_json(
        DATA,
        output_dir=output_dir,
        snapshot_dir=snapshot_dir,
        profile=PROFILE_RUN,
    )

    # 결과 요약
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"출력 디렉토리: {output_dir}", file=sys.stderr)
    print(f"시드 수: {len(results)}", file=sys.stderr)

    for hub in results:
        print(f"\n[{hub.seed_id}] {hub.seed_question[:60]}", file=sys.stderr)
        print(f"  전체 키워드: {len(hub.keywords)}개", file=sys.stderr)

    # 스냅샷 파일 확인
    print(f"\n{'='*60}", file=sys.stderr)
    print("생성된 파일:", file=sys.stderr)
    for p in sorted(Path(output_dir).rglob("*.json")):
        size = p.stat().st_size
        print(f"  {p.relative_to(output_dir)}  ({size:,} bytes)", file=sys.stderr)

    # hub_0_seeds 요약
    seeds_path = Path(snapshot_dir) / f"{run_date}_hub_0_seeds.json"
    if seeds_path.exists():
        with open(seeds_path) as f:
            seeds_data = json.load(f)
        for s in seeds_data.get("seeds", []):
            print(f"\n[시드 추출] {s['seed_id']}:", file=sys.stderr)
            print(f"  메인: {s.get('main_keyword', '')}", file=sys.stderr)
            print(f"  시드: {s.get('seeds', s.get('filtered_seeds', []))}", file=sys.stderr)

    # hub_1_keywords 요약
    kw_path = Path(snapshot_dir) / f"{run_date}_hub_1_keywords.json"
    if kw_path.exists():
        with open(kw_path) as f:
            kw_data = json.load(f)
        for sid, info in kw_data.get("per_seed", {}).items():
            aff = info.get("affinity", {})
            main_kw = info.get("main_keyword", "")
            print(f"\n[키워드] {sid}: 메인={main_kw}", file=sys.stderr)
            if aff:
                print(f"  친화도 필터: {aff.get('before', '?')} → {aff.get('after_affinity', '?')} (탈락 {aff.get('dropped_count', '?')}개)", file=sys.stderr)
            print(f"  최종: SEO {info['seo_count']}개, PAA {info['paa_count']}개", file=sys.stderr)
            if info['seo_count'] <= 30:
                print(f"  SEO 목록: {info['seo_keywords']}", file=sys.stderr)

    # hub_2_clusters 요약
    cl_path = Path(snapshot_dir) / f"{run_date}_hub_2_clusters.json"
    if cl_path.exists():
        with open(cl_path) as f:
            cl_data = json.load(f)
        print(f"\n[클러스터링] 대표 키워드: {cl_data['total_representatives']}개", file=sys.stderr)
        print(f"  대표 목록: {cl_data['representatives']}", file=sys.stderr)
        for sid, info in cl_data.get("per_seed", {}).items():
            clusters = info.get("clusters", [])
            orphans = info.get("orphans", [])
            main_kw = info.get("main_keyword", "")
            print(f"\n  [{sid}] 메인={main_kw} | 클러스터 {len(clusters)}개, 고립 {len(orphans)}개", file=sys.stderr)
            for cl in clusters:
                print(f"    {cl['id']}: 대표={cl['representative']} | 의도={cl['shared_intent']} | 멤버 {len(cl['members'])}개", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
