"""리서처 에이전트 실행 테스트."""

import asyncio
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

from core.agents.researcher import run_keyword_research


async def main() -> None:
    result = await run_keyword_research(
        question_intent="비교 판단",
        questions=[
            "ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?",
            "앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?",
            "외주 개발 프로젝트를 진행할 때 가장 자주 발생하는 문제는 무엇이며, 어떻게 해결할 수 있나요?",
        ],
        content_direction="판단 기준 제시",
    )

    print("\n" + "=" * 60)
    print("리서처 결과 (KeywordSearchData v3)")
    print("=" * 60)

    data = json.loads(result.model_dump_json())

    # 전체 JSON 저장
    with open("output/last_run.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print("전체 결과 → output/last_run.json 저장 완료")

    # 품질 체크 요약
    print("\n" + "=" * 60)
    print("품질 체크")
    print("=" * 60)
    keywords = data["keywords"]
    pages = data["page_analyses"]
    print(f"키워드 수: {len(keywords)}")
    print(f"페이지 분석 수: {len(pages)}")

    # 트렌드 데이터
    naver_trend_count = sum(1 for k in keywords if k["naver_trend"] > 0)
    google_trend_count = sum(1 for k in keywords if k["google_trend"] > 0)
    print(f"네이버 트렌드 있는 키워드: {naver_trend_count}/{len(keywords)}")
    print(f"구글 트렌드 있는 키워드: {google_trend_count}/{len(keywords)}")

    # 검색량
    volume_count = sum(1 for k in keywords if k["monthly_volume"] > 0)
    print(f"검색량 데이터 있는 키워드: {volume_count}/{len(keywords)}")

    # 자동완성
    ac_count = sum(1 for k in keywords if k["autocomplete_suggestions"])
    print(f"자동완성 있는 키워드: {ac_count}/{len(keywords)}")

    # 연관 키워드
    rel_count = sum(1 for k in keywords if k["related_keywords"])
    print(f"연관 키워드 있는 키워드: {rel_count}/{len(keywords)}")

    # 검색 결과
    search_results = data["search_results"]
    sources = {}
    for sr in search_results:
        s = sr.get("source", "unknown")
        sources[s] = sources.get(s, 0) + 1
    print(f"통합 검색 결과: {len(search_results)}개 (소스: {sources})")

    # 페이지 분석
    if pages:
        unique_urls = {p["url"] for p in pages}
        pages_with_h2 = sum(1 for p in pages if p["h2_structure"])
        naver_pages = sum(1 for p in pages if p["source"] == "naver")
        google_pages = sum(1 for p in pages if p["source"] == "google")
        print(f"고유 URL: {len(unique_urls)}개")
        print(f"H2 구조 있는 페이지: {pages_with_h2}/{len(pages)}")
        print(f"페이지 소스: 네이버={naver_pages}, 구글={google_pages}")

    # 상위 키워드 5개 요약
    print("\n" + "=" * 60)
    print("상위 키워드 (combined_trend 기준)")
    print("=" * 60)
    sorted_kw = sorted(keywords, key=lambda k: k["combined_trend"], reverse=True)
    for i, kw in enumerate(sorted_kw[:10], 1):
        ac = len(kw["autocomplete_suggestions"])
        rel = len(kw["related_keywords"])
        print(
            f"  {i:2d}. {kw['keyword']}"
            f"  N={kw['naver_trend']:.0f} G={kw['google_trend']:.0f}"
            f"  combined={kw['combined_trend']:.1f}"
            f"  vol={kw['monthly_volume']}"
            f"  방향: N-{kw['naver_trend_direction']} G-{kw['google_trend_direction']}"
            f"  자동완성={ac} 연관={rel}"
        )


if __name__ == "__main__":
    asyncio.run(main())
