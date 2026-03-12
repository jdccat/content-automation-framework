"""리서치 유닛: 검색량 + 추이 수집."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from core.agents.researcher.parser import _chunk, _normalize_keyword

logger = logging.getLogger(__name__)

SafeToolCall = Callable[..., Awaitable]


async def collect_volumes(
    reps: list[str],
    *,
    all_keywords: list[str] | None = None,
    existing_volumes: dict[str, int] | None = None,
    existing_volumes_pc: dict[str, int] | None = None,
    existing_volumes_mobile: dict[str, int] | None = None,
    safe_tool_call: SafeToolCall,
    naver_keyword_volume_fn: Callable,
    google_keyword_trend_fn: Callable,
    naver_keyword_trend_fn: Callable,
) -> dict[str, dict]:
    """대표 키워드 + 전체 키워드에 대해 볼륨/추이 수집.

    stage2.py의 _stage2a_volume()을 이동.
    """
    volume_targets = all_keywords if all_keywords else reps
    logger.info("볼륨 조회 대상: %d개 (대표 %d개)", len(volume_targets), len(reps))

    s1_vol = existing_volumes or {}
    s1_pc = existing_volumes_pc or {}
    s1_mob = existing_volumes_mobile or {}

    result: dict[str, dict] = {}
    for kw in volume_targets:
        nk = _normalize_keyword(kw)
        result[kw] = {
            "naver_volume": s1_vol.get(nk, 0),
            "naver_volume_pc": s1_pc.get(nk, 0),
            "naver_volume_mobile": s1_mob.get(nk, 0),
            "google_trend_avg": 0.0,
            "naver_trend_avg": 0.0,
            "google_direction": "stable",
            "naver_direction": "stable",
        }
    for kw in reps:
        if kw not in result:
            nk = _normalize_keyword(kw)
            result[kw] = {
                "naver_volume": s1_vol.get(nk, 0),
                "naver_volume_pc": s1_pc.get(nk, 0),
                "naver_volume_mobile": s1_mob.get(nk, 0),
                "google_trend_avg": 0.0,
                "naver_trend_avg": 0.0,
                "google_direction": "stable",
                "naver_direction": "stable",
            }

    # normalized → original key 역매핑
    norm_to_keys: dict[str, list[str]] = {}
    for kw in result:
        n = _normalize_keyword(kw)
        norm_to_keys.setdefault(n, []).append(kw)

    # Stage 1에서 이미 볼륨 있는 키워드 제외
    need_query = [kw for kw in volume_targets if result[kw]["naver_volume"] == 0]

    # 띄어쓰기 제거 변형 추가
    nospace_to_orig: dict[str, list[str]] = {}
    query_set: list[str] = []
    seen_q: set[str] = set()
    for kw in need_query:
        nk = _normalize_keyword(kw)
        if nk not in seen_q:
            query_set.append(kw)
            seen_q.add(nk)
        nospace = nk.replace(" ", "")
        if nospace != nk and nospace not in seen_q:
            query_set.append(nospace)
            seen_q.add(nospace)
            nospace_to_orig.setdefault(nospace, []).append(kw)

    logger.info(
        "searchad 쿼리 대상: %d개 (+변형 %d개, 프리필: %d개 스킵)",
        len(need_query), len(query_set) - len(need_query),
        len(volume_targets) - len(need_query),
    )

    # naver_searchad — 순차 (429 방지)
    searchad_results = []
    for i, batch in enumerate(_chunk(query_set, 5)):
        if i > 0:
            await asyncio.sleep(1)
        raw = await safe_tool_call(
            f"naver_searchad({batch})",
            naver_keyword_volume_fn(batch),
            "{}",
        )
        searchad_results.append(raw)
    for raw in searchad_results:
        if not raw:
            continue
        try:
            data = json.loads(raw)
            for item in data.get("input_keywords", []):
                kw_raw = item.get("keyword", "")
                n = _normalize_keyword(kw_raw)
                vol_total = item.get("monthly_total", 0)
                vol_pc = item.get("monthly_pc", 0)
                vol_mob = item.get("monthly_mobile", 0)
                for key in norm_to_keys.get(n, []):
                    result[key]["naver_volume"] = vol_total
                    result[key]["naver_volume_pc"] = vol_pc
                    result[key]["naver_volume_mobile"] = vol_mob
                for orig_kw in nospace_to_orig.get(n, []):
                    if result.get(orig_kw, {}).get("naver_volume", 0) == 0:
                        result[orig_kw]["naver_volume"] = vol_total
                        result[orig_kw]["naver_volume_pc"] = vol_pc
                        result[orig_kw]["naver_volume_mobile"] = vol_mob
        except (json.JSONDecodeError, TypeError):
            pass

    # google_trends — 순차 (429 방지, 배치 간 5초 대기)
    batches = _chunk(reps, 5)
    for i, batch in enumerate(batches):
        if i > 0:
            await asyncio.sleep(5)
        raw = await safe_tool_call(
            f"google_trends({batch})",
            google_keyword_trend_fn(batch),
            "{}",
        )
        if raw:
            try:
                data = json.loads(raw)
                for kw, td in data.get("trends", {}).items():
                    if kw in result:
                        result[kw]["google_trend_avg"] = td.get("average", 0.0)
                        result[kw]["google_direction"] = td.get("direction", "stable")
                        result[kw]["google_trend_series"] = td.get("series", [])
            except (json.JSONDecodeError, TypeError):
                pass

    # naver_datalab — 병렬 (배치 5)
    datalab_tasks = [
        safe_tool_call(
            f"naver_datalab({batch})",
            naver_keyword_trend_fn(batch),
            "{}",
        )
        for batch in _chunk(reps, 5)
    ]
    datalab_results = await asyncio.gather(*datalab_tasks)
    for raw in datalab_results:
        if not raw:
            continue
        try:
            data = json.loads(raw)
            for kw, td in data.items():
                if kw in result:
                    result[kw]["naver_trend_avg"] = td.get("average", 0.0)
                    result[kw]["naver_direction"] = td.get("direction", "stable")
                    result[kw]["naver_trend_series"] = td.get("series", [])
        except (json.JSONDecodeError, TypeError):
            pass

    return result
