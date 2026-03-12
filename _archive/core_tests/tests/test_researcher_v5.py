"""리서처 v5 테스트 — 리서치 유닛 + Stage 1 허브 + 레거시 호환 + 파서."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.schemas import (
    ClusterDraft,
    HubResearchData,
    ParsedInput,
    PROFILE_DEMAND,
    PROFILE_FULL,
    PROFILE_UMBRELLA,
    ResearchProfile,
    ResearchUnitOutput,
    SeedQuestion,
    Stage2Output,
    Stage3Output,
)


# ── 공용 mock 도구 팩토리 ─────────────────────────────────────────


def _make_tool_fns():
    """모든 도구를 AsyncMock으로 생성."""
    return {
        "safe_tool_call": AsyncMock(side_effect=lambda label, coro, default="{}": default),
        "google_search_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "naver_keyword_volume_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "google_keyword_trend_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "naver_keyword_trend_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "naver_blog_search_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "web_fetch_fn": MagicMock(return_value=AsyncMock(return_value="")),
        "naver_serp_features_fn": MagicMock(return_value=AsyncMock(
            return_value=json.dumps({
                "keyword": "test",
                "knowledge_snippet": False,
                "smart_block": False,
                "smart_block_components": [],
            }),
        )),
        "search_suggestions_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "google_related_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "google_paa_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "ai_search_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "perplexity_search_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "geo_claude_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
        "geo_gemini_fn": MagicMock(return_value=AsyncMock(return_value="{}")),
    }


# ── 리서치 유닛 테스트 ────────────────────────────────────────────


class TestRunResearchUnit:
    @pytest.mark.asyncio
    async def test_full_profile(self):
        """FULL 프로파일 → 모든 모듈 호출."""
        from core.agents.researcher.research_unit.runner import run_research_unit

        fns = _make_tool_fns()
        result = await run_research_unit(
            ["외주 개발", "앱 개발"],
            PROFILE_FULL,
            **fns,
        )
        assert isinstance(result, ResearchUnitOutput)
        # safe_tool_call이 호출되었어야 함 (SERP, volumes, content 등)
        assert fns["safe_tool_call"].call_count > 0

    @pytest.mark.asyncio
    async def test_demand_profile(self):
        """DEMAND 프로파일 → geo/paa/related 미호출."""
        from core.agents.researcher.research_unit.runner import run_research_unit

        fns = _make_tool_fns()
        result = await run_research_unit(
            ["외주 개발"],
            PROFILE_DEMAND,
            **fns,
        )
        assert isinstance(result, ResearchUnitOutput)
        # DEMAND 프로파일은 paa/related/geo 없음
        assert result.paa_questions == {}
        assert result.related_keywords == {}
        assert result.geo_citations == {}

    @pytest.mark.asyncio
    async def test_empty_keywords(self):
        """빈 입력 → 빈 출력."""
        from core.agents.researcher.research_unit.runner import run_research_unit

        fns = _make_tool_fns()
        result = await run_research_unit([], PROFILE_FULL, **fns)
        assert isinstance(result, ResearchUnitOutput)
        assert result.volumes == {}
        assert result.google_content_metas == {}

    @pytest.mark.asyncio
    async def test_volumes_only_profile(self):
        """volumes만 켠 커스텀 프로파일."""
        from core.agents.researcher.research_unit.runner import run_research_unit

        profile = ResearchProfile(
            volumes=True, related_keywords=False, paa=False,
            content=False, serp_features=False, geo=False,
        )
        fns = _make_tool_fns()
        result = await run_research_unit(["테스트"], profile, **fns)
        assert isinstance(result, ResearchUnitOutput)
        assert result.google_content_metas == {}
        assert result.google_serp_features == {}

    @pytest.mark.asyncio
    async def test_serp_features_populated(self):
        """serp_features 프로파일 → 구글/네이버 SERP 피처 수집."""
        from core.agents.researcher.research_unit.runner import run_research_unit

        fns = _make_tool_fns()
        result = await run_research_unit(
            ["외주 개발"],
            ResearchProfile(
                volumes=False, related_keywords=False, paa=False,
                content=False, serp_features=True, geo=False,
            ),
            **fns,
        )
        assert "외주 개발" in result.google_serp_features
        assert "외주 개발" in result.naver_serp_features


# ── Stage 1 허브 리서치 테스트 ────────────────────────────────────


def _make_seed_questions(n: int = 3) -> list[SeedQuestion]:
    """테스트용 시드 질문 생성."""
    questions = [
        "ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?",
        "앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?",
        "외주 개발 프로젝트를 진행할 때 가장 자주 발생하는 문제는 무엇이며, 어떻게 해결할 수 있나요?",
    ]
    return [
        SeedQuestion(
            seed_id=f"sq{i + 1:03d}",
            question=questions[i],
            intent=["비교 판단"],
            content_direction=["판단 기준 제시"],
        )
        for i in range(min(n, len(questions)))
    ]


def _hub_patches():
    """허브 리서치의 LLM 의존 단계를 mock — 시드추출/도메인필터/클러스터링/대표선정."""
    async def _passthrough_domain_filter(deduped, *args, **kwargs):
        return deduped

    async def _simple_clustering(deduped, **kwargs):
        if not deduped:
            return [], []
        cd = ClusterDraft(
            cluster_id="c000",
            keywords=deduped,
            representative=deduped[0][0],
        )
        return [cd], []

    # 시드 추출 mock — 질문에서 간단한 시드 반환
    async def _mock_seed_extract(question, intent, direction, **kwargs):
        # 질문의 첫 3~4 토큰을 main_keyword로
        tokens = question.split()[:4]
        main_kw = " ".join(t.rstrip("을를이가의") for t in tokens if len(t) >= 2)[:20]
        return main_kw, [main_kw]

    return (
        patch(
            "core.agents.researcher.stage1_hub._llm_extract_seeds",
            new_callable=AsyncMock,
            side_effect=_mock_seed_extract,
        ),
        patch(
            "core.agents.researcher.stage1_hub._domain_filter",
            new_callable=AsyncMock,
            side_effect=_passthrough_domain_filter,
        ),
        patch(
            "core.agents.researcher.stage1_hub._stage1d_llm_clustering",
            new_callable=AsyncMock,
            side_effect=_simple_clustering,
        ),
        patch(
            "core.agents.researcher.stage1_hub._stage1e_llm_representative",
            new_callable=AsyncMock,
        ),
    )


class TestStage1Hub:
    @pytest.mark.asyncio
    async def test_hub_per_seed(self):
        """시드 3개 → HubResearchData 3개."""
        from core.agents.researcher.stage1_hub import stage1_hub_research

        seeds = _make_seed_questions(3)
        fns = _make_tool_fns()
        config = {"longtail": {"max_second_pass": 0}}

        p0, p1, p2, p3 = _hub_patches()
        with p0, p1, p2, p3:
            result = await stage1_hub_research(
                seeds,
                config=config,
                llm_call_fn=AsyncMock(return_value="[]"),
                **fns,
            )
        assert len(result) == 3
        for i, hub in enumerate(result):
            assert isinstance(hub, HubResearchData)
            assert hub.seed_id == f"sq{i + 1:03d}"
            assert hub.seed_question == seeds[i].question

    @pytest.mark.asyncio
    async def test_research_unit_called_once(self):
        """합산 키워드로 run_research_unit 1회 호출."""
        from core.agents.researcher.stage1_hub import stage1_hub_research

        seeds = _make_seed_questions(2)
        fns = _make_tool_fns()
        config = {"longtail": {"max_second_pass": 0}}

        p0, p1, p2, p3 = _hub_patches()
        with (
            patch(
                "core.agents.researcher.stage1_hub.run_research_unit",
                new_callable=AsyncMock,
                return_value=ResearchUnitOutput(),
            ) as mock_ru,
            p0, p1, p2, p3,
        ):
            await stage1_hub_research(
                seeds,
                config=config,
                llm_call_fn=AsyncMock(return_value="[]"),
                **fns,
            )
            # run_research_unit은 정확히 1회 호출
            assert mock_ru.call_count == 1

    @pytest.mark.asyncio
    async def test_slice_by_seed(self):
        """시드별 분배 정확성."""
        from core.agents.researcher.stage1_hub import _slice_research_by_keywords
        from core.schemas import ResearchUnitOutput

        research = ResearchUnitOutput(
            volumes={
                "erp 외주": {"naver_volume": 100},
                "앱 개발": {"naver_volume": 200},
                "외주 비용": {"naver_volume": 50},
            },
            google_content_metas={
                "erp 외주": [{"rank": 1, "title": "ERP"}],
                "앱 개발": [{"rank": 1, "title": "App"}],
            },
        )

        sliced = _slice_research_by_keywords(research, ["ERP 외주", "외주 비용"])
        assert "erp 외주" in sliced.volumes
        assert "외주 비용" in sliced.volumes
        assert "앱 개발" not in sliced.volumes
        assert "erp 외주" in sliced.google_content_metas
        assert "앱 개발" not in sliced.google_content_metas

    @pytest.mark.asyncio
    async def test_empty_seeds(self):
        """빈 시드 → 빈 결과."""
        from core.agents.researcher.stage1_hub import stage1_hub_research

        fns = _make_tool_fns()
        result = await stage1_hub_research(
            [],
            config={},
            llm_call_fn=AsyncMock(return_value="[]"),
            **fns,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_paa_separation(self):
        """PAA 질문은 키워드 풀에서 분리된다."""
        from core.agents.researcher.stage1_hub import _separate_paa

        deduped = [
            ("erp 외주 개발", "google"),
            ("erp 외주 개발 업체란 무엇인가요?", "google"),
            ("외주 비용", "keyword_tool"),
            ("외주 업체 선정 기준은 무엇인가요?", "google"),
        ]
        seo, paa = _separate_paa(deduped)
        assert len(seo) == 2
        assert len(paa) == 2
        assert seo[0][0] == "erp 외주 개발"
        assert "무엇인가요" in paa[0][0]

    @pytest.mark.asyncio
    async def test_seed_filter(self):
        """1토큰 시드가 제거된다."""
        from core.agents.researcher.stage1_hub import _filter_seeds

        seeds = ["ERP 외주 개발", "ERP 외주", "ERP", "외주", "개발"]
        filtered, removed = _filter_seeds(seeds, "질문")
        assert filtered == ["ERP 외주 개발", "ERP 외주"]
        assert set(removed) == {"ERP", "외주", "개발"}

    @pytest.mark.asyncio
    async def test_seed_filter_all_single(self):
        """모든 시드가 1토큰이면 질문 전체를 시드로."""
        from core.agents.researcher.stage1_hub import _filter_seeds

        filtered, removed = _filter_seeds(["ERP", "외주"], "ERP 외주 개발 비용?")
        assert filtered == ["ERP 외주 개발 비용?"]
        assert removed == ["ERP", "외주"]

    @pytest.mark.asyncio
    async def test_cap_representatives(self):
        """대표 키워드 캡이 적용된다."""
        from core.agents.researcher.stage1_hub import _cap_representatives

        clusters = [
            ClusterDraft(cluster_id=f"c{i:03d}", representative=f"rep{i}")
            for i in range(10)
        ]
        orphans = [f"orphan{i}" for i in range(5)]
        volumes = {f"rep{i}": (10 - i) * 100 for i in range(10)}

        reps = _cap_representatives(clusters, orphans, volumes, max_reps=5)
        assert len(reps) == 5
        # 볼륨 높은 순
        assert reps[0] == "rep0"

    def test_affinity_filter_domain_qualifier(self):
        """도메인 한정어(erp) 없는 범용 키워드가 제거된다."""
        from core.agents.researcher.stage1_hub import _affinity_filter

        keywords = [
            ("erp 외주 개발 업체 비교", "google"),
            ("erp 외주 가격", "keyword_tool"),
            ("erp 외주 개발 사례", "google"),
            ("외주 개발 비용", "keyword_tool"),     # erp 없음
            ("개발 업체", "google"),                 # erp 없음
            ("앱 개발 업체", "google"),              # erp 없음
            ("외주 개발 계약서", "keyword_tool"),    # erp 없음
        ]
        passed, dropped = _affinity_filter(keywords, "ERP 외주 개발 업체")
        passed_kws = {kw for kw, _ in passed}
        dropped_kws = {kw for kw, _ in dropped}

        # ERP 포함 키워드는 통과
        assert "erp 외주 개발 업체 비교" in passed_kws
        assert "erp 외주 가격" in passed_kws
        assert "erp 외주 개발 사례" in passed_kws

        # ERP 없는 범용 키워드는 탈락
        assert "외주 개발 비용" in dropped_kws
        assert "개발 업체" in dropped_kws
        assert "앱 개발 업체" in dropped_kws
        assert "외주 개발 계약서" in dropped_kws

    def test_affinity_filter_short_main_keyword(self):
        """2토큰 메인 키워드 → 헤드=첫 1토큰."""
        from core.agents.researcher.stage1_hub import _affinity_filter

        # "외주 개발" → 헤드 {"외주"}, 겹침 ≥ 50%
        keywords = [
            ("외주 개발 비용", "google"),     # 헤드 "외주" ✓, 2/2
            ("외주 개발 업체", "google"),     # 헤드 ✓, 2/2
            ("외주 개발 방법", "google"),     # 헤드 ✓, 2/2
            ("앱 개발", "google"),           # "외주" 없음 → 탈락
            ("이사 청소", "google"),          # 겹침 0%
        ]
        passed, dropped = _affinity_filter(keywords, "외주 개발")
        passed_kws = {kw for kw, _ in passed}
        assert "외주 개발 비용" in passed_kws
        assert "외주 개발 업체" in passed_kws
        assert "외주 개발 방법" in passed_kws
        assert "앱 개발" not in passed_kws
        assert "이사 청소" not in passed_kws

    def test_affinity_filter_overlap_threshold(self):
        """겹침 비율 임계값이 적용된다."""
        from core.agents.researcher.stage1_hub import _affinity_filter

        keywords = [
            ("erp 외주 개발 업체 비교", "google"),  # 4/4 = 100%
            ("erp 외주", "google"),                  # 2/4 = 50%
            ("erp 웹", "google"),                    # 1/4 = 25%
        ]
        # min_overlap_ratio=0.5 (기본)
        passed, dropped = _affinity_filter(keywords, "ERP 외주 개발 업체")
        passed_kws = {kw for kw, _ in passed}
        assert "erp 외주 개발 업체 비교" in passed_kws  # 100%
        assert "erp 외주" in passed_kws                  # 50%
        assert "erp 웹" not in passed_kws                # 25% < 50%


# ── LLM 시드 추출 테스트 ──────────────────────────────────────────────


class TestLlmExtractSeeds:
    @pytest.mark.asyncio
    async def test_basic_extraction(self):
        """LLM이 올바른 JSON 반환 → main_keyword + seeds."""
        from core.agents.researcher.stage1_hub import _llm_extract_seeds

        llm_fn = AsyncMock(return_value=json.dumps({
            "main_keyword": "앱 개발 견적",
            "seeds": ["앱 개발 견적", "앱 개발 비용", "앱 개발 업체"],
        }))

        main_kw, seeds = await _llm_extract_seeds(
            "앱 개발 견적이 업체마다 다른 이유", "비교 판단", "판단 기준 제시",
            llm_call_fn=llm_fn,
        )
        assert main_kw == "앱 개발 견적"
        assert "앱 개발 견적" in seeds
        assert "앱 개발 비용" in seeds
        assert len(seeds) == 3

    @pytest.mark.asyncio
    async def test_main_keyword_auto_inserted(self):
        """main_keyword가 seeds에 없으면 자동 추가."""
        from core.agents.researcher.stage1_hub import _llm_extract_seeds

        llm_fn = AsyncMock(return_value=json.dumps({
            "main_keyword": "ERP 외주",
            "seeds": ["ERP 외주 개발", "ERP 외주 비용"],
        }))

        main_kw, seeds = await _llm_extract_seeds(
            "ERP 외주 업체 선정 기준", "", "",
            llm_call_fn=llm_fn,
        )
        assert main_kw == "ERP 외주"
        assert seeds[0] == "ERP 외주"  # 선두에 추가

    @pytest.mark.asyncio
    async def test_empty_response_fallback(self):
        """LLM 빈 응답 → 빈 결과."""
        from core.agents.researcher.stage1_hub import _llm_extract_seeds

        main_kw, seeds = await _llm_extract_seeds(
            "질문", "", "",
            llm_call_fn=AsyncMock(return_value=""),
        )
        assert main_kw == ""
        assert seeds == []

    @pytest.mark.asyncio
    async def test_invalid_json_fallback(self):
        """잘못된 JSON → 빈 결과."""
        from core.agents.researcher.stage1_hub import _llm_extract_seeds

        main_kw, seeds = await _llm_extract_seeds(
            "질문", "", "",
            llm_call_fn=AsyncMock(return_value="not json"),
        )
        assert main_kw == ""
        assert seeds == []


# ── 고립 재그룹핑 + 우산 키워드 분리 테스트 ────────────────────────────


class TestRegroupOrphans:
    def test_fuzzy_token_overlap_substring(self):
        """부분 문자열 매칭 — 계약서 ⊃ 계약."""
        from core.agents.researcher.stage1_hub import _fuzzy_token_overlap

        assert _fuzzy_token_overlap({"계약서"}, {"계약"}) is True
        assert _fuzzy_token_overlap({"계약"}, {"계약서"}) is True

    def test_fuzzy_token_overlap_no_match(self):
        """겹침 없음."""
        from core.agents.researcher.stage1_hub import _fuzzy_token_overlap

        assert _fuzzy_token_overlap({"가격"}, {"후기"}) is False

    def test_fuzzy_token_overlap_short_tokens_ignored(self):
        """1글자 토큰은 무시."""
        from core.agents.researcher.stage1_hub import _fuzzy_token_overlap

        assert _fuzzy_token_overlap({"a"}, {"ab"}) is False

    def test_regroup_orphans_groups_similar(self):
        """계약서/계약 관련 고립이 함께 그룹핑."""
        from core.agents.researcher.stage1_hub import _regroup_orphans

        clusters: list[ClusterDraft] = []
        orphans = [
            "erp 외주 개발 업체 계약서 샘플",
            "erp 외주 계약 시 주의사항",
        ]
        main_kw = "ERP 외주 개발 업체"

        new_clusters, remaining = _regroup_orphans(clusters, orphans, main_kw)
        assert len(new_clusters) == 1  # 미니 클러스터 1개 생성
        assert len(remaining) == 0
        members = [kw for kw, _ in new_clusters[0].keywords]
        assert "erp 외주 개발 업체 계약서 샘플" in members
        assert "erp 외주 계약 시 주의사항" in members

    def test_regroup_orphans_merge_into_existing(self):
        """고유 토큰 겹침으로 기존 클러스터에 병합."""
        from core.agents.researcher.stage1_hub import _regroup_orphans

        clusters = [
            ClusterDraft(
                cluster_id="c000",
                keywords=[("erp 외주 가격", "google"), ("erp 외주 개발 비용", "google")],
                representative="erp 외주 가격",
            ),
        ]
        orphans = ["erp 외주 비용 비교"]  # "비용" 토큰이 c000과 겹침
        main_kw = "ERP 외주 개발 업체"

        new_clusters, remaining = _regroup_orphans(clusters, orphans, main_kw)
        assert len(remaining) == 0
        # c000에 병합되었어야 함
        members = [kw for kw, _ in new_clusters[0].keywords]
        assert "erp 외주 비용 비교" in members

    def test_regroup_orphans_truly_isolated(self):
        """겹침 없는 고립은 그대로 남음."""
        from core.agents.researcher.stage1_hub import _regroup_orphans

        clusters: list[ClusterDraft] = []
        orphans = ["erp 외주 xyz"]
        main_kw = "ERP 외주 개발 업체"

        new_clusters, remaining = _regroup_orphans(clusters, orphans, main_kw)
        assert len(new_clusters) == 0
        assert remaining == ["erp 외주 xyz"]

    def test_regroup_orphans_empty(self):
        """빈 고립 목록 → 변화 없음."""
        from core.agents.researcher.stage1_hub import _regroup_orphans

        clusters, remaining = _regroup_orphans([], [], "main")
        assert clusters == []
        assert remaining == []


class TestNoopToolFn:
    @pytest.mark.asyncio
    async def test_noop_returns_empty_json(self):
        """noop 도구가 빈 JSON 반환."""
        from core.agents.researcher.stage1_hub import _noop_tool_fn

        coro = _noop_tool_fn("any_keyword")
        result = await coro
        assert result == "{}"

    @pytest.mark.asyncio
    async def test_paa_not_collected_in_expansion(self):
        """확장 단계에서 google_paa_fn이 호출되지 않음 (research_unit에서만 호출)."""
        from core.agents.researcher.stage1_hub import stage1_hub_research

        seeds = _make_seed_questions(1)
        fns = _make_tool_fns()
        config = {"longtail": {"max_second_pass": 0}}

        p0, p1, p2, p3 = _hub_patches()
        with (
            p0, p1, p2, p3,
            patch(
                "core.agents.researcher.stage1_hub.run_research_unit",
                new_callable=AsyncMock,
                return_value=ResearchUnitOutput(),
            ),
        ):
            await stage1_hub_research(
                seeds,
                config=config,
                llm_call_fn=AsyncMock(return_value="[]"),
                **fns,
            )
        # google_paa_fn은 확장 단계에서 호출되지 않아야 함
        # (research_unit도 모킹했으므로 전체 0회)
        assert fns["google_paa_fn"].call_count == 0


class TestSeparateUmbrella:
    def test_subset_detected(self):
        """메인 키워드 부분집합이 우산으로 분리."""
        from core.agents.researcher.stage1_hub import _separate_umbrella

        reps = ["erp 외주 가격", "erp 외주 개발", "erp 외주"]
        main_kw = "ERP 외주 개발 업체"

        research, umbrella = _separate_umbrella(reps, main_kw)
        assert "erp 외주 가격" in research     # 비부분집합 (가격 ∉ main)
        assert "erp 외주 개발" in umbrella      # 부분집합
        assert "erp 외주" in umbrella           # 부분집합

    def test_exact_match_not_umbrella(self):
        """메인 키워드와 동일 토큰 → 우산 아님 (len < len 조건 불충족)."""
        from core.agents.researcher.stage1_hub import _separate_umbrella

        research, umbrella = _separate_umbrella(
            ["erp 외주 개발 업체"], "ERP 외주 개발 업체",
        )
        assert research == ["erp 외주 개발 업체"]
        assert umbrella == []

    def test_no_umbrella(self):
        """모두 비부분집합 → 우산 없음."""
        from core.agents.researcher.stage1_hub import _separate_umbrella

        research, umbrella = _separate_umbrella(
            ["erp 외주 가격", "erp 외주 후기"], "ERP 외주 개발 업체",
        )
        assert len(research) == 2
        assert umbrella == []

    def test_empty_reps(self):
        from core.agents.researcher.stage1_hub import _separate_umbrella

        research, umbrella = _separate_umbrella([], "ERP 외주")
        assert research == []
        assert umbrella == []


class TestUmbrellaResearchUnit:
    @pytest.mark.asyncio
    async def test_umbrella_calls_research_unit_separately(self):
        """우산 키워드가 별도 research_unit 호출로 수집된다."""
        from core.agents.researcher.stage1_hub import stage1_hub_research

        seeds = _make_seed_questions(1)
        fns = _make_tool_fns()
        config = {"longtail": {"max_second_pass": 0}}

        # 클러스터링이 "erp 외주"를 포함하는 대표를 만들도록 설정
        async def _clustering_with_umbrella(deduped, **kwargs):
            if not deduped:
                return [], []
            cd = ClusterDraft(
                cluster_id="c000",
                keywords=deduped[:3],
                representative=deduped[0][0],
            )
            return [cd], ["erp 외주"]  # "erp 외주" = orphan → 대표로 포함 → umbrella

        ru_calls: list[tuple] = []
        async def _track_ru(keywords, profile, **kwargs):
            ru_calls.append((keywords, profile))
            return ResearchUnitOutput()

        async def _mock_seeds(q, i, d, **kw):
            return "ERP 외주 개발 업체", ["ERP 외주 개발 업체", "ERP 외주 개발"]

        p0 = patch(
            "core.agents.researcher.stage1_hub._llm_extract_seeds",
            new_callable=AsyncMock,
            side_effect=_mock_seeds,
        )
        p1 = patch(
            "core.agents.researcher.stage1_hub._domain_filter",
            new_callable=AsyncMock,
            side_effect=lambda d, *a, **kw: d,
        )
        p2 = patch(
            "core.agents.researcher.stage1_hub._stage1d_llm_clustering",
            new_callable=AsyncMock,
            side_effect=_clustering_with_umbrella,
        )
        p3 = patch(
            "core.agents.researcher.stage1_hub._stage1e_llm_representative",
            new_callable=AsyncMock,
        )
        p4 = patch(
            "core.agents.researcher.stage1_hub.run_research_unit",
            new_callable=AsyncMock,
            side_effect=_track_ru,
        )

        with p0, p1, p2, p3, p4:
            result = await stage1_hub_research(
                seeds,
                config=config,
                llm_call_fn=AsyncMock(return_value="[]"),
                **fns,
            )

        # research_unit 2회 호출 (대표 + 우산)
        assert len(ru_calls) == 2
        # 두 번째 호출: 우산 프로파일 (FULL ∩ UMBRELLA)
        umb_prof = ru_calls[1][1]
        assert umb_prof.volumes is True
        assert umb_prof.serp_features is True
        assert umb_prof.paa is True
        assert umb_prof.content is False  # UMBRELLA 제한
        assert umb_prof.geo is False      # UMBRELLA 제한
        assert "erp 외주" in ru_calls[1][0]

        # 우산 키워드가 all_kws에 포함
        assert "erp 외주" in result[0].keywords


# ── 레거시 호환 테스트 ────────────────────────────────────────────


class TestLegacyCompat:
    @pytest.mark.asyncio
    async def test_legacy_stage2_validation(self):
        """_legacy.py 래퍼가 Stage2Output 반환."""
        from core.agents.researcher.research_unit import stage2_validation

        fns = _make_tool_fns()
        result = await stage2_validation(
            ["외주 개발"],
            safe_tool_call=fns["safe_tool_call"],
            google_search_fn=fns["google_search_fn"],
            naver_keyword_volume_fn=fns["naver_keyword_volume_fn"],
            google_keyword_trend_fn=fns["google_keyword_trend_fn"],
            naver_keyword_trend_fn=fns["naver_keyword_trend_fn"],
            naver_blog_search_fn=fns["naver_blog_search_fn"],
            web_fetch_fn=fns["web_fetch_fn"],
            naver_serp_features_fn=fns["naver_serp_features_fn"],
        )
        assert isinstance(result, Stage2Output)

    @pytest.mark.asyncio
    async def test_legacy_stage2_empty_reps(self):
        """빈 reps → 빈 Stage2Output."""
        from core.agents.researcher.research_unit import stage2_validation

        fns = _make_tool_fns()
        result = await stage2_validation(
            [],
            safe_tool_call=fns["safe_tool_call"],
            google_search_fn=fns["google_search_fn"],
            naver_keyword_volume_fn=fns["naver_keyword_volume_fn"],
            google_keyword_trend_fn=fns["google_keyword_trend_fn"],
            naver_keyword_trend_fn=fns["naver_keyword_trend_fn"],
            naver_blog_search_fn=fns["naver_blog_search_fn"],
            web_fetch_fn=fns["web_fetch_fn"],
            naver_serp_features_fn=fns["naver_serp_features_fn"],
        )
        assert isinstance(result, Stage2Output)
        assert result.volumes == {}

    @pytest.mark.asyncio
    async def test_legacy_stage3_geo(self):
        """_legacy.py 래퍼가 Stage3Output 반환."""
        from core.agents.researcher.research_unit import stage3_geo

        fns = _make_tool_fns()
        result = await stage3_geo(
            ["외주 개발"],
            safe_tool_call=fns["safe_tool_call"],
            ai_search_fn=fns["ai_search_fn"],
            perplexity_search_fn=fns["perplexity_search_fn"],
            geo_claude_fn=fns["geo_claude_fn"],
            geo_gemini_fn=fns["geo_gemini_fn"],
        )
        assert isinstance(result, Stage3Output)

    @pytest.mark.asyncio
    async def test_legacy_stage3_geo_empty(self):
        """빈 reps → 빈 Stage3Output."""
        from core.agents.researcher.research_unit import stage3_geo

        fns = _make_tool_fns()
        result = await stage3_geo(
            [],
            safe_tool_call=fns["safe_tool_call"],
            ai_search_fn=fns["ai_search_fn"],
            perplexity_search_fn=fns["perplexity_search_fn"],
            geo_claude_fn=fns["geo_claude_fn"],
            geo_gemini_fn=fns["geo_gemini_fn"],
        )
        assert isinstance(result, Stage3Output)
        assert result.citations == {}

    @pytest.mark.asyncio
    async def test_legacy_import_from_init(self):
        """__init__.py에서 정상 import 확인."""
        from core.agents.researcher.research_unit import (
            PROFILE_DEMAND,
            PROFILE_FULL,
            run_research_unit,
            stage2_validation,
            stage3_geo,
        )
        assert callable(run_research_unit)
        assert callable(stage2_validation)
        assert callable(stage3_geo)
        assert PROFILE_FULL.geo is True
        assert PROFILE_DEMAND.geo is False


# ── 파서 seed_questions 테스트 ────────────────────────────────────


class TestParserSeedQuestions:
    def test_parse_input_seed_questions_created(self):
        """구조화 입력 → seed_questions 자동 생성."""
        from core.agents.researcher.parser import parse_input

        text = """질문 의도 : 비교 판단
