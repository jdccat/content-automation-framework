"""네이버 DataLab 검색어 트렌드 API 도구."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"


def _get_credentials() -> tuple[str, str]:
    client_id = os.environ.get("NAVER_CLIENT_ID", "")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise ValueError(
            "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 설정되지 않았습니다."
        )
    return client_id, client_secret


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


async def naver_keyword_trend(keywords: list[str | list[str]]) -> str:
    """네이버 DataLab API로 키워드별 상대 검색 트렌드를 조회한다.

    Args:
        keywords: 비교할 키워드 목록 (최대 5개).
            - 문자열: 단일 키워드 (예: "앱 개발")
            - 리스트: OR 합산 동의어 그룹 (예: ["앱 개발", "어플 개발", "앱개발"])
              리스트의 첫 번째 항목이 그룹명으로 사용됨.

    Returns:
        JSON — 키워드별 {average, series, direction}.
    """
    if not keywords:
        return json.dumps({"error": "키워드를 1개 이상 입력하세요."})

    keywords = keywords[:5]

    client_id, client_secret = _get_credentials()

    end_date = datetime.now()
    start_date = end_date - timedelta(days=90)

    keyword_groups = []
    for kw in keywords:
        if isinstance(kw, list):
            keyword_groups.append({"groupName": kw[0], "keywords": kw})
        else:
            keyword_groups.append({"groupName": kw, "keywords": [kw]})

    body = {
        "startDate": start_date.strftime("%Y-%m-%d"),
        "endDate": end_date.strftime("%Y-%m-%d"),
        "timeUnit": "week",
        "keywordGroups": keyword_groups,
    }

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(DATALAB_URL, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    results: dict[str, dict] = {}
    for group in data.get("results", []):
        title = group.get("title", "")
        data_points = group.get("data", [])
        ratios = [d.get("ratio", 0) for d in data_points]
        avg = sum(ratios) / len(ratios) if ratios else 0.0

        series = [
            {"period": d.get("period", ""), "ratio": round(d.get("ratio", 0), 2)}
            for d in data_points
        ]
        direction = _calc_direction(ratios)

        results[title] = {
            "average": round(avg, 2),
            "series": series,
            "direction": direction,
        }

    logger.info("DataLab 트렌드 조회 완료: %s", {k: v["average"] for k, v in results.items()})
    return json.dumps(results, ensure_ascii=False)
