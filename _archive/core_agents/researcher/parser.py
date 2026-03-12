"""입력 파싱 및 텍스트 유틸리티."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from core.schemas import ParsedInput, SeedQuestion

# ── 불용어 ────────────────────────────────────────────────────────

_STOPWORDS: set[str] = {
    # 한국어 조사·접속사·대명사
    "의", "가", "이", "은", "는", "을", "를", "에", "와", "과",
    "도", "로", "으로", "에서", "까지", "부터", "보다", "처럼",
    "만큼", "및", "또는", "그리고", "하지만", "그러나", "때문에",
    "위해", "통해", "대해", "관련", "따라", "있는", "없는", "하는",
    "되는", "같은", "다른", "모든", "각", "어떤", "이런", "그런",
    "것", "수", "등", "더", "또", "즉", "단", "중", "후", "전",
    "위", "아래", "안", "밖", "곳", "때", "뒤", "점",
    # 영어
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "as", "into",
    "through", "during", "before", "after", "about", "between",
    "and", "but", "or", "not", "so", "very", "it", "its", "this",
    "that", "these", "those", "what", "which", "who", "how", "when",
    "where", "why", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "no", "only", "own", "same", "than",
    "too", "just", "also", "out", "over", "up",
}

# ── 한국어 키워드 추출 유틸리티 ────────────────────────────────────

# 조사 접미사 (길이 역순 — 긴 것부터 매칭)
_PARTICLES: list[str] = [
    "에서는", "으로는", "이라는",
    "라는", "에서", "으로", "에게", "까지", "부터", "처럼", "만큼", "마다",
    "을", "를", "이", "가", "은", "는", "의", "에", "로", "과", "와", "도",
]

# 부사/지시어 불용어 (키워드 추출 시 필터링)
_ADVERB_STOPS: set[str] = {
    "가장", "매우", "자주", "어떻게", "어떤", "어떻다", "무엇",
    "얼마나", "왜", "언제", "어디", "정말", "아주", "상당히",
}

# 동사/형용사 어미 패턴 (토큰 끝)
_VERB_SUFFIX_RE = re.compile(
    r"(하는|해야|할|하게|봐야|있는|없는|되는|같은|인가요|인지|한다|된다|하다|해야|되어야)$"
)

# 동사/형용사 활용형 (조사 strip 전 원형) — 질문에서 흔히 나타나는 관형형
# _VERB_SUFFIX_RE가 못 잡는 패턴을 보완
_VERBAL_FORMS: set[str] = {
    # ㄹ-불규칙 동사 -(으)ㄹ 관형형 (고르다→고를 등 — 조사 strip 오탐 방지)
    "고를", "따를", "부를", "누를", "모를", "자를", "흐를",
    "오를", "이를", "다를", "바를",
    # 규칙 동사 -(으)ㄹ 관형형
    "잡을", "받을", "넣을", "얻을", "찾을", "만들", "나눌",
    "맡길", "있을", "없을", "나올", "들을", "정할", "택할",
    # 단음절 -(으)ㄹ 관형형
    "볼", "갈", "올", "줄", "쓸", "알", "풀", "낼", "열", "걸",
    # 형용사 -(으)ㄴ 관형형 (_STOPWORDS 미포함 항목)
    "좋은", "나쁜", "많은", "적은", "높은", "낮은", "큰", "작은",
    "중요한", "필요한", "다양한", "적절한", "새로운", "어려운", "쉬운",
    "빠른", "느린",
}

# 질문 어미 패턴 (문장 끝에서 제거)
_QUESTION_ENDING_RE = re.compile(
    r"\s*(은|는)?\s*"
    r"(무엇인가요|무엇일까요|뭔가요|뭘까요|어떻게\s+\S+\s*(수\s+)?있나요"
    r"|어떤가요|어떨까요|어떤 것인가요|알려주세요|설명해주세요"
    r"|인가요|일까요|하나요|할까요|될까요|볼까요)\s*\??\s*$"
)

# 절 분리 패턴
_CLAUSE_SPLIT_RE = re.compile(r"\s+때\s+|이며|으며|,\s*")


def _strip_particle(token: str) -> str:
    """토큰 끝에서 가장 긴 조사를 제거. 잔여 길이 1자 이상 보존."""
    for p in _PARTICLES:
        if token.endswith(p) and len(token) - len(p) >= 1:
            return token[: -len(p)]
    return token


def _is_verb_or_stop(token: str) -> bool:
    """동사/부사/불용어/활용형 여부 판별."""
    if token in _STOPWORDS or token in _ADVERB_STOPS:
        return True
    if token in _VERBAL_FORMS:
        return True
    if _VERB_SUFFIX_RE.search(token):
        return True
    return False


def _extract_keywords_from_question(question: str) -> list[str]:
    """질문 문자열에서 SEO 시드 키워드를 규칙 기반으로 추출.

    1. 질문 어미 제거
    2. 절 분리
    3. 각 절: 토큰화 → 조사 strip → 동사/부사/불용어 필터
    4. 연속 명사 토큰 스팬 → n-gram 생성 (4→2, 단일 포함)
    5. 중복 제거 후 반환
    """
    text = question.strip()
    if not text:
        return []

    # 1. 질문 어미 제거
    text = _QUESTION_ENDING_RE.sub("", text)

    # 2. 절 분리
    clauses = _CLAUSE_SPLIT_RE.split(text)

    seen: dict[str, None] = {}  # 순서 유지 중복 제거

    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue

        # 3. 토큰화 → 조사 strip → 필터
        raw_tokens = clause.split()
        noun_spans: list[list[str]] = []
        current_span: list[str] = []

        for raw in raw_tokens:
            raw = raw.rstrip("?!.;:")
            if not raw:
                continue
            stripped = _strip_particle(raw)
            if _is_verb_or_stop(stripped) or _is_verb_or_stop(raw):
                if current_span:
                    noun_spans.append(current_span)
                    current_span = []
            else:
                if stripped:
                    current_span.append(stripped)

        if current_span:
            noun_spans.append(current_span)

        # 4-5. 각 스팬에서 n-gram 생성
        for span in noun_spans:
            max_n = min(len(span), 4)
            for n in range(max_n, 0, -1):
                for start in range(len(span) - n + 1):
                    gram = " ".join(span[start : start + n])
                    if gram not in seen:
                        seen[gram] = None

    return list(seen.keys())


# ── 유틸리티 함수 ────────────────────────────────────────────────


def _chunk(items: list, size: int) -> list[list]:
    """리스트를 size 단위로 분할."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _normalize_keyword(kw: str) -> str:
    return re.sub(r"\s+", " ", kw.strip().lower())


