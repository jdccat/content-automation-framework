"""리서처 에이전트 v5 (spec v2) 테스트.

단위 테스트: 순수 함수 (_parse_input, etc.)
통합 테스트: 모킹된 도구+LLM으로 run() 전체 파이프라인 → ResearchResult 스키마 검증
미구현 도구 스킵 테스트: NotImplementedError 발생해도 파이프라인 완주 확인
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.agents.researcher.agent import (
    ResearcherAgent,
    _chunk,
    _extract_domain,
    _extract_keywords_from_question,
    _is_relevant_serp_item,
    _keyword_to_question,
    _map_content_type,
    _normalize_keyword,
    _parse_web_fetch_result,
    _strip_particle,
)
from core.agents.researcher.stage1 import _rule_based_prefilter
from core.schemas import (
    ClusterDraft,
    ParsedInput,
    RawKeywordPool,
    ResearchResult,
    Stage1Output,
    Stage2Output,
    Stage3Output,
)


# ── 단위 테스트: _parse_input ───────────────────────────────────


class TestParseInput:
    def test_label_format_korean(self):
        parsed = ResearcherAgent._parse_input(
            "키워드: 앱 개발, 모먼트: 비용 문의"
        )
        assert isinstance(parsed, ParsedInput)
        assert parsed.main_keyword == "앱 개발"
        assert parsed.entry_moment == "비용 문의"

    def test_label_format_english(self):
        parsed = ResearcherAgent._parse_input(
            "keyword: app development, moment: cost inquiry"
        )
        assert parsed.main_keyword == "app development"
        assert parsed.entry_moment == "cost inquiry"

    def test_comma_separated(self):
        parsed = ResearcherAgent._parse_input("앱 개발, 비용 문의")
        assert parsed.main_keyword == "앱 개발"
        assert parsed.entry_moment == "비용 문의"

    def test_newline_separated(self):
        parsed = ResearcherAgent._parse_input("앱 개발\n비용 문의")
        assert parsed.main_keyword == "앱 개발"
        assert parsed.entry_moment == "비용 문의"

    def test_fallback_single_string(self):
        parsed = ResearcherAgent._parse_input("앱 개발")
        assert parsed.main_keyword == "앱 개발"
        assert parsed.entry_moment == "general"

    def test_empty_string(self):
        parsed = ResearcherAgent._parse_input("")
        assert parsed.main_keyword == ""
        assert parsed.entry_moment == "general"

    def test_whitespace_handling(self):
        parsed = ResearcherAgent._parse_input(
            "  키워드:  앱 개발  ,  모먼트:  비용 문의  "
        )
        assert parsed.main_keyword == "앱 개발"
        assert parsed.entry_moment == "비용 문의"


# ── 단위 테스트: _keyword_to_question ───────────────────────────


class TestKeywordToQuestion:
    def test_short_keyword(self):
        assert _keyword_to_question("앱 개발") == "앱 개발란?"

    def test_long_keyword(self):
        assert (
            _keyword_to_question("앱 개발 비용 절감 방법")
            == "앱 개발 비용 절감 방법에 대해 알려주세요"
        )

    def test_already_question(self):
        assert _keyword_to_question("앱 개발이란?") == "앱 개발이란?"

    def test_single_word(self):
        assert _keyword_to_question("개발") == "개발란?"


# ── 단위 테스트: 유틸리티 함수 ──────────────────────────────────


class TestUtilities:
    def test_chunk(self):
        assert _chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
        assert _chunk([], 3) == []
        assert _chunk([1], 5) == [[1]]

    def test_normalize_keyword(self):
        assert _normalize_keyword("  앱  개발  ") == "앱 개발"
        assert _normalize_keyword("APP 개발") == "app 개발"

    def test_extract_domain(self):
        assert _extract_domain("https://www.example.com/path") == "www.example.com"
        assert _extract_domain("") == ""

    def test_map_content_type(self):
        assert _map_content_type("blog") == "corporate_blog"
        assert _map_content_type("news") == "media"
        assert _map_content_type("unknown") == "other"

    def test_parse_web_fetch_result(self):
        text = (
            "URL: https://example.com\n"
            'H2 구조: ["섹션1", "섹션2"]\n'
            "글자 수: 5000\n"
            "본문 (앞 500자):\n"
            "본문 내용입니다."
        )
        result = _parse_web_fetch_result(text)
        assert result["url"] == "https://example.com"
        assert result["h2_structure"] == ["섹션1", "섹션2"]
        assert result["char_count"] == 5000
        assert result["body"] == "본문 내용입니다."

    def test_parse_web_fetch_result_empty(self):
        result = _parse_web_fetch_result("")
        assert result["url"] == ""
        assert result["h2_structure"] == []
        assert result["char_count"] == 0


# ── 단위 테스트: _deduplicate_keywords ──────────────────────────


class TestDeduplicateKeywords:
    def test_removes_duplicates(self):
        pool = RawKeywordPool(
            google=["앱 개발", "앱  개발", "APP 개발"],
            naver=["앱 개발"],
            internal_data=["앱 개발"],
        )
        result = ResearcherAgent._deduplicate_keywords(pool)
        normalized_kws = [kw for kw, _ in result]
        assert len(set(normalized_kws)) == len(normalized_kws)
        assert "앱 개발" in normalized_kws
        assert "app 개발" in normalized_kws

    def test_preserves_first_source(self):
        pool = RawKeywordPool(
            google=["앱 개발"],
            internal_data=["앱 개발"],
        )
        result = dict(ResearcherAgent._deduplicate_keywords(pool))
        assert result["앱 개발"] == "google"

    def test_naver_source(self):
        pool = RawKeywordPool(
            naver=["네이버 키워드"],
        )
        result = dict(ResearcherAgent._deduplicate_keywords(pool))
        assert result["네이버 키워드"] == "naver"

    def test_keyword_tool_source(self):
        pool = RawKeywordPool(
            keyword_tool=["검색광고 키워드"],
        )
        result = dict(ResearcherAgent._deduplicate_keywords(pool))
        assert result["검색광고 키워드"] == "keyword_tool"

    def test_empty_pool(self):
        pool = RawKeywordPool()
        assert ResearcherAgent._deduplicate_keywords(pool) == []


# ── 단위 테스트: _strip_particle ──────────────────────────────


class TestStripParticle:
    def test_을(self):
        assert _strip_particle("업체를") == "업체"

    def test_이(self):
        assert _strip_particle("견적이") == "견적"

    def test_에서(self):
        assert _strip_particle("프로젝트에서") == "프로젝트"

    def test_에서는(self):
        assert _strip_particle("프로젝트에서는") == "프로젝트"

    def test_no_particle(self):
        assert _strip_particle("ERP") == "ERP"

    def test_short_token_preserves(self):
        assert _strip_particle("값을") == "값"

    def test_single_char_no_strip(self):
        assert _strip_particle("을") == "을"

    def test_마다(self):
        assert _strip_particle("업체마다") == "업체"

    def test_으로(self):
        assert _strip_particle("기준으로") == "기준"


# ── 단위 테스트: _extract_keywords_from_question ─────────────────


class TestExtractKeywordsFromQuestion:
    def test_erp_question(self):
        q = "ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?"
        result = _extract_keywords_from_question(q)
        assert "ERP 외주 개발 업체" in result
        assert "외주 개발 업체" in result
        assert "ERP 외주 개발" in result
        assert "외주 개발" in result
        assert "기준" in result

    def test_app_cost_question(self):
        q = "앱 개발 견적이 업체마다 다른 이유는 무엇이며, 적정 비용을 판단할 기준은?"
        result = _extract_keywords_from_question(q)
        assert "앱 개발 견적" in result
        assert "앱 개발" in result
        assert "업체" in result
        assert "기준" in result

    def test_outsource_project_question(self):
        q = "외주 개발 프로젝트를 진행할 때 자주 발생하는 문제는 무엇인가요?"
        result = _extract_keywords_from_question(q)
        assert "외주 개발 프로젝트" in result
        assert "외주 개발" in result
        assert "문제" in result

    def test_empty_question(self):
        assert _extract_keywords_from_question("") == []

    def test_english_only(self):
        result = _extract_keywords_from_question("What is ERP?")
        assert isinstance(result, list)

    def test_no_verbs_in_output(self):
        q = "외주 개발을 진행하는 방법은?"
        result = _extract_keywords_from_question(q)
        for kw in result:
            for token in kw.split():
                assert not token.endswith("하는")
                assert not token.endswith("진행")


# ── 단위 테스트: 구조화 입력 _parse_input ────────────────────────


class TestParseInputStructured:
    STRUCTURED_INPUT = (
        "질문 의도 : ERP 외주 개발 업체 선정 기준 파악\n"
        "질문 형태 :\n"
        "1. ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?\n"
        "2. 앱 개발 견적이 업체마다 다른 이유는 무엇이며, 적정 비용을 판단할 기준은?\n"
        "3. 외주 개발 프로젝트를 진행할 때 자주 발생하는 문제는 무엇인가요?\n"
        "콘텐츠 방향성 : 실무 가이드 중심, 위시켓 사례 활용"
    )

    def test_intent_extracted(self):
        parsed = ResearcherAgent._parse_input(self.STRUCTURED_INPUT)
        assert parsed.intent == "ERP 외주 개발 업체 선정 기준 파악"

    def test_direction_extracted(self):
        parsed = ResearcherAgent._parse_input(self.STRUCTURED_INPUT)
        assert parsed.direction == "실무 가이드 중심, 위시켓 사례 활용"

    def test_questions_extracted(self):
        parsed = ResearcherAgent._parse_input(self.STRUCTURED_INPUT)
        assert len(parsed.questions) == 3
        assert any("ERP" in q for q in parsed.questions)

    def test_seeds_non_empty(self):
        parsed = ResearcherAgent._parse_input(self.STRUCTURED_INPUT)
        assert len(parsed.extracted_seeds) > 0
        seeds_str = " ".join(parsed.extracted_seeds)
        assert "외주 개발" in seeds_str

    def test_main_keyword_multi_token(self):
        parsed = ResearcherAgent._parse_input(self.STRUCTURED_INPUT)
        assert len(parsed.main_keyword.split()) >= 2

    def test_single_question(self):
        text = (
            "질문 의도 : 비용 이해\n"
            "질문 형태 :\n"
            "1. 앱 개발 비용은 얼마인가요?\n"
            "콘텐츠 방향성 : 초보자 가이드"
        )
        parsed = ResearcherAgent._parse_input(text)
        assert parsed.intent == "비용 이해"
        assert len(parsed.questions) == 1
        assert parsed.direction == "초보자 가이드"

    def test_entry_moment_equals_intent(self):
        parsed = ResearcherAgent._parse_input(self.STRUCTURED_INPUT)
        assert parsed.entry_moment == parsed.intent


class TestParseInputStructuredBackwardCompat:
    """구형 포맷이 ParsedInput으로 여전히 동작하는지 확인."""

    def test_old_label_format(self):
        parsed = ResearcherAgent._parse_input(
            "키워드: 외주 개발, 모먼트: 비교 판단"
        )
        assert parsed.main_keyword == "외주 개발"
        assert parsed.entry_moment == "비교 판단"
        assert parsed.extracted_seeds == []
        assert parsed.questions == []

    def test_old_simple_format(self):
        parsed = ResearcherAgent._parse_input("외주 개발")
        assert parsed.main_keyword == "외주 개발"
        assert parsed.entry_moment == "general"
        assert parsed.extracted_seeds == []


# ── 통합 테스트: 전체 파이프라인 ────────────────────────────────

# 모든 도구를 모킹하는 헬퍼


def _mock_autocomplete(keyword: str):
    """autocomplete mock: 시드에서 파생 키워드 2개 생성."""
    return json.dumps({
        "keyword": keyword,
        "naver": [f"{keyword} 비용", f"{keyword} 방법"],
        "google": [f"{keyword} 가이드", f"{keyword} 추천"],
    })


def _mock_google_search(query: str, num: int = 5):
    """google_search mock: 키워드별 SERP 결과 반환."""
    base = hash(query) % 100
    items = [
        {
            "title": f"{query} 관련 글 {i}",
            "link": f"https://site{base + i}.com/page",
            "snippet": f"{query}에 대한 설명 {i}",
            "content_type": "blog",
        }
        for i in range(min(num, 5))
    ]
    return json.dumps({
        "query": query,
        "total": len(items),
        "items": items,
        "serp_features": {
            "content_type_distribution": {"blog": len(items)},
            "ai_overview": False,
            "featured_snippet_exists": False,
            "featured_snippet_url": None,
            "paa_questions": [],
            "has_video": False,
            "has_news": False,
            "domains": [f"site{base + i}.com" for i in range(len(items))],
        },
    })


def _mock_naver_searchad(keywords: list[str]):
    """naver_searchad mock: 키워드별 검색량 반환 (PC/모바일 분리)."""
    input_kws = [
        {
            "keyword": kw,
            "monthly_pc": 100,
            "monthly_mobile": 900,
            "monthly_total": 1000,
            "competition": "medium",
        }
        for kw in keywords
    ]
    related = [
        {
            "keyword": f"{keywords[0]} 연관어",
            "monthly_pc": 50,
            "monthly_mobile": 450,
            "monthly_total": 500,
        },
    ] if keywords else []
    return json.dumps({
        "input_keywords": input_kws,
        "related_keywords": related,
    })


def _mock_google_trends(keywords: list[str]):
    """google_trends mock."""
    trends = {
        kw: {"average": 50.0, "series": [], "direction": "stable"}
        for kw in keywords
    }
    return json.dumps({
        "trends": trends,
        "related_queries": {kw: [] for kw in keywords},
    })


def _mock_naver_datalab(keywords):
    """naver_datalab mock."""
    return json.dumps({
        kw if isinstance(kw, str) else kw[0]: {
            "average": 60.0,
            "series": [],
            "direction": "rising",
        }
        for kw in keywords
    })


def _mock_web_fetch(url: str, max_chars: int = 5000):
    """web_fetch mock."""
    return (
        f"URL: {url}\n"
        'H2 구조: ["섹션1", "섹션2"]\n'
        "글자 수: 3000\n"
        f"본문 (앞 {max_chars}자):\n"
        "테스트 본문 내용입니다."
    )


def _mock_naver_blog_search(query: str, display: int = 5):
    """naver_blog_search mock."""
    items = [
        {
            "title": f"{query} 블로그 {i}",
            "link": f"https://blog.naver.com/post{i}",
            "description": f"{query} 관련 내용 {i}",
            "postdate": "20260201",
            "blogger_name": f"블로거{i}",
        }
        for i in range(min(display, 5))
    ]
    return json.dumps({
        "query": query,
        "total": len(items),
        "items": items,
    })


def _mock_google_related(keyword: str):
    """google_related mock."""
    return json.dumps({
        "keyword": keyword,
        "related_searches": [
            f"{keyword} 비용",
            f"{keyword} 후기",
            f"{keyword} 비교",
            f"{keyword} 추천",
            f"{keyword} 장단점",
        ],
    }, ensure_ascii=False)


def _mock_google_paa(keyword: str):
    """google_paa mock."""
    return json.dumps({
        "keyword": keyword,
        "questions": [
            f"{keyword} 비용은 얼마인가요?",
            f"{keyword} 어떻게 시작하나요?",
            f"{keyword} 장점과 단점은?",
            f"{keyword} 업체 선정 기준은?",
        ],
    }, ensure_ascii=False)


def _mock_ai_search(query: str, num: int = 5):
    """ai_search mock."""
    return json.dumps({
        "query": query,
        "answer": f"{query}에 대한 답변입니다. 출처를 참고하세요.",
        "citations": ["https://cite1.com", "https://cite2.com"],
        "citation_details": [
            {"url": "https://cite1.com", "title": "출처1", "context_snippet": f"{query}에 대한 답변입니다."},
            {"url": "https://cite2.com", "title": "출처2", "context_snippet": "출처를 참고하세요."},
        ],
        "total": 2,
    })


def _mock_perplexity(query: str, num: int = 5):
    """perplexity mock."""
    return json.dumps({
        "query": query,
        "answer": f"{query} 답변",
        "citations": ["https://pplx1.com", "https://pplx2.com"],
        "total": 2,
    })


def _mock_llm_clustering_response(keywords: list[str]) -> str:
    """LLM 클러스터링 mock: 키워드를 3개씩 묶어서 클러스터 생성."""
    clusters = []
    orphans = []
    for i in range(0, len(keywords) - 2, 3):
        clusters.append({
            "keywords": keywords[i : i + 3],
            "shared_intent": f"{keywords[i]} 관련 정보 탐색",
        })
    remainder = len(keywords) % 3
    if remainder:
        orphans.extend(keywords[-remainder:])
    return json.dumps({"clusters": clusters, "orphans": orphans}, ensure_ascii=False)


def _mock_llm_representative_response(clusters_input: list[dict]) -> str:
    """LLM 대표 선정 mock."""
    results = []
    for c in clusters_input:
        kws = c.get("keywords", [])
        first = kws[0] if kws else ""
        # keywords가 {"keyword": ..., "volume": ...} dict 형태일 수 있음
        if isinstance(first, dict):
            first = first.get("keyword", "")
        results.append({
            "id": c["id"],
            "representative": first,
            "rationale": "가장 포괄적인 키워드",
        })
    return json.dumps(results, ensure_ascii=False)


def _mock_llm_archive_response(new_clusters: list[dict]) -> str:
    """LLM 아카이브 비교 mock: 전부 new."""
    return json.dumps(
        [
            {"new_rep": c.get("representative", ""), "verdict": "new", "matched_archive_rep": ""}
            for c in new_clusters
        ],
        ensure_ascii=False,
    )


def _mock_llm_focus_response(clusters: list[dict]) -> str:
    """LLM 포커스 선정 mock: 전부 focus=true."""
    return json.dumps(
        [{"id": c["id"], "focus": True} for c in clusters],
        ensure_ascii=False,
    )


# 도구 패치 경로
_TOOL_BASE = "core.agents.researcher.agent"


@pytest.fixture(autouse=True)
def _no_archive(monkeypatch):
    """통합 테스트에서 아카이브 파일 생성 방지."""
    monkeypatch.setattr(ResearcherAgent, "_save_archive", lambda self, r, s=None: None)
    monkeypatch.setattr(ResearcherAgent, "_load_archive_reps", lambda self: [])
    monkeypatch.setattr(ResearcherAgent, "_load_archive_clusters", lambda self: {})


@pytest.fixture
def mock_all_tools():
    """모든 도구 + LLM 호출을 mock으로 교체."""

    async def _mock_llm_call(self, label, system, user, model="", max_tokens=4096):
        """LLM 호출 mock: label에 따라 적절한 응답 반환."""
        if "domain_filter" in label:
            # 도메인 필터: 테스트에서는 전부 통과 (mock 데이터는 이미 도메인 내)
            return user
        elif "seed_filter" in label:
            # 시드 필터: 순수 수식어/의문사만 제거, 나머지 keep
            drop = {"이유", "기준", "방법", "문제", "무엇", "어떻게", "가장", "중요"}
            try:
                seeds = json.loads(user)
                return json.dumps([s for s in seeds if s not in drop], ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                return user
        elif "clustering" in label:
            try:
                data = json.loads(user.split("\n")[0])
                keywords = data.get("keywords", [])
            except (json.JSONDecodeError, IndexError):
                keywords = []
            return _mock_llm_clustering_response(keywords)
        elif "representative" in label:
            try:
                data = json.loads(user.split("\n")[0])
                clusters = data.get("clusters", [])
            except (json.JSONDecodeError, IndexError):
                clusters = []
            return _mock_llm_representative_response(clusters)
        elif "archive" in label:
            try:
                data = json.loads(user.split("\n")[0])
                new_reps = data.get("new_clusters", [])
            except (json.JSONDecodeError, IndexError):
                new_reps = []
            return _mock_llm_archive_response(new_reps)
        elif "focus" in label:
            try:
                data = json.loads(user.split("\n")[0])
                clusters = data.get("clusters", [])
            except (json.JSONDecodeError, IndexError):
                clusters = []
            return _mock_llm_focus_response(clusters)
        return ""

    with (
        patch(f"{_TOOL_BASE}.search_suggestions", new_callable=AsyncMock) as ac,
        patch(f"{_TOOL_BASE}.google_related_searches", new_callable=AsyncMock) as rel,
        patch(f"{_TOOL_BASE}.google_paa", new_callable=AsyncMock) as paa,
        patch(f"{_TOOL_BASE}.naver_keyword_volume", new_callable=AsyncMock) as vol,
        patch(f"{_TOOL_BASE}.google_search", new_callable=AsyncMock) as gs,
        patch(f"{_TOOL_BASE}.google_keyword_trend", new_callable=AsyncMock) as gt,
        patch(f"{_TOOL_BASE}.naver_keyword_trend", new_callable=AsyncMock) as nt,
        patch(f"{_TOOL_BASE}.web_fetch", new_callable=AsyncMock) as wf,
        patch(f"{_TOOL_BASE}.naver_blog_search", new_callable=AsyncMock) as ns,
        patch(f"{_TOOL_BASE}.ai_search", new_callable=AsyncMock) as ai,
        patch(f"{_TOOL_BASE}.perplexity_search", new_callable=AsyncMock) as pplx,
        patch.object(ResearcherAgent, "_llm_call", _mock_llm_call),
    ):
        ac.side_effect = lambda kw: _mock_autocomplete(kw)
        rel.side_effect = lambda kw: _mock_google_related(kw)
        paa.side_effect = lambda kw: _mock_google_paa(kw)
        vol.side_effect = lambda kws: _mock_naver_searchad(kws)
        gs.side_effect = lambda q, num=5: _mock_google_search(q, num)
        gt.side_effect = lambda kws: _mock_google_trends(kws)
        nt.side_effect = lambda kws: _mock_naver_datalab(kws)
        wf.side_effect = lambda url, mc=5000: _mock_web_fetch(url, mc)
        ns.side_effect = lambda q, d=5: _mock_naver_blog_search(q, d)
        ai.side_effect = lambda q, n=5: _mock_ai_search(q, n)
        pplx.side_effect = lambda q, n=5: _mock_perplexity(q, n)

        yield {
            "autocomplete": ac,
            "google_related": rel,
            "google_paa": paa,
            "naver_searchad": vol,
            "google_search": gs,
            "google_trends": gt,
            "naver_datalab": nt,
            "web_fetch": wf,
            "naver_search": ns,
            "ai_search": ai,
            "perplexity": pplx,
        }


@pytest.mark.asyncio
async def test_full_pipeline_produces_valid_result(mock_all_tools):
    """전체 파이프라인이 유효한 ResearchResult를 반환하는지 검증."""
    agent = ResearcherAgent()
    result = await agent.run("키워드: 앱 개발, 모먼트: 비용 문의")

    assert isinstance(result, ResearchResult)
    assert result.main_keyword == "앱 개발"
    assert result.entry_moment == "비용 문의"
    assert result.run_date

    # 스키마 직렬화 검증
    data = result.model_dump()
    assert "clusters" in data
    assert "orphan_keywords" in data
    assert isinstance(data["clusters"], list)

    # spec v2: 구글/네이버 분리 필드 존재 확인
    for cluster in data["clusters"]:
        assert "google_content_meta" in cluster
        assert "naver_content_meta" in cluster
        assert "google_serp_features" in cluster
        assert "naver_serp_features" in cluster
        assert "total_volume_naver_pc" in cluster
        assert "total_volume_naver_mobile" in cluster


@pytest.mark.asyncio
async def test_pipeline_with_not_implemented_tools(mock_all_tools):
    """미구현 도구가 있어도 파이프라인이 끝까지 실행."""
    agent = ResearcherAgent()
    result = await agent.run("앱 개발")

    assert isinstance(result, ResearchResult)
    assert result.main_keyword == "앱 개발"


@pytest.mark.asyncio
async def test_pipeline_all_tools_fail():
    """모든 도구가 실패해도 빈 ResearchResult 반환."""

    async def _fail_llm(self, label, system, user, model="", max_tokens=4096):
        return ""

    with (
        patch(f"{_TOOL_BASE}.search_suggestions", new_callable=AsyncMock) as ac,
        patch(f"{_TOOL_BASE}.google_related_searches", new_callable=AsyncMock) as rel,
        patch(f"{_TOOL_BASE}.google_paa", new_callable=AsyncMock) as paa,
        patch(f"{_TOOL_BASE}.naver_keyword_volume", new_callable=AsyncMock) as vol,
        patch(f"{_TOOL_BASE}.google_search", new_callable=AsyncMock) as gs,
        patch(f"{_TOOL_BASE}.google_keyword_trend", new_callable=AsyncMock) as gt,
        patch(f"{_TOOL_BASE}.naver_keyword_trend", new_callable=AsyncMock) as nt,
        patch(f"{_TOOL_BASE}.web_fetch", new_callable=AsyncMock) as wf,
        patch(f"{_TOOL_BASE}.naver_blog_search", new_callable=AsyncMock) as ns,
        patch(f"{_TOOL_BASE}.ai_search", new_callable=AsyncMock) as ai,
        patch(f"{_TOOL_BASE}.perplexity_search", new_callable=AsyncMock) as pplx,
        patch.object(ResearcherAgent, "_llm_call", _fail_llm),
    ):
        for mock_fn in [ac, rel, paa, vol, gs, gt, nt, wf, ns, ai, pplx]:
            mock_fn.side_effect = RuntimeError("도구 실패")

        agent = ResearcherAgent()
        result = await agent.run("앱 개발")

        assert isinstance(result, ResearchResult)
        assert result.main_keyword == "앱 개발"


@pytest.mark.asyncio
async def test_geo_citations_collected(mock_all_tools):
    """3단계 GEO 인용이 수집되는지 확인."""
    agent = ResearcherAgent()
    result = await agent.run("앱 개발, 비용 문의")

    for cluster in result.clusters:
        if cluster.geo_citations:
            assert all(c.url for c in cluster.geo_citations)
            assert all(c.domain for c in cluster.geo_citations)
            # spec v2: source 필드에 chatgpt/perplexity 중 하나
            assert all(c.source in ("chatgpt", "perplexity", "claude", "gemini")
                       for c in cluster.geo_citations)
            break


@pytest.mark.asyncio
async def test_naver_content_meta_collected(mock_all_tools):
    """2단계 네이버 상위 콘텐츠가 수집되는지 확인."""
    agent = ResearcherAgent()
    result = await agent.run("앱 개발, 비용 문의")

    for cluster in result.clusters:
        if cluster.naver_content_meta:
            meta = cluster.naver_content_meta[0]
            assert meta.platform == "naver"
            assert meta.url
            break


@pytest.mark.asyncio
async def test_google_content_meta_collected(mock_all_tools):
    """2단계 구글 상위 콘텐츠가 수집되는지 확인."""
    agent = ResearcherAgent()
    result = await agent.run("앱 개발, 비용 문의")

    for cluster in result.clusters:
        if cluster.google_content_meta:
            meta = cluster.google_content_meta[0]
            assert meta.platform == "google"
            assert meta.url
            break


@pytest.mark.asyncio
async def test_quality_gate():
    """품질 게이트 로직 검증."""
    agent = ResearcherAgent()

    empty_result = ResearchResult(
        run_date="2026-01-01",
        main_keyword="test",
        entry_moment="test",
    )
    assert agent._check_quality_gate(empty_result) is False

    from core.schemas import Cluster, ClusterKeyword

    result_with_small = ResearchResult(
        run_date="2026-01-01",
        main_keyword="test",
        entry_moment="test",
        clusters=[
            Cluster(
                cluster_id="c000",
                representative_keyword="test",
                keywords=[
                    ClusterKeyword(keyword="test"),
                ],
            ),
        ],
    )
    assert agent._check_quality_gate(result_with_small) is False


@pytest.mark.asyncio
async def test_safe_tool_call_isolates_errors():
    """_safe_tool_call이 에러를 격리하는지 확인."""

    async def raise_not_impl():
        raise NotImplementedError

    async def raise_runtime():
        raise RuntimeError("test error")

    async def succeed():
        return "ok"

    assert await ResearcherAgent._safe_tool_call("t1", raise_not_impl(), "default") == "default"
    assert await ResearcherAgent._safe_tool_call("t2", raise_runtime(), "default") == "default"
    assert await ResearcherAgent._safe_tool_call("t3", succeed(), "default") == "ok"


@pytest.mark.asyncio
async def test_archive_load_nonexistent():
    """아카이브 파일이 없으면 빈 리스트 반환."""
    agent = ResearcherAgent()
    agent._archive_cfg = {
        "runs_dir": "/tmp/nonexistent_test_dir/runs",
        "index_file": "/tmp/nonexistent_test_dir/index.json",
    }
    reps = agent._load_archive_reps()
    assert reps == []


# ── 단위 테스트: _filter_archive_seeds ─────────────────────────


class TestFilterArchiveSeeds:
    def test_keeps_relevant_seeds(self):
        archive = [
            "외주 개발 비용",
            "ERP 개발 업체",
            "앱 개발 외주",
            "모바일 신분증",
            "이러닝센터",
        ]
        ref = ["외주 개발", "ERP 외주 개발 업체", "앱 개발 견적"]
        result = ResearcherAgent._filter_archive_seeds(archive, ref)
        assert "외주 개발 비용" in result
        assert "ERP 개발 업체" in result
        assert "앱 개발 외주" in result
        assert "모바일 신분증" not in result
        assert "이러닝센터" not in result

    def test_removes_noise(self):
        archive = [
            "모바일 게임",
            "모바일 마비노기",
            "심리상담센터",
            "직업상담사",
            "로고제작",
            "제작년 재작년",
        ]
        ref = ["외주 개발", "앱 개발 견적"]
        result = ResearcherAgent._filter_archive_seeds(archive, ref)
        assert result == []

    def test_empty_archive(self):
        result = ResearcherAgent._filter_archive_seeds([], ["외주 개발"])
        assert result == []

    def test_empty_reference(self):
        archive = ["모바일 신분증", "외주 개발"]
        result = ResearcherAgent._filter_archive_seeds(archive, [])
        assert result == archive

    def test_single_char_tokens_ignored(self):
        archive = ["앱 스토어"]
        ref = ["앱 개발"]
        result = ResearcherAgent._filter_archive_seeds(archive, ref)
        assert "앱 스토어" not in result

    def test_case_insensitive(self):
        archive = ["ERP 시스템"]
        ref = ["erp 외주 개발"]
        result = ResearcherAgent._filter_archive_seeds(archive, ref)
        assert "ERP 시스템" in result


@pytest.mark.asyncio
async def test_structured_input_pipeline(mock_all_tools):
    """구조화 입력 → run() → ResearchResult에 메타 정보가 채워지는지 확인."""
    structured = (
        "질문 의도 : ERP 외주 개발 업체 선정 기준 파악\n"
        "질문 형태 :\n"
        "1. ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?\n"
        "2. 앱 개발 견적이 업체마다 다른 이유는 무엇이며, 적정 비용을 판단할 기준은?\n"
        "3. 외주 개발 프로젝트를 진행할 때 자주 발생하는 문제는 무엇인가요?\n"
        "콘텐츠 방향성 : 실무 가이드 중심, 위시켓 사례 활용"
    )
    agent = ResearcherAgent()
    result = await agent.run(structured)

    assert isinstance(result, ResearchResult)
    assert result.intent == "ERP 외주 개발 업체 선정 기준 파악"
    assert result.content_direction == "실무 가이드 중심, 위시켓 사례 활용"
    assert len(result.source_questions) == 3
    assert len(result.extracted_seeds) > 0
    assert result.main_keyword

    data = result.model_dump()
    assert "intent" in data
    assert "source_questions" in data
    assert "content_direction" in data
    assert "extracted_seeds" in data


# ── 단위 테스트: LLM 클러스터링 ─────────────────────────────────


class TestLlmClustering:
    @pytest.mark.asyncio
    async def test_clusters_and_orphans(self):
        llm_response = json.dumps({
            "clusters": [
                {"keywords": ["앱 개발", "앱 개발 비용", "앱 개발 견적"],
                 "shared_intent": "앱 개발 비용 정보 탐색"},
                {"keywords": ["외주 개발", "외주 개발 업체", "외주 개발 비용"],
                 "shared_intent": "외주 개발 업체/비용 비교"},
            ],
            "orphans": ["ERP"],
        }, ensure_ascii=False)

        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return llm_response

        agent = ResearcherAgent()
        with patch.object(ResearcherAgent, "_llm_call", _mock_llm):
            deduped = [
                ("앱 개발", "google"),
                ("앱 개발 비용", "google"),
                ("앱 개발 견적", "google"),
                ("외주 개발", "naver"),
                ("외주 개발 업체", "keyword_tool"),
                ("외주 개발 비용", "keyword_tool"),
                ("ERP", "google"),
            ]
            clusters, orphans = await agent._stage1d_llm_clustering(deduped)

        assert len(clusters) == 2
        assert "ERP" in orphans
        c0_kws = [kw for kw, _ in clusters[0].keywords]
        assert "앱 개발" in c0_kws
        assert "앱 개발 비용" in c0_kws
        assert clusters[0].shared_intent == "앱 개발 비용 정보 탐색"

    @pytest.mark.asyncio
    async def test_two_member_cluster_becomes_orphan(self):
        """최소 3개 미만 클러스터는 orphans로 처리."""
        llm_response = json.dumps({
            "clusters": [
                {"keywords": ["외주", "외주 개발"],
                 "shared_intent": "외주 관련"},
                {"keywords": ["앱 개발", "앱 개발 비용", "앱 개발 견적"],
                 "shared_intent": "앱 비용 탐색"},
            ],
            "orphans": [],
        }, ensure_ascii=False)

        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return llm_response

        agent = ResearcherAgent()
        with patch.object(ResearcherAgent, "_llm_call", _mock_llm):
            deduped = [
                ("앱 개발", "google"),
                ("앱 개발 비용", "google"),
                ("앱 개발 견적", "google"),
                ("외주", "naver"),
                ("외주 개발", "naver"),
            ]
            clusters, orphans = await agent._stage1d_llm_clustering(deduped)

        assert len(clusters) == 1  # 3개짜리만 생존
        assert "외주" in orphans
        assert "외주 개발" in orphans


class TestLlmClusteringFallback:
    @pytest.mark.asyncio
    async def test_invalid_json_all_orphans(self):
        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return "not valid json"

        agent = ResearcherAgent()
        with patch.object(ResearcherAgent, "_llm_call", _mock_llm):
            deduped = [("앱 개발", "google"), ("외주 개발", "naver")]
            clusters, orphans = await agent._stage1d_llm_clustering(deduped)

        assert len(clusters) == 0
        assert set(orphans) == {"앱 개발", "외주 개발"}

    @pytest.mark.asyncio
    async def test_empty_response_all_orphans(self):
        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return ""

        agent = ResearcherAgent()
        with patch.object(ResearcherAgent, "_llm_call", _mock_llm):
            deduped = [("앱 개발", "google")]
            clusters, orphans = await agent._stage1d_llm_clustering(deduped)

        assert len(clusters) == 0
        assert orphans == ["앱 개발"]


class TestLlmRepresentative:
    @pytest.mark.asyncio
    async def test_representative_and_rationale(self):
        llm_response = json.dumps([
            {"id": "c000", "representative": "앱 개발", "rationale": "가장 포괄적"},
        ], ensure_ascii=False)

        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return llm_response

        cd = ClusterDraft(
            cluster_id="c000",
            keywords=[("앱 개발", "google"), ("앱 개발 비용", "google")],
        )
        agent = ResearcherAgent()
        with patch.object(ResearcherAgent, "_llm_call", _mock_llm):
            await agent._stage1e_llm_representative([cd], {})

        assert cd.representative == "앱 개발"
        assert cd.representative_rationale == "가장 포괄적"


class TestLlmRepresentativeFallback:
    @pytest.mark.asyncio
    async def test_fallback_to_volume(self):
        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return "invalid"

        cd = ClusterDraft(
            cluster_id="c000",
            keywords=[("앱 개발", "google"), ("앱 개발 비용", "google")],
        )
        volumes = {"앱 개발": 1000, "앱 개발 비용": 500}
        agent = ResearcherAgent()
        with patch.object(ResearcherAgent, "_llm_call", _mock_llm):
            await agent._stage1e_llm_representative([cd], volumes)

        assert cd.representative == "앱 개발"


class TestArchiveComparison:
    @pytest.mark.asyncio
    async def test_archive_verdicts(self):
        llm_response = json.dumps([
            {"new_rep": "앱 개발", "verdict": "duplicate", "matched_archive_rep": "앱개발"},
            {"new_rep": "외주 개발", "verdict": "new", "matched_archive_rep": ""},
        ], ensure_ascii=False)

        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return llm_response

        cd1 = ClusterDraft(
            cluster_id="c000", representative="앱 개발",
            keywords=[("앱 개발", "google"), ("앱 개발 비용", "google")],
        )
        cd2 = ClusterDraft(
            cluster_id="c001", representative="외주 개발",
            keywords=[("외주 개발", "naver")],
        )
        agent = ResearcherAgent()
        agent._archive_cfg = {"index_file": ""}
        with (
            patch.object(ResearcherAgent, "_llm_call", _mock_llm),
            patch.object(ResearcherAgent, "_load_archive_reps", return_value=["앱개발"]),
            patch.object(
                ResearcherAgent, "_load_archive_clusters",
                return_value={"앱개발": ["앱개발", "앱 개발 비용"]},
            ),
        ):
            await agent._stage1f_archive_comparison([cd1, cd2])

        assert cd1.archive_verdict == "duplicate"
        assert cd1.matched_archive_representative == "앱개발"
        assert cd2.archive_verdict == "new"


class TestArchiveComparisonEmpty:
    @pytest.mark.asyncio
    async def test_empty_archive_all_new(self):
        cd = ClusterDraft(cluster_id="c000", representative="앱 개발")
        agent = ResearcherAgent()
        with patch.object(ResearcherAgent, "_load_archive_reps", return_value=[]):
            await agent._stage1f_archive_comparison([cd])

        assert cd.archive_verdict == "new"


class TestArchiveMerge:
    @pytest.mark.asyncio
    async def test_merge_adds_archive_keywords(self):
        llm_response = json.dumps([
            {
                "new_rep": "앱 개발",
                "verdict": "merge",
                "matched_archive_rep": "앱개발",
            },
        ], ensure_ascii=False)

        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return llm_response

        cd = ClusterDraft(
            cluster_id="c000",
            representative="앱 개발",
            keywords=[("앱 개발", "google"), ("앱 개발 비용", "google")],
        )
        agent = ResearcherAgent()
        agent._archive_cfg = {"index_file": ""}
        with (
            patch.object(ResearcherAgent, "_llm_call", _mock_llm),
            patch.object(
                ResearcherAgent, "_load_archive_reps",
                return_value=["앱개발"],
            ),
            patch.object(
                ResearcherAgent, "_load_archive_clusters",
                return_value={
                    "앱개발": ["앱개발", "앱개발 업체", "앱개발 외주"],
                },
            ),
        ):
            await agent._stage1f_archive_comparison([cd])

        assert cd.archive_verdict == "merge"
        kw_set = {kw for kw, _ in cd.keywords}
        assert "앱개발 업체" in kw_set
        assert "앱개발 외주" in kw_set
        assert "앱 개발" in kw_set
        assert "앱 개발 비용" in kw_set

    @pytest.mark.asyncio
    async def test_merge_no_duplicates(self):
        llm_response = json.dumps([
            {
                "new_rep": "외주 개발",
                "verdict": "merge",
                "matched_archive_rep": "외주개발",
            },
        ], ensure_ascii=False)

        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return llm_response

        cd = ClusterDraft(
            cluster_id="c000",
            representative="외주 개발",
            keywords=[("외주 개발", "google"), ("외주 업체", "naver")],
        )
        agent = ResearcherAgent()
        agent._archive_cfg = {"index_file": ""}
        with (
            patch.object(ResearcherAgent, "_llm_call", _mock_llm),
            patch.object(
                ResearcherAgent, "_load_archive_reps",
                return_value=["외주개발"],
            ),
            patch.object(
                ResearcherAgent, "_load_archive_clusters",
                return_value={"외주개발": ["외주 개발", "외주 비용"]},
            ),
        ):
            await agent._stage1f_archive_comparison([cd])

        kw_list = [kw for kw, _ in cd.keywords]
        assert kw_list.count("외주 개발") == 1
        assert "외주 비용" in kw_list

    @pytest.mark.asyncio
    async def test_new_verdict_no_merge(self):
        llm_response = json.dumps([
            {"new_rep": "앱 개발", "verdict": "new", "matched_archive_rep": ""},
        ], ensure_ascii=False)

        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return llm_response

        cd = ClusterDraft(
            cluster_id="c000",
            representative="앱 개발",
            keywords=[("앱 개발", "google")],
        )
        agent = ResearcherAgent()
        agent._archive_cfg = {"index_file": ""}
        with (
            patch.object(ResearcherAgent, "_llm_call", _mock_llm),
            patch.object(
                ResearcherAgent, "_load_archive_reps",
                return_value=["외주개발"],
            ),
            patch.object(
                ResearcherAgent, "_load_archive_clusters",
                return_value={"외주개발": ["외주개발", "외주 비용"]},
            ),
        ):
            await agent._stage1f_archive_comparison([cd])

        assert len(cd.keywords) == 1


# ── 단위 테스트: 아카이브 저장 (누적 merge) ──────────────────────


class TestArchiveSave:
    """save_archive 누적 갱신 검증."""

    def _make_result(self, run_date="2026-02-26"):
        from unittest.mock import MagicMock
        r = MagicMock()
        r.run_date = run_date
        r.model_dump.return_value = {"run_date": run_date}
        r.orphan_keywords = []
        r.clusters = []
        return r

    def _make_stage1(self, drafts):
        return Stage1Output(cluster_drafts=drafts)

    def test_new_creates_entry(self, tmp_path):
        from core.agents.researcher.archive import save_archive
        cfg = {
            "runs_dir": str(tmp_path / "runs"),
            "index_file": str(tmp_path / "index.json"),
        }
        cd = ClusterDraft(
            cluster_id="c000", representative="앱 개발",
            keywords=[("앱 개발", "google"), ("앱 개발 비용", "google")],
            archive_verdict="new",
        )
        result = self._make_result()
        save_archive(cfg, result, self._make_stage1([cd]))

        with open(tmp_path / "index.json") as f:
            index = json.load(f)
        data = index["cluster_data"]["앱 개발"]
        assert "앱 개발" in data["keywords"]
        assert data["created"] == "2026-02-26"
        assert data["last_seen"] == "2026-02-26"

    def test_merge_extends_keywords(self, tmp_path):
        from core.agents.researcher.archive import save_archive
        idx_path = tmp_path / "index.json"
        # 기존 인덱스 생성
        existing = {
            "last_run": "2026-02-25",
            "cluster_representatives": ["앱개발"],
            "orphan_keywords": [],
            "cluster_data": {
                "앱개발": {
                    "keywords": ["앱개발", "앱 개발 비용"],
                    "created": "2026-02-20",
                    "last_seen": "2026-02-25",
                },
            },
        }
        with open(idx_path, "w") as f:
            json.dump(existing, f)

        cfg = {
            "runs_dir": str(tmp_path / "runs"),
            "index_file": str(idx_path),
        }
        cd = ClusterDraft(
            cluster_id="c000", representative="앱 개발 신규",
            keywords=[("앱 개발 신규", "google"), ("앱 개발 견적", "google")],
            archive_verdict="merge",
            matched_archive_representative="앱개발",
        )
        result = self._make_result()
        save_archive(cfg, result, self._make_stage1([cd]))

        with open(idx_path) as f:
            index = json.load(f)
        data = index["cluster_data"]["앱개발"]
        assert "앱 개발 견적" in data["keywords"]
        assert "앱 개발 신규" in data["keywords"]
        assert data["created"] == "2026-02-20"  # 기존 유지
        assert data["last_seen"] == "2026-02-26"  # 갱신

    def test_duplicate_updates_last_seen_only(self, tmp_path):
        from core.agents.researcher.archive import save_archive
        idx_path = tmp_path / "index.json"
        existing = {
            "last_run": "2026-02-25",
            "cluster_representatives": ["앱개발"],
            "orphan_keywords": [],
            "cluster_data": {
                "앱개발": {
                    "keywords": ["앱개발", "앱 개발 비용"],
                    "created": "2026-02-20",
                    "last_seen": "2026-02-25",
                },
            },
        }
        with open(idx_path, "w") as f:
            json.dump(existing, f)

        cfg = {
            "runs_dir": str(tmp_path / "runs"),
            "index_file": str(idx_path),
        }
        cd = ClusterDraft(
            cluster_id="c000", representative="앱 개발",
            keywords=[("앱 개발", "google"), ("앱 개발 비용", "google")],
            archive_verdict="duplicate",
            matched_archive_representative="앱개발",
        )
        result = self._make_result()
        save_archive(cfg, result, self._make_stage1([cd]))

        with open(idx_path) as f:
            index = json.load(f)
        data = index["cluster_data"]["앱개발"]
        assert len(data["keywords"]) == 2  # 키워드 추가 없음
        assert data["last_seen"] == "2026-02-26"

    def test_cumulative_preserves_existing(self, tmp_path):
        """신규 실행이 기존 클러스터를 삭제하지 않는다."""
        from core.agents.researcher.archive import save_archive
        idx_path = tmp_path / "index.json"
        existing = {
            "last_run": "2026-02-25",
            "cluster_representatives": ["기존 클러스터"],
            "orphan_keywords": [],
            "cluster_data": {
                "기존 클러스터": {
                    "keywords": ["기존 키워드"],
                    "created": "2026-02-20",
                    "last_seen": "2026-02-25",
                },
            },
        }
        with open(idx_path, "w") as f:
            json.dump(existing, f)

        cfg = {
            "runs_dir": str(tmp_path / "runs"),
            "index_file": str(idx_path),
        }
        cd = ClusterDraft(
            cluster_id="c000", representative="새 클러스터",
            keywords=[("새 키워드", "google")],
            archive_verdict="new",
        )
        result = self._make_result()
        save_archive(cfg, result, self._make_stage1([cd]))

        with open(idx_path) as f:
            index = json.load(f)
        assert "기존 클러스터" in index["cluster_data"]
        assert "새 클러스터" in index["cluster_data"]


# ── 단위 테스트: 포커스 클러스터 선정 ─────────────────────────────


class TestFocusSelection:
    @pytest.mark.asyncio
    async def test_selects_focus_clusters(self):
        llm_response = json.dumps([
            {"id": "c000", "focus": True},
            {"id": "c001", "focus": False},
        ], ensure_ascii=False)

        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return llm_response

        cd0 = ClusterDraft(
            cluster_id="c000", representative="앱 개발",
            keywords=[("앱 개발", "google"), ("앱 개발 비용", "google")],
        )
        cd1 = ClusterDraft(
            cluster_id="c001", representative="웹 디자인",
            keywords=[("웹 디자인", "google"), ("UI 디자인", "naver")],
        )
        questions = ["앱 개발 비용은 얼마인가요?"]

        agent = ResearcherAgent()
        with patch.object(ResearcherAgent, "_llm_call", _mock_llm):
            await agent._stage1g_focus_selection([cd0, cd1], questions)

        assert cd0.is_focus is True
        assert cd1.is_focus is False

    @pytest.mark.asyncio
    async def test_all_focus_on_empty_questions(self):
        cd = ClusterDraft(cluster_id="c000", representative="앱 개발")
        agent = ResearcherAgent()
        await agent._stage1g_focus_selection([cd], [])
        assert cd.is_focus is True


class TestFocusSelectionFallback:
    @pytest.mark.asyncio
    async def test_invalid_json_keeps_all_focus(self):

        async def _mock_llm(self, label, system, user, model="", max_tokens=4096):
            return "invalid json"

        cd = ClusterDraft(cluster_id="c000", representative="앱 개발")
        agent = ResearcherAgent()
        with patch.object(ResearcherAgent, "_llm_call", _mock_llm):
            await agent._stage1g_focus_selection([cd], ["앱 개발 비용?"])
        assert cd.is_focus is True


# ── 단위 테스트: _filter_seeds (agent._llm_call 기반) ──────────


class TestFilterSeeds:
    @pytest.mark.asyncio
    async def test_keeps_domain_keywords_drops_modifiers(self):
        """도메인 키워드 keep, 순수 수식어 drop."""
        agent = ResearcherAgent()

        mock_llm_response = json.dumps(
            ["ERP 외주 개발 업체", "외주 개발", "앱 개발 견적", "ERP", "비용"],
            ensure_ascii=False,
        )
        agent._llm_call = AsyncMock(return_value=mock_llm_response)

        result = await agent._filter_seeds(
            ["ERP 외주 개발 업체", "외주 개발", "기준", "이유", "앱 개발 견적", "ERP", "비용"],
            questions=["ERP 외주 개발 업체를 고를 때 기준은?"],
            intent="비교 판단",
            direction="판단 기준 제시",
        )

        assert "ERP 외주 개발 업체" in result
        assert "외주 개발" in result
        assert "ERP" in result
        assert "비용" in result
        assert "기준" not in result
        assert "이유" not in result

    @pytest.mark.asyncio
    async def test_llm_call_uses_seed_filter_label(self):
        """_llm_call이 'seed_filter' label로 호출되는지 확인."""
        agent = ResearcherAgent()
        agent._llm_call = AsyncMock(return_value='["외주 개발"]')

        await agent._filter_seeds(
            ["외주 개발", "기준"],
            questions=["질문"],
            intent="test", direction="test",
        )

        agent._llm_call.assert_called_once()
        call_args = agent._llm_call.call_args
        assert call_args[0][0] == "seed_filter"

    @pytest.mark.asyncio
    async def test_fallback_on_empty_llm_response(self):
        """LLM 빈 응답 시 원본 반환."""
        agent = ResearcherAgent()
        agent._llm_call = AsyncMock(return_value="")

        seeds = ["외주 개발", "기준"]
        result = await agent._filter_seeds(seeds, [], "test", "test")
        assert result == seeds

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_json(self):
        """LLM JSON 파싱 실패 시 원본 반환."""
        agent = ResearcherAgent()
        agent._llm_call = AsyncMock(return_value="not valid json")

        seeds = ["외주 개발", "기준"]
        result = await agent._filter_seeds(seeds, [], "test", "test")
        assert result == seeds

    @pytest.mark.asyncio
    async def test_empty_seeds_returns_empty(self):
        """빈 시드 리스트 처리."""
        agent = ResearcherAgent()
        agent._llm_call = AsyncMock()

        result = await agent._filter_seeds([], [], "test", "test")
        assert result == []
        agent._llm_call.assert_not_called()


class TestSeedFilterInPipeline:
    @pytest.mark.asyncio
    async def test_seed_filter_disabled(self, mock_all_tools):
        agent = ResearcherAgent()
        agent._config["seed_filter"] = {"enabled": False}

        structured = (
            "질문 의도 : ERP 외주 개발 업체 선정\n"
            "질문 형태 :\n"
            "1. ERP 외주 개발 업체를 고를 때 기준은 무엇인가요?\n"
            "콘텐츠 방향성 : 실무 가이드"
        )
        result = await agent.run(structured)

        assert isinstance(result, ResearchResult)

    @pytest.mark.asyncio
    async def test_structured_input_with_seed_filter(self, mock_all_tools):
        """seed_filter가 _llm_call mock을 통해 동작하고 파이프라인 완주."""
        agent = ResearcherAgent()
        agent._config["seed_filter"] = {"enabled": True}

        structured = (
            "질문 의도 : ERP 외주 개발 업체 선정 기준 파악\n"
            "질문 형태 :\n"
            "1. ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?\n"
            "2. 앱 개발 견적이 업체마다 다른 이유는 무엇이며, 적정 비용을 판단할 기준은?\n"
            "3. 외주 개발 프로젝트를 진행할 때 자주 발생하는 문제는 무엇인가요?\n"
            "콘텐츠 방향성 : 실무 가이드 중심, 위시켓 사례 활용"
        )
        result = await agent.run(structured)

        assert isinstance(result, ResearchResult)
        assert result.intent == "ERP 외주 개발 업체 선정 기준 파악"


# ── 단위 테스트: 규칙 기반 사전 필터 ──────────────────────────


class TestRuleBasedPrefilter:
    """_rule_based_prefilter: searchad(keyword_tool) 출처는 시드 토큰 겹침 2+, 2어절+ 필수."""

    SEEDS = ["ERP 외주 개발", "앱 개발 비용"]

    def test_keyword_tool_passes_with_overlap(self):
        deduped = [("erp 외주 업체", "keyword_tool")]
        result = _rule_based_prefilter(deduped, self.SEEDS)
        assert len(result) == 1

    def test_keyword_tool_blocked_single_word(self):
        deduped = [("ERP", "keyword_tool")]
        result = _rule_based_prefilter(deduped, self.SEEDS)
        assert len(result) == 0

    def test_keyword_tool_blocked_low_overlap(self):
        deduped = [("이사 견적 비교", "keyword_tool")]
        result = _rule_based_prefilter(deduped, self.SEEDS)
        assert len(result) == 0

    def test_google_source_passes_loosely(self):
        deduped = [("소프트웨어", "google")]
        result = _rule_based_prefilter(deduped, self.SEEDS)
        assert len(result) == 1

    def test_naver_source_passes_loosely(self):
        deduped = [("IT 아웃소싱", "naver")]
        result = _rule_based_prefilter(deduped, self.SEEDS)
        assert len(result) == 1

    def test_dedup_after_normalization(self):
        deduped = [
            ("ERP 외주", "google"),
            ("erp 외주", "keyword_tool"),  # 정규화 후 동일 → 압축
        ]
        result = _rule_based_prefilter(deduped, self.SEEDS)
        assert len(result) == 1
        assert result[0][1] == "google"  # 먼저 나온 소스 유지

    def test_empty_input(self):
        assert _rule_based_prefilter([], self.SEEDS) == []

    def test_empty_seeds(self):
        deduped = [("erp 외주 업체", "keyword_tool")]
        result = _rule_based_prefilter(deduped, [])
        # 시드 토큰 없으면 keyword_tool은 겹침 0 → 탈락
        assert len(result) == 0


# ── 단위 테스트: 고객 언어 로딩 ──────────────────────────────


class TestCustomerLanguage:
    def test_no_client_returns_empty(self):
        agent = ResearcherAgent()
        assert agent._stage1b_customer_language("") == []

    def test_missing_file_returns_empty(self):
        agent = ResearcherAgent()
        agent._config["context_path"] = "/tmp/nonexistent/{client}/context.yaml"
        assert agent._stage1b_customer_language("testclient") == []

    def test_loads_keywords_from_context(self, tmp_path):
        context_dir = tmp_path / "testclient"
        context_dir.mkdir()
        context_file = context_dir / "context.yaml"
        context_file.write_text(
            "customer_language:\n"
            "  pain_points:\n"
            "    - 외주 개발 실패\n"
            "    - 견적 비교\n"
            "  jargon:\n"
            "    - SI 업체\n"
            "    - 유지보수 계약\n",
            encoding="utf-8",
        )
        agent = ResearcherAgent()
        agent._config["context_path"] = str(tmp_path / "{client}/context.yaml")
        result = agent._stage1b_customer_language("testclient")
        assert "외주 개발 실패" in result
        assert "견적 비교" in result
        assert "SI 업체" in result
        assert "유지보수 계약" in result
        assert len(result) == 4

    def test_loads_list_format(self, tmp_path):
        context_dir = tmp_path / "client2"
        context_dir.mkdir()
        context_file = context_dir / "context.yaml"
        context_file.write_text(
            "customer_language:\n"
            "  - 키워드1\n"
            "  - 키워드2\n",
            encoding="utf-8",
        )
        agent = ResearcherAgent()
        agent._config["context_path"] = str(tmp_path / "{client}/context.yaml")
        result = agent._stage1b_customer_language("client2")
        assert result == ["키워드1", "키워드2"]

    def test_empty_customer_language(self, tmp_path):
        context_dir = tmp_path / "client3"
        context_dir.mkdir()
        context_file = context_dir / "context.yaml"
        context_file.write_text("brand_name: Test\n", encoding="utf-8")
        agent = ResearcherAgent()
        agent._config["context_path"] = str(tmp_path / "{client}/context.yaml")
        result = agent._stage1b_customer_language("client3")
        assert result == []


# ── Google SERP Hallucination 필터 ───────────────────────────────


class TestIsRelevantSerpItem:
    """_is_relevant_serp_item 토큰 겹침 필터 테스트."""

    # -- 관련 있는 결과: True --

    def test_exact_keyword_in_title(self):
        item = {"title": "앱 견적 산출 방법", "snippet": ""}
        assert _is_relevant_serp_item("앱 견적", item) is True

    def test_token_in_snippet(self):
        item = {"title": "개발 가이드", "snippet": "외주 프로젝트 관리 방법"}
        assert _is_relevant_serp_item("외주 개발", item) is True

    def test_multi_word_partial_match(self):
        item = {"title": "ERP 시스템 구축", "snippet": "기업용 솔루션"}
        assert _is_relevant_serp_item("ERP 외주 개발", item) is True

    def test_compound_korean_bigram_match(self):
        """'외주개발사' → bigram '개발'이 snippet에 포함."""
        item = {"title": "소프트웨어 개발 업체", "snippet": ""}
        assert _is_relevant_serp_item("외주개발사", item) is True

    def test_case_insensitive(self):
        item = {"title": "erp implementation guide", "snippet": ""}
        assert _is_relevant_serp_item("ERP", item) is True

    # -- 무관한 결과 (hallucination): False --

    def test_fashion_for_app_cost(self):
        """'앱 견적' → ssense.com 패션 가방 결과 필터링."""
        item = {"title": "Apo G Bags Collection", "snippet": "luxury fashion bags and accessories"}
        assert _is_relevant_serp_item("앱 견적", item) is False

    def test_crypto_for_outsourcing(self):
        """'외주개발사' → btcc.com 암호화폐 결과 필터링."""
        item = {"title": "Bitcoin Trading Platform", "snippet": "crypto exchange and futures"}
        assert _is_relevant_serp_item("외주개발사", item) is False

    def test_bus_for_app_developer(self):
        """'앱개발자' → 버스 정류장 결과 필터링."""
        item = {"title": "City Bus Route Finder", "snippet": "find bus stops near you"}
        assert _is_relevant_serp_item("앱개발자", item) is False

    def test_candle_for_dev_cost(self):
        """'개발 견적' → 양초 가게 결과 필터링."""
        item = {"title": "Candle Works", "snippet": "handmade candles and wax products"}
        assert _is_relevant_serp_item("개발 견적", item) is False

    def test_tourism_for_app_developer(self):
        """'앱개발자' → 일본 관광 결과 필터링."""
        item = {"title": "Fukushima Tourism Guide", "snippet": "visit beautiful spots in Japan"}
        assert _is_relevant_serp_item("앱개발자", item) is False

    def test_apple_tv_for_outsourcing(self):
        """'외주개발사' → Apple TV 결과 필터링."""
        item = {"title": "Apple TV+ Shows", "snippet": "watch the latest shows on apple tv+"}
        assert _is_relevant_serp_item("외주개발사", item) is False

    # -- 엣지 케이스 --

    def test_empty_title_and_snippet(self):
        """title/snippet 없으면 False."""
        item = {"title": "", "snippet": ""}
        assert _is_relevant_serp_item("앱 견적", item) is False

    def test_single_char_keyword_passthrough(self):
        """단일 글자 키워드는 필터 통과 (너무 짧아서 판단 불가)."""
        item = {"title": "anything", "snippet": "whatever"}
        assert _is_relevant_serp_item("앱", item) is True

    def test_none_title_snippet(self):
        """title/snippet이 None이어도 에러 없이 처리."""
        item = {"title": None, "snippet": None}
        assert _is_relevant_serp_item("외주 개발", item) is False


# ── 스냅샷 라운드트립 테스트 ─────────────────────────────────────


class TestSnapshotRoundTrip:
    """직렬화 → 역직렬화 라운드트립 검증."""

    def test_parsed_input_roundtrip(self, tmp_path):
        from core.agents.researcher.snapshot import save_snapshot, load_input

        original = ParsedInput(
            main_keyword="외주 개발",
            entry_moment="비용 문의",
            intent="비교 판단",
            questions=["질문1", "질문2"],
            direction="판단 기준 제시",
            extracted_seeds=["외주 개발", "앱 개발"],
        )
        save_snapshot("input", original, "2026-01-01", str(tmp_path))
        restored = load_input("2026-01-01", str(tmp_path))

        assert restored.main_keyword == original.main_keyword
        assert restored.entry_moment == original.entry_moment
        assert restored.intent == original.intent
        assert restored.questions == original.questions
        assert restored.direction == original.direction
        assert restored.extracted_seeds == original.extracted_seeds

    def test_raw_keyword_pool_roundtrip(self, tmp_path):
        from core.agents.researcher.snapshot import save_snapshot, load_pool

        original = RawKeywordPool(
            google=["외주 개발", "앱 개발"],
            naver=["외주 개발 비용"],
            keyword_tool=["앱 개발 견적"],
            internal_data=["SI 개발"],
            paa=["외주 개발 장단점은?"],
            paa_questions={"외주 개발": ["장단점은?", "비용은?"]},
            volumes={"외주 개발": 1000, "앱 개발": 500},
            volumes_pc={"외주 개발": 600},
            volumes_mobile={"외주 개발": 400},
            related_from_searchad=["ERP 개발"],
        )
        save_snapshot("stage1_keywords", original, "2026-01-01", str(tmp_path))
        restored = load_pool("2026-01-01", str(tmp_path))

        assert restored.google == original.google
        assert restored.naver == original.naver
        assert restored.keyword_tool == original.keyword_tool
        assert restored.volumes == original.volumes
        assert restored.paa_questions == original.paa_questions

    def test_stage1_output_roundtrip(self, tmp_path):
        from core.agents.researcher.snapshot import save_snapshot, load_stage1

        cd1 = ClusterDraft(
            cluster_id="c000",
            keywords=[("외주 개발", "google"), ("앱 개발", "naver")],
            representative="외주 개발",
            representative_rationale="포괄적",
            archive_verdict="new",
            is_focus=True,
        )
        cd2 = ClusterDraft(
            cluster_id="c001",
            keywords=[("ERP 개발", "keyword_tool")],
            representative="ERP 개발",
            is_focus=False,
        )
        original = Stage1Output(
            cluster_drafts=[cd1, cd2],
            orphan_keywords=["잡다한 키워드"],
            paa_questions={"외주 개발": ["비용은?"]},
            volumes={"외주 개발": 1000},
            volumes_pc={"외주 개발": 600},
            volumes_mobile={"외주 개발": 400},
        )
        save_snapshot("stage1_clusters", original, "2026-01-01", str(tmp_path))
        restored = load_stage1("2026-01-01", str(tmp_path))

        assert len(restored.cluster_drafts) == 2
        assert restored.cluster_drafts[0].cluster_id == "c000"
        assert restored.cluster_drafts[0].representative == "외주 개발"
        assert restored.cluster_drafts[0].is_focus is True
        assert restored.cluster_drafts[1].is_focus is False
        assert restored.orphan_keywords == ["잡다한 키워드"]
        assert restored.volumes == {"외주 개발": 1000}

    def test_cluster_draft_keywords_tuple_preserved(self, tmp_path):
        """ClusterDraft.keywords 튜플 보존 검증."""
        from core.agents.researcher.snapshot import save_snapshot, load_stage1

        cd = ClusterDraft(
            cluster_id="c000",
            keywords=[("kw1", "google"), ("kw2", "naver"), ("kw3", "keyword_tool")],
        )
        original = Stage1Output(cluster_drafts=[cd])
        save_snapshot("stage1_clusters", original, "2026-01-01", str(tmp_path))
        restored = load_stage1("2026-01-01", str(tmp_path))

        kws = restored.cluster_drafts[0].keywords
        assert len(kws) == 3
        assert kws[0] == ("kw1", "google")
        assert kws[1] == ("kw2", "naver")
        assert kws[2] == ("kw3", "keyword_tool")
        # 타입 확인: tuple
        assert isinstance(kws[0], tuple)

    def test_stage2_output_roundtrip(self, tmp_path):
        from core.agents.researcher.snapshot import save_snapshot, load_stage2

        original = Stage2Output(
            volumes={"외주 개발": {"naver_volume": 1000, "naver_direction": "rising"}},
            google_content_metas={"외주 개발": [{"rank": 1, "title": "가이드", "url": "https://example.com"}]},
            naver_content_metas={"외주 개발": [{"rank": 1, "title": "블로그", "url": "https://blog.naver.com/x"}]},
            h2_topics={"외주 개발": ["비용", "절차"]},
            google_serp_features={"외주 개발": {"ai_overview": True}},
            naver_serp_features={"외주 개발": {"knowledge_snippet": False}},
        )
        save_snapshot("stage2_serp", original, "2026-01-01", str(tmp_path))
        restored = load_stage2("2026-01-01", str(tmp_path))

        assert restored.volumes == original.volumes
        assert restored.google_content_metas == original.google_content_metas
        assert restored.h2_topics == original.h2_topics
        assert restored.google_serp_features == original.google_serp_features

    def test_stage3_output_roundtrip(self, tmp_path):
        from core.agents.researcher.snapshot import save_snapshot, load_stage3

        original = Stage3Output(
            citations={"외주 개발": [
                {"url": "https://example.com", "domain": "example.com",
                 "source": "chatgpt", "context_summary": "참조"},
            ]},
        )
        save_snapshot("stage3_geo", original, "2026-01-01", str(tmp_path))
        restored = load_stage3("2026-01-01", str(tmp_path))

        assert restored.citations == original.citations

    def test_empty_data_roundtrip(self, tmp_path):
        """빈 데이터 처리 검증."""
        from core.agents.researcher.snapshot import save_snapshot, load_stage1, load_stage2, load_stage3

        s1 = Stage1Output()
        save_snapshot("stage1_clusters", s1, "2026-01-01", str(tmp_path))
        r1 = load_stage1("2026-01-01", str(tmp_path))
        assert r1.cluster_drafts == []
        assert r1.orphan_keywords == []

        s2 = Stage2Output()
        save_snapshot("stage2_serp", s2, "2026-01-01", str(tmp_path))
        r2 = load_stage2("2026-01-01", str(tmp_path))
        assert r2.volumes == {}

        s3 = Stage3Output()
        save_snapshot("stage3_geo", s3, "2026-01-01", str(tmp_path))
        r3 = load_stage3("2026-01-01", str(tmp_path))
        assert r3.citations == {}

    def test_snapshot_file_created(self, tmp_path):
        """save_snapshot이 실제 파일을 생성하는지 확인."""
        from core.agents.researcher.snapshot import save_snapshot

        data = ParsedInput(main_keyword="test", entry_moment="general")
        path = save_snapshot("input", data, "2026-01-01", str(tmp_path))
        assert path.exists()
        assert path.name == "2026-01-01_input.json"

        content = json.loads(path.read_text(encoding="utf-8"))
        assert content["main_keyword"] == "test"

    def test_deduped_roundtrip(self, tmp_path):
        """중복 제거 키워드 리스트 직렬화/역직렬화."""
        from core.agents.researcher.snapshot import save_deduped, load_deduped

        pool = RawKeywordPool(
            volumes={"외주 개발": 1000, "앱 개발": 500},
            volumes_pc={"외주 개발": 600},
            volumes_mobile={"외주 개발": 400},
            paa_questions={"외주 개발": ["비용은?", "기간은?"]},
        )
        deduped = [("외주 개발", "google"), ("앱 개발", "naver"), ("ERP", "keyword_tool")]
        save_deduped(deduped, pool, "2026-01-01", str(tmp_path))
        restored_deduped, pool_meta = load_deduped("2026-01-01", str(tmp_path))

        assert len(restored_deduped) == 3
        assert restored_deduped[0] == ("외주 개발", "google")
        assert restored_deduped[2] == ("ERP", "keyword_tool")
        assert isinstance(restored_deduped[0], tuple)
        assert pool_meta["volumes"] == {"외주 개발": 1000, "앱 개발": 500}
        assert pool_meta["paa_questions"] == {"외주 개발": ["비용은?", "기간은?"]}

    def test_stage1_sub_roundtrip(self, tmp_path):
        """Stage 1 서브스텝 중간 스냅샷 라운드트립."""
        from core.agents.researcher.snapshot import save_stage1_sub, load_stage1_sub

        pool = RawKeywordPool(
            volumes={"외주 개발": 1000},
            volumes_pc={"외주 개발": 600},
            volumes_mobile={"외주 개발": 400},
            paa_questions={"외주 개발": ["비용은?"]},
        )
        cd = ClusterDraft(
            cluster_id="c000",
            keywords=[("외주 개발", "google"), ("앱 개발", "naver")],
        )
        save_stage1_sub(
            "stage1d_clusters", [cd], ["orphan1"], pool,
            "2026-01-01", str(tmp_path),
        )
        restored = load_stage1_sub("stage1d_clusters", "2026-01-01", str(tmp_path))

        assert len(restored.cluster_drafts) == 1
        assert restored.cluster_drafts[0].keywords[0] == ("외주 개발", "google")
        assert isinstance(restored.cluster_drafts[0].keywords[0], tuple)
        assert restored.orphan_keywords == ["orphan1"]
        assert restored.volumes == {"외주 개발": 1000}
        assert restored.paa_questions == {"외주 개발": ["비용은?"]}

    def test_stage1_sub_progressive_state(self, tmp_path):
        """1d→1e→1f 순차적으로 상태가 누적되는지 검증."""
        from core.agents.researcher.snapshot import save_stage1_sub, load_stage1_sub

        pool = RawKeywordPool(volumes={"kw1": 100})
        cd = ClusterDraft(
            cluster_id="c000",
            keywords=[("kw1", "google"), ("kw2", "naver")],
        )
        # 1d: 클러스터링 직후 (대표 미선정)
        save_stage1_sub("stage1d_clusters", [cd], [], pool, "2026-01-01", str(tmp_path))
        r1d = load_stage1_sub("stage1d_clusters", "2026-01-01", str(tmp_path))
        assert r1d.cluster_drafts[0].representative == ""

        # 1e: 대표 선정
        cd.representative = "kw1"
        cd.representative_rationale = "볼륨 최대"
        save_stage1_sub("stage1e_clusters", [cd], [], pool, "2026-01-01", str(tmp_path))
        r1e = load_stage1_sub("stage1e_clusters", "2026-01-01", str(tmp_path))
        assert r1e.cluster_drafts[0].representative == "kw1"

        # 1f: 아카이브 판정
        cd.archive_verdict = "new"
        save_stage1_sub("stage1f_clusters", [cd], [], pool, "2026-01-01", str(tmp_path))
        r1f = load_stage1_sub("stage1f_clusters", "2026-01-01", str(tmp_path))
        assert r1f.cluster_drafts[0].archive_verdict == "new"
        assert r1f.cluster_drafts[0].representative == "kw1"

    def test_deduped_empty(self, tmp_path):
        """빈 deduped 리스트 처리."""
        from core.agents.researcher.snapshot import save_deduped, load_deduped

        pool = RawKeywordPool()
        save_deduped([], pool, "2026-01-01", str(tmp_path))
        restored_deduped, pool_meta = load_deduped("2026-01-01", str(tmp_path))
        assert restored_deduped == []
        assert pool_meta["volumes"] == {}
