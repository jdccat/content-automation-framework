"""schedule JSON에 plan JSON에서 누락된 필드를 패치하는 스크립트.

Usage:
    .venv/bin/python cli/patch_schedule.py <schedule_json> <plan_json1> <plan_json2> ...

plan JSON들에서 editorial_summary, content_approach, input_question, data_candidates를
읽어 schedule JSON의 해당 항목에 패치한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_plan_index(plan_paths: list[str]) -> dict:
    """plan JSON들에서 keyword → (editorial_summary, content_approach, input_question, h2_data_candidates) 매핑 구축."""
    index: dict[str, dict] = {}
    input_questions: list[dict] = []

    for plan_path in plan_paths:
        plan = load_json(plan_path)
        input_q = plan.get("input_question", "")

        seed = plan.get("seed_content", {})
        seed_keyword = seed.get("keyword", "")

        # input_questions 수집 (plan당 1개)
        if input_q:
            # cluster = seed의 keyword에서 추출 (schedule의 cluster와 매칭하기 위해)
            # 실제로는 schedule 항목의 cluster가 별도로 있으므로 나중에 매핑
            input_questions.append({
                "question": input_q,
                "seed_keyword": seed_keyword,
            })

        # seed 항목 인덱스
        if seed_keyword:
            h2_dc = _extract_h2_data_candidates(seed.get("h2_structure", []))
            index[seed_keyword] = {
                "editorial_summary": seed.get("editorial_summary", ""),
                "content_approach": seed.get("content_approach", "standard"),
                "input_question": input_q,
                "h2_data_candidates": h2_dc,
            }

        # sub_contents 인덱스
        for sub in plan.get("sub_contents", []):
            sub_keyword = sub.get("keyword", "")
            if sub_keyword:
                h2_dc = _extract_h2_data_candidates(sub.get("h2_structure", []))
                index[sub_keyword] = {
                    "editorial_summary": sub.get("editorial_summary", ""),
                    "content_approach": sub.get("content_approach", "standard"),
                    "input_question": input_q,
                    "h2_data_candidates": h2_dc,
                }

    return index, input_questions


def _extract_h2_data_candidates(h2_structure: list) -> dict[str, list]:
    """h2_structure에서 heading → data_candidates 매핑 추출."""
    result: dict[str, list] = {}
    for h2 in h2_structure:
        if isinstance(h2, dict):
            heading = h2.get("heading", "")
            dc = h2.get("data_candidates", [])
            result[heading] = dc if isinstance(dc, list) else []
    return result


def patch_schedule(schedule_path: str, plan_paths: list[str]) -> tuple[int, list[str]]:
    """schedule JSON을 패치하고 저장.

    Returns:
        (패치된 항목 수, 메시지 리스트)
    """
    schedule_data = load_json(schedule_path)
    index, input_questions_raw = build_plan_index(plan_paths)

    messages: list[str] = []
    patched = 0

    # schedule 항목 패치
    for items_list_name in ("schedule", "waitlist"):
        items = schedule_data.get(items_list_name, [])
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            keyword = item.get("keyword", "")
            plan_data = index.get(keyword)
            if not plan_data:
                messages.append(f"WARN: {items_list_name}[{i}] keyword={keyword!r} — plan에서 매칭 실패")
                # 기본값으로 패치
                if "editorial_summary" not in item:
                    item["editorial_summary"] = ""
                if "content_approach" not in item:
                    item["content_approach"] = "standard"
                if "input_question" not in item:
                    item["input_question"] = ""
                # h2_structure에 data_candidates 기본값
                for h2 in item.get("h2_structure", []):
                    if isinstance(h2, dict) and "data_candidates" not in h2:
                        h2["data_candidates"] = []
                continue

            # 필드 패치
            item["editorial_summary"] = plan_data["editorial_summary"]
            item["content_approach"] = plan_data["content_approach"]
            item["input_question"] = plan_data["input_question"]

            # h2_structure에 data_candidates 패치
            h2_dc_map = plan_data["h2_data_candidates"]
            for h2 in item.get("h2_structure", []):
                if isinstance(h2, dict):
                    heading = h2.get("heading", "")
                    if heading in h2_dc_map:
                        h2["data_candidates"] = h2_dc_map[heading]
                    elif "data_candidates" not in h2:
                        h2["data_candidates"] = []

            patched += 1

    # input_questions top-level 생성
    # seed_keyword → cluster 매핑 (schedule 항목에서 추출)
    keyword_to_cluster: dict[str, str] = {}
    for item in schedule_data.get("schedule", []):
        kw = item.get("keyword", "")
        cl = item.get("cluster", "")
        if kw and cl:
            keyword_to_cluster[kw] = cl

    input_questions: list[dict] = []
    seen_questions: set[str] = set()
    for iq_raw in input_questions_raw:
        q = iq_raw["question"]
        if q in seen_questions:
            continue
        seen_questions.add(q)
        # cluster 찾기: seed_keyword로 매핑
        cluster = keyword_to_cluster.get(iq_raw["seed_keyword"], "")
        if not cluster:
            # schedule의 다른 항목에서 같은 input_question을 가진 항목의 cluster 찾기
            for item in schedule_data.get("schedule", []):
                if item.get("input_question") == q:
                    cluster = item.get("cluster", "")
                    break
        input_questions.append({"question": q, "cluster": cluster})

    schedule_data["input_questions"] = input_questions
    messages.append(f"input_questions: {len(input_questions)}개 추가")

    save_json(schedule_path, schedule_data)
    messages.append(f"패치 완료: {patched}개 항목")

    return patched, messages


def main():
    if len(sys.argv) < 3:
        print("Usage: patch_schedule.py <schedule_json> <plan_json1> [plan_json2] ...")
        sys.exit(1)

    schedule_path = sys.argv[1]
    plan_paths = sys.argv[2:]

    patched, messages = patch_schedule(schedule_path, plan_paths)
    for msg in messages:
        print(msg)
    print(f"\nDone. {patched} items patched.")


if __name__ == "__main__":
    main()
