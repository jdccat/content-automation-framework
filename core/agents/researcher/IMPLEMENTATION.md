# 리서처 에이전트 — 구현 참조 문서

> **기준 코드**: `core/agents/researcher/agent.py` / config v5 / spec v2
> **최종 갱신**: 2026-02-27

---

## 목차

1. [개요](#1-개요)
2. [입출력 스키마](#2-입출력-스키마)
3. [전체 파이프라인 흐름](#3-전체-파이프라인-흐름)
4. [사전 처리: 파싱 + 시드 필터](#4-사전-처리-파싱--시드-필터)
5. [1단계: 키워드 확장 및 클러스터링](#5-1단계-키워드-확장-및-클러스터링)
6. [2단계: 검증 수집](#6-2단계-검증-수집)
7. [3단계: 외부 AI 환경 수집](#7-3단계-외부-ai-환경-수집)
8. [LLM 호출 전체 목록](#8-llm-호출-전체-목록)
9. [도구 전체 목록](#9-도구-전체-목록)
10. [에러 처리 패턴](#10-에러-처리-패턴)
11. [스냅샷 및 아카이브](#11-스냅샷-및-아카이브)
12. [설정 파일](#12-설정-파일)
13. [환경 변수](#13-환경-변수)
14. [실행 방법](#14-실행-방법)

---

## 1. 개요

### 에이전트 역할

사용자 입력(질문 의도 + 질문 형태 + 콘텐츠 방향성)을 받아 **키워드 리서치 데이터를 수집**한다.
판단·우선순위·퍼널 분류는 하지 않는다. 모든 판단은 플래너 에이전트가 수행한다.

### 파이프라인 구조

```
입력 텍스트
    ↓
[파서] → ParsedInput
    ↓
[시드 필터] (LLM, gpt-4.1-mini)
    ↓
┌─────────────────────────────┐
│  1단계: 키워드 확장·클러스터링  │
│  LLM 4회, 도구 다수 (병렬+순차) │
└─────────────────────────────┘
    ↓  포커스 클러스터 대표 키워드만 전달
┌─────────────────────────────┐
│  2단계: 검증 수집             │
│  도구 위주, LLM 없음           │
└─────────────────────────────┘
    ↓
┌─────────────────────────────┐
│  3단계: GEO 인용 수집         │
│  4개 AI 서비스 병렬             │
└─────────────────────────────┘
    ↓
[어셈블러] → ResearchResult
    ↓
[품질 게이트 검사]
    ↓
[아카이브 저장]
```

### LLM 호출 수

| 구분 | 모델 | 호출 수 |
|------|------|---------|
| 시드 필터 | gpt-4.1-mini | 1회 |
| 1d 클러스터링 | gpt-5.2 | 1회 |
| 1e 대표 선정 | gpt-4.1-mini | 1회 |
| 1f 아카이브 비교 | gpt-5.2 | 1회 |
| 1g 포커스 선정 | gpt-5.2 | 1회 |
| **합계** | | **5회** |

도메인 필터(`domain_filter`)가 활성화되면 최대 6회.

---

## 2. 입출력 스키마

### 입력 (`ParsedInput` — dataclass)

```python
main_keyword: str         # 입력에서 추출한 핵심 키워드
entry_moment: str         # 사용자 진입 모먼트
intent: str               # 의도 (비교 판단 / 정보 탐색 / 문제 해결 등)
questions: list[str]      # 원본 질문 목록
direction: str            # 콘텐츠 방향성
extracted_seeds: list[str]  # 정규식으로 추출한 초기 시드 키워드
```

### 출력 (`ResearchResult` — Pydantic)

```python
run_date: str
main_keyword: str
entry_moment: str
intent: str
source_questions: list[str]
content_direction: str
extracted_seeds: list[str]
clusters: list[Cluster]       # is_focus=True 클러스터만 포함
orphan_keywords: list[str]    # 어떤 클러스터에도 속하지 못한 키워드
```

### 클러스터 (`Cluster` — Pydantic)

```python
cluster_id: str
representative_keyword: str
representative_rationale: str
is_focus: bool
archive_verdict: str          # "new" | "merged" | "duplicate"
keywords: list[KeywordInCluster]

# 2단계 데이터
total_volume_naver_pc: int
total_volume_naver_mobile: int
google_trend_series: list[{period, ratio}]
naver_trend_series: list[{period, ratio}]
google_content_metas: list[ContentMeta]
naver_content_metas: list[ContentMeta]
h2_topics: list[str]
google_serp_features: GoogleSerpFeatures
naver_serp_features: NaverSerpFeatures

# 3단계 데이터
geo_citations: list[GeoCitation]
```

---

## 3. 전체 파이프라인 흐름

```
입력 텍스트
│
├─[파서] 정규식으로 시드 추출 + 의도/방향성 파싱
│
├─[_filter_archive_seeds()] 아카이브 중복 제거 (토큰 겹침 기준)
│
├─[seed_filter] LLM (gpt-4.1-mini) → 범용 명사 필터링
│
└─▶ STAGE 1
    │
    ├─[1a] asyncio.gather 병렬 도구 수집
    │       ├─ search_suggestions (Google + Naver 자동완성)
    │       ├─ google_related_searches (연관 검색어)
    │       ├─ google_paa (PAA 질문)
    │       └─ naver_keyword_volume (검색량 + 연관 키워드, 배치 5)
    │
    ├─[1a-LT] 2토큰 키워드 롱테일 재확장 (최대 max_second_pass=10개)
    │
    ├─[1b] clients/{client}/context.yaml → customer_language 추가
    │
    ├─[1d] LLM 클러스터링 (gpt-5.2, max_completion_tokens=16384)
    │       → ClusterDraft[] + orphans[]
    │
    ├─[1e] LLM 대표 키워드 선정 (gpt-4.1-mini)
    │       fallback: 볼륨 최고 → 길이 짧은 순
    │
    ├─[1f] LLM 아카이브 비교 (gpt-5.2)
    │       → archive_verdict: new | merged | duplicate
    │
    └─[1g] LLM 포커스 클러스터 선정 (gpt-5.2)
            → is_focus 플래그

    ↓ 포커스 클러스터 대표 키워드 목록만 전달

    └─▶ STAGE 2 (3개 태스크 asyncio.gather 병렬)
        │
        ├─[2a] 검색량 + 트렌드 수집 (순차, rate limit 회피)
        │       ├─ naver_searchad: 배치 5, 1초 간격
        │       ├─ google_trends: 배치 5, 5초 간격
        │       └─ naver_datalab: 배치 5, asyncio.gather 병렬
        │
        ├─[2c] 상위 콘텐츠 분석 (플랫폼 분리)
        │       ├─ Google: google_search(10) → web_fetch H2 추출 (병렬)
        │       └─ Naver: naver_blog_search(10), 배치 5, 1초 간격
        │
        ├─[2d] H2 갭 분석 (룰 베이스, API 없음)
        │       → 노이즈 정규식 필터 → 페이지당 10개, 클러스터당 30개
        │
        └─[2e] SERP 피처 수집
                ├─ Google: google_search 캐시에서 휴리스틱 추출
                └─ Naver: naver_serp_features (OpenAI web_search_preview)

    ↓

    └─▶ STAGE 3 (키워드별, 4개 서비스 asyncio.gather 병렬)
        │
        ├─ ai_search (ChatGPT 인용)
        ├─ perplexity_search (NotImplementedError → 스킵)
        ├─ geo_claude_browser (Playwright — 수동 로그인 필요)
        └─ geo_gemini_browser (Playwright — 수동 로그인 필요)

    ↓

    └─▶ [어셈블러] ResearchResult 조립
        → [품질 게이트] clusters_min=1, keywords_per_cluster_min=3
        → [아카이브 저장] archive/researcher/
```

---

## 4. 사전 처리: 파싱 + 시드 필터

### 입력 파서

정규식 기반. LLM 없음.

```
질문 의도 : 비교 판단          → intent
질문 형태
ERP 외주 개발 업체를 ...       → questions[0]
앱 개발 견적이 ...             → questions[1]
콘텐츠 방향성 : 판단 기준 제시  → direction
```

추출된 키워드 시드는 `extracted_seeds`로 저장.

### 아카이브 시드 필터 (`_filter_archive_seeds`)

토큰 겹침 기준으로 이미 아카이브에 있는 클러스터와 완전히 겹치는 시드를 제외.
LLM 없음, 룰 베이스.

### 시드 필터 LLM 호출 (gpt-4.1-mini)

범용 명사(일반 단어)와 의미 없는 키워드를 제거한다.

- 프롬프트: `core/prompts/seed_filter.txt`
- 모델: `gpt-4.1-mini`
- `max_completion_tokens`: 2048
- 입력: `extracted_seeds` JSON 배열
- 출력: 필터링된 키워드 JSON 배열

---

## 5. 1단계: 키워드 확장 및 클러스터링

### 1a. 병렬 도구 수집

`asyncio.gather`로 4개 도구 동시 실행.

| 도구 | API | 입력 | 출력 |
|------|-----|------|------|
| `search_suggestions()` | 자동완성 API | 시드 키워드 | `{google: [], naver: []}` |
| `google_related_searches()` | OpenAI web_search_preview | 시드 키워드 | 연관 검색어 목록 |
| `google_paa()` | OpenAI web_search_preview | 시드 키워드 | PAA 질문 목록 |
| `naver_keyword_volume()` | 네이버 SearchAd | 키워드 목록 (최대 5개) | 검색량 + 연관 키워드 |

**주의**: `search_suggestions()`는 2토큰 이상 시드에만 호출.
네이버 자동완성은 User-Agent 헤더 필수.

수집 결과는 `RawKeywordPool`에 플랫폼별로 분리 저장:

```python
google: list[str]        # 구글 자동완성 + 연관 검색어 + 시드
naver: list[str]         # 네이버 자동완성
keyword_tool: list[str]  # SearchAd 연관 키워드
paa: list[str]           # PAA 질문을 키워드화
paa_questions: dict[str, list[str]]  # 키워드 → PAA 질문 매핑
volumes: dict[str, int]   # 정규화 키워드 → 월간 총 검색량
volumes_pc: dict[str, int]
volumes_mobile: dict[str, int]
```

### 1a-LT. 롱테일 재확장

2토큰 키워드만 대상. 자동완성 재호출 → 3토큰 이상 결과만 추가.
최대 `config.longtail.max_second_pass`개 (기본값: 10).

### 1b. 고객 언어 추가

`clients/{client}/context.yaml`의 `customer_language` 섹션을 로드.
해당 파일이 없거나 섹션이 비어있으면 건너뜀.
추가된 키워드는 `discovery_source: "internal_data"`로 태깅.

### 1d. LLM 클러스터링

**LLM 호출 #1**

| 항목 | 값 |
|------|-----|
| 모델 | `gpt-5.2` |
| 프롬프트 | `core/prompts/1d_clustering.txt` |
| `max_completion_tokens` | 16384 |
| 응답 형식 | `json_object` |
| temperature | 0 |

입력: 중복 제거된 전체 키워드 JSON 배열
출력 형식:
```json
{
  "clusters": [
    {"keywords": [...], "shared_intent": "..."}
  ],
  "orphans": [...]
}
```

제약:
- 클러스터당 최소 3개, 최대 25개 키워드 (초과분 → orphans)
- 응답이 ` ```json ... ``` ` 블록 또는 raw JSON 모두 파싱 처리

### 1e. LLM 대표 키워드 선정

**LLM 호출 #2**

| 항목 | 값 |
|------|-----|
| 모델 | `gpt-4.1-mini` |
| 프롬프트 | `core/prompts/1e_representative.txt` |
| 응답 형식 | `json_object` |

입력: 클러스터별 키워드 목록 + 검색량
출력: `[{id, representative, rationale}, ...]`

**폴백 (LLM 실패 시)**: 검색량 최고 키워드 → 동률이면 짧은 키워드.

### 1f. 아카이브 비교

**LLM 호출 #3**

| 항목 | 값 |
|------|-----|
| 모델 | `gpt-5.2` |
| 프롬프트 | `core/prompts/1f_archive.txt` |
| 응답 형식 | `json_object` |

입력: 신규 대표 키워드 vs 아카이브 대표 키워드 목록
출력 (`archive_verdict`):

| 값 | 의미 | 처리 |
|----|------|------|
| `"new"` | 아카이브에 없음 | 신규 등록 |
| `"merged"` | 기존 클러스터와 의미 동일 | 신규 키워드를 기존 클러스터에 병합 |
| `"duplicate"` | 완전 중복 | 플래그 설정 (이후 단계에서 중복 콘텐츠 방지용) |

### 1g. 포커스 클러스터 선정

**LLM 호출 #4**

| 항목 | 값 |
|------|-----|
| 모델 | `gpt-5.2` |
| 프롬프트 | `core/prompts/1g_focus.txt` |
| 응답 형식 | `json_object` |

입력: 사용자 원본 질문 목록 + 전체 클러스터(대표 키워드, 하부 키워드)
출력: 포커스 클러스터 ID 목록 → 각 클러스터에 `is_focus` 플래그 설정

제약: `config.focus.max_ratio = 0.70` → 포커스 클러스터 ≤ 전체 70%.

### 1단계 출력 (`Stage1Output`)

```python
cluster_drafts: list[ClusterDraft]   # 전체 클러스터 (포커스 + 비포커스)
orphan_keywords: list[str]
paa_questions: dict[str, list[str]]
volumes: dict[str, int]
volumes_pc: dict[str, int]
volumes_mobile: dict[str, int]
```

**2단계로 전달하는 것**: 포커스 클러스터의 대표 키워드만.
**아카이브로 저장하는 것**: 전체 클러스터 (포커스 여부 무관).

---

## 6. 2단계: 검증 수집

포커스 클러스터 대표 키워드별로 수행. 3개 태스크를 `asyncio.gather`로 병렬 실행.

```
2a (검색량+트렌드)  ─┐
2c (상위 콘텐츠)    ─┼─ asyncio.gather → Stage2Output
2e (SERP 피처)     ─┘
     ↕
2d는 2c 결과에서 룰 베이스로 파생 (별도 API 없음)
```

### 2a. 검색량 + 트렌드 수집

**rate limit 회피를 위해 순차 실행.**

#### 네이버 SearchAd (순차, 배치 5, 1초 간격)

- 1단계에서 이미 볼륨이 있으면 스킵.
- 공백 제거 변형도 추가 조회 (`"외주 개발"` → `"외주개발"`).

#### Google Trends (순차, 배치 5, **5초 간격**)

- 6개월 일간 데이터.
- 방향성 계산: 전반/후반 평균 비교, ±5 임계값.
- 429 에러 시 지수 백오프 (10초 → 20초 → 40초).

#### 네이버 DataLab (배치 5, asyncio.gather 병렬)

- 6개월 일간 데이터.

**출력 형식 (키워드별)**:
```python
{
    "naver_volume": int,
    "naver_volume_pc": int,
    "naver_volume_mobile": int,
    "google_trend_avg": float,
    "naver_trend_avg": float,
    "google_direction": "상승" | "안정" | "하락",
    "naver_direction": "상승" | "안정" | "하락",
    "google_trend_series": [{"period": str, "ratio": float}, ...],
    "naver_trend_series": [{"period": str, "ratio": float}, ...]
}
```

### 2c. 상위 콘텐츠 분석 (플랫폼 분리)

#### 구글 (병렬 H2 수집)

1. `google_search(keyword, 10)` 호출 (결과 캐시됨, 2e와 공유)
2. 관련성 필터: 키워드 토큰 겹침 체크 → 환각성 결과 제거
3. 상위 10개 URL에 `web_fetch()` 병렬 호출 → H2 헤딩 구조 추출
4. 각 결과: title, url, H2 목록, publish_date, content_type, is_competitor

#### 네이버 (순차, 배치 5, 1초 간격)

1. `naver_blog_search(keyword, 10)` 호출
2. 각 결과: title, url, exposure_area, publish_date, content_type, is_competitor

**content_type 분류**: 도메인 기반 룰 (video / blog / news / wiki / government / community / website)

**경쟁사 플래그**: `config.competitor_domains` 목록에 포함된 도메인 자동 태깅.

### 2d. H2 갭 분석 (룰 베이스, API 없음)

구글 상위 콘텐츠에서 추출한 H2를 정제한다.

**노이즈 필터 (정규식)**:
- 내비게이션 / 푸터 / 광고 / UI 요소 패턴 제거

**품질 기준**:
- 길이: 2~80자
- 언어: 한국어/영어 혼용 (외국 문자 40% 초과 → 노이즈)
- 페이지당 최대 10개, 클러스터당 최대 30개

### 2e. SERP 피처 수집

#### 구글 (캐시 기반 + 휴리스틱)

`google_search` 결과 캐시에서 추출. 별도 API 없음.

```python
{
    "ai_overview": False,              # 현재 하드코딩 (Google API 필요)
    "featured_snippet_exists": bool,   # 첫 스니펫 200자+ 이면 True
    "featured_snippet_url": str,
    "paa_questions": list[str]         # 1단계 google_paa 결과 재사용
}
```

#### 네이버 (naver_serp_features 도구)

OpenAI web_search_preview 기반.

```python
{
    "knowledge_snippet": bool,         # 지식스니펫 노출 여부
    "smart_block": bool,               # 스마트블록 노출 여부
    "smart_block_components": list[str]  # 스마트블록 구성 요소
}
```

### 2단계 출력 (`Stage2Output`)

```python
volumes: dict[str, VolumeData]
google_content_metas: dict[str, list[ContentMeta]]
naver_content_metas: dict[str, list[ContentMeta]]
h2_topics: dict[str, list[str]]
google_serp_features: dict[str, GoogleSerpFeatures]
naver_serp_features: dict[str, NaverSerpFeatures]
```

---

## 7. 3단계: 외부 AI 환경 수집

### 동작 방식

대표 키워드를 질문 형태로 변환 후 4개 AI 서비스에 병렬 입력.

**키워드 → 질문 변환**: `_keyword_to_question(kw)` 내부 함수
예: `"ERP 외주 개발"` → `"ERP 외주 개발은 어떻게 하나요?"`

**4개 서비스 (asyncio.gather)**:

| 서비스 | 도구 | 현재 상태 |
|--------|------|----------|
| ChatGPT | `ai_search()` | 정상 작동 |
| Perplexity | `perplexity_search()` | NotImplementedError → 자동 스킵 |
| Claude | `geo_claude_browser()` | Playwright, 수동 로그인 필요 |
| Gemini | `geo_gemini_browser()` | Playwright, 수동 로그인 필요 |

### 인용 데이터 형식 (`GeoCitation`)

```python
url: str
domain: str
context_summary: str   # 해당 소스가 인용된 맥락 요약
source: "chatgpt" | "perplexity" | "claude" | "gemini"
is_own_domain: bool    # wishket.com 여부
is_competitor: bool    # competitor_domains 포함 여부
```

중복 제거: `seen_urls` set으로 서비스 간 URL 중복 방지.

### Playwright 브라우저 초기 설정

Claude/Gemini 브라우저 도구 첫 실행 전 수동 로그인 필요:

```bash
python -m core.tools.geo_browser --login claude
python -m core.tools.geo_browser --login gemini
```

---

## 8. LLM 호출 전체 목록

모든 LLM 호출은 `_llm_call()` 래퍼를 통해 실행된다.

| 단계 | 레이블 | 모델 | max_completion_tokens | 응답 형식 | 용도 |
|------|--------|------|----------------------|-----------|------|
| 사전 | `seed_filter` | gpt-4.1-mini | 2048 | json_object | 범용 명사 필터 |
| 1d | `1d_clustering` | gpt-5.2 | 16384 | json_object | 의미 클러스터링 |
| 1e | `1e_representative` | gpt-4.1-mini | 기본값 | json_object | 대표 키워드 선정 |
| 1f | `1f_archive` | gpt-5.2 | 기본값 | json_object | 아카이브 비교 |
| 1g | `1g_focus` | gpt-5.2 | 기본값 | json_object | 포커스 선정 |

### `_llm_call()` 동작 규칙

- `temperature: 0` (결정론적)
- **`max_tokens` 사용 금지** — gpt-5.2는 `max_completion_tokens`만 지원
- 시스템/유저 프롬프트에 "JSON" 포함 시 `response_format: {"type": "json_object"}` 자동 설정
- 재시도: 최대 2회 (총 3회 시도), 지수 백오프 2초 → 4초

---

## 9. 도구 전체 목록

### 1단계 도구

| 도구 | 파일 | API | 배치 | 지연 |
|------|------|-----|------|------|
| `search_suggestions()` | `core/tools/autocomplete.py` | 자동완성 | 단건 | 없음 |
| `google_related_searches()` | `core/tools/google_related.py` | OpenAI web_search_preview | 단건 | 없음 |
| `google_paa()` | `core/tools/google_paa.py` | OpenAI web_search_preview | 단건 | 없음 |
| `naver_keyword_volume()` | `core/tools/naver_searchad.py` | 네이버 SearchAd | 5 | 1초 |

### 2단계 도구

| 도구 | 파일 | API | 배치 | 지연 |
|------|------|-----|------|------|
| `naver_keyword_volume()` | `core/tools/naver_searchad.py` | 네이버 SearchAd | 5 | 1초 |
| `google_keyword_trend()` | `core/tools/google_trends.py` | Google Trends (pytrends) | 5 | **5초** |
| `naver_keyword_trend()` | `core/tools/naver_datalab.py` | 네이버 DataLab | 5 | 병렬 |
| `google_search()` | `core/tools/google_search.py` | OpenAI web_search_preview | 단건 | 없음 |
| `naver_blog_search()` | `core/tools/naver_search.py` | 네이버 검색 API | 5 | 1초 |
| `web_fetch()` | `core/tools/web_fetch.py` | 직접 HTTP | 단건 | 없음 |
| `naver_serp_features()` | `core/tools/naver_serp.py` | OpenAI web_search_preview | 단건 | 없음 |

### 3단계 도구

| 도구 | 파일 | 상태 |
|------|------|------|
| `ai_search()` | `core/tools/ai_search.py` | 정상 작동 |
| `perplexity_search()` | `core/tools/perplexity_search.py` | NotImplementedError (키 없음) |
| `geo_claude_browser()` | `core/tools/geo_browser.py` | Playwright (수동 로그인 필요) |
| `geo_gemini_browser()` | `core/tools/geo_browser.py` | Playwright (수동 로그인 필요) |

---

## 10. 에러 처리 패턴

### `_safe_tool_call()` 래퍼

모든 도구 호출을 감싸는 표준 에러 격리 패턴.

```python
async def _safe_tool_call(label: str, coro, default=None):
    try:
        return await coro
    except NotImplementedError:
        logger.info("[%s] 미구현 — 스킵", label)
        return default
    except Exception as e:
        logger.warning("[%s] 실패: %s", label, e)
        return default
```

**원칙**: 도구 실패는 파이프라인을 중단하지 않는다. default 값으로 계속 진행.

| 도구 실패 시 default | 효과 |
|----------------------|------|
| `{}` (빈 딕셔너리) | 해당 키워드 데이터 없음으로 처리 |
| `[]` (빈 리스트) | 해당 결과 없음으로 처리 |
| `None` | 해당 필드 null |

### Rate Limit 처리

| API | 전략 |
|-----|------|
| Google Trends 429 | 지수 백오프: 10초 → 20초 → 40초 |
| 네이버 SearchAd | 순차 + 배치 5 + 1초 간격 |
| 네이버 Blog Search | 순차 + 배치 5 + 1초 간격 |
| Google Trends (일반) | 배치 5 + **5초 간격** (적극적 예방) |

### JSON 파싱 보호

OpenAI web_search_preview 응답에 잘못된 유니코드 이스케이프가 포함될 수 있음:

```python
# 적용 위치: web_search_preview 응답 파싱 전
text = re.sub(r'\\u(?![0-9a-fA-F]{4})', r'\\\\u', text)
```

---

## 11. 스냅샷 및 아카이브

### 스냅샷 (실행마다 저장)

단계별 중간 결과를 JSON으로 저장. 디버깅 및 재시작용.

| 스냅샷 키 | 내용 |
|-----------|------|
| `input` | ParsedInput |
| `stage1_keywords` | RawKeywordPool (1a 도구 수집 결과) |
| `stage1_deduped` | 중복 제거 + 필터 이후 |
| `stage1d_clusters` | 클러스터링 결과 |
| `stage1e_clusters` | 대표 키워드 선정 후 |
| `stage1f_clusters` | 아카이브 비교 후 |
| `stage2_serp` | 2단계 전체 출력 |
| `stage3_geo` | 3단계 인용 수집 결과 |

### 아카이브

| 항목 | 경로 |
|------|------|
| 인덱스 | `archive/researcher/index.json` |
| 실행별 결과 | `archive/researcher/runs/{YYYY-MM-DD}.json` |

**용도**:
1. `1f`에서 신규 클러스터와 기존 대표 키워드 비교
2. 다음 실행 시 고립 키워드를 추가 시드로 재투입

---

## 12. 설정 파일

**경로**: `core/agents/researcher/config.yaml`

```yaml
name: researcher
version: 5

models:
  main: gpt-5.2       # 클러스터링·아카이브·포커스
  mini: gpt-4.1-mini  # 시드 필터·대표 선정

seed_filter:
  enabled: true

archive:
  runs_dir: archive/researcher/runs
  index_file: archive/researcher/index.json

longtail:
  max_second_pass: 10   # 롱테일 재확장 대상 키워드 최대 수

focus:
  max_ratio: 0.70       # 포커스 클러스터 비율 상한

competitor_domains:
  - wishket.com
  - freemoa.net
  - kmong.com
  # ...

quality_gate:
  clusters_min: 1
  keywords_per_cluster_min: 3

tools:
  stage1_expansion: [autocomplete, google_related, google_paa, naver_searchad]
  stage2_validation: [naver_searchad, google_trends, naver_datalab, google_search, naver_search, web_fetch, naver_serp_features]
  stage3_geo: [ai_search, perplexity_search, claude_browser, gemini_browser]
```

---

## 13. 환경 변수

### 필수

| 변수 | 용도 |
|------|------|
| `OPENAI_API_KEY` | gpt-5.2, gpt-4.1-mini, web_search_preview |
| `NAVER_AD_CUSTOMER_ID` | 네이버 SearchAd |
| `NAVER_AD_API_KEY` | 네이버 SearchAd |
| `NAVER_AD_API_SECRET` | 네이버 SearchAd |

### 선택

| 변수 | 용도 | 없을 때 |
|------|------|---------|
| `NAVER_CLIENT_ID` | 네이버 Open API | 네이버 블로그 검색 불가 |
| `NAVER_CLIENT_SECRET` | 네이버 Open API | 네이버 블로그 검색 불가 |
| `PERPLEXITY_API_KEY` | Perplexity 인용 수집 | NotImplementedError, 자동 스킵 |

---

## 14. 실행 방법

### 직접 실행

```bash
# .env 로드 후 실행
.venv/bin/python run_researcher_v4.py
```

**표준 입력 템플릿**:
```
질문 의도 : 비교 판단
질문 형태
ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?
앱 개발 견적이 업체마다 다른 이유는 무엇이며 어떤 기준으로 판단해야 하나요?
외주 개발 프로젝트를 진행할 때 가장 자주 발생하는 문제는 무엇이며, 어떻게 해결할 수 있나요?
콘텐츠 방향성 : 판단 기준 제시
```

### 테스트 실행

```bash
.venv/bin/python -m pytest core/tests/test_researcher_v4.py
# → 102개 테스트 통과 (spec v2 + 품질 개선 5건)
```

### Playwright 로그인 (3단계 브라우저 도구 사용 전)

```bash
python -m core.tools.geo_browser --login claude
python -m core.tools.geo_browser --login gemini
```

---

## 부록: 알려진 제약 및 주의사항

| 항목 | 내용 |
|------|------|
| gpt-5.2 토큰 파라미터 | `max_tokens` 지원 안 함 → 반드시 `max_completion_tokens` 사용 |
| Google Trends 안정성 | 429 rate limit 빈번. 배치 5 + 5초 간격으로 완화. 완전 해결 안 됨 |
| 네이버 자동완성 | User-Agent 헤더 없으면 빈 응답 반환 |
| web_search_preview 파싱 | 잘못된 `\uXXXX` 이스케이프 포함 가능 → 보호 regex 필수 |
| Perplexity | API 키 미설정 상태. NotImplementedError 반환 → 자동 스킵 처리됨 |
| Google AI Overview | 현재 하드코딩 False (Google API 없음) |
| naver_searchad 429 | 1단계 대량 호출 후 2단계에서 rate limit 발생 가능 |
