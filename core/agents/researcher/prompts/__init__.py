"""프롬프트 로더."""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent


def load_prompt(name: str, **kwargs: str) -> str:
    """prompts/{name}.txt 를 읽어 반환. kwargs가 있으면 str.format() 적용."""
    text = (_DIR / f"{name}.txt").read_text(encoding="utf-8")
    if kwargs:
        text = text.format(**kwargs)
    return text
