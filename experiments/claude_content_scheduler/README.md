# Content Scheduler (Claude 서브에이전트)

content-designer가 생산한 N개 기획 JSON을 받아 우선순위 산출 → 월간 12건 발행일 배정 → 대시보드 HTML을 생성하는 서브에이전트.

## 사용법

```
@content-scheduler
대상 월: 2026-04
콘텐츠 기획:
- output/claude_content_designer/plan_erp_외주_업체_선정_20260309.json
- output/claude_content_designer/plan_앱_개발_견적_20260309.json
- output/claude_content_designer/plan_외주_개발_문제_해결_20260309.json
```

## 파이프라인

```
Step 1: 데이터 로드 (N개 기획 JSON + 가이드)
Step 2: 우선순위 산출 (규칙 기반, LLM 없음)
Step 3: 발행일 배정 (12건 선발 + 월·수·금 배치)
Step 4: 조립 + 대시보드 저장
```

## 우선순위 가중치

| 차원 | 가중치 | 비고 |
|------|--------|------|
| role | 0.30 | hub=10, sub=6 |
| expansion_role | 0.15 | 심화=8, 실행=7, 보완=6 |
| volume_trend | 0.30 | log 정규화 × 트렌드 보정 |
| geo_signals | 0.20 | AI overview, PAA, GEO 인용, 기발행 |
| funnel_proximity | 0.05 | funnel_criteria.md 참조 |

## 배치 규칙 (우선순위순)

1. **hub-before-subs** (강제) — hub를 해당 클러스터 첫 발행일에 배정
2. **클러스터 순환** (강제) — 같은 주 같은 클러스터 2건 이하
3. **클러스터 내 우선순위순** (강제)
4. **퍼널 교차** (optional) — 연속 동일 퍼널 회피 시도, 불가피하면 무시

## 출력

| 파일 | 경로 |
|------|------|
| 스케줄 JSON | `output/claude_content_scheduler/schedule_{월}_{날짜}.json` |
| 대시보드 HTML | `docs/{월}_wishket_{날짜}.html` |
| 인덱스 | `docs/index.html` (자동 업데이트) |

## 대시보드 탭

1. **발행 캘린더** — 날짜·퍼널·GEO·제목·검색량·우선순위
2. **클러스터 뷰** — hub→sub 트리, expansion_role 뱃지, internal_link 화살표
3. **콘텐츠 상세** — 개별 콘텐츠 상세 (기존 대시보드 패턴 확장)

## 참조 파일

| 파일 | 용도 |
|------|------|
| `core/agents/planner/publishing_schedule.md` | 발행 규칙 (12건, 월수금) |
| `core/agents/planner/funnel_criteria.md` | 퍼널 정의 |
| `core/dashboard.py` | HTML 대시보드 패턴 |
