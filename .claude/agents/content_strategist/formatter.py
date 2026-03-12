"""슬랙 Block Kit 메시지 조립."""

from __future__ import annotations

WEEKDAY_KO = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}
FUNNEL_KO = {
    "awareness": "인지",
    "consideration": "고려",
    "conversion": "전환",
    "unclassified": "-",
}

USAGE_HINT = (
    "아래 형식으로 입력해 주세요.\n"
    "```\n"
    "질문 의도 : 정보 탐색 | 비교 판단 | 추천\n"
    "질문 형태\n"
    "질문을 한 줄씩 입력하세요.\n"
    "콘텐츠 방향성 : 카테고리 포지셔닝 | 문제 인식 확산 | 판단 기준 제시 | 실행 가이드\n"
    "발행 월 : YYYY-MM  (선택, 기본값: 다음 달)\n"
    "```"
)


# ── 정규 요청 메시지 ──────────────────────────────────────────────


def build_input_request(target_month: str) -> list[dict]:
    """매월 첫 영업일에 봇이 채널에 게시하는 입력 요청 Block Kit."""
    year, mon = target_month.split("-")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":clipboard: *{year}년 {int(mon)}월 콘텐츠 계획 입력 요청*\n"
                    "아래 버튼을 눌러 입력해 주세요."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "open_content_modal",
                    "text": {"type": "plain_text", "text": "입력하기"},
                    "style": "primary",
                }
            ],
        },
    ]


