"""플래너 에이전트 테스트 — Phase 0 ~ Phase 6 (Stage 5, Archive, Stage 7 포함).

현재 검증 범위:
  - PlannerAgent 초기화
  - _validate_input 유효/무효 케이스
  - _load_guide 파일 로드
  - run(stop_at_stage=0) 입력 검증 통과
  - run(stop_at_stage=1) Stage 1 실행 → DerivedQuestion 반환
  - run(stop_at_stage=2) Stage 2 실행 → 퍼널 태깅 완료
  - _llm_call 디스패치: claude-* → Anthropic, gpt-* → OpenAI
  - _strip_json_fence 유틸리티
  - _stage1_sort: 정상 매핑, unassigned 폴백, 빈 클러스터, question_id 배정
  - _stage2_funnel: 퍼널 배정, unassigned 제외, 태그 누락 처리, 빈 목록 처리
  - _stage4_priority: 점수 계산, 선발/대기 배정, LLM 근거 생성, fallback
  - _stage5_balance: 분포 계산, 편중 경고, no-swap, 아카이브 연동, 발행일 배정
  - archive: save_plan, load_previous_funnel, _prev_month
  - _stage7_calendar: publish_date 기반 단순 조립, 필드 검증
  - _stage7_document: Markdown 기획 문서 3섹션, 발행일 포함, 제목·H2 포함
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core.agents.planner.archive as archive_mod
from core.agents.planner.agent import (
    PlannerAgent,
    _compute_keyword_overlap,
    _compute_urgency,
    _strip_json_fence,
)
from core.schemas import (
    Cluster,
    ClusterKeyword,
    ContentPiece,
    ContentPlan,
    DerivedQuestion,
    DuplicateResult,
    FunnelDistribution,
    PlannerInput,
    PublishedContent,
    ResearchResult,
)


# ── 픽스처 ─────────────────────────────────────────────────────────


def _make_cluster(cluster_id: str, keyword: str, paa: list[str] | None = None) -> Cluster:
    """테스트용 최소 Cluster."""
    return Cluster(
        cluster_id=cluster_id,
        representative_keyword=keyword,
        paa_questions=paa or [],
        keywords=[ClusterKeyword(keyword=keyword)],
    )


def _make_research_result(clusters: list[Cluster] | None = None) -> ResearchResult:
    """테스트용 최소 ResearchResult."""
    return ResearchResult(
        run_date="2026-02-27",
        main_keyword="ERP 외주 개발",
        entry_moment="비교 판단",
        intent="비교 판단",
        source_questions=["ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?"],
        content_direction="판단 기준 제시",
        clusters=clusters or [],
    )


def _make_planner_input(**overrides) -> PlannerInput:
    """테스트용 기본 PlannerInput."""
    base = dict(
        intent=["비교 판단"],
        questions=["ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?"],
        content_direction=["판단 기준 제시"],
        research_result=_make_research_result(),
        target_month="2026-03",
        client_name="wishket",
    )
    base.update(overrides)
    return PlannerInput(**base)


@pytest.fixture
def agent() -> PlannerAgent:
    return PlannerAgent(client_name="wishket")


# ── 스키마 단위 테스트 ─────────────────────────────────────────────


class TestDerivedQuestion:
    def test_defaults(self) -> None:
        dq = DerivedQuestion(
            question="ERP 외주 개발 업체 선택 기준은?",
            category="ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?",
        )
        assert dq.funnel == "unclassified"
        assert dq.is_selected is False
        assert dq.exploration_order == 0
        assert dq.question_id == ""  # 기본값 빈 문자열
        assert dq.funnel_journey_reasoning == ""
        assert dq.funnel_searcher_state == ""
        assert dq.funnel_judgment_basis == ""
        assert dq.funnel_signals_used == ""

    def test_question_id_field(self) -> None:
        dq = DerivedQuestion(question="q", category="cat", question_id="q001")
        assert dq.question_id == "q001"

    def test_funnel_assignment(self) -> None:
        dq = DerivedQuestion(
            question="q",
            category="cat",
            funnel="consideration",
        )
        assert dq.funnel == "consideration"


class TestFunnelDistribution:
    def test_total_computed(self) -> None:
        dist = FunnelDistribution(awareness=3, consideration=5, conversion=2)
        assert dist.total == 10

    def test_default_zero(self) -> None:
        dist = FunnelDistribution()
        assert dist.total == 0


class TestContentPlan:
    def test_minimum_fields(self) -> None:
        plan = ContentPlan(
            run_date="2026-02-27",
            target_month="2026-03",
            client_name="wishket",
        )
        assert plan.content_pieces == []
        assert plan.funnel_distribution.total == 0


# ── PlannerAgent 초기화 ────────────────────────────────────────────


class TestPlannerAgentInit:
    def test_default_init(self, agent: PlannerAgent) -> None:
        assert agent._client_name == "wishket"
        assert agent._model_main == "claude-sonnet-4-6"
        assert agent._model_mini == "gpt-4.1-mini"

    def test_valid_intents_loaded(self, agent: PlannerAgent) -> None:
        assert "비교 판단" in agent._valid_intents
        assert "정보 탐색" in agent._valid_intents

    def test_valid_directions_loaded(self, agent: PlannerAgent) -> None:
        assert "판단 기준 제시" in agent._valid_directions


# ── _validate_input ────────────────────────────────────────────────


class TestValidateInput:
    def test_valid_input(self, agent: PlannerAgent) -> None:
        pi = _make_planner_input()
        agent._validate_input(pi)  # 예외 없어야 함

    def test_empty_intent(self, agent: PlannerAgent) -> None:
        pi = _make_planner_input(intent=[])
        with pytest.raises(ValueError, match="질문 의도"):
            agent._validate_input(pi)

    def test_invalid_intent(self, agent: PlannerAgent) -> None:
        pi = _make_planner_input(intent=["잘못된의도"])
        with pytest.raises(ValueError, match="유효하지 않은 질문 의도"):
            agent._validate_input(pi)

    def test_empty_questions(self, agent: PlannerAgent) -> None:
        pi = _make_planner_input(questions=[])
        with pytest.raises(ValueError, match="질문을 최소"):
            agent._validate_input(pi)

    def test_too_short_question(self, agent: PlannerAgent) -> None:
        pi = _make_planner_input(questions=["ERP"])
        with pytest.raises(ValueError, match="키워드가 아닌"):
            agent._validate_input(pi)

    def test_empty_direction(self, agent: PlannerAgent) -> None:
        pi = _make_planner_input(content_direction=[])
        with pytest.raises(ValueError, match="콘텐츠 방향성"):
            agent._validate_input(pi)

    def test_invalid_direction(self, agent: PlannerAgent) -> None:
        pi = _make_planner_input(content_direction=["잘못된방향"])
        with pytest.raises(ValueError, match="유효하지 않은 콘텐츠 방향성"):
            agent._validate_input(pi)


# ── _load_guide ────────────────────────────────────────────────────


class TestLoadGuide:
    def test_load_existing_guide(self, agent: PlannerAgent) -> None:
        content = agent._load_guide("funnel_criteria.md")
        assert len(content) > 0

    def test_missing_guide_returns_empty(self, agent: PlannerAgent) -> None:
        content = agent._load_guide("nonexistent_guide.md")
        assert content == ""


# ── _find_latest_snapshot / _load_derived_from_snapshot ───────────


class TestSnapshotHelpers:
    def test_find_latest_snapshot_returns_newest(self, tmp_path) -> None:
        """파일명 내림차순에서 마지막(=최신)을 반환한다."""
        (tmp_path / "2026-02-27_100000_stage1.json").write_text("{}", encoding="utf-8")
        (tmp_path / "2026-02-27_143000_stage1.json").write_text("{}", encoding="utf-8")
        (tmp_path / "2026-02-28_090000_stage1.json").write_text("{}", encoding="utf-8")

        result = PlannerAgent._find_latest_snapshot("stage1", str(tmp_path))
        assert result is not None
        assert result.name == "2026-02-28_090000_stage1.json"

    def test_find_latest_snapshot_empty_dir(self, tmp_path) -> None:
        result = PlannerAgent._find_latest_snapshot("stage1", str(tmp_path))
        assert result is None

    def test_find_latest_snapshot_no_dir(self, tmp_path) -> None:
        result = PlannerAgent._find_latest_snapshot("stage1", str(tmp_path / "nonexistent"))
        assert result is None

    def test_find_latest_snapshot_ignores_other_names(self, tmp_path) -> None:
        """다른 단계 이름의 파일은 포함하지 않는다."""
        (tmp_path / "2026-02-27_100000_stage2.json").write_text("{}", encoding="utf-8")
        result = PlannerAgent._find_latest_snapshot("stage1", str(tmp_path))
        assert result is None

    def test_load_derived_from_snapshot(self, tmp_path, agent: PlannerAgent) -> None:
        """스냅샷 파일에서 DerivedQuestion 목록을 복원한다."""
        snap_data = {
            "stage": 1,
            "derived_questions": [{
                "question_id": "q001",
                "question": "ERP 외주 개발이란?",
                "category": _CAT_ERP,
                "source_cluster_id": "c000",
                "mapping_rationale": "테스트",
                "exploration_order": 0,
                "funnel": "unclassified",
                "funnel_journey_reasoning": "",
                "funnel_searcher_state": "",
                "funnel_judgment_basis": "",
                "funnel_signals_used": "",
                "duplicate_result": None,
                "priority_score": 0.0,
                "is_selected": False,
                "is_waitlist": False,
                "selection_rationale": "",
            }]
        }
        path = tmp_path / "2026-02-27_143000_stage1.json"
        path.write_text(json.dumps(snap_data, ensure_ascii=False), encoding="utf-8")

        result = agent._load_derived_from_snapshot(1, str(tmp_path))
        assert len(result) == 1
        assert result[0].question_id == "q001"
        assert result[0].question == "ERP 외주 개발이란?"

    def test_load_derived_from_snapshot_not_found(
        self, tmp_path, agent: PlannerAgent
    ) -> None:
        """스냅샷이 없으면 FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="스냅샷 없음"):
            agent._load_derived_from_snapshot(1, str(tmp_path))


# ── run (stop_at_stage) ────────────────────────────────────────────


