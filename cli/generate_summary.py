"""위시켓 내부 데이터 summary.json 생성 스크립트.

사용법:
    .venv/bin/python cli/generate_summary.py

입력: data/wishket_internal/raw/*.csv
출력: data/wishket_internal/summary.json
"""

from __future__ import annotations

import csv
import json
import re
import statistics
from collections import Counter
from pathlib import Path

RAW_DIR = Path("data/wishket_internal/raw")
OUTPUT_PATH = Path("data/wishket_internal/summary.json")


def _find_csv(prefix: str) -> Path | None:
    """prefix로 시작하는 CSV 파일 찾기 (가장 최신 1개)."""
    matches = sorted(RAW_DIR.glob(f"{prefix}*.csv"), reverse=True)
    return matches[0] if matches else None


def _read_csv(path: Path) -> list[dict]:
    """BOM 처리 포함 CSV 읽기."""
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _parse_int(value: str) -> int | None:
    """문자열을 정수로 파싱. 실패 시 None."""
    if not value or not value.strip():
        return None
    try:
        return int(float(value.strip()))
    except (ValueError, OverflowError):
        return None


def _stats(values: list[int]) -> dict:
    """min, median, avg, max 통계."""
    if not values:
        return {"min": 0, "median": 0, "avg": 0, "max": 0}
    return {
        "min": min(values),
        "median": int(statistics.median(values)),
        "avg": int(statistics.mean(values)),
        "max": max(values),
    }


def _all_counts(counter: Counter) -> dict[str, int]:
    """전체 항목을 건수 내림차순으로."""
    return dict(counter.most_common())


def _extract_date_range() -> str:
    """raw 파일명에서 기간 추출."""
    csvs = list(RAW_DIR.glob("*.csv"))
    if not csvs:
        return ""
    # 파일명 패턴: *_YY.MM.DD_YY.MM.DD.csv
    dates: list[str] = []
    for p in csvs:
        found = re.findall(r"(\d{2}\.\d{2}\.\d{2})", p.stem)
        dates.extend(found)
    if len(dates) < 2:
        return ""
    dates.sort()
    start = "20" + dates[0].replace(".", "-")
    end = "20" + dates[-1].replace(".", "-")
    return f"{start} ~ {end}"


def _extract_keywords(texts: list[str]) -> dict[str, int]:
    """프로젝트명/계약명에서 주요 키워드 추출. 전체 건수 포함."""
    keyword_patterns = [
        "앱", "웹", "ERP", "AI", "디자인", "쇼핑몰", "홈페이지",
        "플랫폼", "SaaS", "CRM", "IoT", "블록체인", "챗봇",
        "커머스", "관리 시스템", "자동화", "데이터", "게임",
        "퍼블리싱", "API", "백엔드", "프론트엔드", "풀스택",
        "iOS", "안드로이드", "리뉴얼", "유지보수", "고도화",
        "SI", "솔루션", "보안", "클라우드", "펌웨어",
    ]
    counter: Counter = Counter()
    for text in texts:
        for kw in keyword_patterns:
            if kw.lower() in text.lower():
                counter[kw] += 1
    return dict(counter.most_common())


def _build_contracts(rows: list[dict]) -> dict:
    """계약 데이터 집계."""
    amounts = [v for r in rows if (v := _parse_int(r.get("최초 계약 금액", ""))) and v > 0]
    durations = [v for r in rows if (v := _parse_int(r.get("최초 계약 기간(추정)", ""))) and v > 0]
    names = [r.get("계약 명", "") for r in rows if r.get("계약 명")]

    contract_types = Counter(
        r.get("계약 형태", "").strip()
        for r in rows
        if r.get("계약 형태", "").strip()
    )

    return {
        "count": len(rows),
        "금액": _stats(amounts),
        "기간_일": _stats(durations),
        "계약형태": _all_counts(contract_types),
        "키워드": _extract_keywords(names),
    }