def build_mention_reply() -> list[dict]:
    """@멘션 시 봇이 응답하는 버튼 메시지."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "콘텐츠 계획을 입력하시겠어요?",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "open_content_modal",
                    "text": {"type": "plain_text", "text": "입력하기"},
                    "style": "primary",
                }
            ],
        },
    ]


# ── 슬래시 커맨드 모달 ────────────────────────────────────────────


def build_modal(
    private_metadata: str = "",
    initial_month: str = "",
    initial_intent: str = "",
    initial_direction: str = "",
) -> dict:
    """질문 입력 모달. 질문 1개 + 의도 + 방향성 + 발행 월."""
    # 의도 셀렉트 — 이전 값 유지
    intent_options = [
        {"text": {"type": "plain_text", "text": "정보 탐색"}, "value": "정보 탐색"},
        {"text": {"type": "plain_text", "text": "비교 판단"}, "value": "비교 판단"},
        {"text": {"type": "plain_text", "text": "추천"}, "value": "추천"},
    ]
    intent_element: dict = {
        "type": "static_select",
        "action_id": "intent_select",
        "placeholder": {"type": "plain_text", "text": "선택하세요"},
        "options": intent_options,
    }
    if initial_intent:
        intent_element["initial_option"] = {
            "text": {"type": "plain_text", "text": initial_intent},
            "value": initial_intent,
        }

    # 방향성 셀렉트 — 이전 값 유지
    direction_options = [
        {"text": {"type": "plain_text", "text": "카테고리 포지셔닝"}, "value": "카테고리 포지셔닝"},
        {"text": {"type": "plain_text", "text": "문제 인식 확산"}, "value": "문제 인식 확산"},
        {"text": {"type": "plain_text", "text": "판단 기준 제시"}, "value": "판단 기준 제시"},
        {"text": {"type": "plain_text", "text": "실행 가이드"}, "value": "실행 가이드"},
    ]
    direction_element: dict = {
        "type": "static_select",
        "action_id": "direction_select",
        "placeholder": {"type": "plain_text", "text": "선택하세요"},
        "options": direction_options,
    }
    if initial_direction:
        direction_element["initial_option"] = {
            "text": {"type": "plain_text", "text": initial_direction},
            "value": initial_direction,
        }

    return {
        "type": "modal",
        "callback_id": "content_input_modal",
        "notify_on_close": True,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "콘텐츠 계획 입력"},
        "submit": {"type": "plain_text", "text": "질문 등록"},
        "close": {"type": "plain_text", "text": "입력 완료"},
        "blocks": [
            {
                "type": "input",
                "block_id": "questions_block",
                "label": {"type": "plain_text", "text": "질문"},
                "hint": {"type": "plain_text", "text": "질문은 하나만 입력해 주세요."},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "questions_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "ERP 외주 개발 업체를 고를 때 기준은?",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "intent_block",
                "label": {"type": "plain_text", "text": "질문 의도"},
                "element": intent_element,
            },
            {
                "type": "input",
                "block_id": "direction_block",
                "label": {"type": "plain_text", "text": "콘텐츠 방향성"},
                "element": direction_element,
            },
            {
                "type": "input",
                "block_id": "month_block",
                "label": {"type": "plain_text", "text": "발행 월"},
                "hint": {"type": "plain_text", "text": "비워두면 다음 달로 자동 설정"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "month_input",
                    "placeholder": {"type": "plain_text", "text": "예: 2026-04"},
                    **({"initial_value": initial_month} if initial_month else {}),
                },
            },
        ],
    }


# ── 진행 메시지 ───────────────────────────────────────────────────


def progress(icon: str, text: str) -> str:
    return f"{icon} {text}"


# ── 풀 파이프라인 세션 메시지 ─────────────────────────────────────


def pipeline_session_start() -> str:
    """세션 시작."""
    return ":mag: *전략을 위한 정보 수집을 시작합니다.*"


def pipeline_questions_collected_blocks(questions: list[str]) -> list[dict]:
    """질문 수집 완료 Block Kit. 리서치 시작 + 질문 추가 버튼."""
    q_lines = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":clipboard: *질문 {len(questions)}개 등록 완료*\n{q_lines}",
            },
        },
    ]

    elements: list[dict] = []
    if len(questions) >= 2:
        elements.append({
            "type": "button",
            "action_id": "pipeline_start_research",
            "text": {"type": "plain_text", "text": "리서치 시작"},
            "style": "primary",
        })
    elements.append({
        "type": "button",
        "action_id": "pipeline_next_question",
        "text": {"type": "plain_text", "text": "질문 추가하기"},
    })
    if len(questions) < 2:
        elements.append({
            "type": "button",
            "action_id": "pipeline_start_research",
            "text": {"type": "plain_text", "text": "리서치 시작"},
        })

    blocks.append({"type": "actions", "elements": elements})

    if len(questions) < 2:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": ":warning: 최소 2개 질문을 권장합니다."}],
        })

    return blocks


def pipeline_question_done_blocks(question_number: int, keyword: str) -> list[dict]:
    """질문 완료 Block Kit. 다음 질문 + 종료 버튼."""
    text = f":white_check_mark: 질문 {question_number} 완료 — _{keyword}_"

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]

    next_btn: dict = {
        "type": "button",
        "action_id": "pipeline_next_question",
        "text": {"type": "plain_text", "text": "다음 질문 입력하기"},
    }
    finish_btn: dict = {
        "type": "button",
        "action_id": "pipeline_finish",
        "text": {"type": "plain_text", "text": "종료하고 스케줄링"},
    }

    if question_number >= 3:
        finish_btn["style"] = "primary"
        blocks.append({"type": "actions", "elements": [finish_btn, next_btn]})
    elif question_number >= 2:
        next_btn["style"] = "primary"
        blocks.append({"type": "actions", "elements": [next_btn, finish_btn]})
    else:
        next_btn["style"] = "primary"
        blocks.append({"type": "actions", "elements": [next_btn]})

    return blocks


def pipeline_summary_blocks(
    target_month: str,
    schedule_path: str,
    dashboard_path: str,
    run_id: str,
    dashboard_url: str = "",
) -> list[dict]:
    """scheduler 완료 후 최종 요약 Block Kit."""
    year, mon = target_month.split("-")
    lines = [
        f":white_check_mark: *{year}년 {int(mon)}월 풀 파이프라인 완료*",
    ]
    if schedule_path:
        lines.append(f":calendar: 스케줄: `{schedule_path}`")
    if dashboard_path:
        lines.append(f":bar_chart: 대시보드: `{dashboard_path}`")
    if dashboard_url:
        lines.append(f":globe_with_meridians: <{dashboard_url}|대시보드 보기>")
    lines.append(f":label: Run ID: `{run_id}`")

    return [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]


# ── 신규: Phase 0 액션 플랜 ───────────────────────────────────────


def action_plan_blocks(
    questions: list[str],
    intent: str,
    direction: str,
    month: str,
) -> list[dict]:
    """Phase 0 액션 플랜 요약. 질문 목록 + 예상 단계 + 도구."""
    q_lines = "\n".join(f"  {i}. {q}" for i, q in enumerate(questions, 1))
    total_steps = len(questions) * 2 + 1  # researcher + designer per Q + planner

    text = (
        f":clipboard: *액션 플랜*\n"
        f"*의도*: {intent} | *방향성*: {direction} | *발행 월*: {month}\n\n"
        f"*질문 ({len(questions)}개)*:\n{q_lines}\n\n"
        f"*예상 단계*: {total_steps}단계 "
        f"(리서치 {len(questions)} + 설계 {len(questions)} + 플래닝 1)\n"
        f"*도구*: researcher, content-designer, content-planner"
    )

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]


# ── 신규: 검증 게이트 결과 ────────────────────────────────────────


def verification_blocks(stage: str, success: bool, details: list[str]) -> list[dict]:
    """검증 게이트 결과. 성공=체크, 실패=X + 항목별 결과."""
    icon = ":white_check_mark:" if success else ":x:"
    status = "통과" if success else "실패"
    header = f"{icon} *{stage} 검증 {status}*"

    if details:
        detail_lines = "\n".join(f"  - {d}" for d in details)
        text = f"{header}\n{detail_lines}"
    else:
        text = header

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]


# ── 신규: 피드백 카드 ────────────────────────────────────────────


def feedback_content_card(item: dict, index: int) -> list[dict]:
    """콘텐츠 아이템 피드백 카드.

    키워드, 퍼널, GEO, 제목, 발행일 표시.
    버튼 3개: approve / seed_change / meta_change
    """
    keyword = item.get("keyword", "")
    funnel = FUNNEL_KO.get(item.get("funnel", ""), "-")
    geo_type = item.get("geo_type", "")
    title_seo = item.get("title_seo", "")
    publish_date = item.get("publish_date", "")

    text = (
        f"*{keyword}*\n"
        f"퍼널: {funnel} | GEO: {geo_type} | 발행일: {publish_date}\n"
        f"_{title_seo}_"
    )

    return [
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": f"feedback_approve_{index}",
                    "text": {"type": "plain_text", "text": "승인"},
                    "style": "primary",
                },
                {
                    "type": "button",
                    "action_id": f"feedback_seed_change_{index}",
                    "text": {"type": "plain_text", "text": "시드 변경"},
                },
                {
                    "type": "button",
                    "action_id": f"feedback_meta_change_{index}",
                    "text": {"type": "plain_text", "text": "메타 변경"},
                },
            ],
        },
    ]


def feedback_seed_modal(item: dict, private_metadata: str) -> dict:
    """시드 변경 모달. 원래 질문 표시 + 새 질문 입력 필드."""
    keyword = item.get("keyword", "")
    return {
        "type": "modal",
        "callback_id": "feedback_seed_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "시드 키워드 변경"},
        "submit": {"type": "plain_text", "text": "변경 실행"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*현재 키워드*: {keyword}",
                },
            },
            {
                "type": "input",
                "block_id": "new_question_block",
                "label": {"type": "plain_text", "text": "새 질문"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "new_question_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "새로운 질문을 입력하세요"},
                },
            },
        ],
    }


def feedback_meta_modal(item: dict, private_metadata: str) -> dict:
    """메타 변경 모달. 현재 퍼널/GEO/제목 표시 + 변경 내용 입력 필드."""
    keyword = item.get("keyword", "")
    funnel = item.get("funnel", "")
    geo_type = item.get("geo_type", "")
    title_seo = item.get("title_seo", "")

    return {
        "type": "modal",
        "callback_id": "feedback_meta_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "메타 정보 변경"},
        "submit": {"type": "plain_text", "text": "변경 실행"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*키워드*: {keyword}\n"
                        f"*퍼널*: {funnel} | *GEO*: {geo_type}\n"
                        f"*제목*: {title_seo}"
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "meta_change_block",
                "label": {"type": "plain_text", "text": "변경 지시"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "meta_change_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "예: 퍼널을 consideration으로, 제목을 변경해 주세요",
                    },
                },
            },
        ],
    }


def feedback_issues_button(run_id: str) -> list[dict]:
    """'GitHub Issues 피드백 처리 시작' 버튼."""
    return [
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":github: *GitHub Issues 피드백* (Run: `{run_id}`)",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "feedback_process_issues",
                    "text": {"type": "plain_text", "text": "GitHub Issues 피드백 처리"},
                },
                {
                    "type": "button",
                    "action_id": "feedback_complete",
                    "text": {"type": "plain_text", "text": "피드백 완료"},
                    "style": "primary",
                },
            ],
        },
    ]


def feedback_complete_blocks(changed_items: list[dict]) -> list[dict]:
    """피드백 처리 완료 요약."""
    if not changed_items:
        text = ":white_check_mark: *피드백 처리 완료* — 변경 사항 없음"
    else:
        lines = [":white_check_mark: *피드백 처리 완료*"]
        for item in changed_items:
            keyword = item.get("keyword", "")
            change_type = item.get("change_type", "")
            lines.append(f"  - {keyword}: {change_type}")
        text = "\n".join(lines)

    return [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]
