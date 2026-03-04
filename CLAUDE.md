# Wishket Blog Content Automation Pipeline

위시켓 블로그 주 3회 발행 자동화. OpenAI API 기반 에이전트 파이프라인.

## 참조 문서

- `context/AX_project_wishket.pdf` — 6단계 워크플로, TCA 상세
- `context/content_automation_framework.pdf` — 자산 프레임워크, 확장 전략, 전체 디렉토리 설계

## PoC 스코프 (4주)

1. 월간 키워드 전략 자동 생성
2. 주 3회 콘텐츠 자동 생성
3. SEO/GEO 자동 검수
4. 뉴스레터 자동 변환

## 아키텍처

```
[Slack] ←→ [오케스트레이터] ←→ [Google Workspace]
                  │
    ┌─────────────┼─────────────┐
    ▼             ▼             ▼
[리서처→플래너] [콘텐츠 라이터] [SEO 검수기]
                                │
                          [뉴스레터 변환기]
```

### 리서처 → 플래너 2단계 파이프라인

```
입력 (질문 의도 + 질문 형태 + 콘텐츠 방향성)
    │
    ▼
┌──────────────────────────────────────────────┐
│  리서처 (researcher) — 3-Phase 파이프라인 (v3)  │
│                                              │
│  Phase 1: 키워드 분해+확장 (LLM 1회)          │
│    → 15~25개 키워드 + 도구 호출 계획           │
│                                              │
│  Phase 2: 데이터 수집                         │
│    2a: asyncio.gather 병렬 도구 실행           │
│        (트렌드·검색량·검색·자동완성 동시)        │
│    2b: URL 선별 (LLM 1회)                     │
│    2c: asyncio.gather 병렬 크롤링 (500자)      │
│                                              │
│  Phase 3: 키워드 키 직접 매칭으로 조립          │
│    → KeywordSearchData JSON 조립              │
│                                              │
│  도구: 8가지 (트렌드·검색·크롤링)               │
│  출력: KeywordSearchData                      │
└──────────────┬───────────────────────────────┘
               ▼
┌──────────────────────────────────────────────┐
│  플래너 (planner)                              │
│  목표: 원시 데이터로 콘텐츠 전략 수립             │
│  도구: web_fetch (기존 글 확인용)                │
│  출력: ContentPlan                             │
└──────────────────────────────────────────────┘
```

- **리서처**: 3-Phase 결정론적 파이프라인. LLM 2회 고정 (Phase 1 + Phase 2b), Phase 3은 룰 베이스. 키워드 중심. 태깅·판단 안 함.
- **플래너**: 퍼널/GEO 태깅, 위시켓 기존 블로그 중복 분석, 질문 선별, 전략 수립.

## 파일 구조

```
core/
  agents/
    researcher.py          # 리서처 에이전트 (3-Phase v3 키워드 중심)
    planner.py             # 플래너 에이전트 (전략 특화)
  prompts/v1/
    researcher.md          # 리서처 시스템 프롬프트 (레거시)
    planner.md             # 플래너 시스템 프롬프트
  prompts/v2/
    researcher_phase1.md   # Phase 1: 쿼리 확장 (v2, 보존)
    researcher_phase2_curation.md  # Phase 2b: URL 선별
    researcher_phase3.md   # Phase 3: 데이터 합성 (v2, 보존)
    CHANGELOG.md           # v1→v2 변경 이력
  prompts/v3/
    researcher_phase1.md   # Phase 1: 키워드 분해+확장 (현재)
    researcher_phase2_curation.md  # Phase 2b: URL 선별
    CHANGELOG.md           # v2→v3 변경 이력
  schemas.py               # 공용 Pydantic 스키마
  tests/
    test_researcher.py
    test_planner.py
  tools/
    naver_datalab.py       # 네이버 DataLab 트렌드
    google_trends.py       # Google Trends
    naver_search.py        # 네이버 블로그 검색
    google_search.py       # 구글 웹 검색 (OpenAI web_search_preview)
    web_fetch.py           # 개별 페이지 크롤링
```

## 스키마 구조

```
리서처 출력 (v3):  KeywordSearchData
                    ├── keywords: list[KeywordData]           # 15~25개 키워드 중심 데이터
                    │     ├── keyword, source_questions
                    │     ├── naver_trend, google_trend, combined_trend
                    │     ├── monthly_volume
                    │     ├── related_keywords, autocomplete_suggestions
                    │     └── naver_trend_direction, google_trend_direction
                    ├── page_analyses: list[PageAnalysis]     # 크롤링된 경쟁 콘텐츠
                    ├── search_results: list[dict]            # source 태깅된 통합 검색 결과
                    └── raw_naver_trends, raw_google_trends, ...  # 원본 보존

리서처 출력 (v2, 보존):  RawSearchData
                          └── expanded_queries: list[SearchQuery]

플래너 출력:  ContentPlan
               ├── selected_questions: list[PlannedQuestion]  # 선별 5~10개 + 퍼널/GEO 태그
               ├── dropped_questions: list[str]               # 탈락 사유
               ├── duplication_checks: list[DuplicationCheck] # 위시켓 기존 글 대조
               ├── competitor_analysis: list[PageAnalysis]
               └── recommendations: str

레거시(제거 예정): KeywordResearchResult, IntentQuestion, CompetitorContent
```

## 도구 (tools)

| 도구 | 역할 | API |
|------|------|-----|
| naver_keyword_trend | 네이버 검색 트렌드 (동의어 그룹 지원) | 네이버 DataLab |
| google_keyword_trend | 구글 검색 트렌드 | Google Trends (pytrends) |
| naver_blog_search | 네이버 블로그 검색 | 네이버 검색 API |
| google_search | 구글 웹 검색 | OpenAI web_search_preview |
| web_fetch | 개별 페이지 크롤링 (네이버 모바일 변환 포함) | 직접 HTTP |

## 에이전트 규칙

- 단일 책임. 리서처는 검색만, 플래너는 판단만.
- 실패 시 오케스트레이터가 최대 2회 재시도 후 Slack 알림.
- 출력은 `output/`에 저장 후 다음 단계로 전달.

## 품질 게이트

| 단계 | 합격 | 불합격 시 |
|------|------|----------|
| 리서처 | keywords 5개+, page_analyses 3개+ | 재실행 |
| 플래너 | 퍼널 균형, 중복 위험 < 0.3 | Slack → 사용자 재조정 |
| 콘텐츠 초안 | SEO >= 70, 키워드 밀도 1~3%, H2 3개+ | 재생성 (최대 2회) |
| 최종 검수 | 톤앤매너, CTA 포함, 유사도 < 0.7 | Slack → 사람 검토 |

## 환경변수

```
OPENAI_API_KEY         # OpenAI API (에이전트 + 구글 검색)
NAVER_CLIENT_ID        # 네이버 Open API
NAVER_CLIENT_SECRET    # 네이버 Open API
```

## 개발 컨벤션

- snake_case 전체 (Python, YAML, md)
- type hints 필수
- 에이전트 try-except → 오케스트레이터 재시도
- 파이프라인 단계별 구조화 로그
- `.env`에 시크릿, git 추적 안 함
- 프롬프트 변경 시 새 버전 디렉토리(v2/) + CHANGELOG.md 기록
- 클라이언트 고유 지시는 `clients/{name}/brand_profile.yaml`, 범용은 `core/prompts/`