def _build_onsite(rows: list[dict]) -> dict:
    """상주 데이터 집계."""
    monthly_pay = [v for r in rows if (v := _parse_int(r.get("월 금액", ""))) and v > 0]
    durations = [v for r in rows if (v := _parse_int(r.get("예상 기간", ""))) and v > 0]

    job_counter = Counter(
        r.get("직군", "").strip()
        for r in rows
        if r.get("직군", "").strip()
    )
    level_counter = Counter(
        r.get("레벨", "").strip()
        for r in rows
        if r.get("레벨", "").strip()
    )
    industry_counter = Counter(
        r.get("프로젝트 산업 분야", "").strip()
        for r in rows
        if r.get("프로젝트 산업 분야", "").strip()
    )
    hiring_type_counter = Counter(
        r.get("구인 유형", "").strip()
        for r in rows
        if r.get("구인 유형", "").strip()
    )

    # 직군별 월금액 중위값
    pay_by_job: dict[str, list[int]] = {}
    for r in rows:
        job = r.get("직군", "").strip()
        pay = _parse_int(r.get("월 금액", ""))
        if job and pay and pay > 0:
            pay_by_job.setdefault(job, []).append(pay)

    pay_by_job_median = {
        job: int(statistics.median(pays))
        for job, pays in sorted(pay_by_job.items(), key=lambda x: -len(x[1]))
    }

    return {
        "count": len(rows),
        "월금액": _stats(monthly_pay),
        "예상기간_일": _stats(durations),
        "직군": _all_counts(job_counter),
        "레벨": _all_counts(level_counter),
        "산업분야": _all_counts(industry_counter),
        "구인유형": _all_counts(hiring_type_counter),
        "직군별_월금액_중위값": pay_by_job_median,
    }


def _build_outsourcing(rows: list[dict]) -> dict:
    """외주 데이터 집계."""
    amounts = [v for r in rows if (v := _parse_int(r.get("예상 금액", ""))) and v > 0]
    durations = [v for r in rows if (v := _parse_int(r.get("예상 기간", ""))) and v > 0]
    applicants = [v for r in rows if (v := _parse_int(r.get("지원자 수", ""))) is not None and v >= 0]

    field_counter: Counter = Counter()
    for r in rows:
        raw_field = r.get("프로젝트 분야", "").strip()
        if raw_field:
            for f in re.split(r"[·•,]", raw_field):
                f = f.strip()
                if f:
                    field_counter[f] += 1

    tech_counter: Counter = Counter()
    for r in rows:
        raw_tech = r.get("관련 기술", "").strip()
        if raw_tech:
            for t in raw_tech.split(","):
                t = t.strip()
                if t:
                    tech_counter[t] += 1

    return {
        "count": len(rows),
        "예상금액": _stats(amounts),
        "예상기간_일": _stats(durations),
        "지원자수": _stats(applicants),
        "분야": _all_counts(field_counter),
        "기술": {k: v for k, v in tech_counter.most_common() if v >= 3},
    }


def main() -> None:
    contracts_csv = _find_csv("contracts")
    onsite_csv = _find_csv("project_onsite")
    outsourcing_csv = _find_csv("project_outsourcing")

    if not all([contracts_csv, onsite_csv, outsourcing_csv]):
        missing = []
        if not contracts_csv:
            missing.append("contracts")
        if not onsite_csv:
            missing.append("project_onsite")
        if not outsourcing_csv:
            missing.append("project_outsourcing")
        print(f"ERROR: CSV 파일 누락: {', '.join(missing)}")
        print(f"  경로: {RAW_DIR}")
        return

    contracts_rows = _read_csv(contracts_csv)
    onsite_rows = _read_csv(onsite_csv)
    outsourcing_rows = _read_csv(outsourcing_csv)

    summary = {
        "data_period": _extract_date_range(),
        "updated_at": str(Path(contracts_csv).stat().st_mtime),
        "계약": _build_contracts(contracts_rows),
        "상주": _build_onsite(onsite_rows),
        "외주": _build_outsourcing(outsourcing_rows),
    }

    # updated_at을 날짜 형식으로 변환
    from datetime import datetime

    latest_mtime = max(
        p.stat().st_mtime for p in [contracts_csv, onsite_csv, outsourcing_csv]
    )
    summary["updated_at"] = datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"summary.json 생성 완료: {OUTPUT_PATH}")
    print(f"  계약: {summary['계약']['count']}건")
    print(f"  상주: {summary['상주']['count']}건")
    print(f"  외주: {summary['외주']['count']}건")


if __name__ == "__main__":
    main()
