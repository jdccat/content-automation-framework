"""리서처 JSON 입력 실행 스크립트 — 시드별 개별 출력."""

import asyncio
import json
import logging
import sys
from datetime import date

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

from core.agents.researcher.agent import ResearcherAgent

SAMPLE_JSON = {
    "questions": [
        {
            "question": "ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?",
            "intent": "비교 판단",
            "direction": "판단 기준 제시",
        },
        {
            "question": "앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?",
            "intent": "비교 판단",
            "direction": "판단 기준 제시",
        },
        {
            "question": "외주 개발 프로젝트를 진행할 때 가장 자주 발생하는 문제는 무엇이며, 어떻게 해결할 수 있나요?",
            "intent": "비교 판단",
            "direction": "판단 기준 제시",
        },
    ],
    "target_month": "2026-04",
}


async def main():
    agent = ResearcherAgent()
    run_date = str(date.today())
    output_dir = f"output/researcher/{run_date}"

    results = await agent.run_json(SAMPLE_JSON, output_dir=output_dir)

    # 요약 출력
    print(f"\n=== run_json 요약 ===", file=sys.stderr)
    print(f"출력 디렉토리: {output_dir}", file=sys.stderr)
    print(f"시드 수: {len(results)}", file=sys.stderr)
    for hub in results:
        print(
            f"  [{hub.seed_id}] {hub.seed_question[:50]}... "
            f"(keywords={len(hub.keywords)})",
            file=sys.stderr,
        )

    # manifest 확인
    manifest_path = f"{output_dir}/manifest.json"
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
        print(f"\nmanifest.json:", file=sys.stderr)
        print(json.dumps(manifest, ensure_ascii=False, indent=2), file=sys.stderr)
    except FileNotFoundError:
        print(f"manifest.json 미생성", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
