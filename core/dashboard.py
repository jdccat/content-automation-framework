"""ContentPlan JSON → 단일 HTML 대시보드 생성기.

사용법:
  python -m core.dashboard output/planner/2026-03_wishket_plan.json
  → docs/2026-03_wishket_20260304.html 생성 (run_date 기준 버전)
  → docs/index.html 자동 생성 (월별 인덱스)

파이프라인 내부:
  run_planner.py 완료 후 자동 호출.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

# ── 한글 매핑 ────────────────────────────────────────────────────

FUNNEL_KO = {
    "awareness": "인지",
    "consideration": "고려",
    "conversion": "전환",
    "unclassified": "미분류",
}
GEO_KO = {
    "definition": "정의형",
    "comparison": "비교형",
    "problem_solving": "문제해결형",
}
TREND_KO = {"rising": "상승", "stable": "안정", "declining": "하락"}
DAY_KO = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}


# ── 공개 API ─────────────────────────────────────────────────────


def generate(
    plan_path: str | Path,
    out_dir: str | Path = "docs",
    run_date: date | None = None,
) -> Path:
    """ContentPlan JSON 을 읽어 HTML 대시보드를 생성한다.

    Args:
        plan_path: ContentPlan JSON 경로.
        out_dir: 출력 디렉토리 (기본 docs/).
        run_date: 버전 기준 날짜 (기본 today).

    Returns:
        생성된 HTML 파일의 Path.
    """
    plan_path = Path(plan_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if run_date is None:
        run_date = date.today()

    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)

    items = _build_items(plan, run_date)
    html = _render(plan, items)

    month = plan.get("target_month", "unknown")
    client = plan.get("client_name", "client")
    datestamp = run_date.strftime("%Y%m%d")
    filename = f"{month}_{client}_{datestamp}.html"
    out_file = out_dir / filename
    out_file.write_text(html, encoding="utf-8")
    return out_file


def generate_index(out_dir: str | Path = "docs") -> Path:
    """docs/ 내 HTML 대시보드를 스캔하여 index.html 을 생성한다.

    월별 그룹핑, 최신 버전 강조. GitHub Pages에서 바로 서빙 가능.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # index.html 자체는 제외하고 스캔
    html_files = sorted(
        (f for f in out_dir.glob("*.html") if f.name != "index.html"),
        reverse=True,
    )

    # 월별 그룹핑: "2026-03" → [파일목록]
    groups: dict[str, list[Path]] = {}
    pattern = re.compile(r"^(\d{4}-\d{2})_")
    for f in html_files:
        m = pattern.match(f.name)
        month = m.group(1) if m else "기타"
        groups.setdefault(month, []).append(f)

    rows_html = []
    for month in sorted(groups, reverse=True):
        files = groups[month]
        for i, f in enumerate(files):
            is_latest = i == 0
            badge = ' <span style="background:#059669;color:#fff;padding:1px 8px;border-radius:999px;font-size:10px;font-weight:600;margin-left:6px">최신</span>' if is_latest else ""
            weight = "700" if is_latest else "400"
            bg = "#F0FDF4" if is_latest else "#fff"
            rows_html.append(
                f'<tr style="background:{bg}">'
                f'<td style="padding:10px 14px;font-weight:{weight}">{month}</td>'
                f'<td style="padding:10px 14px"><a href="{f.name}" style="color:#7C3AED;text-decoration:none;font-weight:{weight}">{f.name}</a>{badge}</td>'
                f'<td style="padding:10px 14px;color:#6B7280;font-size:12px">{_file_size_label(f)}</td>'
                f"</tr>"
            )

    index_html = _INDEX_TEMPLATE.replace("%%ROWS%%", "\n".join(rows_html))
    index_path = out_dir / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    return index_path


def _file_size_label(f: Path) -> str:
    size = f.stat().st_size
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KB"


# ── 내부 헬퍼 ────────────────────────────────────────────────────


def _short_category(cat: str) -> str:
    """카테고리 질문 → 짧은 토픽 라벨."""
    text = re.sub(r"^\d+\.\s*", "", cat)
    for particle in ["를 ", "이 ", "을 ", "에서 "]:
        idx = text.find(particle)
        if 0 < idx < 20:
            return text[:idx]
    return text[:15] + ("…" if len(text) > 15 else "")


