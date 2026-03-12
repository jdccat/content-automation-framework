"""research_unit 결과 확인용 실행 스크립트."""
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

async def main():
    run_date = str(date.today())
    output_dir = f"output/researcher/{run_date}_ru_test"
    snapshot_dir = f"output/researcher/{run_date}_ru_test/snapshots"

    agent = ResearcherAgent()
    results = await agent.run_json(DATA, output_dir=output_dir, snapshot_dir=snapshot_dir)

    # 결과 요약
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"출력 디렉토리: {output_dir}", file=sys.stderr)
    print(f"시드 수: {len(results)}", file=sys.stderr)

    for hub in results:
        print(f"\n[{hub.seed_id}] {hub.seed_question[:60]}", file=sys.stderr)
        print(f"  키워드: {len(hub.keywords)}개", file=sys.stderr)
        print(f"  키워드 목록: {hub.keywords[:20]}", file=sys.stderr)
        r = hub.research
        print(f"  volumes: {len(r.volumes)}개", file=sys.stderr)
        print(f"  google_content_metas: {len(r.google_content_metas)}개", file=sys.stderr)
        print(f"  naver_content_metas: {len(r.naver_content_metas)}개", file=sys.stderr)
        print(f"  h2_topics: {len(r.h2_topics)}개", file=sys.stderr)
        print(f"  google_serp_features: {len(r.google_serp_features)}개", file=sys.stderr)
        print(f"  naver_serp_features: {len(r.naver_serp_features)}개", file=sys.stderr)
        print(f"  geo_citations: {len(r.geo_citations)}개", file=sys.stderr)
        print(f"  related_keywords: {len(r.related_keywords)}개", file=sys.stderr)
        print(f"  paa_questions: {len(r.paa_questions)}개", file=sys.stderr)

    # 파일 목록
    print(f"\n{'='*60}", file=sys.stderr)
    print("생성된 파일:", file=sys.stderr)
    for p in sorted(Path(output_dir).rglob("*.json")):
        size = p.stat().st_size
        print(f"  {p.relative_to(output_dir)}  ({size:,} bytes)", file=sys.stderr)

if __name__ == "__main__":
    asyncio.run(main())
