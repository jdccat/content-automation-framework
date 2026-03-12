# 플래너 에이전트 — 구현 참조 문서

> **기준 파일**: `core/agents/planner/spec.md`
> **최종 갱신**: 2026-02-27
> **구축 방식**: Phase별 점진 구축. 한 Phase 완료 후 실행 확인 → 다음 Phase 진행.

---

## 목차

1. [개요](#1-개요)
2. [점진 구축 로드맵](#2-점진-구축-로드맵)
3. [입출력 스키마](#3-입출력-스키마)
4. [파이프라인 흐름](#4-파이프라인-흐름)
5. [Phase 0 — 기반 구조](#5-phase-0--기반-구조)
6. [Phase 1 — Stage 1: 카테고리 매핑](#6-phase-1--stage-1-카테고리-매핑)
7. [Phase 2 — Stage 2: 퍼널 태깅](#7-phase-2--stage-2-퍼널-태깅)
8. [Phase 3 — Stage 3: 중복 판정](#8-phase-3--stage-3-중복-판정)
9. [Phase 4 — Stage 4: 우선순위 배정 + 선정 근거](#9-phase-4--stage-4-우선순위-배정--선정-근거)
10. [Phase 5 — Stage 5 + 7: 퍼널 균형 + 캘린더 (룰 기반 묶음)](#10-phase-5--stage-5--7-퍼널-균형--캘린더-룰-기반-묶음)
11. [Phase 6 — Stage 6: 콘텐츠 세부 구조 설계](#11-phase-6--stage-6-콘텐츠-세부-구조-설계)
12. [Phase 7 — 조립 + 출력](#12-phase-7--조립--출력)
13. [LLM 호출 전체 목록](#13-llm-호출-전체-목록)
14. [가이드 파일 로드 규칙](#14-가이드-파일-로드-규칙)
15. [에러 처리 패턴](#15-에러-처리-패턴)
16. [스냅샷 및 아카이브](#16-스냅샷-및-아카이브)
17. [설정 파일](#17-설정-파일)
18. [실행 방법](#18-실행-방법)

---

## 1. 개요

### 에이전트 역할

리서처가 수집한 팩트 데이터(`ResearchResult`)와 사용자 입력을 받아 **월간 콘텐츠 기획 문서(`ContentPlan`)를 생성**한다.
검색 데이터 수집은 하지 않는다. 모든 판단(퍼널 분류, 우선순위, 구조 설계)은 플래너가 수행한다.

### 핵심 원칙 (spec.md)

- 사용자가 입력한 질문이 가장 중요한 기준. 검색량·트렌드보다 우선.
- 모든 선정에는 두 가지 근거가 필수: **리서처 수집 데이터 기반 + 기존 블로그 기반**.
- 가이드 파일은 단계 진입 시 해당 파일만 로드. 전체를 한꺼번에 읽지 않음.
- 7단계를 **1개 에이전트가 순차 실행**.

### LLM 호출 수

| 단계 | 모델 | 호출 방식 | 용도 |
|------|------|-----------|------|
| Stage 1 | gpt-4.1 | 1회 (일괄) | 카테고리 매핑 |
| Stage 2 | gpt-4.1 | 1회 (일괄) | SERP 기반 퍼널 태깅 |
| Stage 3 | gpt-4.1-mini | 질문당 1회 (병렬) | 소재 중복 H2 목차 비교 |
| Stage 4 | gpt-4.1 | 1회 (일괄) | 선정 근거 문장 생성 |
| Stage 6 | gpt-4.1 | 1회 (일괄) | H2 구조 + 제목안 + CTA 설계 |
| **합계** | | **5 + N회** | N = 3단계 병렬 호출 수 |

Stage 5, Stage 7은 LLM 없음 (룰 기반).

---

## 2. 점진 구축 로드맵

```
Phase 0  기반 구조 확정 (config, archive 스캐폴딩, agent 스텁, 실행 스크립트)
   ↓
Phase 1  Stage 1 — 카테고리 매핑
   ↓  결과 확인: 클러스터 → 카테고리 매핑, unassigned 처리
Phase 2  Stage 2 — 퍼널 태깅
   ↓  결과 확인: SERP 기반 인지/고려/전환 분류
Phase 3  Stage 3 — 중복 판정
   ↓  결과 확인: 위험도 점수, 각도 전환 근거
Phase 4  Stage 4 — 우선순위 배정 + 선정 근거
   ↓  결과 확인: 카테고리당 배정 수, 선정 근거 품질
Phase 5  Stage 5 + Stage 7 — 퍼널 균형 + 캘린더 (룰 기반 묶음)
   ↓  결과 확인: 70% 교체 동작, 발행 일정 배치 규칙
Phase 6  Stage 6 — 콘텐츠 세부 구조 설계
   ↓  결과 확인: GEO × 퍼널 조합, H2 품질, CTA 적합성
Phase 7  조립 + 출력 (ContentPlan → 마크다운 + JSON 저장)
```

### 각 Phase 진입 기준

각 Phase는 이전 Phase 결과를 `snapshots/planner/stageN_{date}.json`에서 이어받아 실행할 수 있다.
이전 Phase 결과에 문제가 있으면 해당 Phase를 재실행한다. 앞 단계는 재실행하지 않는다.

---

## 3. 입출력 스키마

### 입력 (`PlannerInput` — Pydantic, `core/schemas.py`)

```python
intent: list[str]                    # ["비교 판단"] — input_template.md 선택지
questions: list[str]                 # 카테고리 원본 질문 (1개 이상)
content_direction: list[str]         # ["판단 기준 제시"] — input_template.md 선택지
research_result: ResearchResult      # 리서처 산출물
published_contents: list[PublishedContent]  # 기발행 콘텐츠 DB
target_month: str                    # "2026-03"
client_name: str                     # "wishket"
```

### 출력 (`ContentPlan` — Pydantic, `core/schemas.py`)

```python
run_date: str
target_month: str
client_name: str
strategy_objective: str              # 이번 달 핵심 목표 (한 줄)
strategy_direction: str              # 전략 방향 요약

categories: list[str]               # 사용자 입력 질문 원본
content_pieces: list[ContentPiece]  # 선발된 콘텐츠 세부 구조 (6단계 산출)
waitlist: list[DerivedQuestion]     # 대기 풀 (카테고리별 분리)

funnel_distribution: FunnelDistribution
previous_month_funnel: FunnelDistribution | None

calendar: list[CalendarEntry]       # 7단계 산출
update_candidates: list[UpdateCandidate]  # 3단계 제외 질문 → 업데이트 후보
all_derived_questions: list[DerivedQuestion]  # 전체 파생 질문 (추적용)
```

### 중간 데이터: `DerivedQuestion` (1~4단계를 거치며 필드가 채워짐)

```python
question: str                        # 파생 질문 텍스트
category: str                        # 소속 카테고리 (사용자 입력 질문 원문)
source_cluster_id: str               # 리서처 클러스터 참조

# 1단계 후
exploration_order: int               # 카테고리 내 탐색 경로 순서 (0-based)

# 2단계 후
funnel: FUNNEL_STAGE                 # awareness / consideration / conversion / unclassified
funnel_rationale: str                # SERP 기반 판단 근거

# 3단계 후
duplicate_result: DuplicateResult | None

# 4단계 후
priority_score: float
is_selected: bool
is_waitlist: bool
selection_rationale: str
```

### 연결된 스키마

- `ContentPiece` — 6단계 설계 결과 (H2, 제목안, CTA 포함)
- `CalendarEntry` — 7단계 발행 일정 항목
- `UpdateCandidate` — 기존 글 업데이트 후보
- `FunnelDistribution` — 5단계 퍼널 분포 현황
- `DuplicateResult` — 3단계 중복 판정 상세

→ 전체 정의: `core/schemas.py` 374번째 줄부터

---

## 4. 파이프라인 흐름

```
PlannerInput
    │
    ├─ [입력 검증] input_template.md 기준 — 실패 시 ValueError
    │
    ├─ [STAGE 1] 카테고리 매핑 + 탐색 경로 정렬
    │   - 가이드 없음 (LLM이 spec 규칙 내재화)
    │   - LLM: 클러스터 → 카테고리 매핑 + 카테고리별 탐색 경로 순서 배정
    │   → DerivedQuestion[] (category, exploration_order 채워짐)
    │
    ├─ [STAGE 2] 퍼널 태깅
    │   - 가이드: funnel_criteria.md 로드
    │   - LLM: SERP 상위 콘텐츠 유형 기반 인지/고려/전환 판단
    │   → DerivedQuestion.funnel + funnel_rationale 채워짐
    │
    ├─ [STAGE 3] 기발행 콘텐츠 중복 판정
    │   - 가이드: duplicate_check.md 로드
    │   - 룰: 키워드 겹침(0.4) + LLM: 제목 유사도(0.3) + 소재 중복(0.3) 가중합
    │   - 병렬 처리 (asyncio.gather)
    │   → DerivedQuestion.duplicate_result 채워짐
    │   → UpdateCandidate[] (0.7↑ 또는 각도 전환 불가)
    │
    ├─ [STAGE 4] 우선순위 배정 + 선정 근거 생성
    │   - 가이드: publishing_schedule.md 로드 (월간 발행 수 확인)
    │   - 룰: 카테고리당 배정 수 계산 → 탐색 경로 순서 + 검색량 + 트렌드 기반 정렬
    │   - LLM: data_rationale + content_rationale 문장 생성
    │   → DerivedQuestion.is_selected / is_waitlist / selection_rationale 채워짐
    │
    ├─ [STAGE 5] 퍼널 균형 검증
    │   - 룰 기반 (LLM 없음)
    │   - 70% 초과 퍼널 있으면 대기 풀에서 부족 퍼널 콘텐츠로 교체
    │   - 아카이브에서 직전 월 퍼널 분포 로드
    │   → FunnelDistribution + previous_month_funnel
    │
    ├─ [STAGE 6] 콘텐츠 세부 구조 설계
    │   - 가이드: content_direction.md + geo_classification.md 로드
    │   - LLM: GEO 유형 배정 → H2 3~5개 + 제목안 2개(SEO형/CTR형) + CTA 제안
    │   → ContentPiece[] 완성
    │
    ├─ [STAGE 7] 월간 발행 일정 배치
    │   - 가이드: publishing_schedule.md 로드 (요일, 배치 규칙 확인)
    │   - 룰 기반 (LLM 없음)
    │   - 규칙 1 퍼널 교차 → 규칙 2 카테고리 순환 → 규칙 3 탐색 경로 순서
    │   → CalendarEntry[]
    │
    └─ [어셈블러] ContentPlan 조립
        → [아카이브 저장] archive/planner/runs/YYYY-MM.json
        → [스냅샷 저장] snapshots/planner/final_{date}.json
```

---

## 5. Phase 0 — 기반 구조

### 만드는 파일

| 파일 | 설명 |
|------|------|
| `core/agents/planner/config.yaml` | 모델, 경로, 품질 게이트 설정 |
| `core/agents/planner/archive.py` | 아카이브 save/load (구조 확정, 로직은 Phase 5에서 완성) |
| `core/agents/planner/agent.py` | `PlannerAgent` 클래스 — 7개 stage 메서드 stub |
| `core/agents/planner/prompts/planning.md` | 시스템 프롬프트 (spec 기반으로 교체) |
| `run_planner.py` | 루트 실행 스크립트 (`--stage N` 옵션) |

### agent.py 스캐폴딩 구조

```python
class PlannerAgent:
    def __init__(self): ...              # config 로드, 가이드 경로 설정

    async def run(self, planner_input: PlannerInput) -> ContentPlan:
        self._validate_input(planner_input)
        derived = await self._stage1_sort(planner_input)
        derived = await self._stage2_funnel(derived, planner_input)
        derived, candidates = await self._stage3_duplicate(derived, planner_input)
        derived, selected = await self._stage4_priority(derived, planner_input)
        selected, dist = self._stage5_balance(selected, planner_input)
        pieces = await self._stage6_structure(selected, planner_input)
        calendar = self._stage7_calendar(pieces, planner_input)
        return self._assemble(pieces, calendar, dist, candidates, planner_input)

    # Stage stubs — Phase별로 채워나감
    async def _stage1_sort(self, ...): raise NotImplementedError
    async def _stage2_funnel(self, ...): raise NotImplementedError
    async def _stage3_duplicate(self, ...): raise NotImplementedError
    async def _stage4_priority(self, ...): raise NotImplementedError
    def _stage5_balance(self, ...): raise NotImplementedError
    async def _stage6_structure(self, ...): raise NotImplementedError
    def _stage7_calendar(self, ...): raise NotImplementedError

    def _llm_call(self, prompt, context, json=False): ...   # OpenAI 호출 래퍼
    def _load_guide(self, filename): ...                     # 가이드 파일 로드
```

### run_planner.py 인터페이스

```bash
# 전체 실행 (리서처 스냅샷 입력)
python run_planner.py --input snapshots/researcher/2026-03-01.json

# N단계까지만 실행 후 중간 결과 저장
python run_planner.py --input snapshots/researcher/2026-03-01.json --stage 1

# 이전 중간 결과 이어받아 N단계부터 재실행
python run_planner.py --resume snapshots/planner/stage3_2026-03-01.json --stage 4
```

### 검증 기준

`python run_planner.py --stage 0` → `PlannerAgent` 초기화 성공, 입력 검증 통과

---

## 6. Phase 1 — Stage 1: 카테고리 매핑

### 만드는 파일

| 파일 | 설명 |
|------|------|
| `core/agents/planner/agent.py` | `_stage1_sort()` 구현 |
| `core/agents/planner/prompts/stage1_sort.md` | LLM 프롬프트 |

### 처리 로직

```
ResearchResult.clusters (전체 클러스터)
+ PlannerInput.questions (카테고리 원본 질문 목록)
    ↓
[LLM] 각 클러스터를 어느 카테고리 질문에 속하는지 의미 매핑
    ↓
DerivedQuestion 생성
  - question: cluster.paa_questions[0] 우선, 없으면 representative_keyword를 질문 형태로 변환
  - category: 매핑된 카테고리 원문 (매핑 불가 → "unassigned")
  - source_cluster_id: cluster.cluster_id
```

**파생 질문 소스 우선순위**:
1. `cluster.paa_questions[0]` — 이미 질문 형태로 완성
2. `cluster.representative_keyword` — LLM이 자연스러운 질문 형태로 변환

**LLM 입력/출력 형식**:

```json
// 입력
{
  "categories": ["ERP 외주 개발 업체를 고를 때...", "앱 개발 견적이..."],
  "clusters": [
    {
      "cluster_id": "c000",
      "representative_keyword": "외주개발사",
      "paa_questions": ["외주 개발 업체란 무엇인가요?", ...],
      "top_keywords": ["외주개발사", "외주 개발 업체", ...]
    }
  ]
}

// 출력
{
  "mappings": [
    {
      "cluster_id": "c000",
      "category": "ERP 외주 개발 업체를 고를 때...",
      "derived_question": "외주 개발 업체란 무엇인가요?",
      "question_source": "paa"
    }
  ]
}
```

- 매핑 불가 클러스터도 `mappings`에 포함 (`category: "unassigned"`)
- 입력의 모든 `cluster_id`가 `mappings`에 반드시 1개씩 포함

### 중간 저장

`snapshots/planner/{run_date}_stage1.json` — `list[DerivedQuestion]` 직렬화

### 검증 포인트

- [ ] 모든 클러스터(18개)가 어느 카테고리에 매핑됐는지
- [ ] 매핑 누락 클러스터 없음 (`category: "unassigned"` 포함 여부)
- [ ] `derived_question`이 자연스러운 질문 형태인지
- [ ] 3개 카테고리에 클러스터가 고르게 분포됐는지

---

## 7. Phase 2 — Stage 2: 퍼널 태깅

### 만드는 파일

| 파일 | 설명 |
|------|------|
| `core/agents/planner/agent.py` | `_stage2_funnel()` 구현 |
| `core/agents/planner/prompts/stage2_funnel.md` | LLM 프롬프트 (funnel_criteria.md 내용 포함) |

### 처리 로직

```
DerivedQuestion[] (1단계 출력)
+ 각 질문의 source_cluster_id → ResearchResult.clusters에서 SERP 데이터 참조
    ↓
[LLM] SERP 상위 콘텐츠 유형으로 퍼널 판단
  - google_content_meta + naver_content_meta의 content_type 분포
  - 정보설명형 다수 → awareness
  - 비교·리뷰형 다수 → consideration
  - 가격·계약·상담형 다수 → conversion
  - 명확하지 않으면 → unclassified
    ↓
DerivedQuestion.funnel + funnel_rationale 채워짐
```

**LLM 입력에 포함되는 SERP 데이터**:
- `google_serp_features.paa_questions` — 관련 질문 패턴
- `google_content_meta[0:5].content_type` — 구글 상위 콘텐츠 유형
- `naver_content_meta[0:5].content_type` — 네이버 상위 콘텐츠 유형
- `naver_serp_features.knowledge_snippet` — 네이버 지식스니펫 유무

### 검증 포인트

- [ ] SERP 데이터를 실제로 참조했는지 (funnel_rationale에 근거 포함 여부)
- [ ] 억지 분류 없이 unclassified가 적절히 사용됐는지
- [ ] 콘텐츠 방향성(content_direction)과 퍼널 분포가 대체로 일치하는지

---

## 8. Phase 3 — Stage 3: 중복 판정

### 만드는 파일

| 파일 | 설명 |
|------|------|
| `core/agents/planner/agent.py` | `_stage3_duplicate()` 구현 |
| `core/agents/planner/prompts/stage3_duplicate.md` | LLM 프롬프트 (소재 중복 H2 비교용) |

### 처리 로직

`published_contents`가 비어있으면 전체 통과 (위험도 0, verdict: "new").

중복 위험도 = **(키워드 겹침 × 0.4) + (제목 유사도 × 0.3) + (소재 중복 × 0.3)**

| 신호 | 판단 방식 | 비고 |
|------|-----------|------|
| 키워드 겹침 | **룰** — 질문의 대표 키워드 토큰이 published_contents.main_keyword에 출현하는 비율 | LLM 없음 |
| 제목 유사도 | **LLM** — 같은 질문에 답하려는 의도인지 의미 비교 | |
| 소재 중복 | **LLM** — 경쟁 콘텐츠 H2 목차 vs 기발행 콘텐츠 H2 비교 | cluster.h2_topics 활용 |

**비동기 병렬 처리**: 질문당 1회 LLM 호출 → `asyncio.gather`로 전체 병렬 실행

**판정 임계값** (duplicate_check.md 기본값):

| 구간 | verdict | 처리 |
|------|---------|------|
| 0.7 이상 | `update_existing` | UpdateCandidate로 분리, 신규 발행 대상 제외 |
| 0.4 ~ 0.7 | `angle_shift` | 각도 전환 가능 여부 추가 판단 → 가능하면 유지, 불가하면 UpdateCandidate |
| 0.4 미만 | `new` | 통과 |

**각도 전환 가능 조건** (duplicate_check.md):
- 기발행 글에 없는 H2 소주제 2개 이상 존재
- 퍼널 태그가 기발행 글과 다른 경우
- 기발행 글 발행일이 12개월 이상 경과 + SERP가 최근 6개월 글로 교체된 경우

### 검증 포인트

- [ ] 위험도 점수가 기발행 콘텐츠와 논리적으로 일치하는지
- [ ] 각도 전환 근거가 구체적인지 (H2 목차 비교 포함 여부)
- [ ] published_contents 없을 때 전체 통과되는지
- [ ] UpdateCandidate에 필수 필드(url, title, risk_score, improvement_points, urgency) 포함 여부

---

## 9. Phase 4 — Stage 4: 우선순위 배정 + 선정 근거

### 만드는 파일

| 파일 | 설명 |
|------|------|
| `core/agents/planner/agent.py` | `_stage4_priority()` 구현 |
| `core/agents/planner/prompts/stage4_rationale.md` | LLM 프롬프트 (선정 근거 문장 생성용) |

### 처리 로직

```
1. publishing_schedule.md에서 월간 발행 수 확인 (위시켓: 12건)
2. 카테고리당 배정 수 = 12 ÷ 카테고리 수
   - 나누어떨어지지 않으면 클러스터 수가 더 많은 카테고리에 1건씩 추가
3. 카테고리별 독립 정렬 (우선순위 기준 순서):
   a. total_volume_naver (검색량) — 1순위
   b. volume_trend (추세: rising > stable > declining) — 보조
4. 배정 수만큼 is_selected = True, 나머지는 is_waitlist = True
5. [LLM] 선발 콘텐츠 일괄 입력 → data_rationale + content_rationale 문장 생성
```

**우선순위 점수 계산 (룰)**:

```python
priority_score = (
    volume_weight(total_volume_naver)      # 로그 스케일 정규화 — 1순위
    + trend_weight(volume_trend)           # rising=1, stable=0.5, declining=0 — 보조
)
```

**content_direction 반영**:

| 방향성 | 선발 효과 |
|--------|-----------|
| 카테고리 포지셔닝 | awareness + 정의형 GEO 가중치 ↑ |
| 문제 인식 확산 | awareness~consideration 가중치 ↑ |
| 판단 기준 제시 | consideration + 비교형 GEO 가중치 ↑ |
| 실행 가이드 | consideration~conversion + 문제해결형 GEO 가중치 ↑ |

**선정 근거 형식** (LLM 출력):

```
data_rationale: "네이버 월간 검색량 3,200건(PC 1,200 / 모바일 2,000),
                  6개월 상승 추세. 구글 SERP 상위 5건 중 '비교 기준' 관점 없음."
content_rationale: "블로그에 이 키워드 관련 글 0건. 신규 발행 가능."
```

### 검증 포인트

- [ ] 카테고리당 배정 수가 맞는지 (`12 ÷ N`)
- [ ] 탐색 경로 상위 질문이 실제로 먼저 선발됐는지
- [ ] 선정 근거에 리서처 데이터 + 기존 블로그 두 가지가 포함됐는지
- [ ] 대기 풀이 카테고리별로 분리됐는지

---

## 10. Phase 5 — Stage 5 + 7: 퍼널 균형 + 캘린더 (룰 기반 묶음)

두 단계 모두 LLM 없음. 룰 기반이므로 한 Phase에 묶어 처리.
`archive.py` 완성도 이 Phase에서 함께 진행.

### 만드는 파일

| 파일 | 설명 |
|------|------|
| `core/agents/planner/agent.py` | `_stage5_balance()`, `_stage7_calendar()` 구현 |
| `core/agents/planner/archive.py` | `save_plan()`, `load_previous_funnel()` 완성 |

### Stage 5: 퍼널 균형 검증

```python
def _stage5_balance(selected, planner_input):
    dist = _compute_funnel_dist(selected)
    dominant = max(dist, key=dist.get)
    if dist[dominant] / dist.total >= 0.70:
        # 과잉 퍼널의 하위 순위 콘텐츠를 대기 풀의 부족 퍼널 콘텐츠로 교체
        # 교체 시 선정 근거 품질 유지 (근거가 약한 것으로 교체하지 않음)
        ...
    prev = archive.load_previous_funnel(planner_input.target_month)
    return selected, dist, prev
```

### Stage 7: 캘린더 배치

```python
def _stage7_calendar(pieces, planner_input):
    # 발행 요일 목록 생성 (publishing_schedule.md: 월/수/금, 09:00 KST)
    dates = _generate_publish_dates(planner_input.target_month)  # 12개

    # 배치 규칙 (우선순위 순서)
    # 규칙 1. 퍼널 교차: 같은 퍼널 연속 2회 이상 배치 방지
    # 규칙 2. 카테고리 순환: 같은 주에 같은 카테고리 2건 이상 방지
    ...
```

**예외 처리**:

| 상황 | 처리 |
|------|------|
| 해당 월 공휴일이 발행 요일과 겹침 | `is_holiday: True` 태그, 발행일 이동 없음 |
| 선발 콘텐츠 < 12건 | 대기 풀에서 추가 선발 → 여전히 부족하면 실제 수로 구성 + 미달 사유 표기 |
| 카테고리 수 < 발행 요일 수/주 | 퍼널 교차 우선 적용 |

### 검증 포인트

- [ ] 70% 초과 퍼널 있을 때 교체가 실제로 일어나는지
- [ ] 캘린더가 월/수/금 발행 요일을 따르는지
- [ ] 퍼널 교차 → 카테고리 순환 우선순위가 올바른지
- [ ] 직전 월 아카이브 없을 때 `previous_month_funnel: None`으로 graceful 처리되는지
- [ ] `archive.save_plan()`이 `archive/planner/runs/YYYY-MM.json`에 저장되는지

---

## 11. Phase 6 — Stage 6: 콘텐츠 세부 구조 설계

### 만드는 파일

| 파일 | 설명 |
|------|------|
| `core/agents/planner/agent.py` | `_stage6_structure()` 구현 |
| `core/agents/planner/prompts/stage6_structure.md` | LLM 프롬프트 (GEO × 퍼널 조합 매트릭스 포함) |

### 처리 로직

```
확정 ContentPiece 목록 (5단계 통과)
    ↓
[GEO 유형 배정] (질문 텍스트 패턴 기반 — LLM)
  - "~란", "~이란", "~뜻" → definition
  - "vs", "비교", "차이", "선정 기준" → comparison
  - "방법", "절차", "하는 법", "해결" → problem_solving
    ↓
[content_direction.md 조합 매트릭스 참조]
  GEO 유형 × 퍼널 태그 → H2 패턴 + CTA 강도 결정
    ↓
[LLM] 각 콘텐츠 설계 (선발 콘텐츠 일괄 입력)
  - H2 소제목 3~5개 (heading + description)
  - 경쟁 콘텐츠 H2 목차와 비교해 고유 관점 1개 이상 포함
  - 제목안 2개: SEO형(키워드 포함, 숫자/연도) + CTR형(호기심 유발, 문제 제기)
  - CTA 1개 (content_direction.md CTA 매핑 테이블 참조)
```

**GEO × 퍼널 조합 매트릭스** (content_direction.md):

| | 인지 | 고려 | 전환 |
|---|------|------|------|
| 정의형 | H2: 정의→구성→맥락→탐색 안내 / CTA: 관련 콘텐츠 | H2: 정의→상황별 적합성 / CTA: 체크리스트 | H2: 정의 짧게→실행 절차 / CTA: 프로젝트 등록 |
| 비교형 | H2: 비교 프레임 두텁게 / CTA: 가이드 | H2: 기준별 비교표 / CTA: 견적 비교 | H2: 조건부 추천 강화 / CTA: 상담 신청 |
| 문제해결형 | H2: 문제 제시 + 실수 강화 / CTA: 자가 진단 | H2: 단계 절차 + 판단 기준 / CTA: 체크리스트 | H2: 절차 구체화(숫자/기간) / CTA: 프로젝트 등록 |

### 검증 포인트

- [ ] GEO 유형 배정이 질문 텍스트와 일치하는지
- [ ] 복수 유형에 걸치는 질문은 지배적 유형 1개 선택 + 부차 패턴 삽입됐는지
- [ ] H2가 3~5개 범위인지
- [ ] 경쟁 콘텐츠 H2와 겹치지 않는 고유 관점이 1개 이상인지
- [ ] 제목안이 SEO형/CTR형 두 가지로 분리됐는지
- [ ] CTA가 퍼널에 맞게 배정됐는지 (인지→관련 콘텐츠, 전환→상담/등록)

---

## 12. Phase 7 — 조립 + 출력

### 만드는 파일

| 파일 | 설명 |
|------|------|
| `core/agents/planner/agent.py` | `_assemble()` 완성 |
| `run_planner.py` | 마크다운 리포트 출력 로직 추가 |

### 출력 파일

```
output/planner/{YYYY-MM}_{client}_plan.md    # 사람이 읽는 기획 문서
output/planner/{YYYY-MM}_{client}_plan.json  # 다음 단계 에이전트용 구조화 데이터
```

### 마크다운 출력 구조 (spec.md 기준)

```markdown
# [2026년 3월 위시켓 블로그 콘텐츠 전략]

## 1. 전략 요약
- 이번 달 핵심 목표: ...
- 전략 방향: ...

## 2. 콘텐츠 기획안 요약
### 기획 표 (카테고리 / 제목안 / 퍼널 / 발행일)
### 주요 근거
  1) 리서치 결과 기반
  2) 기존 콘텐츠 기반 (직전 월 퍼널 분포, 상위 성과 콘텐츠 포함)

## 3. 콘텐츠 세부 기획

### [카테고리 질문] 카테고리
#### [하위 질문] 콘텐츠 1
- 발행 목적
- 예상 제목안
- H2 구조
- 연동 CTA

## 부록: 기존 글 업데이트 후보
```

### 검증 포인트

- [ ] spec.md 출력 구조와 일치하는지
- [ ] 업데이트 후보 목록이 부록으로 포함됐는지
- [ ] 직전 월 퍼널 분포가 2. 주요 근거에 포함됐는지 (아카이브 있을 때)
- [ ] JSON 출력이 `ContentPlan` 스키마와 일치하는지

---

## 13. LLM 호출 전체 목록

모든 LLM 호출은 `_llm_call()` 래퍼를 통해 실행된다.

| 단계 | 레이블 | 모델 | 응답 형식 | 호출 방식 | 용도 |
|------|--------|------|-----------|-----------|------|
| Stage 1 | `stage1_sort` | gpt-4.1 | json_object | 1회 일괄 | 카테고리 매핑 |
| Stage 2 | `stage2_funnel` | gpt-4.1 | json_object | 1회 일괄 | SERP 기반 퍼널 태깅 |
| Stage 3 | `stage3_duplicate` | gpt-4.1-mini | json_object | 질문당 병렬 | 소재 중복 H2 비교 |
| Stage 4 | `stage4_rationale` | gpt-4.1 | text | 1회 일괄 | 선정 근거 문장 생성 |
| Stage 6 | `stage6_structure` | gpt-4.1 | json_object | 1회 일괄 | H2 + 제목안 + CTA 설계 |

### `_llm_call()` 동작 규칙

- `temperature: 0` (결정론적)
- `max_completion_tokens` 사용 (`max_tokens` 사용 금지 — gpt-5.2 호환성)
- JSON 응답 시 `response_format: {"type": "json_object"}` 자동 설정
- 재시도: 최대 2회 (총 3회 시도), 지수 백오프 2초 → 4초

---

## 14. 가이드 파일 로드 규칙

각 단계 진입 시 해당 가이드만 로드. 전체를 한꺼번에 읽지 않는다.
가이드 파일이 없으면 플래너 내장 기본값으로 동작하고 출력에 "기본값 적용됨" 표기.

| 단계 | 로드하는 가이드 |
|------|----------------|
| Stage 2 | `funnel_criteria.md` |
| Stage 3 | `duplicate_check.md` |
| Stage 4 | `publishing_schedule.md` |
| Stage 5 | `publishing_schedule.md` |
| Stage 6 | `content_direction.md`, `geo_classification.md` |
| Stage 7 | `publishing_schedule.md` |

**가이드 경로**:
- 기본(위시켓): `core/agents/planner/{filename}`
- 클라이언트별 확장: `clients/{client}/guides/{filename}` (해당 파일 있으면 우선)

`_load_guide(filename)` 메서드가 클라이언트 경로 → 기본 경로 순으로 탐색.

---

## 15. 에러 처리 패턴

### LLM 호출 실패

```python
def _llm_call(self, label, prompt, context, json=False):
    for attempt in range(3):
        try:
            return self._call_openai(prompt, context, json)
        except Exception as e:
            if attempt == 2:
                logger.error("[%s] LLM 호출 실패 3회: %s", label, e)
                raise
            wait = 2 ** attempt
            logger.warning("[%s] 재시도 %d/2, %ds 대기", label, attempt+1, wait)
            time.sleep(wait)
```

### 단계별 실패 처리 정책

| 단계 | 실패 시 |
|------|---------|
| Stage 1 | 예외 전파 (카테고리 매핑 없이 진행 불가) |
| Stage 2 | `funnel: "unclassified"` 기본값으로 계속 |
| Stage 3 | `verdict: "new"`, `risk_score: 0.0`으로 계속 (안전 방향) |
| Stage 4 | 예외 전파 (선정 없이 진행 불가) |
| Stage 5 | 균형 검증 스킵, 원본 선발 결과 유지 |
| Stage 6 | 해당 콘텐츠 구조 미완성으로 표기 후 계속 |
| Stage 7 | 예외 전파 (캘린더 없이 출력 불가) |

---

## 16. 스냅샷 및 아카이브

### 스냅샷 (단계별 중간 결과)

`--stage N` 실행 시 해당 단계 결과를 저장.

| 파일 | 내용 |
|------|------|
| `snapshots/planner/stage1_{date}.json` | `list[DerivedQuestion]` (category, exploration_order) |
| `snapshots/planner/stage2_{date}.json` | `list[DerivedQuestion]` (+ funnel) |
| `snapshots/planner/stage3_{date}.json` | `list[DerivedQuestion]` (+ duplicate_result) + `list[UpdateCandidate]` |
| `snapshots/planner/stage4_{date}.json` | `list[DerivedQuestion]` (+ is_selected) |
| `snapshots/planner/stage5_{date}.json` | 선발 확정 목록 + `FunnelDistribution` |
| `snapshots/planner/stage6_{date}.json` | `list[ContentPiece]` |
| `snapshots/planner/final_{date}.json` | `ContentPlan` 전체 |

`--resume` 옵션으로 특정 스냅샷에서 이어받아 재실행 가능.

### 아카이브

| 항목 | 경로 |
|------|------|
| 월별 기획 결과 | `archive/planner/runs/{YYYY-MM}.json` |

**저장 내용**: `ContentPlan` 전체 (퍼널 분포 포함)

**사용 용도**:
- Stage 5에서 직전 월 퍼널 분포 로드 → 현재 월 분포와 비교
- Phase 7 출력에서 직전 월 상위 성과 콘텐츠 참조

그 외 과거 데이터는 참조하지 않는다.

---

## 17. 설정 파일

**경로**: `core/agents/planner/config.yaml`

```yaml
name: planner
version: 1

models:
  main: gpt-4.1          # Stage 1, 2, 4, 6
  mini: gpt-4.1-mini     # Stage 3 (소재 중복 판단, 병렬)

output_schema: ContentPlan
guides_path: core/agents/planner/

archive:
  runs_dir: archive/planner/runs

quality_gate:
  min_selected: 1        # 최소 선발 콘텐츠 수 (미달 시 경고)

# 발행 파라미터 (publishing_schedule.md와 동기화)
publishing:
  monthly_count: 12
  days_of_week: [월, 수, 금]
  publish_time: "09:00"
  timezone: Asia/Seoul

# 중복 판정 임계값 (duplicate_check.md와 동기화)
duplicate:
  high_risk_threshold: 0.70
  angle_shift_threshold: 0.40
  weights:
    keyword_overlap: 0.4
    title_similarity: 0.3
    topic_overlap: 0.3

# 퍼널 균형
funnel:
  dominance_threshold: 0.70   # 이 비율 초과 시 편향으로 판단
```

---

## 18. 실행 방법

### 표준 실행

```bash
# 전체 실행 (리서처 스냅샷을 입력으로)
.venv/bin/python run_planner.py --input snapshots/researcher/2026-03-01.json

# N단계까지만 실행하고 중간 결과 확인
.venv/bin/python run_planner.py --input snapshots/researcher/2026-03-01.json --stage 1
.venv/bin/python run_planner.py --input snapshots/researcher/2026-03-01.json --stage 2

# 이전 중간 결과 이어받아 특정 단계부터 재실행
.venv/bin/python run_planner.py --resume snapshots/planner/stage3_2026-03-01.json --stage 4
```

### 테스트

```bash
.venv/bin/python -m pytest core/tests/test_planner.py
```

### 표준 입력 (리서처 표준 입력 결과 사용)

```
질문 의도 : 비교 판단
질문 형태
ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?
앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?
외주 개발 프로젝트를 진행할 때 가장 자주 발생하는 문제는 무엇이며, 어떻게 해결할 수 있나요?
콘텐츠 방향성 : 판단 기준 제시
```

---

## 부록: 리서처 → 플래너 인터페이스

플래너가 리서처 산출물에서 참조하는 필드 목록.

| 플래너 사용 위치 | ResearchResult 필드 | 용도 |
|-----------------|---------------------|------|
| 전 단계 | `source_questions` | 카테고리 원본 질문 확인 |
| Stage 1 | `clusters[].cluster_id` | DerivedQuestion.source_cluster_id |
| Stage 1 | `clusters[].representative_keyword` | 파생 질문 텍스트 생성 |
| Stage 1 | `clusters[].paa_questions` | 파생 질문 텍스트 생성 (우선) |
| Stage 1 | `clusters[].keywords[].keyword` | 파생 질문 텍스트 보완 |
| Stage 2 | `clusters[].google_content_meta[].content_type` | 퍼널 태깅 근거 |
| Stage 2 | `clusters[].naver_content_meta[].content_type` | 퍼널 태깅 근거 |
| Stage 2 | `clusters[].google_serp_features` | 퍼널 태깅 보조 |
| Stage 2 | `clusters[].naver_serp_features` | 퍼널 태깅 보조 |
| Stage 3 | `clusters[].h2_topics` | 소재 중복 비교 (경쟁 콘텐츠 H2) |
| Stage 4 | `clusters[].total_volume_naver` | 우선순위 검색량 기준 |
| Stage 4 | `clusters[].volume_trend` | 우선순위 트렌드 기준 |
| Stage 4 | `clusters[].google_content_meta` | data_rationale 근거 |
| Stage 6 | `clusters[].h2_topics` | 경쟁 콘텐츠 H2 비교 → 고유 관점 발굴 |
| Stage 6 | `clusters[].geo_citations` | GEO 노출 가능성 언급 |
