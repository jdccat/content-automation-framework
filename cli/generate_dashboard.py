#!/usr/bin/env python3
"""Generate redesigned dashboard HTML from a schedule JSON.

Usage:
    .venv/bin/python cli/generate_dashboard.py <schedule_json> [--output <html_path>]

If --output is not given, derives: docs/{target_month}_wishket_{date}_v{N}.html
Also updates docs/index.html after generation.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

FUNNEL_KO: dict[str, str] = {
    "awareness": "인지",
    "consideration": "고려",
    "conversion": "전환",
    "unclassified": "미분류",
}

GEO_KO: dict[str, str] = {
    "comparison": "비교형",
    "problem_solving": "문제해결형",
    "definition": "정의형",
}

ROLE_KO: dict[str, str] = {
    "hub": "허브",
    "sub": "서브",
}

FUNNEL_ORDER: list[str] = ["consideration", "conversion", "awareness"]
GEO_ORDER: list[str] = ["problem_solving", "comparison", "definition"]

# Distribution bar colours (by key, not translated)
FUNNEL_COLOUR: dict[str, str] = {
    "awareness": "#D97706",
    "consideration": "#7C3AED",
    "conversion": "#059669",
}
GEO_COLOUR: dict[str, str] = {
    "comparison": "#2563EB",
    "problem_solving": "#CA8A04",
    "definition": "#6B7280",
}

TREND_KO: dict[str, str] = {
    "stable": "안정",
    "rising": "상승",
    "falling": "하락",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _esc(text: str | None) -> str:
    """HTML-escape text, returning empty string for None."""
    if text is None:
        return ""
    return html.escape(str(text))


def _highlight_class(highlight: str) -> str:
    """Map priority_highlight text to CSS class name."""
    return "b-hi-" + highlight.replace(" ", "")


def _dim_colour_class(score: int | float) -> str:
    """Return CSS class for a dimension score bar."""
    if score >= 7:
        return "dim-green"
    if score >= 5:
        return "dim-orange"
    return "dim-red"


def _iso_week(date_str: str) -> int:
    """Return ISO week number from a YYYY-MM-DD date string."""
    return datetime.strptime(date_str, "%Y-%m-%d").isocalendar()[1]


def _month_week_number(date_str: str, schedule: list[dict[str, Any]]) -> int:
    """Return the 1-based week index within the schedule's month.

    Weeks are grouped by ISO week number; the first distinct ISO week
    in the schedule is week 1, the second is week 2, etc.
    """
    seen_weeks: list[int] = []
    for item in schedule:
        w = _iso_week(item["publish_date"])
        if w not in seen_weeks:
            seen_weeks.append(w)
    target = _iso_week(date_str)
    return seen_weeks.index(target) + 1 if target in seen_weeks else 1


def _parse_output_path(schedule_path: Path) -> Path:
    """Derive output HTML path from a schedule JSON filename.

    Expects: schedule_{target_month}_{date}_v{N}.json
    Produces: docs/{target_month}_wishket_{date}_v{N}.html
    """
    stem = schedule_path.stem  # e.g. schedule_2026-04_20260315_v1
    parts = stem.split("_", 1)  # ["schedule", "2026-04_20260315_v1"]
    suffix = parts[1] if len(parts) > 1 else stem
    return Path("docs") / f"{suffix.split('_', 1)[0]}_wishket_{'_'.join(suffix.split('_')[1:])}.html"


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CSS = """\
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#111;background:#F9FAFB;min-height:100vh}
.wrap{max-width:960px;margin:0 auto;padding:40px 20px}
.back{font-size:11px;color:#7C3AED;text-decoration:none;font-weight:600;letter-spacing:.3px;margin-bottom:16px;display:inline-block}
.back:hover{text-decoration:underline}
.eyebrow{font-size:11px;font-weight:700;color:#7C3AED;letter-spacing:1.2px;margin-bottom:4px}
h1{font-size:24px;font-weight:800;margin-bottom:4px}
.subtitle{font-size:13px;color:#6B7280;margin-bottom:32px}

/* stats cards — 2 columns */
.stats{display:grid;grid-template-columns:1fr 2fr;gap:12px;margin-bottom:20px}
.stat-card{background:#fff;border:1px solid #E5E7EB;border-radius:10px;padding:16px 18px}
.stat-label{font-size:10px;font-weight:700;color:#9CA3AF;letter-spacing:.6px;margin-bottom:6px}
.stat-value{font-size:22px;font-weight:800;color:#111;margin-bottom:2px}
.stat-sub{font-size:11px;color:#6B7280}

/* dist boxes */
.dist-row{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:28px}
.dist-box{background:#fff;border:1px solid #E5E7EB;border-radius:10px;padding:14px 16px}
.dist-title{font-size:10px;font-weight:700;color:#9CA3AF;letter-spacing:.6px;margin-bottom:10px}
.dist-item{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;font-size:12px}
.dist-bar-wrap{flex:1;height:5px;background:#F3F4F6;border-radius:3px;margin:0 8px;overflow:hidden}
.dist-bar-fill{height:100%;border-radius:3px;background:#7C3AED}
.dist-count{font-size:11px;font-weight:700;color:#374151;min-width:16px;text-align:right}

/* tabs */
.tabs{display:flex;gap:2px;margin-bottom:0;border-bottom:2px solid #E5E7EB}
.tab-btn{padding:10px 18px;font-size:12px;font-weight:600;color:#9CA3AF;background:none;border:none;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px}
.tab-btn.active{color:#7C3AED;border-bottom-color:#7C3AED}
.tab-content{display:none;background:#fff;border:1px solid #E5E7EB;border-top:none;border-radius:0 0 10px 10px;overflow:hidden}
.tab-content.active{display:block}

/* calendar table */
table{width:100%;border-collapse:collapse}
th{background:#F9FAFB;text-align:left;padding:10px 14px;font-size:10px;font-weight:700;color:#9CA3AF;letter-spacing:.5px;border-bottom:1px solid #E5E7EB}
td{padding:11px 14px;border-bottom:1px solid #F3F4F6;font-size:12px;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#FAFAFA}

/* badge base */
.badge{display:inline-flex;align-items:center;padding:1px 7px;border-radius:999px;font-size:10px;font-weight:600;white-space:nowrap}
/* funnel */
.b-awareness{background:#FEF3C714;color:#D97706;border:1px solid #D9770630}
.b-consideration{background:#EDE9FE;color:#7C3AED;border:1px solid #7C3AED30}
.b-conversion{background:#D1FAE514;color:#059669;border:1px solid #05966930}
/* geo */
.b-comparison{background:#DBEAFE;color:#2563EB;border:1px solid #2563EB30}
.b-problem_solving{background:#FEF9C3;color:#CA8A04;border:1px solid #CA8A0430}
.b-definition{background:#F3F4F6;color:#6B7280;border:1px solid #6B728030}
/* role */
.b-role-hub{background:#7C3AED14;color:#7C3AED;border:1px solid #7C3AED30}
.b-role-sub{background:#6B728014;color:#6B7280;border:1px solid #6B728030}
/* expansion_role */
.b-exp-심화{background:#2563EB14;color:#2563EB;border:1px solid #2563EB30}
.b-exp-보완{background:#D9770614;color:#D97706;border:1px solid #D9770630}
.b-exp-실행{background:#05966914;color:#059669;border:1px solid #05966930}
/* priority_highlight */
.b-hi-경쟁기회{background:#05966914;color:#059669;border:1px solid #05966930}
.b-hi-경쟁심화{background:#DC262614;color:#DC2626;border:1px solid #DC262630}
.b-hi-퍼널적합{background:#7C3AED14;color:#7C3AED;border:1px solid #7C3AED30}
.b-hi-방향적합{background:#7C3AED14;color:#7C3AED;border:1px solid #7C3AED30}
.b-hi-AI노출{background:#2563EB14;color:#2563EB;border:1px solid #2563EB30}
.b-hi-검색량{background:#D9770614;color:#D97706;border:1px solid #D9770630}
.b-hi-균형{background:#6B728014;color:#6B7280;border:1px solid #6B728030}
/* cutline */
.cutline{border-top:2px dashed #D1D5DB;margin:8px 0;position:relative}
.cutline::after{content:"컷라인";position:absolute;top:-9px;left:50%;transform:translateX(-50%);background:#fff;padding:0 8px;font-size:10px;color:#9CA3AF;font-weight:600}
/* cluster view */
.cluster-block{padding:16px 18px;border-bottom:1px solid #F3F4F6}
.cluster-block:last-child{border-bottom:none}
.cluster-name{font-size:13px;font-weight:700;color:#111;margin-bottom:12px}
.cluster-meta{font-size:11px;color:#9CA3AF;font-weight:400;margin-left:8px}
.content-row{display:flex;align-items:center;gap:8px;margin-bottom:7px;font-size:12px}
.dot-filled{color:#7C3AED;font-size:14px;line-height:1}
.dot-empty{color:#D1D5DB;font-size:14px;line-height:1}
.content-title-text{flex:1;color:#374151}
.content-title-text.waitlisted{color:#9CA3AF}
.score-bar-wrap{width:60px;height:5px;background:#F3F4F6;border-radius:3px;overflow:hidden;display:inline-block;vertical-align:middle}
.score-bar-fill{height:100%;border-radius:3px;background:#7C3AED}
.score-num{font-size:11px;font-weight:700;color:#6B7280;min-width:28px;text-align:right}
.link-hint{font-size:10px;color:#9CA3AF;margin-left:4px}
/* detail view — individual card navigation */
.detail-nav{display:flex;justify-content:space-between;align-items:center;padding:12px 18px;border-bottom:1px solid #F3F4F6}
.nav-btn{background:none;border:none;cursor:pointer;font-size:12px;font-weight:600;color:#7C3AED;padding:4px 0}
.nav-btn:disabled{color:#D1D5DB;cursor:default}
.nav-dots{display:flex;gap:4px;flex-wrap:wrap}
.nav-dot{width:24px;height:24px;border-radius:6px;border:none;font-size:11px;font-weight:600;cursor:pointer;background:#F3F4F6;color:#999}
.nav-dot.active{background:#7C3AED;color:#fff}
.detail-card{display:none;padding:24px 22px}
.detail-card.active{display:block}
.detail-empty{padding:40px 16px;text-align:center;font-size:14px;color:#999}
.d-meta{font-size:11px;color:#9CA3AF;font-weight:600;margin-bottom:4px}
.d-title{font-size:20px;font-weight:800;line-height:1.35;margin-bottom:14px}
.detail-badges{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:20px}
.detail-section{margin-bottom:14px;background:#FAFBFC;border:1px solid #F0F1F3;border-radius:8px;padding:14px 16px}
.detail-section-label{font-size:10px;font-weight:700;color:#7C3AED;letter-spacing:.6px;margin-bottom:8px}
.detail-section-text{font-size:13px;color:#374151;line-height:1.7}
.h2-list{list-style:none;padding:0;margin:0}
.h2-list li{display:flex;gap:0;background:#fff;border:1px solid #F0F1F3;border-radius:6px;margin-bottom:6px;overflow:hidden}
.h2-list li:last-child{margin-bottom:0}
.h2-num{display:flex;align-items:center;justify-content:center;min-width:56px;padding:8px 0;font-size:9px;font-weight:600;color:#C4B5FD;background:#FAFAFF;border-right:1px solid #F0F1F3;letter-spacing:.3px;flex-shrink:0}
.h2-body{flex:1;padding:8px 12px;font-size:13px;color:#374151;line-height:1.5}
/* week divider */
.week-row td{background:#F9FAFB;font-size:10px;font-weight:700;color:#9CA3AF;letter-spacing:.5px;padding:6px 14px;border-bottom:1px solid #E5E7EB}
/* content_status: update badge */
.b-update{background:#EFF6FF;color:#2563EB;border:1px solid #BFDBFE}
/* content_approach: data_driven badge */
.b-approach-data{background:#DBEAFE;color:#1D4ED8;border:1px solid #93C5FD}
/* skip card in cluster view */
.skip-card{background:#FFF7ED;border:1px solid #FED7AA;border-radius:10px;padding:14px;margin-bottom:12px}
.skip-card .lbl{font-size:10px;font-weight:700;color:#C2410C;letter-spacing:.8px;margin-bottom:4px}
.skip-card .txt{font-size:12px;color:#92400E;line-height:1.5}
.skip-card a{color:#C2410C;text-decoration:underline}
/* update existing content card in detail */
.ic-update{background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:14px 16px}
.ic-update .lbl{font-size:10px;font-weight:700;color:#1D4ED8;letter-spacing:.6px;margin-bottom:6px}
.ic-update .txt{font-size:12px;color:#1E3A5F;line-height:1.6}
.ic-update a{color:#2563EB;text-decoration:underline}
/* update existing content card in detail (top) */
/* NEW — data tag */
.data-tag{display:inline-flex;padding:1px 6px;border-radius:4px;font-size:9px;font-weight:600;background:#FEF3C7;color:#92400E;border:1px solid #FDE68A;margin-left:4px}
/* h2 description */
.h2-desc{font-size:11px;color:#9CA3AF;margin-top:4px;line-height:1.5}
/* NEW — question list in stat card */
.q-list{list-style:none;padding:0;margin:0}
.q-list li{font-size:12px;color:#374151;padding:4px 0;line-height:1.5}
.q-list .q-cluster{display:inline-flex;padding:1px 7px;border-radius:999px;font-size:10px;font-weight:600;background:#F3F4F6;color:#6B7280;border:1px solid #E5E7EB;margin-left:6px}
"""

# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

JS = """\
var currentDetail=null;
var totalDetails=0;
function switchTab(name){
  document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.remove('active')});
  document.querySelectorAll('.tab-content').forEach(function(c){c.classList.remove('active')});
  document.getElementById('tab-'+name).classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(function(b){
    if(b.getAttribute('data-tab')===name) b.classList.add('active');
  });
}
function showDetail(idx){
  currentDetail=idx;
  totalDetails=document.querySelectorAll('.detail-card').length;
  var empty=document.getElementById('detail-empty');
  var nav=document.getElementById('detail-nav');
  if(empty) empty.style.display='none';
  if(nav) nav.style.display='flex';
  document.querySelectorAll('.detail-card').forEach(function(c){c.classList.remove('active')});
  var el=document.getElementById('detail-'+idx);
  if(el) el.classList.add('active');
  // update dots
  document.querySelectorAll('.nav-dot').forEach(function(d,i){
    d.classList.toggle('active',i===idx);
  });
  // update prev/next
  var prev=document.getElementById('nav-prev');
  var next=document.getElementById('nav-next');
  if(prev) prev.disabled=(idx<=0);
  if(next) next.disabled=(idx>=totalDetails-1);
  // highlight calendar row
  document.querySelectorAll('#tab-cal tr[data-idx]').forEach(function(r){
    r.style.background=parseInt(r.getAttribute('data-idx'))===idx?'#F5F3FF':'';
  });
}
function goToDetail(idx){
  switchTab('detail');
  setTimeout(function(){showDetail(idx)},50);
}
function prevDetail(){if(currentDetail>0)showDetail(currentDetail-1)}
function nextDetail(){if(currentDetail<totalDetails-1)showDetail(currentDetail+1)}
"""

# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_header(data: dict[str, Any]) -> str:
    """Eyebrow, h1, subtitle, back link."""
    target = _esc(data["target_month"])
    year, month = target.split("-")
    sc = data["metadata"]["scheduled_count"]
    n_clusters = len(data.get("categories", []))
    return (
        f'<a href="index.html" class="back">&larr; 전체 목록으로</a>\n'
        f'<div class="eyebrow">WISHKET BLOG &middot; CONTENT STRATEGY</div>\n'
        f'<h1>위시켓 {year}년 {int(month)}월 콘텐츠 전략</h1>\n'
        f'<p class="subtitle">월간 {sc}건 발행 계획 &middot; '
        f'{n_clusters}개 클러스터 &middot; 규칙 기반 우선순위 산출</p>\n'
    )


def _build_stats(data: dict[str, Any]) -> str:
    """Two-column stats cards: scheduled count + input questions."""
    meta = data["metadata"]
    sc = meta["scheduled_count"]
    wl = meta["waitlist_count"]
    sk = meta.get("skip_seed_count", meta.get("skipped_count", 0))
    questions = data.get("input_questions", [])

    # Card 1: 예약 콘텐츠
    card1 = (
        '<div class="stat-card">\n'
        '  <div class="stat-label">예약 콘텐츠</div>\n'
        f'  <div class="stat-value">{sc}</div>\n'
        f'  <div class="stat-sub">대기 {wl}건 &middot; skip {sk}건</div>\n'
        '</div>\n'
    )

    # Card 2: 사용자 질문
    q_items = ""
    for i, q in enumerate(questions, 1):
        q_text = _esc(q["question"])
        q_cluster = _esc(q["cluster"])
        q_items += (
            f'    <li>{i}. {q_text}'
            f'  <span class="q-cluster">{q_cluster}</span></li>\n'
        )
    card2 = (
        '<div class="stat-card">\n'
        '  <div class="stat-label">사용자 질문</div>\n'
        f'  <ul class="q-list">\n{q_items}  </ul>\n'
        '</div>\n'
    )

    return f'<div class="stats">\n{card1}{card2}</div>\n'


def _build_distributions(data: dict[str, Any]) -> str:
    """Funnel, geo, cluster distribution boxes."""
    meta = data["metadata"]
    fs = meta.get("funnel_summary", {})
    gs = meta.get("geo_summary", {})
    cs = meta.get("cluster_summary", {})

    total_sched = max(meta["scheduled_count"], 1)

    def dist_box(title: str, items: list[tuple[str, int, str]]) -> str:
        rows = ""
        for label, count, colour in items:
            pct = int(count / total_sched * 100) if total_sched else 0
            style = f'style="width:{pct}%;background:{colour}"' if colour != "#7C3AED" else f'style="width:{pct}%"'
            rows += (
                f'    <div class="dist-item"><span>{_esc(label)}</span>'
                f'<div class="dist-bar-wrap"><div class="dist-bar-fill" {style}></div></div>'
                f'<span class="dist-count">{count}</span></div>\n'
            )
        return (
            f'  <div class="dist-box">\n'
            f'    <div class="dist-title">{_esc(title)}</div>\n{rows}'
            f'  </div>\n'
        )

    funnel_items = [(FUNNEL_KO.get(k, k), fs.get(k, 0), FUNNEL_COLOUR.get(k, "#7C3AED")) for k in FUNNEL_ORDER]
    geo_items = [(GEO_KO.get(k, k), gs.get(k, 0), GEO_COLOUR.get(k, "#7C3AED")) for k in GEO_ORDER]
    cluster_items = [(k, v, "#7C3AED") for k, v in cs.items()]

    return (
        '<div class="dist-row">\n'
        + dist_box("퍼널 분포", funnel_items)
        + dist_box("GEO 구조", geo_items)
        + dist_box("클러스터 분포", cluster_items)
        + '</div>\n'
    )


def _build_calendar(schedule: list[dict[str, Any]]) -> str:
    """Calendar tab content: table with week dividers."""
    rows = ""
    last_week: int | None = None

    for idx, item in enumerate(schedule):
        week = _month_week_number(item["publish_date"], schedule)
        if week != last_week:
            rows += f'    <tr class="week-row"><td colspan="5">{week}주차</td></tr>\n'
            last_week = week

        # Date cell
        pd = item["publish_date"]  # "2026-04-01"
        mm_dd = f'{pd[5:7]}/{pd[8:10]}'
        wd = _esc(item.get("weekday", ""))
        date_cell = f'<td><strong>{mm_dd}</strong><br><span style="font-size:10px;color:#9CA3AF">{wd}</span></td>'

        # Funnel + GEO cell
        funnel = item.get("funnel", "")
        geo = item.get("geo_type", "")
        funnel_label = FUNNEL_KO.get(funnel, funnel)
        geo_label = GEO_KO.get(geo, geo)
        fg_cell = (
            f'<td><span class="badge b-{_esc(funnel)}">{_esc(funnel_label)}</span><br>'
            f'<span class="badge b-{_esc(geo)}" style="margin-top:3px">{_esc(geo_label)}</span></td>'
        )

        # Title cell — clickable, with role/expansion badges
        role = item.get("role", "")
        role_label = ROLE_KO.get(role, role)
        exp = item.get("expansion_role")
        title_text = _esc(item.get("title", ""))

        badges_html = f'<span class="badge b-role-{_esc(role)}" style="margin-bottom:3px">{_esc(role_label)}</span>'
        if exp:
            badges_html += f' <span class="badge b-exp-{_esc(exp)}" style="margin-bottom:3px">{_esc(exp)}</span>'

        title_cell = (
            f'<td>{badges_html}<br>'
            f'<span onclick="goToDetail({idx})" style="cursor:pointer;color:#2563EB">{title_text}</span></td>'
        )

        # 유형 column: update / data_driven badges
        type_badges = ""
        if item.get("content_status") == "update":
            type_badges += '<span class="badge b-update">업데이트</span> '
        if item.get("content_approach") == "data_driven":
            type_badges += '<span class="badge b-approach-data">데이터 중심 콘텐츠</span> '
        type_cell = f"<td>{type_badges.strip()}</td>"

        # Cluster cell
        cluster = _esc(item.get("cluster", ""))
        cluster_cell = f'<td><span class="badge" style="background:#F3F4F6;color:#374151;border:1px solid #E5E7EB">{cluster}</span></td>'

        rows += f'    <tr data-idx="{idx}">{date_cell}{fg_cell}{title_cell}{type_cell}{cluster_cell}</tr>\n'

    return (
        '<div class="tab-content active" id="tab-cal">\n'
        '  <table>\n'
        '    <tr><th>발행일</th><th>퍼널 &middot; GEO</th><th>제목</th><th>유형</th><th>클러스터</th></tr>\n'
        f'{rows}'
        '  </table>\n'
        '</div>\n'
    )


def _build_cluster_view(
    schedule: list[dict[str, Any]],
    waitlist: list[dict[str, Any]],
    skip_seeds: list[dict[str, Any]],
    categories: list[str],
) -> str:
    """Cluster view tab."""

    # Group scheduled, waitlisted, skipped by cluster
    sched_by_cluster: dict[str, list[dict[str, Any]]] = {}
    wait_by_cluster: dict[str, list[dict[str, Any]]] = {}
    skip_by_cluster: dict[str, list[dict[str, Any]]] = {}

    for item in schedule:
        c = item.get("cluster", "")
        sched_by_cluster.setdefault(c, []).append(item)
    for item in waitlist:
        c = item.get("cluster", "")
        wait_by_cluster.setdefault(c, []).append(item)
    for item in skip_seeds:
        c = item.get("cluster", "")
        skip_by_cluster.setdefault(c, []).append(item)

    blocks = ""
    for cluster_name in categories:
        s_items = sched_by_cluster.get(cluster_name, [])
        w_items = wait_by_cluster.get(cluster_name, [])
        sk_items = skip_by_cluster.get(cluster_name, [])

        n_sched = len(s_items)
        n_wait = len(w_items)

        meta_parts = [f"선발 {n_sched}", f"대기 {n_wait}"]
        if sk_items:
            meta_parts.append("hub skip")

        block = (
            f'  <div class="cluster-block">\n'
            f'    <div class="cluster-name">{_esc(cluster_name)} '
            f'<span class="cluster-meta">{" / ".join(meta_parts)}</span></div>\n'
        )

        # Skip card
        for sk in sk_items:
            ec = sk.get("existing_content", {})
            sk_keyword = _esc(sk.get("keyword", ""))
            sk_reason = _esc(sk.get("skip_reason", ""))
            ec_url = _esc(ec.get("url", "")) if ec else ""
            ec_title = _esc(ec.get("title", "")) if ec else ""
            ec_date = _esc(ec.get("publish_date", "")) if ec else ""
            block += (
                f'    <div class="skip-card">\n'
                f'      <div class="lbl">SKIP &middot; 허브 콘텐츠</div>\n'
                f'      <div class="txt">\n'
                f'        <strong>{sk_keyword}</strong> &mdash; {sk_reason}<br>\n'
            )
            if ec_url:
                block += (
                    f'        <a href="{ec_url}" target="_blank">기존 글: {ec_title}</a>\n'
                    f'        <span style="font-size:10px;color:#9CA3AF;margin-left:4px">({ec_date} 발행)</span>\n'
                )
            block += '      </div>\n    </div>\n'

        # Scheduled items
        for item in s_items:
            role = item.get("role", "")
            exp = item.get("expansion_role")
            keyword = _esc(item.get("keyword", ""))
            score = item.get("priority_score", 0)
            highlight = item.get("priority_highlight", "")
            hi_cls = _highlight_class(highlight)
            pct = int(score * 10)

            role_label = ROLE_KO.get(role, role)
            badge = f'<span class="badge b-role-{_esc(role)}">{_esc(role_label)}</span>' if role == "hub" else ""
            if exp:
                badge = f'<span class="badge b-exp-{_esc(exp)}">{_esc(exp)}</span>'

            # Link hint
            link_text = ""
            ilh = item.get("internal_link_hint", "")
            shl = item.get("seed_h2_link", "")
            if shl:
                link_text = f' <span class="link-hint">&rarr; {_esc(shl)}</span>'
            elif ilh and role != "hub":
                # Try to extract a referenced H2 from hint
                pass

            block += (
                f'    <div class="content-row">\n'
                f'      <span class="dot-filled">&#9679;</span>\n'
                f'      {badge}\n'
                f'      <span class="content-title-text">{keyword}{link_text}</span>\n'
                f'      <div class="score-bar-wrap"><div class="score-bar-fill" style="width:{pct}%"></div></div>\n'
                f'      <span class="score-num">{score}</span>\n'
                f'      <span class="badge {hi_cls}">{_esc(highlight)}</span>\n'
                f'    </div>\n'
            )

        # Cutline
        if w_items:
            block += '    <div class="cutline"></div>\n'

        # Waitlisted items
        for item in w_items:
            exp = item.get("expansion_role")
            keyword = _esc(item.get("keyword", ""))
            score = item.get("priority_score", 0)
            highlight = item.get("priority_highlight", "")
            hi_cls = _highlight_class(highlight)
            pct = int(score * 10)

            badge = f'<span class="badge b-exp-{_esc(exp)}">{_esc(exp)}</span>' if exp else ""

            link_text = ""
            shl = item.get("seed_h2_link")
            if shl:
                link_text = f' <span class="link-hint">&rarr; {_esc(shl)}</span>'

            block += (
                f'    <div class="content-row">\n'
                f'      <span class="dot-empty">&#9675;</span>\n'
                f'      {badge}\n'
                f'      <span class="content-title-text waitlisted">{keyword}{link_text}</span>\n'
                f'      <div class="score-bar-wrap"><div class="score-bar-fill" style="width:{pct}%;background:#D1D5DB"></div></div>\n'
                f'      <span class="score-num" style="color:#D1D5DB">{score}</span>\n'
                f'      <span class="badge {hi_cls}" style="opacity:.4">{_esc(highlight)}</span>\n'
                f'    </div>\n'
            )

        block += '  </div>\n'
        blocks += block

    return f'<div class="tab-content" id="tab-cluster">\n{blocks}</div>\n'


def _build_detail_card(idx: int, item: dict[str, Any]) -> str:
    """Single detail card (individual card, one at a time)."""
    pd = item["publish_date"]
    mm_dd = f'{pd[5:7]}/{pd[8:10]}'
    wd = _esc(item.get("weekday", ""))
    title = _esc(item.get("title", ""))
    cluster = _esc(item.get("cluster", ""))

    role = item.get("role", "")
    exp = item.get("expansion_role")
    funnel = item.get("funnel", "")
    geo = item.get("geo_type", "")

    # 1. Meta line (date + cluster)
    meta = f'  <div class="d-meta">{mm_dd} ({wd}) &middot; {cluster}</div>\n'

    # 2. Title
    title_html = f'  <div class="d-title">{title}</div>\n'

    # 3. Badges
    role_label = ROLE_KO.get(role, role)
    badge_list = [f'<span class="badge b-role-{_esc(role)}">{_esc(role_label)}</span>']
    if exp:
        badge_list.append(f'<span class="badge b-exp-{_esc(exp)}">{_esc(exp)}</span>')
    if item.get("content_status") == "update":
        badge_list.append('<span class="badge b-update">업데이트</span>')
    if item.get("content_approach") == "data_driven":
        badge_list.append('<span class="badge b-approach-data">데이터 중심 콘텐츠</span>')
    badge_list.append(f'<span class="badge b-{_esc(funnel)}">{_esc(FUNNEL_KO.get(funnel, funnel))}</span>')
    badge_list.append(f'<span class="badge b-{_esc(geo)}">{_esc(GEO_KO.get(geo, geo))}</span>')

    badges = f'  <div class="detail-badges">{"".join(badge_list)}</div>\n'

    # 4. 업데이트 대상 (existing_content) — top
    update_section = ""
    ec = item.get("existing_content")
    if ec:
        ec_url = _esc(ec.get("url", ""))
        ec_title_text = _esc(ec.get("title", ""))
        ec_date = _esc(ec.get("publish_date", ""))
        ec_gap = _esc(ec.get("gap_analysis", ""))
        update_section = (
            '  <div class="ic-update" style="margin-bottom:14px">\n'
            '    <div class="lbl">업데이트 대상</div>\n'
            f'    <div class="txt"><a href="{ec_url}" target="_blank">{ec_title_text}</a>'
        )
        if ec_date:
            update_section += f' <span style="font-size:10px;color:#9CA3AF;margin-left:4px">({ec_date} 발행)</span>'
        if ec_gap:
            update_section += f'<br>{ec_gap}'
        update_section += '</div>\n  </div>\n'

    # 5. 발행 목적 (publishing_purpose)
    purpose = item.get("publishing_purpose", "")
    purpose_section = ""
    if purpose:
        purpose_section = (
            '  <div class="detail-section">\n'
            '    <div class="detail-section-label">발행 목적</div>\n'
            f'    <div class="detail-section-text">{_esc(purpose)}</div>\n'
            '  </div>\n'
        )

    # 6. 선정 이유 (editorial_summary)
    editorial = item.get("editorial_summary", "")
    editorial_section = ""
    if editorial:
        editorial_section = (
            '  <div class="detail-section">\n'
            '    <div class="detail-section-label">선정 이유</div>\n'
            f'    <div class="detail-section-text">{_esc(editorial)}</div>\n'
            '  </div>\n'
        )

    # 7. H2 구조
    h2_section = ""
    h2s = item.get("h2_structure", [])
    if h2s:
        h2_section = (
            '  <div class="detail-section">\n'
            '    <div class="detail-section-label">H2 구조</div>\n'
            '    <ul class="h2-list">\n'
        )
        for si, h2 in enumerate(h2s, 1):
            heading = _esc(h2.get("heading", ""))
            desc = h2.get("description", "")
            data_cands = h2.get("data_candidates", []) or []

            heading_line = f'<strong>{heading}</strong>'
            for dc in data_cands:
                heading_line += f'<span class="data-tag">{_esc(dc)}</span>'

            h2_section += f'      <li><span class="h2-num">section{si}</span><div class="h2-body">{heading_line}'
            if desc:
                h2_section += f'\n        <div class="h2-desc">{_esc(desc)}</div>'
            h2_section += '</div></li>\n'
        h2_section += '    </ul>\n  </div>\n'

    # 8. CTA 컨셉
    cta_section = ""
    cta = item.get("cta_suggestion", "")
    if cta:
        cta_section = (
            '  <div class="detail-section">\n'
            '    <div class="detail-section-label">CTA 컨셉</div>\n'
            f'    <div class="detail-section-text">{_esc(cta)}</div>\n'
            '  </div>\n'
        )

    # 10. 연관된 기존 위시켓 콘텐츠 (existing_wishket_urls) — bottom
    reference_section = ""
    ew = item.get("existing_wishket_urls", []) or []
    if ew:
        reference_section = '  <div class="detail-section">\n'
        reference_section += '    <div class="detail-section-label">연관된 기존 위시켓 콘텐츠</div>\n'
        for url in ew:
            url_esc = _esc(url)
            reference_section += f'    <div class="detail-section-text"><a href="{url_esc}" target="_blank" style="color:#7C3AED;text-decoration:underline">{url_esc}</a></div>\n'
        reference_section += '  </div>\n'

    # Card content: meta → title → badges → 업데이트 대상 → 발행 목적 → 선정 이유 → H2 → 내부 링크 → CTA → 연관 콘텐츠
    body = (
        meta
        + title_html
        + badges
        + update_section
        + purpose_section
        + editorial_section
        + h2_section
        + cta_section
        + reference_section
    )

    return f'<div class="detail-card" id="detail-{idx}">\n{body}</div>\n'


def _build_details(schedule: list[dict[str, Any]]) -> str:
    """Detail tab: navigation bar + individual cards (one visible at a time)."""
    n = len(schedule)

    # Navigation dots
    dots = ""
    for i in range(n):
        dots += f'<button class="nav-dot" onclick="showDetail({i})">{i + 1}</button>'

    nav = (
        f'<div class="detail-nav" id="detail-nav" style="display:none">\n'
        f'  <button class="nav-btn" id="nav-prev" onclick="prevDetail()" disabled>&larr; 이전</button>\n'
        f'  <div class="nav-dots">{dots}</div>\n'
        f'  <button class="nav-btn" id="nav-next" onclick="nextDetail()">다음 &rarr;</button>\n'
        f'</div>\n'
    )

    empty = '<div class="detail-empty" id="detail-empty">캘린더에서 콘텐츠를 선택하세요</div>\n'

    cards = ""
    for idx, item in enumerate(schedule):
        cards += _build_detail_card(idx, item)
    return f'<div class="tab-content" id="tab-detail">\n{nav}{empty}{cards}</div>\n'


# ---------------------------------------------------------------------------
# Full HTML assembly
# ---------------------------------------------------------------------------


def generate_dashboard(data: dict[str, Any]) -> str:
    """Generate the full dashboard HTML string."""
    schedule = data.get("schedule", [])
    waitlist = data.get("waitlist", [])
    skip_seeds = data.get("skip_seeds", [])
    categories = data.get("categories", [])
    target = _esc(data.get("target_month", ""))

    body_parts = [
        _build_header(data),
        _build_stats(data),
        _build_distributions(data),
        # Tabs
        '<div class="tabs">\n'
        '  <button class="tab-btn active" data-tab="cal" onclick="switchTab(\'cal\')">발행 캘린더</button>\n'
        '  <button class="tab-btn" data-tab="cluster" onclick="switchTab(\'cluster\')">클러스터 뷰</button>\n'
        '  <button class="tab-btn" data-tab="detail" onclick="switchTab(\'detail\')">콘텐츠 상세</button>\n'
        '</div>\n',
        _build_calendar(schedule),
        _build_cluster_view(schedule, waitlist, skip_seeds, categories),
        _build_details(schedule),
    ]

    return (
        '<!DOCTYPE html>\n<html lang="ko">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'<title>위시켓 {target} 콘텐츠 전략 대시보드</title>\n'
        f'<style>\n{CSS}</style>\n'
        '</head>\n<body>\n'
        '<div class="wrap">\n'
        + "\n".join(body_parts)
        + '\n</div><!-- /wrap -->\n\n'
        f'<script>\n{JS}</script>\n'
        '</body>\n</html>\n'
    )


# ---------------------------------------------------------------------------
# Index updater
# ---------------------------------------------------------------------------


def update_index(html_path: Path, data: dict[str, Any]) -> None:
    """Add or update the entry in docs/index.html for this dashboard."""
    index_path = html_path.parent / "index.html"
    target = data.get("target_month", "")
    year, month = target.split("-") if "-" in target else ("", "")
    filename = html_path.name
    ts = data.get("metadata", {}).get("timestamp", "")
    date_str = ts[:10].replace("-", ".") if ts else ""

    link_label = f"위시켓 {int(month)}월 콘텐츠 전략"

    new_row = (
        '<tr style="background:#F0FDF4">\n'
        f'<td style="padding:10px 14px"><a href="{_esc(filename)}" '
        f'style="color:#7C3AED;text-decoration:none;font-weight:700">{_esc(link_label)}</a> '
        f'<span style="background:#059669;color:#fff;padding:1px 8px;border-radius:999px;'
        f'font-size:10px;font-weight:600;margin-left:6px">최신</span></td>\n'
        f'<td style="padding:10px 14px;color:#6B7280;font-size:12px">{_esc(date_str)} 업데이트</td>\n'
        '</tr>'
    )

    if not index_path.exists():
        # Create minimal index
        index_html = (
            '<!DOCTYPE html>\n<html lang="ko">\n<head>\n'
            '<meta charset="UTF-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            '<title>콘텐츠 전략 대시보드</title>\n'
            '<style>\n'
            '*{margin:0;padding:0;box-sizing:border-box}\n'
            'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#111;'
            'background:#fff;max-width:800px;margin:0 auto;padding:40px 20px}\n'
            'h1{font-size:22px;font-weight:800;margin-bottom:6px}\n'
            '.sub{font-size:13px;color:#6B7280;margin-bottom:28px}\n'
            'table{width:100%;border-collapse:collapse;border:1px solid #E5E7EB;border-radius:8px;overflow:hidden}\n'
            'th{background:#F9FAFB;text-align:left;padding:10px 14px;font-size:11px;font-weight:700;'
            'color:#9CA3AF;letter-spacing:.5px;border-bottom:1px solid #E5E7EB}\n'
            'td{border-bottom:1px solid #F3F4F6}\n'
            'tr:last-child td{border-bottom:none}\n'
            'a:hover{text-decoration:underline!important}\n'
            '</style>\n</head>\n<body>\n'
            '<div style="font-size:11px;font-weight:700;color:#7C3AED;letter-spacing:1.2px;margin-bottom:4px">'
            'WISHKET BLOG &middot; CONTENT STRATEGY</div>\n'
            '<h1>콘텐츠 전략 대시보드</h1>\n'
            '<p class="sub">월별 발행 전략 대시보드 버전 목록</p>\n'
            '<table>\n'
            '<tr><th>콘텐츠 전략</th><th>업데이트</th></tr>\n'
            f'{new_row}\n'
            '</table>\n</body>\n</html>\n'
        )
        index_path.write_text(index_html, encoding="utf-8")
        return

    content = index_path.read_text(encoding="utf-8")

    # Remove "최신" badge from existing rows
    content = re.sub(
        r' <span style="background:#059669;color:#fff;padding:1px 8px;border-radius:999px;'
        r'font-size:10px;font-weight:600;margin-left:6px">최신</span>',
        "",
        content,
    )
    # Remove green background from previously "latest" row
    content = content.replace('<tr style="background:#F0FDF4">', "<tr>")

    # Check if this file already has an entry for this filename
    if filename in content:
        # Update existing row — replace the <tr> containing this filename
        pattern = r"<tr[^>]*>\s*<td[^>]*><a[^>]*" + re.escape(filename) + r".*?</tr>"
        content = re.sub(pattern, new_row, content, flags=re.DOTALL)
    else:
        # Insert new row after header row
        header_end = '<tr><th>콘텐츠 전략</th><th>업데이트</th></tr>'
        content = content.replace(header_end, header_end + "\n" + new_row)

    index_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate dashboard HTML from schedule JSON."
    )
    parser.add_argument("schedule_json", type=Path, help="Path to schedule JSON file")
    parser.add_argument(
        "--output", "-o", type=Path, default=None, help="Output HTML path"
    )
    args = parser.parse_args()

    schedule_path: Path = args.schedule_json
    if not schedule_path.exists():
        print(f"Error: {schedule_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(schedule_path, encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)

    html_content = generate_dashboard(data)

    output_path: Path = args.output if args.output else _parse_output_path(schedule_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding="utf-8")
    print(f"Dashboard written to {output_path}")

    # Update index
    update_index(output_path, data)
    print(f"Index updated at {output_path.parent / 'index.html'}")


if __name__ == "__main__":
    main()
