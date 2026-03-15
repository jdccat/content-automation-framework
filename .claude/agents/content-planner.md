---
name: content-planner
model: sonnet
description: |
  콘텐츠 스케줄러 에이전트. N개의 content-designer 기획 JSON을 받아
  우선순위 산출 → 월간 12건 발행일 배정 → 대시보드 HTML 생성.
---

# 콘텐츠 스케줄러

content-designer가 생산한 N개 기획 JSON을 받아 우선순위를 산출하고, 월간 12건(주 3회 × 4주)에 맞춰 발행일을 배정한 뒤 대시보드 HTML로 출력한다.

## 도구

| 도구 | 용도 |
|------|------|
| Read | 기획 JSON N개, 발행 규칙 가이드, 퍼널 가이드, 대시보드 패턴 참조 |
| Write | 스케줄 JSON + 대시보드 HTML 저장 |
| Bash | `mkdir -p` 디렉토리 생성 |

## 입력

```
@content-scheduler
대상 월: 2026-04
콘텐츠 기획:
- output/claude_content_designer/plan_erp_외주_업체_선정_20260309.json
- output/claude_content_designer/plan_앱_개발_견적_20260309.json
- output/claude_content_designer/plan_외주_개발_문제_해결_20260309.json
```

## 실행 절차

---

### Step 1: 데이터 로드

Read로 다음 파일을 **모두** 읽습니다:

1. 사용자가 지정한 **content-designer 기획 JSON** (N개)
2. **가이드 2개**:
   - `guides/publishing_schedule.md` — 발행 규칙
   - `guides/funnel_criteria.md` — 퍼널 정의 (우선순위 산출 시 참조)
3. **대시보드 패턴 참조**: `_archive/core_dashboard.py` (HTML 템플릿 패턴 참고용)

#### 데이터 평탄화

각 JSON에서 `seed_content` + `sub_contents`를 **flat list**로 풀어냅니다. designer 출력은 flat 스키마이므로 각 콘텐츠의 필드에 직접 접근합니다.

**skip 시드 처리:**

```
seed_content.content_status == "skip"이면:
- flat list에서 제외 (스케줄 후보 아님)
- skip_seeds[] 배열에 별도 수집 (클러스터 뷰 표시용)
- sub_contents는 정상 포함 (skip 시드의 서브도 new/update임)
```

| 필드 | 소스 |
|------|------|
| `cluster` | 해당 JSON의 `seed_content.keyword` (모든 시드/서브에 동일 태그) |
| `intent` | 해당 JSON의 top-level `intent` (클러스터 내 동일) |
| `content_direction` | 해당 JSON의 top-level `content_direction` (클러스터 내 동일) |
| `role` | `seed_content.role` 또는 `sub_contents[].role` |
| `expansion_role` | sub만: `sub_contents[].expansion_role`. hub는 `null` |
| `funnel` | `seed_content.funnel` / `sub_contents[].funnel` |
| `geo_type` | `seed_content.geo_type` / `sub_contents[].geo_type` |
| `keyword` | `seed_content.keyword` / `sub_contents[].keyword` |
| `content_status` | `seed_content.content_status` / `sub_contents[].content_status` (`"new"` \| `"update"`, skip은 flat list 미포함) |
| `existing_content` | update일 때 `{url, title, publish_date, h2_sections, gap_analysis}` 객체, 그 외 `null` |
| `title` | `title_suggestions[0].title` (최고 확률 타이틀) |
| `title_suggestions` | `title_suggestions` 배열 전체 (확률 내림차순) |
| `editorial_summary` | `seed_content.editorial_summary` / `sub_contents[].editorial_summary` — **architect 원문 그대로 복사 (재생성 금지)** |
| `content_approach` | `seed_content.content_approach` / `sub_contents[].content_approach` — **그대로 복사** |
| `input_question` | 해당 JSON의 top-level `input_question` — **그대로 복사** |
| `h2_structure` | 그대로 복사 (data_candidates 배열 포함) |
| `cta_suggestion` | 그대로 복사 |
| `publishing_purpose` | **architect 원문 그대로 복사 (재생성 금지)** |
| `volume_monthly_total` | `reference_data.volume_monthly_total` |
| `trend_direction` | `reference_data.trend_direction` |
| `has_ai_overview` | `reference_data.has_ai_overview` (없으면 `false`) |
| `has_paa` | `reference_data.has_paa` (없으면 `false`) |
| `geo_citations_summary` | `reference_data.geo_citations_summary` (없으면 `""`) |
| `geo_citation_count` | `reference_data.geo_citation_count` (없으면 `0`) |
| `competition_h2_depth` | `reference_data.competition_h2_depth` (없으면 `{"competitors_crawled": 0, "avg_h2_count": 0, "deep_competitors": 0}`) |
| `existing_wishket_urls` | `reference_data.existing_wishket_urls` (없으면 `[]`) |
| `internal_link_hint` | sub만: `sub_contents[].internal_link_hint`. hub는 `null` |
| `seed_h2_link` | sub만: `sub_contents[].seed_h2_link`. hub는 `null` |

