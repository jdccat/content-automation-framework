"""플래너 에이전트 — 7단계 순차 파이프라인.

구현 참조: core/agents/planner/IMPLEMENTATION.md
단계별 구현 현황:
  Phase 0: __init__, _validate_input, _llm_call, _load_guide, run, _save_snapshot
  Phase 1: _stage1_sort
  Phase 2: _stage2_funnel
  Phase 3: _stage3_duplicate, _check_one_duplicate
  Phase 4: _stage4_priority
  Phase 5: _stage5_balance (발행일 배정 포함), archive.py
  Phase 6: _stage6_structure
  Phase 7: _stage7_document, _stage7_calendar (단순 조립), _assemble
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from core.schemas import (
    CalendarEntry,
    Cluster,
    ContentPiece,
    ContentPlan,
    DerivedQuestion,
    DuplicateResult,
    FunnelDistribution,
    H2Section,
    PlannerInput,
    PublishedContent,
    TitleSuggestion,
    UpdateCandidate,
)
from core.agents.planner import archive as archive_mod

logger = logging.getLogger(__name__)

# 발행 요일: weekday() 인덱스 → 한국어 요일명 (월/수/금)
PUBLISH_WEEKDAYS: dict[int, str] = {0: "월", 2: "수", 4: "금"}


def _strip_json_fence(text: str) -> str:
    """LLM 응답에서 마크다운 JSON 코드 펜싱 제거.

    ```json ... ``` 또는 ``` ... ``` 형태를 벗겨낸다.
    닫는 펜스 없이 잘린 응답(max_tokens 초과)도 처리한다.
    """
    text = text.strip()
    # 완전한 펜스: 열고 닫는 ``` 모두 있음
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if m:
        return m.group(1).strip()
    # 열린 펜스만 있는 경우 (잘린 응답) — 펜스 헤더만 제거
    m2 = re.match(r"^```(?:json)?\s*([\s\S]*)$", text)
    if m2:
        return m2.group(1).strip()
    return text


# ── Stage 3 헬퍼 (모듈 레벨) ─────────────────────────────────────

# 한국어 중복 판정에서 의미 없는 기능어·조사 토큰 제거용
# 한국어 핵심 명사("외주", "개발", "업체" 등)는 2자이므로 2자 이상 + 불용어 방식을 사용
_KR_STOPWORDS: frozenset[str] = frozenset({
    # 의문 표현
    "이란", "이라", "이고", "이며", "이면", "이라는",
    "인가요", "있나요", "있습니까", "입니까", "입니다", "합니다",
    "무엇인가요", "무엇을", "무엇이", "어떻게", "어떤", "어디서",
    "왜요", "인지",
    # 조사·어미
    "위해", "통해", "대한", "관한", "위한", "통한",
    "에서", "에게", "으로", "로써", "부터", "까지", "보다",
    "처럼", "만큼", "때문", "할때", "하는", "있는", "없는",
    "해야", "해도", "할수", "수있는", "그리고", "또는",
    "하면", "하고", "위해서", "때문에",
})


def _tokenize(text: str) -> set[str]:
    """공백·구두점 기준으로 토큰화. 2자 이상 + 불용어 제외.

    한국어 핵심 명사("외주", "개발", "업체" 등)가 2자이므로 2자 이상 기준을 사용한다.
    """
    tokens = re.split(r"[\s,.·/\-\[\]()?!「」『』<>]+", text.lower())
    return {t for t in tokens if len(t) >= 2 and t not in _KR_STOPWORDS}


def _compute_keyword_overlap(
    question: str,
    cluster_kws: list[str],
    pub: PublishedContent,
) -> float:
    """파생 질문·클러스터 키워드 vs 기발행 콘텐츠 제목·키워드 토큰 겹침 비율.

    반환값: 0.0~1.0 (질문 토큰 중 기발행 콘텐츠에 등장하는 비율)
    """
    q_tokens = _tokenize(f"{question} {' '.join(cluster_kws)}")
    pub_tokens = _tokenize(f"{pub.main_keyword} {pub.title}")
    if not q_tokens:
        return 0.0
    return len(q_tokens & pub_tokens) / len(q_tokens)


def _compute_urgency(pub: PublishedContent) -> str:
    """기발행 콘텐츠 발행일 기준 업데이트 긴급도 산출.

    18개월↑ → high, 6개월↑ → medium, 나머지 → low
    """
    if not pub.publish_date:
        return "low"
    try:
        pub_dt = datetime.strptime(pub.publish_date, "%Y-%m-%d").date()
        today = date.today()
        months = (today.year - pub_dt.year) * 12 + (today.month - pub_dt.month)
        if months >= 18:
            return "high"
        if months >= 6:
            return "medium"
    except ValueError:
        pass
    return "low"


class PlannerAgent:
    """spec.md 기반 7단계 콘텐츠 기획 파이프라인."""

    def __init__(self, client_name: str = "wishket") -> None:
        config_path = Path(__file__).parent / "config.yaml"
        with open(config_path, encoding="utf-8") as f:
            self._cfg: dict = yaml.safe_load(f)

        self._client_name = client_name

        # 가이드 경로: 클라이언트별 우선, 없으면 기본
        self._guides_path = Path(self._cfg["guides_path"])
        self._client_guides_path = Path(f"clients/{client_name}/guides")

        # 모델
        self._model_main: str = self._cfg["models"]["main"]
        self._model_mini: str = self._cfg["models"]["mini"]

        # 입력 검증용 허용값 (config.yaml과 동기화)
        self._valid_intents: set[str] = set(self._cfg.get("valid_intents", []))
        self._valid_directions: set[str] = set(self._cfg.get("valid_directions", []))

        # 아카이브 경로
        self._archive_runs_dir: str = self._cfg["archive"]["runs_dir"]

        logger.info(
            "PlannerAgent 초기화 완료: client=%s, model=%s",
            client_name,
            self._model_main,
        )

    # ── 공개 진입점 ───────────────────────────────────────────────

    async def run(
        self,
        planner_input: PlannerInput,
        stop_at_stage: int = 7,
        start_at_stage: int = 1,
        snapshot_dir: str = "snapshots/planner",
    ) -> ContentPlan | dict:
        """7단계 파이프라인 실행.

        stop_at_stage=N  : N단계 완료 후 중간 결과 dict를 반환한다.
        stop_at_stage=7  : ContentPlan을 반환한다.
        stop_at_stage=0  : 입력 검증만 수행하고 빈 dict를 반환한다.
        start_at_stage=N : 이전 단계(N-1)의 가장 최근 스냅샷을 로드해 N단계부터 실행한다.
                           stop_at_stage가 start_at_stage보다 작으면 ValueError를 발생시킨다.
        """
        # 스냅샷 재개 시 입력 검증 생략 (이전 단계에서 이미 검증됨)
        if start_at_stage <= 1:
            self._validate_input(planner_input)

        if stop_at_stage > 0 and stop_at_stage < start_at_stage:
            raise ValueError(
                f"stop_at_stage({stop_at_stage}) < start_at_stage({start_at_stage}): "
                "중단 단계가 시작 단계보다 앞에 있습니다."
            )

        run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")

        monthly_count = self._compute_monthly_count(planner_input.target_month)
        logger.info(
            "플래너 시작: month=%s, client=%s, categories=%d, intent=%s, "
            "monthly_count=%d, start=%d, stop=%d, run_id=%s",
            planner_input.target_month,
            planner_input.client_name,
            len(planner_input.questions),
            planner_input.intent,
            monthly_count,
            start_at_stage,
            stop_at_stage,
            run_id,
        )

        # 모든 중간 상태 변수를 초기값으로 선언 (stop_at_stage 분기 안전성)
        derived: list[DerivedQuestion] = []
        candidates: list[UpdateCandidate] = []
        selected: list[DerivedQuestion] = []
        dist = FunnelDistribution()
        prev_dist: FunnelDistribution | None = None
        pieces: list[ContentPiece] = []
        calendar: list[CalendarEntry] = []

        # ── Stage 0: 초기화 + 입력 검증만 ───────────────────────
        if stop_at_stage < 1:
            logger.info("Stage 0 완료: 입력 검증 통과")
            return {}

        # ── 이전 단계 스냅샷 로드 (재개 시) ─────────────────────
        if start_at_stage > 1:
            derived = self._load_derived_from_snapshot(start_at_stage - 1, snapshot_dir)
            # stage 5 이상 재개 시 selected 복원 (stage4 스냅샷에 is_selected 플래그 포함)
            if start_at_stage >= 5:
                selected = [dq for dq in derived if dq.is_selected]
                logger.info("스냅샷에서 selected 복원: %d개", len(selected))

        # ── Stage 1: 카테고리 매핑 + 탐색 경로 정렬 ─────────────
        if start_at_stage <= 1:
            logger.info("=== Stage 1: 카테고리 매핑 + 탐색 경로 정렬 ===")
            derived = await self._stage1_sort(planner_input)
            self._save_snapshot(
                "stage1",
                {"stage": 1, "derived_questions": [q.model_dump() for q in derived]},
                run_id,
                snapshot_dir,
            )
            if stop_at_stage == 1:
                return {"stage": 1, "derived_questions": [q.model_dump() for q in derived]}

        # ── Stage 2: 퍼널 태깅 ───────────────────────────────────
        if start_at_stage <= 2:
            logger.info("=== Stage 2: 퍼널 태깅 ===")
            derived = await self._stage2_funnel(derived, planner_input)
            self._save_snapshot(
                "stage2",
                {"stage": 2, "derived_questions": [q.model_dump() for q in derived]},
                run_id,
                snapshot_dir,
            )
            if stop_at_stage == 2:
                return {"stage": 2, "derived_questions": [q.model_dump() for q in derived]}

        # ── Stage 3: 기발행 콘텐츠 중복 판정 ───────────────────
        if start_at_stage <= 3:
            logger.info("=== Stage 3: 중복 판정 ===")
            derived, candidates = await self._stage3_duplicate(derived, planner_input)
            self._save_snapshot(
                "stage3",
                {
                    "stage": 3,
                    "derived_questions": [q.model_dump() for q in derived],
                    "update_candidates": [c.model_dump() for c in candidates],
                },
                run_id,
                snapshot_dir,
            )
            if stop_at_stage == 3:
                return {
                    "stage": 3,
                    "derived_questions": [q.model_dump() for q in derived],
                    "update_candidates": [c.model_dump() for c in candidates],
                }

        # ── Stage 4: 우선순위 배정 + 선정 근거 ─────────────────
        if start_at_stage <= 4:
            logger.info("=== Stage 4: 우선순위 배정 ===")
            derived, selected = await self._stage4_priority(derived, planner_input)
            self._save_snapshot(
                "stage4",
                {
                    "stage": 4,
                    "all_questions": [q.model_dump() for q in derived],
                    "selected": [q.model_dump() for q in selected],
                },
                run_id,
                snapshot_dir,
            )
            if stop_at_stage == 4:
                return {
                    "stage": 4,
                    "all_questions": [q.model_dump() for q in derived],
                    "selected": [q.model_dump() for q in selected],
                }

        # ── Stage 5: 퍼널 균형 검증 + 발행일 배정 ──────────────
        if start_at_stage <= 5:
            logger.info("=== Stage 5: 퍼널 균형 검증 + 발행일 배정 ===")
            selected, dist, prev_dist = self._stage5_balance(selected, planner_input)
            self._save_snapshot(
                "stage5",
                {
                    "stage": 5,
                    "selected": [q.model_dump() for q in selected],
                    "funnel_distribution": dist.model_dump(),
                    "previous_month_funnel": prev_dist.model_dump() if prev_dist else None,
                },
                run_id,
                snapshot_dir,
            )
            if stop_at_stage == 5:
                return {
                    "stage": 5,
                    "selected": [q.model_dump() for q in selected],
                    "funnel_distribution": dist.model_dump(),
                }

        # ── Stage 6: 콘텐츠 세부 구조 설계 ─────────────────────
        if start_at_stage <= 6:
            logger.info("=== Stage 6: 콘텐츠 세부 구조 설계 ===")
            pieces = await self._stage6_structure(selected, planner_input)
            self._save_snapshot(
                "stage6",
                {"stage": 6, "content_pieces": [p.model_dump() for p in pieces]},
                run_id,
                snapshot_dir,
            )
            if stop_at_stage == 6:
                return {"stage": 6, "content_pieces": [p.model_dump() for p in pieces]}

        # ── Stage 7: 기획 문서 생성 + 캘린더 조립 ──────────────
        logger.info("=== Stage 7: 기획 문서 생성 ===")
        planning_document = await self._stage7_document(pieces, dist, planner_input, prev_dist)
        calendar = self._stage7_calendar(pieces, planner_input)

        # ── 조립 ─────────────────────────────────────────────────
        logger.info("=== 조립: ContentPlan ===")
        plan = self._assemble(
            derived, selected, pieces, calendar,
            dist, prev_dist, candidates,
            planner_input, run_id,
            planning_document=planning_document,
        )
        self._save_snapshot("final", plan.model_dump(), run_id, snapshot_dir)

        return plan

    # ── 입력 검증 ────────────────────────────────────────────────

    def _validate_input(self, pi: PlannerInput) -> None:
        """PlannerInput 유효성 검사. 실패 시 ValueError."""
        errors: list[str] = []

        # intent
        if not pi.intent:
            errors.append(
                "질문 의도를 정보 탐색 / 비교 판단 / 추천 중 1개 이상 선택해 주세요."
            )
        else:
            invalid = set(pi.intent) - self._valid_intents
            if invalid:
                errors.append(
                    f"유효하지 않은 질문 의도: {invalid}. "
                    "정보 탐색 / 비교 판단 / 추천 중에서 선택하세요."
                )

        # questions — 존재 여부
        if not pi.questions:
            errors.append("질문을 최소 1개 이상 입력해 주세요.")
        else:
            # 완결된 질문 형태 간이 체크: 6자 미만이면 키워드 나열 가능성 높음
            for q in pi.questions:
                if len(q.strip()) < 6:
                    errors.append(
                        "키워드가 아닌 질문 형태로 입력해 주세요. "
                        "예: '외주 개발' → '외주 개발 진행 과정을 알려줘'"
                    )
                    break

        # content_direction
        if not pi.content_direction:
            errors.append(
                "콘텐츠 방향성을 카테고리 포지셔닝 / 문제 인식 확산 / "
                "판단 기준 제시 / 실행 가이드 중 1개 이상 선택해 주세요."
            )
        else:
            invalid = set(pi.content_direction) - self._valid_directions
            if invalid:
                errors.append(f"유효하지 않은 콘텐츠 방향성: {invalid}.")

        if errors:
            raise ValueError("\n".join(errors))

    # ── LLM 호출 래퍼 ────────────────────────────────────────────

    async def _llm_call(
        self,
        label: str,
        system: str,
        user: str,
        model: str = "",
        max_tokens: int = 4096,
        retries: int = 2,
    ) -> str:
        """프로바이더 디스패처. claude-* → Anthropic, 나머지 → OpenAI."""
        _model = model or self._model_main
        if _model.startswith("claude"):
            return await self._llm_call_anthropic(label, system, user, _model, max_tokens, retries)
        return await self._llm_call_openai(label, system, user, _model, max_tokens, retries)

    async def _llm_call_anthropic(
        self,
        label: str,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        retries: int,
    ) -> str:
        """Anthropic Messages API 호출. 빈 응답/실패 시 최대 retries회 재시도.

        - temperature: 0 (결정론적)
        - max_tokens 사용 (Anthropic SDK 표준)
        - stop_reason == 'max_tokens' 시 경고 로그
        """
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()  # ANTHROPIC_API_KEY 환경변수에서 자동 로드

        last_exc: Exception | None = None
        for attempt in range(1, retries + 2):
            try:
                resp = await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=0,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                if resp.stop_reason == "max_tokens":
                    logger.warning("[%s] 응답이 max_tokens에서 잘림", label)
                content = resp.content[0].text if resp.content else ""
                if not content.strip():
                    raise ValueError("빈 응답")
                logger.debug("[%s] Anthropic 완료 (시도 %d/%d)", label, attempt, retries + 1)
                return content
            except Exception as exc:
                last_exc = exc
                if attempt == retries + 1:
                    logger.error("[%s] Anthropic 호출 실패 %d회: %s", label, retries + 1, exc)
                    raise
                wait = 2 ** (attempt - 1)
                logger.warning("[%s] 재시도 %d/%d, %ds 대기: %s", label, attempt, retries, wait, exc)
                await asyncio.sleep(wait)

        raise RuntimeError(f"[{label}] 예상치 못한 종료") from last_exc

    async def _llm_call_openai(
        self,
        label: str,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        retries: int,
    ) -> str:
        """OpenAI Chat Completions 호출. 빈 응답/실패 시 최대 retries회 재시도.

        - temperature: 0 (결정론적)
        - system 또는 user에 'JSON' 포함 시 json_object 모드 자동 설정
        - max_completion_tokens 사용 (max_tokens 사용 금지)
        """
        from openai import AsyncOpenAI
        client = AsyncOpenAI()  # OPENAI_API_KEY 환경변수에서 자동 로드

        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if "JSON" in system or "JSON" in user:
            kwargs["response_format"] = {"type": "json_object"}

        last_exc: Exception | None = None
        for attempt in range(1, retries + 2):
            try:
                resp = await client.chat.completions.create(**kwargs)
                choice = resp.choices[0]
                content = choice.message.content or ""
                if choice.finish_reason == "length":
                    logger.warning("[%s] 응답이 max_tokens에서 잘림", label)
                if not content.strip():
                    raise ValueError("빈 응답")
                logger.debug("[%s] OpenAI 완료 (시도 %d/%d)", label, attempt, retries + 1)
                return content
            except Exception as exc:
                last_exc = exc
                if attempt == retries + 1:
                    logger.error("[%s] OpenAI 호출 실패 %d회: %s", label, retries + 1, exc)
                    raise
                wait = 2 ** (attempt - 1)
                logger.warning("[%s] 재시도 %d/%d, %ds 대기: %s", label, attempt, retries, wait, exc)
                await asyncio.sleep(wait)

        raise RuntimeError(f"[{label}] 예상치 못한 종료") from last_exc

    # ── 가이드 로더 ──────────────────────────────────────────────

    def _load_guide(self, filename: str) -> str:
        """가이드 파일 로드. 클라이언트 경로 우선, 없으면 기본 경로.

        파일이 어디에도 없으면 빈 문자열 반환 + 경고 로그.
        호출 측은 빈 문자열 수신 시 '기본값 적용됨' 처리.
        """
        client_path = self._client_guides_path / filename
        default_path = self._guides_path / filename

        if client_path.exists():
            logger.debug("가이드 로드 (클라이언트): %s", client_path)
            return client_path.read_text(encoding="utf-8")
        elif default_path.exists():
            logger.debug("가이드 로드 (기본): %s", default_path)
            return default_path.read_text(encoding="utf-8")
        else:
            logger.warning("가이드 파일 없음: %s — 기본값 적용됨", filename)
            return ""

    # ── 스냅샷 저장 ──────────────────────────────────────────────

    @staticmethod
    def _compute_monthly_count(target_month: str) -> int:
        """target_month의 실제 발행 요일(월·수·금) 개수를 반환한다.

        예: "2026-02" → 12, "2026-03" → 13
        """
        year, month = map(int, target_month.split("-"))
        count = 0
        for day in range(1, 32):
            try:
                d = date(year, month, day)
            except ValueError:
                break
            if d.weekday() in PUBLISH_WEEKDAYS:
                count += 1
        return count

    @staticmethod
    def _save_snapshot(
        name: str,
        data: dict,
        run_id: str,
        snapshot_dir: str,
    ) -> Path:
        """단계별 중간 결과를 JSON으로 저장.

        저장 경로: {snapshot_dir}/{run_id}_{name}.json
        run_id는 실행 시작 시각(예: 2026-02-27_143052) — 같은 날 여러 번 실행해도 덮어쓰지 않는다.
        """
        d = Path(snapshot_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{run_id}_{name}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("스냅샷 저장: %s", path)
        return path

    @staticmethod
    def _find_latest_snapshot(name: str, snapshot_dir: str) -> Path | None:
        """지정 이름의 가장 최근 스냅샷 파일 경로 반환.

        패턴: {snapshot_dir}/*_{name}.json
        파일명을 내림차순 정렬 — run_id가 ISO 형식(2026-02-27_143052)이므로
        lexicographic 정렬이 시간 순서와 일치한다.
        """
        d = Path(snapshot_dir)
        if not d.exists():
            return None
        candidates = sorted(d.glob(f"*_{name}.json"), key=lambda p: p.stat().st_mtime)
        return candidates[-1] if candidates else None

    def _load_derived_from_snapshot(
        self, stage: int, snapshot_dir: str
    ) -> list[DerivedQuestion]:
        """지정 단계의 가장 최근 스냅샷에서 파생 질문 목록을 로드.

        스냅샷 파일이 없으면 FileNotFoundError를 발생시킨다.
        """
        snap_path = self._find_latest_snapshot(f"stage{stage}", snapshot_dir)
        if snap_path is None:
            raise FileNotFoundError(
                f"Stage {stage} 스냅샷 없음 — "
                f"{snapshot_dir}/*_stage{stage}.json 을 먼저 생성하세요."
            )
        logger.info("스냅샷 로드 (재개 stage %d): %s", stage, snap_path)
        with open(snap_path, encoding="utf-8") as f:
            snap_data = json.load(f)
        # stage4: "all_questions", stage5: "selected", 나머지: "derived_questions"
        raw_list = (
            snap_data.get("derived_questions")
            or snap_data.get("all_questions")
            or snap_data.get("selected")
            or []
        )
        derived = [DerivedQuestion(**dq) for dq in raw_list]
        logger.info("파생 질문 %d개 로드 완료", len(derived))
        return derived

    # ── Stage stubs ───────────────────────────────────────────────

    async def _stage1_sort(self, pi: PlannerInput) -> list[DerivedQuestion]:
        """클러스터 → 카테고리 의미 매핑 (LLM 1회).

        각 클러스터를 사용자 입력 질문(카테고리) 중 하나에 배정한다.
        매핑 불가 클러스터는 category="unassigned"로 보존한다.
        """
        clusters = pi.research_result.clusters
        if not clusters:
            logger.warning("Stage 1: 클러스터 없음 — 빈 목록 반환")
            return []

        system = self._load_guide("prompts/stage1_sort.md")

        # 클러스터별 리서처 수집 데이터 요약
        # - paa_questions: 실제 사용자 질문 패턴 (최대 8개)
        # - keywords: 세부 키워드 목록 (최대 8개)
        # - naver_top_titles: 네이버 상위 콘텐츠 제목 — 가장 신뢰도 높은 신호 (최대 5개)
        # - google_top_titles: 구글 상위 콘텐츠 제목 (최대 3개)
        # h2_topics는 크롤링 노이즈가 심해 제외
        cluster_summaries = [
            {
                "cluster_id": c.cluster_id,
                "representative_keyword": c.representative_keyword,
                "paa_questions": c.paa_questions[:8],
                "keywords": [k.keyword for k in c.keywords[:8]],
                "naver_top_titles": [m.title for m in c.naver_content_meta[:5]],
                "google_top_titles": [m.title for m in c.google_content_meta[:3]],
            }
            for c in clusters
        ]

        monthly_count = self._compute_monthly_count(pi.target_month)
        n_cats = max(1, len(pi.questions))
        per_cat = max(1, monthly_count // n_cats)

        user_content = json.dumps(
            {
                "categories": pi.questions,
                "per_category_target": per_cat,
                "clusters": cluster_summaries,
            },
            ensure_ascii=False,
            indent=2,
        )

        raw = await self._llm_call(
            label="stage1_sort",
            system=system,
            user=user_content,
            max_tokens=16384,
        )

        # JSON 파싱
        try:
            parsed = json.loads(_strip_json_fence(raw))
        except json.JSONDecodeError as exc:
            logger.error("Stage 1: JSON 파싱 실패: %s\n원문(앞 300자): %s", exc, raw[:300])
            raise

        mappings: list[dict] = parsed.get("mappings", [])

        # derived_questions 배열 → derived_question 정규화 (단일 문자열 호환)
        for m in mappings:
            if "derived_questions" in m:
                qs = [q for q in m["derived_questions"] if q]
                m["derived_question"] = qs[0] if qs else ""
                m["_extra_questions"] = qs[1:]
            elif "derived_question" not in m:
                m["derived_question"] = ""
                m["_extra_questions"] = []
            else:
                m.setdefault("_extra_questions", [])

        # 누락 cluster_id → unassigned fallback
        mapped_ids = {m["cluster_id"] for m in mappings}
        for c in clusters:
            if c.cluster_id not in mapped_ids:
                logger.warning("Stage 1: 매핑 누락 → unassigned: %s", c.cluster_id)
                q = (
                    c.paa_questions[0]
                    if c.paa_questions
                    else f"{c.representative_keyword}란 무엇인가요?"
                )
                mappings.append(
                    {
                        "cluster_id": c.cluster_id,
                        "category": "unassigned",
                        "derived_question": q,
                        "_extra_questions": [],
                        "mapping_reason": "fallback: LLM 응답 누락",
                    }
                )

        # 매핑 검증 + 오류 교정 (LLM 2차 호출 — mini 모델)
        mappings = await self._stage1_validate(clusters, pi, mappings)

        derived: list[DerivedQuestion] = []
        qid = 1
        for m in mappings:
            questions_to_add = [m.get("derived_question", "")]
            questions_to_add += m.get("_extra_questions", [])
            for q_text in questions_to_add:
                if not q_text:
                    continue
                derived.append(
                    DerivedQuestion(
                        question_id=f"q{qid:03d}",
                        question=q_text,
                        category=m["category"],
                        source_cluster_id=m["cluster_id"],
                        mapping_rationale=m.get("mapping_reason", ""),
                    )
                )
                qid += 1

        logger.info(
            "Stage 1 완료: 파생 질문 %d개 (unassigned: %d개)",
            len(derived),
            sum(1 for d in derived if d.category == "unassigned"),
        )
        return derived

    async def _stage1_validate(
        self,
        clusters: list,
        pi: PlannerInput,
        mappings: list[dict],
    ) -> list[dict]:
        """매핑 결과 검증 및 오류 교정 (LLM 1회, mini 모델).

        기준 1: derived_question 도메인 ↔ category 도메인 일치
        기준 2: derived_question이 category 독자 의도와 연결
        기준 3: 같은 카테고리 내 질문 각도 중복 없음

        verdict: ok      → 변경 없음
        verdict: reassign → 카테고리 + 질문 교정
        verdict: rewrite  → 질문 각도만 교정

        JSON 파싱 실패 또는 예외 발생 시 원본 mappings를 그대로 반환한다.
        """
        system = self._load_guide("prompts/stage1_validate.md")

        validate_payload = {
            "categories": pi.questions,
            "clusters": [
                {
                    "cluster_id": c.cluster_id,
                    "representative_keyword": c.representative_keyword,
                    "paa_questions": c.paa_questions[:5],
                    "naver_top_titles": [m.title for m in c.naver_content_meta[:3]],
                }
                for c in clusters
            ],
            "mappings": [
                {
                    "cluster_id": m["cluster_id"],
                    "category": m["category"],
                    "derived_question": m["derived_question"],
                }
                for m in mappings
            ],
        }

        try:
            raw = await self._llm_call(
                label="stage1_validate",
                system=system,
                user=json.dumps(validate_payload, ensure_ascii=False, indent=2),
                max_tokens=4096,
                model=self._model_mini,
            )
            parsed = json.loads(_strip_json_fence(raw))
        except Exception as exc:
            logger.warning("Stage 1 검증: 실패 — 원본 매핑 유지: %s", exc)
            return mappings

        validations = {v["cluster_id"]: v for v in parsed.get("validations", [])}

        corrections = 0
        for m in mappings:
            cid = m["cluster_id"]
            v = validations.get(cid)
            if not v or v.get("verdict") == "ok":
                continue

            verdict = v.get("verdict")
            issue = v.get("issue", "")

            if verdict == "reassign":
                old_cat = m["category"]
                m["category"] = v.get("corrected_category", m["category"])
                m["derived_question"] = v.get("corrected_question", m["derived_question"])
                m["mapping_reason"] = (
                    m.get("mapping_reason", "") + f" [검증 교정 reassign: {issue}]"
                )
                logger.warning(
                    "Stage 1 검증: reassign [%s] '%s' → '%s' | %s",
                    cid, old_cat[:35], m["category"][:35], issue,
                )
                corrections += 1

            elif verdict == "rewrite":
                old_q = m["derived_question"]
                m["derived_question"] = v.get("corrected_question", m["derived_question"])
                m["mapping_reason"] = (
                    m.get("mapping_reason", "") + f" [검증 교정 rewrite: {issue}]"
                )
                logger.warning(
                    "Stage 1 검증: rewrite [%s] '%s' → '%s' | %s",
                    cid, old_q[:40], m["derived_question"][:40], issue,
                )
                corrections += 1

        logger.info("Stage 1 검증 완료: %d건 교정", corrections)
        return mappings

    async def _stage2_funnel(
        self,
        derived: list[DerivedQuestion],
        pi: PlannerInput,
    ) -> list[DerivedQuestion]:
        """각 파생 질문에 퍼널(awareness/consideration/conversion/unclassified)을 배정한다.

        - unassigned 카테고리 질문은 건너뛰고 funnel="unclassified"로 유지한다.
        - 1순위 신호: 질문 텍스트 패턴
        - 2순위 신호: naver_knowledge_snippet, google_paa_count, naver_top_titles
        - question_id를 기본 키로 사용 (source_cluster_id는 SERP 조회용 참조만)
        """
        system = self._load_guide("prompts/stage2_funnel.md")

        # question_id로 태깅할 질문 목록 (unassigned 제외)
        taggable = [dq for dq in derived if dq.category != "unassigned"]
        if not taggable:
            logger.warning("Stage 2: 태깅할 질문 없음 — funnel=unclassified 유지")
            return derived

        # source_cluster_id → Cluster 빠른 조회 맵
        cluster_map = {c.cluster_id: c for c in pi.research_result.clusters}

        # LLM 입력 페이로드 구성
        question_items = []
        for dq in taggable:
            cluster = cluster_map.get(dq.source_cluster_id)
            serp: dict = {
                "naver_knowledge_snippet": False,
                "google_paa_count": 0,
                "naver_top_titles": [],
            }
            if cluster:
                serp["naver_knowledge_snippet"] = bool(
                    getattr(cluster, "naver_serp_features", None)
                    and cluster.naver_serp_features.knowledge_snippet
                )
                serp["google_paa_count"] = len(
                    getattr(cluster, "paa_questions", [])
                )
                serp["naver_top_titles"] = [
                    m.title for m in cluster.naver_content_meta[:5]
                ]
            question_items.append({
                "question_id": dq.question_id,
                "question": dq.question,
                "category": dq.category,
                "serp_signals": serp,
            })

        user_content = json.dumps(
            {"questions": question_items},
            ensure_ascii=False,
            indent=2,
        )

        raw = await self._llm_call(
            label="stage2_funnel",
            system=system,
            user=user_content,
            max_tokens=8192,
        )

        # JSON 파싱
        try:
            parsed = json.loads(_strip_json_fence(raw))
        except json.JSONDecodeError as exc:
            logger.error("Stage 2: JSON 파싱 실패: %s\n원문(앞 300자): %s", exc, raw[:300])
            raise

        # question_id 기준으로 결과 적용
        tags_by_qid: dict[str, dict] = {
            t["question_id"]: t for t in parsed.get("funnel_tags", [])
        }

        applied = 0
        for dq in derived:
            tag = tags_by_qid.get(dq.question_id)
            if not tag:
                if dq.category != "unassigned":
                    logger.warning("Stage 2: 태그 누락 [%s] — unclassified 유지", dq.question_id)
                continue
            dq.funnel = tag.get("funnel", "unclassified")
            rationale = tag.get("funnel_rationale", {})
            if isinstance(rationale, dict):
                dq.funnel_journey_reasoning = rationale.get("journey_reasoning", "")
                dq.funnel_searcher_state = rationale.get("searcher_state", "")
                dq.funnel_judgment_basis = rationale.get("judgment_basis", "")
                dq.funnel_signals_used = rationale.get("signals_used", "없음")
            applied += 1

        logger.info(
            "Stage 2 완료: %d개 태깅 (awareness=%d, consideration=%d, conversion=%d, unclassified=%d)",
            applied,
            sum(1 for d in derived if d.funnel == "awareness"),
            sum(1 for d in derived if d.funnel == "consideration"),
            sum(1 for d in derived if d.funnel == "conversion"),
            sum(1 for d in derived if d.funnel == "unclassified"),
        )
        return derived

    async def _stage3_duplicate(
        self,
        derived: list[DerivedQuestion],
        pi: PlannerInput,
    ) -> tuple[list[DerivedQuestion], list[UpdateCandidate]]:
        """각 질문과 pi.published_contents를 대조해 중복 위험도(0~1)를 산출한다.

        중복 위험도 = (키워드겹침×0.4) + (제목유사도×0.3) + (소재중복×0.3)
        0.7↑ → update_existing (UpdateCandidate 생성)
        0.4~0.7 → angle_shift 가능 여부 LLM 판단 → 불가 시 update_existing
        0.4미만 → new (통과)
        가이드: duplicate_check.md
        반환: (판정 완료 derived, UpdateCandidate 목록)
        """
        guide = self._load_guide("prompts/stage3_duplicate.md")
        cfg = self._cfg

        # published_contents 없으면 전체 new 통과 (LLM 호출 없음)
        if not pi.published_contents:
            logger.info("Stage 3: published_contents 없음 — 전체 new 통과")
            for dq in derived:
                dq.duplicate_result = DuplicateResult(verdict="new")
            return derived, []

        # 클러스터 맵 구성 (source_cluster_id → Cluster)
        cluster_map: dict[str, Cluster] = {
            c.cluster_id: c for c in pi.research_result.clusters
        }

        # 병렬 실행
        tasks = [
            self._check_one_duplicate(dq, pi, cluster_map, guide, self._cfg)
            for dq in derived
        ]
        results: list[tuple[DerivedQuestion, UpdateCandidate | None]] = (
            await asyncio.gather(*tasks)
        )

        updated_derived: list[DerivedQuestion] = []
        candidates: list[UpdateCandidate] = []
        for dq, cand in results:
            updated_derived.append(dq)
            if cand is not None:
                candidates.append(cand)

        verdict_counts: dict[str, int] = {}
        for dq in updated_derived:
            v = dq.duplicate_result.verdict if dq.duplicate_result else "none"
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
        logger.info(
            "Stage 3 완료: %d개 판정 (%s), 업데이트 후보 %d개",
            len(updated_derived),
            ", ".join(f"{k}={v}" for k, v in sorted(verdict_counts.items())),
            len(candidates),
        )
        return updated_derived, candidates

    async def _check_one_duplicate(
        self,
        dq: DerivedQuestion,
        pi: PlannerInput,
        cluster_map: dict[str, Cluster],
        guide: str,
        cfg: dict,
    ) -> tuple[DerivedQuestion, UpdateCandidate | None]:
        """질문 1개에 대한 중복 판정 코루틴. _stage3_duplicate 내부 병렬 실행용."""
        # ── 키워드 겹침 계산 (룰) ──────────────────────────────────
        cluster = cluster_map.get(dq.source_cluster_id)
        cluster_kws = [kw.keyword for kw in cluster.keywords] if cluster else []
        cluster_h2 = cluster.h2_topics if cluster else []

        overlaps = [
            (_compute_keyword_overlap(dq.question, cluster_kws, pub), pub)
            for pub in pi.published_contents
        ]
        best_overlap, best_pub = max(overlaps, key=lambda x: x[0])

        # 겹침이 거의 없으면 LLM 없이 new 처리
        if best_overlap < 0.1:
            dq.duplicate_result = DuplicateResult(
                keyword_overlap=round(best_overlap, 3),
                risk_score=0.0,
                verdict="new",
                rationale="기발행 콘텐츠와 키워드 겹침 없음.",
            )
            return dq, None

        # ── LLM 호출: 제목 유사도 + 소재 중복 ────────────────────
        user_ctx = json.dumps(
            {
                "question": dq.question,
                "question_funnel": dq.funnel,
                "cluster_h2_topics": cluster_h2,
                "matched_content": {
                    "url": best_pub.url,
                    "title": best_pub.title,
                    "main_keyword": best_pub.main_keyword,
                    "publish_date": best_pub.publish_date or "",
                    "funnel": best_pub.funnel,
                    "h2_sections": best_pub.h2_sections,
                    "excerpt": best_pub.excerpt,
                    "word_count": best_pub.word_count,
                    "content_type": best_pub.content_type,
                },
            },
            ensure_ascii=False,
        )
        try:
            raw = await self._llm_call(
                "stage3_duplicate",
                guide,
                user_ctx,
                model=cfg["models"]["mini"],
                max_tokens=512,
            )
            data = json.loads(_strip_json_fence(raw))
        except Exception as exc:
            logger.warning(
                "[stage3_duplicate] %s LLM 실패 — new 처리: %s", dq.question_id, exc
            )
            dq.duplicate_result = DuplicateResult(
                keyword_overlap=round(best_overlap, 3),
                verdict="new",
                rationale="LLM 호출 실패 — 안전 방향으로 new 처리.",
            )
            return dq, None

        title_sim = min(1.0, max(0.0, float(data.get("title_similarity", 0.0))))
        topic_ov = min(1.0, max(0.0, float(data.get("topic_overlap", 0.0))))
        angle_ok = bool(data.get("angle_shift_possible", False))
        rationale = str(data.get("rationale", ""))
        angle_reason = str(data.get("angle_shift_reason", ""))

        # ── 가중 합산 ─────────────────────────────────────────────
        w = cfg["duplicate"]["weights"]
        risk_score = round(
            min(
                1.0,
                best_overlap * w["keyword_overlap"]
                + title_sim * w["title_similarity"]
                + topic_ov * w["topic_overlap"],
            ),
            3,
        )

        # ── verdict 판정 ──────────────────────────────────────────
        high = cfg["duplicate"]["high_risk_threshold"]
        mid = cfg["duplicate"]["angle_shift_threshold"]

        if risk_score >= high:
            verdict = "update_existing"
        elif risk_score >= mid:
            verdict = "angle_shift" if angle_ok else "update_existing"
        else:
            verdict = "new"

        dq.duplicate_result = DuplicateResult(
            keyword_overlap=round(best_overlap, 3),
            title_similarity=round(title_sim, 3),
            topic_overlap=round(topic_ov, 3),
            risk_score=risk_score,
            verdict=verdict,
            matched_content_url=best_pub.url,
            matched_content_title=best_pub.title,
            rationale=rationale,
        )

        # ── UpdateCandidate 생성 ──────────────────────────────────
        candidate: UpdateCandidate | None = None
        if verdict == "update_existing":
            candidate = UpdateCandidate(
                published_url=best_pub.url,
                published_title=best_pub.title,
                risk_score=risk_score,
                improvement_points=[p for p in [angle_reason] if p],
                urgency=_compute_urgency(best_pub),  # type: ignore[arg-type]
            )

        return dq, candidate

    async def _stage4_priority(
        self,
        derived: list[DerivedQuestion],
        pi: PlannerInput,
    ) -> tuple[list[DerivedQuestion], list[DerivedQuestion]]:
        """우선순위 점수 계산 → 카테고리별 발행 예산 배분 → 선발/대기 배정 → LLM 근거 생성.

        반환: (전체 derived, 선발된 것만)
        """
        TREND_WEIGHTS = {"rising": 1.0, "stable": 0.5, "declining": 0.0}

        # ── Step 1: 클러스터 맵 빌드 + 점수 계산 (룰 기반) ───────────────
        cluster_map = {c.cluster_id: c for c in pi.research_result.clusters}

        # log 정규화 기준값 계산
        vols = [
            cluster_map[dq.source_cluster_id].total_volume_naver
            for dq in derived
            if dq.source_cluster_id in cluster_map
        ]
        max_vol = max(vols) if vols else 1
        log_max = math.log10(max_vol + 1) or 1.0

        for dq in derived:
            cluster = cluster_map.get(dq.source_cluster_id)
            vol = cluster.total_volume_naver if cluster else 0
            trend = cluster.volume_trend if cluster else "stable"
            log_score = math.log10(vol + 1) / log_max
            trend_weight = TREND_WEIGHTS.get(trend, 0.5)
            dq.priority_score = round(0.7 * log_score + 0.3 * trend_weight, 4)

        # ── Step 2: 카테고리별 발행 예산 배분 ────────────────────────────
        monthly_count = self._compute_monthly_count(pi.target_month)
        categories = pi.questions
        n_cats = len(categories) or 1
        base_alloc = monthly_count // n_cats
        remainder = monthly_count % n_cats

        alloc: dict[str, int] = {cat: base_alloc for cat in categories}
        if remainder > 0:
            # 파생 질문이 많은 유효 카테고리에 나머지 배정
            cat_counts = Counter(
                dq.category for dq in derived if dq.category in alloc
            )
            sorted_cats = sorted(alloc, key=lambda c: cat_counts.get(c, 0), reverse=True)
            for i in range(remainder):
                alloc[sorted_cats[i % len(sorted_cats)]] += 1

        # ── Step 3: 선발 / 대기 배정 ─────────────────────────────────────
        cat_groups: dict[str, list[DerivedQuestion]] = defaultdict(list)
        for dq in derived:
            if dq.category in alloc:  # unassigned 제외
                cat_groups[dq.category].append(dq)

        for cat, questions in cat_groups.items():
            # update_existing 제외
            eligible = [
                dq for dq in questions
                if not (
                    dq.duplicate_result
                    and dq.duplicate_result.verdict == "update_existing"
                )
            ]
            # priority_score DESC 정렬
            eligible.sort(key=lambda q: q.priority_score, reverse=True)

            n_select = alloc.get(cat, base_alloc)
            n_waitlist = min(3, max(0, len(eligible) - n_select))

            for i, dq in enumerate(eligible):
                if i < n_select:
                    dq.is_selected = True
                elif i < n_select + n_waitlist:
                    dq.is_waitlist = True

        # ── Step 3b: 쿼터 재배분 — monthly_count 보장 ─────────────────────
        shortfall = monthly_count - sum(1 for dq in derived if dq.is_selected)
        if shortfall > 0:
            fill_candidates = [
                dq for dq in derived
                if not dq.is_selected
                and dq.category in alloc
                and not (
                    dq.duplicate_result
                    and dq.duplicate_result.verdict == "update_existing"
                )
            ]
            fill_candidates.sort(key=lambda q: q.priority_score, reverse=True)
            for dq in fill_candidates[:shortfall]:
                dq.is_selected = True
                dq.is_waitlist = False
            promoted = min(shortfall, len(fill_candidates))
            remaining = shortfall - promoted
            if remaining > 0:
                logger.warning(
                    "Stage 4: 월간 목표 %d개 미달 — eligible 부족으로 %d개만 선발 가능",
                    monthly_count,
                    monthly_count - remaining,
                )
            else:
                logger.info("Stage 4: 쿼터 재배분으로 %d개 추가 선발", promoted)

        selected = [dq for dq in derived if dq.is_selected]

        # ── Step 4: LLM 선정 근거 생성 (선발 질문 배치 1회 호출) ──────────
        if selected:
            system = self._load_guide("prompts/stage4_rationale.md")

            lines: list[str] = [
                f"콘텐츠 방향성: {', '.join(pi.content_direction)}",
                f"질문 의도: {', '.join(pi.intent)}",
                "",
                "선발된 질문 목록:",
            ]
            for dq in selected:
                cluster = cluster_map.get(dq.source_cluster_id)
                vol = cluster.total_volume_naver if cluster else 0
                trend = cluster.volume_trend if cluster else "stable"
                dup = dq.duplicate_result.verdict if dq.duplicate_result else "new"
                lines.append(
                    f"\n[{dq.question_id}]\n"
                    f"질문: {dq.question}\n"
                    f"카테고리: {dq.category}\n"
                    f"퍼널: {dq.funnel}\n"
                    f"월 검색량(네이버): {vol:,}\n"
                    f"검색 트렌드: {trend}\n"
                    f"중복 판정: {dup}"
                )
            user_content = "\n".join(lines)

            try:
                raw = await self._llm_call(
                    label="stage4_rationale",
                    system=system,
                    user=user_content,
                    max_tokens=4096,
                )
                data = json.loads(_strip_json_fence(raw))
                rationales_by_qid = {
                    r["question_id"]: r for r in data.get("rationales", [])
                }
                for dq in selected:
                    r = rationales_by_qid.get(dq.question_id, {})
                    data_rat = r.get("data_rationale", "")
                    content_rat = r.get("content_rationale", "")
                    if data_rat or content_rat:
                        dq.selection_rationale = (
                            f"[데이터] {data_rat}  [방향] {content_rat}"
                        )
            except Exception as exc:
                logger.warning("Stage 4: LLM 근거 생성 실패 — fallback 빈 문자열: %s", exc)
                # selection_rationale은 이미 "" 기본값이므로 추가 조치 불필요

        logger.info(
            "Stage 4 완료: 총 %d개 중 선발 %d개, 대기 %d개",
            len(derived),
            len(selected),
            sum(1 for dq in derived if dq.is_waitlist),
        )
        return derived, selected

    def _stage5_balance(
        self,
        selected: list[DerivedQuestion],
        pi: PlannerInput,
    ) -> tuple[list[DerivedQuestion], FunnelDistribution, FunnelDistribution | None]:
        """선발 콘텐츠의 퍼널 분포 계산 + 편중 경고 (자동 교체 없음).

        swap_enabled=false → 경고 로그만 남기고 selected 목록은 변경하지 않는다.
        아카이브에서 직전 월 퍼널 분포를 로드해 로그로 비교한다.
        반환: (변경 없는 selected, 현재 퍼널 분포, 직전 월 퍼널 분포 또는 None)
        """
        counts = Counter(dq.funnel for dq in selected)
        total = len(selected) or 1
        dist = FunnelDistribution(
            awareness=counts.get("awareness", 0),
            consideration=counts.get("consideration", 0),
            conversion=counts.get("conversion", 0),
            unclassified=counts.get("unclassified", 0),
        )

        threshold: float = self._cfg["funnel"]["dominance_threshold"]
        for funnel, count in counts.items():
            if count / total >= threshold:
                logger.warning(
                    "Stage 5: 퍼널 편중 감지 — %s %.0f%% (임계값 %.0f%%)",
                    funnel,
                    count / total * 100,
                    threshold * 100,
                )

        prev_dist = archive_mod.load_previous_funnel(pi.target_month, self._archive_runs_dir)
        if prev_dist:
            logger.info(
                "Stage 5: 전월 분포 — awareness=%d, consideration=%d, conversion=%d",
                prev_dist.awareness,
                prev_dist.consideration,
                prev_dist.conversion,
            )

        logger.info(
            "Stage 5 완료: awareness=%d, consideration=%d, conversion=%d, unclassified=%d",
            dist.awareness,
            dist.consideration,
            dist.conversion,
            dist.unclassified,
        )

        # 발행일 배정: 월·수·금 날짜 목록 생성 → 퍼널 교대 정렬 → zip
        year, month = map(int, pi.target_month.split("-"))
        publish_dates: list[date] = []
        for day in range(1, 32):
            try:
                d = date(year, month, day)
            except ValueError:
                break
            if d.weekday() in PUBLISH_WEEKDAYS:
                publish_dates.append(d)

        groups: dict[str, list[DerivedQuestion]] = defaultdict(list)
        for dq in sorted(selected, key=lambda x: x.priority_score, reverse=True):
            groups[dq.funnel].append(dq)

        ordered: list[DerivedQuestion] = []
        last_funnel: str | None = None
        while any(lst for lst in groups.values()):
            candidates_f = [f for f, lst in groups.items() if lst and f != last_funnel]
            if not candidates_f:
                candidates_f = [f for f, lst in groups.items() if lst]
            chosen = candidates_f[0]
            item = groups[chosen].pop(0)
            ordered.append(item)
            last_funnel = chosen

        for dq, pub_date in zip(ordered, publish_dates):
            dq.publish_date = pub_date.isoformat()

        logger.info("Stage 5: 발행일 배정 완료 — %d건", len(ordered))
        return selected, dist, prev_dist

    async def _stage6_structure(
        self,
        selected: list[DerivedQuestion],
        pi: PlannerInput,
    ) -> list[ContentPiece]:
        """GEO 유형 배정 + H2 구조 설계 (LLM 1회).

        - 시스템 프롬프트: stage6_structure.md + geo_classification.md + content_direction.md
        - 페이로드: cluster.keywords (h2_topics 불사용) + 경쟁사 제목
        - LLM이 geo_type 직접 결정 (규칙 함수 없음)
        """
        if not selected:
            logger.warning("Stage 6: 선발 질문 없음 — 빈 목록 반환")
            return []

        cluster_map = {c.cluster_id: c for c in pi.research_result.clusters}

        # 시스템 프롬프트: 3개 가이드 동적 합산
        system = (
            self._load_guide("prompts/stage6_structure.md")
            + "\n\n---\n\n"
            + self._load_guide("geo_classification.md")
            + "\n\n---\n\n"
            + self._load_guide("content_direction.md")
        )

        # 네이버 볼륨 상대 티어 산출 (선발 질문 전체 대비)
        _raw_vols = [
            (cluster_map[dq.source_cluster_id].total_volume_naver
             if dq.source_cluster_id in cluster_map else 0)
            for dq in selected
        ]
        _sorted_vols = sorted(_raw_vols)

        def _naver_tier(vol: int) -> str:
            rank = sum(1 for v in _sorted_vols if v <= vol)
            pct = rank / max(len(_sorted_vols), 1)
            if pct >= 0.67:
                return "높음"
            elif pct >= 0.34:
                return "중간"
            return "낮음"

        # 페이로드: h2_topics 대신 cluster.keywords 사용
        payload: list[dict] = []
        for dq in selected:
            cluster = cluster_map.get(dq.source_cluster_id)
            cluster_keywords = [kw.keyword for kw in cluster.keywords] if cluster else []
            competitor_titles: list[str] = []
            google_paa_count = 0
            google_featured_snippet = False
            google_ai_overview = False
            if cluster:
                competitor_titles = [m.title for m in cluster.google_content_meta[:3]]
                competitor_titles += [m.title for m in cluster.naver_content_meta[:5]]
                gsf = cluster.google_serp_features
                google_paa_count = len(gsf.paa_questions)
                google_featured_snippet = gsf.featured_snippet_exists
                google_ai_overview = gsf.ai_overview
            payload.append({
                "question_id": dq.question_id,
                "question": dq.question,
                "category": dq.category,
                "funnel": dq.funnel,
                "mapping_rationale": dq.mapping_rationale,
                "funnel_journey_reasoning": dq.funnel_journey_reasoning,
                "cluster_keywords": cluster_keywords,
                "competitor_titles": competitor_titles,
                # 수요 신호
                "naver_volume_tier": _naver_tier(
                    cluster.total_volume_naver if cluster else 0
                ),  # 선발 질문 내 상대 수요 (높음/중간/낮음)
                "google_paa_count": google_paa_count,
                "google_featured_snippet": google_featured_snippet,
                "google_ai_overview": google_ai_overview,
            })

        resp = await self._llm_call(
            label="stage6_structure",
            system=system,
            user=json.dumps({"questions": payload}, ensure_ascii=False),
            max_tokens=16384,
        )

        try:
            parsed = json.loads(_strip_json_fence(resp))
        except json.JSONDecodeError as exc:
            logger.error("Stage 6: JSON 파싱 실패: %s\n원문(앞 300자): %s", exc, resp[:300])
            raise

        struct_map = {s["question_id"]: s for s in parsed.get("structures", [])}

        pieces: list[ContentPiece] = []
        for dq in selected:
            s = struct_map.get(dq.question_id, {})
            cluster = cluster_map.get(dq.source_cluster_id)
            pieces.append(ContentPiece(
                content_id=dq.question_id,
                question=dq.question,
                category=dq.category,
                funnel=dq.funnel,
                geo_type=s.get("geo_type", "definition"),
                publishing_purpose=s.get("publishing_purpose", ""),
                title_suggestions=[TitleSuggestion(**t) for t in s.get("title_suggestions", [])],
                h2_structure=[H2Section(**h) for h in s.get("h2_structure", [])],
                cta_suggestion=s.get("cta_suggestion", ""),
                priority_score=dq.priority_score,
                publish_date=dq.publish_date,
                data_rationale=dq.selection_rationale,
                mapping_rationale=dq.mapping_rationale,
                funnel_journey_reasoning=dq.funnel_journey_reasoning,
                source_cluster_id=dq.source_cluster_id,
                representative_keyword=cluster.representative_keyword if cluster else "",
                monthly_volume_naver=cluster.total_volume_naver if cluster else 0,
                volume_trend=cluster.volume_trend if cluster else "stable",
            ))

        logger.info("Stage 6 완료: ContentPiece %d개", len(pieces))
        return pieces

    def _selected_to_pieces(self, selected: list[DerivedQuestion]) -> list[ContentPiece]:
        """Stage 6 미구현 시 임시 브리지: DerivedQuestion → 최소 ContentPiece 변환.

        content_id = question_id, geo_type = "definition" (플레이스홀더).
        Stage 6 구현 후에는 이 메서드를 사용하지 않는다.
        """
        return [
            ContentPiece(
                content_id=dq.question_id,
                question=dq.question,
                category=dq.category,
                funnel=dq.funnel,
                geo_type="definition",
                publishing_purpose="",
                priority_score=dq.priority_score,
                source_cluster_id=dq.source_cluster_id,
            )
            for dq in selected
        ]

    def _sort_pieces_for_calendar(self, pieces: list[ContentPiece]) -> list[ContentPiece]:
        """greedy 퍼널 교대 정렬: 같은 퍼널이 연속되지 않도록 배열한다.

        pieces는 이미 priority_score 내림차순으로 정렬된 상태로 전달된다.
        """
        groups: dict[str, list[ContentPiece]] = defaultdict(list)
        for p in pieces:
            groups[p.funnel].append(p)

        result: list[ContentPiece] = []
        last_funnel: str | None = None
        while any(lst for lst in groups.values()):
            candidates = [f for f, lst in groups.items() if lst and f != last_funnel]
            if not candidates:
                candidates = [f for f, lst in groups.items() if lst]
            chosen = candidates[0]
            result.append(groups[chosen].pop(0))
            last_funnel = chosen
        return result

    async def _stage7_document(
        self,
        pieces: list[ContentPiece],
        dist: FunnelDistribution,
        pi: PlannerInput,
        prev_dist: FunnelDistribution | None = None,
    ) -> str:
        """ContentPiece → Markdown 월간 기획 문서 생성 (LLM 1회 호출)."""
        year, month_num = map(int, pi.target_month.split("-"))

        trend_ko_map = {"rising": "상승", "stable": "안정", "declining": "하락"}

        sorted_pieces = sorted(pieces, key=lambda x: x.publish_date or "")

        # ── 페이로드 조립 ─────────────────────────────────────────
        payload = {
            "meta": {
                "year": year,
                "month_ko": f"{month_num}월",
                "client_name": pi.client_name,
                "target_month": pi.target_month,
                "intent": pi.intent,
                "content_direction": pi.content_direction,
                "funnel_distribution": dist.model_dump(exclude={"total"}),
                "previous_month_funnel": (
                    prev_dist.model_dump(exclude={"total"}) if prev_dist else None
                ),
            },
            "content_pieces": [
                {
                    "content_id": p.content_id,
                    "publish_date": p.publish_date or "미정",
                    "category": p.category,
                    "question": p.question,
                    "funnel": p.funnel,
                    "geo_type": p.geo_type,
                    "publishing_purpose": p.publishing_purpose,
                    "title_suggestions": [
                        {"title": t.title, "strategy": t.strategy}
                        for t in p.title_suggestions
                    ],
                    "h2_structure": [
                        {"heading": h.heading, "description": h.description}
                        for h in p.h2_structure
                    ],
                    "cta_suggestion": p.cta_suggestion,
                    "monthly_volume_naver": p.monthly_volume_naver,
                    "volume_trend": trend_ko_map.get(p.volume_trend, p.volume_trend),
                    "data_rationale": p.data_rationale,
                    "content_rationale": p.content_rationale,
                }
                for p in sorted_pieces
            ],
        }

        system = self._load_guide("prompts/stage7_document.md")
        resp = await self._llm_call(
            label="stage7_document",
            system=system,
            user=json.dumps(payload, ensure_ascii=False, indent=2),
            max_tokens=16384,
        )

        logger.info("Stage 7 완료: 기획 문서 + 발행 일정 %d개", len(pieces))
        return resp.strip()

    def _stage7_calendar(
        self,
        pieces: list[ContentPiece],
        pi: PlannerInput,
    ) -> list[CalendarEntry]:
        """publish_date 기반 CalendarEntry 조립 (날짜 재계산 없음)."""
        entries: list[CalendarEntry] = []
        for p in sorted(pieces, key=lambda x: x.publish_date or ""):
            if not p.publish_date:
                continue
            d = date.fromisoformat(p.publish_date)
            entries.append(
                CalendarEntry(
                    date=p.publish_date,
                    day_of_week=PUBLISH_WEEKDAYS[d.weekday()],
                    content_id=p.content_id,
                    is_holiday=False,
                )
            )
        return entries

    def _assemble(
        self,
        all_derived: list[DerivedQuestion],
        selected: list[DerivedQuestion],
        pieces: list[ContentPiece],
        calendar: list[CalendarEntry],
        dist: FunnelDistribution,
        prev_dist: FunnelDistribution | None,
        candidates: list[UpdateCandidate],
        pi: PlannerInput,
        run_date: str,
        planning_document: str = "",
    ) -> ContentPlan:
        """모든 단계 산출물을 ContentPlan으로 조립한다."""
        waitlist = [dq for dq in all_derived if dq.is_waitlist]
        plan = ContentPlan(
            run_date=run_date,
            target_month=pi.target_month,
            client_name=pi.client_name,
            intent=pi.intent,
            content_direction=pi.content_direction,
            categories=pi.questions,
            content_pieces=pieces,
            waitlist=waitlist,
            funnel_distribution=dist,
            previous_month_funnel=prev_dist,
            calendar=calendar,
            planning_document=planning_document,
            update_candidates=candidates,
            all_derived_questions=all_derived,
        )
        logger.info(
            "조립 완료: content_pieces=%d, calendar=%d, update_candidates=%d",
            len(pieces),
            len(calendar),
            len(candidates),
        )
        return plan
