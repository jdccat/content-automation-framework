"""검증 게이트 — 에이전트 출력 구조 검증."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path


# ── tagger 시드 발행일 보정 ─────────────────────────────────────

_RECENCY_DAYS = 90


def normalize_tagger_output(
    path: str, today: date | None = None,
) -> tuple[bool, str]:
    """tagger 출력의 시드 content_status를 발행일 기준으로 보정.

    시드가 update이고 existing_content.publish_date가 오늘 기준
    90일 이내이면 skip으로 강제 변환한다.

    Returns:
        (보정 수행 여부, 메시지)
    """
    try:
        data = _load_json(path)
    except Exception as exc:
        return False, f"JSON 로드 실패: {exc}"

    seed = data.get("seed_content")
    if not isinstance(seed, dict):
        return False, "seed_content 누락"

    if seed.get("content_status") != "update":
        return False, "보정 불필요 (update 아님)"

    existing = seed.get("existing_content")
    if not isinstance(existing, dict):
        return False, "보정 불필요 (existing_content 없음)"

    pub_str = existing.get("publish_date", "")
    try:
        pub_date = datetime.strptime(pub_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False, f"publish_date 파싱 실패: {pub_str!r}"

    ref_date = today or date.today()
    elapsed = (ref_date - pub_date).days

    if elapsed > _RECENCY_DAYS:
        return False, f"보정 불필요 (발행 {elapsed}일 전, >{_RECENCY_DAYS}일)"

    # ── skip으로 강제 변환 ──
    title = existing.get("title", "제목 없음")
    seed["content_status"] = "skip"
    seed["skip_reason"] = (
        f"{pub_str} 발행 '{title}'과 주제 동일, "
        f"재설계 불필요. 서브 콘텐츠만 제작 권장."
    )

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return True, (
        f"시드 update→skip 보정 완료 "
        f"(발행 {elapsed}일 전 ≤ {_RECENCY_DAYS}일: '{title}')"
    )


# ── legacy 필드 정규화 ─────────────────────────────────────────

def normalize_designer_output(path: str) -> tuple[bool, list[str]]:
    """designer 출력의 legacy 필드를 정규화.

    - classification_reasoning → funnel_reasoning + geo_reasoning
    - title_suggestions[].strategy → estimated_ctr 기본값
    - update_target_url 등 stray 필드 제거
    - skip 시드의 h2_structure/title_suggestions/cta_suggestion 강제 비움

    Returns:
        (보정 수행 여부, 보정 메시지 리스트)
    """
    try:
        data = _load_json(path)
    except Exception as exc:
        return False, [f"JSON 로드 실패: {exc}"]

    seed = data.get("seed_content")
    if not isinstance(seed, dict):
        return False, ["seed_content 누락"]

    messages: list[str] = []
    changed = False

    # skip 시드 구조 필드 강제 비움
    if seed.get("content_status") == "skip":
        changed |= _clear_skip_seed_structure(seed, messages)

    for content in [seed] + (data.get("sub_contents") or []):
        if not isinstance(content, dict):
            continue
        changed |= _normalize_content(content)

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if not messages:
            messages.append("legacy 필드 보정 완료")
        return True, messages
    return False, ["보정 불필요"]


def _clear_skip_seed_structure(seed: dict, messages: list[str]) -> bool:
    """skip 시드의 구조 필드(h2/title/cta)를 빈 값으로 강제 비움."""
    changed = False

    h2 = seed.get("h2_structure")
    if isinstance(h2, list) and len(h2) > 0:
        seed["h2_structure"] = []
        changed = True

    titles = seed.get("title_suggestions")
    if isinstance(titles, list) and len(titles) > 0:
        seed["title_suggestions"] = []
        changed = True

    cta = seed.get("cta_suggestion")
    if cta is not None:
        seed["cta_suggestion"] = None
        changed = True

    if changed:
        messages.append(
            "skip 시드 구조 필드 비움 "
            "(h2_structure=[], title_suggestions=[], cta_suggestion=null)"
        )
    return changed


def _normalize_content(content: dict) -> bool:
    """단일 콘텐츠 블록의 legacy 필드 정규화. 변경 시 True 반환."""
    changed = False

    # classification_reasoning → funnel_reasoning + geo_reasoning
    if "classification_reasoning" in content:
        value = content.pop("classification_reasoning")
        if "funnel_reasoning" not in content:
            content["funnel_reasoning"] = value
        if "geo_reasoning" not in content:
            content["geo_reasoning"] = value
        changed = True

    # title_suggestions[].strategy → estimated_ctr
    titles = content.get("title_suggestions", [])
    if isinstance(titles, list):
        for item in titles:
            if isinstance(item, dict) and "strategy" in item and "estimated_ctr" not in item:
                item["estimated_ctr"] = 40
                item.pop("strategy", None)
                changed = True

    # stray 필드 제거
    if "update_target_url" in content:
        content.pop("update_target_url")
        changed = True

    return changed


# ── 검증 함수 ────────────────────────────────────────────────────

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


def verify_tagger(
    path: str, today: date | None = None,
) -> tuple[bool, list[str]]:
    """tagger JSON 출력 구조 검증 (분류/평가 필드만).

    검증 전 시드 발행일 보정(update→skip)을 자동 수행한다.

    Returns:
        (성공 여부, 검증 결과 메시지 리스트)
    """
    # 시드 발행일 자동 보정
    info: list[str] = []
    normalized, norm_msg = normalize_tagger_output(path, today=today)
    if normalized:
        info.append(f"[자동 보정] {norm_msg}")

    errors: list[str] = []
    try:
        data = _load_json(path)
    except Exception as exc:
        return False, info + [f"JSON 로드 실패: {exc}"]

    seed = data.get("seed_content", {})
    if not isinstance(seed, dict):
        errors.append("seed_content 누락 또는 dict 아님")
        return (False, info + errors)

    # ── seed_content 분류/평가 ──

    # funnel 값 검증
    seed_funnel = seed.get("funnel")
    if seed_funnel not in ("awareness", "consideration", "conversion", "unclassified"):
        errors.append(f"seed_content.funnel 값 오류: {seed_funnel!r}")

    # geo_type 값 검증
    seed_geo = seed.get("geo_type")
    if seed_geo not in ("definition", "comparison", "problem_solving"):
        errors.append(f"seed_content.geo_type 값 오류: {seed_geo!r}")

    # content_approach 필수
    seed_approach = seed.get("content_approach")
    if seed_approach not in ("standard", "data_driven"):
        errors.append(f"seed_content.content_approach 값 오류: {seed_approach!r}")

    # content_status + existing_content + skip_reason
    seed_status = seed.get("content_status")
    if seed_status not in ("new", "update", "skip"):
        errors.append(f"seed_content.content_status 값 오류: {seed_status!r}")
    elif seed_status == "update":
        _check_existing_content(seed.get("existing_content"), "seed_content", errors)
    elif seed_status == "skip":
        _check_existing_content(seed.get("existing_content"), "seed_content", errors)
        skip_reason = seed.get("skip_reason")
        if not isinstance(skip_reason, str) or len(skip_reason) < 5:
            errors.append("seed_content.content_status='skip'이나 skip_reason 누락 또는 5자 미만")
    elif seed_status == "new":
        if seed.get("existing_content") is not None:
            errors.append("seed_content.content_status='new'이나 existing_content가 null 아님")

    # funnel_reasoning (필수, 10자+)
    funnel_r = seed.get("funnel_reasoning")
    if not isinstance(funnel_r, str) or len(funnel_r) < 10:
        errors.append("seed_content.funnel_reasoning 누락 또는 10자 미만")

    # geo_reasoning (필수, 10자+)
    geo_r = seed.get("geo_reasoning")
    if not isinstance(geo_r, str) or len(geo_r) < 10:
        errors.append("seed_content.geo_reasoning 누락 또는 10자 미만")

    # editorial_summary (필수, 20자+)
    seed_summary = seed.get("editorial_summary")
    if not isinstance(seed_summary, str) or len(seed_summary) < 20:
        errors.append("seed_content.editorial_summary 누락 또는 20자 미만")

    # publishing_purpose (필수, 10자+)
    seed_purpose = seed.get("publishing_purpose")
    if not isinstance(seed_purpose, str) or len(seed_purpose) < 10:
        errors.append("seed_content.publishing_purpose 누락 또는 10자 미만")

    # reference_data.competition_h2_depth (3-key 객체)
    seed_ref = seed.get("reference_data")
    seed_comp = seed_ref.get("competition_h2_depth") if isinstance(seed_ref, dict) else None
    _check_competition_h2_depth(seed_comp, "seed_content", errors)

    # ── sub_contents ──

    subs = data.get("sub_contents")
    if not isinstance(subs, list):
        errors.append("sub_contents 배열 누락")
        return (False, info + errors)

    ranks: list[int] = []
    for i, sub in enumerate(subs):
        if not isinstance(sub, dict):
            errors.append(f"sub_contents[{i}] dict 아님")
            continue

        # funnel
        sub_funnel = sub.get("funnel")
        if sub_funnel not in ("awareness", "consideration", "conversion", "unclassified"):
            errors.append(f"sub_contents[{i}].funnel 값 오류: {sub_funnel!r}")

        # geo_type
        sub_geo = sub.get("geo_type")
        if sub_geo not in ("definition", "comparison", "problem_solving"):
            errors.append(f"sub_contents[{i}].geo_type 값 오류: {sub_geo!r}")

        # content_approach
        sub_approach = sub.get("content_approach")
        if sub_approach not in ("standard", "data_driven"):
            errors.append(f"sub_contents[{i}].content_approach 값 오류: {sub_approach!r}")

        # content_status + existing_content (서브는 new/update만, skip 없음)
        sub_status = sub.get("content_status")
        if sub_status not in ("new", "update"):
            errors.append(f"sub_contents[{i}].content_status 값 오류: {sub_status!r}")
        elif sub_status == "update":
            _check_existing_content(sub.get("existing_content"), f"sub_contents[{i}]", errors)
        elif sub_status == "new":
            if sub.get("existing_content") is not None:
                errors.append(f"sub_contents[{i}].content_status='new'이나 existing_content가 null 아님")

        # funnel_reasoning (필수, 10자+)
        sub_funnel_r = sub.get("funnel_reasoning")
        if not isinstance(sub_funnel_r, str) or len(sub_funnel_r) < 10:
            errors.append(f"sub_contents[{i}].funnel_reasoning 누락 또는 10자 미만")

        # geo_reasoning (필수, 10자+)
        sub_geo_r = sub.get("geo_reasoning")
        if not isinstance(sub_geo_r, str) or len(sub_geo_r) < 10:
            errors.append(f"sub_contents[{i}].geo_reasoning 누락 또는 10자 미만")

        # editorial_summary (필수, 20자+)
        sub_edit_sum = sub.get("editorial_summary")
        if not isinstance(sub_edit_sum, str) or len(sub_edit_sum) < 20:
            errors.append(f"sub_contents[{i}].editorial_summary 누락 또는 20자 미만")

        # expansion_role
        exp_role = sub.get("expansion_role")
        if exp_role not in ("심화", "보완", "실행"):
            errors.append(f"sub_contents[{i}].expansion_role 값 오류: {exp_role!r}")

        # priority 필드
        rank = sub.get("priority_rank")
        if not isinstance(rank, int):
            errors.append(f"sub_contents[{i}].priority_rank 누락 또는 정수 아님")
        else:
            ranks.append(rank)

        score = sub.get("priority_score")
        if not isinstance(score, (int, float)):
            errors.append(f"sub_contents[{i}].priority_score 누락 또는 숫자 아님")
        elif score > 10:
            errors.append(f"sub_contents[{i}].priority_score={score} — 10점 만점 초과")

        p_reasoning = sub.get("priority_reasoning")
        if not isinstance(p_reasoning, str) or len(p_reasoning) < 20:
            errors.append(f"sub_contents[{i}].priority_reasoning 누락 또는 20자 미만")

        # reference_data.competition_h2_depth
        sub_ref = sub.get("reference_data")
        sub_comp = sub_ref.get("competition_h2_depth") if isinstance(sub_ref, dict) else None
        _check_competition_h2_depth(sub_comp, f"sub_contents[{i}]", errors)

    # priority_rank 1~N 연속 검증
    if ranks:
        expected = list(range(1, len(subs) + 1))
        if sorted(ranks) != expected:
            errors.append(f"priority_rank 1~{len(subs)} 연속이 아님 (실제: {sorted(ranks)})")

    return (len(errors) == 0, info + errors)


def verify_designer(path: str) -> tuple[bool, list[str]]:
    """designer JSON 출력 구조 검증 (flat 스키마).

    Returns:
        (성공 여부, 검증 결과 메시지 리스트)
    """
    # legacy 필드 + skip 시드 구조 자동 보정
    normalized, norm_msgs = normalize_designer_output(path)
    info: list[str] = []
    if normalized:
        for nm in norm_msgs:
            info.append(f"[자동 보정] {nm}")

    errors: list[str] = []
    try:
        data = _load_json(path)
    except Exception as exc:
        return False, info + [f"JSON 로드 실패: {exc}"]

    seed = data.get("seed_content", {})
    if not isinstance(seed, dict):
        errors.append("seed_content 누락 또는 dict 아님")
        return (False, info + errors)

    # ── seed_content ──

    # skip 시드일 때 h2/title 최소 개수 검증 우회
    seed_status = seed.get("content_status")
    is_skip_seed = seed_status == "skip"

    # h2_structure 3~6개 (skip 시드는 빈 배열 허용)
    h2 = seed.get("h2_structure")
    if is_skip_seed:
        if not isinstance(h2, list):
            errors.append("seed_content.h2_structure 배열이 아님")
    elif not isinstance(h2, list) or len(h2) < 3:
        errors.append(f"seed_content.h2_structure 부족 (현재: {len(h2) if isinstance(h2, list) else 0}, 최소 3)")
    elif len(h2) > 6:
        errors.append(f"seed_content.h2_structure 초과 (현재: {len(h2)}, 최대 6)")

    # title_suggestions >= 3 (skip 시드는 빈 배열 허용)
    titles = seed.get("title_suggestions")
    if is_skip_seed:
        if not isinstance(titles, list):
            errors.append("seed_content.title_suggestions 배열이 아님")
    elif not isinstance(titles, list) or len(titles) < 3:
        errors.append(f"seed_content.title_suggestions 부족 (현재: {len(titles) if isinstance(titles, list) else 0}, 최소 3)")

    # title_suggestions 내부 필드 검증
    if isinstance(titles, list):
        for j, t in enumerate(titles):
            if isinstance(t, dict) and "estimated_ctr" not in t:
                errors.append(f"seed_content.title_suggestions[{j}].estimated_ctr 누락")

    # data_candidates 포맷 검증
    if isinstance(h2, list):
        for j, h2_item in enumerate(h2):
            if isinstance(h2_item, dict):
                _check_data_candidates(h2_item.get("data_candidates", []), f"seed_content.h2_structure[{j}]", errors)

    # funnel_reasoning (필수, 10자 이상)
    funnel_r = seed.get("funnel_reasoning")
    if not isinstance(funnel_r, str) or len(funnel_r) < 10:
        errors.append("seed_content.funnel_reasoning 누락 또는 10자 미만")

    # geo_reasoning (필수, 10자 이상)
    geo_r = seed.get("geo_reasoning")
    if not isinstance(geo_r, str) or len(geo_r) < 10:
        errors.append("seed_content.geo_reasoning 누락 또는 10자 미만")

    # editorial_summary (필수, 20자 이상)
    seed_summary = seed.get("editorial_summary")
    if not isinstance(seed_summary, str) or len(seed_summary) < 20:
        errors.append("seed_content.editorial_summary 누락 또는 20자 미만")

    # reference_data.competition_h2_depth (객체 형식)
    seed_ref = seed.get("reference_data")
    seed_comp = seed_ref.get("competition_h2_depth") if isinstance(seed_ref, dict) else None
    _check_competition_h2_depth(seed_comp, "seed_content", errors)

    # content_approach 필수
    seed_approach = seed.get("content_approach")
    if seed_approach not in ("standard", "data_driven"):
        errors.append(f"seed_content.content_approach 누락 또는 값 오류: {seed_approach!r} (standard|data_driven)")

    # content_status + existing_content 검증
    seed_status = seed.get("content_status")
    if seed_status not in ("new", "update", "skip"):
        errors.append(f"seed_content.content_status 값 오류: {seed_status!r} (new|update|skip)")
    elif seed_status == "update":
        _check_existing_content(
            seed.get("existing_content"),
            "seed_content",
            errors,
        )
    elif seed_status == "skip":
        _check_existing_content(
            seed.get("existing_content"),
            "seed_content",
            errors,
        )
        skip_reason = seed.get("skip_reason")
        if not isinstance(skip_reason, str) or len(skip_reason) < 5:
            errors.append("seed_content.content_status='skip'이나 skip_reason 누락 또는 5자 미만")
    elif seed_status == "new":
        existing = seed.get("existing_content")
        if existing is not None:
            errors.append("seed_content.content_status='new'이나 existing_content가 null 아님")

    # ── sub_contents ──

    subs = data.get("sub_contents")
    if not isinstance(subs, list):
        errors.append("sub_contents 배열 누락")
        return (False, info + errors)

    ranks: list[int] = []
    for i, sub in enumerate(subs):
        if not isinstance(sub, dict):
            errors.append(f"sub_contents[{i}] dict 아님")
            continue

        # priority 필드
        rank = sub.get("priority_rank")
        if not isinstance(rank, int):
            errors.append(f"sub_contents[{i}].priority_rank 누락 또는 정수 아님")
        else:
            ranks.append(rank)

        score = sub.get("priority_score")
        if not isinstance(score, (int, float)):
            errors.append(f"sub_contents[{i}].priority_score 누락 또는 숫자 아님")
        elif score > 10:
            errors.append(f"sub_contents[{i}].priority_score={score} — 10점 만점 초과 (100점 스케일 사용 금지)")

        p_reasoning = sub.get("priority_reasoning")
        if not isinstance(p_reasoning, str) or len(p_reasoning) < 20:
            errors.append(f"sub_contents[{i}].priority_reasoning 누락 또는 20자 미만")

        # funnel_reasoning, geo_reasoning (필수)
        sub_funnel_r = sub.get("funnel_reasoning")
        if not isinstance(sub_funnel_r, str) or len(sub_funnel_r) < 10:
            errors.append(f"sub_contents[{i}].funnel_reasoning 누락 또는 10자 미만")

        sub_geo_r = sub.get("geo_reasoning")
        if not isinstance(sub_geo_r, str) or len(sub_geo_r) < 10:
            errors.append(f"sub_contents[{i}].geo_reasoning 누락 또는 10자 미만")

        # editorial_summary (필수)
        sub_edit_sum = sub.get("editorial_summary")
        if not isinstance(sub_edit_sum, str) or len(sub_edit_sum) < 20:
            errors.append(f"sub_contents[{i}].editorial_summary 누락 또는 20자 미만")

        # reference_data.competition_h2_depth
        sub_ref = sub.get("reference_data")
        sub_comp = sub_ref.get("competition_h2_depth") if isinstance(sub_ref, dict) else None
        _check_competition_h2_depth(sub_comp, f"sub_contents[{i}]", errors)

        # content_approach
        sub_approach = sub.get("content_approach")
        if sub_approach not in ("standard", "data_driven"):
            errors.append(f"sub_contents[{i}].content_approach 누락 또는 값 오류: {sub_approach!r}")

        # h2_structure data_candidates 포맷 검증
        sub_h2s = sub.get("h2_structure")
        if isinstance(sub_h2s, list):
            for j, h2_item in enumerate(sub_h2s):
                if isinstance(h2_item, dict):
                    _check_data_candidates(h2_item.get("data_candidates", []), f"sub_contents[{i}].h2_structure[{j}]", errors)

        # title_suggestions 내부 필드 검증
        sub_titles = sub.get("title_suggestions")
        if isinstance(sub_titles, list):
            for j, t in enumerate(sub_titles):
                if isinstance(t, dict) and "estimated_ctr" not in t:
                    errors.append(f"sub_contents[{i}].title_suggestions[{j}].estimated_ctr 누락")

        sub_h2_list = sub.get("h2_structure")
        if not isinstance(sub_h2_list, list) or len(sub_h2_list) < 3:
            errors.append(f"sub_contents[{i}].h2_structure 부족 (현재: {len(sub_h2_list) if isinstance(sub_h2_list, list) else 0}, 최소 3)")
        elif len(sub_h2_list) > 6:
            errors.append(f"sub_contents[{i}].h2_structure 초과 (현재: {len(sub_h2_list)}, 최대 6)")

        # content_status + existing_content 검증 (서브는 new/update만)
        sub_status = sub.get("content_status")
        if sub_status not in ("new", "update"):
            errors.append(f"sub_contents[{i}].content_status 값 오류: {sub_status!r}")
        elif sub_status == "update":
            _check_existing_content(
                sub.get("existing_content"),
                f"sub_contents[{i}]",
                errors,
            )
        elif sub_status == "new":
            sub_existing = sub.get("existing_content")
            if sub_existing is not None:
                errors.append(f"sub_contents[{i}].content_status='new'이나 existing_content가 null 아님")

    # priority_rank 1~N 연속 검증
    if ranks:
        expected = list(range(1, len(subs) + 1))
        if sorted(ranks) != expected:
            errors.append(f"priority_rank 1~{len(subs)} 연속이 아님 (실제: {sorted(ranks)})")

    return (len(errors) == 0, info + errors)


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

        # content_status 검증
        cs = item.get("content_status")
        if cs not in ("new", "update"):
            errors.append(
                f"schedule[{i}].content_status 값 오류: {cs!r} "
                f"(new|update만 허용, skip은 schedule에 포함 불가)"
            )
        elif cs == "update":
            ec = item.get("existing_content")
            if not isinstance(ec, dict):
                errors.append(
                    f"schedule[{i}].content_status='update'이나 "
                    f"existing_content 누락 또는 dict 아님"
                )

        # title 필드 검증
        title = item.get("title")
        if not isinstance(title, str) or len(title) < 1:
            errors.append(f"schedule[{i}].title 누락 또는 빈 문자열")

        # editorial_summary (20자+)
        es = item.get("editorial_summary")
        if not isinstance(es, str) or len(es) < 20:
            errors.append(f"schedule[{i}].editorial_summary 누락 또는 20자 미만")

        # content_approach
        ca = item.get("content_approach")
        if ca not in ("standard", "data_driven"):
            errors.append(f"schedule[{i}].content_approach 값 오류: {ca!r}")

        # input_question (5자+)
        iq = item.get("input_question")
        if not isinstance(iq, str) or len(iq) < 5:
            errors.append(f"schedule[{i}].input_question 누락 또는 5자 미만")

        # h2_structure[].data_candidates 존재 확인
        h2s = item.get("h2_structure")
        if isinstance(h2s, list):
            for j, h2_item in enumerate(h2s):
                if isinstance(h2_item, dict) and "data_candidates" not in h2_item:
                    errors.append(f"schedule[{i}].h2_structure[{j}].data_candidates 누락")

    # input_questions 배열 검증 — [{question, cluster}] 구조
    input_qs = data.get("input_questions")
    if not isinstance(input_qs, list) or len(input_qs) < 1:
        errors.append("input_questions 배열 누락 또는 비어있음")
    else:
        for i, iq_item in enumerate(input_qs):
            if not isinstance(iq_item, dict):
                errors.append(f"input_questions[{i}] dict 아님")
            else:
                if not iq_item.get("question"):
                    errors.append(f"input_questions[{i}].question 누락")
                if not iq_item.get("cluster"):
                    errors.append(f"input_questions[{i}].cluster 누락")

    # skip_seeds 배열 검증
    skip_seeds = data.get("skip_seeds")
    if skip_seeds is not None:
        if not isinstance(skip_seeds, list):
            errors.append("skip_seeds가 list 타입이 아님")
        else:
            for i, ss in enumerate(skip_seeds):
                if not isinstance(ss, dict):
                    errors.append(f"skip_seeds[{i}] dict 아님")
                    continue
                kw = ss.get("keyword")
                if not isinstance(kw, str) or len(kw) < 1:
                    errors.append(f"skip_seeds[{i}].keyword 누락 또는 빈 문자열")
                sr = ss.get("skip_reason")
                if not isinstance(sr, str) or len(sr) < 1:
                    errors.append(f"skip_seeds[{i}].skip_reason 누락 또는 빈 문자열")

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

    elif target == "architect":
        # tagger 출력에서 architect가 필요로 하는 필드
        required_paths = [
            "seed_content.keyword",
            "seed_content.funnel",
            "seed_content.geo_type",
            "seed_content.content_approach",
            "sub_contents",
        ]
        for rp in required_paths:
            if _deep_get(data, rp) is None:
                errors.append(f"architect 입력 필수 필드 누락: {rp}")

    elif target == "planner":
        # designer 출력에서 planner가 필요로 하는 필드 (flat 경로)
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

        # editorial_summary (architect → planner 전달용)
        seed_es = _deep_get(data, "seed_content.editorial_summary")
        if not isinstance(seed_es, str) or len(seed_es) < 20:
            errors.append("planner 입력 필수: seed_content.editorial_summary 누락 또는 20자 미만")

        # content_approach (architect → planner 전달용)
        seed_ca = _deep_get(data, "seed_content.content_approach")
        if seed_ca not in ("standard", "data_driven"):
            errors.append(f"planner 입력 필수: seed_content.content_approach 값 오류: {seed_ca!r}")

        # input_question (top-level → planner 전달용)
        top_iq = data.get("input_question")
        if not isinstance(top_iq, str) or len(top_iq) < 5:
            errors.append("planner 입력 필수: input_question 누락 또는 5자 미만")

        # content_status 필드 검증
        seed_status = _deep_get(data, "seed_content.content_status")
        if seed_status is None:
            errors.append("planner 입력 필수 필드 누락: seed_content.content_status")

        # skip이 아닌 시드의 title_suggestions[0].title 검증
        if seed_status != "skip":
            seed_titles = _deep_get(data, "seed_content.title_suggestions")
            if isinstance(seed_titles, list) and len(seed_titles) > 0:
                first_title = seed_titles[0]
                if not isinstance(first_title, dict) or not first_title.get("title"):
                    errors.append(
                        "planner 입력 필수: seed_content.title_suggestions[0].title 누락"
                    )
            elif isinstance(seed_titles, list) and len(seed_titles) == 0:
                errors.append(
                    "planner 입력: seed_content.title_suggestions 빈 배열 (skip 아닌 시드)"
                )

        # reference_data 필드 검증 (geo_citation_count, competition_h2_depth)
        seed_ref = _deep_get(data, "seed_content.reference_data")
        if isinstance(seed_ref, dict):
            if "geo_citation_count" not in seed_ref:
                errors.append("planner 입력 필수 필드 누락: seed_content.reference_data.geo_citation_count")
            if "competition_h2_depth" not in seed_ref:
                errors.append("planner 입력 필수 필드 누락: seed_content.reference_data.competition_h2_depth")
            else:
                _check_competition_h2_depth(seed_ref.get("competition_h2_depth"), "seed_content", errors)

        subs = data.get("sub_contents", [])
        if isinstance(subs, list):
            for i, sub in enumerate(subs):
                if not isinstance(sub, dict):
                    continue
                sub_ref = sub.get("reference_data")
                if isinstance(sub_ref, dict):
                    if "geo_citation_count" not in sub_ref:
                        errors.append(f"planner 입력 필수 필드 누락: sub_contents[{i}].reference_data.geo_citation_count")
                    if "competition_h2_depth" not in sub_ref:
                        errors.append(f"planner 입력 필수 필드 누락: sub_contents[{i}].reference_data.competition_h2_depth")
                    else:
                        _check_competition_h2_depth(sub_ref.get("competition_h2_depth"), f"sub_contents[{i}]", errors)

                # editorial_summary + content_approach (architect → planner 전달용)
                sub_es = sub.get("editorial_summary")
                if not isinstance(sub_es, str) or len(sub_es) < 20:
                    errors.append(f"planner 입력 필수: sub_contents[{i}].editorial_summary 누락 또는 20자 미만")
                sub_ca = sub.get("content_approach")
                if sub_ca not in ("standard", "data_driven"):
                    errors.append(f"planner 입력 필수: sub_contents[{i}].content_approach 값 오류: {sub_ca!r}")

                # 우선순위 필드 검증
                for field in ("priority_rank", "priority_score", "priority_reasoning"):
                    if sub.get(field) is None:
                        errors.append(f"planner 입력 필수 필드 누락: sub_contents[{i}].{field}")

                # content_status 필드 검증
                sub_status = sub.get("content_status")
                if sub_status is None:
                    errors.append(f"planner 입력 필수 필드 누락: sub_contents[{i}].content_status")

                # sub의 title_suggestions[0].title 검증
                sub_titles = sub.get("title_suggestions")
                if isinstance(sub_titles, list) and len(sub_titles) > 0:
                    first_sub_title = sub_titles[0]
                    if not isinstance(first_sub_title, dict) or not first_sub_title.get("title"):
                        errors.append(
                            f"planner 입력 필수: sub_contents[{i}].title_suggestions[0].title 누락"
                        )

    return (len(errors) == 0, errors)


# ── 내부 헬퍼 ────────────────────────────────────────────────────

# data_candidates에 허용되는 카테고리.필드 패턴
_DATA_CANDIDATE_CATEGORIES = {"계약", "상주", "외주"}
_DATA_CANDIDATE_RE = re.compile(
    r"^(" + "|".join(_DATA_CANDIDATE_CATEGORIES) + r")\.\S+$"
)


def _check_competition_h2_depth(
    value: object, prefix: str, errors: list[str]
) -> None:
    """competition_h2_depth가 3-key 객체인지 검증."""
    if value is None:
        errors.append(f"{prefix}.reference_data.competition_h2_depth 누락")
        return
    if not isinstance(value, dict):
        errors.append(
            f"{prefix}.reference_data.competition_h2_depth 타입 오류 — "
            f"객체 필요 (실제: {type(value).__name__} {value!r})"
        )
        return
    for key in ("competitors_crawled", "avg_h2_count", "deep_competitors"):
        if key not in value:
            errors.append(
                f"{prefix}.reference_data.competition_h2_depth.{key} 누락"
            )


def _check_existing_content(
    value: object, prefix: str, errors: list[str]
) -> None:
    """existing_content가 update 시 필수 필드를 가진 객체인지 검증."""
    if value is None:
        errors.append(
            f"{prefix}.content_status='update'이나 "
            f"existing_content 누락"
        )
        return
    if not isinstance(value, dict):
        errors.append(
            f"{prefix}.existing_content 타입 오류 — "
            f"객체 필요 (실제: {type(value).__name__})"
        )
        return
    # url 필수
    url = value.get("url")
    if not isinstance(url, str) or len(url) < 5:
        errors.append(f"{prefix}.existing_content.url 누락 또는 5자 미만")
    # h2_sections 필수 (배열)
    h2 = value.get("h2_sections")
    if not isinstance(h2, list) or len(h2) < 1:
        errors.append(f"{prefix}.existing_content.h2_sections 누락 또는 빈 배열")
    # gap_analysis 필수 (10자 이상)
    gap = value.get("gap_analysis")
    if not isinstance(gap, str) or len(gap) < 10:
        errors.append(f"{prefix}.existing_content.gap_analysis 누락 또는 10자 미만")


def _check_data_candidates(
    candidates: object, prefix: str, errors: list[str]
) -> None:
    """data_candidates 배열의 각 항목이 카테고리.필드 형식인지 검증."""
    if not isinstance(candidates, list):
        return
    for k, item in enumerate(candidates):
        if not isinstance(item, str):
            errors.append(f"{prefix}.data_candidates[{k}] 문자열 아님")
        elif not _DATA_CANDIDATE_RE.match(item):
            errors.append(
                f"{prefix}.data_candidates[{k}] 포맷 오류: {item!r} "
                f"— '카테고리.필드' 형식 필요 (허용 카테고리: 계약, 상주, 외주)"
            )


# ── 보정 분류 + repair 프롬프트 ────────────────────────────────

_REPAIRABLE_PATTERNS = [
    "funnel_reasoning 누락",
    "geo_reasoning 누락",
    "editorial_summary 누락",
    "existing_content 누락",
    "title_suggestions 부족",
    "estimated_ctr 누락",
    "h2_structure 부족",
    "h2_structure 초과",
]


def classify_errors(messages: list[str]) -> tuple[list[str], list[str]]:
    """검증 메시지를 보정 가능/불가능으로 분류.

    Returns:
        (repairable, non_repairable)
    """
    repairable: list[str] = []
    non_repairable: list[str] = []
    for msg in messages:
        if msg.startswith("["):  # [자동 보정] 등 info 메시지 스킵
            continue
        if any(p in msg for p in _REPAIRABLE_PATTERNS):
            repairable.append(msg)
        else:
            non_repairable.append(msg)
    return repairable, non_repairable


def build_repair_prompt(
    designer_path: str, researcher_path: str, errors: list[str],
    tagger_path: str = "",
) -> str:
    """누락 필드 보정을 위한 focused repair 프롬프트 생성."""
    error_list = "\n".join(f"  - {e}" for e in errors)

    tagger_section = ""
    if tagger_path:
        tagger_section = f"\n## 태그 데이터\n{tagger_path}\n"

    return f"""\
# Architect 출력 누락 필드 보정

## 현재 출력
{designer_path}

## 리서처 데이터
{researcher_path}
{tagger_section}
## 누락 필드 ({len(errors)}건)
{error_list}

## 지시사항
1. Read로 위 architect 출력 JSON을 읽으세요.
2. Read로 위 리서처 데이터 JSON을 읽으세요.
3. 아래 가이드를 참고하여 누락 필드만 채우세요:
   - title_suggestions: guides/brand_tone.md 타이틀 규칙. 3개 이상, title + estimated_ctr 두 키만
   - h2_structure: guides/brand_tone.md H2 규칙 + guides/content_direction.md GEO×퍼널 패턴. 3~6개
   - estimated_ctr: 0~100 정수
4. 같은 경로에 Write로 저장하세요.

## 중요
- **태그 필드(funnel, geo_type, reasoning 등) 변경 금지** — 구조 필드만 보정
- seed_content와 모든 sub_contents에서 해당 필드가 누락된 곳을 모두 보정
"""


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
