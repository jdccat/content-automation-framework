"""Google Sheets 업로더 — ContentPlan → 스프레드시트 월별 탭 기록.

탭 레이아웃 (13열 A~M):

  ┌─────────────────────────────────────────────────────────┐
  │ WISHKET BLOG · CONTENT STRATEGY                         │
  │ YYYY년 MM월 콘텐츠 전략                                  │
  │ 핵심 목표 (회색)                                         │
  ├──────────────────┬──────────────────────────────────────┤
  │ 이번 달 요약(A:E) │ 사용자 입력 질문 N건 (F:M)           │
  ├────────┬─────────┼──────────────────────────────────────┤
  │총 콘텐츠│9건·주3회│1. 질문                               │
  │전략 방향│판단 기준│2. 질문                               │
  │검색 의도│비교 판단│3. 질문                               │
  ├────────┴─────────┴──────────────────────────────────────┤
  │ 발행 캘린더                                              │
  ├───┬──┬──┬────┬─────┬─────┬──────┬────┬────┬─┬────┬────┬─┤
  │발행│요│퍼│GEO│제목1│제목2│H2목차│CTA│발행│ │데이│방향│퍼널│
  │일 │일│널│유형│    │    │     │문구│목적│ │터  │   │근거│
  └───┴──┴──┴────┴─────┴─────┴──────┴────┴────┴─┴────┴────┴─┘

요약 라벨: A:B  |  요약 값: C:E  |  질문: F:M
캘린더 발행정보: A:I  |  구분: J  |  선정근거: K:M
인증: Service Account (GOOGLE_SERVICE_ACCOUNT_JSON)
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from functools import partial

import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build

from core.schemas import ContentPlan, ContentPiece

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_NAME = "위시켓 콘텐츠 플랜"
WEEKDAY_KO = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}
FUNNEL_KO  = {"awareness": "인지", "consideration": "고려", "conversion": "전환", "unclassified": "-"}
GEO_KO     = {"definition": "정의형", "comparison": "비교형", "problem_solving": "문제해결형"}

# ── 열 레이아웃 ──────────────────────────────────────────────────
NUM_COLS = 13

# 요약 섹션 (A:B 라벨 | C:E 값)
SUM_LBL_S = 0
SUM_LBL_E = 2   # A:B  (exclusive → cols 0, 1)
SUM_VAL_S = 2
SUM_VAL_E = 5   # C:E  (exclusive → cols 2, 3, 4)

# 질문 섹션 (F:M)
Q_COL_S = 5
Q_COL_E = 13    # F:M  (exclusive → cols 5‥12)

# 캘린더 섹션
COL_PUB_START = 0
COL_PUB_END   = 9   # A:I
COL_SEP       = 9   # J
COL_RAT_START = 10
COL_RAT_END   = 13  # K:M

PUB_HEADERS = ["발행일", "요일", "퍼널", "검색 의도",
               "제목 후보 1", "제목 후보 2", "H2 목차", "CTA 문구", "발행 목적"]
RAT_HEADERS = ["데이터", "방향", "퍼널 근거"]


# ── 컬러 팔레트 ──────────────────────────────────────────────────
def _rgb(r: float, g: float, b: float) -> dict:
    return {"red": r, "green": g, "blue": b}

_C = {
    "white":        _rgb(1.0,   1.0,   1.0),
    "bg_brand":     _rgb(0.961, 0.949, 1.0),
    "purple":       _rgb(0.486, 0.227, 0.929),
    "text_main":    _rgb(0.067, 0.067, 0.067),
    "text_gray":    _rgb(0.420, 0.447, 0.502),
    "text_label":   _rgb(0.612, 0.627, 0.651),
    "lbl_cell_bg":  _rgb(0.945, 0.953, 0.961),
    "val_cell_bg":  _rgb(0.988, 0.992, 0.996),
    "border":       _rgb(0.780, 0.800, 0.820),
    "border_light": _rgb(0.878, 0.894, 0.910),
    "border_inner": _rgb(0.890, 0.898, 0.910),
    "blue_hdr":     _rgb(0.220, 0.533, 0.733),
    "teal_hdr":     _rgb(0.133, 0.522, 0.467),
    "sep":          _rgb(0.933, 0.933, 0.933),
    "row_even":     _rgb(1.0,   1.0,   1.0),
    "row_odd":      _rgb(0.957, 0.969, 0.980),
    "cal_hdr_bg":   _rgb(0.180, 0.180, 0.220),
}

def _border(key: str, w: int = 1) -> dict:
    return {"style": "SOLID", "width": w, "color": _C[key]}


# ── 공개 인터페이스 ──────────────────────────────────────────────

async def upload_to_sheets(
    plan: ContentPlan,
    folder_id: str,
    creds_json_path: str,
) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, partial(_upload_sync, plan, folder_id, creds_json_path)
    )


def _upload_sync(plan: ContentPlan, folder_id: str, creds_json_path: str) -> str:
    creds = service_account.Credentials.from_service_account_file(
        creds_json_path, scopes=SCOPES
    )
    gc    = gspread.authorize(creds)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    sh = _get_or_create_spreadsheet(gc, drive, folder_id)
    strategy = _parse_strategy_summary(plan.planning_document or "")
    rows, layout = _build_rows(plan, strategy)

    ws = _get_or_create_worksheet(sh, plan.target_month, len(rows) + 5)

    # ① 이전 병합 먼저 해제
    #    ws.clear()는 셀 값만 지우고 merge는 유지됨.
    #    데이터를 쓰기 전에 unmerge하지 않으면 non-anchor 셀에 쓴 값이 소실된다.
    sh.batch_update({"requests": [{"unmergeCells": {
        "range": {"sheetId": ws.id,
                  "startRowIndex": 0,
                  "endRowIndex": len(rows) + 20,
                  "startColumnIndex": 0,
                  "endColumnIndex": 26},
    }}]})

    # ② 데이터 쓰기
    ws.update("A1", rows, value_input_option="RAW")

    # ③ 서식 적용
    _format_sheet(sh, ws, layout)

    url = f"https://docs.google.com/spreadsheets/d/{sh.id}/edit"
    logger.info("Google Sheets 업로드 완료: %s (탭: %s)", url, plan.target_month)
    return url


# ── 행 데이터 조립 ───────────────────────────────────────────────

def _build_rows(plan: ContentPlan, strategy: dict) -> tuple[list[list], dict]:
    rows: list[list] = []
    layout: dict = {}
    E = [""] * NUM_COLS

    total       = len(plan.content_pieces)
    content_dir = _extract_direction(plan, strategy)
    intent_text = " · ".join(plan.intent) if plan.intent else _extract_intent(strategy)
    questions   = list(plan.categories or [])

    year, month = plan.target_month.split("-")
    title = f"{year}년 {int(month)}월 콘텐츠 전략"

    # ── 1. 헤더 섹션
    layout["row_label"] = len(rows)
    rows.append(["WISHKET BLOG · CONTENT STRATEGY"] + [""] * (NUM_COLS - 1))

    layout["row_title"] = len(rows)
    rows.append([title] + [""] * (NUM_COLS - 1))

    layout["row_desc"] = len(rows)
    rows.append([strategy.get("core_objective", "")] + [""] * (NUM_COLS - 1))

    layout["row_blank_1"] = len(rows)
    rows.append(list(E))

    # ── 2. 요약(좌 A:E) + 질문(우 F:M) 나란히
    n_sum  = 3  # 총 콘텐츠 / 전략 방향 / 검색 의도
    n_rows = max(n_sum, len(questions))

    layout["row_combined_hdr"] = len(rows)
    rows.append(
        ["이번 달 요약"] + [""] * (Q_COL_S - 1)                              # A:E (5열)
        + [f"사용자 입력 질문 ({len(questions)}건)"] + [""] * (NUM_COLS - Q_COL_S - 1)  # F:M (8열)
    )

    sum_labels = ["총 콘텐츠", "전략 방향", "검색 의도"]
    sum_values = [
        f"{total}건  ·  주 3회 · 월/수/금",
        content_dir,
        intent_text,
    ]

    layout["row_combined_start"] = len(rows)
    for i in range(n_rows):
        lbl = sum_labels[i] if i < n_sum else ""
        val = sum_values[i] if i < n_sum else ""
        q   = f"{i + 1}.  {questions[i]}" if i < len(questions) else ""
        rows.append(
            [lbl, ""]                                      # A:B  (2열)
            + [val, "", ""]                                # C:E  (3열)
            + [q] + [""] * (NUM_COLS - Q_COL_S - 1)       # F:M  (8열)
        )
    layout["row_combined_end"] = len(rows)

    layout["row_blank_2"] = len(rows)
    rows.append(list(E))

    # ── 3. 발행 캘린더
    layout["row_cal_hdr"] = len(rows)
    rows.append(["콘텐츠 세부 기획안"] + [""] * (NUM_COLS - 1))

    layout["row_table_hdr"] = len(rows)
    rows.append(PUB_HEADERS + [""] + RAT_HEADERS)

    layout["row_data_start"] = len(rows)
    for piece in sorted(plan.content_pieces, key=lambda p: p.publish_date or ""):
        rows.append(_piece_to_row(piece))
    layout["row_data_end"] = len(rows)

    return rows, layout


# ── 스프레드시트 / 워크시트 관리 ─────────────────────────────────

def _get_or_create_spreadsheet(gc: gspread.Client, drive, folder_id: str) -> gspread.Spreadsheet:
    query = (
        f"name='{SPREADSHEET_NAME}' "
        f"and mimeType='application/vnd.google-apps.spreadsheet' "
        f"and trashed=false"
    )
    results = drive.files().list(
        q=query, fields="files(id, name)",
        includeItemsFromAllDrives=True, supportsAllDrives=True,
    ).execute()
    files = results.get("files", [])
    if files:
        return gc.open_by_key(files[0]["id"])
    raise RuntimeError(
        f"'{SPREADSHEET_NAME}' 스프레드시트를 찾을 수 없습니다. "
        "Drive에서 생성 후 서비스 계정과 공유해 주세요."
    )


def _get_or_create_worksheet(sh: gspread.Spreadsheet, month: str, total_rows: int) -> gspread.Worksheet:
    try:
        ws = sh.worksheet(month)
        ws.clear()
        return ws
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=month, rows=total_rows, cols=NUM_COLS)


# ── 데이터 변환 ──────────────────────────────────────────────────

def _piece_to_row(piece: ContentPiece) -> list:
    try:
        d       = date.fromisoformat(piece.publish_date)
        weekday = WEEKDAY_KO[d.weekday()]
    except Exception:
        weekday = ""

    seo = next((t.title for t in piece.title_suggestions if t.strategy == "seo"), "")
    ctr = next((t.title for t in piece.title_suggestions if t.strategy == "ctr"), "")
    if not seo and piece.title_suggestions:
        seo = piece.title_suggestions[0].title

    h2 = "\n".join(f"{i+1}. {h.heading}" for i, h in enumerate(piece.h2_structure))

    data_text, dir_text, funnel_rsn = _split_rationale(piece)

    return [
        piece.publish_date,                            # A  0
        weekday,                                       # B  1
        FUNNEL_KO.get(piece.funnel, piece.funnel),     # C  2
        GEO_KO.get(piece.geo_type, piece.geo_type),    # D  3
        seo,                                           # E  4
        ctr,                                           # F  5
        h2,                                            # G  6
        piece.cta_suggestion,                          # H  7
        piece.publishing_purpose,                      # I  8
        "",                                            # J  9  구분
        data_text,                                     # K 10
        dir_text,                                      # L 11
        funnel_rsn,                                    # M 12
    ]


def _split_rationale(piece: ContentPiece) -> tuple[str, str, str]:
    raw = getattr(piece, "data_rationale", "") or ""
    dm  = re.search(r"\[데이터\]\s*(.*?)(?=\s*\[방향\]|$)", raw, re.DOTALL)
    bm  = re.search(r"\[방향\]\s*(.*?)$",                    raw, re.DOTALL)
    data_text = dm.group(1).strip() if dm else raw.strip()
    dir_text  = bm.group(1).strip() if bm else ""
    funnel    = (getattr(piece, "funnel_journey_reasoning", "") or "").strip()
    return data_text, dir_text, funnel


# ── 텍스트 파싱 헬퍼 ─────────────────────────────────────────────

def _parse_strategy_summary(doc: str) -> dict:
    result = {"core_objective": "", "strategy_direction": ""}
    m = re.search(r"\*\*이번 달 핵심 목표\*\*:\s*(.+?)(?=\n-\s*\*\*|\Z)", doc, re.DOTALL)
    if m:
        result["core_objective"] = m.group(1).strip()
    m = re.search(r"\*\*전략 방향\*\*:\s*(.+?)(?=\n##|\Z)", doc, re.DOTALL)
    if m:
        result["strategy_direction"] = m.group(1).strip()
    return result


def _extract_direction(plan: ContentPlan, strategy: dict) -> str:
    if plan.content_direction:
        return " · ".join(plan.content_direction)
    raw = strategy.get("strategy_direction", "")
    m = re.search(r"방향성:\s*([^|]+)", raw)
    if m:
        return m.group(1).strip()
    return raw[:40] if raw else "—"


def _extract_intent(strategy: dict) -> str:
    obj = strategy.get("core_objective", "")
    m = re.search(r"['\u2018\u2019\u201c\u201d]([^''\u2018\u2019\u201c\u201d]+)['\u2019\u201d]", obj)
    return m.group(1) if m else "—"


# ── 시트 서식 ─────────────────────────────────────────────────────

def _format_sheet(sh: gspread.Spreadsheet, ws: gspread.Worksheet, layout: dict) -> None:
    n_data      = layout["row_data_end"] - layout["row_data_start"]
    row_d_start = layout["row_data_start"]
    row_d_end   = layout["row_data_end"]
    total_rows  = row_d_end + 10
    reqs: list[dict] = []

    # ── 0-a. 기존 데이터 유효성 검사 전체 제거
    reqs.append({"setDataValidation": {
        "range": {"sheetId": ws.id,
                  "startRowIndex": 0, "endRowIndex": total_rows,
                  "startColumnIndex": 0, "endColumnIndex": NUM_COLS},
    }})

    # ── 0-b. 전체 기본 서식
    reqs.append({"repeatCell": {
        "range": {"sheetId": ws.id},
        "cell": {"userEnteredFormat": {
            "wrapStrategy": "WRAP",
            "verticalAlignment": "MIDDLE",
            "horizontalAlignment": "LEFT",
            "textFormat": {"fontSize": 10, "fontFamily": "Arial"},
            "backgroundColor": _C["white"],
        }},
        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,horizontalAlignment,textFormat,backgroundColor)",
    }})

    # ── 1. 행 고정 해제
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 0}},
        "fields": "gridProperties.frozenRowCount",
    }})

    # ── 2. 브랜드 레이블
    r = layout["row_label"]
    reqs += _merge(ws.id, r, r+1, 0, NUM_COLS)
    reqs += _cell_fmt(ws.id, r, r+1, 0, NUM_COLS, {
        "backgroundColor": _C["bg_brand"],
        "textFormat": {"bold": True, "fontSize": 9,
                       "foregroundColor": _C["purple"], "fontFamily": "Arial"},
        "padding": {"top": 5, "bottom": 5, "left": 14, "right": 14},
    })

    # ── 3. 타이틀
    r = layout["row_title"]
    reqs += _merge(ws.id, r, r+1, 0, NUM_COLS)
    reqs += _cell_fmt(ws.id, r, r+1, 0, NUM_COLS, {
        "backgroundColor": _C["white"],
        "textFormat": {"bold": True, "fontSize": 22,
                       "foregroundColor": _C["text_main"], "fontFamily": "Arial"},
        "padding": {"top": 8, "bottom": 4, "left": 14, "right": 14},
    })

    # ── 4. 디스크립션
    r = layout["row_desc"]
    reqs += _merge(ws.id, r, r+1, 0, NUM_COLS)
    reqs += _cell_fmt(ws.id, r, r+1, 0, NUM_COLS, {
        "backgroundColor": _C["white"],
        "textFormat": {"fontSize": 12, "foregroundColor": _C["text_gray"], "fontFamily": "Arial"},
        "padding": {"top": 4, "bottom": 14, "left": 14, "right": 14},
    })

    # ── 5. 요약+질문 나란히 섹션
    r_hdr   = layout["row_combined_hdr"]
    r_start = layout["row_combined_start"]
    r_end   = layout["row_combined_end"]

    # 섹션 헤더: "이번 달 요약" (A:E) | "사용자 입력 질문" (F:M)
    reqs += _merge(ws.id, r_hdr, r_hdr+1, 0, Q_COL_S)
    reqs += _cell_fmt(ws.id, r_hdr, r_hdr+1, 0, Q_COL_S, {
        "backgroundColor": _C["lbl_cell_bg"],
        "textFormat": {"bold": True, "fontSize": 10,
                       "foregroundColor": _C["text_label"], "fontFamily": "Arial"},
        "padding": {"top": 8, "bottom": 6, "left": 14, "right": 14},
    })
    reqs += _merge(ws.id, r_hdr, r_hdr+1, Q_COL_S, NUM_COLS)
    reqs += _cell_fmt(ws.id, r_hdr, r_hdr+1, Q_COL_S, NUM_COLS, {
        "backgroundColor": _C["lbl_cell_bg"],
        "textFormat": {"bold": True, "fontSize": 10,
                       "foregroundColor": _C["text_label"], "fontFamily": "Arial"},
        "padding": {"top": 8, "bottom": 6, "left": 14, "right": 14},
    })

    # 데이터 행: 라벨(A:B) | 값(C:E) | 질문(F:M)
    for row in range(r_start, r_end):
        reqs += _merge(ws.id, row, row+1, SUM_LBL_S, SUM_LBL_E)
        reqs += _cell_fmt(ws.id, row, row+1, SUM_LBL_S, SUM_LBL_E, {
            "backgroundColor": _C["lbl_cell_bg"],
            "textFormat": {"bold": True, "fontSize": 10,
                           "foregroundColor": _C["text_label"], "fontFamily": "Arial"},
            "padding": {"top": 4, "bottom": 4, "left": 14, "right": 14},
        })
        reqs += _merge(ws.id, row, row+1, SUM_VAL_S, SUM_VAL_E)
        reqs += _cell_fmt(ws.id, row, row+1, SUM_VAL_S, SUM_VAL_E, {
            "backgroundColor": _C["val_cell_bg"],
            "textFormat": {"fontSize": 12, "foregroundColor": _C["text_main"], "fontFamily": "Arial"},
            "padding": {"top": 4, "bottom": 4, "left": 14, "right": 14},
        })
        reqs += _merge(ws.id, row, row+1, Q_COL_S, NUM_COLS)
        reqs += _cell_fmt(ws.id, row, row+1, Q_COL_S, NUM_COLS, {
            "backgroundColor": _C["val_cell_bg"],
            "textFormat": {"fontSize": 11, "foregroundColor": _C["text_main"], "fontFamily": "Arial"},
            "padding": {"top": 4, "bottom": 4, "left": 20, "right": 20},
        })

    # 테두리: 좌(A:E)와 우(F:M) 각각 독립
    reqs.append({"updateBorders": {
        "range": _range(ws.id, r_hdr, r_end, 0, Q_COL_S),
        "top": _border("border"), "bottom": _border("border"),
        "left": _border("border"), "right": _border("border"),
        "innerHorizontal": _border("border_light"),
    }})
    reqs.append({"updateBorders": {
        "range": _range(ws.id, r_hdr, r_end, Q_COL_S, NUM_COLS),
        "top": _border("border"), "bottom": _border("border"),
        "left": _border("border"), "right": _border("border"),
        "innerHorizontal": _border("border_light"),
    }})
    # 라벨/값 구분선 (A:B | C:E)
    if r_end > r_start:
        reqs.append({"updateBorders": {
            "range": _range(ws.id, r_start, r_end, SUM_LBL_E, SUM_LBL_E + 1),
            "left": _border("border"),
        }})

    # ── 6. 발행 캘린더 타이틀
    r = layout["row_cal_hdr"]
    reqs += _merge(ws.id, r, r+1, 0, NUM_COLS)
    reqs += _cell_fmt(ws.id, r, r+1, 0, NUM_COLS, {
        "backgroundColor": _C["cal_hdr_bg"],
        "textFormat": {"bold": True, "fontSize": 12,
                       "foregroundColor": _C["white"], "fontFamily": "Arial"},
        "padding": {"top": 8, "bottom": 8, "left": 14, "right": 14},
    })

    # ── 7. 캘린더 테이블 열 헤더
    r = layout["row_table_hdr"]
    reqs += _cell_fmt(ws.id, r, r+1, COL_PUB_START, COL_PUB_END, {
        "backgroundColor": _C["blue_hdr"],
        "textFormat": {"bold": True, "foregroundColor": _C["white"], "fontFamily": "Arial"},
        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
    })
    reqs += _cell_fmt(ws.id, r, r+1, COL_RAT_START, COL_RAT_END, {
        "backgroundColor": _C["teal_hdr"],
        "textFormat": {"bold": True, "foregroundColor": _C["white"], "fontFamily": "Arial"},
        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
    })
    reqs += _cell_fmt(ws.id, r, r+1, COL_SEP, COL_SEP+1, {"backgroundColor": _C["sep"]})

    # ── 8. 데이터 행 교번 색상 + 패딩 + 구분 열
    _DATA_PAD = {"top": 3, "bottom": 3, "left": 10, "right": 10}
    for i in range(n_data):
        bg = _C["row_even"] if i % 2 == 0 else _C["row_odd"]
        reqs += _cell_fmt(ws.id, row_d_start+i, row_d_start+i+1, 0, NUM_COLS,
                          {"backgroundColor": bg, "padding": _DATA_PAD})
    if n_data > 0:
        reqs += _cell_fmt(ws.id, row_d_start, row_d_end, COL_SEP, COL_SEP+1,
                          {"backgroundColor": _C["sep"]})

    # ── 10. 드롭다운 (퍼널 C열=2, GEO D열=3) — 캘린더 데이터 행 전용
    for col_idx, values in [
        (2, ["인지", "고려", "전환", "-"]),
        (3, ["정의형", "비교형", "문제해결형"]),
    ]:
        reqs.append({"setDataValidation": {
            "range": _range(ws.id, row_d_start, row_d_end, col_idx, col_idx+1),
            "rule": {
                "condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": v} for v in values]},
                "showCustomUi": True, "strict": False,
            },
        }})

    # ── 10. 격자선 (캘린더)
    for cs, ce in [(COL_PUB_START, COL_PUB_END), (COL_RAT_START, COL_RAT_END)]:
        reqs.append({"updateBorders": {
            "range": _range(ws.id, layout["row_table_hdr"], row_d_end, cs, ce),
            "top":             _border("border", 2),
            "bottom":          _border("border", 2),
            "left":            _border("border", 2),
            "right":           _border("border", 2),
            "innerHorizontal": _border("border_inner"),
            "innerVertical":   _border("border_inner"),
        }})

    # ── 11. 행 높이
    height_map: dict[int, int] = {
        layout["row_label"]:        26,
        layout["row_title"]:        54,
        layout["row_desc"]:         58,
        layout["row_blank_1"]:      14,
        layout["row_combined_hdr"]: 32,
        layout["row_blank_2"]:      14,
        layout["row_cal_hdr"]:      44,
        layout["row_table_hdr"]:    36,
    }
    for row in range(layout["row_combined_start"], layout["row_combined_end"]):
        height_map[row] = 40
    for row_idx, px in height_map.items():
        reqs.append(_row_height(ws.id, row_idx, px))

    if n_data > 0:
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "ROWS",
                      "startIndex": row_d_start, "endIndex": row_d_end},
            "properties": {"pixelSize": 130},
            "fields": "pixelSize",
        }})

    # ── 12. 열 너비  (B=60, C=80, D=120 으로 확장)
    for ci, px in enumerate([100, 60, 80, 120, 190, 190, 380, 120, 210, 16, 260, 240, 320]):
        reqs.append(_col_width(ws.id, ci, px))

    try:
        sh.batch_update({"requests": reqs})
    except Exception as exc:
        logger.warning("시트 서식 적용 실패 (무시): %s", exc)


# ── 서식 헬퍼 ─────────────────────────────────────────────────────

def _range(sid, r1, r2, c1, c2) -> dict:
    return {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}

def _merge(sid, r1, r2, c1, c2) -> list[dict]:
    return [{"mergeCells": {"range": _range(sid, r1, r2, c1, c2), "mergeType": "MERGE_ALL"}}]

def _cell_fmt(sid, r1, r2, c1, c2, fmt: dict) -> list[dict]:
    fields = "userEnteredFormat(" + ",".join(fmt.keys()) + ")"
    return [{"repeatCell": {
        "range": _range(sid, r1, r2, c1, c2),
        "cell": {"userEnteredFormat": fmt},
        "fields": fields,
    }}]

def _row_height(sid, row, px) -> dict:
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS",
                  "startIndex": row, "endIndex": row+1},
        "properties": {"pixelSize": px}, "fields": "pixelSize",
    }}

def _col_width(sid, col, px) -> dict:
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS",
                  "startIndex": col, "endIndex": col+1},
        "properties": {"pixelSize": px}, "fields": "pixelSize",
    }}
