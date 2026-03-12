# 리서치 도구 레퍼런스

모든 도구는 Bash로 호출. 프로젝트 루트에서 실행.

## A. Bash 도구 (tool_runner.py)

### 1. naver_volume — 네이버 검색량 + 연관 키워드
```bash
.venv/bin/python experiments/claude_researcher/tool_runner.py naver_volume '["키워드1", "키워드2"]'
```
- 입력: 키워드 배열 (최대 5개)
- 출력: `input_keywords` [{keyword, monthly_pc, monthly_mobile, monthly_total, competition}], `related_keywords` (볼륨 순, 최대 20개)

### 2. naver_trend — 네이버 검색 트렌드 (90일)
```bash
.venv/bin/python experiments/claude_researcher/tool_runner.py naver_trend '["키워드1", "키워드2"]'
```
- 입력: 키워드 배열 (최대 5개). 동의어 그룹: `[["앱 개발", "어플 개발"]]`
- 출력: `{키워드: {average, series: [{period, ratio}], direction}}`

### 3. google_trend — 구글 검색 트렌드 (3개월)
```bash
.venv/bin/python experiments/claude_researcher/tool_runner.py google_trend '["키워드1", "키워드2"]'
```
- 입력: 키워드 배열 (최대 5개)
- 출력: `{"trends": {키워드: {average, series, direction}}}`

### 4. naver_search — 네이버 블로그 검색
```bash
.venv/bin/python experiments/claude_researcher/tool_runner.py naver_search '["검색어", 5]'
```
- 입력: [검색어, 결과수(기본5)]
- 출력: {query, total, items: [{title, link, description, postdate, blogger_name}]}

### 5. autocomplete — 자동완성 제안
```bash
.venv/bin/python experiments/claude_researcher/tool_runner.py autocomplete '"키워드"'
```
- 출력: {keyword, naver: [...], google: [...]}

### 6. geo_chatgpt — ChatGPT AI 검색 인용
```bash
.venv/bin/python experiments/claude_researcher/tool_runner.py geo_chatgpt '"쿼리 문자열"'
```
- 출력: {query, answer, citations: [url], citation_details: [{url, title, context_snippet}]}

### 7. geo_claude — Claude API 웹검색 인용
```bash
.venv/bin/python experiments/claude_researcher/tool_runner.py geo_claude '"쿼리 문자열"'
```
- 출력: 동일 형식 (API 기반, 브라우저 불필요)

### 8. geo_gemini — Gemini AI 인용
```bash
.venv/bin/python experiments/claude_researcher/tool_runner.py geo_gemini '"쿼리 문자열"'
```
- 출력: 동일 형식 (Google Gemini API + Search grounding, 브라우저 불필요)

### 9. web_fetch — 페이지 크롤링 (fallback)
```bash
.venv/bin/python experiments/claude_researcher/tool_runner.py web_fetch '"https://example.com"'
```
- 출력: URL, H2 구조, 발행일, 글자 수, 본문 앞 5000자
- 네이버 블로그 자동 모바일 URL 변환, 커스텀 User-Agent
- WebFetch가 차단될 때 fallback으로 사용

### 10. naver_serp — 네이버 SERP 피처 감지
```bash
.venv/bin/python experiments/claude_researcher/tool_runner.py naver_serp '"키워드"'
```
- 출력: {keyword, knowledge_snippet: bool, smart_block: bool, smart_block_components: [...]}
- OpenAI web_search_preview 기반 (차단 없음)

## B. 내장 도구

| 용도 | 도구 | 비고 |
|------|------|------|
| 구글 웹 검색 | WebSearch | 경쟁 콘텐츠 발견, PAA 수집 |
| 페이지 크롤링 | WebFetch | H2 구조, 본문 요약 추출 |
| AI 개요 확인 | WebSearch | AI Overview/Featured Snippet 존재 확인 |

## 호출 규칙

- **배치 제한**: naver_volume, naver_trend, google_trend → 한 번에 최대 5개
- **병렬 호출**: 독립적인 도구는 여러 Bash를 동시에 호출
- **Google Trends 429**: 1회 발생 시 **이후 모든 google_trend 호출 건너뛰기** (재시도 70초 낭비 방지)
- **WebFetch 실패 시**: tool_runner web_fetch로 재시도 → 그래도 실패하면 h2_headings: []
- **GEO 도구**: 3개 모두 API 기반 → 전부 병렬 호출 가능
- **naver_serp**: 반드시 tool_runner로 수집 (m.search.naver.com 직접 크롤링 금지)