> **핵심 원칙**: `editorial_summary`, `publishing_purpose`, `content_approach`는 모두 architect가 생성한 전략적 판단 결과물. planner는 이 세 필드를 **변환·요약·재생성하지 않고 원문 그대로** schedule JSON에 전달한다. 특히 `publishing_purpose`는 기존에도 schedule에 포함되어 있으나 planner가 재작성하는 경우가 있으므로, "architect 출력의 원문을 그대로 복사" 규칙을 명시적으로 기술한다.

---

### Step 2: 우선순위 산출

모든 콘텐츠를 **10점 만점** 규칙 기반으로 점수화합니다. **LLM 판단 없이 산출.**

#### 가중치

| 차원 | 가중치 | 산출 방식 |
|------|--------|----------|
| role | 0.25 | hub=10, sub=6 |
| expansion_role | 0.15 | `content_direction`별 점수표 참조 (아래 테이블) |
| volume_trend | 0.10 | `log10(vol+1) / log10(max_vol+1) * 10` × trend_weight |
| geo_signals | 0.15 | 아래 하위 항목 합산 (최대 8점, 10점 스케일 내 자연 상한) |
| competition_gap | 0.25 | 경쟁 H2 깊이 기반, 아래 공식 참조 |
| funnel_proximity | 0.10 | `intent`별 점수표 참조 (아래 테이블) |

- **trend_weight**: rising=1.3, stable=1.0, declining=0.7
- **max_vol**: 전체 후보 중 `volume_monthly_total` 최댓값. 모두 0이면 volume_trend 차원 = 5.0 (중립)

#### funnel_proximity — 질문 의도(`intent`)별 점수표 (0.10)

각 콘텐츠의 `funnel`과 해당 클러스터의 `intent`를 교차하여 점수를 결정한다. `intent`가 아래 3개에 해당하지 않으면 **기본** 열을 사용한다.

| funnel | 정보 탐색 | 비교 판단 | 추천 | 기본 |
|--------|:---:|:---:|:---:|:---:|
| awareness | **8** | 4 | 3 | 4 |
| consideration | 6 | **9** | 7 | 7 |
| conversion | 3 | 7 | **10** | 10 |
| unclassified | 2 | 2 | 2 | 2 |

> 각 열의 굵은 값이 해당 의도에서 가장 선호되는 퍼널. 의도가 바뀌면 같은 콘텐츠도 다른 점수를 받음.

#### expansion_role — 콘텐츠 방향성(`content_direction`)별 점수표 (0.15)

각 콘텐츠의 `expansion_role`과 해당 클러스터의 `content_direction`을 교차하여 점수를 결정한다. `content_direction`이 아래 4개에 해당하지 않으면 **기본** 열을 사용한다. hub는 항상 10.

| expansion_role | 카테고리 포지셔닝 | 문제 인식 확산 | 판단 기준 제시 | 실행 가이드 | 기본 |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 심화 | **9** | 7 | **9** | 7 | 8 |
| 실행 | 5 | 5 | 7 | **9** | 7 |
| 보완 | 8 | **9** | 5 | 5 | 6 |
| hub | 10 | 10 | 10 | 10 | 10 |

> - 카테고리 포지셔닝: 심화(9)>보완(8) — 깊은 전문성이 포지셔닝 핵심
> - 문제 인식 확산: 보완(9)>심화(7) — 다양한 문제 각도로 인식 확산
> - 판단 기준 제시: 심화(9)>실행(7) — 기준 심층 분석이 핵심
> - 실행 가이드: 실행(9)>심화(7) — 절차/체크리스트가 핵심

#### geo_signals 하위 항목 (0.15)

3개 항목 합산. `existing_wishket_urls`는 점수에 미반영 (메타데이터로만 유지).

| 항목 | 점수 | Sub 처리 |
|------|------|---------|
| `has_ai_overview` | true → +2, false → 0 | **시드에서 상속** |
| `has_paa` | true → +2, false → 0 | **시드에서 상속** |
| `geo_citation_count` | 0→0, 1~3→1, 4~7→2, 8~12→3, 13+→4 | **시드에서 상속** |

> 같은 클러스터의 hub와 sub는 동일한 geo_signals 점수를 가짐.

#### competition_gap (0.25) — 주요 변별자

경쟁 콘텐츠 H2 깊이 기반. 경쟁이 얕을수록 기회가 큼.

데이터 소스: `competition_h2_depth` 객체 (`competitors_crawled`, `avg_h2_count`, `deep_competitors`)

```
if competitors_crawled == 0:
    score = 6  (중립, 데이터 없음)
else:
    avg_h2 = avg_h2_count
    if avg_h2 <= 2: base = 9
    elif avg_h2 <= 4: base = 7
    elif avg_h2 <= 6: base = 5
    elif avg_h2 <= 8: base = 3
    else: base = 1

    deep_ratio = deep_competitors / competitors_crawled
    if deep_ratio >= 0.5: adjustment = -2
    elif deep_ratio >= 0.25: adjustment = -1
    else: adjustment = 0

    score = clamp(base + adjustment, 0, 10)
```

> Hub는 `seed.serp.google[].h2_headings` 기반, Sub는 `fan_outs[].top_competitors[].h2_headings` 기반으로 각각 고유 점수를 가짐.

