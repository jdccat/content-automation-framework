"""네이버 SERP 피처 감지.

OpenAI Responses API (web_search_preview) 기반.
네이버 검색 결과 페이지에서 지식스니펫/스마트블록 존재 여부를 감지한다.
"""

from __future__ import annotations

import json
import logging
import os
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


async def naver_serp_features(keyword: str) -> str:
    """네이버 SERP 피처(지식스니펫, 스마트블록)를 감지한다.

    Args:
        keyword: 검색 키워드.

    Returns:
        JSON 문자열.
        {
            "keyword": str,
            "knowledge_snippet": bool,
            "smart_block": bool,
            "smart_block_components": list[str]
        }
    """
    empty = json.dumps(
        {
            "keyword": keyword,
            "knowledge_snippet": False,
            "smart_block": False,
            "smart_block_components": [],
        },
        ensure_ascii=False,
    )

    if not keyword or not keyword.strip():
        return empty

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        logger.info("naver_serp_features: OPENAI_API_KEY 미설정 — 스킵")
        return empty

    keyword = keyword.strip()
    client = AsyncOpenAI(api_key=api_key)

    prompt = (
        f'한국어 키워드 "{keyword}"를 네이버(naver.com)에서 검색한 결과를 분석해 주세요.\n\n'
        "확인 항목:\n"
        "1. 지식스니펫(Knowledge Snippet): 검색 결과 상단에 정의/요약 박스가 표시되는지\n"
        "2. 스마트블록(Smart Block): 통합검색에서 특수 UI 블록(VIEW, 지식백과, "
        "쇼핑, 뉴스, 이미지, 동영상, 지도 등)이 표시되는지\n"
        "   - 표시되는 스마트블록 구성요소를 목록으로 작성\n\n"
        "반드시 아래 JSON 형식으로만 반환하세요:\n"
        '{"knowledge_snippet": true/false, "smart_block": true/false, '
        '"smart_block_components": ["VIEW", "뉴스", ...]}\n'
        "JSON만 반환하고 다른 텍스트는 포함하지 마세요."
    )

    try:
        response = await client.responses.create(
            model="gpt-4.1-mini",
            tools=[{"type": "web_search_preview"}],
            input=prompt,
        )

        text = response.output_text or ""
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        text = re.sub(r'\\u(?![0-9a-fA-F]{4})', r'\\\\u', text)

        data = json.loads(text)

        result = {
            "keyword": keyword,
            "knowledge_snippet": bool(data.get("knowledge_snippet", False)),
            "smart_block": bool(data.get("smart_block", False)),
            "smart_block_components": data.get("smart_block_components", []),
        }

        logger.info(
            "네이버 SERP 피처: %s → ks=%s, sb=%s, components=%s",
            keyword,
            result["knowledge_snippet"],
            result["smart_block"],
            result["smart_block_components"],
        )
        return json.dumps(result, ensure_ascii=False)

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.warning("naver_serp_features: 응답 파싱 실패: %s", e)
        return empty
    except Exception as e:
        logger.warning("naver_serp_features: API 호출 실패: %s", e)
        return empty