def _format_rationale(piece: dict) -> str:
    """data_rationale + funnel_journey_reasoning → 3칼럼 파싱 가능 텍스트."""
    text = piece.get("data_rationale", "")
    text = re.sub(r"\[데이터\]\s*", "데이터 — ", text)
    text = re.sub(r"\s*\[방향\]\s*", "\n방향 — ", text)

    funnel_reasoning = piece.get("funnel_journey_reasoning", "")
    if funnel_reasoning:
        text += f"\n퍼널 — {funnel_reasoning}"
    return text


def _build_items(plan: dict, run_date: date | None = None) -> list[dict]:
    """content_pieces → JS D 배열 형식 리스트.

    run_date 이전 발행일의 콘텐츠는 제외한다 (플랜 원본은 보존).
    """
    today = run_date or date.today()
    pieces = sorted(
        plan.get("content_pieces", []),
        key=lambda p: p.get("publish_date", ""),
    )
    items = []
    for p in pieces:
        # 과거 발행일 필터링
        pub_str = p.get("publish_date", "")
        if pub_str:
            try:
                if date.fromisoformat(pub_str) < today:
                    continue
            except ValueError:
                pass
        pub = p.get("publish_date", "")
        dt = date.fromisoformat(pub) if pub else None

        seo = ctr = ""
        for ts in p.get("title_suggestions", []):
            if ts["strategy"] == "seo":
                seo = ts["title"]
            elif ts["strategy"] == "ctr":
                ctr = ts["title"]

        items.append(
            {
                "date": dt.strftime("%m.%d") if dt else "",
                "day": DAY_KO.get(dt.weekday(), "") if dt else "",
                "cat": _short_category(p.get("category", "")),
                "funnel": FUNNEL_KO.get(p.get("funnel", ""), "미분류"),
                "geo": GEO_KO.get(p.get("geo_type", ""), ""),
                "seo": seo,
                "ctr": ctr,
                "h2": [h["heading"] for h in p.get("h2_structure", [])],
                "cta": p.get("cta_suggestion", ""),
                "purpose": p.get("publishing_purpose", ""),
                "vol": p.get("monthly_volume_naver", 0),
                "pri": round(p.get("priority_score", 0) * 10, 1),
                "parentQ": p.get("category", ""),
                "question": p.get("question", ""),
                "rationale": _format_rationale(p),
                "trend": TREND_KO.get(p.get("volume_trend", "stable"), "안정"),
            }
        )
    return items


def _render_questions_html(questions: list[str]) -> str:
    """입력 질문 목록 → HTML."""
    parts = []
    for i, q in enumerate(questions, 1):
        parts.append(
            f'<div style="display:flex;align-items:flex-start;gap:8px">'
            f'<span style="min-width:18px;height:18px;border-radius:5px;background:#7C3AED;'
            f"color:#fff;font-size:10px;font-weight:700;display:flex;align-items:center;"
            f'justify-content:center;flex-shrink:0">{i}</span>'
            f'<span style="font-size:13px;color:#374151;line-height:1.5">{q}</span></div>'
        )
    return "\n    ".join(parts)


def _render(plan: dict, items: list[dict]) -> str:
    """HTML 문자열 조립."""
    target = plan.get("target_month", "")
    year_month = ""
    if target and "-" in target:
        y, m = target.split("-")
        year_month = f"{y}년 {int(m)}월"

    total = len(items)
    intent = ", ".join(plan.get("intent", []))
    direction = ", ".join(plan.get("content_direction", []))
    questions = plan.get("categories", [])

    geo_counts: dict[str, int] = {}
    for item in items:
        g = item["geo"]
        geo_counts[g] = geo_counts.get(g, 0) + 1
    geo_summary = " · ".join(f"{k} {v}건" for k, v in geo_counts.items())

    desc = (
        f"'{intent}' 의도의 검색자를 대상으로 '{direction}' 방향의 "
        f"콘텐츠 {total}건을 기획하여 월·수·금 주 3회 발행한다."
    )

    data_json = json.dumps(items, ensure_ascii=False)
    questions_html = _render_questions_html(questions)

    html = _HTML_TEMPLATE
    replacements = {
        "%%YEAR_MONTH%%": year_month,
        "%%DESC%%": desc,
        "%%TOTAL%%": str(total),
        "%%DIRECTION%%": direction,
        "%%INTENT%%": intent,
        "%%GEO_SUMMARY%%": geo_summary,
        "%%QUESTIONS_HTML%%": questions_html,
        "%%QUESTIONS_COUNT%%": str(len(questions)),
        "%%DATA_JSON%%": data_json,
    }
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    return html


