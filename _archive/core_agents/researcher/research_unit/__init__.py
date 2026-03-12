"""리서치 유닛 패키지 — 프로파일 기반 데이터 수집 모듈."""

from core.agents.researcher.research_unit.runner import run_research_unit
from core.schemas import PROFILE_DEMAND, PROFILE_FULL

# 레거시 호환: agent.py가 이 이름으로 import
from core.agents.researcher.research_unit._legacy import (
    stage2_validation,
    stage3_geo,
)

__all__ = [
    "run_research_unit",
    "PROFILE_FULL",
    "PROFILE_DEMAND",
    "stage2_validation",
    "stage3_geo",
]