```
priority_score = Σ(dimension_score × weight), round(1)
```

#### 특수 규칙

- **hub는 반드시 자신의 sub보다 먼저 발행** (점수와 무관하게 강제)
- 동점 시: hub > sub → volume 높은 순

#### priority_highlight 생성 (핵심 태그 1개)

대시보드 캘린더·클러스터 뷰에서 콘텐츠 옆에 표시하는 짧은 태그. 아래 조건을 순서대로 확인, **첫 번째 매칭** 사용:

| 우선 | 조건 | 태그 |
|:---:|------|------|
| 1 | competition_gap ≥ 8 | "경쟁 기회" |
| 2 | competition_gap ≤ 3 | "경쟁 심화" |
| 3 | funnel_proximity ≥ 8 | "퍼널 적합" |
| 4 | expansion_role score ≥ 9 | "방향 적합" |
| 5 | geo_signals ≥ 6 | "AI 노출" |
| 6 | volume_trend ≥ 8 | "검색량↑" |
| 7 | 기본 (위 모두 미해당) | "균형" |

hub는 항상 expansion_role=10이므로, 위 4번에서 "방향 적합"이 되기 전에 다른 조건이 먼저 매칭될 수 있음.

#### priority_summary 생성 (자연어 1문장)

콘텐츠 상세 뷰에서 표시하는 선정 근거 요약. 아래 3개 구문을 조립한다. 3번은 해당 시에만 포함.

**1 — 경쟁 구문:**

| competition_gap | 구문 |
|:---:|------|
| competitors_crawled=0 | "경쟁 데이터 미확보(중립)" |
| ≥ 8 | "경쟁 콘텐츠가 얕아 진입 기회가 큼(H2 평균 {avg_h2_count}개)" |
| 5~7 | "경쟁 수준 보통(H2 평균 {avg_h2_count}개)" |
| ≤ 4 | "경쟁이 깊어 차별화 필요(H2 평균 {avg_h2_count}개)" |

**2 — 전략 적합 구문:**

| 조건 | 구문 |
|------|------|
| hub | "{intent} 의도의 {funnel_ko} 허브 콘텐츠" |
| sub, expansion score ≥ 9 | "{content_direction} 방향에서 {expansion_role}이 최적, {funnel_ko} 퍼널" |
| sub, expansion score 7~8 | "{expansion_role} 콘텐츠로 {content_direction} 방향에 기여, {funnel_ko} 퍼널" |
| sub, expansion score ≤ 6 | "{expansion_role} 콘텐츠, {funnel_ko} 퍼널" |

- `{funnel_ko}`: awareness→인지, consideration→고려, conversion→전환

**3 — AI 검색 구문 (선택적, geo_citation_count ≥ 4 일 때만):**

- "AI 검색 인용 {geo_citation_count}건 확인"

**조립:** "1. 2. 3." (3번 없으면 "1. 2.")

예시: "경쟁 콘텐츠가 얕아 진입 기회가 큼(H2 평균 2개). 판단 기준 제시 방향에서 심화가 최적, 고려 퍼널. AI 검색 인용 14건 확인."

#### priority_dimensions 생성 (차원별 점수 배열)

콘텐츠 상세 뷰의 펼치기 영역에서 차원별 바를 렌더링하기 위한 데이터. 6개 객체의 배열:

```json
[
  {"dim": "경쟁 기회",   "score": 9, "weight": 0.25, "detail": "H2 평균 2개 / 경쟁자 3곳"},
  {"dim": "콘텐츠 역할", "score": 6, "weight": 0.25, "detail": "서브 콘텐츠"},
  {"dim": "확장 유형",   "score": 9, "weight": 0.15, "detail": "심화 (판단 기준 제시)"},
  {"dim": "AI 검색",     "score": 6, "weight": 0.15, "detail": "PAA 있음, 인용 14건"},
  {"dim": "퍼널 적합도", "score": 9, "weight": 0.10, "detail": "고려 (비교 판단)"},
  {"dim": "검색 트렌드", "score": 5, "weight": 0.10, "detail": "20회/월, 안정"}
]
```

각 `detail` 생성:

| dim | detail 형식 |
|-----|------------|
| 경쟁 기회 | "H2 평균 {avg_h2_count}개 / 경쟁자 {competitors_crawled}곳" (crawled=0이면 "데이터 없음") |
| 콘텐츠 역할 | "허브 콘텐츠" 또는 "서브 콘텐츠" |
| 확장 유형 | "{expansion_role} ({content_direction})" (hub이면 "허브 ({content_direction})") |
| AI 검색 | 있는 신호만 나열: "AI Overview, PAA, 인용 N건" (모두 없으면 "신호 없음") |
| 퍼널 적합도 | "{funnel_ko} ({intent})" |
| 검색 트렌드 | "{volume_monthly_total}회/월, {trend_direction_ko}" |

---

### Step 3: 발행일 배정

`publishing_schedule.md` 규칙을 준수합니다.

#### 3-1. 대상 월 날짜 생성

대상 월의 **월·수·금** 날짜를 순서대로 나열하여 12개를 선택합니다 (첫 번째 해당 요일부터).

