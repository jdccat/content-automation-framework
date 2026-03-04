"""네이버 검색광고 API — 키워드 월간 검색량 + 연관 키워드 도구."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

SEARCHAD_URL = "https://api.searchad.naver.com/keywordstool"


def _get_credentials() -> tuple[str, str, str]:
    customer_id = os.environ.get("NAVER_AD_CUSTOMER_ID", "")
    api_key = os.environ.get("NAVER_AD_API_KEY", "")
    api_secret = os.environ.get("NAVER_AD_API_SECRET", "")
    if not all([customer_id, api_key, api_secret]):
        raise ValueError(
            "NAVER_AD_CUSTOMER_ID / NAVER_AD_API_KEY / NAVER_AD_API_SECRET "
            "환경변수가 설정되지 않았습니다."
        )
    return customer_id, api_key, api_secret


def _make_signature(timestamp: str, method: str, uri: str, secret: str) -> str:
    """HMAC-SHA256 서명 생성."""
    message = f"{timestamp}.{method}.{uri}"
    sign = hmac.new(secret.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(sign.digest()).decode()


def _parse_volume(value: int | float | str) -> int:
    """검색량 값 파싱 ('< 10' 등 문자열 처리)."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().lstrip("< ").replace(",", "")
        try:
            return int(cleaned) if cleaned else 0
        except ValueError:
            return 0
    return 0


async def naver_keyword_volume(keywords: list[str]) -> str:
    """네이버 검색광고 API로 키워드별 월간 검색량과 연관 키워드를 조회한다.

    Args:
        keywords: 조회할 키워드 목록 (최대 5개).

    Returns:
        JSON — {input_keywords: [{keyword, monthly_pc, monthly_mobile,
                monthly_total, competition}],
                related_keywords: [{keyword, monthly_total}]}
    """
    if not keywords:
        return json.dumps({"error": "키워드를 1개 이상 입력하세요."})

    # 네이버 검색광고 API는 공백 없는 키워드만 허용
    keywords = [kw.replace(" ", "") for kw in keywords[:5]]

    try:
        customer_id, api_key, api_secret = _get_credentials()
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    timestamp = str(int(time.time() * 1000))
    method = "GET"
    uri = "/keywordstool"
    signature = _make_signature(timestamp, method, uri, api_secret)

    headers = {
        "X-Timestamp": timestamp,
        "X-API-KEY": api_key,
        "X-Customer": customer_id,
        "X-Signature": signature,
    }

    params = [("hintKeywords", kw) for kw in keywords]
    params.append(("showDetail", "1"))

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(SEARCHAD_URL, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error("네이버 검색광고 API 조회 실패: %s", e)
        return json.dumps({"error": f"조회 실패: {e}"}, ensure_ascii=False)

    keyword_list = data.get("keywordList", [])
    input_normalized = {kw.replace(" ", "").lower() for kw in keywords}

    input_results = []
    related_results = []

    for item in keyword_list:
        rel_kw = item.get("relKeyword", "")
        pc = _parse_volume(item.get("monthlyPcQcCnt", 0))
        mobile = _parse_volume(item.get("monthlyMobileQcCnt", 0))
        total = pc + mobile
        comp = item.get("compIdx", "")

        entry = {
            "keyword": rel_kw,
            "monthly_pc": pc,
            "monthly_mobile": mobile,
            "monthly_total": total,
            "competition": comp,
        }

        if rel_kw.replace(" ", "").lower() in input_normalized:
            input_results.append(entry)
        else:
            related_results.append(entry)

    # 연관 키워드는 검색량 내림차순, 상위 20개
    related_results.sort(key=lambda x: x["monthly_total"], reverse=True)

    logger.info(
        "네이버 검색광고 조회 완료: 입력 %d개, 연관 %d개",
        len(input_results), len(related_results),
    )
    return json.dumps(
        {
            "input_keywords": input_results,
            "related_keywords": related_results[:20],
        },
        ensure_ascii=False,
    )
