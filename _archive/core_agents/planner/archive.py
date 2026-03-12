"""플래너 아카이브 — 월별 기획 문서 저장 및 이전 월 퍼널 분포 로드."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.schemas import ContentPlan, FunnelDistribution

logger = logging.getLogger(__name__)


def _prev_month(ym: str) -> str:
    """'YYYY-MM'에서 직전 월 반환. 예: '2026-03' → '2026-02', '2026-01' → '2025-12'."""
    year, month = map(int, ym.split("-"))
    month -= 1
    if month == 0:
        month, year = 12, year - 1
    return f"{year}-{month:02d}"


def save_plan(plan: ContentPlan, runs_dir: str) -> Path:
    """ContentPlan을 {runs_dir}/{target_month}.json에 저장.

    파일이 이미 존재하면 덮어쓴다.
    저장된 경로를 반환한다.
    """
    runs_path = Path(runs_dir)
    runs_path.mkdir(parents=True, exist_ok=True)
    path = runs_path / f"{plan.target_month}.json"
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    logger.info("아카이브 저장: %s", path)
    return path


def load_previous_funnel(
    target_month: str,
    runs_dir: str,
) -> FunnelDistribution | None:
    """target_month 기준 직전 월 퍼널 분포를 아카이브에서 로드.

    직전 월 파일이 없으면 None을 반환한다. 예외를 발생시키지 않는다.

    Args:
        target_month: "2026-03" 형식
        runs_dir:     "archive/planner/runs"
    """
    prev_ym = _prev_month(target_month)
    path = Path(runs_dir) / f"{prev_ym}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fd = data.get("funnel_distribution")
        return FunnelDistribution(**fd) if fd else None
    except Exception as exc:
        logger.warning("아카이브 로드 실패: %s — None 반환: %s", path, exc)
        return None