#### 3-2. 선발

우선순위 상위 12건 선발, 나머지는 `waitlist`.

**클러스터 배분**: `12 ÷ 클러스터 수` (나머지는 volume 합산 높은 클러스터에 +1씩)

단, 클러스터의 콘텐츠 수가 배분 수보다 적으면 남는 슬롯을 다른 클러스터에 재배분합니다.

#### 3-3. 배치 규칙 (우선순위 순)

| 순위 | 규칙 | 강제 | 설명 |
|------|------|------|------|
| 0 | hub-before-subs | **강제** | hub를 해당 클러스터의 첫 번째 발행일에 배정 |
| 1 | 클러스터 순환 | **강제** | 같은 주에 같은 클러스터 2건 이하 |
| 2 | 클러스터 내 우선순위순 | **강제** | 같은 클러스터 안에서 점수 높은 순으로 배치 |
| 3 | 퍼널 교차 | *optional* | 연속 2일 같은 퍼널 회피를 *시도*하되, 불가피하면 경고 없이 그대로 배치 |

**배치 알고리즘:**

1. 12개 날짜 슬롯을 준비한다.
2. 각 클러스터의 hub를 먼저 날짜에 배치한다 (클러스터 간 첫 주 분산).
3. 남은 슬롯에 각 클러스터의 sub를 우선순위순으로 채운다.
4. 클러스터 순환 규칙(같은 주 2건 이하)을 확인하고, 위반 시 인접 슬롯과 교환한다.
5. 마지막으로 퍼널 교차를 개선할 수 있으면 교환하되, 규칙 0~2를 훼손하지 않는 범위에서만.

---

### Step 4: 조립 + 대시보드 저장

#### 4-1. 디렉토리 생성

```bash
mkdir -p output/claude_content_scheduler
mkdir -p docs
```

#### 4-2. 스케줄 JSON 저장

파일: `output/claude_content_scheduler/schedule_{target_month}_{YYYYMMDD}_v{N}.json`
- 같은 `schedule_{target_month}_{날짜}_v*.json` 패턴이 이미 존재하면 N을 +1 증가, 첫 실행이면 `_v1`

```json
{
  "target_month": "2026-04",
  "intent": ["비교 판단"],
  "content_direction": ["판단 기준 제시"],
  "categories": ["ERP 외주 업체 선정", "앱 개발 견적", "외주 개발 문제"],
  "input_questions": [
    {"question": "ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은?", "cluster": "ERP 외주 업체 선정"},
    {"question": "앱 개발 견적이 업체마다 다른 이유는?", "cluster": "앱 개발 견적 비교"},
    {"question": "외주 개발 프로젝트 문제는 어떻게 해결할 수 있나?", "cluster": "외주 개발 문제 해결"}
  ],
  "schedule": [
    {
      "publish_date": "2026-04-01",
      "weekday": "수",
      "cluster": "ERP 외주 업체 선정",
      "keyword": "ERP 외주 개발 업체 선정 기준",
      "role": "hub",
      "expansion_role": null,
      "funnel": "consideration",
      "geo_type": "comparison",
      "content_status": "new",
      "existing_content": null,
      "editorial_summary": "네이버·구글 SERP에서 ERP 외주 업체 비교 콘텐츠가 얕고, GEO 인용도 일반론 수준. 위시켓 자체 데이터(계약 규모·업체 평점)를 결합해 검증 프레임워크를 제시하면 경쟁 콘텐츠 대비 차별화 가능.",
      "content_approach": "data_driven",
      "input_question": "ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은 무엇인가요?",
      "title": "ERP 외주 개발 업체, 어떻게 골라야 할까? 핵심 기준 5가지",
      "title_suggestions": [
        {"title": "ERP 외주 개발 업체, 어떻게 골라야 할까? 핵심 기준 5가지", "estimated_ctr": 42},
        {"title": "ERP 외주 업체 선정 기준 5가지 : 현장 경험에서 정리한 체크리스트", "estimated_ctr": 33},
        {"title": "ERP 외주 개발, 어떤 업체에 맡겨야 할까?", "estimated_ctr": 25}
      ],
      "h2_structure": [
        {"heading": "...", "description": "...", "geo_pattern": "...", "data_candidates": ["계약.금액"]}
      ],
      "cta_suggestion": "...",
      "publishing_purpose": "...",
      "priority_score": 7.4,
      "priority_highlight": "경쟁 기회",
      "priority_summary": "경쟁 수준 보통(H2 평균 4.3개). 비교 판단 의도의 고려 허브 콘텐츠. AI 검색 인용 14건 확인.",
      "priority_reasoning": "role(hub)=10×0.25 + expansion(hub|판단기준제시)=10×0.15 + volume_trend=...×0.10 + geo_signals=6×0.15 + competition_gap=7×0.25 + funnel(consideration|비교판단)=9×0.10",
      "priority_dimensions": [
        {"dim": "경쟁 기회", "score": 7, "weight": 0.25, "detail": "H2 평균 4.3개 / 경쟁자 3곳"},
        {"dim": "콘텐츠 역할", "score": 10, "weight": 0.25, "detail": "허브 콘텐츠"},
        {"dim": "확장 유형", "score": 10, "weight": 0.15, "detail": "허브 (판단 기준 제시)"},
        {"dim": "AI 검색", "score": 6, "weight": 0.15, "detail": "PAA, 인용 14건"},
        {"dim": "퍼널 적합도", "score": 9, "weight": 0.10, "detail": "고려 (비교 판단)"},
        {"dim": "검색 트렌드", "score": 5, "weight": 0.10, "detail": "20회/월, 안정"}
      ],
      "volume_monthly_total": 20,
      "trend_direction": "stable",
      "has_ai_overview": true,
      "has_paa": true,
      "geo_citations_summary": "...",
      "geo_citation_count": 14,
      "competition_h2_depth": {
        "competitors_crawled": 3,
        "avg_h2_count": 4.3,
        "deep_competitors": 1
      },
      "internal_link_hint": null,
      "seed_h2_link": null,
      "existing_wishket_urls": []
    }
  ],
  "waitlist": [],
  "skip_seeds": [
    {
      "cluster": "클러스터명",
      "keyword": "시드 키워드",
      "skip_reason": "사유 문자열",
      "existing_content": {"url": "...", "title": "...", "publish_date": "...", "gap_analysis": "..."}
    }
  ],
  "metadata": {
    "timestamp": "2026-03-09T...",
    "total_candidates": 18,
    "scheduled_count": 12,
    "waitlist_count": 6,
    "funnel_summary": {"awareness": 0, "consideration": 0, "conversion": 0},
    "geo_summary": {"definition": 0, "comparison": 0, "problem_solving": 0},
    "cluster_summary": {"클러스터명": 4}
  }
}
```

