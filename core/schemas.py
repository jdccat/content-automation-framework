"""파이프라인 공용 Pydantic 스키마 — spec v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

from pydantic import BaseModel, Field, computed_field


# ── 공용 타입 ──────────────────────────────────────────────────

TREND_DIRECTION = Literal["rising", "stable", "declining"]
DISCOVERY_SOURCE = Literal["google", "naver", "keyword_tool", "internal_data", "archive"]


# ── 리서처 내부 파이프라인 데이터클래스 ────────────────────────────


@dataclass
class ParsedInput:
    """입력 파싱 결과."""

    main_keyword: str
    entry_moment: str
    intent: str = ""
    questions: list[str] = field(default_factory=list)
    direction: str = ""
    extracted_seeds: list[str] = field(default_factory=list)


@dataclass
class RawKeywordPool:
    """1-2~1-3 수집 원시 키워드 (플랫폼별 분리)."""

    google: list[str] = field(default_factory=list)
    naver: list[str] = field(default_factory=list)
    keyword_tool: list[str] = field(default_factory=list)
    internal_data: list[str] = field(default_factory=list)
    paa: list[str] = field(default_factory=list)
    paa_questions: dict[str, list[str]] = field(default_factory=dict)
    volumes: dict[str, int] = field(default_factory=dict)
    volumes_pc: dict[str, int] = field(default_factory=dict)
    volumes_mobile: dict[str, int] = field(default_factory=dict)
    related_from_searchad: list[str] = field(default_factory=list)


@dataclass
class ClusterDraft:
    """1d~1g 클러스터 중간 결과."""

    cluster_id: str
    keywords: list[tuple[str, str]] = field(default_factory=list)
    shared_intent: str = ""
    representative: str = ""
    representative_rationale: str = ""
    archive_verdict: str = ""
    matched_archive_representative: str = ""
    is_focus: bool = True


@dataclass
class Stage1Output:
    """1단계 전체 산출물."""

    cluster_drafts: list[ClusterDraft] = field(default_factory=list)
    orphan_keywords: list[str] = field(default_factory=list)
    paa_questions: dict[str, list[str]] = field(default_factory=dict)
    volumes: dict[str, int] = field(default_factory=dict)
    volumes_pc: dict[str, int] = field(default_factory=dict)
    volumes_mobile: dict[str, int] = field(default_factory=dict)


@dataclass
class Stage2Output:
    """2단계 검증 데이터 — 구글/네이버 분리."""

    volumes: dict[str, dict] = field(default_factory=dict)
    google_content_metas: dict[str, list[dict]] = field(default_factory=dict)
    naver_content_metas: dict[str, list[dict]] = field(default_factory=dict)
    h2_topics: dict[str, list[str]] = field(default_factory=dict)
    google_serp_features: dict[str, dict] = field(default_factory=dict)
    naver_serp_features: dict[str, dict] = field(default_factory=dict)


@dataclass
class Stage3Output:
    """3단계 GEO 인용 데이터."""

    citations: dict[str, list[dict]] = field(default_factory=dict)


# ── 리서처 출력: 클러스터 구성 요소 ──────────────────────────────


class ClusterKeyword(BaseModel):
    """클러스터에 포함된 개별 키워드."""

    keyword: str
    discovery_source: DISCOVERY_SOURCE = "google"
    monthly_volume_google: int = Field(default=0, ge=0)
    monthly_volume_naver: int = Field(default=0, ge=0)
    monthly_volume_naver_pc: int = Field(default=0, ge=0)
    monthly_volume_naver_mobile: int = Field(default=0, ge=0)
    volume_trend: TREND_DIRECTION = "stable"


class ContentMeta(BaseModel):
    """SERP 상위 콘텐츠 메타 정보 (구글/네이버 공용)."""

    rank: int = Field(ge=1)
    title: str
    url: str
    h2_structure: list[str] = Field(default_factory=list)
    publish_date: str | None = None
    content_type: str = "other"
    is_competitor: bool = False
    platform: Literal["google", "naver"] = "google"
    exposure_area: str = ""  # 네이버 전용: VIEW / 블로그 탭 / 지식iN 등


class GoogleSerpFeatures(BaseModel):
    """구글 SERP 피처 체크리스트."""

    ai_overview: bool = False
    featured_snippet_exists: bool = False
    featured_snippet_url: str | None = None
    paa_questions: list[str] = Field(default_factory=list)


class NaverSerpFeatures(BaseModel):
    """네이버 SERP 피처 체크리스트."""

    knowledge_snippet: bool = False
    smart_block: bool = False
    smart_block_components: list[str] = Field(default_factory=list)


class GeoCitation(BaseModel):
    """GEO 인용 소스."""

    url: str
    domain: str
    context_summary: str = ""
    source: Literal["chatgpt", "perplexity", "claude", "gemini"] = "chatgpt"
    is_own_domain: bool = False
    is_competitor: bool = False


# ── 리서처 출력: 클러스터 ────────────────────────────────────────


class Cluster(BaseModel):
    """LLM 의미 기반 키워드 클러스터."""

    # 식별 정보
    cluster_id: str
    representative_keyword: str
    representative_rationale: str = ""
    archive_verdict: str = ""
    matched_archive_representative: str = ""
    is_focus: bool = True

    # 1단계: 확장 데이터
    keywords: list[ClusterKeyword] = Field(default_factory=list)

    # 2단계: 검증 데이터
    total_volume_google: int = Field(default=0, ge=0)
    total_volume_naver: int = Field(default=0, ge=0)
    total_volume_naver_pc: int = Field(default=0, ge=0)
    total_volume_naver_mobile: int = Field(default=0, ge=0)
    volume_trend: TREND_DIRECTION = "stable"
    google_trend_series: list[dict] = Field(default_factory=list)  # [{"period","ratio"}]
    naver_trend_series: list[dict] = Field(default_factory=list)   # [{"period","ratio"}]
    google_content_meta: list[ContentMeta] = Field(default_factory=list)
    naver_content_meta: list[ContentMeta] = Field(default_factory=list)
    h2_topics: list[str] = Field(default_factory=list)
    paa_questions: list[str] = Field(default_factory=list)
    google_serp_features: GoogleSerpFeatures = Field(default_factory=GoogleSerpFeatures)
    naver_serp_features: NaverSerpFeatures = Field(default_factory=NaverSerpFeatures)

    # 3단계: GEO 데이터
    geo_citations: list[GeoCitation] = Field(default_factory=list)

    @classmethod
    def from_draft(
        cls,
        cd: ClusterDraft,
        stage1: Stage1Output,
        stage2: Stage2Output,
        stage3: Stage3Output,
        *,
        normalize_keyword: Callable[[str], str],
        is_competitor: Callable[[str], bool],
    ) -> Cluster:
        """ClusterDraft + 스테이지 데이터를 조합하여 Cluster를 생성한다."""
        rep = cd.representative

        # Stage 2 볼륨 (대표 키워드)
        s2vol = stage2.volumes.get(rep, {})
        s2_naver = s2vol.get("naver_volume", 0)
        s2_naver_pc = s2vol.get("naver_volume_pc", 0)
        s2_naver_mobile = s2vol.get("naver_volume_mobile", 0)

        # ClusterKeyword 모델 — Stage 2 볼륨 우선, fallback Stage 1
        kw_models = []
        for kw, src in cd.keywords:
            nk = normalize_keyword(kw)
            # Stage 2 volumes에서 키워드 직접 조회 (전체 키워드 볼륨 포함)
            s2kw = stage2.volumes.get(kw, {})
            if not s2kw:
                # normalized 키 매칭 시도
                s2kw = stage2.volumes.get(nk, {})
            if isinstance(s2kw, dict) and s2kw.get("naver_volume", 0) > 0:
                vol_n = s2kw["naver_volume"]
                vol_pc = s2kw.get("naver_volume_pc", 0)
                vol_mob = s2kw.get("naver_volume_mobile", 0)
            else:
                # fallback: Stage 1
                vol_n = stage1.volumes.get(nk, 0)
                vol_pc = stage1.volumes_pc.get(nk, 0)
                vol_mob = stage1.volumes_mobile.get(nk, 0)
            kw_models.append(
                ClusterKeyword(
                    keyword=kw,
                    discovery_source=src,
                    monthly_volume_naver=vol_n,
                    monthly_volume_naver_pc=vol_pc,
                    monthly_volume_naver_mobile=vol_mob,
                    monthly_volume_google=0,
                )
            )

        # 합산 검색량 — Stage 2 우선, fallback Stage 1
        total_naver = 0
        total_naver_pc = 0
        total_naver_mobile = 0
        for kw, _ in cd.keywords:
            nk = normalize_keyword(kw)
            s2kw = stage2.volumes.get(kw, {})
            if not s2kw:
                s2kw = stage2.volumes.get(nk, {})
            if isinstance(s2kw, dict) and s2kw.get("naver_volume", 0) > 0:
                total_naver += s2kw["naver_volume"]
                total_naver_pc += s2kw.get("naver_volume_pc", 0)
                total_naver_mobile += s2kw.get("naver_volume_mobile", 0)
            else:
                total_naver += stage1.volumes.get(nk, 0)
                total_naver_pc += stage1.volumes_pc.get(nk, 0)
                total_naver_mobile += stage1.volumes_mobile.get(nk, 0)

        # 추이 방향 — 다수결
        vol = stage2.volumes.get(rep, {})
        directions = [
            vol.get("naver_direction", "stable"),
            vol.get("google_direction", "stable"),
        ]
        trend = (
            max(set(directions), key=directions.count)
            if directions
            else "stable"
        )

        # 구글 상위 콘텐츠 메타
        google_metas = [
            ContentMeta(
                rank=m.get("rank", 1),
                title=m.get("title", ""),
                url=m.get("url", ""),
                h2_structure=m.get("h2_structure", []),
                publish_date=m.get("publish_date"),
                content_type=m.get("content_type", "other"),
                is_competitor=is_competitor(m.get("url", "")),
                platform="google",
            )
            for m in stage2.google_content_metas.get(rep, [])
        ]

        # 네이버 상위 콘텐츠 메타
        naver_metas = [
            ContentMeta(
                rank=m.get("rank", 1),
                title=m.get("title", ""),
                url=m.get("url", ""),
                h2_structure=m.get("h2_structure", []),
                publish_date=m.get("publish_date"),
                content_type=m.get("content_type", "other"),
                is_competitor=is_competitor(m.get("url", "")),
                platform="naver",
                exposure_area=m.get("exposure_area", ""),
            )
            for m in stage2.naver_content_metas.get(rep, [])
        ]

        # PAA 질문 (클러스터 키워드 기준 수집)
        paa_qs: list[str] = []
        for kw, _ in cd.keywords:
            paa_qs.extend(stage1.paa_questions.get(kw, []))
        paa_qs = list(dict.fromkeys(paa_qs))

        # 구글 SERP 피처
        gsf = stage2.google_serp_features.get(rep, {})
        google_serp = GoogleSerpFeatures(
            ai_overview=gsf.get("ai_overview", False),
            featured_snippet_exists=gsf.get("featured_snippet_exists", False),
            featured_snippet_url=gsf.get("featured_snippet_url"),
            paa_questions=gsf.get("paa_questions", []),
        )

        # 네이버 SERP 피처
        nsf = stage2.naver_serp_features.get(rep, {})
        naver_serp = NaverSerpFeatures(
            knowledge_snippet=nsf.get("knowledge_snippet", False),
            smart_block=nsf.get("smart_block", False),
            smart_block_components=nsf.get("smart_block_components", []),
        )

        # GEO 인용 (서비스별 분리)
        geo_cites = [
            GeoCitation(
                url=c["url"],
                domain=c["domain"],
                context_summary=c.get("context_summary", ""),
                source=c.get("source", "chatgpt"),
                is_own_domain=c.get("is_own_domain", False),
                is_competitor=is_competitor(c["url"]),
            )
            for c in stage3.citations.get(rep, [])
        ]

        return cls(
            cluster_id=cd.cluster_id,
            representative_keyword=rep,
            representative_rationale=cd.representative_rationale,
            archive_verdict=cd.archive_verdict,
            matched_archive_representative=cd.matched_archive_representative,
            is_focus=cd.is_focus,
            keywords=kw_models,
            total_volume_google=0,
            total_volume_naver=total_naver,
            total_volume_naver_pc=total_naver_pc,
            total_volume_naver_mobile=total_naver_mobile,
            volume_trend=trend,
            google_trend_series=vol.get("google_trend_series", []),
            naver_trend_series=vol.get("naver_trend_series", []),
            google_content_meta=google_metas,
            naver_content_meta=naver_metas,
            h2_topics=stage2.h2_topics.get(rep, []),
            paa_questions=paa_qs,
            google_serp_features=google_serp,
            naver_serp_features=naver_serp,
            geo_citations=geo_cites,
        )


# ── 리서처 최종 출력 ─────────────────────────────────────────────


class ResearchResult(BaseModel):
    """리서처 에이전트 최종 산출물."""

    run_date: str
    main_keyword: str
    entry_moment: str
    clusters: list[Cluster] = Field(default_factory=list)
    orphan_keywords: list[str] = Field(default_factory=list)
    # 구조화 입력에서 추출된 메타 정보
    intent: str = ""
    source_questions: list[str] = Field(default_factory=list)
    content_direction: str = ""
    extracted_seeds: list[str] = Field(default_factory=list)


# ── 플래너 공용 타입 ──────────────────────────────────────────────

FUNNEL_STAGE = Literal["awareness", "consideration", "conversion", "unclassified"]
GEO_TYPE = Literal["definition", "comparison", "problem_solving"]
DUPLICATE_VERDICT = Literal["new", "angle_shift", "update_existing"]


# ── 플래너 입력 ──────────────────────────────────────────────────


class PublishedContent(BaseModel):
    """기발행 콘텐츠 DB 항목."""

    url: str
    title: str
    main_keyword: str = ""
    publish_date: str | None = None
    funnel: FUNNEL_STAGE = "unclassified"
    # 구조/메타데이터
    h2_sections: list[str] = Field(default_factory=list)  # H2/H3 텍스트 목록
    excerpt: str = ""  # TL;DR 또는 meta description
    word_count: int = 0  # 본문 단어 수 추정
    content_type: str = "other"  # listicle / guide / comparison / definition / other
    category: str = ""  # 블로그 카테고리 레이블
    # 성과 데이터
    ctr: float | None = None
    search_rank: float | None = None
    avg_time_on_page: float | None = None  # seconds


class PlannerInput(BaseModel):
    """플래너 에이전트 입력."""

    # 사용자 입력 (input_template.md 기반)
    intent: list[str]  # ["비교 판단"] — 복수 선택 가능
    questions: list[str]  # 질문 형태 (카테고리 원본, 1개 이상)
    content_direction: list[str]  # ["판단 기준 제시"] — 복수 선택 가능

    # 리서처 산출물
    research_result: ResearchResult

    # 기발행 콘텐츠 DB
    published_contents: list[PublishedContent] = Field(default_factory=list)

    # 실행 컨텍스트
    target_month: str  # "2026-03"
    client_name: str = "wishket"


# ── 플래너 중간 데이터 ───────────────────────────────────────────


class DuplicateResult(BaseModel):
    """3단계: 개별 질문의 기발행 콘텐츠 중복 판정 결과."""

    keyword_overlap: float = Field(default=0, ge=0, le=1)
    title_similarity: float = Field(default=0, ge=0, le=1)
    topic_overlap: float = Field(default=0, ge=0, le=1)
    risk_score: float = Field(default=0, ge=0, le=1)  # 가중 합산
    verdict: DUPLICATE_VERDICT = "new"
    matched_content_url: str | None = None
    matched_content_title: str | None = None
    rationale: str = ""


class DerivedQuestion(BaseModel):
    """1~4단계를 거친 파생 질문 단위."""

    # 식별
    question_id: str = ""  # 파이프라인 내 기본 키 (q001, q002, ...)
    question: str  # 하위 질문 텍스트
    category: str  # 소속 카테고리 (사용자 입력 질문 원문)
    source_cluster_id: str = ""  # 리서처 클러스터 참조 (read-only)

    # 1단계: 카테고리 매핑
    mapping_rationale: str = ""  # LLM이 이 클러스터를 이 카테고리에 매핑한 이유
    exploration_order: int = 0   # 미사용 (탐색 경로 규칙 제거됨)

    # 2단계: 퍼널 태깅
    funnel: FUNNEL_STAGE = "unclassified"
    funnel_journey_reasoning: str = "" # 1순위 추론 사고 흐름 (질문→검색자 상태→전환 거리→결론)
    funnel_searcher_state: str = ""    # 검색자 상태 요약 (예: "외주 업체 선정 조건을 따지는 단계")
    funnel_judgment_basis: str = ""    # 판단 기준: "1순위 추론" | "2순위 시그널 보조"
    funnel_signals_used: str = ""      # 사용한 보조 시그널 (없으면 "없음")

    # 3단계: 중복 판정
    duplicate_result: DuplicateResult | None = None

    # 4단계: 우선순위
    priority_score: float = 0.0
    is_selected: bool = False
    is_waitlist: bool = False
    selection_rationale: str = ""

    # 5단계: 발행일 배정
    publish_date: str = ""  # "2026-03-02" ISO — Stage 5에서 배정


# ── 플래너 출력: 콘텐츠 세부 구조 (6단계) ──────────────────────────


class TitleSuggestion(BaseModel):
    """예상 제목안."""

    title: str
    strategy: Literal["seo", "ctr"]  # 검색 유입 최적화 / 클릭 유도 최적화


class H2Section(BaseModel):
    """H2 소제목 단위."""

    heading: str
    description: str = ""  # 다루는 내용 요약
    geo_pattern: GEO_TYPE | None = None  # 부차적 GEO 패턴 삽입 시


class ContentPiece(BaseModel):
    """6단계: 확정 콘텐츠 세부 구조."""

    # 식별
    content_id: str  # "cat1_01"
    question: str
    category: str

    # 태그
    funnel: FUNNEL_STAGE
    geo_type: GEO_TYPE

    # 6단계 설계
    publishing_purpose: str  # 발행 목적 (사용자 의도 정렬)
    title_suggestions: list[TitleSuggestion] = Field(default_factory=list)
    h2_structure: list[H2Section] = Field(default_factory=list)
    cta_suggestion: str = ""

    # 4단계 우선순위 점수 (정렬용)
    priority_score: float = 0.0

    # 5단계: 발행일 배정
    publish_date: str = ""  # DerivedQuestion.publish_date 전달

    # 4단계 선정 근거
    data_rationale: str = ""  # 리서처 수집 데이터 기반
    content_rationale: str = ""  # 기존 블로그 기반

    # 1~2단계 추론 근거 (플래너 파이프라인에서 전달)
    mapping_rationale: str = ""       # 카테고리 배정 근거
    funnel_journey_reasoning: str = ""  # 퍼널 판단 추론 흐름

    # 리서처 데이터 참조
    source_cluster_id: str = ""
    representative_keyword: str = ""
    monthly_volume_naver: int = 0
    volume_trend: TREND_DIRECTION = "stable"


# ── 플래너 출력: 캘린더 (7단계) ───────────────────────────────────


class CalendarEntry(BaseModel):
    """7단계: 발행 일정 항목."""

    date: str  # "2026-03-02" ISO
    day_of_week: str  # "월" / "수" / "금"
    content_id: str  # ContentPiece.content_id 참조
    is_holiday: bool = False


# ── 플래너 출력: 업데이트 후보 ─────────────────────────────────────


class UpdateCandidate(BaseModel):
    """기존 글 업데이트 후보 (0.7↑ 또는 각도 전환 불가)."""

    published_url: str
    published_title: str
    risk_score: float = Field(ge=0, le=1)
    improvement_points: list[str] = Field(default_factory=list)
    urgency: Literal["high", "medium", "low"] = "medium"


# ── 플래너 출력: 퍼널 분포 ────────────────────────────────────────


class FunnelDistribution(BaseModel):
    """퍼널 분포 현황 (5단계 균형 검증용)."""

    awareness: int = 0
    consideration: int = 0
    conversion: int = 0
    unclassified: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return self.awareness + self.consideration + self.conversion + self.unclassified


# ── 플래너 최종 출력 ──────────────────────────────────────────────


class ContentPlan(BaseModel):
    """플래너 에이전트 최종 산출물 — 월간 콘텐츠 기획 문서."""

    # 메타
    run_date: str
    target_month: str  # "2026-03"
    client_name: str

    # 전략 요약
    strategy_objective: str = ""  # 이번 달 핵심 목표
    strategy_direction: str = ""  # 전략 방향 요약

    # 입력 메타 (업로더/문서 생성용)
    intent: list[str] = Field(default_factory=list)            # 검색 의도 (예: ["비교 판단"])
    content_direction: list[str] = Field(default_factory=list) # 콘텐츠 방향성 (예: ["판단 기준 제시"])

    # 카테고리별 콘텐츠 기획
    categories: list[str] = Field(default_factory=list)  # 사용자 입력 질문 원본
    content_pieces: list[ContentPiece] = Field(default_factory=list)
    waitlist: list[DerivedQuestion] = Field(default_factory=list)

    # 퍼널 분포
    funnel_distribution: FunnelDistribution = Field(default_factory=FunnelDistribution)
    previous_month_funnel: FunnelDistribution | None = None

    # 발행 일정
    calendar: list[CalendarEntry] = Field(default_factory=list)

    # 기획 문서 (7단계 생성)
    planning_document: str = ""  # Stage 7에서 생성된 Markdown 기획 문서

    # 업데이트 후보
    update_candidates: list[UpdateCandidate] = Field(default_factory=list)

    # 파생 질문 전체 (추적용)
    all_derived_questions: list[DerivedQuestion] = Field(default_factory=list)