@pytest.mark.asyncio
class TestRunStage:
    async def test_stage0_returns_empty_dict(self, agent: PlannerAgent) -> None:
        pi = _make_planner_input()
        result = await agent.run(pi, stop_at_stage=0)
        assert result == {}

    async def test_run_stage2_returns_funnel_tagged(self, agent: PlannerAgent) -> None:
        """run(stop_at_stage=2)가 funnel 태깅된 derived_questions를 반환한다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_planner_input(
            research_result=_make_research_result(clusters=clusters),
        )
        sort_response = json.dumps({
            "mappings": [{
                "cluster_id": "c000",
                "category": pi.questions[0],
                "derived_question": "ERP 외주 개발이란?",
                "mapping_reason": "naver_top_titles 1개 중 1개에서 ERP 외주 관련 확인.",
            }]
        }, ensure_ascii=False)
        validate_response = json.dumps({
            "validations": [{"cluster_id": "c000", "verdict": "ok"}]
        }, ensure_ascii=False)
        funnel_response = json.dumps({
            "funnel_tags": [{"question_id": "q001", "funnel": "awareness", "funnel_rationale": {
                "journey_reasoning": "이 질문은 ERP 외주 개발이란 무엇인지를 묻는다 → 검색자는 아직 외주 개발이라는 카테고리 자체를 탐색하는 중이다 → 위시켓 프로젝트 등록까지 거리가 멀다 → awareness",
                "searcher_state": "외주 개발 카테고리를 탐색하기 전 단계",
                "judgment_basis": "1순위 추론",
                "signals_used": "없음",
            }}]
        }, ensure_ascii=False)
        with patch.object(
            agent, "_llm_call",
            new=AsyncMock(side_effect=[sort_response, validate_response, funnel_response]),
        ):
            result = await agent.run(pi, stop_at_stage=2)

        assert result["stage"] == 2
        dq = result["derived_questions"][0]
        assert dq["question_id"] == "q001"
        assert dq["funnel"] == "awareness"
        assert dq["funnel_journey_reasoning"] != ""
        assert dq["funnel_searcher_state"] != ""
        assert dq["funnel_judgment_basis"] == "1순위 추론"
        assert dq["funnel_signals_used"] == "없음"

    async def test_start_at_stage2_loads_snapshot_and_runs(
        self, tmp_path, agent: PlannerAgent
    ) -> None:
        """start_at_stage=2: stage1 최신 스냅샷을 로드하고 stage2만 실행한다."""
        snap_data = {
            "stage": 1,
            "derived_questions": [{
                "question_id": "q001",
                "question": "ERP 외주 개발이란?",
                "category": _CAT_ERP,
                "source_cluster_id": "c000",
                "mapping_rationale": "",
                "exploration_order": 0,
                "funnel": "unclassified",
                "funnel_journey_reasoning": "",
                "funnel_searcher_state": "",
                "funnel_judgment_basis": "",
                "funnel_signals_used": "",
                "duplicate_result": None,
                "priority_score": 0.0,
                "is_selected": False,
                "is_waitlist": False,
                "selection_rationale": "",
            }]
        }
        (tmp_path / "2026-02-27_120000_stage1.json").write_text(
            json.dumps(snap_data, ensure_ascii=False), encoding="utf-8"
        )
        pi = _make_stage1_input([_make_cluster("c000", "ERP 외주")])
        funnel_response = json.dumps({
            "funnel_tags": [{"question_id": "q001", "funnel": "awareness", "funnel_rationale": {
                "journey_reasoning": "이 질문은 개념을 묻는다 → awareness",
                "searcher_state": "개념 탐색 단계",
                "judgment_basis": "1순위 추론",
                "signals_used": "없음",
            }}]
        }, ensure_ascii=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=funnel_response)):
            result = await agent.run(
                pi, stop_at_stage=2, start_at_stage=2, snapshot_dir=str(tmp_path)
            )

        # stage1 LLM 호출 없이 stage2만 실행됨 (LLM 1회 = funnel만)
        assert result["stage"] == 2
        assert result["derived_questions"][0]["funnel"] == "awareness"

    async def test_stop_before_start_raises(self, agent: PlannerAgent) -> None:
        """stop_at_stage < start_at_stage이면 ValueError."""
        pi = _make_planner_input()
        with pytest.raises(ValueError, match="stop_at_stage"):
            await agent.run(pi, stop_at_stage=1, start_at_stage=2)

    async def test_start_at_stage_snapshot_missing_raises(
        self, tmp_path, agent: PlannerAgent
    ) -> None:
        """start_at_stage=2인데 stage1 스냅샷이 없으면 FileNotFoundError."""
        pi = _make_stage1_input([_make_cluster("c000", "ERP 외주")])
        with pytest.raises(FileNotFoundError, match="스냅샷 없음"):
            await agent.run(pi, stop_at_stage=2, start_at_stage=2, snapshot_dir=str(tmp_path))

    async def test_invalid_input_raises_before_stage(self, agent: PlannerAgent) -> None:
        pi = _make_planner_input(intent=[])
        with pytest.raises(ValueError):
            await agent.run(pi, stop_at_stage=0)


# ── _llm_call 디스패치 ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestLlmCallDispatch:
    async def test_claude_model_calls_anthropic(self, agent: PlannerAgent) -> None:
        with patch.object(agent, "_llm_call_anthropic", new=AsyncMock(return_value="ok")) as mock_a, \
             patch.object(agent, "_llm_call_openai", new=AsyncMock(return_value="ok")) as mock_o:
            result = await agent._llm_call("test", "sys", "user", model="claude-sonnet-4-6")
            mock_a.assert_awaited_once()
            mock_o.assert_not_awaited()
            assert result == "ok"

    async def test_gpt_model_calls_openai(self, agent: PlannerAgent) -> None:
        with patch.object(agent, "_llm_call_anthropic", new=AsyncMock(return_value="ok")) as mock_a, \
             patch.object(agent, "_llm_call_openai", new=AsyncMock(return_value="ok")) as mock_o:
            result = await agent._llm_call("test", "sys", "user", model="gpt-4.1-mini")
            mock_o.assert_awaited_once()
            mock_a.assert_not_awaited()
            assert result == "ok"

    async def test_default_model_uses_main(self, agent: PlannerAgent) -> None:
        # main 모델은 claude-sonnet-4-6 → Anthropic 디스패치
        with patch.object(agent, "_llm_call_anthropic", new=AsyncMock(return_value="ok")) as mock_a:
            await agent._llm_call("test", "sys", "user")
            mock_a.assert_awaited_once()


# ── _strip_json_fence ─────────────────────────────────────────────


class TestStripJsonFence:
    def test_plain_json_unchanged(self) -> None:
        raw = '{"key": "value"}'
        assert _strip_json_fence(raw) == raw

    def test_json_fenced_block(self) -> None:
        raw = '```json\n{"key": "value"}\n```'
        assert _strip_json_fence(raw) == '{"key": "value"}'

    def test_plain_fenced_block(self) -> None:
        raw = '```\n{"key": "value"}\n```'
        assert _strip_json_fence(raw) == '{"key": "value"}'

    def test_strips_surrounding_whitespace(self) -> None:
        raw = '  ```json\n{"a": 1}\n```  '
        assert _strip_json_fence(raw) == '{"a": 1}'


# ── _stage1_sort ───────────────────────────────────────────────────

_CAT_ERP = "ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?"
_CAT_APP = "앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?"


def _make_stage1_input(clusters: list[Cluster]) -> PlannerInput:
    rr = _make_research_result(clusters=clusters)
    return PlannerInput(
        intent=["비교 판단"],
        questions=[_CAT_ERP, _CAT_APP],
        content_direction=["판단 기준 제시"],
        research_result=rr,
        target_month="2026-03",
        client_name="wishket",
    )


@pytest.mark.asyncio
class TestStage1Sort:
    async def test_basic_mapping(self, agent: PlannerAgent) -> None:
        """LLM이 올바른 JSON을 반환하면 DerivedQuestion 목록이 생성된다."""
        clusters = [
            _make_cluster("c000", "ERP 외주 개발 업체", paa=["ERP 외주 개발 업체 선정 기준은?"]),
            _make_cluster("c001", "앱개발업체"),
        ]
        pi = _make_stage1_input(clusters)

        sort_response = json.dumps({
            "mappings": [
                {
                    "cluster_id": "c000",
                    "category": _CAT_ERP,
                    "derived_question": "ERP 외주 개발 업체를 선정할 때 반드시 확인해야 할 기준은 무엇인가요?",
                    "mapping_reason": "naver_top_titles 3개 중 2개에서 '업체 선정 기준' 패턴 확인.",
                },
                {
                    "cluster_id": "c001",
                    "category": _CAT_APP,
                    "derived_question": "앱 개발 업체를 선택할 때 무엇을 확인해야 하나요?",
                    "mapping_reason": "paa_questions 2개 중 2개가 앱 개발 업체 선택 주제.",
                },
            ]
        }, ensure_ascii=False)
        validate_response = json.dumps({
            "validations": [
                {"cluster_id": "c000", "verdict": "ok"},
                {"cluster_id": "c001", "verdict": "ok"},
            ]
        }, ensure_ascii=False)

        with patch.object(
            agent, "_llm_call", new=AsyncMock(side_effect=[sort_response, validate_response])
        ):
            result = await agent._stage1_sort(pi)

        assert len(result) == 2
        assert result[0].question_id == "q001"
        assert result[0].source_cluster_id == "c000"
        assert result[0].category == _CAT_ERP
        assert "기준" in result[0].question
        assert result[0].mapping_rationale != ""  # 매핑 이유가 저장됨
        assert result[1].question_id == "q002"
        assert result[1].source_cluster_id == "c001"
        assert result[1].category == _CAT_APP
        assert result[1].mapping_rationale != ""

    async def test_missing_cluster_fallback(self, agent: PlannerAgent) -> None:
        """LLM 응답에서 누락된 cluster_id는 unassigned로 폴백된다."""
        clusters = [
            _make_cluster("c000", "ERP 외주"),
            _make_cluster("c001", "앱 견적", paa=["앱 견적은 어떻게 받나요?"]),
        ]
        pi = _make_stage1_input(clusters)

        # c001은 LLM 응답에 없음 → fallback unassigned
        sort_response = json.dumps({
            "mappings": [
                {
                    "cluster_id": "c000",
                    "category": _CAT_ERP,
                    "derived_question": "ERP 외주 개발 시 무엇을 확인해야 하나요?",
                    "mapping_reason": "naver_top_titles 2개 중 2개에서 외주 개발 확인사항 패턴 발견.",
                },
            ]
        }, ensure_ascii=False)
        # fallback c001은 unassigned → 검증에서 ok 처리
        validate_response = json.dumps({
            "validations": [
                {"cluster_id": "c000", "verdict": "ok"},
                {"cluster_id": "c001", "verdict": "ok"},
            ]
        }, ensure_ascii=False)

        with patch.object(
            agent, "_llm_call", new=AsyncMock(side_effect=[sort_response, validate_response])
        ):
            result = await agent._stage1_sort(pi)

        assert len(result) == 2
        unassigned = [d for d in result if d.category == "unassigned"]
        assert len(unassigned) == 1
        assert unassigned[0].source_cluster_id == "c001"
        # paa_questions[0]이 파생 질문으로 사용됨
        assert unassigned[0].question == "앱 견적은 어떻게 받나요?"

    async def test_empty_clusters_returns_empty(self, agent: PlannerAgent) -> None:
        """클러스터가 없으면 LLM 호출 없이 빈 목록을 반환한다."""
        pi = _make_stage1_input(clusters=[])

        with patch.object(agent, "_llm_call", new=AsyncMock()) as mock_llm:
            result = await agent._stage1_sort(pi)

        assert result == []
        mock_llm.assert_not_awaited()

    async def test_json_fence_in_llm_response(self, agent: PlannerAgent) -> None:
        """LLM이 마크다운 코드 블록으로 감싸 반환해도 파싱된다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)

        inner = json.dumps({
            "mappings": [{
                "cluster_id": "c000",
                "category": _CAT_ERP,
                "derived_question": "ERP 외주 개발이란 무엇인가요?",
                "mapping_reason": "naver_top_titles 1개 중 1개에서 ERP 외주 관련 확인.",
            }]
        }, ensure_ascii=False)
        fenced_response = f"```json\n{inner}\n```"
        validate_response = json.dumps({
            "validations": [{"cluster_id": "c000", "verdict": "ok"}]
        }, ensure_ascii=False)

        with patch.object(
            agent, "_llm_call", new=AsyncMock(side_effect=[fenced_response, validate_response])
        ):
            result = await agent._stage1_sort(pi)

        assert len(result) == 1
        assert result[0].source_cluster_id == "c000"

    async def test_run_stage1_returns_derived_questions(self, agent: PlannerAgent) -> None:
        """run(stop_at_stage=1)이 derived_questions를 포함한 dict를 반환한다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)

        sort_response = json.dumps({
            "mappings": [{
                "cluster_id": "c000",
                "category": _CAT_ERP,
                "derived_question": "ERP 외주 개발이란 무엇인가요?",
                "mapping_reason": "naver_top_titles 1개 중 1개에서 ERP 외주 관련 확인.",
            }]
        }, ensure_ascii=False)
        validate_response = json.dumps({
            "validations": [{"cluster_id": "c000", "verdict": "ok"}]
        }, ensure_ascii=False)

        with patch.object(
            agent, "_llm_call", new=AsyncMock(side_effect=[sort_response, validate_response])
        ):
            result = await agent.run(pi, stop_at_stage=1)

        assert isinstance(result, dict)
        assert result["stage"] == 1
        assert len(result["derived_questions"]) == 1
        dq = result["derived_questions"][0]
        assert dq["category"] == _CAT_ERP
        assert dq["mapping_rationale"] != ""

    async def test_derived_questions_array_parsed(self, agent: PlannerAgent) -> None:
        """LLM이 derived_questions 배열을 반환하면 각 항목이 DerivedQuestion으로 생성된다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)

        sort_response = json.dumps({
            "mappings": [{
                "cluster_id": "c000",
                "category": _CAT_ERP,
                "derived_questions": [
                    "ERP 외주 개발 업체 선정 기준은 무엇인가요?",
                    "ERP 외주 개발 계약 시 주의할 점은 무엇인가요?",
                ],
                "mapping_reason": "naver_top_titles 3개 중 2개에서 업체 선정 패턴 확인.",
            }]
        }, ensure_ascii=False)
        validate_response = json.dumps({
            "validations": [{"cluster_id": "c000", "verdict": "ok"}]
        }, ensure_ascii=False)

        with patch.object(
            agent, "_llm_call", new=AsyncMock(side_effect=[sort_response, validate_response])
        ):
            result = await agent._stage1_sort(pi)

        assert len(result) == 2
        assert result[0].question_id == "q001"
        assert result[1].question_id == "q002"
        assert result[0].source_cluster_id == "c000"
        assert result[1].source_cluster_id == "c000"
        assert result[0].category == _CAT_ERP
        assert result[1].category == _CAT_ERP
        assert "선정" in result[0].question
        assert "계약" in result[1].question

    async def test_derived_questions_single_item_array(self, agent: PlannerAgent) -> None:
        """derived_questions 배열에 항목 1개면 DerivedQuestion 1개만 생성된다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)

        sort_response = json.dumps({
            "mappings": [{
                "cluster_id": "c000",
                "category": _CAT_ERP,
                "derived_questions": ["ERP 외주 개발이란 무엇인가요?"],
                "mapping_reason": "naver_top_titles 1개 중 1개에서 ERP 관련 확인.",
            }]
        }, ensure_ascii=False)
        validate_response = json.dumps({
            "validations": [{"cluster_id": "c000", "verdict": "ok"}]
        }, ensure_ascii=False)

        with patch.object(
            agent, "_llm_call", new=AsyncMock(side_effect=[sort_response, validate_response])
        ):
            result = await agent._stage1_sort(pi)

        assert len(result) == 1
        assert result[0].question_id == "q001"

    async def test_per_category_target_in_payload(self, agent: PlannerAgent) -> None:
        """per_category_target이 LLM 페이로드에 포함된다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)

        captured: list[str] = []

        async def mock_llm(label: str, system: str, user: str, **kwargs: Any) -> str:
            if label == "stage1_sort":
                captured.append(user)
            return json.dumps({
                "mappings": [{
                    "cluster_id": "c000",
                    "category": _CAT_ERP,
                    "derived_question": "ERP 외주 개발이란 무엇인가요?",
                    "mapping_reason": "test",
                }]
            }, ensure_ascii=False)

        with patch.object(agent, "_llm_call", new=mock_llm):
            await agent._stage1_sort(pi)

        assert len(captured) == 1
        payload = json.loads(captured[0])
        assert "per_category_target" in payload
        monthly_count = agent._compute_monthly_count(pi.target_month)
        n_cats = len(pi.questions)
        expected = max(1, monthly_count // n_cats)
        assert payload["per_category_target"] == expected


# ── _stage1_validate ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestStage1Validate:
    async def test_ok_verdict_no_change(self, agent: PlannerAgent) -> None:
        """모든 verdict가 ok면 mappings가 변경되지 않는다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)
        mappings = [{
            "cluster_id": "c000",
            "category": _CAT_ERP,
            "derived_question": "ERP 업체 선정 기준은 무엇인가요?",
            "mapping_reason": "naver_top_titles 3개 중 2개에서 업체 기준 확인.",
        }]
        validate_response = json.dumps({
            "validations": [{"cluster_id": "c000", "verdict": "ok"}]
        }, ensure_ascii=False)

        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=validate_response)):
            result = await agent._stage1_validate(clusters, pi, mappings)

        assert result[0]["category"] == _CAT_ERP
        assert result[0]["derived_question"] == "ERP 업체 선정 기준은 무엇인가요?"
        # mapping_reason은 교정 접미사 없음
        assert "[검증 교정" not in result[0]["mapping_reason"]

    async def test_reassign_corrects_category_and_question(self, agent: PlannerAgent) -> None:
        """reassign verdict가 있으면 카테고리와 질문이 교정된다."""
        clusters = [_make_cluster("c007", "앱개발업체")]
        pi = _make_stage1_input(clusters)
        mappings = [{
            "cluster_id": "c007",
            "category": _CAT_ERP,  # 잘못 배정됨
            "derived_question": "앱 개발 업체를 선택할 때 확인해야 할 점은 무엇인가요?",
            "mapping_reason": "초기 매핑 이유.",
        }]
        validate_response = json.dumps({
            "validations": [{
                "cluster_id": "c007",
                "verdict": "reassign",
                "issue": "derived_question이 앱 개발 업체 주제인데 category는 ERP 외주 업체 기준 — 도메인 불일치.",
                "corrected_category": _CAT_APP,
                "corrected_question": "앱 개발 업체를 선정할 때 견적 이외에 확인해야 할 기준은 무엇인가요?",
            }]
        }, ensure_ascii=False)

        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=validate_response)):
            result = await agent._stage1_validate(clusters, pi, mappings)

        assert result[0]["category"] == _CAT_APP
        assert "견적 이외에" in result[0]["derived_question"]
        assert "[검증 교정 reassign:" in result[0]["mapping_reason"]

    async def test_rewrite_changes_question_only(self, agent: PlannerAgent) -> None:
        """rewrite verdict는 카테고리는 유지하고 질문만 교정한다."""
        clusters = [
            _make_cluster("c003", "위험요소"),
            _make_cluster("c004", "손실예방"),
        ]
        pi = _make_stage1_input(clusters)
        mappings = [
            {
                "cluster_id": "c003",
                "category": _CAT_ERP,
                "derived_question": "외주 개발 위험 요소와 예방책은 무엇인가요?",
                "mapping_reason": "원본 이유.",
            },
            {
                "cluster_id": "c004",
                "category": _CAT_ERP,
                "derived_question": "외주 개발 손실 원인과 예방책은 무엇인가요?",
                "mapping_reason": "원본 이유.",
            },
        ]
        validate_response = json.dumps({
            "validations": [
                {"cluster_id": "c003", "verdict": "ok"},
                {
                    "cluster_id": "c004",
                    "verdict": "rewrite",
                    "issue": "c003과 '예방책' 각도가 중복됨.",
                    "corrected_question": "외주 개발 프로젝트에서 비용 손실이 주로 발생하는 단계는 어디인가요?",
                },
            ]
        }, ensure_ascii=False)

        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=validate_response)):
            result = await agent._stage1_validate(clusters, pi, mappings)

        assert result[1]["category"] == _CAT_ERP  # 카테고리 유지
        assert "비용 손실이 주로 발생하는 단계" in result[1]["derived_question"]
        assert "[검증 교정 rewrite:" in result[1]["mapping_reason"]

    async def test_json_error_returns_original_mappings(self, agent: PlannerAgent) -> None:
        """검증 LLM이 파싱 불가 응답을 반환하면 원본 mappings를 그대로 반환한다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)
        original_mappings = [{
            "cluster_id": "c000",
            "category": _CAT_ERP,
            "derived_question": "원본 질문인가요?",
            "mapping_reason": "원본 이유.",
        }]

        with patch.object(agent, "_llm_call", new=AsyncMock(return_value="not valid json {{{")):
            result = await agent._stage1_validate(clusters, pi, original_mappings)

        # 원본 그대로 반환
        assert result[0]["derived_question"] == "원본 질문인가요?"
        assert "[검증 교정" not in result[0]["mapping_reason"]


