"""1단계만 실행하여 클러스터 결과 확인."""

import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

from core.agents.researcher.agent import ResearcherAgent
from core.tools.seed_filter import filter_generic_seeds

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
    parsed = agent._parse_input(SAMPLE_INPUT)

    if parsed.extracted_seeds:
        all_seeds = list(dict.fromkeys(parsed.extracted_seeds))
        seeds = all_seeds
    else:
        seeds = [parsed.main_keyword]
        all_seeds = seeds

    # 시드 필터
    if parsed.extracted_seeds and agent._config.get("seed_filter", {}).get("enabled"):
        model = agent._config.get("seed_filter", {}).get("model", "gpt-4.1-mini")
        filtered = await agent._safe_tool_call(
            "seed_filter",
            filter_generic_seeds(seeds, parsed.intent, parsed.direction, model),
            default=seeds,
        )
        if isinstance(filtered, list) and filtered:
            print(f"시드 필터: {len(seeds)} → {len(filtered)}", file=sys.stderr)
            seeds = filtered

    stage1 = await agent._stage1_expansion(
        parsed.main_keyword, seeds, parsed.questions,
        client="", intent=parsed.intent, direction=parsed.direction,
        all_seeds=all_seeds,
    )

    # 결과 출력
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"클러스터 수: {len(stage1.cluster_drafts)}", file=sys.stderr)
    focus = [c for c in stage1.cluster_drafts if c.is_focus]
    nonfocus = [c for c in stage1.cluster_drafts if not c.is_focus]
    print(f"포커스: {len(focus)}  |  비포커스: {len(nonfocus)}", file=sys.stderr)
    print(f"고립 키워드: {len(stage1.orphan_keywords)}개", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    for c in stage1.cluster_drafts:
        label = "★" if c.is_focus else "  "
        kws = [kw for kw, _ in c.keywords]
        print(
            f"{label} [{c.cluster_id}] 대표: {c.representative}  "
            f"(kw={len(c.keywords)}, archive={c.archive_verdict or 'N/A'})",
            file=sys.stderr,
        )
        print(f"   키워드: {', '.join(kws)}", file=sys.stderr)
        print(file=sys.stderr)

    if stage1.orphan_keywords:
        print(f"고립: {', '.join(stage1.orphan_keywords[:30])}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
