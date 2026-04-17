#!/usr/bin/env python3
"""Render writer MD + assembler YAML into final HTML using a fixed template.

Usage:
    .venv/bin/python cli/render_article.py <writer_md> <assembler_yaml> [--output <path>]
"""

from __future__ import annotations

import argparse
import html as html_mod
import datetime
import json
import logging
import re
import sys
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


class _DateEncoder(json.JSONEncoder):
    """Handle datetime.date/datetime objects in JSON serialization."""
    def default(self, o: Any) -> Any:
        if isinstance(o, (datetime.date, datetime.datetime)):
            return o.isoformat()
        return super().default(o)

import markdown
import yaml
from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"

# ---------------------------------------------------------------------------
# 1. Input Loading
# ---------------------------------------------------------------------------


def load_writer_md(path: Path) -> tuple[dict[str, Any], str]:
    """Load writer markdown. Returns (frontmatter_dict, body_text)."""
    text = path.read_text(encoding="utf-8")
    # Split YAML frontmatter
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()
            return fm, body
    return {}, text.strip()


def load_assembler_yaml(path: Path) -> dict[str, Any]:
    """Load assembler YAML output."""
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


# ---------------------------------------------------------------------------
# 2. Markdown → HTML Conversion
# ---------------------------------------------------------------------------


def md_to_html(md_text: str) -> str:
    """Convert markdown to HTML using python-markdown with tables."""
    return markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code"],
        output_format="html",
    )


# ---------------------------------------------------------------------------
# 3. Body Parsing — split into intro + H2 sections
# ---------------------------------------------------------------------------