# ── _stage2_funnel ─────────────────────────────────────────────────


@pytest.mark.asyncio
class TestStage2Funnel:
    def _make_derived(
        self,
        qid: str,
        question: str,
        category: str = _CAT_ERP,
        cluster_id: str = "c000",
    ) -> DerivedQuestion:
        return DerivedQuestion(
            question_id=qid,
            question=question,
            category=category,
            source_cluster_id=cluster_id,
        )

    async def test_funnel_applied_by_question_id(self, agent: PlannerAgent) -> None:
        """LLM 응답의 funnel_tag가 question_id 기준으로 DerivedQuestion에 적용된다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)
        derived = [
            self._make_derived("q001", "ERP 외주 개발이란 무엇인가요?"),
            self._make_derived("q002", "ERP 외주 업체 선정 기준은 무엇인가요?"),
        ]
        funnel_response = json.dumps({
            "funnel_tags": [
                {"question_id": "q001", "funnel": "awareness", "funnel_rationale": {
                    "journey_reasoning": "이 질문은 ERP 외주 개발이란 무엇인지를 묻는다 → 검색자는 아직 외주 개발 카테고리 자체를 탐색하는 중이다 → 위시켓 전환까지 거리가 멀다 → awareness",
                    "searcher_state": "외주 개념을 탐색하기 전 단계",
                    "judgment_basis": "1순위 추론",
                    "signals_used": "없음",
                }},
                {"question_id": "q002", "funnel": "consideration", "funnel_rationale": {
                    "journey_reasoning": "이 질문은 외주 업체 선정 기준을 묻는다 → 검색자는 이미 외주를 선택지로 알고 어떤 업체를 고를지 조건을 따지는 중이다 → 아직 특정 플랫폼을 정하지 않아 전환까지 한 단계 남았다 → consideration",
                    "searcher_state": "외주 업체 선정 기준을 따지는 단계",
                    "judgment_basis": "1순위 추론",
                    "signals_used": "없음",
                }},
            ]
        }, ensure_ascii=False)

        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=funnel_response)):
            result = await agent._stage2_funnel(derived, pi)

        assert result[0].funnel == "awareness"
        assert result[0].funnel_journey_reasoning != ""
        assert result[0].funnel_searcher_state == "외주 개념을 탐색하기 전 단계"
        assert result[0].funnel_judgment_basis == "1순위 추론"
        assert result[0].funnel_signals_used == "없음"
        assert result[1].funnel == "consideration"
        assert result[1].funnel_journey_reasoning != ""
        assert result[1].funnel_judgment_basis == "1순위 추론"

    async def test_unassigned_questions_skipped(self, agent: PlannerAgent) -> None:
        """unassigned 카테고리 질문은 LLM 페이로드에서 제외되고 funnel=unclassified 유지."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)
        derived = [
            self._make_derived("q001", "ERP 외주 개발이란?", category=_CAT_ERP),
            self._make_derived("q002", "분류 불가 클러스터", category="unassigned"),
        ]
        funnel_response = json.dumps({
            "funnel_tags": [
                {"question_id": "q001", "funnel": "awareness", "funnel_rationale": {
                    "journey_reasoning": "이 질문은 외주 개발이란 무엇인지를 묻는다 → 검색자는 아직 외주 카테고리를 탐색하는 중이다 → 전환까지 거리가 멀다 → awareness",
                    "searcher_state": "외주 개념 탐색 전 단계",
                    "judgment_basis": "1순위 추론",
                    "signals_used": "없음",
                }},
            ]
        }, ensure_ascii=False)

        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=funnel_response)) as mock:
            result = await agent._stage2_funnel(derived, pi)

        # unassigned 질문은 LLM 페이로드에 없었고 funnel 변경되지 않음
        assert result[1].funnel == "unclassified"
        assert result[1].funnel_journey_reasoning == ""
        assert result[1].funnel_searcher_state == ""
        assert result[1].funnel_judgment_basis == ""
        # LLM 호출 1회 (unassigned 제외 후 taggable=1개 남아 있어 호출됨)
        mock.assert_awaited_once()

    async def test_all_unassigned_no_llm_call(self, agent: PlannerAgent) -> None:
        """태깅할 질문이 없으면 LLM 호출 없이 원본을 반환한다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)
        derived = [
            self._make_derived("q001", "분류 불가", category="unassigned"),
        ]

        with patch.object(agent, "_llm_call", new=AsyncMock()) as mock:
            result = await agent._stage2_funnel(derived, pi)

        mock.assert_not_awaited()
        assert result[0].funnel == "unclassified"

    async def test_missing_tag_keeps_unclassified(self, agent: PlannerAgent) -> None:
        """LLM 응답에서 question_id가 누락되면 해당 질문은 unclassified 유지."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)
        derived = [
            self._make_derived("q001", "ERP 외주 개발이란?"),
            self._make_derived("q002", "앱 개발 견적 비교 기준은?"),
        ]
        # q002 태그 누락
        funnel_response = json.dumps({
            "funnel_tags": [
                {"question_id": "q001", "funnel": "awareness", "funnel_rationale": {
                    "journey_reasoning": "이 질문은 외주 개발이란 무엇인지를 묻는다 → 검색자는 아직 외주 카테고리를 탐색하는 중이다 → 전환까지 거리가 멀다 → awareness",
                    "searcher_state": "외주 개념 탐색 전 단계",
                    "judgment_basis": "1순위 추론",
                    "signals_used": "없음",
                }},
            ]
        }, ensure_ascii=False)

        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=funnel_response)):
            result = await agent._stage2_funnel(derived, pi)

        assert result[0].funnel == "awareness"
        assert result[1].funnel == "unclassified"  # 누락 → 기본값 유지
        assert result[1].funnel_journey_reasoning == ""  # 기록 없음
        assert result[1].funnel_judgment_basis == ""

    async def test_question_id_not_mutated(self, agent: PlannerAgent) -> None:
        """_stage2_funnel이 question_id를 변경하지 않는다."""
        clusters = [_make_cluster("c000", "ERP 외주")]
        pi = _make_stage1_input(clusters)
        derived = [self._make_derived("q003", "ERP 외주 계약서 필수 항목은?")]
        funnel_response = json.dumps({
            "funnel_tags": [{"question_id": "q003", "funnel": "conversion", "funnel_rationale": {
                "journey_reasoning": "이 질문은 외주 계약서의 필수 항목을 묻는다 → 검색자는 외주를 하기로 결정했고 계약 실무 정보를 찾는 중이다 → 위시켓에서 프로젝트 등록 직전 단계다 → conversion",
                "searcher_state": "외주 계약 실무 정보를 찾는 단계",
                "judgment_basis": "1순위 추론",
                "signals_used": "없음",
            }}]
        }, ensure_ascii=False)

        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=funnel_response)):
            result = await agent._stage2_funnel(derived, pi)

        assert result[0].question_id == "q003"  # 변경 없음
        assert result[0].funnel == "conversion"
        assert result[0].funnel_journey_reasoning != ""
        assert result[0].funnel_judgment_basis == "1순위 추론"
        assert result[0].funnel_signals_used == "없음"


