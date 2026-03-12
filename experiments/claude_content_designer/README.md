# 콘텐츠 설계자 서브에이전트

리서처 결과를 받아 시드 + 팬아웃 콘텐츠 기획을 생성합니다.

## 호출 방법

```
@content-designer
리서처 결과: output/claude_researcher/seed_erp_외주_업체_선정_20260309.json
기발행 DB: data/wishket_published.json
```

## 파이프라인

```
리서처 JSON → [Step 1: 데이터 로드] → [Step 2: 시드 설계] → [Step 3: 팬아웃 설계] → [Step 4: 조립+저장]
```

## 출력

```
output/claude_content_designer/plan_{시드키워드요약}_{날짜}.json
```

## 설계 항목 (콘텐츠당)

| 항목 | 설명 |
|------|------|
| funnel | awareness / consideration / conversion |
| geo_type | definition / comparison / problem_solving |
| h2_structure | 3~5개 H2 + 섹션 설명 + geo_pattern |
| title_suggestions | SEO용 + CTR용 각 1개 |
| cta_suggestion | 퍼널별 CTA 매핑 기준 |
| publishing_purpose | 검색 의도 + 위시켓 가치 연결 |

## 참조 가이드

- `core/agents/planner/funnel_criteria.md` — 퍼널 3단계 판정 기준
- `core/agents/planner/geo_classification.md` — GEO 3타입 구조 패턴
- `core/agents/planner/content_direction.md` — GEO×퍼널 9셀 매트릭스 + CTA 매핑

## 스코프 외

카테고리 매핑, 우선순위 점수, 날짜 배정, 캘린더 생성은 별도 서브에이전트에서 처리합니다.