def _extract_items_from_serp(raw_json: str) -> list[dict]:
    """google_search JSON 결과에서 items 추출."""
    try:
        data = json.loads(raw_json)
        return data.get("items", [])
    except (json.JSONDecodeError, TypeError):
        return []


def _extract_serp_features(raw_json: str) -> dict:
    """google_search JSON 결과에서 serp_features 추출."""
    try:
        data = json.loads(raw_json)
        return data.get("serp_features", {})
    except (json.JSONDecodeError, TypeError):
        return {}


def _keyword_to_question(keyword: str) -> str:
    """키워드를 질문 형태로 변환 (휴리스틱)."""
    kw = keyword.strip()
    if kw.endswith("?") or kw.endswith("란") or kw.endswith("요"):
        return kw
    words = kw.split()
    if len(words) <= 2:
        return f"{kw}란?"
    return f"{kw}에 대해 알려주세요"


def _extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _extract_pplx_context(answer: str, cite_num: int, max_len: int = 120) -> str:
    """Perplexity 답변에서 [N] 인용 번호 주변 문맥을 추출한다."""
    marker = f"[{cite_num}]"
    pos = answer.find(marker)
    if pos < 0:
        return ""
    # 인용 마커 앞뒤 문장을 추출
    start = max(0, answer.rfind(".", 0, pos) + 1)
    end = answer.find(".", pos + len(marker))
    if end < 0:
        end = min(len(answer), pos + max_len)
    else:
        end += 1
    snippet = answer[start:end].strip()
    return snippet[:max_len]