# ── Stage 3 헬퍼 단위 테스트 ────────────────────────────────────────


class TestKeywordOverlapHelpers:
    def _pub(self, title: str, keyword: str = "") -> PublishedContent:
        return PublishedContent(url="https://example.com", title=title, main_keyword=keyword)

    def test_overlap_high_when_keywords_match(self) -> None:
        """질문 토큰과 기발행 콘텐츠 토큰이 많이 겹치면 높은 점수."""
        score = _compute_keyword_overlap(
            "ERP 외주 개발 업체 선정 기준",
            ["ERP 외주", "업체 선정"],
            self._pub("ERP 외주 개발 업체 고르는 법", "ERP 외주 개발"),
        )
        assert score > 0.3

    def test_overlap_low_when_different_domain(self) -> None:
        """도메인이 다른 경우 겹침이 낮다."""
        score = _compute_keyword_overlap(
            "앱 개발 견적 비교 기준",
            ["앱 개발", "견적"],
            self._pub("ERP 도입 비용 절감 방법", "ERP 도입"),
        )
        assert score < 0.3

    def test_overlap_zero_on_empty_question(self) -> None:
        score = _compute_keyword_overlap("", [], self._pub("ERP 외주"))
        assert score == 0.0

    def test_urgency_high_for_old_content(self) -> None:
        """발행 18개월↑이면 high."""
        pub = PublishedContent(url="u", title="t", publish_date="2024-06-01")
        assert _compute_urgency(pub) == "high"

    def test_urgency_medium_for_mid_age(self) -> None:
        """발행 6~17개월이면 medium."""
        pub = PublishedContent(url="u", title="t", publish_date="2025-06-01")
        assert _compute_urgency(pub) == "medium"

    def test_urgency_low_when_no_date(self) -> None:
        """발행일 없으면 low."""
        pub = PublishedContent(url="u", title="t")
        assert _compute_urgency(pub) == "low"


# ── _stage3_duplicate ────────────────────────────────────────────


def _make_derived_q(
    question_id: str = "q001",
    question: str = "ERP 외주 개발 업체 선정 기준은?",
    source_cluster_id: str = "c000",
    funnel: str = "consideration",
) -> DerivedQuestion:
    return DerivedQuestion(
        question_id=question_id,
        question=question,
        category="ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?",
        source_cluster_id=source_cluster_id,
        funnel=funnel,  # type: ignore[arg-type]
    )


def _make_pub(
    title: str = "ERP 외주 개발 업체 선정 방법",
    keyword: str = "ERP 외주 개발",
    url: str = "https://blog.wishket.com/erp-outsourcing",
    publish_date: str = "2024-06-01",
) -> PublishedContent:
    return PublishedContent(
        url=url,
        title=title,
        main_keyword=keyword,
        publish_date=publish_date,
    )


def _llm_dup_resp(
    title_sim: float,
    topic_ov: float,
    angle_ok: bool,
    angle_reason: str = "",
    rationale: str = "테스트 판단 근거.",
) -> str:
    return json.dumps({
        "title_similarity": title_sim,
        "topic_overlap": topic_ov,
        "angle_shift_possible": angle_ok,
        "angle_shift_reason": angle_reason,
        "rationale": rationale,
    }, ensure_ascii=False)


