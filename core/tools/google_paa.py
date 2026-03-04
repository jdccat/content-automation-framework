"""구글 People Also Ask 수집.

OpenAI Responses API (web_search_preview) 기반.
키워드로 웹 검색을 수행한 뒤 PAA 질문을 추출한다.
OPENAI_API_KEY 미설정이나 API 오류 시 빈 리스트 반환.
"""

from __future__ import annotations

import json
import logging
import os
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


async def google_paa(keyword: str) -> str:
    """키워드의 PAA 질문 목록을 수집한다.

    Args:
        keyword: 검색 키워드.

    Returns:
        JSON 문자열. {"keyword": str, "questions": list[str]}
    """
    empty = json.dumps(
        {"keyword": keyword, "questions": []}, ensure_ascii=False,
    )

    if not keyword or not keyword.strip():
        return empty

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        logger.info("google_paa: OPENAI_API_KEY 미설정 — 스킵")
        return empty

    keyword = keyword.strip()
    client = AsyncOpenAI(api_key=api_key)

    prompt = (
        f'한국어 키워드 "{keyword}"로 웹 검색을 수행하고, '
        "이 키워드에 대해 사람들이 자주 물어보는 질문(People Also Ask)을 "
        "6~8개 추출하세요.\n\n"
        "규칙:\n"
        "- 실제 사용자가 검색엔진에 물어볼 법한 자연어 질문 형태\n"
        "- 각 질문은 물음표(?)로 끝나야 합니다\n"
        "- 한국어로 작성\n"
        "- 다양한 의도(정의, 비교, 방법, 비용, 장단점 등) 포함\n\n"
        '반드시 아래 JSON 형식으로만 반환하세요:\n'
        '{"questions": ["질문1?", "질문2?", ...]}\n'
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
        questions = data.get("questions", [])

        if isinstance(questions, list) and questions:
            logger.info(
                "구글 PAA: %s → %d개", keyword, len(questions),
            )
            return json.dumps(
                {"keyword": keyword, "questions": questions[:8]},
                ensure_ascii=False,
            )

        logger.warning("google_paa: 응답 파싱 결과 비어있음")
        return empty

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.warning("google_paa: 응답 파싱 실패: %s", e)
        return empty
    except Exception as e:
        logger.warning("google_paa: API 호출 실패: %s", e)
        return empty