질문 형태
ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?
앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?
콘텐츠 방향성 : 판단 기준 제시"""

        parsed = parse_input(text)
        assert len(parsed.seed_questions) == 2
        assert parsed.seed_questions[0].seed_id == "sq001"
        assert parsed.seed_questions[1].seed_id == "sq002"
        assert "ERP" in parsed.seed_questions[0].question
        assert "앱 개발" in parsed.seed_questions[1].question

    def test_parse_input_seed_id_format(self):
        """seed_id 형식 "sq001" 확인."""
        from core.agents.researcher.parser import parse_input

        text = """질문 의도 : 탐색
질문 형태
외주 개발이란 무엇인가요?
콘텐츠 방향성 : 정의 설명"""

        parsed = parse_input(text)
        assert len(parsed.seed_questions) == 1
        assert parsed.seed_questions[0].seed_id == "sq001"

    def test_parse_input_intent_direction_propagated(self):
        """intent/direction이 SeedQuestion에 전파."""
        from core.agents.researcher.parser import parse_input

        text = """질문 의도 : 비교 판단
질문 형태
앱 개발 비용은 얼마인가요?
콘텐츠 방향성 : 비용 안내"""

        parsed = parse_input(text)
        sq = parsed.seed_questions[0]
        assert sq.intent == ["비교 판단"]
        assert sq.content_direction == ["비용 안내"]

    def test_parse_input_no_struct_no_seed_questions(self):
        """비구조화 입력 → seed_questions 비어있음."""
        from core.agents.researcher.parser import parse_input

        parsed = parse_input("앱 개발, 비용 문의")
        assert parsed.seed_questions == []

    def test_parse_input_three_questions(self):
        """질문 3개 → seed_questions 3개."""
        from core.agents.researcher.parser import parse_input

        text = """질문 의도 : 비교 판단