@pytest.mark.asyncio
class TestStage3Duplicate:
    async def test_empty_published_all_new(self, agent: PlannerAgent) -> None:
        """published_contents 없으면 전체 new, LLM 호출 없음."""
        derived = [_make_derived_q("q001"), _make_derived_q("q002")]
        pi = _make_planner_input()  # published_contents=[]

        with patch.object(agent, "_llm_call", new=AsyncMock()) as mock_llm:
            result_derived, candidates = await agent._stage3_duplicate(derived, pi)

        mock_llm.assert_not_awaited()
        assert len(candidates) == 0
        for dq in result_derived:
            assert dq.duplicate_result is not None
            assert dq.duplicate_result.verdict == "new"
            assert dq.duplicate_result.risk_score == 0.0

    async def test_new_verdict_low_risk(self, agent: PlannerAgent) -> None:
        """risk_score < 0.4이면 verdict=new, UpdateCandidate 없음."""
        # title_sim=0.1, topic_ov=0.1 → LLM 응답
        # keyword_overlap은 실제 계산 (앱 개발 견적 vs ERP 외주 — 낮게 나와야 함)
        derived = [_make_derived_q(question="앱 개발 견적 비교 방법은?", source_cluster_id="c000")]
        pub = _make_pub(title="ERP 외주 개발 업체 고르는 법", keyword="ERP 외주 개발")
        clusters = [_make_cluster("c000", "앱 개발 견적")]
        rr = _make_research_result(clusters=clusters)
        pi = _make_planner_input(research_result=rr, published_contents=[pub])

        llm_resp = _llm_dup_resp(title_sim=0.1, topic_ov=0.1, angle_ok=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=llm_resp)):
            result_derived, candidates = await agent._stage3_duplicate(derived, pi)

        dq = result_derived[0]
        assert dq.duplicate_result is not None
        assert dq.duplicate_result.verdict == "new"
        assert len(candidates) == 0

    async def test_angle_shift_verdict(self, agent: PlannerAgent) -> None:
        """risk 0.4~0.7 + angle_ok=True → verdict=angle_shift."""
        # keyword_overlap 높게 나오도록 question과 pub을 동일 도메인으로 설정
        derived = [_make_derived_q(question="ERP 외주 개발 업체 선정 기준은?")]
        pub = _make_pub()
        clusters = [_make_cluster("c000", "ERP 외주 개발 업체")]
        rr = _make_research_result(clusters=clusters)
        pi = _make_planner_input(research_result=rr, published_contents=[pub])

        # keyword_overlap을 원하는 값으로 고정
        import core.agents.planner.agent as agent_mod
        with patch.object(agent_mod, "_compute_keyword_overlap", return_value=0.5):
            # risk = 0.5×0.4 + 0.4×0.3 + 0.3×0.3 = 0.20+0.12+0.09 = 0.41 → angle_shift 구간
            llm_resp = _llm_dup_resp(
                title_sim=0.4, topic_ov=0.3, angle_ok=True,
                angle_reason="기존 글은 업체 유형 분류 중심, 이 글은 선정 기준 비교에 집중."
            )
            with patch.object(agent, "_llm_call", new=AsyncMock(return_value=llm_resp)):
                result_derived, candidates = await agent._stage3_duplicate(derived, pi)

        dq = result_derived[0]
        assert dq.duplicate_result is not None
        assert dq.duplicate_result.verdict == "angle_shift"
        assert len(candidates) == 0  # angle_shift는 UpdateCandidate 생성 안 함

    async def test_update_existing_high_risk(self, agent: PlannerAgent) -> None:
        """risk ≥ 0.7 → update_existing + UpdateCandidate 생성."""
        derived = [_make_derived_q(question="ERP 외주 개발 업체 선정 기준은?")]
        pub = _make_pub()
        clusters = [_make_cluster("c000", "ERP 외주 개발 업체")]
        rr = _make_research_result(clusters=clusters)
        pi = _make_planner_input(research_result=rr, published_contents=[pub])

        import core.agents.planner.agent as agent_mod
        with patch.object(agent_mod, "_compute_keyword_overlap", return_value=0.8):
            # risk = 0.8×0.4 + 0.9×0.3 + 0.8×0.3 = 0.32+0.27+0.24 = 0.83 → update_existing
            llm_resp = _llm_dup_resp(title_sim=0.9, topic_ov=0.8, angle_ok=False)
            with patch.object(agent, "_llm_call", new=AsyncMock(return_value=llm_resp)):
                result_derived, candidates = await agent._stage3_duplicate(derived, pi)

        dq = result_derived[0]
        assert dq.duplicate_result is not None
        assert dq.duplicate_result.verdict == "update_existing"
        assert dq.duplicate_result.risk_score >= 0.7
        assert len(candidates) == 1
        assert candidates[0].published_url == pub.url
        assert candidates[0].published_title == pub.title
        assert candidates[0].urgency == "high"  # 2024-06-01 → 18개월↑

    async def test_angle_fail_becomes_update_existing(self, agent: PlannerAgent) -> None:
        """risk 0.4~0.7 + angle_ok=False → update_existing."""
        derived = [_make_derived_q()]
        pub = _make_pub()
        clusters = [_make_cluster("c000", "ERP 외주 개발 업체")]
        rr = _make_research_result(clusters=clusters)
        pi = _make_planner_input(research_result=rr, published_contents=[pub])

        import core.agents.planner.agent as agent_mod
        with patch.object(agent_mod, "_compute_keyword_overlap", return_value=0.5):
            # risk = 0.5×0.4 + 0.4×0.3 + 0.3×0.3 = 0.41 → angle_shift 구간
            llm_resp = _llm_dup_resp(title_sim=0.4, topic_ov=0.3, angle_ok=False)
            with patch.object(agent, "_llm_call", new=AsyncMock(return_value=llm_resp)):
                result_derived, candidates = await agent._stage3_duplicate(derived, pi)

        dq = result_derived[0]
        assert dq.duplicate_result.verdict == "update_existing"
        assert len(candidates) == 1

    async def test_llm_failure_falls_back_to_new(self, agent: PlannerAgent) -> None:
        """LLM 호출 실패 시 안전 방향으로 verdict=new."""
        derived = [_make_derived_q()]
        pub = _make_pub()
        clusters = [_make_cluster("c000", "ERP 외주 개발 업체")]
        rr = _make_research_result(clusters=clusters)
        pi = _make_planner_input(research_result=rr, published_contents=[pub])

        import core.agents.planner.agent as agent_mod
        with patch.object(agent_mod, "_compute_keyword_overlap", return_value=0.6):
            with patch.object(agent, "_llm_call", new=AsyncMock(side_effect=RuntimeError("timeout"))):
                result_derived, candidates = await agent._stage3_duplicate(derived, pi)

        assert result_derived[0].duplicate_result.verdict == "new"
        assert len(candidates) == 0

    async def test_run_stop_at_stage3_returns_candidates(
        self, tmp_path, agent: PlannerAgent
    ) -> None:
        """run(stop_at_stage=3)이 update_candidates를 반환한다."""
        # stage2 스냅샷 준비
        dq_data = _make_derived_q("q001", source_cluster_id="c000").model_dump()
        dq_data["funnel"] = "consideration"
        snap_data = {"stage": 2, "derived_questions": [dq_data]}
        (tmp_path / "2026-02-27_120000_stage2.json").write_text(
            json.dumps(snap_data, ensure_ascii=False), encoding="utf-8"
        )

        clusters = [_make_cluster("c000", "ERP 외주 개발 업체")]
        pub = _make_pub()
        rr = _make_research_result(clusters=clusters)
        pi = _make_planner_input(research_result=rr, published_contents=[pub])

        import core.agents.planner.agent as agent_mod
        with patch.object(agent_mod, "_compute_keyword_overlap", return_value=0.0):
            # overlap < 0.1 → LLM 없이 new 처리
            result = await agent.run(
                pi, stop_at_stage=3, start_at_stage=3, snapshot_dir=str(tmp_path)
            )

        assert result["stage"] == 3
        assert len(result["derived_questions"]) == 1
        assert result["derived_questions"][0]["duplicate_result"]["verdict"] == "new"
        assert result["update_candidates"] == []