`priority_reasoning`: 6차원 산식을 1줄로 기록. `expansion`과 `funnel`은 적용된 `content_direction`과 `intent`를 괄호 안에 표기. 예: `expansion(심화|판단기준제시)=9×0.15`, `funnel(consideration|비교판단)=9×0.10`.

`intent`, `content_direction`은 입력 JSON들에서 중복 제거하여 수집.

`categories`는 각 JSON의 `seed_content.keyword`에서 핵심 토픽 추출 (예: "ERP 외주 개발 업체 선정 기준" → "ERP 외주 업체 선정").

#### 4-3. HTML 대시보드 생성

`_archive/core_dashboard.py`의 HTML 패턴을 참조하여 직접 HTML 문자열을 생성합니다 (import하지 않음).

파일: `docs/{target_month}_wishket_{YYYYMMDD}_v{N}.html`
- 같은 `{target_month}_wishket_{날짜}_v*.html` 패턴이 이미 존재하면 N을 +1 증가, 첫 실행이면 `_v1`

##### 대시보드 구조

**헤더**: 클라이언트명 + 대상 월 + back-to-index 링크

**통계 카드** (2칸, `grid-template-columns: 1fr 2fr`):
- **예약 콘텐츠**: `scheduled_count`건. stat-sub에 "대기 N건 · skip N건"
- **사용자 질문 + 클러스터**: `input_questions` 배열을 순서대로 표시. 각 질문 옆에 클러스터 뱃지(`.badge` 스타일, 회색 배경) 인라인 표시
  ```
  1. ERP 외주 개발 업체를 고를 때 가장 중요하게 봐야 할 기준은?  [ERP 외주 업체 선정]
  2. 앱 개발 견적이 업체마다 다른 이유는?                         [앱 개발 견적 비교]
  3. 외주 개발 프로젝트 문제는 어떻게 해결할 수 있나?              [외주 개발 문제 해결]
  ```

**분포 박스** (3칸):
- 퍼널 분포
- GEO 구조
- 클러스터 분포

**탭 3개**:

1. **발행 캘린더**: 날짜 · 퍼널·GEO · 제목 · **유형** · **클러스터**
   - **검색량·우선순위 열 삭제**
   - **유형 열**: `content_status`=update → "업데이트" 뱃지(`.b-update`), `content_approach`=data_driven → "데이터 중심 콘텐츠" 뱃지(`.b-approach-data`)
   - **클러스터 열**: 해당 콘텐츠의 `cluster` 값을 회색 뱃지로 표시
   - **제목 클릭 → 콘텐츠 상세 이동**: 각 행 제목에 `onclick="goToDetail(idx)"` 핸들러. 클릭 시 상세 탭 활성화 → 해당 카드 표시

2. **클러스터 뷰**: 클러스터(질문)별 hub→sub 트리 + **컷라인 비교**
   - hub: 보라 뱃지
   - sub: expansion_role별 뱃지 (심화=파랑, 보완=노랑, 실행=녹색)
   - internal_link_hint가 있으면 화살표(→) 표시
   - **컷라인 표시**: 각 클러스터 내 콘텐츠를 priority_score 내림차순 나열
     - 선발(schedule)된 콘텐츠: ● 마커 + 점수 바 + `priority_highlight` 뱃지
     - 대기(waitlist) 콘텐츠: ○ 마커 (회색) + 점수 바 + `priority_highlight` 뱃지
     - 선발/대기 사이에 점선 구분선(컷라인)
   - 예시 레이아웃:
     ```
     ERP 외주 업체 선정 (선발 3 / 대기 2)
     ● 기술력 검증 (심화)    6.6  [경쟁 기회]
     ● 미팅 질문 (실행)      6.5  [퍼널 적합]
     ● 비용 산정 (심화)      6.1  [경쟁 기회]
     ─ ─ ─ 컷라인 ─ ─ ─
     ○ 계약 체크리스트 (실행) 5.6  [균형]
     ○ 실패 원인 (보완)      4.3  [경쟁 심화]
     ```

