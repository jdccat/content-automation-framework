# 콘텐츠 설계자 에이전트 스펙

## 목적

리서처 출력 JSON을 받아 시드 콘텐츠 1개 + 팬아웃 콘텐츠 N개의 **기획 문서**를 생성한다.
퍼널 태깅, GEO 타입 결정, H2 구조 설계, 타이틀 2개, CTA가 각 콘텐츠별로 포함된다.

**스코프 외**: 카테고리 매핑, 우선순위 점수, 날짜 배정, 중복 검출 점수화, 캘린더 생성.

---

## 선행 에이전트

```
researcher → [output/claude_researcher/seed_*.json] → content-designer
```

---

## 파이프라인 (4 Stage)

| Stage | 타입 | 입력 | 출력 |
|-------|------|------|------|
| 1. 데이터 로드 | rule | 리서처 JSON + 가이드 3개 + 위시켓 발행 DB | 메모리 내 컨텍스트 |
| 2. 시드 설계 | llm | seed.* + intent + content_direction | SeedContent |
| 3. 팬아웃 설계 | llm | fan_outs[] + SeedContent | SubContent[] |
| 4. 조립 + 저장 | rule | SeedContent + SubContent[] | plan_*.json |

---

## 입력 스키마 (리서처 JSON 활용 필드)

### 시드 설계에 사용

| 리서처 필드 | 활용 |
|------------|------|
| `intent` | 퍼널 1차 시그널 |
| `content_direction` | GEO 타입 1차 시그널 |
| `seed.keyword` | 시드 콘텐츠 키워드 |
| `seed.volume.monthly_total` | reference_data.volume_monthly_total |
| `seed.naver_trend.seed.direction` | reference_data.trend_direction (0이면 parent_keywords 대표값) |
| `seed.serp_features.google` | has_ai_overview, has_paa |
| `seed.paa_questions` | 퍼널 보조 시그널 + H2 아이디어 |
| `seed.h2_topics` | 경쟁 H2 참고 (차별화 소스) |
| `seed.geo_citations` | geo_citations_summary 생성 소스 |
| `seed.serp.google[].h2_headings` | 경쟁 H2 구조 참고 |

### 팬아웃 설계에 사용

| 리서처 필드 | 활용 |
|------------|------|
| `fan_outs[].keyword` | 서브 콘텐츠 키워드 |
| `fan_outs[].question` | 서브 콘텐츠 질문 |
| `fan_outs[].relation` | expansion_role 결정 힌트 |
| `fan_outs[].content_angle` | publishing_purpose 힌트 |
| `fan_outs[].volume.monthly_total` | reference_data.volume_monthly_total |
| `fan_outs[].naver_trend.direction` | reference_data.trend_direction |
| `fan_outs[].top_competitors[].h2_headings` | H2 경쟁 분석 |

---

## 출력 스키마

```json
{
  "input_question": "string",
  "intent": "string",
  "content_direction": "string",

  "seed_content": {
    "keyword": "string",
    "role": "hub",
    "funnel": "awareness | consideration | conversion | unclassified",
    "funnel_reasoning": "string (2~3문장)",
    "geo_type": "definition | comparison | problem_solving",
    "geo_reasoning": "string (1~2문장)",
    "publishing_purpose": "string (1문장: 검색 의도 + 위시켓 가치 연결)",
    "title_suggestions": [
      {"title": "string", "strategy": "seo"},
      {"title": "string", "strategy": "ctr"}
    ],
    "h2_structure": [
      {
        "heading": "string",
        "description": "string (해당 섹션에서 다룰 내용 1~2문장)",
        "geo_pattern": "definition | comparison | problem_solving"
      }
    ],
    "cta_suggestion": "string (content_direction.md CTA 매핑 기준)",
    "reference_data": {
      "volume_monthly_total": "int",
      "trend_direction": "rising | stable | declining",
      "has_ai_overview": "bool",
      "has_paa": "bool",
      "geo_citations_summary": "string (예: ChatGPT 4건, Claude 5건, 위시켓 2건 인용)",
      "existing_wishket_urls": ["string"]
    }
  },

  "sub_contents": [
    {
      "keyword": "string",
      "question": "string",
      "relation": "string",
      "role": "sub",
      "expansion_role": "심화 | 보완 | 실행",
      "seed_h2_link": "string | null (시드 H2 헤딩 텍스트, 없으면 null)",
      "funnel": "awareness | consideration | conversion | unclassified",
      "funnel_reasoning": "string",
      "geo_type": "definition | comparison | problem_solving",
      "geo_reasoning": "string",
      "publishing_purpose": "string (시드와의 관계 포함)",
      "title_suggestions": [
        {"title": "string", "strategy": "seo"},
        {"title": "string", "strategy": "ctr"}
      ],
      "h2_structure": [
        {
          "heading": "string",
          "description": "string",
          "geo_pattern": "definition | comparison | problem_solving"
        }
      ],
      "cta_suggestion": "string",
      "internal_link_hint": "string (시드의 어떤 H2에서 이 콘텐츠로 링크할지)",
      "reference_data": {
        "volume_monthly_total": "int",
        "trend_direction": "rising | stable | declining",
        "existing_wishket_urls": ["string"]
      }
    }
  ],

  "metadata": {
    "timestamp": "ISO8601",
    "seed_count": 1,
    "sub_count": "int",
    "funnel_summary": {"awareness": "int", "consideration": "int", "conversion": "int"},
    "geo_summary": {"definition": "int", "comparison": "int", "problem_solving": "int"}
  }
}
```

---

## 핵심 설계 결정

### expansion_role 정의

| 값 | 의미 | seed_h2_link |
|----|------|-------------|
| 심화 | 시드 H2의 특정 주제를 별도 글로 깊게 파기 | 해당 H2 텍스트 |
| 보완 | 시드가 다루지 못한 관점 추가 | null |
| 실행 | 시드의 개념을 구체적 실행 절차로 확장 | null 또는 관련 H2 |

### trend_direction 결정 규칙

`seed.naver_trend.seed.direction`이 "stable"이고 average가 0이면:
→ `seed.naver_trend.parent_keywords` 중 대표 키워드의 direction 사용.

### existing_wishket_urls 결정 규칙

`data/wishket_published.json`에서 현재 키워드의 핵심 토큰(2개 이상)을 `main_keyword` 또는 `title`에 포함한 항목의 URL만 수집.

### geo_citations_summary 생성 규칙

`seed.geo_citations` 전체를 순회하여:
- 서비스별 총 인용 건수 집계
- `is_wishket: true`인 인용 건수 집계
- 형식: "ChatGPT N건, Claude N건, 위시켓 N건 인용" (인용 0건인 서비스는 생략)

---

## 품질 게이트

| 항목 | 기준 |
|------|------|
| h2_structure | 시드+각 서브 모두 3개 이상 |
| title_suggestions | 모든 콘텐츠에 seo+ctr 각 1개 |
| funnel_reasoning | 2문장 이상, 구체적 근거 포함 |
| sub_contents | fan_outs 수와 동일 |
| expansion_role | 팬아웃 전체가 동일 role로 몰리지 않음 |

---

## 도구

| 도구 | 용도 |
|------|------|
| Read | 리서처 JSON, 가이드 파일 3개, wishket_published.json |
| Write | 결과 JSON 저장 |
| Bash | `mkdir -p output/claude_content_designer` |

외부 API 호출 없음. 리서처가 이미 데이터 수집 완료.