# ── _stage4_priority ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestStage4Priority:
    """Stage 4: 우선순위 점수 계산 + 선발/대기 배정 + LLM 근거 생성."""

    # ── 헬퍼 ────────────────────────────────────────────────────────

    def _make_cluster_vol(
        self,
        cluster_id: str,
        keyword: str,
        volume: int = 1000,
        trend: str = "stable",
    ) -> Cluster:
        return Cluster(
            cluster_id=cluster_id,
            representative_keyword=keyword,
            paa_questions=[],
            keywords=[ClusterKeyword(keyword=keyword)],
            total_volume_naver=volume,
            volume_trend=trend,  # type: ignore[arg-type]
        )

    def _make_dq(
        self,
        qid: str,
        question: str,
        category: str,
        cluster_id: str,
        verdict: str | None = None,
        funnel: str = "consideration",
    ) -> DerivedQuestion:
        dq = DerivedQuestion(
            question_id=qid,
            question=question,
            category=category,
            source_cluster_id=cluster_id,
            funnel=funnel,  # type: ignore[arg-type]
        )
        if verdict is not None:
            dq.duplicate_result = DuplicateResult(verdict=verdict)  # type: ignore[arg-type]
        return dq

    # ── 테스트 ──────────────────────────────────────────────────────

    async def test_priority_score_log_normalized(self, agent: PlannerAgent) -> None:
        """log 정규화 + trend_weight 합산 정확성 검증."""
        import math

        c_high = self._make_cluster_vol("c_high", "고볼륨", volume=10000, trend="rising")
        c_low = self._make_cluster_vol("c_low", "저볼륨", volume=100, trend="declining")
        rr = _make_research_result(clusters=[c_high, c_low])
        pi = _make_planner_input(research_result=rr, questions=[_CAT_ERP])

        dq_high = self._make_dq("q001", "고볼륨 질문?", _CAT_ERP, "c_high")
        dq_low = self._make_dq("q002", "저볼륨 질문?", _CAT_ERP, "c_low")

        mock_resp = json.dumps({"rationales": []}, ensure_ascii=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=mock_resp)):
            out, _ = await agent._stage4_priority([dq_high, dq_low], pi)

        log_max = math.log10(10001)
        expected_high = round(0.7 * (math.log10(10001) / log_max) + 0.3 * 1.0, 4)
        expected_low = round(0.7 * (math.log10(101) / log_max) + 0.3 * 0.0, 4)

        assert out[0].priority_score == expected_high
        assert out[1].priority_score == expected_low
        assert out[0].priority_score > out[1].priority_score

    async def test_selection_within_monthly_budget(self, agent: PlannerAgent) -> None:
        """is_selected 개수 ≤ monthly_count."""
        c = self._make_cluster_vol("c000", "ERP 외주", volume=1000)
        rr = _make_research_result(clusters=[c])
        pi = _make_planner_input(research_result=rr, questions=[_CAT_ERP])

        derived = [
            self._make_dq(f"q{i:03d}", f"질문 {i}?", _CAT_ERP, "c000")
            for i in range(1, 6)
        ]
        mock_resp = json.dumps({"rationales": []}, ensure_ascii=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=mock_resp)):
            _, selected = await agent._stage4_priority(derived, pi)

        monthly_count = agent._compute_monthly_count(pi.target_month)
        assert len(selected) <= monthly_count
        assert all(dq.is_selected for dq in selected)

    async def test_waitlist_assignment(self, agent: PlannerAgent) -> None:
        """is_waitlist=True 질문이 존재하고, is_selected와 배타적이다."""
        c = self._make_cluster_vol("c000", "키워드", volume=1000)
        rr = _make_research_result(clusters=[c])
        pi = PlannerInput(
            intent=["비교 판단"],
            questions=[_CAT_ERP, _CAT_APP],  # n_cats=2 → alloc=6 each
            content_direction=["판단 기준 제시"],
            research_result=rr,
            target_month="2026-03",
            client_name="wishket",
        )
        # 각 카테고리에 8개 → alloc=6 → 6 선발 + min(3, 8-6)=2 대기
        derived = [
            self._make_dq(f"qe{i:02d}", f"ERP 질문 {i}?", _CAT_ERP, "c000")
            for i in range(1, 9)
        ] + [
            self._make_dq(f"qa{i:02d}", f"앱 질문 {i}?", _CAT_APP, "c000")
            for i in range(1, 9)
        ]
        mock_resp = json.dumps({"rationales": []}, ensure_ascii=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=mock_resp)):
            all_qs, _ = await agent._stage4_priority(derived, pi)

        waitlisted = [dq for dq in all_qs if dq.is_waitlist]
        assert len(waitlisted) > 0
        for dq in all_qs:
            assert not (dq.is_selected and dq.is_waitlist), (
                f"{dq.question_id} is_selected와 is_waitlist가 동시에 True"
            )

    async def test_allocation_per_category(self, agent: PlannerAgent) -> None:
        """카테고리별 선발 수 = alloc[cat] (2026-02: monthly_count=12, n_cats=2 → alloc=6씩)."""
        c = self._make_cluster_vol("c000", "키워드", volume=1000)
        rr = _make_research_result(clusters=[c])
        pi = PlannerInput(
            intent=["비교 판단"],
            questions=[_CAT_ERP, _CAT_APP],
            content_direction=["판단 기준 제시"],
            research_result=rr,
            target_month="2026-02",  # 월·수·금 12개 (균등 배분 확인용)
            client_name="wishket",
        )
        # 2026-02: monthly_count=12, n_cats=2 → base_alloc=6, remainder=0 → 각 6개
        derived = [
            self._make_dq(f"qe{i:02d}", f"ERP 질문 {i}?", _CAT_ERP, "c000")
            for i in range(1, 9)
        ] + [
            self._make_dq(f"qa{i:02d}", f"앱 질문 {i}?", _CAT_APP, "c000")
            for i in range(1, 9)
        ]
        mock_resp = json.dumps({"rationales": []}, ensure_ascii=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=mock_resp)):
            _, selected = await agent._stage4_priority(derived, pi)

        erp_sel = [dq for dq in selected if dq.category == _CAT_ERP]
        app_sel = [dq for dq in selected if dq.category == _CAT_APP]
        assert len(erp_sel) == 6
        assert len(app_sel) == 6

    async def test_update_existing_excluded(self, agent: PlannerAgent) -> None:
        """verdict=update_existing 질문은 is_selected=False."""
        c = self._make_cluster_vol("c000", "ERP 외주", volume=5000, trend="rising")
        rr = _make_research_result(clusters=[c])
        pi = _make_planner_input(research_result=rr, questions=[_CAT_ERP])

        dq_update = self._make_dq(
            "q001", "ERP 중복 질문?", _CAT_ERP, "c000", verdict="update_existing"
        )
        dq_new = self._make_dq("q002", "ERP 신규 질문?", _CAT_ERP, "c000", verdict="new")

        mock_resp = json.dumps({"rationales": [
            {"question_id": "q002", "data_rationale": "검색량 높음.", "content_rationale": "방향 적합."},
        ]}, ensure_ascii=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=mock_resp)):
            all_qs, selected = await agent._stage4_priority([dq_update, dq_new], pi)

        assert not dq_update.is_selected
        assert dq_new.is_selected
        assert dq_update not in selected

    async def test_llm_rationale_populated(self, agent: PlannerAgent) -> None:
        """선발 질문의 selection_rationale이 비어 있지 않다."""
        c = self._make_cluster_vol("c000", "ERP 외주", volume=1000)
        rr = _make_research_result(clusters=[c])
        pi = _make_planner_input(research_result=rr, questions=[_CAT_ERP])

        dq = self._make_dq("q001", "ERP 외주 업체 선정 기준?", _CAT_ERP, "c000")
        rationale_resp = json.dumps({"rationales": [{
            "question_id": "q001",
            "data_rationale": "월 1,000회 검색, 트렌드 안정적.",
            "content_rationale": "판단 기준 제시 방향에 부합.",
        }]}, ensure_ascii=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=rationale_resp)):
            _, selected = await agent._stage4_priority([dq], pi)

        assert len(selected) == 1
        assert selected[0].selection_rationale != ""
        assert "[데이터]" in selected[0].selection_rationale
        assert "[방향]" in selected[0].selection_rationale

    async def test_llm_failure_fallback(self, agent: PlannerAgent) -> None:
        """LLM 예외 시 selection_rationale='' — 파이프라인 미중단."""
        c = self._make_cluster_vol("c000", "ERP 외주", volume=1000)
        rr = _make_research_result(clusters=[c])
        pi = _make_planner_input(research_result=rr, questions=[_CAT_ERP])

        dq = self._make_dq("q001", "ERP 외주 업체?", _CAT_ERP, "c000")
        with patch.object(
            agent, "_llm_call", new=AsyncMock(side_effect=RuntimeError("LLM 타임아웃"))
        ):
            _, selected = await agent._stage4_priority([dq], pi)  # 예외 없어야 함

        assert len(selected) == 1
        assert selected[0].selection_rationale == ""

    async def test_run_stop_at_stage4(self, tmp_path, agent: PlannerAgent) -> None:
        """stop_at_stage=4 → dict 반환, stage=4, all_questions·selected 포함."""
        # stage3 스냅샷 준비
        dq = DerivedQuestion(
            question_id="q001",
            question="ERP 외주 개발 업체 선정 기준은?",
            category=_CAT_ERP,
            source_cluster_id="c000",
            funnel="consideration",  # type: ignore[arg-type]
        )
        dq.duplicate_result = DuplicateResult(verdict="new")
        snap = {
            "stage": 3,
            "derived_questions": [dq.model_dump()],
            "update_candidates": [],
        }
        (tmp_path / "2026-02-27_120000_stage3.json").write_text(
            json.dumps(snap, ensure_ascii=False), encoding="utf-8"
        )

        c = Cluster(
            cluster_id="c000",
            representative_keyword="ERP 외주",
            paa_questions=[],
            keywords=[ClusterKeyword(keyword="ERP 외주")],
            total_volume_naver=1000,
            volume_trend="stable",
        )
        rr = _make_research_result(clusters=[c])
        pi = _make_planner_input(research_result=rr, questions=[_CAT_ERP])

        rationale_resp = json.dumps({"rationales": [{
            "question_id": "q001",
            "data_rationale": "데이터 근거.",
            "content_rationale": "방향 근거.",
        }]}, ensure_ascii=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=rationale_resp)):
            result = await agent.run(
                pi, stop_at_stage=4, start_at_stage=4, snapshot_dir=str(tmp_path)
            )

        assert isinstance(result, dict)
        assert result["stage"] == 4
        assert "all_questions" in result
        assert "selected" in result
        assert len(result["all_questions"]) == 1
        assert result["all_questions"][0]["is_selected"] is True
        assert len(result["selected"]) == 1

    async def test_fillup_promotes_eligible_when_shortfall(self, agent: PlannerAgent) -> None:
        """선발 수가 monthly_count에 미달하면 fill-up 패스가 eligible 질문을 추가 선발한다."""
        # eligible 3개뿐 → 초기 선발 3개, fill-up 시도하지만 fill_candidates 0 → WARNING
        c = self._make_cluster_vol("c000", "ERP 외주", volume=1000)
        rr = _make_research_result(clusters=[c])
        pi = _make_planner_input(research_result=rr, questions=[_CAT_ERP])

        dqs = [
            self._make_dq(f"q{i + 1:03d}", f"질문{i + 1}?", _CAT_ERP, "c000", verdict="new")
            for i in range(3)
        ]

        mock_resp = json.dumps({"rationales": []}, ensure_ascii=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=mock_resp)):
            all_qs, selected = await agent._stage4_priority(dqs, pi)

        # eligible 3개만 있으므로 3개 선발 (monthly_count 미달이지만 채울 후보 없음)
        assert len(selected) == 3
        assert all(dq.is_selected for dq in dqs)

    async def test_fillup_reaches_monthly_count(self, agent: PlannerAgent) -> None:
        """초기 선발이 monthly_count 미달이지만 fill_candidates로 채울 수 있으면 목표치에 도달한다."""
        # 2026-02: monthly_count=12, n_cats=2 → base_alloc=6, remainder=0 → 각 6
        c_erp = self._make_cluster_vol("c001", "ERP", volume=500)
        c_app = self._make_cluster_vol("c002", "앱개발", volume=500)
        rr = _make_research_result(clusters=[c_erp, c_app])
        pi = PlannerInput(
            intent=["비교 판단"],
            questions=[_CAT_ERP, _CAT_APP],
            content_direction=["판단 기준 제시"],
            research_result=rr,
            target_month="2026-02",  # monthly_count=12, alloc=6씩
            client_name="wishket",
        )
        monthly_count = agent._compute_monthly_count(pi.target_month)  # 12

        # ERP: eligible 4개 (alloc=6 → 4선발, shortfall 2)
        erp_dqs = [
            self._make_dq(f"q{i + 1:03d}", f"ERP질문{i + 1}?", _CAT_ERP, "c001", verdict="new")
            for i in range(4)
        ]
        # APP: eligible 8개 (alloc=6 → 6선발 + 대기 2개)
        app_dqs = [
            self._make_dq(f"q{i + 5:03d}", f"APP질문{i + 1}?", _CAT_APP, "c002", verdict="new")
            for i in range(8)
        ]
        all_dqs = erp_dqs + app_dqs

        mock_resp = json.dumps({"rationales": []}, ensure_ascii=False)
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=mock_resp)):
            out, selected = await agent._stage4_priority(all_dqs, pi)

        # 초기: ERP 4개 + APP 6개 = 10개 (shortfall 2)
        # fill-up: APP 대기 2개 추가 → 총 12개
        assert len(selected) == monthly_count


# ── _stage5_balance ────────────────────────────────────────────────


def _make_selected_dqs(funnels: list[str]) -> list[DerivedQuestion]:
    """퍼널 목록으로 is_selected=True 파생 질문 목록 생성."""
    return [
        DerivedQuestion(
            question_id=f"q{i + 1:03d}",
            question=f"질문 {i + 1}",
            category=_CAT_ERP,
            funnel=funnel,  # type: ignore[arg-type]
            is_selected=True,
        )
        for i, funnel in enumerate(funnels)
    ]


