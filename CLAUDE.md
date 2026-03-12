# Wishket Blog Content Automation Pipeline

위시켓 블로그 주 3회 발행 자동화. Claude Code CLI 에이전트 파이프라인.

## 참조 문서

- `context/AX_project_wishket.pdf` — 6단계 워크플로, TCA 상세
- `context/content_automation_framework.pdf` — 자산 프레임워크, 확장 전략, 전체 디렉토리 설계

## 아키텍처

```
[Slack] ←→ [content-strategist 오케스트레이터]
                │
    ┌───────────┼───────────┐
    ▼           ▼           ▼
[@researcher] [@content-designer] [@content-planner]
    │                              │
    ▼                              ▼
[cli/tool_runner.py]         [검증 게이트 + 피드백 루프]
       │
       ▼
[core/tools/*]
```

### 파이프라인 흐름

```
입력 (질문 의도 + 질문 형태 + 콘텐츠 방향성)
    │
    ▼
┌──────────────────────────────────────────────┐
│  @researcher — 시드 + 팬아웃 리서치            │
│  도구: cli/tool_runner.py + WebSearch/WebFetch │
│  출력: output/claude_researcher/seed_*.json    │
└──────────────┬───────────────────────────────┘
               ▼
┌──────────────────────────────────────────────┐
│  @content-designer — 콘텐츠 기획 설계          │
│  가이드: guides/*.md                          │
│  출력: output/claude_content_designer/plan_*.json │
└──────────────┬───────────────────────────────┘
               ▼
┌──────────────────────────────────────────────┐
│  @content-planner — 월간 스케줄 + 대시보드       │
│  출력: output/claude_content_scheduler/schedule_*.json │
│        docs/*.html (대시보드)                  │
└──────────────┬───────────────────────────────┘
               ▼
┌──────────────────────────────────────────────┐
│  검증 게이트 + 피드백 루프                      │
│  validator.py — 에이전트 출력 구조 검증          │
│  feedback.py — GitHub Issues 피드백 수집/처리    │
└──────────────────────────────────────────────┘
```

## 파일 구조

```
content-automation-framework/
├── .claude/agents/                    # CLI 에이전트 정의
│   ├── content_strategist/            # 오케스트레이터 (통합)
│   │   ├── agent.md                   # 에이전트 정의 (7-Phase)
│   │   ├── orchestrator.py            # 파이프라인 실행 로직
│   │   ├── state.py                   # 세션 상태 관리
│   │   ├── validator.py               # 검증 게이트
│   │   ├── feedback.py                # GitHub Issues 피드백 루프
│   │   └── formatter.py               # Slack Block Kit 메시지
│   ├── researcher.md                  # 서브 에이전트
│   ├── content-designer.md            # 서브 에이전트
│   └── content-planner.md             # 서브 에이전트 (구 content-scheduler)
├── cli/                               # CLI 도구 (tool_runner, assembler)
│   ├── tool_runner.py                 # 도구 CLI 디스패처
│   ├── assembler.py                   # 리서처 출력 JSON 조립기
│   ├── tools_reference.md             # 도구 호출 레퍼런스
│   └── output_schema.json             # 리서처 출력 스키마
├── core/
│   ├── tools/                         # 도구 구현체 (17개)
│   └── dashboard.py                   # 대시보드 생성 유틸 (피드백 링크 포함)
├── guides/                            # 콘텐츠 전략 가이드
│   ├── funnel_criteria.md
│   ├── geo_classification.md
│   ├── content_direction.md
│   └── publishing_schedule.md
├── interfaces/slack/                  # Slack thin wrapper
│   ├── app.py                         # 진입점
│   ├── handlers.py                    # 이벤트 → strategist 위임
│   ├── config.py                      # Slack 설정
│   ├── parser.py                      # 입력 파싱
│   └── scheduler.py                   # APScheduler
├── clients/wishket/                   # 클라이언트 프로필
├── data/                              # 기발행 DB
├── output/                            # 실행 결과
├── _archive/                          # 아카이브 (레거시 core 파이프라인)
│   ├── core_agents/
│   ├── core_tests/
│   └── scripts/
└── .env
```

## 도구 (core/tools/)

| 도구 | 역할 | CLI 이름 |
|------|------|---------|
| naver_datalab.py | 네이버 검색 트렌드 | naver_trend |
| google_trends.py | 구글 검색 트렌드 | google_trend |
| naver_search.py | 네이버 블로그 검색 | naver_search |
| naver_searchad.py | 네이버 키워드 볼륨 | naver_volume |
| naver_serp.py | 네이버 SERP 피처 | naver_serp |
| autocomplete.py | 자동완성 제안 | autocomplete |
| web_fetch.py | 페이지 크롤링 | web_fetch |
| ai_search.py | ChatGPT GEO 인용 | geo_chatgpt |
| claude_search.py | Claude GEO 인용 | geo_claude |
| gemini_search.py | Gemini GEO 인용 | geo_gemini |

도구 호출: `.venv/bin/python cli/tool_runner.py <도구이름> '<JSON인자>'`

## 환경변수

```
OPENAI_API_KEY         # OpenAI API (구글 검색, AI 검색)
NAVER_CLIENT_ID        # 네이버 Open API
NAVER_CLIENT_SECRET    # 네이버 Open API
NAVER_AD_API_KEY       # 네이버 검색광고 API
NAVER_AD_API_SECRET    # 네이버 검색광고 API
NAVER_AD_CUSTOMER_ID   # 네이버 검색광고 API
```

## 개발 컨벤션

- snake_case 전체 (Python, YAML, md)
- type hints 필수
- `.env`에 시크릿, git 추적 안 함
- 클라이언트 고유 지시는 `clients/{name}/brand_profile.yaml`
- 가이드 문서는 `guides/`에 배치
- CLI 에이전트 정의는 `.claude/agents/`에 배치
