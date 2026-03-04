"""슬랙 Block Kit 메시지 조립."""

from __future__ import annotations

import datetime as dt_mod

from core.schemas import ContentPlan

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


def build_modal(private_metadata: str = "", initial_month: str = "") -> dict:
    """'/content' 슬래시 커맨드 실행 시 열리는 입력 모달."""
    return {
        "type": "modal",
        "callback_id": "content_input_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "콘텐츠 계획 입력"},
        "submit": {"type": "plain_text", "text": "제출하기"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {
                "type": "input",
                "block_id": "intent_block",
                "label": {"type": "plain_text", "text": "질문 의도"},
                "element": {
                    "type": "static_select",
                    "action_id": "intent_select",
                    "placeholder": {"type": "plain_text", "text": "선택하세요"},
                    "options": [
                        {"text": {"type": "plain_text", "text": "정보 탐색"}, "value": "정보 탐색"},
                        {"text": {"type": "plain_text", "text": "비교 판단"}, "value": "비교 판단"},
                        {"text": {"type": "plain_text", "text": "추천"}, "value": "추천"},
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "questions_block",
                "label": {"type": "plain_text", "text": "질문 형태"},
                "hint": {"type": "plain_text", "text": "쿼리 팬아웃 전 큰 단위 질문 — 한 줄에 하나씩"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "questions_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "ERP 외주 개발 업체를 고를 때 기준은?\n앱 개발 견적이 업체마다 다른 이유는?",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "direction_block",
                "label": {"type": "plain_text", "text": "콘텐츠 방향성"},
                "element": {
                    "type": "static_select",
                    "action_id": "direction_select",
                    "placeholder": {"type": "plain_text", "text": "선택하세요"},
                    "options": [
                        {"text": {"type": "plain_text", "text": "카테고리 포지셔닝"}, "value": "카테고리 포지셔닝"},
                        {"text": {"type": "plain_text", "text": "문제 인식 확산"}, "value": "문제 인식 확산"},
                        {"text": {"type": "plain_text", "text": "판단 기준 제시"}, "value": "판단 기준 제시"},
                        {"text": {"type": "plain_text", "text": "실행 가이드"}, "value": "실행 가이드"},
                    ],
                },
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


# ── 최종 요약 ─────────────────────────────────────────────────────


def summary_blocks(plan: ContentPlan, output_paths: dict[str, str]) -> list[dict]:
    """최종 콘텐츠 계획 요약 Block Kit. 퍼널 분포 제외."""
    year, mon = plan.target_month.split("-")

    # 발행 일정
    calendar_lines: list[str] = []
    for entry in plan.calendar:
        try:
            d = dt_mod.date.fromisoformat(entry.publish_date)
            date_label = f"{d.month}/{d.day}({WEEKDAY_KO[d.weekday()]})"
        except Exception:
            date_label = entry.publish_date
        title = (entry.title[:28] + "…") if len(entry.title) > 28 else entry.title
        calendar_lines.append(f"• {date_label}  {title}")
    calendar_text = "\n".join(calendar_lines) if calendar_lines else "(일정 없음)"

    # 콘텐츠 목록
    piece_lines: list[str] = []
    for i, piece in enumerate(plan.content_pieces, 1):
        funnel_ko = FUNNEL_KO.get(piece.funnel, piece.funnel)
        best_title = (
            piece.title_suggestions[0].title
            if piece.title_suggestions
            else piece.question
        )
        title = (best_title[:32] + "…") if len(best_title) > 32 else best_title
        piece_lines.append(
            f"{i}. [{funnel_ko}]  {title}  →  {piece.publish_date}"
        )
    pieces_text = "\n".join(piece_lines) if piece_lines else "(없음)"

    blocks: list[dict] = [
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":white_check_mark: *{year}년 {int(mon)}월 콘텐츠 계획 완료*"
                    f"  —  총 *{len(plan.content_pieces)}건*"
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*발행 일정*\n{calendar_text}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*콘텐츠 목록*\n{pieces_text}"},
        },
    ]

    # Sheets 링크 / 로컬 파일 경로
    path_lines: list[str] = []
    for k, v in output_paths.items():
        if not v:
            continue
        if k == "sheets":
            path_lines.append(f":bar_chart: <{v}|Google Sheets에서 보기>")
        elif k == "sheets_error":
            path_lines.append(f":warning: Sheets 업로드 실패: `{v}`")
        elif k == "md":
            path_lines.append(f":file_folder: `{v}`")
        # json 경로는 생략
    if path_lines:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(path_lines)},
        })

    # 콘텐츠 플랜 기획안 시트 고정 링크
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                ":memo: <https://docs.google.com/spreadsheets/d/"
                "1HRvOnEKtRMU1hoc0ptJTubxK7N_WIK0cRcQhIYwR1JU/edit"
                "?gid=1989654033#gid=1989654033"
                "|콘텐츠 플랜 기획안 시트 열기>"
            ),
        },
    })

    return blocks