def _is_relevant_serp_item(keyword: str, item: dict) -> bool:
    """검색 결과가 키워드와 관련 있는지 토큰 겹침으로 판단한다.

    OpenAI web_search_preview가 한국어 키워드를 잘못 해석하여
    무관한 결과(hallucination)를 반환하는 문제를 사전 필터링한다.
    """
    title = (item.get("title") or "").lower()
    snippet = (item.get("snippet") or "").lower()
    haystack = f"{title} {snippet}"

    kw_lower = keyword.lower().strip()

    # 키워드 전체가 포함되면 관련
    if kw_lower in haystack:
        return True

    tokens = kw_lower.split()

    if len(tokens) > 1:
        # 멀티워드: 아무 토큰이나 포함되면 관련
        return any(tok in haystack for tok in tokens)

    # 단일 단어
    word = tokens[0]

    # ASCII 단어(영문) → 전체 매칭
    if word.isascii():
        return word in haystack

    # 한국어 복합어 → 2글자 n-gram 중 하나라도 포함
    if len(word) >= 2:
        return any(word[i:i + 2] in haystack for i in range(len(word) - 1))

    return True


def _map_content_type(google_type: str) -> str:
    """google_search content_type → 스키마 CONTENT_TYPE."""
    mapping = {
        "blog": "corporate_blog",
        "news": "media",
        "video": "other",
        "wiki": "other",
        "government": "official_docs",
        "community": "community",
        "website": "other",
    }
    return mapping.get(google_type, "other")


def _parse_web_fetch_result(text: str) -> dict:
    """web_fetch 반환 문자열 → {url, h2_structure, char_count, body, publish_date}."""
    result: dict = {
        "url": "",
        "h2_structure": [],
        "char_count": 0,
        "body": "",
        "publish_date": None,
    }
    if not text:
        return result

    lines = text.split("\n")
    body_start = -1
    for i, line in enumerate(lines):
        if line.startswith("URL: "):
            result["url"] = line[len("URL: "):]
        elif line.startswith("H2 구조: "):
            try:
                result["h2_structure"] = json.loads(line[len("H2 구조: "):])
            except json.JSONDecodeError:
                pass
        elif line.startswith("발행일: "):
            val = line[len("발행일: "):].strip()
            if val:
                result["publish_date"] = val
        elif line.startswith("글자 수: "):
            try:
                result["char_count"] = int(line[len("글자 수: "):].strip())
            except ValueError:
                pass
        elif line.startswith("본문"):
            body_start = i + 1
            break

    if 0 < body_start < len(lines):
        result["body"] = "\n".join(lines[body_start:])
    return result


def parse_json_input(data: dict) -> ParsedInput:
    """JSON 입력 → ParsedInput. 질문별 intent/direction 지원.

    입력:
        {
            "questions": [
                {"question": "...", "intent": "비교 판단", "direction": "판단 기준 제시"},
                ...
            ],
            "target_month": "2026-04"
        }
    """
    questions_raw = data.get("questions", [])
    if not questions_raw:
        return ParsedInput(main_keyword="", entry_moment="general")

    questions: list[str] = []
    seed_questions: list[SeedQuestion] = []
    all_seeds: list[str] = []

    for i, q_item in enumerate(questions_raw):
        q_text = q_item.get("question", "").strip()
        if not q_text:
            continue
        q_intent = q_item.get("intent", "")
        q_direction = q_item.get("direction", "")

        questions.append(q_text)
        seed_questions.append(SeedQuestion(
            seed_id=f"sq{i + 1:03d}",
            question=q_text,
            intent=[q_intent] if q_intent else [],
            content_direction=[q_direction] if q_direction else [],
        ))
        all_seeds.extend(_extract_keywords_from_question(q_text))

    # 중복 제거 (순서 유지)
    seeds = list(dict.fromkeys(all_seeds))

    # main_keyword 선정: 2~3 토큰 구 우선, 빈도 기준
    main_kw = ""
    if seeds:
        multi_token = [s for s in seeds if len(s.split()) >= 2]
        if multi_token:
            def _freq(seed: str) -> int:
                return sum(1 for q in questions if seed in q)
            main_kw = max(multi_token, key=lambda s: (_freq(s), -len(s)))
        else:
            main_kw = seeds[0]

    # 레거시 호환: 첫 번째 질문의 intent/direction
    first_intent = seed_questions[0].intent[0] if seed_questions and seed_questions[0].intent else ""
    first_direction = seed_questions[0].content_direction[0] if seed_questions and seed_questions[0].content_direction else ""

    return ParsedInput(
        main_keyword=main_kw,
        entry_moment=first_intent or "general",
        intent=first_intent,
        questions=questions,
        direction=first_direction,
        extracted_seeds=seeds,
        seed_questions=seed_questions,
    )