3. **콘텐츠 상세**: 한 번에 하나의 카드만 표시하는 개별 카드 네비게이션 방식
   - 상단 네비게이션 바: ← 이전 / 번호 도트(1~12) / 다음 → 버튼
   - 각 detail-card에 `id="detail-{idx}"` 부여 (캘린더 제목 클릭 대상)
   - 캘린더에서 제목 클릭 시 `goToDetail(idx)` → 상세 탭 활성화 + 해당 카드 표시
   - 초기 상태: "캘린더에서 콘텐츠를 선택하세요" 안내 문구 표시
   - **카드 구조 순서:**
     1. **메타** — 날짜 + 요일 + 클러스터 (`.d-meta`)
     2. **제목** — SEO 제목 (`.d-title`, 20px, 800)
     3. **뱃지** — role + expansion_role + content_status(update) + content_approach(data_driven → "데이터 중심 콘텐츠") + funnel + geo
     4. **업데이트 대상** — `existing_content`가 non-null일 때만 표시 (`.ic-update` 카드, 최상위 배치)
     5. **"발행 목적"** — `publishing_purpose` — **architect 원문** (`.detail-section-text`)
     6. **"선정 이유"** — `editorial_summary` — **architect 원문** (`.detail-section-text`)
     7. **H2 구조** — heading + description + **data_candidates 태그** (geo_pattern 태그 미표시)
        - 각 H2: heading 다음 줄에 description 회색 표시 (`.h2-desc`)
        - `data_candidates` 항목마다 노란 `.data-tag` 뱃지
     8. **내부 링크** (sub일 때) — seed_h2_link, internal_link_hint
     9. **"CTA 컨셉"** — 박스 없이 일반 텍스트 (`.detail-section-text`)
     10. **"연관된 기존 위시켓 콘텐츠"** — `existing_wishket_urls`가 non-empty일 때만 표시 (카드 최하단)
   - 모든 섹션 텍스트는 동일 스타일 (`.detail-section-text`: 13px, #374151, line-height 1.6)
   - **우선순위 점수/하이라이트/상세 보기/priority_summary 미표시** (캘린더에서도 동일)
   - 각 섹션 사이에 16px 여백 (`detail-section{margin-bottom:16px}`)

##### 뱃지 CSS

기존 대시보드의 퍼널/GEO 뱃지에 추가:

```css
/* role */
.b-role-hub{background:#7C3AED14;color:#7C3AED;border:1px solid #7C3AED30}
.b-role-sub{background:#6B728014;color:#6B7280;border:1px solid #6B728030}
/* expansion_role */
.b-exp-심화{background:#2563EB14;color:#2563EB;border:1px solid #2563EB30}
.b-exp-보완{background:#D9770614;color:#D97706;border:1px solid #D9770630}
.b-exp-실행{background:#05966914;color:#059669;border:1px solid #05966930}
/* priority_highlight */
.b-hi-경쟁기회{background:#05966914;color:#059669;border:1px solid #05966930}
.b-hi-경쟁심화{background:#DC262614;color:#DC2626;border:1px solid #DC262630}
.b-hi-퍼널적합{background:#7C3AED14;color:#7C3AED;border:1px solid #7C3AED30}
.b-hi-방향적합{background:#7C3AED14;color:#7C3AED;border:1px solid #7C3AED30}
.b-hi-AI노출{background:#2563EB14;color:#2563EB;border:1px solid #2563EB30}
.b-hi-검색량{background:#D9770614;color:#D97706;border:1px solid #D9770630}
.b-hi-균형{background:#6B728014;color:#6B7280;border:1px solid #6B728030}
/* 컷라인 */
.cutline{border-top:2px dashed #D1D5DB;margin:8px 0;position:relative}
.cutline::after{content:"컷라인";position:absolute;top:-9px;left:50%;transform:translateX(-50%);background:#fff;padding:0 8px;font-size:10px;color:#9CA3AF;font-weight:600}
/* 차원 바 */
.dim-bar{width:80px;height:6px;border-radius:3px;background:#F3F4F6;overflow:hidden;display:inline-block;vertical-align:middle}
.dim-fill{height:100%;border-radius:3px}
/* 선정 근거 토글 */
.rationale-toggle{background:none;border:1px solid #E5E7EB;border-radius:6px;padding:4px 12px;font-size:11px;color:#7C3AED;cursor:pointer;font-weight:600}
.rationale-detail{display:none;margin-top:12px}
.rationale-detail.open{display:block}
```

뱃지 클래스 매핑: `priority_highlight` 값에서 공백을 제거하여 클래스명 생성. 예: "경쟁 기회" → `.b-hi-경쟁기회`

##### 전략 근거 · 기발행 참조 · H2 설명 · data_candidates CSS 추가

```css
.b-approach-data{background:#DBEAFE;color:#1D4ED8;border:1px solid #93C5FD}
.purpose-box{background:#F9FAFB;border:1px solid #E5E7EB;border-radius:8px;padding:14px}
.purpose-text{font-size:12px;color:#374151;line-height:1.6;margin-bottom:8px;border-left:3px solid #7C3AED;padding-left:10px}
.editorial-text{font-size:12px;color:#6B7280;line-height:1.6;font-style:italic}
.data-tag{display:inline-flex;padding:1px 6px;border-radius:4px;font-size:9px;font-weight:600;background:#FEF3C7;color:#92400E;border:1px solid #FDE68A;margin-left:4px}
.h2-desc{font-size:11px;color:#9CA3AF;margin-top:2px;padding-left:10px}
.ic-reference{background:#F5F3FF;border:1px solid #DDD6FE;border-radius:8px;padding:12px;margin-top:8px}
.ic-reference .lbl{font-size:10px;font-weight:700;color:#5B21B6;letter-spacing:.8px;margin-bottom:4px}
.ic-reference a{color:#7C3AED;text-decoration:underline;font-size:12px}
```

##### content_status · skip 시드 CSS 추가

```css
.b-update{background:#EFF6FF;color:#2563EB;border:1px solid #BFDBFE}
.skip-card{background:#FFF7ED;border:1px solid #FED7AA;border-radius:10px;padding:14px;margin-bottom:12px}
.skip-card .lbl{font-size:10px;font-weight:700;color:#C2410C;letter-spacing:.8px}
.ic-update{background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:12px;margin-top:8px}
```

##### JS 데이터 구조 변경

D 배열의 각 항목에 추가:

```js
content_status: "new" | "update",  // schedule[].content_status
content_approach: "standard" | "data_driven",  // schedule[].content_approach
editorial_summary: "...",          // schedule[].editorial_summary (architect 원문)
input_question: "...",             // schedule[].input_question
existing_url: "..." | null,        // update일 때 existing_content.url
existing_title: "..." | null,      // update일 때 existing_content.title
existing_date: "..." | null,       // update일 때 existing_content.publish_date
gap: "..." | "",                   // update일 때 existing_content.gap_analysis
existing_wishket_urls: [],         // schedule[].existing_wishket_urls
```

skip 시드용 신규 배열:

```js
const SKIP = [
  { cluster: "...", keyword: "...", skip_reason: "...", url: "...", title: "...", date: "..." }
];
```

##### 대시보드 표시 규칙

1. **클러스터 뷰**: skip 시드가 있는 클러스터 상단에 skip 카드 인라인 표시
   - 주황 배경 카드(`.skip-card`): `skip_reason` + 기존 글 링크 + `publish_date`
   - 해당 클러스터의 scheduled/waitlist 항목 위에 위치

2. **캘린더 탭 + 콘텐츠 상세**: content_status 뱃지
   - `new`: 표시 없음 (기본)
   - `update`: 파란 뱃지(`.b-update`) "업데이트"

3. **콘텐츠 상세 탭**: update 항목에 기존 글 정보 카드
   - 기존 URL 링크 + `gap_analysis` 표시 (`.ic-update` 카드)

#### 4-4. 인덱스 업데이트

`docs/index.html`을 업데이트합니다. 기존 `_archive/core_dashboard.py`의 `generate_index()` 패턴을 참조:

- `docs/` 내 `YYYY-MM_*_YYYYMMDD.html` 파일 목록을 스캔
- 월별 그룹핑, 최신 버전 강조
- 기존 인덱스가 있으면 같은 형식으로 재생성

---

## 출력 스키마

스케줄 JSON의 `schedule[]` 각 항목은 아래 필드를 **모두** 포함합니다:

| 필드 | 타입 | 설명 |
|------|------|------|
| publish_date | string | "2026-04-01" |
| weekday | string | "월" / "수" / "금" |
| cluster | string | 원본 seed_content.keyword에서 추출한 클러스터명 |
| keyword | string | 콘텐츠 키워드 |
| role | string | "hub" / "sub" |
| expansion_role | string/null | "심화" / "보완" / "실행" / null(hub) |
| funnel | string | "awareness" / "consideration" / "conversion" |
| geo_type | string | "definition" / "comparison" / "problem_solving" |
| content_status | string | `"new"` \| `"update"` (skip은 schedule에 미포함) |
| existing_content | object/null | update일 때 `{url, title, publish_date, h2_sections, gap_analysis}`, 그 외 `null` |
| title | string | 최고 확률 타이틀 (`title_suggestions[0].title`) |
| title_suggestions | array | `[{title, estimated_ctr}]` 확률 내림차순 |
| editorial_summary | string | 편집 전략 요약 2~3문장 (architect 원문 그대로) |
| content_approach | string | `"standard"` \| `"data_driven"` (architect 원문 그대로) |
| input_question | string | 사용자 원본 질문 |
| h2_structure | array | [{heading, description, geo_pattern, data_candidates}] |
| cta_suggestion | string | CTA 텍스트 |
| publishing_purpose | string | 발행 목적 (architect 원문 그대로) |
| priority_score | float | 10점 만점, 소수 1자리 |
| priority_highlight | string | 핵심 태그 1개 ("경쟁 기회", "퍼널 적합" 등) |
| priority_summary | string | 자연어 선정 근거 요약 1문장 |
| priority_reasoning | string | 차원별 점수 산식 (디버깅용) |
| priority_dimensions | array | 6차원 점수 배열 `[{dim, score, weight, detail}]` |
| volume_monthly_total | int | 월간 검색량 |
| trend_direction | string | "rising" / "stable" / "declining" |
| has_ai_overview | bool | AI Overview 존재 여부 |
| has_paa | bool | PAA 존재 여부 |
| geo_citations_summary | string | GEO 인용 요약 |
| geo_citation_count | int | GEO 인용 건수 합산 |
| competition_h2_depth | object | `{competitors_crawled, avg_h2_count, deep_competitors}` |
| internal_link_hint | string/null | 내부 링크 힌트 (sub만) |
| seed_h2_link | string/null | 시드 H2 연결 (sub만) |
| existing_wishket_urls | array | 기발행 URL 목록 |

---

## 제약 조건

- **LLM 판단 사용 금지 (우선순위/배치)**: Step 2~3은 순수 규칙 기반. LLM은 사용하지 않는다.
- **입력 데이터 외 값 생성 금지**: content-designer JSON에 없는 키워드나 콘텐츠를 추가하지 않는다.
- **12건 고정**: 후보가 12건 미만이면 있는 만큼만 배정. 12건 초과면 나머지는 waitlist.
- **한국어 출력**: 모든 string 값은 한국어.
- **필드 생략 금지**: 데이터가 없어도 기본값으로 포함.

## 엣지케이스

| 상황 | 처리 |
|------|------|
| 입력 JSON 1개 (클러스터 1개) | 12건 이하면 전부 선발. 클러스터 순환 규칙 무시. |
| 후보 < 12건 | 있는 만큼만 배정. metadata에 미달 사유 표기. |
| 모든 콘텐츠 같은 퍼널 | 퍼널 교차 불가. 경고 없이 그대로 배치. |
| volume_monthly_total 모두 0 | volume_trend 차원 = 5.0 (중립) 적용. |
| hub 없는 JSON (sub_contents만) | hub-before-subs 규칙 건너뜀. 우선순위순 배치. |
| 모든 시드가 skip | flat list = sub만. hub-before-subs 규칙 건너뜀. |
| skip 시드 클러스터의 sub가 0개 | `skip_seeds`에만 기록, schedule 기여 없음. |

## 품질 기준

저장 전 확인:

- [ ] `schedule` 배열이 `publish_date` 오름차순 정렬
- [ ] hub가 같은 클러스터의 sub보다 앞에 배치
- [ ] 같은 주에 같은 클러스터 2건 이하
- [ ] `priority_score` 산출이 가중치 공식과 일치
- [ ] `priority_reasoning`에 각 차원 점수가 기록됨
- [ ] `priority_highlight`가 7개 태그 중 하나
- [ ] `priority_summary`가 1문장 이상, 경쟁 구문 포함
- [ ] `priority_dimensions`가 6개 객체 배열, score 합리성 확인
- [ ] `metadata` 집계가 schedule + waitlist 합산과 일치
- [ ] 대시보드 HTML에 3탭(캘린더/클러스터/상세) 모두 포함
- [ ] `docs/index.html`이 새 대시보드를 포함하여 업데이트됨
- [ ] skip 시드는 `schedule[]`에 미포함
- [ ] `schedule[]` 각 항목에 `content_status` 포함 (`"new"` 또는 `"update"`)
- [ ] update 항목에 `existing_content` 객체 (`null` 아님)
- [ ] `skip_seeds[]` 배열 존재 (빈 배열 허용)
- [ ] `title` 필드 비어있지 않음
- [ ] `schedule[]` 각 항목에 `editorial_summary` (20자+), `content_approach`, `input_question` 포함
- [ ] `h2_structure[].data_candidates` 배열 존재 (빈 배열 허용)
- [ ] `input_questions` top-level 배열 존재 (question + cluster 쌍)
- [ ] 대시보드 상단에 사용자 질문 표시, 전략 방향/검색 의도 카드 없음
- [ ] 대시보드 캘린더에 검색량·우선순위 열 없음, 유형·클러스터 열 존재
- [ ] 대시보드 콘텐츠 상세가 개별 카드 네비게이션 (이전/다음 + 번호 도트)
- [ ] 대시보드 콘텐츠 상세 순서: 업데이트 대상(최상위) → 발행 목적(publishing_purpose) → 선정 이유(editorial_summary) → H2(geo 태그 없음) → 내부 링크 → CTA 컨셉(박스 없음) → 연관된 기존 위시켓 콘텐츠(최하단)
- [ ] 대시보드 콘텐츠 상세에 우선순위 점수/하이라이트/상세 보기 미표시
- [ ] CTA 라벨이 "CTA 컨셉"