def _make_content_piece(
    content_id: str,
    funnel: str = "consideration",
    priority: float = 1.0,
    publish_date: str = "",
) -> ContentPiece:
    """테스트용 최소 ContentPiece."""
    return ContentPiece(
        content_id=content_id,
        question="테스트 질문",
        category="테스트 카테고리",
        funnel=funnel,  # type: ignore[arg-type]
        geo_type="definition",  # type: ignore[arg-type]
        publishing_purpose="테스트 목적",
        priority_score=priority,
        publish_date=publish_date,
    )


class TestStage5Balance:
    def test_funnel_distribution_counts(self, agent: PlannerAgent) -> None:
        """awareness/consideration/conversion 카운트가 정확하게 계산된다."""
        selected = _make_selected_dqs(["awareness", "consideration", "consideration", "conversion"])
        pi = _make_planner_input()
        _, dist, _ = agent._stage5_balance(selected, pi)
        assert dist.awareness == 1
        assert dist.consideration == 2
        assert dist.conversion == 1
        assert dist.total == 4

    def test_dominance_warning_logged(self, agent: PlannerAgent, caplog) -> None:
        """consideration ≥ 70% → WARNING 로그에 '편중' 포함."""
        selected = _make_selected_dqs(["consideration", "consideration", "consideration", "awareness"])
        pi = _make_planner_input()
        with caplog.at_level(logging.WARNING, logger="core.agents.planner.agent"):
            agent._stage5_balance(selected, pi)
        assert any("편중" in r.message for r in caplog.records)

    def test_no_warning_below_threshold(self, agent: PlannerAgent, caplog) -> None:
        """분포가 임계값 미만이면 편중 WARNING이 발생하지 않는다."""
        selected = _make_selected_dqs(["awareness", "consideration", "conversion", "awareness"])
        pi = _make_planner_input()
        with caplog.at_level(logging.WARNING, logger="core.agents.planner.agent"):
            agent._stage5_balance(selected, pi)
        assert not any("편중" in r.message for r in caplog.records)

    def test_no_autoswap(self, agent: PlannerAgent) -> None:
        """swap_enabled=false → selected 목록 순서·내용 변경 없음."""
        selected = _make_selected_dqs(["consideration"] * 8 + ["awareness"])
        pi = _make_planner_input()
        result_selected, _, _ = agent._stage5_balance(selected, pi)
        assert len(result_selected) == len(selected)
        for orig, res in zip(selected, result_selected):
            assert orig.question_id == res.question_id

    def test_prev_month_loaded(self, agent: PlannerAgent, tmp_path, monkeypatch) -> None:
        """직전 월 아카이브 파일이 있으면 prev_dist가 로드된다."""
        prev_plan = ContentPlan(
            run_date="2026-02-01",
            target_month="2026-02",
            client_name="wishket",
            funnel_distribution=FunnelDistribution(awareness=3, consideration=5, conversion=4),
        )
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        (runs_dir / "2026-02.json").write_text(prev_plan.model_dump_json(), encoding="utf-8")

        monkeypatch.setattr(agent, "_archive_runs_dir", str(runs_dir))
        selected = _make_selected_dqs(["consideration"])
        pi = _make_planner_input(target_month="2026-03")
        _, _, prev_dist = agent._stage5_balance(selected, pi)

        assert prev_dist is not None
        assert prev_dist.awareness == 3
        assert prev_dist.consideration == 5
        assert prev_dist.conversion == 4

    def test_prev_month_missing_no_error(self, agent: PlannerAgent, tmp_path, monkeypatch) -> None:
        """직전 월 아카이브 파일이 없어도 예외 없이 None을 반환한다."""
        runs_dir = tmp_path / "empty_runs"
        monkeypatch.setattr(agent, "_archive_runs_dir", str(runs_dir))
        selected = _make_selected_dqs(["consideration"])
        pi = _make_planner_input(target_month="2026-03")
        _, _, prev_dist = agent._stage5_balance(selected, pi)
        assert prev_dist is None

    def test_returns_funnel_distribution(self, agent: PlannerAgent) -> None:
        """반환값 두 번째 요소가 FunnelDistribution 인스턴스여야 한다."""
        selected = _make_selected_dqs(["awareness", "consideration"])
        pi = _make_planner_input()
        _, dist, _ = agent._stage5_balance(selected, pi)
        assert isinstance(dist, FunnelDistribution)

    def test_publish_dates_assigned(self, agent: PlannerAgent) -> None:
        """Stage 5 완료 후 선발 질문에 publish_date가 배정된다."""
        selected = _make_selected_dqs(["awareness", "consideration", "conversion"])
        pi = _make_planner_input(target_month="2026-03")
        result, _, _ = agent._stage5_balance(selected, pi)
        assigned = [dq for dq in result if dq.publish_date]
        assert len(assigned) == len(result), "모든 선발 질문에 publish_date가 있어야 한다"

    def test_publish_dates_are_mon_wed_fri(self, agent: PlannerAgent) -> None:
        """배정된 publish_date가 모두 월(0)/수(2)/금(4)이다."""
        selected = _make_selected_dqs(["awareness", "consideration", "conversion"])
        pi = _make_planner_input(target_month="2026-03")
        result, _, _ = agent._stage5_balance(selected, pi)
        for dq in result:
            if dq.publish_date:
                d = date.fromisoformat(dq.publish_date)
                assert d.weekday() in {0, 2, 4}, f"{dq.publish_date} 는 월/수/금이 아님"

    def test_funnel_alternation_in_publish_dates(self, agent: PlannerAgent) -> None:
        """교대 배치 가능한 경우 연속된 publish_date에 동일 퍼널이 배치되지 않는다."""
        selected = _make_selected_dqs(["awareness", "consideration", "awareness", "consideration"])
        pi = _make_planner_input(target_month="2026-03")
        result, _, _ = agent._stage5_balance(selected, pi)
        # publish_date 오름차순 정렬 후 퍼널 확인
        ordered = sorted([dq for dq in result if dq.publish_date], key=lambda x: x.publish_date)
        funnels = [dq.funnel for dq in ordered]
        for i in range(len(funnels) - 1):
            assert funnels[i] != funnels[i + 1], (
                f"연속 퍼널: 인덱스 {i}({funnels[i]})와 {i+1}({funnels[i+1]})"
            )


# ── archive ────────────────────────────────────────────────────────


class TestArchive:
    def test_save_and_load_plan(self, tmp_path) -> None:
        """save_plan → load_previous_funnel 왕복 시 퍼널 분포가 일치한다."""
        plan = ContentPlan(
            run_date="2026-03-01",
            target_month="2026-03",
            client_name="wishket",
            funnel_distribution=FunnelDistribution(awareness=2, consideration=6, conversion=4),
        )
        archive_mod.save_plan(plan, str(tmp_path))

        # 2026-04에서 이전 달(2026-03) 로드
        prev = archive_mod.load_previous_funnel("2026-04", str(tmp_path))
        assert prev is not None
        assert prev.awareness == 2
        assert prev.consideration == 6
        assert prev.conversion == 4

    def test_prev_month_calculation(self) -> None:
        """'YYYY-MM' → 직전 월 변환이 연도 경계를 포함해 정확하다."""
        assert archive_mod._prev_month("2026-03") == "2026-02"
        assert archive_mod._prev_month("2026-01") == "2025-12"

    def test_load_missing_returns_none(self, tmp_path) -> None:
        """아카이브 파일이 없으면 None을 반환한다."""
        result = archive_mod.load_previous_funnel("2026-03", str(tmp_path))
        assert result is None


# ── _stage7_calendar ───────────────────────────────────────────────


class TestStage7Calendar:
    """_stage7_calendar 단위 테스트.

    신규 동작: publish_date가 이미 설정된 ContentPiece를 CalendarEntry로 단순 조립한다.
    날짜 계산 없음 — publish_date가 없는 piece는 건너뛴다.
    """

    # 2026-03: 월(2), 수(4), 금(6)이 모두 존재하는 달
    _MON = "2026-03-02"
    _WED = "2026-03-04"
    _FRI = "2026-03-06"

    def test_calendar_only_mon_wed_fri(self, agent: PlannerAgent) -> None:
        """publish_date가 월/수/금으로 설정된 경우, CalendarEntry가 올바른 요일만 포함한다."""
        pieces = [
            _make_content_piece("c0", publish_date=self._MON),
            _make_content_piece("c1", publish_date=self._WED),
            _make_content_piece("c2", publish_date=self._FRI),
        ]
        pi = _make_planner_input(target_month="2026-03")
        entries = agent._stage7_calendar(pieces, pi)
        assert len(entries) == 3
        for e in entries:
            d = date.fromisoformat(e.date)
            assert d.weekday() in {0, 2, 4}, f"{e.date} 는 월/수/금이 아님 (weekday={d.weekday()})"

    def test_calendar_count_le_pieces(self, agent: PlannerAgent) -> None:
        """publish_date 없는 piece는 건너뛰므로 항목 수 ≤ pieces 수."""
        pieces = [
            _make_content_piece("c0", publish_date=self._MON),
            _make_content_piece("c1", publish_date=""),  # 건너뜀
            _make_content_piece("c2", publish_date=self._FRI),
        ]
        pi = _make_planner_input(target_month="2026-03")
        entries = agent._stage7_calendar(pieces, pi)
        assert len(entries) <= len(pieces)
        assert len(entries) == 2  # publish_date 있는 2건만

    def test_calendar_entry_fields(self, agent: PlannerAgent) -> None:
        """CalendarEntry의 date/day_of_week/content_id/is_holiday 필드가 올바르다."""
        pieces = [_make_content_piece("piece_001", publish_date=self._MON)]
        pi = _make_planner_input(target_month="2026-03")
        entries = agent._stage7_calendar(pieces, pi)

        assert len(entries) == 1
        e = entries[0]
        assert e.date == self._MON
        assert e.day_of_week == "월"
        assert e.content_id == "piece_001"
        assert e.is_holiday is False


# ── _stage7_document ───────────────────────────────────────────────


