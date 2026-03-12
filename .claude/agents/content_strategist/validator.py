"""검증 게이트 — 에이전트 출력 구조 검증."""

from __future__ import annotations

import json
import re
from pathlib import Path


def verify_researcher(path: str) -> tuple[bool, list[str]]:
    """researcher JSON 출력 구조 검증.

    Returns:
        (성공 여부, 검증 결과 메시지 리스트)
    """
    errors: list[str] = []
    try:
        data = _load_json(path)
    except Exception as exc:
        return False, [f"JSON 로드 실패: {exc}"]

    # meta.assembled_at
    assembled = _deep_get(data, "meta.assembled_at")
    if not assembled:
        errors.append("meta.assembled_at 누락")

    # seed.keyword
    keyword = _deep_get(data, "seed.keyword")
    if not keyword:
        errors.append("seed.keyword 누락")

    # seed.volume.monthly_total
    vol = _deep_get(data, "seed.volume.monthly_total")
    if vol is None or not isinstance(vol, (int, float)):
        errors.append("seed.volume.monthly_total 누락 또는 숫자 아님")

    # fan_outs >= 1
    fan_outs = data.get("fan_outs", [])
    if not isinstance(fan_outs, list) or len(fan_outs) < 1:
        errors.append(f"fan_outs 부족 (현재: {len(fan_outs) if isinstance(fan_outs, list) else 0})")

    # seed.serp.google
    serp_google = _deep_get(data, "seed.serp.google")
    if not isinstance(serp_google, list) or len(serp_google) < 1:
        errors.append("seed.serp.google 누락 또는 빈 배열")

    # seed.h2_topics
    h2_topics = _deep_get(data, "seed.h2_topics")
    if not isinstance(h2_topics, list) or len(h2_topics) < 1:
        errors.append("seed.h2_topics 누락 또는 빈 배열")

    return (len(errors) == 0, errors)


def verify_designer(path: str) -> tuple[bool, list[str]]:
    """designer JSON 출력 구조 검증.

    Returns:
        (성공 여부, 검증 결과 메시지 리스트)
    """
    errors: list[str] = []
    try:
        data = _load_json(path)
    except Exception as exc:
        return False, [f"JSON 로드 실패: {exc}"]

    # seed_content.h2_structure >= 3
    h2 = _deep_get(data, "seed_content.h2_structure")
    if not isinstance(h2, list) or len(h2) < 3:
        errors.append(f"seed_content.h2_structure 부족 (현재: {len(h2) if isinstance(h2, list) else 0}, 최소 3)")

    # seed_content.title_suggestions >= 2
    titles = _deep_get(data, "seed_content.title_suggestions")
    if not isinstance(titles, list) or len(titles) < 2:
        errors.append(f"seed_content.title_suggestions 부족 (현재: {len(titles) if isinstance(titles, list) else 0}, 최소 2)")

    # seed_content.funnel_reasoning
    reasoning = _deep_get(data, "seed_content.funnel_reasoning")
    if not isinstance(reasoning, str) or len(reasoning) <= 20:
        errors.append("seed_content.funnel_reasoning 누락 또는 20자 미만")

    # sub_contents 존재
    subs = data.get("sub_contents")
    if not isinstance(subs, list):
        errors.append("sub_contents 배열 누락")

    return (len(errors) == 0, errors)


def verify_schedule(path: str) -> tuple[bool, list[str]]:
    """schedule JSON 출력 구조 검증.

    Returns:
        (성공 여부, 검증 결과 메시지 리스트)
    """
    errors: list[str] = []
    try:
        data = _load_json(path)
    except Exception as exc:
        return False, [f"JSON 로드 실패: {exc}"]

    # schedule 배열
    schedule = data.get("schedule")
    if not isinstance(schedule, list) or len(schedule) < 1:
        errors.append("schedule 배열 누락 또는 비어있음")
        return False, errors

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for i, item in enumerate(schedule):
        # publish_date 형식
        pub_date = item.get("publish_date", "")
        if not date_pattern.match(pub_date):
            errors.append(f"schedule[{i}].publish_date 형식 오류: {pub_date!r}")

        # priority_score 숫자
        score = item.get("priority_score")
        if not isinstance(score, (int, float)):
            errors.append(f"schedule[{i}].priority_score 누락 또는 숫자 아님")

    return (len(errors) == 0, errors)


def check_format_compat(path: str, target: str) -> tuple[bool, list[str]]:
    """에이전트 간 포맷 호환성 검증.

    Args:
        path: 소스 JSON 경로
        target: "designer" (researcher→designer) 또는 "planner" (designer→planner)

    Returns:
        (호환 여부, 누락 필드 메시지 리스트)
    """
    errors: list[str] = []
    try:
        data = _load_json(path)
    except Exception as exc:
        return False, [f"JSON 로드 실패: {exc}"]

    if target == "designer":
        # researcher 출력에서 designer가 필요로 하는 필드
        required_paths = [
            "seed.keyword",
            "seed.volume",
            "seed.serp",
            "seed.h2_topics",
            "fan_outs",
        ]
        for rp in required_paths:
            if _deep_get(data, rp) is None:
                errors.append(f"designer 입력 필수 필드 누락: {rp}")

    elif target == "planner":
        # designer 출력에서 planner가 필요로 하는 필드
        required_paths = [
            "intent",
            "content_direction",
            "seed_content.keyword",
            "seed_content.funnel",
            "seed_content.geo_type",
            "seed_content.h2_structure",
            "seed_content.title_suggestions",
        ]
        for rp in required_paths:
            if _deep_get(data, rp) is None:
                errors.append(f"planner 입력 필수 필드 누락: {rp}")

        # 우선순위 모델 신규 필드 검증 (geo_citation_count, competition_h2_depth)
        seed_ref = _deep_get(data, "seed_content.reference_data")
        if isinstance(seed_ref, dict):
            if "geo_citation_count" not in seed_ref:
                errors.append("planner 입력 필수 필드 누락: seed_content.reference_data.geo_citation_count")
            if "competition_h2_depth" not in seed_ref:
                errors.append("planner 입력 필수 필드 누락: seed_content.reference_data.competition_h2_depth")

        subs = data.get("sub_contents", [])
        if isinstance(subs, list):
            for i, sub in enumerate(subs):
                sub_ref = sub.get("reference_data") if isinstance(sub, dict) else None
                if isinstance(sub_ref, dict):
                    if "geo_citation_count" not in sub_ref:
                        errors.append(f"planner 입력 필수 필드 누락: sub_contents[{i}].reference_data.geo_citation_count")
                    if "competition_h2_depth" not in sub_ref:
                        errors.append(f"planner 입력 필수 필드 누락: sub_contents[{i}].reference_data.competition_h2_depth")

    return (len(errors) == 0, errors)


# ── 내부 헬퍼 ────────────────────────────────────────────────────


def _load_json(path: str) -> dict:
    """JSON 파일 로드."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _deep_get(data: dict, dotted_path: str, default=None):
    """중첩 dict에서 점 경로로 값 추출.

    예: _deep_get(data, "seed.volume.monthly_total")
    """
    keys = dotted_path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
            if current is None:
                return default
        else:
            return default
    return current