def parse_body(md_body: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Parse MD body into (title, intro_md, [(h2_heading, section_md), ...])."""
    lines = md_body.split("\n")
    title = ""
    intro_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_h2: str | None = None
    current_lines: list[str] = []

    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            if current_h2 is not None:
                sections.append((current_h2, current_lines))
            elif intro_lines or current_lines:
                intro_lines = current_lines
            current_h2 = line[3:].strip()
            current_lines = []
            continue
        current_lines.append(line)

    # Last section
    if current_h2 is not None:
        sections.append((current_h2, current_lines))
    elif current_lines:
        intro_lines = current_lines

    intro_md = "\n".join(intro_lines).strip()
    section_tuples = [(h, "\n".join(ls).strip()) for h, ls in sections]
    return title, intro_md, section_tuples


# ---------------------------------------------------------------------------
# 3-1. Blockquote Box Conversion
# ---------------------------------------------------------------------------

BOX_STYLES = {
    "📌": {"border": "#2E6BAA", "bg": "#F0F6FF", "title_color": "#1A1A1A", "text_color": "#1A1A1A"},
    "💡": {"border": "#2E6BAA", "bg": "#F0F6FF", "title_color": "#1A1A1A", "text_color": "#1A1A1A"},
    "✅": {"border": "#2E6BAA", "bg": "#F0F6FF", "title_color": "#1A1A1A", "text_color": "#1A1A1A"},
    "💬": {"border": "#2E6BAA", "bg": "#F0F6FF", "title_color": "#1A1A1A", "text_color": "#1A1A1A"},
}

# Writer가 이모지 없이 볼드 제목으로 작성하는 박스 유형.
# 첫 <strong> 텍스트가 이 중 하나로 시작하면 styled box로 변환.
BOLD_TITLE_BOX_PATTERNS = [
    "위시켓 매니저 Tip",
    "용어 정의",
    "체크리스트",
    "FAQ",
]
DEFAULT_BOX_STYLE = {"border": "#2E6BAA", "bg": "#F0F6FF", "title_color": "#1A1A1A", "text_color": "#1A1A1A"}


CASE_BADGE_COLORS = {
    "low": {"bg": "#e8f5e9", "color": "#2e7d32"},
    "mid": {"bg": "#e3f2fd", "color": "#1565c0"},
    "high": {"bg": "#fce4ec", "color": "#c62828"},
}
CASE_BADGE_LABELS = {"low": "저가 사례", "mid": "중가 사례", "high": "고가 사례"}


def _parse_case_cards(paragraphs: list) -> list[dict]:
    """Parse case card paragraphs into structured data.

    Handles two formats:
    1. Each line is a separate <p> (ideal markdown rendering)
    2. Multiple lines inside a single <p> separated by <br/> or newlines
       (python-markdown collapses consecutive > lines into one <p>)

    Each case has 3 lines:
    - **Title** | Price
    - Meta info (platform · duration · scope)
    - Description
    """
    # First, flatten all paragraphs into individual lines
    all_lines: list[str] = []
    for p in paragraphs:
        # Get inner HTML and split by <br/> or newlines
        inner = str(p)
        # Remove <p> wrapper
        inner = re.sub(r'^<p>|</p>$', '', inner.strip())
        # Split on <br/>, <br>, or actual newlines
        parts = re.split(r'<br\s*/?>|\n', inner)
        for part in parts:
            clean = re.sub(r'<[^>]+>', '', part).strip()  # strip tags for detection
            if clean:
                all_lines.append(part.strip())

    cases: list[dict] = []
    current: dict = {}
    line_in_case = 0

    for line in all_lines:
        # Strip HTML for text matching
        text = re.sub(r'<[^>]+>', '', line).strip()
        if not text:
            continue

        # New case starts with bold title containing |
        has_strong = '<strong>' in line
        if has_strong and '|' in text:
            if current:
                cases.append(current)
            # Extract title from <strong>
            title_match = re.search(r'<strong>([^<]+)</strong>', line)
            title = title_match.group(1).strip() if title_match else text.split('|')[0].strip()
            price = text.split('|', 1)[1].strip()
            current = {"title": title, "price": price, "meta": "", "desc": ""}
            line_in_case = 1
        elif line_in_case == 1 and current:
            current["meta"] = text
            line_in_case = 2
        elif line_in_case >= 2 and current:
            current["desc"] = (current["desc"] + " " + text).strip() if current["desc"] else text
            line_in_case = 3

    if current:
        cases.append(current)

    return cases


def _render_case_cards(cases: list[dict]) -> str:
    """Render parsed cases as styled card HTML."""
    # Assign tiers by price order
    tiers = ["low", "mid", "high", "high"]  # 4th+ also high
    cards = []
    for i, case in enumerate(cases):
        tier = tiers[min(i, len(tiers) - 1)]
        badge = CASE_BADGE_COLORS[tier]
        label = CASE_BADGE_LABELS[tier]

        cards.append(
            f'<div style="border:1px solid #E5E7EB;border-radius:14px;padding:18px 20px;background:#fff;">\n'
            f'  <div style="display:flex;align-items:flex-start;gap:12px;margin-bottom:10px;">\n'
            f'    <span style="font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;'
            f'white-space:nowrap;background:{badge["bg"]};color:{badge["color"]};">{label}</span>\n'
            f'    <span style="font-size:15.5px;font-weight:700;color:#0d0d0d;line-height:1.4;">'
            f'{_esc(case["title"])}</span>\n'
            f'  </div>\n'
            f'  <div style="font-size:20px;font-weight:800;color:#2477F3;margin-bottom:4px;">'
            f'{_esc(case["price"])}</div>\n'
            f'  <div style="font-size:12.5px;color:#999;margin-bottom:10px;">'
            f'{_esc(case["meta"])}</div>\n'
            f'  <div style="font-size:14.5px;color:#444;line-height:2.0;">'
            f'{_esc(case["desc"])}</div>\n'
            f'</div>'
        )

    return (
        '<div style="display:flex;flex-direction:column;gap:14px;margin:30px 0;">\n'
        + "\n".join(cards)
        + "\n</div>"
    )


def convert_blockquote_boxes(html_content: str) -> str:
    """Convert blockquote elements with emoji prefixes into styled boxes or case cards."""
    soup = BeautifulSoup(html_content, "html.parser")
    for bq in soup.find_all("blockquote"):
        text = bq.get_text(strip=True)

        # ── Case cards (📋 or **프로젝트 사례**) ──
        if text.startswith("📋") or text.startswith("프로젝트 사례"):
            paragraphs = bq.find_all("p")
            if not paragraphs:
                continue
            # The first <p> may contain both the 📋 title AND first case lines
            # (markdown collapses consecutive > lines into one <p>).
            # We pass ALL paragraphs and let the parser skip non-case lines.
            cases = _parse_case_cards(paragraphs)
            if cases:
                card_html = _render_case_cards(cases)
                new_tag = BeautifulSoup(card_html, "html.parser")
                bq.replace_with(new_tag)
            continue

        # ── Standard boxes (📌💡✅💬 or bold-title patterns) ──
        emoji = None
        for e in BOX_STYLES:
            if text.startswith(e):
                emoji = e
                break

        paragraphs = bq.find_all("p")
        if not paragraphs:
            continue

        # Bold-title detection: first <strong> matches known patterns
        title_strong = paragraphs[0].find("strong")
        bold_title_match = False
        if not emoji and title_strong:
            strong_text = title_strong.get_text(strip=True)
            for pattern in BOLD_TITLE_BOX_PATTERNS:
                if strong_text.startswith(pattern):
                    bold_title_match = True
                    break

        if not emoji and not bold_title_match:
            continue

        style = BOX_STYLES.get(emoji, DEFAULT_BOX_STYLE)

        title_text = title_strong.get_text(strip=True) if title_strong else paragraphs[0].get_text(strip=True).lstrip(emoji or "").strip()

        # python-markdown collapses consecutive > lines into a single <p>.
        # Body may live inside the first <p> (after the title line) AND/OR
        # in subsequent <p>/<ul>/<ol> children of the blockquote.
        body_parts = []

        # 1) Extract body from the first <p> after the title line
        first_p_html = str(paragraphs[0])
        # Remove <p>...</p> wrapper
        first_p_inner = re.sub(r"^<p>|</p>$", "", first_p_html.strip())
        # Split on newlines — first line is title, rest is body
        first_p_lines = first_p_inner.split("\n")
        body_from_first_p = "\n".join(first_p_lines[1:]).strip()
        if body_from_first_p:
            # Convert list-like lines (- item or - [ ] item) into <ul><li>
            list_lines = [l for l in body_from_first_p.split("\n") if l.strip()]
            if all(l.strip().startswith("- ") for l in list_lines):
                _li_prefix = re.compile(r"^-\s+(\[.\]\s+)?")
                items = "".join(
                    "<li>" + _li_prefix.sub("", l.strip()) + "</li>"
                    for l in list_lines
                )
                body_from_first_p = f"<ul>{items}</ul>"
            else:
                body_from_first_p = "<br/>".join(
                    l.strip() for l in body_from_first_p.split("\n") if l.strip()
                )
            body_parts.append(body_from_first_p)

        # 2) Collect subsequent <p> tags — detect inline list items
        for p in paragraphs[1:]:
            p_inner = p.decode_contents()
            p_lines = [l for l in p_inner.split("\n") if l.strip()]
            # Check if any lines look like list items (- ... or - [ ] ...)
            list_lines = [l for l in p_lines if re.match(r"^\s*-\s", l.strip())]
            if list_lines:
                # Split into heading (non-list) and list parts
                parts = []
                current_items: list[str] = []
                for line in p_lines:
                    stripped = line.strip()
                    if re.match(r"^-\s", stripped):
                        # Strip "- [ ] " or "- [x] " or "- " prefix
                        item_text = re.sub(r"^-\s+(\[.\]\s+)?", "", stripped)
                        current_items.append(item_text)
                    else:
                        if current_items:
                            items_html = "".join(f"<li>{it}</li>" for it in current_items)
                            parts.append(f"<ul>{items_html}</ul>")
                            current_items = []
                        parts.append(f"<p>{stripped}</p>")
                if current_items:
                    items_html = "".join(f"<li>{it}</li>" for it in current_items)
                    parts.append(f"<ul>{items_html}</ul>")
                body_parts.append("\n".join(parts))
            else:
                body_parts.append(str(p))

        # 3) Collect non-<p> children (ul, ol, etc.)
        for child in bq.children:
            if hasattr(child, "name") and child.name and child.name not in ("p",):
                body_parts.append(str(child))

        # Style <strong> tags in body
        body_html = "\n".join(body_parts)
        body_html = re.sub(
            r"<strong>([^<]+)</strong>",
            r'<span style="font-weight:700;color:{tc};">\1</span>'.format(tc=style["title_color"]),
            body_html,
        )

        box_html = (
            f'<div style="background:{style["bg"]};border-left:3px solid {style["border"]};'
            f'border-radius:0 14px 14px 0;padding:18px 20px;margin:30px 0;">\n'
            f'<div style="font-size:13px;font-weight:700;color:{style["title_color"]};'
            f'margin-bottom:10px;display:flex;align-items:center;gap:6px;">'
            f'{(emoji + " ") if emoji else ""}{_esc(title_text)}</div>\n'
            f'<div style="font-size:14.5px;color:{style["text_color"]};line-height:2.0;">'
            f'{body_html}</div>\n'
            f'</div>'
        )

        new_tag = BeautifulSoup(box_html, "html.parser")
        bq.replace_with(new_tag)

    return str(soup)


# ---------------------------------------------------------------------------
# 4. Table Post-processing — GEO Template
# ---------------------------------------------------------------------------


def _find_caption_text(table_tag: Tag) -> str | None:
    """Extract caption for a table.

    Sources (checked in order):
    1. A colspan row inside the table itself.
    2. A <p> whose entire text is <strong> (bold-only paragraph before table).
    Never consume H3 — those are section headings, not table titles.
    """
    # Source 1: colspan row inside table
    first_row = table_tag.find("tr")
    if first_row:
        cells = first_row.find_all(["th", "td"])
        if len(cells) == 1:
            cell = cells[0]
            colspan = cell.get("colspan")
            if colspan and int(colspan) > 1:
                text = cell.get_text(strip=True)
                first_row.decompose()
                return text

    # Source 2: bold-only <p> immediately before table
    prev = table_tag.find_previous_sibling()
    if prev and prev.name == "p":
        strong = prev.find("strong")
        if strong and strong.get_text(strip=True) == prev.get_text(strip=True):
            text = prev.get_text(strip=True)
            prev.decompose()
            return text
    return None


def _has_emphasis(td: Tag) -> bool:
    """Check if a <td> contains <strong> wrapping most of its content."""
    strong = td.find("strong")
    if not strong:
        return False
    return len(strong.get_text(strip=True)) > len(td.get_text(strip=True)) * 0.5


def apply_geo_table_style(
    html_content: str, section_heading: str = "", date: str = ""
) -> str:
    """Transform plain <table> tags into v4 GEO-optimized class-based tables."""
    soup = BeautifulSoup(html_content, "html.parser")
    tables = soup.find_all("table")

    for table in tables:
        caption_text = _find_caption_text(table)

        # Get column count
        first_row = table.find("tr")
        if not first_row:
            continue
        cols = len(first_row.find_all(["th", "td"]))

        # Fallback caption from header row
        if not caption_text:
            header_cells = first_row.find_all(["th", "td"])
            col_names = [c.get_text(strip=True) for c in header_cells if c.get_text(strip=True)]
            if col_names:
                caption_text = " · ".join(col_names[:3])
                if section_heading:
                    caption_text = f"{section_heading} — {caption_text}"

        # ── wrapper: .tbl-wrap ──
        new_wrapper = soup.new_tag("div", **{"class": "tbl-wrap"})

        # ── table.geo ──
        new_table = soup.new_tag("table", **{"class": "geo"})

        # Caption (visually hidden, present for GEO)
        caption = soup.new_tag("caption")
        caption.string = caption_text or ""
        new_table.append(caption)

        # Colgroup — smart widths by column count
        col_widths: list[int] = {
            2: [25, 75],
            3: [22, 39, 39],
            4: [14, 27, 27, 32],
        }.get(cols, [14] + [(86) // max(cols - 1, 1)] * max(cols - 1, 1))
        colgroup = soup.new_tag("colgroup")
        for w in col_widths:
            colgroup.append(soup.new_tag("col", style=f"width:{w}%"))
        new_table.append(colgroup)

        # Thead
        thead = table.find("thead")
        header_row = thead.find("tr") if thead else table.find("tr")
        new_thead = soup.new_tag("thead")
        if header_row:
            new_tr = soup.new_tag("tr")
            for th in header_row.find_all(["th", "td"]):
                new_th = soup.new_tag("th", scope="col")
                new_th.string = th.get_text(strip=True)
                new_tr.append(new_th)
            new_thead.append(new_tr)
        new_table.append(new_thead)

        # Tbody
        tbody_tag = table.find("tbody")
        if tbody_tag:
            data_rows = tbody_tag.find_all("tr")
        else:
            all_rows = table.find_all("tr")
            data_rows = all_rows[1:] if len(all_rows) > 1 else []

        new_tbody = soup.new_tag("tbody")
        for row in data_rows:
            cells = row.find_all(["td", "th"])
            new_row = soup.new_tag("tr")
            for ci, cell in enumerate(cells):
                inner = "".join(str(c) for c in cell.children)
                if ci == 0:
                    new_cell = soup.new_tag("th", scope="row")
                else:
                    new_cell = soup.new_tag("td")
                for _node in list(BeautifulSoup(inner, "html.parser").contents):
                    new_cell.append(_node)
                new_row.append(new_cell)
            new_tbody.append(new_row)
        new_table.append(new_tbody)

        # Tfoot — now controlled by assembler YAML (table_footnotes)
        # No automatic tfoot insertion here.

        new_wrapper.append(new_table)
        table.replace_with(new_wrapper)

    return str(soup)


# ---------------------------------------------------------------------------
# 4-B. Table Footnotes (assembler-controlled)
# ---------------------------------------------------------------------------


def apply_table_footnotes(html_content: str, footnotes: list[dict]) -> str:
    """Insert tfoot into tables based on assembler YAML table_footnotes.

    Each footnote dict has:
      - after_h2: int — which H2 section (1-based) the table belongs to
      - text: str — footnote text to display
    """
    if not footnotes:
        return html_content

    soup = BeautifulSoup(html_content, "html.parser")

    # Build a map: h2_index -> footnote text
    fn_map: dict[int, str] = {}
    for fn in footnotes:
        h2_idx = fn.get("after_h2")
        text = fn.get("text", "")
        if h2_idx and text:
            fn_map[h2_idx] = text

    if not fn_map:
        return html_content

    # Find all H2 elements to determine section boundaries
    h2_tags = soup.find_all("h2")

    for h2_idx, footnote_text in fn_map.items():
        if h2_idx < 1 or h2_idx > len(h2_tags):
            continue

        h2_tag = h2_tags[h2_idx - 1]

        # Find the first .tbl-wrap table after this H2 (before the next H2)
        next_h2 = h2_tags[h2_idx] if h2_idx < len(h2_tags) else None
        current = h2_tag.find_next("div", class_="tbl-wrap")
        if not current:
            continue
        # Ensure this table is before the next H2
        if next_h2 and current.find_previous("h2") != h2_tag:
            continue

        table = current.find("table", class_="geo")
        if not table:
            continue

        # Check if tfoot already exists
        if table.find("tfoot"):
            continue

        # Count columns
        first_row = table.find("tr")
        if not first_row:
            continue
        cols = len(first_row.find_all(["th", "td"]))

        # Create tfoot
        new_tfoot = soup.new_tag("tfoot")
        tfoot_tr = soup.new_tag("tr")
        tfoot_td = soup.new_tag("td", colspan=str(cols))
        tfoot_td.string = footnote_text
        tfoot_tr.append(tfoot_td)
        new_tfoot.append(tfoot_tr)
        table.append(new_tfoot)

    return str(soup)


# ---------------------------------------------------------------------------
# 5. Image Guide Insertion
# ---------------------------------------------------------------------------

_IMAGE_GUIDE_TYPE_LABELS = {
    "process_diagram": "프로세스 도해",
    "screenshot": "웹사이트 캡처",
    "infographic": "인포그래픽",
}

_IMAGE_GUIDE_TEMPLATE = """
<table class="guide-box" style="width:calc(100% + 40px);margin-left:-20px;border:2px solid #E5E7EB;border-collapse:collapse;margin-top:30px;margin-bottom:30px;">
<tr><td style="padding:16px 24px;border:none;font-size:13.5px;background:#FAFAFA;">
<p style="margin:0 0 8px 0;font-weight:700;">📷 이미지 제안</p>
<ul style="margin:0;padding-left:18px;list-style:disc;">
<li style="margin-bottom:4px;"><b>유형:</b> {type_label}</li>
<li style="margin-bottom:4px;"><b>설명:</b> {description}</li>
<li style="margin-bottom:0;"><b>alt:</b> {alt}</li>
</ul>
</td></tr>
</table>"""



def render_image_guide(guide: dict, heading: str) -> str:
    """Render a single image guide from YAML data."""
    guide_type = guide.get("type", "infographic")
    type_label = _IMAGE_GUIDE_TYPE_LABELS.get(guide_type, guide_type)
    return _IMAGE_GUIDE_TEMPLATE.format(
        heading=_esc(heading),
        type_label=type_label,
        description=_esc(guide.get("description", "")),
        alt=_esc(guide.get("alt", "")),
    )


# ---------------------------------------------------------------------------
# 6. Internal Link Insertion
# ---------------------------------------------------------------------------


def insert_internal_links(html_content: str, links: list[dict]) -> str:
    """Insert <a> tags for internal links based on near_text matching."""
    soup = BeautifulSoup(html_content, "html.parser")
    for link in links:
        anchor = link.get("anchor", "")
        url = link.get("url", "")
        if not anchor or not url:
            continue
        # Find text nodes containing the anchor text, skip if already in <a>
        for text_node in soup.find_all(string=re.compile(re.escape(anchor))):
            if text_node.parent and text_node.parent.name == "a":
                continue
            new_html = str(text_node).replace(
                anchor, f'<a href="{_esc(url)}">{anchor}</a>', 1
            )
            text_node.replace_with(BeautifulSoup(new_html, "html.parser"))
            break  # Only first occurrence
    return str(soup)


def insert_bridge_link(html_content: str, bridge: dict | None) -> str:
    """Replace last '위시켓' text in content with bridge link."""
    if not bridge or not bridge.get("url"):
        return html_content
    url = bridge["url"]
    # Find last occurrence of 위시켓 not already inside an <a> or guide-box
    soup = BeautifulSoup(html_content, "html.parser")
    targets = []
    for text_node in soup.find_all(string=re.compile("위시켓")):
        parent = text_node.parent
        if parent and parent.name == "a":
            continue
        # Skip if inside a guide-box (image guide / thumbnail)
        in_guide = False
        for ancestor in text_node.parents:
            if isinstance(ancestor, Tag) and "guide-box" in (ancestor.get("class") or []):
                in_guide = True
                break
        if in_guide:
            continue
        targets.append(text_node)
    if targets:
        last = targets[-1]
        new_html = str(last).replace(
            "위시켓", f'<a href="{_esc(url)}">위시켓</a>', 1
        )
        last.replace_with(BeautifulSoup(new_html, "html.parser"))
    return str(soup)


# ---------------------------------------------------------------------------
# 7. H2 Anchor Slugify
# ---------------------------------------------------------------------------


def _slugify_ko(text: str) -> str:
    """Create a URL-safe slug from Korean/mixed text.

    Keeps hangul, ascii alphanumeric, replaces spaces/punctuation with hyphens.
    """
    text = unicodedata.normalize("NFC", text.strip().lower())
    text = re.sub(r"[^\w\s가-힣-]", "", text)  # keep hangul, word chars, spaces
    text = re.sub(r"[\s_]+", "-", text)  # spaces/underscores → hyphens
    text = re.sub(r"-+", "-", text).strip("-")
    return text


# ---------------------------------------------------------------------------
# 8. Component Renderers
# ---------------------------------------------------------------------------


def _esc(text: str | None) -> str:
    if text is None:
        return ""
    return html_mod.escape(str(text))


def render_tldr(tldr_text: str) -> str:
    """Render TLDR callout box as a short paragraph."""
    escaped = _esc(tldr_text).strip()
    return (
        '<div class="summary-box">\n'
        '<p>💡 핵심 요약</p>\n'
        f'<p>{escaped}</p>\n'
        '</div>'
    )


def render_thumbnail(thumb: dict) -> str:
    """Render thumbnail guide box."""
    style = thumb.get("style", "solid_color")
    if style == "solid_color":
        lines = thumb.get("lines", ["", ""])
        return (
            '<table class="guide-box" style="width:100%;border:1px solid #D1D5DB;border-collapse:collapse;margin:20px 0;">\n'
            '<tr><td style="background:#F9FAFB;padding:16px 20px;border:1px solid #D1D5DB;">\n'
            '<p style="margin:0 0 10px 0;font-weight:700;font-size:16px;">🖼 썸네일 제작 가이드</p>\n'
            '<p style="margin:0 0 4px 0;"><b>스타일:</b> 단색 배경 + 텍스트</p>\n'
            f'<p style="margin:0 0 4px 0;"><b>배경색:</b> {_esc(thumb.get("bg_color", ""))}</p>\n'
            '<p style="margin:0 0 4px 0;"><b>제목 텍스트:</b></p>\n'
            f'<p style="margin:0 0 4px 0;">　1줄: {_esc(lines[0] if lines else "")}</p>\n'
            f'<p style="margin:0 0 4px 0;">　2줄: {_esc(lines[1] if len(lines) > 1 else "")}</p>\n'
            f'<p style="margin:0 0 4px 0;"><b>장식 요소:</b> {_esc(thumb.get("decoration", ""))}</p>\n'
            f'<p style="margin:0;"><b>로고:</b> {_esc(thumb.get("logo", "wishket 로고 좌상단"))}</p>\n'
            "</td></tr>\n</table>"
        )
    else:  # photo_overlay
        return (
            '<table class="guide-box" style="width:100%;border:1px solid #D1D5DB;border-collapse:collapse;margin:20px 0;">\n'
            '<tr><td style="background:#F9FAFB;padding:16px 20px;border:1px solid #D1D5DB;">\n'
            '<p style="margin:0 0 10px 0;font-weight:700;font-size:16px;">🖼 썸네일 제작 가이드</p>\n'
            '<p style="margin:0 0 4px 0;"><b>스타일:</b> 실사 배경 + 텍스트 오버레이</p>\n'
            f'<p style="margin:0 0 4px 0;"><b>배경 구도:</b> {_esc(thumb.get("bg_description", ""))}</p>\n'
            f'<p style="margin:0 0 4px 0;"><b>오버레이 텍스트:</b> {_esc(thumb.get("overlay_text", ""))}</p>\n'
            f'<p style="margin:0;"><b>로고:</b> {_esc(thumb.get("logo", "wishket 로고 좌상단"))}</p>\n'
            "</td></tr>\n</table>"
        )


def render_related(related: list[dict]) -> str:
    """Render '함께 읽으면 좋은 콘텐츠' section."""
    if not related:
        return ""
    items = "\n".join(
        f'  <li><a href="{_esc(r["url"])}">{_esc(r["title"])}</a></li>' for r in related
    )
    return f'<h3>함께 읽으면 좋은 콘텐츠</h3>\n<ul>\n{items}\n</ul>'


def _ensure_utm(url: str, medium: str, slug: str, date: str) -> str:
    """Add UTM parameters to URL if not already present."""
    if "utm_source" in url:
        return url
    sep = "&" if "?" in url else "?"
    date_short = date.replace("-", "")[2:]  # YYMMDD
    return f"{url}{sep}utm_source=blog_webflow&utm_medium={medium}&utm_campaign={date_short}_{slug}"


def render_cta_banner(cta: dict, slug: str, date: str) -> str:
    """Render final CTA as a large gradient box with scenarios."""
    if not cta:
        return ""
    url = cta.get("url", "")
    full_url = _ensure_utm(url, "cta_banner", slug, date)

    headline = _esc(cta.get("headline", ""))
    button_text = _esc(cta.get("button_text", ""))
    eyebrow = _esc(cta.get("eyebrow", "다음 단계"))

    # Build scenarios HTML
    scenarios = cta.get("scenarios", [])
    scenarios_html = ""
    if scenarios:
        items = []
        for s in scenarios:
            label = _esc(s.get("label", ""))
            desc = _esc(s.get("description", ""))
            items.append(
                f'<div style="background:rgba(255,255,255,0.12);border-radius:10px;'
                f'padding:14px 16px;font-size:14.5px;line-height:1.65;">'
                f'<span style="font-weight:700;display:block;margin-bottom:4px;'
                f'font-size:13px;opacity:0.85;">{label}</span>{desc}</div>'
            )
        scenarios_html = (
            '<div style="display:flex;flex-direction:column;gap:12px;margin-bottom:24px;">'
            + "\n".join(items)
            + "</div>"
        )

    sub = _esc(cta.get("sub", ""))
    sub_html = f'<p style="font-size:15px;opacity:0.85;margin:0 0 24px 0;">{sub}</p>' if sub and not scenarios else ""

    return (
        '<div style="background:linear-gradient(135deg,#5E9ADE 0%,#7BB2E8 100%);'
        'border-radius:16px;padding:32px 30px;color:#fff;margin:40px 0 0;">\n'
        f'<div style="font-size:12px;font-weight:600;letter-spacing:0.8px;'
        f'opacity:0.75;margin-bottom:10px;text-transform:uppercase;">{eyebrow}</div>\n'
        f'<h3 style="font-size:20px;font-weight:800;line-height:1.45;'
        f'margin-bottom:20px;color:#fff;letter-spacing:-0.3px;">{headline}</h3>\n'
        f'{scenarios_html}{sub_html}'
        f'<div style="text-align:center;">'
        f'<a href="{_esc(full_url)}" style="display:inline-block;background:#fff;'
        f'color:#2477F3;font-size:15px;font-weight:800;padding:13px 28px;'
        f'border-radius:10px;text-decoration:none;">{button_text}</a></div>\n'
        '</div>'
    )


def render_inline_cta(cta: dict, slug: str, date: str) -> str:
    """Render inline CTA as a compact button box."""
    text = _esc(cta.get("text", ""))
    url = cta.get("url", "")
    full_url = _ensure_utm(url, "inline_cta", slug, date)
    button_text = _esc(cta.get("button_text", "더 알아보기"))

    return (
        '<div style="background:linear-gradient(135deg,#5E9ADE 0%,#7BB2E8 100%);'
        'border-radius:14px;padding:20px 24px;margin:30px 0;color:#fff;'
        'display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;">\n'
        f'<p style="font-size:15px;color:#fff;line-height:1.65;margin:0;flex:1;">{text}</p>\n'
        f'<a href="{_esc(full_url)}" style="display:inline-block;background:#fff;'
        f'color:#2477F3;font-size:14px;font-weight:700;padding:10px 18px;'
        f'border-radius:10px;white-space:nowrap;text-decoration:none;">{button_text}</a>\n'
        '</div>'
    )


def render_faq(faq: list[dict]) -> str:
    """Render FAQ section."""
    items = []
    for item in faq:
        q = _esc(item.get("question", item.get("q", "")))
        a = _esc(item.get("answer", item.get("a", "")))
        items.append(
            f'<div class="faq-item">\n'
            f'  <div class="faq-q"><span class="q-mark">Q.</span>{q}</div>\n'
            f'  <div class="faq-a">{a}</div>\n'
            f'</div>'
        )
    return (
        '<h2>자주 묻는 질문</h2>\n'
        '<div class="faq-list">\n'
        + "\n".join(items) +
        '\n</div>'
    )


def render_jsonld(data: dict, title: str, sections: list[tuple[str, str]]) -> str:
    """Generate BlogPosting + FAQPage JSON-LD as a copyable code block.

    Output is a visible <pre> block (not <script>) so the user can copy
    the JSON-LD and paste it into Webflow's Custom Code field.
    """
    author = data.get("author", {})
    if isinstance(author, str):
        author_name = author.split("/")[0].strip()
    else:
        author_name = author.get("name", "")

    # Section anchors for hasPart
    section_parts = [
        {"@type": "WebPageElement", "name": h, "url": f"#{_slugify_ko(h)}"}
        for h, _ in sections
    ]

    blog_posting = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": title,
        "description": data.get("meta_description", ""),
        "datePublished": data.get("date", ""),
        "dateModified": data.get("date", ""),
        "author": {"@type": "Person", "name": author_name},
        "publisher": {
            "@type": "Organization",
            "name": "위시켓",
            "url": "https://www.wishket.com",
        },
        "mainEntityOfPage": {"@type": "WebPage"},
        "keywords": data.get("tag", ""),
        "articleSection": data.get("category", ""),
        "hasPart": section_parts,
    }

    # FAQPage from faq field
    faq_items = data.get("faq", [])
    faq_ld = None
    if faq_items:
        faq_ld = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": item.get("question", item.get("q", "")),
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": item.get("answer", item.get("a", "")),
                    },
                }
                for item in faq_items
            ],
        }

    # Build copyable code block
    blog_json = json.dumps(blog_posting, ensure_ascii=False, indent=2, cls=_DateEncoder)
    script_blog = (
        '&lt;script type="application/ld+json"&gt;\n'
        f"{_esc(blog_json)}\n"
        "&lt;/script&gt;"
    )

    parts = [script_blog]
    if faq_ld:
        faq_json = json.dumps(faq_ld, ensure_ascii=False, indent=2, cls=_DateEncoder)
        script_faq = (
            '\n&lt;script type="application/ld+json"&gt;\n'
            f"{_esc(faq_json)}\n"
            "&lt;/script&gt;"
        )
        parts.append(script_faq)

    code_content = "".join(parts)
    return (
        '<h3 style="font-size:18px;font-weight:600;margin:32px 0 12px 0;">'
        'JSON-LD (Custom Code에 붙여넣기)</h3>\n'
        '<pre style="background:#1E293B;color:#E2E8F0;padding:16px;'
        'border-radius:8px;overflow-x:auto;font-size:12px;line-height:1.5;'
        f'white-space:pre-wrap;">{code_content}</pre>'
    )


def _copyable_block(label: str, code: str) -> str:
    """Render a single copyable code block with label and copy button."""
    return (
        f'<div style="margin:16px 0;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">'
        f'<span style="font-size:14px;font-weight:600;color:#374151;">{_esc(label)}</span>'
        f'<button onclick="copyCodeBlock(this)" '
        f'style="background:#2477F3;color:#fff;border:none;padding:4px 12px;'
        f'border-radius:4px;font-size:12px;font-weight:600;cursor:pointer;">'
        f'복사</button></div>'
        f'<pre style="background:#1E293B;color:#E2E8F0;'
        f'padding:12px;border-radius:8px;overflow-x:auto;font-size:11px;'
        f'line-height:1.4;white-space:pre-wrap;max-height:200px;overflow-y:auto;">'
        f'{_esc(code)}</pre></div>\n'
    )


_GEO_TABLE_CSS = (
    "<style>\n"
    ".tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:1.5rem 0;"
    "border:1px solid #E5E7EB;border-radius:12px;background:#fff}\n"
    "table.geo{width:100%;border-collapse:collapse;border-spacing:0;font-size:14px;"
    "line-height:1.6;table-layout:fixed}\n"
    "table.geo caption{position:absolute;width:1px;height:1px;overflow:hidden;"
    "clip:rect(0,0,0,0);white-space:nowrap}\n"
    "table.geo thead th{padding:12px 18px;font-size:12.5px;font-weight:700;"
    "letter-spacing:.01em;text-align:left;border-bottom:1px solid #D1D1D9;"
    "background:#F6F6F9;vertical-align:bottom}\n"
    "table.geo thead th:first-child{color:#82828E;border-right:1px solid #D1D1D9}\n"
    "table.geo thead th:not(:first-child){color:#1A1A23}\n"
    "table.geo tbody th[scope=row]{padding:14px 18px;font-size:14px;font-weight:600;"
    "color:#1A1A23;text-align:left;border-bottom:1px solid #ECECF0;"
    "border-right:1px solid #ECECF0;background:#fff;vertical-align:top;word-break:keep-all}\n"
    "table.geo tbody td{padding:14px 18px;color:#3A3A47;border-bottom:1px solid #ECECF0;"
    "vertical-align:top;word-break:keep-all}\n"
    "table.geo tbody tr:nth-child(even) td,"
    "table.geo tbody tr:nth-child(even) th[scope=row]{background:#F9F9FB}\n"
    "table.geo tbody tr:hover td,"
    "table.geo tbody tr:hover th[scope=row]{background:#F0F0F5}\n"
    "table.geo tfoot td{padding:9px 18px;font-size:12px;color:#9CA3AF;"
    "border-top:1px solid #ECECF0;background:#F9F9FB}\n"
    "table.geo tfoot time{font-weight:500}\n"
    "@media(max-width:640px){table.geo{table-layout:auto}"
    "table.geo thead th,table.geo tbody th[scope=row],"
    "table.geo tbody td{padding:11px 14px;font-size:13px}"
    "table.geo tfoot td{padding:8px 14px}}\n"
    "</style>\n"
)


def render_embed_code_blocks(content_html: str, cta_banner_html: str) -> str:
    """Extract tables + CTA from rendered HTML and show as copyable code blocks."""
    parts = [
        '<h3 style="font-size:18px;font-weight:600;margin:32px 0 12px 0;">'
        'Embed 코드 (붙여넣기용)</h3>\n'
    ]

    # Tables
    soup = BeautifulSoup(content_html, "html.parser")
    wraps = soup.find_all("div", class_="tbl-wrap")
    for i, wrap in enumerate(wraps, 1):
        table = wrap.find("table")
        if not table:
            continue
        caption = table.find("caption")
        label = caption.get_text(strip=True) if caption else f"표 {i}"
        # Embed code = <style> + <div.tbl-wrap> as one block
        embed_code = _GEO_TABLE_CSS + str(wrap)
        parts.append(_copyable_block(label, embed_code))

    # CTA banner
    if cta_banner_html and cta_banner_html.strip():
        parts.append(_copyable_block("CTA 배너", cta_banner_html.strip()))

    return "".join(parts)


def render_cms_meta(data: dict) -> str:
    """Render CMS metadata table."""
    author = data.get("author", {})
    if isinstance(author, str):
        author_str = author
    else:
        author_str = f'{author.get("name", "")} / {author.get("title", "")} — {author.get("intro", "")}'

    fields = [
        ("써머리", data.get("summary", "")),
        ("slug", data.get("slug", "")),
        ("날짜", data.get("date", "")),
        ("카테고리", data.get("category", "")),
        ("태그", data.get("tag", "")),
        ("메타 디스크립션", data.get("meta_description", "")),
        ("Author", author_str),
        ("TLDR", data.get("tldr", "")),
    ]

    rows = ""
    for label, value in fields:
        rows += (
            "<tr>\n"
            f'  <td style="border:1px solid #E2E8F0;padding:8px 12px;background:#F8FAFC;'
            f'font-weight:600;width:140px;">{_esc(label)}</td>\n'
            f'  <td style="border:1px solid #E2E8F0;padding:8px 12px;">{_esc(value)}</td>\n'
            "</tr>\n"
        )

    return (
        '<table style="width:100%;border-collapse:collapse;margin:16px 0;">\n'
        f"{rows}</table>"
    )


# ---------------------------------------------------------------------------
# 8. Main Assembly
# ---------------------------------------------------------------------------


def _parse_h2_index(value: str | int) -> int:
    """Extract 1-based H2 index from various formats: int, 'H2_3', 'H2_3_text'."""
    if isinstance(value, int):
        return value
    match = re.match(r"H2_(\d+)", str(value))
    return int(match.group(1)) if match else 0


def normalize_assembler_data(data: dict) -> dict:
    """Normalize assembler YAML to match schema expectations.

    LLM output may use variant field names or nesting structures.
    This function maps them to what the renderer expects.
    """

    # ── content nesting: hoist content.* fields to top level ──
    content = data.pop("content", None)
    if isinstance(content, dict):
        for key in ("tldr", "image_guides", "internal_links", "inline_cta",
                     "related", "cta", "bridge_link"):
            if key in content and key not in data:
                data[key] = content[key]

    # ── thumbnail ──
    thumb = data.get("thumbnail", {})
    if isinstance(thumb, dict):
        if "background_color" in thumb and "bg_color" not in thumb:
            thumb["bg_color"] = thumb.pop("background_color")
        if "title_line1" in thumb and "lines" not in thumb:
            thumb["lines"] = [thumb.pop("title_line1", ""), thumb.pop("title_line2", "")]
        if "logo_position" in thumb and "logo" not in thumb:
            pos = thumb.pop("logo_position")
            thumb["logo"] = f"wishket 로고 {pos}" if pos else "wishket 로고 좌상단"
        # decoration: dict → string
        dec = thumb.get("decoration")
        if isinstance(dec, dict):
            thumb["decoration"] = dec.get("type", "geometric_blocks")

    # ── cta: button_url → url, subtext → sub ──
    cta = data.get("cta")
    if isinstance(cta, dict):
        if "button_url" in cta and "url" not in cta:
            cta["url"] = cta.pop("button_url")
        if "subtext" in cta and "sub" not in cta:
            cta["sub"] = cta.pop("subtext")

    # ── inline_cta: list → dict, position_after → after_h2 ──
    inline = data.get("inline_cta")
    if isinstance(inline, list) and inline:
        inline = inline[0]  # take first item
        data["inline_cta"] = inline
    if isinstance(inline, dict):
        if "position_after" in inline and "after_h2" not in inline:
            inline["after_h2"] = _parse_h2_index(inline.pop("position_after"))
        # link_text + text → merged text (renderer uses text only)
        if "link_text" in inline and "text" not in inline:
            inline["text"] = inline.pop("link_text")

    # ── internal_links: anchor_text → anchor ──
    for il in data.get("internal_links", []):
        if "anchor_text" in il and "anchor" not in il:
            il["anchor"] = il.pop("anchor_text")

    # ── author: string → dict ──
    author = data.get("author")
    if isinstance(author, str) and "/" in author:
        # Format: "name / title — intro"
        parts = author.split("/", 1)
        name = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if "—" in rest:
            title_part, intro = rest.split("—", 1)
            data["author"] = {"name": name, "title": title_part.strip(), "intro": intro.strip()}
        elif " — " in rest:
            title_part, intro = rest.split(" — ", 1)
            data["author"] = {"name": name, "title": title_part.strip(), "intro": intro.strip()}
        else:
            data["author"] = {"name": name, "title": rest, "intro": ""}

    # ── image_guides: position_after → after_h2, type normalization ──
    for ig in data.get("image_guides", []):
        if ig.get("skip"):
            continue
        if "position_after" in ig and "after_h2" not in ig:
            ig["after_h2"] = _parse_h2_index(ig.pop("position_after"))
        if ig.get("type") == "generated_image":
            ig["type"] = ig.get("image_type", "infographic")

    # filter out skipped image guides
    data["image_guides"] = [ig for ig in data.get("image_guides", []) if not ig.get("skip")]

    return data


def render_article(writer_md_path: Path, assembler_yaml_path: Path) -> str:
    """Main render pipeline: writer MD + assembler YAML → HTML string."""
    # Load inputs
    _fm, md_body = load_writer_md(writer_md_path)
    asm = load_assembler_yaml(assembler_yaml_path)
    asm = normalize_assembler_data(asm)

    # Parse body
    title, intro_md, sections = parse_body(md_body)
    title = title or asm.get("title", "")
    slug = asm.get("slug", "")
    date = asm.get("date", "")

    # Build image guide index: h2_index (1-based) → list of guides
    ig_index: dict[int, list[dict]] = {}
    for ig in asm.get("image_guides", []):
        h2_num = ig.get("after_h2", 0)
        ig_index.setdefault(h2_num, []).append(ig)

    # Build inline CTA
    inline_cta = asm.get("inline_cta")
    inline_cta_h2 = inline_cta.get("after_h2", 0) if inline_cta else 0

    # Process sections
    section_htmls = []
    for i, (heading, section_md) in enumerate(sections, 1):
        section_html = md_to_html(section_md)
        section_html = convert_blockquote_boxes(section_html)
        section_html = apply_geo_table_style(section_html, section_heading=heading, date=date)

        # Image guides for this section
        for ig in ig_index.get(i, []):
            section_html += render_image_guide(ig, heading)

        # Inline CTA
        if inline_cta and inline_cta_h2 == i:
            section_html += render_inline_cta(inline_cta, slug, date)

        h2_id = _slugify_ko(heading)
        section_htmls.append(f'<h2 id="{h2_id}">{_esc(heading)}</h2>\n{section_html}')

    # Combine content
    intro_html = md_to_html(intro_md) if intro_md else ""
    intro_html = convert_blockquote_boxes(intro_html)
    intro_html = apply_geo_table_style(intro_html, date=date)
    full_content = intro_html + "\n\n" + "\n\n".join(section_htmls)

    # Insert internal links
    internal_links = asm.get("internal_links", [])
    full_content = insert_internal_links(full_content, internal_links)

    # Insert bridge link
    bridge_link = asm.get("bridge_link")
    full_content = insert_bridge_link(full_content, bridge_link)

    # Apply table footnotes (assembler-controlled)
    table_footnotes = asm.get("table_footnotes", [])
    full_content = apply_table_footnotes(full_content, table_footnotes)

    # Render components
    tldr_html = render_tldr(asm.get("tldr", ""))
    thumbnail_html = render_thumbnail(asm.get("thumbnail", {}))
    related_html = render_related(asm.get("related", []))
    cta_banner_html = render_cta_banner(asm.get("cta", {}), slug, date)
    faq_html = render_faq(asm.get("faq", []))
    cms_meta_html = render_cms_meta(asm)
    jsonld_html = render_jsonld(asm, title, sections)
    table_code_html = render_embed_code_blocks(full_content, cta_banner_html)

    # Jinja2 render
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
    )
    template = env.get_template("article.html")

    build_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    return template.render(
        title=title,
        date=date,
        build_ts=build_ts,
        jsonld_html=jsonld_html,
        thumbnail_html=thumbnail_html,
        tldr_html=tldr_html,
        content_html=full_content,
        related_html=related_html,
        cta_banner_html=cta_banner_html,
        faq_html=faq_html,
        cms_meta_html=cms_meta_html,
        table_code_html=table_code_html,
    )


# ---------------------------------------------------------------------------
# 9. HTML Validation
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


class _TagChecker(HTMLParser):
    """Detect unclosed / mismatched tags using stdlib html.parser."""

    # Tags that are self-closing (void elements) — no closing tag expected.
    VOID_TAGS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, int]] = []  # (tag, line)
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() not in self.VOID_TAGS:
            self.stack.append((tag.lower(), self.getpos()[0]))

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in self.VOID_TAGS:
            return
        # Walk the stack backwards to find a match
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag_l:
                # Everything above this on the stack is unclosed
                for j in range(len(self.stack) - 1, i, -1):
                    unclosed_tag, unclosed_line = self.stack[j]
                    self.errors.append(
                        f"Unclosed <{unclosed_tag}> (opened near line {unclosed_line})"
                    )
                self.stack.pop(i)
                # Remove the items we reported
                self.stack = self.stack[:i] + self.stack[i:]
                return
        self.errors.append(f"Unexpected closing </{tag_l}> at line {self.getpos()[0]}")

    def finish(self) -> list[str]:
        for tag, line in self.stack:
            self.errors.append(f"Unclosed <{tag}> (opened near line {line})")
        return self.errors


def validate_html(html_content: str) -> dict[str, Any]:
    """Validate rendered HTML and auto-fix what we can.

    Returns:
        {"auto_fixed": [...], "warnings": [...], "clean_html": str}
    """
    auto_fixed: list[str] = []
    warnings: list[str] = []
    clean = html_content

    # ------------------------------------------------------------------
    # 1. Tag open/close check
    # ------------------------------------------------------------------
    checker = _TagChecker()
    try:
        checker.feed(html_content)
        tag_errors = checker.finish()
    except Exception:
        tag_errors = []
    for err in tag_errors:
        warnings.append(f"[tag-mismatch] {err}")

    # ------------------------------------------------------------------
    # 2. Empty elements — remove empty <p>, <td>, <li>
    # ------------------------------------------------------------------
    _EMPTY_RE = re.compile(
        r"<(p|td|li)(\s[^>]*)?>(\s|&nbsp;|&#160;)*</(p|td|li)>",
        re.IGNORECASE,
    )
    while True:
        match = _EMPTY_RE.search(clean)
        if not match:
            break
        tag_name = match.group(1).lower()
        auto_fixed.append(f"[empty-element] Removed empty <{tag_name}>: {match.group(0)[:80]}")
        clean = clean[:match.start()] + clean[match.end():]

    # ------------------------------------------------------------------
    # 3. Internal anchor links — verify targets exist
    # ------------------------------------------------------------------
    href_re = re.compile(r'href="#([^"]+)"', re.IGNORECASE)
    id_re = re.compile(r'\bid=["\']([^"\']+)["\']', re.IGNORECASE)

    all_ids = {m.group(1) for m in id_re.finditer(clean)}
    for m in href_re.finditer(clean):
        target = m.group(1)
        if target not in all_ids:
            warnings.append(f"[broken-anchor] href=\"#{target}\" has no matching id in document")

    # ------------------------------------------------------------------
    # 4. Table column consistency
    # ------------------------------------------------------------------
    table_re = re.compile(r"<table[^>]*>(.*?)</table>", re.DOTALL | re.IGNORECASE)
    row_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    cell_re = re.compile(r"<(th|td)", re.IGNORECASE)

    for table_match in table_re.finditer(clean):
        table_html = table_match.group(1)
        rows = row_re.findall(table_html)
        if not rows:
            continue
        col_counts = [len(cell_re.findall(r)) for r in rows]
        header_cols = col_counts[0] if col_counts else 0
        for idx, cnt in enumerate(col_counts[1:], 2):
            if cnt != header_cols:
                # Extract a snippet for identification
                snippet = table_match.group(0)[:60].replace("\n", " ")
                warnings.append(
                    f"[table-col-mismatch] Row {idx} has {cnt} cells, "
                    f"header has {header_cols} (table: {snippet}...)"
                )

    # ------------------------------------------------------------------
    # 5. contenteditable body must contain text
    # ------------------------------------------------------------------
    ce_re = re.compile(
        r'<([a-z][a-z0-9]*)\s[^>]*contenteditable=["\']true["\'][^>]*>(.*?)</\1>',
        re.DOTALL | re.IGNORECASE,
    )
    for m in ce_re.finditer(clean):
        inner = re.sub(r"<[^>]+>", "", m.group(2))
        if not inner.strip():
            tag_name = m.group(1)
            warnings.append(
                f"[empty-contenteditable] <{tag_name} contenteditable=\"true\"> has no text content"
            )

    return {"auto_fixed": auto_fixed, "warnings": warnings, "clean_html": clean}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _derive_output_path(writer_md: Path) -> Path:
    """Derive output path from writer MD filename.

    Per-run 구조: 입력 파일과 같은 디렉토리에 HTML을 생성한다.
    """
    stem = writer_md.stem.replace("draft_", "assembled_")
    return writer_md.parent / f"{stem}.html"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render writer MD + assembler YAML into final HTML."
    )
    parser.add_argument("writer_md", type=Path, help="Path to writer markdown file")
    parser.add_argument("assembler_yaml", type=Path, help="Path to assembler YAML file")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output HTML path")
    args = parser.parse_args()

    if not args.writer_md.exists():
        print(f"Error: {args.writer_md} not found", file=sys.stderr)
        sys.exit(1)
    if not args.assembler_yaml.exists():
        print(f"Error: {args.assembler_yaml} not found", file=sys.stderr)
        sys.exit(1)

    html_content = render_article(args.writer_md, args.assembler_yaml)

    # Validate and auto-fix
    result = validate_html(html_content)
    for fix in result["auto_fixed"]:
        print(f"[auto-fix] {fix}", file=sys.stderr)
    for warn in result["warnings"]:
        print(f"[warning] {warn}", file=sys.stderr)
    if result["auto_fixed"]:
        html_content = result["clean_html"]

    output_path = args.output or _derive_output_path(args.writer_md)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding="utf-8")
    print(f"Rendered: {output_path}")


if __name__ == "__main__":
    main()