class TestStage7Document:
    """_stage7_document 단위 테스트 — LLM 기반 Markdown 기획 문서 생성.

    LLM 호출은 AsyncMock으로 대체하여 프롬프트 조립과 반환값 처리만 검증한다.
    """

    def _make_pieces(self) -> list[ContentPiece]:
        from core.schemas import H2Section, TitleSuggestion
        return [
            ContentPiece(
                content_id="q001",
                question="앱 개발 견적이 다른 이유는?",
                category="앱 개발 질문",
                funnel="consideration",
                geo_type="comparison",
                publishing_purpose="견적 판단 기준 제공",
                publish_date="2026-03-02",
                title_suggestions=[
                    TitleSuggestion(title="앱 개발 견적이 다른 5가지 이유", strategy="seo"),
                    TitleSuggestion(title="견적서만 봐선 모릅니다", strategy="ctr"),
                ],
                h2_structure=[
                    H2Section(heading="견적 차이의 원인", description=""),
                    H2Section(heading="비교 체크리스트", description=""),
                ],
                cta_suggestion="여러 업체 견적을 한 번에 비교하세요",
                priority_score=0.9,
                data_rationale="월 400회 검색, 안정적 트렌드",
            ),
        ]

    def _mock_doc(self) -> str:
        return (
            "# 2026년 3월 테스트 블로그 콘텐츠 전략\n\n"
            "## 1. 전략 요약\n\n"
            "- **이번 달 핵심 목표**: 비교 판단 의도 검색자 대상 12건 기획\n"
            "- **전략 방향**: 고려 10건 | 판단 기준 제시\n\n"
            "## 2. 콘텐츠 기획안 요약\n\n"
            "### 콘텐츠 목록\n\n"
            "| 발행일 | 카테고리 | 제안 제목 (SEO) | 하위 선정 질문 | 퍼널 | GEO |\n"
            "|--------|---------|----------------|--------------|------|-----|\n"
            "| 2026-03-02 | 앱 개발 질문 | 앱 개발 견적이 다른 5가지 이유 | 앱 개발 견적이 다른 이유는? | 고려 | 비교형 |\n\n"
            "### 주요 근거\n\n"
            "#### 1) 리서치 결과 기반\n\n"
            "선정 근거: 월 400회 검색, 안정적 트렌드\n\n"
            "#### 2) 기존 콘텐츠 기반\n\n"
            "기발행 콘텐츠 DB 미제공\n\n"
            "## 3. 콘텐츠 세부 기획\n\n"
            "### 앱 개발 질문\n\n"
            "#### [2026-03-02] 앱 개발 견적이 다른 이유는?\n\n"
            "- **발행 목적**: 견적 판단 기준 제공\n"
            "- **제목안**\n"
            "  - SEO: 앱 개발 견적이 다른 5가지 이유\n"
            "  - CTR: 견적서만 봐선 모릅니다\n"
            "- **H2 구조**\n"
            "  1. 견적 차이의 원인\n"
            "  2. 비교 체크리스트\n"
            "- **CTA**: 여러 업체 견적을 한 번에 비교하세요\n"
        )

    @pytest.mark.asyncio
    async def test_document_contains_sections(self, agent: PlannerAgent) -> None:
        """LLM mock → 반환 문자열에 3개 섹션 헤더가 모두 포함된다."""
        pieces = self._make_pieces()
        dist = FunnelDistribution(awareness=0, consideration=1, conversion=0)
        pi = _make_planner_input(target_month="2026-03")
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=self._mock_doc())):
            doc = await agent._stage7_document(pieces, dist, pi)
        assert "1. 전략 요약" in doc
        assert "2. 콘텐츠 기획안 요약" in doc
        assert "3. 콘텐츠 세부 기획" in doc

    @pytest.mark.asyncio
    async def test_document_includes_publish_dates(self, agent: PlannerAgent) -> None:
        """각 ContentPiece의 publish_date가 LLM 응답(mock)에 포함된다."""
        pieces = self._make_pieces()
        dist = FunnelDistribution(awareness=0, consideration=1, conversion=0)
        pi = _make_planner_input(target_month="2026-03")
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=self._mock_doc())):
            doc = await agent._stage7_document(pieces, dist, pi)
        for p in pieces:
            assert p.publish_date in doc, f"{p.publish_date} 가 문서에 없음"

    @pytest.mark.asyncio
    async def test_document_includes_title_and_h2(self, agent: PlannerAgent) -> None:
        """SEO 제목안과 H2 heading이 LLM 응답(mock)에 포함된다."""
        pieces = self._make_pieces()
        dist = FunnelDistribution(awareness=0, consideration=1, conversion=0)
        pi = _make_planner_input(target_month="2026-03")
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=self._mock_doc())):
            doc = await agent._stage7_document(pieces, dist, pi)
        assert "앱 개발 견적이 다른 5가지 이유" in doc
        assert "견적 차이의 원인" in doc
        assert "비교 체크리스트" in doc


class TestStage6Structure:
    """_stage6_structure 단위 테스트.

    LLM 호출은 AsyncMock으로 대체. 가이드 파일은 실제 파일 로드.
    """

    def _make_selected(self) -> list[DerivedQuestion]:
        return [
            DerivedQuestion(
                question_id="q001",
                question="앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?",
                category="앱 개발 관련 질문",
                funnel="consideration",
                is_selected=True,
                priority_score=0.8,
                selection_rationale="높은 검색량 + 경쟁도 낮음",
                source_cluster_id="c1",
                mapping_rationale="naver_top_titles 상위 3개가 업체 비교 관련",
                funnel_journey_reasoning="선택지 인식 후 기준 세우는 단계 → consideration",
            )
        ]

    def _make_pi_with_cluster(self) -> PlannerInput:
        cluster = Cluster(
            cluster_id="c1",
            representative_keyword="앱 개발 견적",
            total_volume_naver=5000,
            keywords=[
                ClusterKeyword(keyword="앱 개발 견적"),
                ClusterKeyword(keyword="앱 개발 비용"),
                ClusterKeyword(keyword="모바일 앱 개발 가격"),
            ],
            h2_topics=["짧", "h2노이즈", "앱"],  # 3자 이하 노이즈 — stage6에서 사용 안 함
        )
        return _make_planner_input(research_result=_make_research_result(clusters=[cluster]))

    def _mock_llm_response(self, geo_type: str = "comparison") -> str:
        return json.dumps({
            "structures": [{
                "question_id": "q001",
                "geo_type": geo_type,
                "publishing_purpose": "견적 판단 기준 제공",
                "h2_structure": [
                    {"heading": "앱 개발 견적, 왜 업체마다 다를까?", "description": "원인 소개", "geo_pattern": None},
                    {"heading": "견적에 영향을 주는 5가지 요소", "description": "기능·디자인·기술스택 비교", "geo_pattern": "comparison"},
                    {"heading": "견적서 비교 체크리스트", "description": "고유 각도", "geo_pattern": None},
                ],
                "title_suggestions": [
                    {"title": "앱 개발 견적이 업체마다 다른 이유 5가지", "strategy": "seo"},
                    {"title": "견적서만 봐선 모릅니다", "strategy": "ctr"},
                ],
                "cta_suggestion": "여러 업체 견적을 한 번에 비교해보세요",
            }]
        })

    @pytest.mark.asyncio
    async def test_stage6_returns_content_pieces(self, agent: PlannerAgent) -> None:
        """LLM mock → ContentPiece 리스트 반환, 기본 필드 + 근거 필드 확인."""
        selected = self._make_selected()
        pi = self._make_pi_with_cluster()
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=self._mock_llm_response())):
            pieces = await agent._stage6_structure(selected, pi)
        assert len(pieces) == 1
        piece = pieces[0]
        assert isinstance(piece, ContentPiece)
        assert piece.content_id == "q001"
        assert piece.question == selected[0].question
        assert piece.funnel == "consideration"
        # 1~2단계 근거가 ContentPiece에 전달됨
        assert piece.mapping_rationale == "naver_top_titles 상위 3개가 업체 비교 관련"
        assert "consideration" in piece.funnel_journey_reasoning

    @pytest.mark.asyncio
    async def test_stage6_geo_type_from_llm(self, agent: PlannerAgent) -> None:
        """geo_type은 LLM 응답값을 그대로 사용한다 (규칙 함수 없음)."""
        selected = self._make_selected()
        pi = self._make_pi_with_cluster()
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=self._mock_llm_response("definition"))):
            pieces = await agent._stage6_structure(selected, pi)
        assert pieces[0].geo_type == "definition"

    @pytest.mark.asyncio
    async def test_stage6_payload_fields(self, agent: PlannerAgent) -> None:
        """페이로드에 h2_topics 대신 cluster.keywords, 선정 근거, 구글 신호가 전달된다."""
        selected = self._make_selected()
        pi = self._make_pi_with_cluster()
        captured: dict = {}

        async def mock_llm(label: str, system: str, user: str, **kwargs) -> str:
            captured["user"] = user
            return self._mock_llm_response()

        with patch.object(agent, "_llm_call", new=mock_llm):
            await agent._stage6_structure(selected, pi)

        payload = json.loads(captured["user"])
        q = payload["questions"][0]
        # 클러스터 키워드 (h2_topics 아님)
        assert "cluster_keywords" in q
        assert "앱 개발 견적" in q["cluster_keywords"]
        assert "h2_topics" not in q
        # 선정 근거 필드
        assert q["mapping_rationale"] == "naver_top_titles 상위 3개가 업체 비교 관련"
        assert "consideration" in q["funnel_journey_reasoning"]
        # 수요 신호: 절대값 아닌 상대 티어
        assert "naver_volume_tier" in q
        assert q["naver_volume_tier"] in ("높음", "중간", "낮음")
        assert "naver_monthly_volume" not in q  # 절대값 전달 금지
        assert "google_paa_count" in q
        assert "google_featured_snippet" in q
        assert "google_ai_overview" in q

    @pytest.mark.asyncio
    async def test_stage6_h2_structure_populated(self, agent: PlannerAgent) -> None:
        """h2_structure에 H2Section이 3개 이상 채워진다."""
        selected = self._make_selected()
        pi = self._make_pi_with_cluster()
        with patch.object(agent, "_llm_call", new=AsyncMock(return_value=self._mock_llm_response())):
            pieces = await agent._stage6_structure(selected, pi)
        assert len(pieces[0].h2_structure) >= 3

    @pytest.mark.asyncio
    async def test_stage6_system_prompt_includes_guides(self, agent: PlannerAgent) -> None:
        """시스템 프롬프트에 geo_classification + content_direction 가이드가 포함된다."""
        selected = self._make_selected()
        pi = self._make_pi_with_cluster()
        captured: dict = {}

        async def mock_llm(label: str, system: str, user: str, **kwargs) -> str:
            captured["system"] = system
            return self._mock_llm_response()

        with patch.object(agent, "_llm_call", new=mock_llm):
            await agent._stage6_structure(selected, pi)

        system = captured["system"]
        assert "GEO 유형" in system       # geo_classification.md 포함
        assert "퍼널" in system           # content_direction.md 포함

    @pytest.mark.asyncio
    async def test_stage6_empty_selected_returns_empty(self, agent: PlannerAgent) -> None:
        """선발 질문 없으면 LLM 호출 없이 빈 목록 반환."""
        pi = _make_planner_input()
        mock_llm = AsyncMock()
        with patch.object(agent, "_llm_call", new=mock_llm):
            pieces = await agent._stage6_structure([], pi)
        assert pieces == []
        mock_llm.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
