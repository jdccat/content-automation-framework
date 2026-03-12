"""Google Trends 검색어 트렌드 도구 (pytrends 기반)."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from pytrends.request import TrendReq

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_BACKOFF = 10  # 초


def _calc_direction(ratios: list[float]) -> str:
    """시계열 데이터에서 추세 방향을 판단한다."""
    if len(ratios) < 2:
        return "stable"
    mid = len(ratios) // 2
    first_half = sum(ratios[:mid]) / max(mid, 1)
    second_half = sum(ratios[mid:]) / max(len(ratios) - mid, 1)
    diff = second_half - first_half
    if diff > 5:
        return "rising"
    if diff < -5:
        return "declining"
    return "stable"


def _fetch_with_retry(keywords: list[str]) -> tuple[TrendReq, "pd.DataFrame"]:
    """429 rate limit 시 지수 백오프로 재시도한다."""
    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            pt = TrendReq(hl="ko", tz=540)
            pt.build_payload(keywords, cat=0, timeframe="today 3-m", geo="KR")
            df = pt.interest_over_time()
            return pt, df
        except Exception as e:
            last_err = e
            err_str = str(e)
            if "429" in err_str or "Too Many" in err_str:
                wait = _BASE_BACKOFF * (2 ** attempt)
                logger.warning(
                    "Google Trends 429 (시도 %d/%d), %d초 대기",
                    attempt + 1, _MAX_RETRIES, wait,
                )
                time.sleep(wait)
            else:
                raise
    raise last_err  # type: ignore[misc]


async def google_keyword_trend(keywords: list[str]) -> str:
    """Google Trends에서 키워드별 상대 검색 트렌드를 조회한다.

    Args:
        keywords: 비교할 키워드 목록 (최대 5개, 문자열만).

    Returns:
        JSON — {trends: {키워드: {average, series, direction}}}.
    """
    if not keywords:
        return json.dumps({"error": "키워드를 1개 이상 입력하세요."})

    keywords = keywords[:5]

    try:
        pytrends, df = await asyncio.to_thread(_fetch_with_retry, keywords)
    except Exception as e:
        logger.error("Google Trends 조회 실패: %s", e)
        return json.dumps(
            {"error": f"Google Trends 조회 실패: {e}"},
            ensure_ascii=False,
        )

    # 트렌드 시계열 + 방향
    trends: dict[str, dict] = {}
    if df.empty:
        for kw in keywords:
            trends[kw] = {"average": 0.0, "series": [], "direction": "stable"}
    else:
        for kw in keywords:
            if kw in df.columns:
                avg = round(float(df[kw].mean()), 2)
                series = [
                    {"period": str(idx.date()), "ratio": round(float(val), 2)}
                    for idx, val in df[kw].items()
                ]
                direction = _calc_direction([p["ratio"] for p in series])
                trends[kw] = {
                    "average": avg,
                    "series": series,
                    "direction": direction,
                }
            else:
                trends[kw] = {"average": 0.0, "series": [], "direction": "stable"}

    # related_queries 호출 제거 — 추가 HTTP 요청이 429를 유발하므로 삭제.
    # 연관 검색어는 naver_volume의 related_keywords로 대체.

    logger.info("Google Trends 조회 완료: %s", {k: v["average"] for k, v in trends.items()})
    return json.dumps(
        {"trends": trends},
        ensure_ascii=False,
    )
