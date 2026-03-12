"""결과 조립 및 품질 게이트."""

from __future__ import annotations

from datetime import date
from typing import Callable

from core.agents.researcher.parser import _normalize_keyword
from core.schemas import (
    Cluster,
    ParsedInput,
    ResearchResult,
    Stage1Output,
    Stage2Output,
    Stage3Output,
)


def assemble_result(
    parsed: ParsedInput,
    stage1: Stage1Output,
    stage2: Stage2Output,
    stage3: Stage3Output,
    *,
    is_competitor: Callable[[str], bool],
) -> ResearchResult:
    """스테이지 산출물 → ResearchResult 조립."""
    clusters = [
        Cluster.from_draft(
            cd, stage1, stage2, stage3,
            normalize_keyword=_normalize_keyword,
            is_competitor=is_competitor,
        )
        for cd in stage1.cluster_drafts
        if cd.is_focus
    ]

    return ResearchResult(
        run_date=str(date.today()),
        main_keyword=parsed.main_keyword,
        entry_moment=parsed.entry_moment,
        clusters=clusters,
        orphan_keywords=stage1.orphan_keywords,
        intent=parsed.intent,
        source_questions=parsed.questions,
        content_direction=parsed.direction,
        extracted_seeds=parsed.extracted_seeds,
    )


def check_quality_gate(
    result: ResearchResult,
    quality_cfg: dict,
) -> bool:
    """품질 게이트 검사."""
    min_clusters = quality_cfg.get("clusters_min", 1)
    min_kw = quality_cfg.get("keywords_per_cluster_min", 2)
    if len(result.clusters) < min_clusters:
        return False
    for c in result.clusters:
        if len(c.keywords) < min_kw:
            return False
    return True