# ── HTML 템플릿 ──────────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>%%YEAR_MONTH%% 위시켓 블로그 콘텐츠 전략</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#111;background:#fff;max-width:1000px;margin:0 auto;padding:28px 20px}
.label{font-size:11px;font-weight:700;color:#7C3AED;letter-spacing:1.2px}
h1{font-size:24px;font-weight:800;margin:4px 0}
.desc{font-size:13px;color:#6B7280;line-height:1.6;margin-bottom:24px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:20px}
.stat{background:#F9FAFB;border-radius:10px;padding:14px 16px;border:1px solid #F3F4F6}
.stat-label{font-size:10px;font-weight:700;color:#9CA3AF;letter-spacing:.8px;margin-bottom:4px}
.stat-val{font-size:22px;font-weight:800}.stat-sub{font-size:11px;color:#6B7280;margin-top:2px}
.dist{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:28px}
.dist-box{background:#F9FAFB;border-radius:10px;padding:14px;border:1px solid #F3F4F6}
.dist-title{font-size:10px;font-weight:700;color:#9CA3AF;letter-spacing:.8px;margin-bottom:8px}
.dist-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.dist-row span:last-child{font-size:13px;font-weight:700}
.badge{display:inline-block;padding:1px 7px;border-radius:999px;font-size:10px;font-weight:600;white-space:nowrap}
.b-funnel-고려{background:#D9770614;color:#D97706;border:1px solid #D9770630}
.b-funnel-인지{background:#2563EB14;color:#2563EB;border:1px solid #2563EB30}
.b-funnel-전환{background:#05966914;color:#059669;border:1px solid #05966930}
.b-funnel-미분류{background:#6B728014;color:#6B7280;border:1px solid #6B728030}
.b-geo-비교형{background:#7C3AED14;color:#7C3AED;border:1px solid #7C3AED30}
.b-geo-문제해결형{background:#DB277714;color:#DB2777;border:1px solid #DB277730}
.b-geo-정의형{background:#0891B214;color:#0891B2;border:1px solid #0891B230}
.tabs{display:flex;border-bottom:1px solid #E5E7EB;margin-bottom:0}
.tab{padding:8px 18px;font-size:13px;font-weight:500;color:#999;background:transparent;border:1px solid transparent;border-bottom:1px solid #E5E7EB;border-radius:8px 8px 0 0;cursor:pointer;margin-bottom:-1px}
.tab.active{font-weight:700;color:#111;background:#fff;border:1px solid #E5E7EB;border-bottom:1px solid #fff}
.panel{border:1px solid #E5E7EB;border-top:none;border-radius:0 0 10px 10px;background:#fff}
.cal-head{display:grid;grid-template-columns:74px 56px 82px 1fr 80px 80px;padding:10px 16px 8px;font-size:10px;font-weight:700;color:#9CA3AF;letter-spacing:.5px;border-bottom:1px solid #F3F4F6}
.cal-row{display:grid;grid-template-columns:74px 56px 82px 1fr 80px 80px;padding:10px 16px;align-items:center;cursor:pointer;border-bottom:1px solid #F9FAFB;transition:background .1s}
.cal-row:nth-child(odd){background:#FAFBFC}
.cal-row:hover,.cal-row.sel{background:#F5F3FF}
.cal-date{font-size:13px;font-weight:700}.cal-day{font-size:10px;color:#999;margin-left:4px}
.cal-title{font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-right:8px}
.cal-vol{font-size:12px;color:#444}
.pri-bar{display:flex;align-items:center;gap:5px}
.pri-track{width:48px;height:5px;border-radius:3px;background:#F3F4F6;overflow:hidden}
.pri-fill{height:100%;border-radius:3px}
.pri-num{font-size:11px;font-weight:700}
.detail{padding:0 20px 20px;display:none}
.detail.show{display:block}
.nav{display:flex;justify-content:space-between;align-items:center;padding:10px 0 16px;border-bottom:1px solid #F3F4F6;margin-bottom:20px}
.nav-btn{background:none;border:none;cursor:pointer;font-size:12px;color:#7C3AED}
.nav-btn:disabled{color:#D1D5DB;cursor:default}
.nav-dots{display:flex;gap:4px}
.nav-dot{width:24px;height:24px;border-radius:6px;border:none;font-size:11px;font-weight:600;cursor:pointer;background:#F3F4F6;color:#999}
.nav-dot.active{background:#7C3AED;color:#fff}
.d-meta{font-size:11px;color:#9CA3AF;font-weight:600;margin-bottom:4px}
.d-title{font-size:20px;font-weight:800;line-height:1.35;margin-bottom:10px}
.d-badges{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:20px}
.d-badges .badge{padding:2px 10px;font-size:11px}
.d-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.d-card{background:#F9FAFB;border-radius:8px;padding:14px}
.d-card-label{font-size:10px;font-weight:700;color:#9CA3AF;letter-spacing:.8px;margin-bottom:6px}
.d-card-text{font-size:13px;color:#374151;line-height:1.55}
.d-card-text.italic{font-size:14px;font-style:italic;line-height:1.5}
.info-card{border-radius:8px;padding:12px 14px;margin-bottom:10px}
.info-card .lbl{font-size:10px;font-weight:700;letter-spacing:.8px;margin-bottom:4px}
.info-card .txt{font-size:13px;line-height:1.6}
.ic-parent{background:#FEFCE8;border:1px solid #FEF08A}.ic-parent .lbl{color:#A16207}.ic-parent .txt{color:#713F12}
.ic-question{background:#F0FDF4;border:1px solid #BBF7D0}.ic-question .lbl{color:#166534}.ic-question .txt{color:#14532D}
.h2-list{margin-top:6px}
.h2-label{font-size:10px;font-weight:700;color:#9CA3AF;letter-spacing:.8px;margin-bottom:10px}
.h2-item{display:flex;gap:10px;margin-bottom:6px;align-items:flex-start}
.h2-tag{min-width:22px;height:22px;border-radius:6px;background:#F3F4F6;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#666;flex-shrink:0}
.h2-text{font-size:13px;color:#374151;line-height:1.5}
.empty{padding:40px 16px;text-align:center;font-size:14px;color:#999}
.empty-btns{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:16px}
.empty-btn{padding:6px 12px;border-radius:6px;border:1px solid #E5E7EB;background:#fff;cursor:pointer;font-size:12px;color:#333}
@media(max-width:700px){.stats{grid-template-columns:1fr 1fr}.dist{grid-template-columns:1fr}.d-grid{grid-template-columns:1fr}.cal-head,.cal-row{grid-template-columns:60px 48px 70px 1fr 60px 60px;font-size:11px}}
</style>
</head>
<body>
<div class="label">WISHKET BLOG · CONTENT STRATEGY</div>
<h1>%%YEAR_MONTH%% 콘텐츠 전략</h1>
<p class="desc">%%DESC%%</p>

<div class="stats">
  <div class="stat"><div class="stat-label">총 콘텐츠</div><div class="stat-val">%%TOTAL%%건</div><div class="stat-sub">주 3회 · 월/수/금</div></div>
  <div class="stat"><div class="stat-label">전략 방향</div><div class="stat-val" style="font-size:16px;margin-top:6px">%%DIRECTION%%</div><div class="stat-sub">고려 단계 집중</div></div>
  <div class="stat"><div class="stat-label">주요 검색 의도</div><div class="stat-val" style="font-size:16px;margin-top:6px">%%INTENT%%</div><div class="stat-sub">%%GEO_SUMMARY%%</div></div>
</div>

<div style="background:#F9FAFB;border-radius:10px;padding:16px 18px;border:1px solid #F3F4F6;margin-bottom:20px">
  <div class="stat-label" style="margin-bottom:10px">사용자 입력 질문 (%%QUESTIONS_COUNT%%건)</div>
  <div style="display:flex;flex-direction:column;gap:8px">
    %%QUESTIONS_HTML%%
  </div>
</div>

<div class="dist">
  <div class="dist-box"><div class="dist-title">퍼널 분포</div><div id="funnelDist"></div></div>
  <div class="dist-box"><div class="dist-title">GEO 구조</div><div id="geoDist"></div></div>
  <div class="dist-box"><div class="dist-title">카테고리</div><div id="catDist"></div></div>
</div>

<div class="tabs">
  <button class="tab active" id="tabCal" onclick="showView('cal')">발행 캘린더</button>
  <button class="tab" id="tabDetail" onclick="showView('detail')">콘텐츠 상세</button>
</div>

<div class="panel">
  <div id="viewCal">
    <div class="cal-head"><span>발행일</span><span>퍼널</span><span>GEO</span><span>제목 (SEO)</span><span>검색량</span><span>우선순위</span></div>
    <div id="calRows"></div>
  </div>
  <div id="viewDetail" style="display:none">
    <div id="detailEmpty" class="empty">
      콘텐츠를 선택하세요
      <div class="empty-btns" id="emptyBtns"></div>
    </div>
    <div id="detailContent" class="detail"></div>
  </div>
</div>

<script>
const D=%%DATA_JSON%%;

let sel=null;
const cnt=(arr,k)=>{const m={};arr.forEach(r=>{const v=r[k];m[v]=(m[v]||0)+1});return m};

function distHTML(counts,type){
  return Object.entries(counts).map(([k,v])=>`<div class="dist-row"><span class="badge b-${type}-${k}">${k}</span><span>${v}건</span></div>`).join("");
}
function catDistHTML(counts){
  return Object.entries(counts).map(([k,v])=>`<div class="dist-row"><span style="font-size:12px;color:#374151">${k}</span><span>${v}건</span></div>`).join("");
}
document.getElementById("funnelDist").innerHTML=distHTML(cnt(D,"funnel"),"funnel");
document.getElementById("geoDist").innerHTML=distHTML(cnt(D,"geo"),"geo");
document.getElementById("catDist").innerHTML=catDistHTML(cnt(D,"cat"));

function priColor(s){return s>=7?"#059669":s>=5?"#D97706":"#DC2626"}
function priHTML(s){
  const c=priColor(s);
  return `<div class="pri-bar"><div class="pri-track"><div class="pri-fill" style="width:${s*10}%;background:${c}"></div></div><span class="pri-num" style="color:${c}">${s.toFixed(1)}</span></div>`;
}

const calEl=document.getElementById("calRows");
D.forEach((r,i)=>{
  const row=document.createElement("div");
  row.className="cal-row";
  row.onclick=()=>{sel=i;showView("detail");renderDetail()};
  row.innerHTML=`
    <div><span class="cal-date">${r.date}</span><span class="cal-day">${r.day}</span></div>
    <span class="badge b-funnel-${r.funnel}">${r.funnel}</span>
    <span class="badge b-geo-${r.geo}">${r.geo}</span>
    <div class="cal-title">${r.seo}</div>
    <span class="cal-vol">${r.vol.toLocaleString()}</span>
    ${priHTML(r.pri)}`;
  calEl.appendChild(row);
});

const emptyEl=document.getElementById("emptyBtns");
D.forEach((r,i)=>{
  const btn=document.createElement("button");
  btn.className="empty-btn";
  btn.textContent=`${r.date} ${r.seo.substring(0,18)}…`;
  btn.onclick=()=>{sel=i;renderDetail()};
  emptyEl.appendChild(btn);
});

function showView(v){
  document.getElementById("viewCal").style.display=v==="cal"?"block":"none";
  document.getElementById("viewDetail").style.display=v==="detail"?"block":"none";
  document.getElementById("tabCal").className=v==="cal"?"tab active":"tab";
  document.getElementById("tabDetail").className=v==="detail"?"tab active":"tab";
  if(v==="detail"&&sel!==null)renderDetail();
  if(v==="detail"&&sel===null){document.getElementById("detailEmpty").style.display="block";document.getElementById("detailContent").className="detail"}
}

function renderDetail(){
  if(sel===null)return;
  const r=D[sel];
  document.getElementById("detailEmpty").style.display="none";
  const el=document.getElementById("detailContent");
  el.className="detail show";

  const trendIcon=r.trend==="상승"?"↑":r.trend==="하락"?"↓":"→";
  const trendColor=r.trend==="상승"?"#059669":"#6B7280";

  const dots=D.map((_,i)=>`<button class="nav-dot ${i===sel?"active":""}" onclick="sel=${i};renderDetail()">${i+1}</button>`).join("");
  const h2s=r.h2.map(h=>`<div class="h2-item"><span class="h2-tag">H2</span><span class="h2-text">${h}</span></div>`).join("");

  el.innerHTML=`
    <div class="nav">
      <button class="nav-btn" ${sel<=0?"disabled":""} onclick="sel--;renderDetail()">← 이전</button>
      <div class="nav-dots">${dots}</div>
      <button class="nav-btn" ${sel>=D.length-1?"disabled":""} onclick="sel++;renderDetail()">다음 →</button>
    </div>
    <div class="d-meta">${r.date} (${r.day}) · ${r.cat}</div>
    <div class="d-title">${r.seo}</div>
    <div class="d-badges">
      <span class="badge b-funnel-${r.funnel}">${r.funnel}</span>
      <span class="badge b-geo-${r.geo}">${r.geo}</span>
      <span class="badge" style="background:#37415114;color:#374151;border:1px solid #37415130">CTA: ${r.cta}</span>
      <span class="badge" style="background:#6B728014;color:#6B7280;border:1px solid #6B728030">${r.vol.toLocaleString()}회/월</span>
      <span class="badge" style="background:${trendColor}14;color:${trendColor};border:1px solid ${trendColor}30">트렌드 ${r.trend} ${trendIcon}</span>
      <span class="badge" style="background:${priColor(r.pri)}14;color:${priColor(r.pri)};border:1px solid ${priColor(r.pri)}30">우선순위 ${r.pri.toFixed(1)}</span>
    </div>
    <div class="d-grid">
      <div class="d-card"><div class="d-card-label">제목 기타 후보</div><div class="d-card-text italic">${r.ctr}</div></div>
      <div class="d-card"><div class="d-card-label">발행 목적</div><div class="d-card-text">${r.purpose}</div></div>
    </div>
    <div class="info-card ic-parent"><div class="lbl">상위 질문</div><div class="txt">${r.parentQ}</div></div>
    <div class="info-card ic-question"><div class="lbl">선정 질문</div><div class="txt">${r.question}</div></div>
    <div class="h2-list"><div class="h2-label">H2 구조</div>${h2s}</div>
    ${(()=>{
      const txt=r.rationale;
      const dm=txt.match(/데이터\s*—\s*([\s\S]*?)(?=\n방향|$)/);
      const drm=txt.match(/방향\s*—\s*([\s\S]*?)(?=\n퍼널|$)/);
      const fm=txt.match(/퍼널\s*—\s*([\s\S]*?)$/);
      const pd=dm?dm[1].trim():"";
      const pdr=drm?drm[1].trim():"";
      const pf=fm?fm[1].trim():"";
      return `
        <div style="font-size:10px;font-weight:700;color:#9CA3AF;letter-spacing:.8px;margin:20px 0 10px">선정 근거</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
          <div class="info-card" style="background:#EFF6FF;border:1px solid #BFDBFE;margin:0"><div class="lbl" style="color:#1D4ED8">데이터</div><div class="txt" style="color:#1E3A5F">${pd}</div></div>
          <div class="info-card" style="background:#F5F3FF;border:1px solid #DDD6FE;margin:0"><div class="lbl" style="color:#5B21B6">방향</div><div class="txt" style="color:#3B0764">${pdr}</div></div>
          <div class="info-card" style="background:#F0FDF4;border:1px solid #BBF7D0;margin:0"><div class="lbl" style="color:#166534">퍼널</div><div class="txt" style="color:#14532D">${pf}</div></div>
        </div>`;
    })()}`;

  document.querySelectorAll(".cal-row").forEach((row,i)=>{row.classList.toggle("sel",i===sel)});
}
</script>
</body>
</html>
"""


# ── 인덱스 HTML 템플릿 ──────────────────────────────────────────

_INDEX_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>콘텐츠 전략 대시보드</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#111;background:#fff;max-width:800px;margin:0 auto;padding:40px 20px}
h1{font-size:22px;font-weight:800;margin-bottom:6px}
.sub{font-size:13px;color:#6B7280;margin-bottom:28px}
table{width:100%;border-collapse:collapse;border:1px solid #E5E7EB;border-radius:8px;overflow:hidden}
th{background:#F9FAFB;text-align:left;padding:10px 14px;font-size:11px;font-weight:700;color:#9CA3AF;letter-spacing:.5px;border-bottom:1px solid #E5E7EB}
td{border-bottom:1px solid #F3F4F6}
tr:last-child td{border-bottom:none}
a:hover{text-decoration:underline!important}
</style>
</head>
<body>
<div style="font-size:11px;font-weight:700;color:#7C3AED;letter-spacing:1.2px;margin-bottom:4px">WISHKET BLOG · CONTENT STRATEGY</div>
<h1>콘텐츠 전략 대시보드</h1>
<p class="sub">월별 발행 전략 대시보드 버전 목록</p>
<table>
<tr><th>월</th><th>파일</th><th>크기</th></tr>
%%ROWS%%
</table>
</body>
</html>
"""


# ── CLI 실행 ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("사용법: python -m core.dashboard <plan.json> [출력 디렉토리]")
        sys.exit(1)

    plan_json = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "docs"
    result = generate(plan_json, out)
    print(f"대시보드 생성 완료: {result}")
    idx = generate_index(out)
    print(f"인덱스 생성 완료: {idx}")
