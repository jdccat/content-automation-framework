"""LLM 기반 범용 키워드 필터.

시드 키워드 목록에서 단독으로 SEO 가치가 없는 범용 명사를 제거한다.
도메인 컨텍스트(질문 의도, 콘텐츠 방향성)를 함께 전달하여
LLM이 도메인별 가치를 판단하게 한다.

OPENAI_API_KEY가 없거나 API 호출 실패 시 원본을 그대로 반환한다.
"""

from __future__ import annotations

import json
import logging
import os

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


async def filter_generic_seeds(
    seeds: list[str],
    intent: str,
    direction: str,
    model: str = "gpt-4.1-mini",
) -> list[str]:
    """시드 키워드에서 범용 명사를 LLM으로 필터링.

    Args:
        seeds: 시드 키워드 목록.
        intent: 질문 의도 (도메인 컨텍스트).
        direction: 콘텐츠 방향성 (도메인 컨텍스트).
        model: 사용할 OpenAI 모델명.

    Returns:
        필터링된 키워드 목록. 실패 시 원본 반환.
    """
    if not seeds:
        return seeds

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        logger.info("seed_filter: OPENAI_API_KEY 미설정 — 필터 스킵")
        return seeds

    client = AsyncOpenAI(api_key=api_key)

    system_prompt = (
        "당신은 SEO 키워드 필터입니다.\n"
        "주어진 키워드 목록에서 단독으로 검색 의도를 가지지 않는 범용 명사를 제거하세요.\n"
        f"도메인 컨텍스트: {intent} / {direction}\n\n"
        "규칙:\n"
        '- "외주 개발", "ERP 업체" 같은 도메인 특화 구(phrase)는 유지\n'
        '- "이유", "기준", "문제", "방법" 같은 단독으로 SEO 가치 없는 범용 명사는 제거\n'
        "- 2토큰 이상 구는 대부분 유지 (구 안의 범용 명사는 맥락이 있으므로 OK)\n"
        "- 결과를 JSON 배열로만 출력 (설명 없이)"
    )

    user_prompt = json.dumps(seeds, ensure_ascii=False)

    try:
        response = await client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        content = response.choices[0].message.content or ""
        filtered = json.loads(content)

        if isinstance(filtered, list) and filtered:
            logger.info(
                "seed_filter: %d → %d 키워드 (제거: %d)",
                len(seeds),
                len(filtered),
                len(seeds) - len(filtered),
            )
            return filtered

        logger.warning("seed_filter: 응답이 비어있음 — 원본 반환")
        return seeds

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.warning("seed_filter: 응답 파싱 실패: %s — 원본 반환", e)
        return seeds
    except Exception as e:
        logger.warning("seed_filter: API 호출 실패: %s — 원본 반환", e)
        return seeds
