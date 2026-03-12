"""사용자 입력 텍스트 파싱 및 검증."""

from __future__ import annotations

import calendar as cal_mod
import datetime as dt_mod
import re
from dataclasses import dataclass, field
from datetime import date

INTENT_OPTIONS = ["정보 탐색", "비교 판단", "추천"]
DIRECTION_OPTIONS = ["카테고리 포지셔닝", "문제 인식 확산", "판단 기준 제시", "실행 가이드"]


@dataclass
class PipelineParams:
    intent: str
    questions: list[str]
    content_direction: str
    target_month: str  # "2026-04"
    question_tags: list[dict] = field(default_factory=list)  # 질문별 태깅 [{question, intent, direction}]
    pipeline_mode: str = "basic"  # "basic" | "full"


@dataclass
class ParseError:
    message: str  # 슬랙에 그대로 노출할 안내 문구


def parse(text: str) -> PipelineParams | ParseError:
    """사용자 입력 텍스트 → PipelineParams. 검증 실패 시 ParseError."""
    lines = [line.strip() for line in text.strip().splitlines()]

    intent = ""
    content_direction = ""
    target_month = ""
    questions: list[str] = []
    in_questions = False

    for line in lines:
        if not line:
            continue
        if line.startswith("질문 의도"):
            m = re.search(r":\s*(.+)", line)
            intent = m.group(1).strip() if m else ""
            in_questions = False
        elif line.startswith("콘텐츠 방향성"):
            m = re.search(r":\s*(.+)", line)
            content_direction = m.group(1).strip() if m else ""
            in_questions = False
        elif line.startswith("발행 월"):
            m = re.search(r":\s*(\d{4}-\d{2})", line)
            target_month = m.group(1).strip() if m else ""
            in_questions = False
        elif line == "질문 형태":
            in_questions = True
        elif in_questions:
            questions.append(line)

    # ── 검증 ──────────────────────────────────────────────────────
    if not intent:
        return ParseError(_error("'질문 의도'를 찾을 수 없습니다."))
    if intent not in INTENT_OPTIONS:
        return ParseError(_error(
            f"'질문 의도' 값이 올바르지 않습니다. (입력: *{intent}*)\n"
            f"허용값: {' / '.join(INTENT_OPTIONS)}"
        ))
    if not questions:
        return ParseError(_error(
            "'질문 형태' 아래에 질문이 없습니다. 1개 이상 입력해 주세요."
        ))
    if not content_direction:
        return ParseError(_error("'콘텐츠 방향성'을 찾을 수 없습니다."))
    if content_direction not in DIRECTION_OPTIONS:
        return ParseError(_error(
            f"'콘텐츠 방향성' 값이 올바르지 않습니다. (입력: *{content_direction}*)\n"
            f"허용값: {' / '.join(DIRECTION_OPTIONS)}"
        ))

    # 기존 텍스트 파싱: 전역 intent/direction을 각 질문에 배정
    question_tags = [
        {"question": q, "intent": intent, "direction": content_direction}
        for q in questions
    ]

    return PipelineParams(
        intent=intent,
        questions=questions,
        content_direction=content_direction,
        target_month=target_month or _next_month(),
        question_tags=question_tags,
    )


def parse_json(data: dict) -> PipelineParams | ParseError:
    """JSON 입력 → PipelineParams. 질문별 intent/direction 지원.

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
        return ParseError(_error("'questions' 배열이 비어 있습니다."))

    question_tags: list[dict] = []
    questions: list[str] = []

    for item in questions_raw:
        q = item.get("question", "").strip()
        if not q:
            continue
        q_intent = item.get("intent", "").strip()
        q_direction = item.get("direction", "").strip()

        if q_intent and q_intent not in INTENT_OPTIONS:
            return ParseError(_error(
                f"'intent' 값이 올바르지 않습니다. (입력: *{q_intent}*)\n"
                f"허용값: {' / '.join(INTENT_OPTIONS)}"
            ))
        if q_direction and q_direction not in DIRECTION_OPTIONS:
            return ParseError(_error(
                f"'direction' 값이 올바르지 않습니다. (입력: *{q_direction}*)\n"
                f"허용값: {' / '.join(DIRECTION_OPTIONS)}"
            ))

        questions.append(q)
        question_tags.append({
            "question": q,
            "intent": q_intent,
            "direction": q_direction,
        })

    if not questions:
        return ParseError(_error("유효한 질문이 없습니다."))

    # 레거시 호환: 첫 번째 질문의 값을 전역으로 사용
    first_intent = question_tags[0]["intent"]
    first_direction = question_tags[0]["direction"]

    if not first_intent:
        return ParseError(_error("첫 번째 질문의 'intent'가 비어 있습니다."))
    if not first_direction:
        return ParseError(_error("첫 번째 질문의 'direction'이 비어 있습니다."))

    target_month = data.get("target_month", "").strip()

    return PipelineParams(
        intent=first_intent,
        questions=questions,
        content_direction=first_direction,
        target_month=target_month or _next_month(),
        question_tags=question_tags,
    )


# ── 내부 헬퍼 ─────────────────────────────────────────────────────


def _next_month() -> str:
    today = date.today()
    last_day = cal_mod.monthrange(today.year, today.month)[1]
    next_first = date(today.year, today.month, last_day) + dt_mod.timedelta(days=1)
    return next_first.strftime("%Y-%m")


def _error(detail: str) -> str:
    return (
        f":x: 입력 오류: {detail}\n\n"
        "*올바른 형식:*\n"
        "```\n"
        f"질문 의도 : {' / '.join(INTENT_OPTIONS)}\n"
        "질문 형태\n"
        "질문을 한 줄씩 입력하세요.\n"
        f"콘텐츠 방향성 : {' / '.join(DIRECTION_OPTIONS)}\n"
        "발행 월 : YYYY-MM  (선택, 기본값: 다음 달)\n"
        "```"
    )