def parse_input(text: str) -> ParsedInput:
    """자연어 입력 → ParsedInput.

    파싱 우선순위:
    1. 구조화 포맷: '질문 의도 :' + 질문 블록 + '콘텐츠 방향성 :'
    2. 라벨 형식: '키워드: X, 모먼트: Y'
    3. 쉼표/줄바꿈 구분 2파트
    4. 폴백: 전체를 keyword로
    """
    text = text.strip()
    if not text:
        return ParsedInput(main_keyword="", entry_moment="general")

    # ── 구조화 포맷 감지 ──
    m_struct = re.search(
        r"질문\s*의도\s*[:：]\s*(.+?)(?:\n|$)", text,
    )
    m_direction = re.search(
        r"콘텐츠\s*방향성\s*[:：]\s*(.+?)(?:\n|$)", text,
    )
    if m_struct:
        intent = m_struct.group(1).strip()
        direction = m_direction.group(1).strip() if m_direction else ""

        # 질문 블록 추출: '질문 형태' 라벨 이후 ~ '콘텐츠 방향성' 이전
        q_block_match = re.search(
            r"질문\s*형태\s*[:：]?\s*\n(.*?)(?=콘텐츠\s*방향성|$)",
            text,
            re.DOTALL,
        )
        questions: list[str] = []
        if q_block_match:
            for line in q_block_match.group(1).split("\n"):
                line = line.strip().lstrip("0123456789.-) ")
                if line and "?" in line:
                    questions.append(line)

        # 각 질문에서 키워드 추출
        all_seeds: list[str] = []
        for q in questions:
            all_seeds.extend(_extract_keywords_from_question(q))

        # 중복 제거 (순서 유지)
        seeds = list(dict.fromkeys(all_seeds))

        # main_keyword 선정: 가장 많은 질문에 등장하는 2~3토큰 구 우선
        main_kw = ""
        if seeds:
            # 2~3 토큰 구 후보 우선
            multi_token = [s for s in seeds if len(s.split()) >= 2]
            if multi_token:
                # 질문 등장 빈도 기준 정렬
                def _freq(seed: str) -> int:
                    return sum(1 for q in questions if seed in q)

                main_kw = max(multi_token, key=lambda s: (_freq(s), -len(s)))
            else:
                main_kw = seeds[0]

        seed_questions = [
            SeedQuestion(
                seed_id=f"sq{i + 1:03d}",
                question=q,
                intent=[intent] if intent else [],
                content_direction=[direction] if direction else [],
            )
            for i, q in enumerate(questions)
        ]

        return ParsedInput(
            main_keyword=main_kw,
            entry_moment=intent,
            intent=intent,
            questions=questions,
            direction=direction,
            extracted_seeds=seeds,
            seed_questions=seed_questions,
        )

    # ── 기존 포맷 폴백 ──

    # 패턴 1: 라벨 형식 "키워드: X, 모먼트: Y"
    m = re.match(
        r"키워드\s*[:：]\s*(.+?)\s*[,，]\s*모먼트\s*[:：]\s*(.+)",
        text,
    )
    if m:
        return ParsedInput(
            main_keyword=m.group(1).strip(),
            entry_moment=m.group(2).strip(),
        )

    # 영문 라벨
    m = re.match(
        r"keyword\s*[:：]\s*(.+?)\s*[,，]\s*moment\s*[:：]\s*(.+)",
        text,
        re.IGNORECASE,
    )
    if m:
        return ParsedInput(
            main_keyword=m.group(1).strip(),
            entry_moment=m.group(2).strip(),
        )

    # 패턴 2: 쉼표/줄바꿈 구분 2파트
    parts = re.split(r"[,，\n]", text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return ParsedInput(
            main_keyword=parts[0].strip(),
            entry_moment=parts[1].strip(),
        )

    # 폴백: 전체를 keyword로
    return ParsedInput(main_keyword=text, entry_moment="general")
