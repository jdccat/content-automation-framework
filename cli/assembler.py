#!/usr/bin/env python3
"""리서처 출력 JSON 조립기.

에이전트가 저장한 중간 파일(도구 출력) + decisions.json → 최종 출력 JSON 조립.
LLM이 전체 JSON을 생성하는 대신, 도구 결과를 스크립트로 조립하여 토큰 절약.

사용법:
  python cli/assembler.py <work_dir> <output_path>

work_dir 내 파일:
  decisions.json       — 에이전트가 생성 (시드 선택, 팬아웃 선택 등)
  nv_seed.json         — naver_volume 시드
  nv_seed2.json        — naver_volume 변형 (2차 배치, 선택)
  nv_fanout.json       — naver_volume 팬아웃
  nt_seed.json         — naver_trend 시드
  nt_fanout.json       — naver_trend 팬아웃
  gt_seed.json         — google_trend 시드 (429 시 없을 수 있음)
  gt_fanout.json       — google_trend 팬아웃 (429 시 없을 수 있음)
  ns_seed.json         — naver_search 시드
  nserp_seed.json      — naver_serp 시드
  geo_{서비스}_{n}.json — GEO 인용 (예: geo_chatgpt_1.json)
  serp_google.json     — 구글 SERP (에이전트가 WebSearch에서 추출)
  h2_seed.json         — 시드 H2 데이터
  h2_fanout.json       — 팬아웃 H2 데이터
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path


def _load(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _normalize(kw: str) -> str:
    """공백 제거 정규화 — naver_searchad API가 공백 없이 반환하므로."""
    return kw.replace(" ", "")


def _vol(nv_data: dict | None, keyword: str) -> dict:
    """naver_volume 결과에서 특정 키워드 볼륨 추출 (공백 무시 매칭)."""
    default = {"monthly_pc": 0, "monthly_mobile": 0, "monthly_total": 0}
    if not nv_data:
        return default
    norm = _normalize(keyword)
    for pool in ("input_keywords", "related_keywords"):
        for item in nv_data.get(pool, []):
            if _normalize(item.get("keyword", "")) == norm:
                return {
                    "monthly_pc": item.get("monthly_pc", 0),
                    "monthly_mobile": item.get("monthly_mobile", 0),
                    "monthly_total": item.get("monthly_total", 0),
                }
    return default


def _trend(data: dict | None, keyword: str, source: str = "naver") -> dict:
    """트렌드 결과에서 특정 키워드 추출."""
    default = {"average": 0.0, "direction": "stable", "series": []}
    if not data:
        return default
    if source == "google" and "trends" in data:
        data = data["trends"]
    entry = data.get(keyword)
    if not entry or isinstance(entry, str):
        return default
    return {
        "average": entry.get("average", 0.0),
        "direction": entry.get("direction", "stable"),
        "series": entry.get("series", []),
    }


def _trend_short(data: dict | None, keyword: str, source: str = "naver") -> dict:
    """트렌드 (series 제외 — 팬아웃/변형용)."""
    t = _trend(data, keyword, source)
    return {"average": t["average"], "direction": t["direction"]}


def assemble(work_dir: Path, output_path: Path) -> None:
    decisions = _load(work_dir / "decisions.json")
    if not decisions:
        print("ERROR: decisions.json not found in", work_dir, file=sys.stderr)
        sys.exit(1)

    # ── 도구 출력 로드 ─────────────────────────────────────────
    nv_seed = _load(work_dir / "nv_seed.json")
    nv_seed2 = _load(work_dir / "nv_seed2.json")
    nv_fanout = _load(work_dir / "nv_fanout.json")
    nt_seed = _load(work_dir / "nt_seed.json")
    nt_fanout = _load(work_dir / "nt_fanout.json")
    gt_seed = _load(work_dir / "gt_seed.json")
    gt_fanout = _load(work_dir / "gt_fanout.json")
    ns_seed = _load(work_dir / "ns_seed.json")
    nserp = _load(work_dir / "nserp_seed.json")
    serp_google = _load(work_dir / "serp_google.json")
    h2_seed = _load(work_dir / "h2_seed.json")
    h2_fanout = _load(work_dir / "h2_fanout.json")

    # naver_volume 배치 병합
    nv_merged: dict = {"input_keywords": [], "related_keywords": []}
    for nv in (nv_seed, nv_seed2):
        if nv and isinstance(nv, dict):
            nv_merged["input_keywords"].extend(nv.get("input_keywords", []))
            nv_merged["related_keywords"].extend(nv.get("related_keywords", []))

    seed_kw = decisions["seed_keyword"]
    variants = decisions.get("keyword_variants", [])
    geo_queries = decisions.get("geo_queries", [])

    # ── seed 볼륨 ──────────────────────────────────────────────
    seed_volume = _vol(nv_merged, seed_kw)
    variant_volumes = {v: _vol(nv_merged, v) for v in variants}

    # ── seed 트렌드 ────────────────────────────────────────────
    seed_naver_trend = _trend(nt_seed, seed_kw, "naver")
    seed_google_trend = _trend(gt_seed, seed_kw, "google")
    variant_trends = {}
    for v in variants:
        variant_trends[v] = {
            "naver": _trend_short(nt_seed, v, "naver"),
            "google": _trend_short(gt_seed, v, "google"),
        }

    # ── seed SERP ──────────────────────────────────────────────
    google_serp = serp_google if isinstance(serp_google, list) else []
    # H2 데이터 병합
    h2_map: dict[str, list] = {}
    if h2_seed and isinstance(h2_seed, list):
        for entry in h2_seed:
            url = entry.get("url", "")
            if url:
                h2_map[url] = entry.get("h2_headings", [])
    for item in google_serp:
        url = item.get("url", "")
        if url in h2_map:
            item["h2_headings"] = h2_map[url]
        elif "h2_headings" not in item:
            item["h2_headings"] = []

    naver_serp_items = []
    if ns_seed and isinstance(ns_seed, dict):
        for item in ns_seed.get("items", []):
            naver_serp_items.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "postdate": item.get("postdate", ""),
            })

    # ── SERP 피처 ──────────────────────────────────────────────
    google_features = decisions.get("google_serp_features", {
        "has_ai_overview": False,
        "has_featured_snippet": False,
        "has_paa": False,
    })
    naver_features = {
        "has_knowledge_snippet": False,
        "has_smart_block": False,
        "smart_block_components": [],
    }
    if nserp and isinstance(nserp, dict):
        naver_features = {
            "has_knowledge_snippet": nserp.get("knowledge_snippet", False),
            "has_smart_block": nserp.get("smart_block", False),
            "smart_block_components": nserp.get("smart_block_components", []),
        }

    # ── GEO 인용 ───────────────────────────────────────────────
    geo_citations: dict[str, dict] = {}
    for geo_file in sorted(work_dir.glob("geo_*.json")):
        geo_data = _load(geo_file)
        if not geo_data:
            continue
        parts = geo_file.stem.split("_")
        if len(parts) < 3:
            continue
        service = parts[1]
        try:
            query_idx = int(parts[2]) - 1
        except ValueError:
            continue
        query = geo_queries[query_idx] if query_idx < len(geo_queries) else geo_data.get("query", f"query_{query_idx}")

        if query not in geo_citations:
            geo_citations[query] = {}

        if "error" in geo_data:
            geo_citations[query][service] = {"error": geo_data["error"]}
        else:
            citations = []
            for detail in geo_data.get("citation_details", []):
                citations.append({
                    "url": detail.get("url", ""),
                    "title": detail.get("title", ""),
                    "is_wishket": "wishket" in detail.get("url", "").lower(),
                })
            if not citations:
                for url in geo_data.get("citations", []):
                    citations.append({
                        "url": url, "title": "",
                        "is_wishket": "wishket" in url.lower(),
                    })
            geo_citations[query][service] = {
                "answer_snippet": (geo_data.get("answer", "") or "")[:200],
                "citations": citations,
            }

    # ── related_keywords_raw ───────────────────────────────────
    related_raw: list[str] = []
    seen = set()
    for item in nv_merged.get("related_keywords", []):
        kw = item.get("keyword", "")
        if kw and kw not in seen:
            related_raw.append(kw)
            seen.add(kw)

    # ── fan_outs ───────────────────────────────────────────────
    fan_outs_meta = decisions.get("fan_outs", [])
    h2_fo_map: dict[str, list] = {}
    if h2_fanout and isinstance(h2_fanout, dict):
        h2_fo_map = h2_fanout
    elif h2_fanout and isinstance(h2_fanout, list):
        for item in h2_fanout:
            if "fanout_keyword" in item:
                h2_fo_map[item["fanout_keyword"]] = item.get("entries", [])

    fan_outs = []
    for fo in fan_outs_meta:
        fo_kw = fo["keyword"]
        top_comps = h2_fo_map.get(fo_kw, [])
        for comp in top_comps:
            if "h2_headings" not in comp:
                comp["h2_headings"] = []
        fan_outs.append({
            "keyword": fo_kw,
            "question": fo.get("question", ""),
            "relation": fo.get("relation", ""),
            "content_angle": fo.get("content_angle", ""),
            "volume": _vol(nv_fanout, fo_kw),
            "naver_trend": _trend_short(nt_fanout, fo_kw, "naver"),
            "google_trend": _trend_short(gt_fanout, fo_kw, "google"),
            "top_competitors": top_comps,
        })

    # ── metadata ───────────────────────────────────────────────
    tools_used = []
    tool_check = {
        "nv_": "naver_volume", "nt_": "naver_trend", "gt_": "google_trend",
        "ns_": "naver_search", "nserp_": "naver_serp",
    }
    for prefix, name in tool_check.items():
        if any(work_dir.glob(f"{prefix}*.json")):
            tools_used.append(name)
    if serp_google:
        tools_used.extend(["WebSearch", "WebFetch"])
    if h2_seed or h2_fanout:
        tools_used.append("web_fetch")
    geo_services = []
    for svc in ("chatgpt", "claude", "gemini"):
        if any(work_dir.glob(f"geo_{svc}_*.json")):
            geo_services.append(svc)
            tools_used.append(f"geo_{svc}")

    # ── 최종 조립 ──────────────────────────────────────────────
    output = {
        "input_question": decisions["input_question"],
        "intent": decisions["intent"],
        "content_direction": decisions["content_direction"],
        "seed_selection": decisions["seed_selection"],
        "seed": {
            "keyword": seed_kw,
            "keyword_variants": variants,
            "volume": seed_volume,
            "variant_volumes": variant_volumes,
            "naver_trend": seed_naver_trend,
            "google_trend": seed_google_trend,
            "variant_trends": variant_trends,
            "serp": {"google": google_serp, "naver": naver_serp_items},
            "serp_features": {"google": google_features, "naver": naver_features},
            "paa_questions": decisions.get("paa_questions", []),
            "h2_topics": decisions.get("h2_topics", []),
            "geo_citations": geo_citations,
            "related_keywords_raw": related_raw,
        },
        "fan_out_selection": decisions["fan_out_selection"],
        "fan_outs": fan_outs,
        "meta": {
            "assembled_at": datetime.now().isoformat(timespec="seconds"),
            "assembler_version": "1.1",
            "work_dir": str(work_dir),
            "seed_keyword_variants_count": len(variants),
            "fan_out_count": len(fan_outs),
            "tools_used": tools_used,
            "geo_services_checked": geo_services,
            "geo_queries_count": len(geo_queries),
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Assembled: {output_path}")
    print(f"  Seed: {seed_kw} ({len(variants)} variants)")
    print(f"  Fan-outs: {len(fan_outs)}")
    print(f"  GEO: {len(geo_queries)} queries × {len(geo_services)} services")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <work_dir> <output_path>", file=sys.stderr)
        sys.exit(1)
    assemble(Path(sys.argv[1]), Path(sys.argv[2]))
