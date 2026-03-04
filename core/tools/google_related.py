"""구글 연관 검색어 수집.

OpenAI Responses API (web_search_preview) 기반.
키워드로 웹 검색을 수행한 뒤 관련 검색어를 추출한다.
OPENAI_API_KEY 미설정이나 API 오류 시 빈 리스트 반환.
"""

from __future__ import annotations

import json
import logging
import os
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


async def google_related_searches(keyword: str) -> str:
    """키워드의 구글 연관 검색어를 수집한다.

    Args:
        keyword: 검색 키워드.

    Returns:
        JSON 문자열. {"keyword": str, "related_searches": list[str]}
    """
    empty = json.dumps(
        {"keyword": keyword, "related_searches": []}, ensure_ascii=False,
    )

    if not keyword or not keyword.strip():
        return empty

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        logger.info("google_related: OPENAI_API_KEY 미설정 — 스킵")
        return empty

    keyword = keyword.strip()
    client = AsyncOpenAI(api_key=api_key)

    prompt = (
        f'한국어 키워드 "{keyword}"로 웹 검색을 수행하고, '
        "이 키워드와 밀접하게 관련된 검색어를 8~10개 추출하세요.\n"
        "구글 연관 검색어 스타일로 — 실제 사용자가 추가로 검색할 만한 구체적인 쿼리.\n\n"
        "규칙:\n"
        "- 원본 키워드를 그대로 반복하지 마세요\n"
        "- 한국어 검색어 위주, 필요 시 영어 혼용 가능\n"
        "- 롱테일 변형, 비교, 비용, 방법, 후기 등 다양한 의도 포함\n\n"
        '반드시 아래 JSON 형식으로만 반환하세요:\n'
        '{"related_searches": ["검색어1", "검색어2", ...]}\n'
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
        searches = data.get("related_searches", [])

        if isinstance(searches, list) and searches:
            logger.info(
                "구글 연관 검색어: %s → %d개", keyword, len(searches),
            )
            return json.dumps(
                {"keyword": keyword, "related_searches": searches[:10]},
                ensure_ascii=False,
            )

        logger.warning("google_related: 응답 파싱 결과 비어있음")
        return empty

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.warning("google_related: 응답 파싱 실패: %s", e)
        return empty
    except Exception as e:
        logger.warning("google_related: API 호출 실패: %s", e)
        return empty
