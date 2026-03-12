"""리서처 v4 실행 스크립트."""

import asyncio
import json
import logging
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

from core.agents.researcher.agent import ResearcherAgent

SAMPLE_INPUT = """\
질문 의도 : 비교 판단
질문 형태
ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?
앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?
외주 개발 프로젝트를 진행할 때 가장 자주 발생하는 문제는 무엇이며, 어떻게 해결할 수 있나요?
콘텐츠 방향성 : 판단 기준 제시\
"""


async def main():
    agent = ResearcherAgent()
    result = await agent.run(SAMPLE_INPUT)

    # JSON 결과 파일 저장
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = f"output/researcher_v4_result_{ts}.json"
    output = result.model_dump()
    with open(output_path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"결과 저장: {output_path}", file=sys.stderr)

    # 요약 통계 (stderr)
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


if __name__ == "__main__":
    asyncio.run(main())
