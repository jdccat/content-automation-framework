"""리서처 에이전트 — spec v2 기반 3단계 파이프라인 구현.

Stage 1: 키워드 확장 및 클러스터링.
Stage 2: 검증 수집 — 클러스터별 구글/네이버 병렬 수집.
Stage 3: 외부 AI 환경 수집 — ChatGPT, Perplexity, Claude, Gemini.
입력 파싱은 정규식 휴리스틱. 미구현 도구는 _safe_tool_call로 격리.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import yaml

from core.schemas import (
    ClusterDraft,
    HubResearchData,
    ParsedInput,
    RawKeywordPool,
    ResearchProfile,
    ResearchResult,
    Stage1Output,
    Stage2Output,
    Stage3Output,
)
from core.agents.researcher import stage1 as stage1_mod
from core.agents.researcher.prompts import load_prompt
from core.agents.researcher.research_unit import stage2_validation, stage3_geo
from core.agents.researcher.assembler import (
    assemble_result,
    check_quality_gate,
)
from core.agents.researcher.archive import (
    filter_archive_seeds,
    filter_by_token_overlap,
    load_archive_clusters,
    load_archive_reps,
    save_archive,
)
from core.agents.researcher.snapshot import (
    save_hub_research_per_seed,
    save_manifest,
    save_snapshot,
)
from core.agents.researcher.stage1_hub import stage1_hub_research
from core.agents.researcher.parser import (
    _chunk,
    _extract_domain,
    _extract_keywords_from_question,
    _is_relevant_serp_item,
    _keyword_to_question,
    _map_content_type,
    _normalize_keyword,
    _parse_web_fetch_result,
    _strip_particle,
    parse_input,
    parse_json_input,
)
from core.tools.ai_search import ai_search
from core.tools.autocomplete import search_suggestions
from core.tools.google_paa import google_paa
from core.tools.google_related import google_related_searches
from core.tools.google_search import google_search
from core.tools.google_trends import google_keyword_trend
from core.tools.naver_datalab import naver_keyword_trend
from core.tools.naver_search import naver_blog_search
from core.tools.naver_searchad import naver_keyword_volume
from core.tools.naver_serp import naver_serp_features
from core.tools.perplexity_search import perplexity_search
from core.tools.geo_browser import geo_claude_browser, geo_gemini_browser
from core.tools.web_fetch import web_fetch

logger = logging.getLogger(__name__)

OWN_DOMAIN = "wishket.com"


# ── ResearcherAgent ──────────────────────────────────────────────


class ResearcherAgent:
    """spec v2 3단계 파이프라인 리서처. 구글/네이버 플랫폼별 분리 수집."""

    def __init__(self) -> None:
        config_path = Path(__file__).parent / "config.yaml"
        with open(config_path) as f:
            self._config: dict = yaml.safe_load(f)
        self._quality: dict = self._config.get("quality_gate", {})
        self._archive_cfg: dict = self._config.get("archive", {})
        # 경쟁사 도메인 (is_competitor 플래그용)
        self._competitor_domains: set[str] = set(
            d.lower() for d in self._config.get("competitor_domains", [])
            if d.lower() != OWN_DOMAIN
        )

    def _is_competitor(self, url: str) -> bool:
        """URL 도메인이 경쟁사 목록에 포함되는지 확인."""
        domain = _extract_domain(url)
        return any(cd in domain for cd in self._competitor_domains)

    # ── 공개 진입점 ──────────────────────────────────────────────

    async def run(
        self,
        input_text: str,
        client: str = "",
        *,
        snapshot_dir: str = "snapshots",
    ) -> ResearchResult:
        """메인 키워드 + 진입 모먼트를 받아 ResearchResult를 반환."""
        parsed = self._parse_input(input_text)
        run_date = str(date.today())
        logger.info(
            "리서처 시작: keyword=%s, moment=%s, seeds=%d",
            parsed.main_keyword, parsed.entry_moment, len(parsed.extracted_seeds),
        )

        if parsed.extracted_seeds:
            all_seeds = list(dict.fromkeys(parsed.extracted_seeds))
            seeds = all_seeds
        else:
            seeds = [parsed.main_keyword]
            all_seeds = seeds

        # LLM 기반 시드 필터
        if parsed.extracted_seeds and self._config.get("seed_filter", {}).get("enabled"):
            seeds = await self._filter_seeds(
                seeds, parsed.questions, parsed.intent, parsed.direction,
            )

        # 스냅샷: input
        save_snapshot("input", parsed, run_date, snapshot_dir)

        # 3단계 파이프라인
        stage1 = await self._stage1_expansion(
            parsed.main_keyword, seeds, parsed.questions,
            client=client, intent=parsed.intent, direction=parsed.direction,
            all_seeds=all_seeds,
            snapshot_dir=snapshot_dir, run_date=run_date,
        )
        # 스냅샷: stage1
        save_snapshot("stage1_clusters", stage1, run_date, snapshot_dir)

        # 포커스 클러스터의 대표 키워드만 2/3단계로 전달
        focus_reps = [
            cd.representative
            for cd in stage1.cluster_drafts
            if cd.representative and cd.is_focus
        ]
        # 포커스 클러스터의 전체 키워드 목록 (볼륨 전파용)
        focus_all_keywords: list[str] = []
        for cd in stage1.cluster_drafts:
            if cd.is_focus:
                focus_all_keywords.extend(kw for kw, _ in cd.keywords)
        focus_all_keywords = list(dict.fromkeys(focus_all_keywords))

        # 대표→PAA 매핑: 클러스터 내 모든 키워드의 PAA 질문을 대표 키워드로 모음
        rep_paa_questions: dict[str, list[str]] = {}
        for cd in stage1.cluster_drafts:
            if cd.is_focus and cd.representative:
                qs: list[str] = []
                for kw, _ in cd.keywords:
                    qs.extend(stage1.paa_questions.get(kw, []))
                rep_paa_questions[cd.representative] = list(dict.fromkeys(qs))

        stage2 = await self._stage2_validation(
            focus_reps,
            all_keywords=focus_all_keywords,
            paa_questions=rep_paa_questions,
            stage1_volumes=stage1.volumes,
            stage1_volumes_pc=stage1.volumes_pc,
            stage1_volumes_mobile=stage1.volumes_mobile,
        )
        # 스냅샷: stage2
        save_snapshot("stage2_serp", stage2, run_date, snapshot_dir)

        stage3 = await self._stage3_geo(focus_reps)
        # 스냅샷: stage3
        save_snapshot("stage3_geo", stage3, run_date, snapshot_dir)

        result = self._assemble_result(parsed, stage1, stage2, stage3)
        if not self._check_quality_gate(result):
            logger.warning("품질 게이트 실패: clusters=%d", len(result.clusters))

        self._save_archive(result, stage1)
        return result

    async def run_json(
        self,
        data: dict,
        *,
        output_dir: str = "",
        snapshot_dir: str = "snapshots",
        profile: ResearchProfile | None = None,
    ) -> list[HubResearchData]:
        """JSON 입력 → 시드별 독립 출력.

        흐름:
        1. parse_json_input() → ParsedInput
        2. stage1_hub_research() → list[HubResearchData]
        3. 시드별 JSON 파일 + manifest.json 저장
        4. list[HubResearchData] 반환
        """
        parsed = parse_json_input(data)
        run_date = str(date.today())

        if not parsed.seed_questions:
            logger.warning("run_json: 파싱된 시드 질문 없음")
            return []

        if not output_dir:
            output_dir = f"output/researcher/{run_date}"

        logger.info(
            "run_json 시작: seeds=%d, output_dir=%s",
            len(parsed.seed_questions), output_dir,
        )

        # 스냅샷: input
        save_snapshot("input", parsed, run_date, snapshot_dir)

        # Stage 1 허브 리서치
        hub_data_list = await stage1_hub_research(
            parsed.seed_questions,
            config=self._config,
            safe_tool_call=self._safe_tool_call,
            llm_call_fn=self._llm_call,
            search_suggestions_fn=search_suggestions,
            google_related_fn=google_related_searches,
            google_paa_fn=google_paa,
            naver_keyword_volume_fn=naver_keyword_volume,
            google_search_fn=google_search,
            google_keyword_trend_fn=google_keyword_trend,
            naver_keyword_trend_fn=naver_keyword_trend,
            naver_blog_search_fn=naver_blog_search,
            web_fetch_fn=web_fetch,
            naver_serp_features_fn=naver_serp_features,
            ai_search_fn=ai_search,
            perplexity_search_fn=perplexity_search,
            geo_claude_fn=geo_claude_browser,
            geo_gemini_fn=geo_gemini_browser,
            profile=profile,
            snapshot_dir=snapshot_dir,
            run_date=run_date,
        )

        # 시드별 파일 + manifest 저장
        save_hub_research_per_seed(hub_data_list, run_date, output_dir)
        save_manifest(hub_data_list, run_date, output_dir, parsed=parsed)

        logger.info("run_json 완료: %d 시드 저장", len(hub_data_list))
        return hub_data_list

    # ── 입력 파싱 ────────────────────────────────────────────────

    @staticmethod
    def _parse_input(text: str) -> ParsedInput:
        return parse_input(text)

    # ── 안전 도구 래퍼 ────────────────────────────────────────────

    @staticmethod
    async def _safe_tool_call(label: str, coro, default=None):
        """NotImplementedError/일반 에러 격리. 파이프라인은 계속 진행."""
        return await stage1_mod.safe_tool_call(label, coro, default)

    async def _llm_call(
        self, label: str, system: str, user: str,
        model: str = "", max_tokens: int = 4096,
        retries: int = 2,
    ) -> str:
        """OpenAI Chat Completions 호출. 빈 응답/실패 시 최대 retries회 재시도."""
        return await stage1_mod.llm_call(
            label, system, user, self._config,
            model=model, max_tokens=max_tokens, retries=retries,
        )

    # ── 시드 필터 ─────────────────────────────────────────────────

    async def _filter_seeds(
        self,
        seeds: list[str],
        questions: list[str],
        intent: str,
        direction: str,
    ) -> list[str]:
        """시드 키워드에서 검색 도구 투입 가치 없는 수식어만 제거.

        판단 기준: '이 시드를 검색 도구에 넣었을 때 질문과 관련된 결과가 나올
        가능성이 있는가'. 도메인 키워드(ERP, 외주, 비용 등)는 단독이라도 keep.
        순수 수식어/의문사(이유, 기준, 방법, 무엇, 어떻게)만 drop.
        """
        if not seeds:
            return seeds

        import json

        q_block = "\n".join(f"- {q}" for q in questions[:10]) if questions else "(없음)"
        system = load_prompt(
            "seed_filter",
            q_block=q_block,
            intent=intent,
            direction=direction,
        )
        user = json.dumps(seeds, ensure_ascii=False)

        raw = await self._llm_call("seed_filter", system, user, max_tokens=2048)
        if not raw:
            logger.warning("시드 필터: LLM 응답 비어있음 — 원본 반환")
            return seeds

        try:
            result = json.loads(raw)
            if isinstance(result, list) and result:
                logger.info("시드 필터: %d → %d (제거: %s)",
                            len(seeds), len(result),
                            [s for s in seeds if s not in result])
                return result
        except (json.JSONDecodeError, TypeError):
            logger.warning("시드 필터: 파싱 실패 — 원본 반환")

        return seeds

    # ── 1단계: 키워드 확장 및 클러스터링 ─────────────────────────

    async def _stage1_expansion(
        self, main_kw: str, seeds: list[str], questions: list[str] | None = None,
        *, client: str = "", intent: str = "", direction: str = "",
        all_seeds: list[str] | None = None,
        snapshot_dir: str = "", run_date: str = "",
    ) -> Stage1Output:
        return await stage1_mod.stage1_expansion(
            main_kw, seeds, questions,
            client=client, intent=intent, direction=direction,
            all_seeds=all_seeds,
            config=self._config,
            safe_tool_call_fn=self._safe_tool_call,
            llm_call_fn=self._llm_call,
            load_archive_reps_fn=self._load_archive_reps,
            load_archive_clusters_fn=self._load_archive_clusters,
            search_suggestions_fn=search_suggestions,
            google_related_fn=google_related_searches,
            google_paa_fn=google_paa,
            naver_keyword_volume_fn=naver_keyword_volume,
            snapshot_dir=snapshot_dir,
            run_date=run_date,
        )

    def _stage1b_customer_language(self, client: str = "") -> list[str]:
        return stage1_mod.stage1b_customer_language(client, self._config)

    @staticmethod
    def _deduplicate_keywords(
        pool: RawKeywordPool,
    ) -> list[tuple[str, str]]:
        return stage1_mod.deduplicate_keywords(pool)

    async def _stage1d_llm_clustering(
        self, deduped: list[tuple[str, str]],
    ) -> tuple[list[ClusterDraft], list[str]]:
        return await stage1_mod._stage1d_llm_clustering(
            deduped, llm_call_fn=self._llm_call, config=self._config,
        )

    async def _stage1e_llm_representative(
        self, cluster_drafts: list[ClusterDraft], volumes: dict[str, int],
    ) -> None:
        await stage1_mod._stage1e_llm_representative(
            cluster_drafts, volumes,
            llm_call_fn=self._llm_call, config=self._config,
        )

    async def _stage1f_archive_comparison(
        self, cluster_drafts: list[ClusterDraft],
    ) -> None:
        await stage1_mod._stage1f_archive_comparison(
            cluster_drafts,
            llm_call_fn=self._llm_call,
            config=self._config,
            load_archive_reps_fn=self._load_archive_reps,
            load_archive_clusters_fn=self._load_archive_clusters,
        )

    async def _stage1g_focus_selection(
        self,
        cluster_drafts: list[ClusterDraft],
        questions: list[str],
        volumes: dict[str, int] | None = None,
    ) -> None:
        await stage1_mod._stage1g_focus_selection(
            cluster_drafts, questions, volumes,
            llm_call_fn=self._llm_call, config=self._config,
        )

    # ── 2단계: 검증 수집 (구글/네이버 병렬) ─────────────────────

    async def _stage2_validation(
        self, reps: list[str],
        *,
        all_keywords: list[str] | None = None,
        paa_questions: dict[str, list[str]] | None = None,
        stage1_volumes: dict[str, int] | None = None,
        stage1_volumes_pc: dict[str, int] | None = None,
        stage1_volumes_mobile: dict[str, int] | None = None,
    ) -> Stage2Output:
        return await stage2_validation(
            reps,
            all_keywords=all_keywords,
            paa_questions=paa_questions,
            stage1_volumes=stage1_volumes,
            stage1_volumes_pc=stage1_volumes_pc,
            stage1_volumes_mobile=stage1_volumes_mobile,
            safe_tool_call=self._safe_tool_call,
            google_search_fn=google_search,
            naver_keyword_volume_fn=naver_keyword_volume,
            google_keyword_trend_fn=google_keyword_trend,
            naver_keyword_trend_fn=naver_keyword_trend,
            naver_blog_search_fn=naver_blog_search,
            web_fetch_fn=web_fetch,
            naver_serp_features_fn=naver_serp_features,
        )

    # ── 3단계: GEO 인용 수집 ─────────────────────────────────────

    async def _stage3_geo(self, reps: list[str]) -> Stage3Output:
        return await stage3_geo(
            reps,
            safe_tool_call=self._safe_tool_call,
            ai_search_fn=ai_search,
            perplexity_search_fn=perplexity_search,
            geo_claude_fn=geo_claude_browser,
            geo_gemini_fn=geo_gemini_browser,
        )

    # ── 결과 조립 ────────────────────────────────────────────────

    def _assemble_result(
        self,
        parsed: ParsedInput,
        stage1: Stage1Output,
        stage2: Stage2Output,
        stage3: Stage3Output,
    ) -> ResearchResult:
        return assemble_result(
            parsed, stage1, stage2, stage3,
            is_competitor=self._is_competitor,
        )

    def _check_quality_gate(self, result: ResearchResult) -> bool:
        return check_quality_gate(result, self._quality)

    # ── 아카이브 ─────────────────────────────────────────────────

    @staticmethod
    def _filter_by_token_overlap(
        candidates: list[str],
        reference: list[str],
        min_overlap: int = 1,
    ) -> list[str]:
        return filter_by_token_overlap(candidates, reference, min_overlap)

    @staticmethod
    def _filter_archive_seeds(
        archive_seeds: list[str],
        reference_seeds: list[str],
    ) -> list[str]:
        return filter_archive_seeds(archive_seeds, reference_seeds)

    def _load_archive_reps(self) -> list[str]:
        return load_archive_reps(self._archive_cfg)

    def _load_archive_clusters(self) -> dict[str, list[str]]:
        return load_archive_clusters(self._archive_cfg)

    def _save_archive(
        self, result: ResearchResult, stage1: Stage1Output | None = None,
    ) -> None:
        save_archive(self._archive_cfg, result, stage1)