질문 형태
ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?
앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?
외주 개발 프로젝트를 진행할 때 가장 자주 발생하는 문제는 무엇이며, 어떻게 해결할 수 있나요?
콘텐츠 방향성 : 판단 기준 제시"""

        parsed = parse_input(text)
        assert len(parsed.seed_questions) == 3
        assert parsed.seed_questions[2].seed_id == "sq003"


# ── 스키마 테스트 ─────────────────────────────────────────────────


class TestSchemas:
    def test_research_profile_defaults(self):
        """ResearchProfile 기본값."""
        p = ResearchProfile()
        assert p.volumes is True
        assert p.geo is False

    def test_profile_full(self):
        assert PROFILE_FULL.geo is True
        assert PROFILE_FULL.volumes is True

    def test_profile_demand(self):
        assert PROFILE_DEMAND.geo is False
        assert PROFILE_DEMAND.related_keywords is False
        assert PROFILE_DEMAND.paa is False
        assert PROFILE_DEMAND.content is True

    def test_profile_umbrella(self):
        assert PROFILE_UMBRELLA.volumes is True
        assert PROFILE_UMBRELLA.serp_features is True
        assert PROFILE_UMBRELLA.paa is True
        assert PROFILE_UMBRELLA.content is False
        assert PROFILE_UMBRELLA.related_keywords is False
        assert PROFILE_UMBRELLA.geo is False

    def test_research_unit_output_defaults(self):
        out = ResearchUnitOutput()
        assert out.volumes == {}
        assert out.geo_citations == {}

    def test_hub_research_data_defaults(self):
        hub = HubResearchData()
        assert hub.seed_id == ""
        assert hub.keywords == []
        assert isinstance(hub.research, ResearchUnitOutput)

    def test_research_result_hub_research_field(self):
        """ResearchResult에 hub_research 필드 존재."""
        from core.schemas import ResearchResult

        r = ResearchResult(
            run_date="2026-03-05",
            main_keyword="test",
            entry_moment="test",
        )
        assert r.hub_research == []


# ── 개별 모듈 테스트 ──────────────────────────────────────────────


class TestVolumeModule:
    @pytest.mark.asyncio
    async def test_collect_volumes_empty(self):
        from core.agents.researcher.research_unit.volume import collect_volumes

        fns = _make_tool_fns()
        result = await collect_volumes(
            [],
            safe_tool_call=fns["safe_tool_call"],
            naver_keyword_volume_fn=fns["naver_keyword_volume_fn"],
            google_keyword_trend_fn=fns["google_keyword_trend_fn"],
            naver_keyword_trend_fn=fns["naver_keyword_trend_fn"],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_collect_volumes_prefill(self):
        """기존 볼륨 프리필 확인."""
        from core.agents.researcher.research_unit.volume import collect_volumes

        fns = _make_tool_fns()
        result = await collect_volumes(
            ["외주 개발"],
            existing_volumes={"외주 개발": 500},
            existing_volumes_pc={"외주 개발": 300},
            existing_volumes_mobile={"외주 개발": 200},
            safe_tool_call=fns["safe_tool_call"],
            naver_keyword_volume_fn=fns["naver_keyword_volume_fn"],
            google_keyword_trend_fn=fns["google_keyword_trend_fn"],
            naver_keyword_trend_fn=fns["naver_keyword_trend_fn"],
        )
        assert result["외주 개발"]["naver_volume"] == 500


class TestContentModule:
    @pytest.mark.asyncio
    async def test_prefetch_serp(self):
        from core.agents.researcher.research_unit.content import prefetch_serp

        fns = _make_tool_fns()
        cache = await prefetch_serp(
            ["test"], fns["safe_tool_call"], fns["google_search_fn"],
        )
        assert "test" in cache

    def test_extract_h2_topics_empty(self):
        from core.agents.researcher.research_unit.content import extract_h2_topics

        result = extract_h2_topics({})
        assert result == {}

    def test_extract_h2_topics_filters_noise(self):
        from core.agents.researcher.research_unit.content import extract_h2_topics

        metas = {
            "kw": [
                {"h2_structure": ["좋은 제목", "댓글 목록", "관련 글"]},
            ]
        }
        result = extract_h2_topics(metas)
        assert "좋은 제목" in result["kw"]
        assert "댓글 목록" not in result["kw"]


class TestDiscoveryModule:
    @pytest.mark.asyncio
    async def test_collect_related_empty(self):
        from core.agents.researcher.research_unit.discovery import collect_related

        fns = _make_tool_fns()
        result = await collect_related(
            [],
            safe_tool_call=fns["safe_tool_call"],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_collect_paa_empty(self):
        from core.agents.researcher.research_unit.discovery import collect_paa

        fns = _make_tool_fns()
        result = await collect_paa(
            [],
            safe_tool_call=fns["safe_tool_call"],
        )
        assert result == {}


class TestGeoModule:
    @pytest.mark.asyncio
    async def test_collect_geo_empty(self):
        from core.agents.researcher.research_unit.geo import collect_geo

        fns = _make_tool_fns()
        result = await collect_geo(
            [],
            safe_tool_call=fns["safe_tool_call"],
            ai_search_fn=fns["ai_search_fn"],
            perplexity_search_fn=fns["perplexity_search_fn"],
            geo_claude_fn=fns["geo_claude_fn"],
            geo_gemini_fn=fns["geo_gemini_fn"],
        )
        assert result == {}


class TestResearchUnitSnapshot:
    @pytest.mark.asyncio
    async def test_snapshots_created(self, tmp_path):
        """snapshot_dir 지정 시 3개 스냅샷 파일 생성."""
        from core.agents.researcher.research_unit.runner import run_research_unit

        fns = _make_tool_fns()
        await run_research_unit(
            ["외주 개발"],
            PROFILE_FULL,
            **fns,
            snapshot_dir=str(tmp_path),
            run_date="2026-03-05",
        )
        files = sorted(p.name for p in tmp_path.glob("*.json"))
        assert "2026-03-05_ru_1_serp_cache.json" in files
        assert "2026-03-05_ru_2_parallel.json" in files
        assert "2026-03-05_ru_3_final.json" in files

    @pytest.mark.asyncio
    async def test_no_snapshot_without_dir(self, tmp_path):
        """snapshot_dir 미지정 시 파일 없음."""
        from core.agents.researcher.research_unit.runner import run_research_unit

        fns = _make_tool_fns()
        await run_research_unit(
            ["외주 개발"],
            PROFILE_FULL,
            **fns,
        )
        # tmp_path에 아무것도 안 생겨야 함 (snapshot_dir 미전달)
        assert list(tmp_path.glob("*.json")) == []

    @pytest.mark.asyncio
    async def test_snapshot_content_valid_json(self, tmp_path):
        """스냅샷 파일이 유효한 JSON."""
        import json as _json
        from core.agents.researcher.research_unit.runner import run_research_unit

        fns = _make_tool_fns()
        await run_research_unit(
            ["테스트"],
            PROFILE_DEMAND,
            **fns,
            snapshot_dir=str(tmp_path),
            run_date="2026-01-01",
        )
        for path in tmp_path.glob("*.json"):
            data = _json.loads(path.read_text())
            assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_no_serp_snapshot_when_content_off(self, tmp_path):
        """content=False, serp_features=False → serp_cache 스냅샷 없음."""
        from core.agents.researcher.research_unit.runner import run_research_unit

        fns = _make_tool_fns()
        await run_research_unit(
            ["테스트"],
            ResearchProfile(
                volumes=True, related_keywords=False, paa=False,
                content=False, serp_features=False, geo=False,
            ),
            **fns,
            snapshot_dir=str(tmp_path),
            run_date="2026-01-01",
        )
        files = [p.name for p in tmp_path.glob("*.json")]
        assert "2026-01-01_ru_1_serp_cache.json" not in files
        assert "2026-01-01_ru_2_parallel.json" in files
        assert "2026-01-01_ru_3_final.json" in files


# ── parse_json_input 테스트 ─────────────────────────────────────────


class TestParseJsonInput:
    def test_parse_json_input_basic(self):
        """3개 질문 → SeedQuestion 3개, 질문별 intent/direction."""
        from core.agents.researcher.parser import parse_json_input

        data = {
            "questions": [
                {"question": "ERP 외주 개발 업체를 고를 때 기준은?", "intent": "비교 판단", "direction": "판단 기준 제시"},
                {"question": "앱 개발 견적이 다른 이유는?", "intent": "비교 판단", "direction": "판단 기준 제시"},
                {"question": "외주 개발 문제와 해결은?", "intent": "비교 판단", "direction": "판단 기준 제시"},
            ],
            "target_month": "2026-04",
        }
        parsed = parse_json_input(data)
        assert len(parsed.seed_questions) == 3
        assert parsed.seed_questions[0].seed_id == "sq001"
        assert parsed.seed_questions[1].seed_id == "sq002"
        assert parsed.seed_questions[2].seed_id == "sq003"
        assert parsed.intent == "비교 판단"
        assert parsed.direction == "판단 기준 제시"

    def test_parse_json_input_mixed_intents(self):
        """서로 다른 intent → 각 SeedQuestion 반영."""
        from core.agents.researcher.parser import parse_json_input

        data = {
            "questions": [
                {"question": "외주란 무엇인가요?", "intent": "정보 탐색", "direction": "문제 인식 확산"},
                {"question": "업체 비교 기준은?", "intent": "비교 판단", "direction": "판단 기준 제시"},
            ],
        }
        parsed = parse_json_input(data)
        assert parsed.seed_questions[0].intent == ["정보 탐색"]
        assert parsed.seed_questions[0].content_direction == ["문제 인식 확산"]
        assert parsed.seed_questions[1].intent == ["비교 판단"]
        assert parsed.seed_questions[1].content_direction == ["판단 기준 제시"]
        # 레거시 호환: 첫 번째 질문의 값
        assert parsed.intent == "정보 탐색"
        assert parsed.direction == "문제 인식 확산"

    def test_parse_json_input_empty(self):
        """빈 questions → 빈 결과."""
        from core.agents.researcher.parser import parse_json_input

        parsed = parse_json_input({"questions": []})
        assert parsed.main_keyword == ""
        assert parsed.seed_questions == []

    def test_parse_json_input_missing_fields(self):
        """direction 누락 → 빈 리스트 기본값."""
        from core.agents.researcher.parser import parse_json_input

        data = {
            "questions": [
                {"question": "외주 개발 비용은?", "intent": "비교 판단"},
            ],
        }
        parsed = parse_json_input(data)
        assert len(parsed.seed_questions) == 1
        assert parsed.seed_questions[0].content_direction == []
        assert parsed.seed_questions[0].intent == ["비교 판단"]


# ── run_json + 시드별 저장 테스트 ────────────────────────────────────


class TestRunJson:
    @pytest.mark.asyncio
    async def test_run_json_calls_hub_research(self):
        """run_json() → stage1_hub_research 호출 확인."""
        from core.agents.researcher.agent import ResearcherAgent

        data = {
            "questions": [
                {"question": "외주 개발 기준은?", "intent": "비교 판단", "direction": "판단 기준 제시"},
            ],
        }
        mock_hub = [HubResearchData(seed_id="sq001", seed_question="외주 개발 기준은?", keywords=["외주"])]

        with patch(
            "core.agents.researcher.agent.stage1_hub_research",
            new_callable=AsyncMock,
            return_value=mock_hub,
        ) as mock_fn, patch(
            "core.agents.researcher.agent.save_hub_research_per_seed",
        ), patch(
            "core.agents.researcher.agent.save_manifest",
        ), patch(
            "core.agents.researcher.agent.save_snapshot",
        ):
            agent = ResearcherAgent()
            result = await agent.run_json(data, output_dir="/tmp/test_run_json")
            assert mock_fn.call_count == 1
            assert len(result) == 1
            assert result[0].seed_id == "sq001"

    @pytest.mark.asyncio
    async def test_run_json_saves_per_seed(self, tmp_path):
        """시드별 파일 + manifest.json 생성 확인."""
        from core.agents.researcher.agent import ResearcherAgent

        data = {
            "questions": [
                {"question": "Q1?", "intent": "비교 판단", "direction": "판단 기준 제시"},
                {"question": "Q2?", "intent": "정보 탐색", "direction": "문제 인식 확산"},
            ],
        }
        hub_list = [
            HubResearchData(seed_id="sq001", seed_question="Q1?", keywords=["kw1"]),
            HubResearchData(seed_id="sq002", seed_question="Q2?", keywords=["kw2"]),
        ]

        with patch(
            "core.agents.researcher.agent.stage1_hub_research",
            new_callable=AsyncMock,
            return_value=hub_list,
        ), patch(
            "core.agents.researcher.agent.save_snapshot",
        ):
            agent = ResearcherAgent()
            result = await agent.run_json(data, output_dir=str(tmp_path))
            assert len(result) == 2

        # 파일 확인
        assert (tmp_path / "sq001_hub_research.json").exists()
        assert (tmp_path / "sq002_hub_research.json").exists()
        assert (tmp_path / "manifest.json").exists()

        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert manifest["seed_count"] == 2
        assert manifest["seeds"][0]["seed_id"] == "sq001"
        assert manifest["seeds"][1]["seed_id"] == "sq002"


# ── save/load manifest 단위 테스트 ───────────────────────────────────


class TestManifest:
    def test_save_hub_research_per_seed(self, tmp_path):
        """파일 내용 직렬화 정확성."""
        from core.agents.researcher.snapshot import save_hub_research_per_seed

        hub_list = [
            HubResearchData(seed_id="sq001", seed_question="Q1?", keywords=["k1", "k2"]),
        ]
        paths = save_hub_research_per_seed(hub_list, "2026-03-05", str(tmp_path))
        assert len(paths) == 1
        assert paths[0].name == "sq001_hub_research.json"

        data = json.loads(paths[0].read_text())
        assert data["seed_id"] == "sq001"
        assert data["keywords"] == ["k1", "k2"]

    def test_save_manifest_structure(self, tmp_path):
        """manifest 구조 (seed_count, seeds 배열)."""
        from core.agents.researcher.snapshot import save_manifest
        from core.schemas import ParsedInput

        hub_list = [
            HubResearchData(seed_id="sq001", seed_question="Q1?", keywords=["k1"]),
            HubResearchData(seed_id="sq002", seed_question="Q2?", keywords=["k2", "k3"]),
        ]
        parsed = ParsedInput(
            main_keyword="test",
            entry_moment="비교 판단",
            seed_questions=[
                SeedQuestion(seed_id="sq001", question="Q1?", intent=["비교 판단"], content_direction=["판단 기준 제시"]),
                SeedQuestion(seed_id="sq002", question="Q2?", intent=["정보 탐색"], content_direction=["문제 인식 확산"]),
            ],
        )
        path = save_manifest(hub_list, "2026-03-05", str(tmp_path), parsed=parsed)
        assert path.name == "manifest.json"

        manifest = json.loads(path.read_text())
        assert manifest["run_date"] == "2026-03-05"
        assert manifest["seed_count"] == 2
        assert manifest["seeds"][0]["intent"] == "비교 판단"
        assert manifest["seeds"][0]["direction"] == "판단 기준 제시"
        assert manifest["seeds"][1]["intent"] == "정보 탐색"
        assert manifest["seeds"][1]["keyword_count"] == 2

    def test_load_manifest(self, tmp_path):
        """저장→로드 왕복."""
        from core.agents.researcher.snapshot import load_manifest, save_manifest

        hub_list = [HubResearchData(seed_id="sq001", seed_question="Q?", keywords=["k"])]
        save_manifest(hub_list, "2026-03-05", str(tmp_path))
        loaded = load_manifest(str(tmp_path))
        assert loaded["seed_count"] == 1
        assert loaded["seeds"][0]["seed_id"] == "sq001"


# ── 슬랙 파서 테스트 ────────────────────────────────────────────────


class TestSlackParseJson:
    def test_slack_parse_json(self):
        """슬랙 JSON 파서 검증."""
        from interfaces.slack.parser import parse_json, PipelineParams

        data = {
            "questions": [
                {"question": "외주 비용은?", "intent": "비교 판단", "direction": "판단 기준 제시"},
                {"question": "앱 개발이란?", "intent": "정보 탐색", "direction": "문제 인식 확산"},
            ],
            "target_month": "2026-04",
        }
        result = parse_json(data)
        assert isinstance(result, PipelineParams)
        assert result.intent == "비교 판단"
        assert result.content_direction == "판단 기준 제시"
        assert len(result.questions) == 2
        assert len(result.question_tags) == 2
        assert result.question_tags[0]["intent"] == "비교 판단"
        assert result.question_tags[1]["intent"] == "정보 탐색"
        assert result.target_month == "2026-04"


class TestSerpModule:
    def test_google_serp_features_empty_cache(self):
        from core.agents.researcher.research_unit.serp import collect_google_serp_features

        result = collect_google_serp_features(["kw"], {})
        assert "kw" in result
        assert result["kw"]["ai_overview"] is False

    @pytest.mark.asyncio
    async def test_naver_serp_features(self):
        from core.agents.researcher.research_unit.serp import collect_naver_serp_features

        fns = _make_tool_fns()
        result = await collect_naver_serp_features(
            ["kw"],
            safe_tool_call=fns["safe_tool_call"],
            naver_serp_features_fn=fns["naver_serp_features_fn"],
        )
        assert "kw" in result
