"""스냅샷 직렬화/역직렬화 — 단계별 산출물 JSON 저장 및 복원."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from core.schemas import (
    ClusterDraft,
    HubResearchData,
    ParsedInput,
    RawKeywordPool,
    Stage1Output,
    Stage2Output,
    Stage3Output,
)

logger = logging.getLogger(__name__)


# ── 직렬화 ───────────────────────────────────────────────────────


def _serialize(obj) -> dict:
    """dataclass → JSON-serializable dict.

    ClusterDraft.keywords: list[tuple[str,str]] → list[dict] 변환.
    Pydantic BaseModel 필드는 .model_dump()으로 변환.
    """
    from pydantic import BaseModel

    def _dict_factory(items):
        """asdict dict_factory: Pydantic 모델을 dict로 변환."""
        result = {}
        for k, v in items:
            if isinstance(v, BaseModel):
                result[k] = v.model_dump()
            elif isinstance(v, list):
                result[k] = [
                    x.model_dump() if isinstance(x, BaseModel) else x
                    for x in v
                ]
            else:
                result[k] = v
        return result

    d = asdict(obj, dict_factory=_dict_factory)
    if "cluster_drafts" in d:
        for cd in d["cluster_drafts"]:
            cd["keywords"] = [
                {"keyword": kw, "source": src}
                for kw, src in cd.get("keywords", [])
            ]
    if "keywords" in d and isinstance(d["keywords"], list):
        # 단독 ClusterDraft 직렬화
        if d["keywords"] and isinstance(d["keywords"][0], (list, tuple)):
            d["keywords"] = [
                {"keyword": kw, "source": src}
                for kw, src in d["keywords"]
            ]
    return d


def _serialize_deduped(
    deduped: list[tuple[str, str]],
    pool: RawKeywordPool,
) -> dict:
    """중복 제거 키워드 리스트 + pool 메타데이터 직렬화."""
    return {
        "deduped": [
            {"keyword": kw, "source": src} for kw, src in deduped
        ],
        "volumes": pool.volumes,
        "volumes_pc": pool.volumes_pc,
        "volumes_mobile": pool.volumes_mobile,
        "paa_questions": pool.paa_questions,
    }


def _save_json(
    name: str,
    data: dict,
    run_date: str,
    snapshot_dir: str,
) -> Path:
    """dict를 JSON 파일로 저장."""
    d = Path(snapshot_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{run_date}_{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("스냅샷 저장: %s", path)
    return path


def save_snapshot(
    name: str,
    data,
    run_date: str,
    snapshot_dir: str = "snapshots",
) -> Path:
    """dataclass 객체를 JSON 파일로 저장. 저장 경로를 반환."""
    serialized = _serialize(data)
    return _save_json(name, serialized, run_date, snapshot_dir)


def save_deduped(
    deduped: list[tuple[str, str]],
    pool: RawKeywordPool,
    run_date: str,
    snapshot_dir: str = "snapshots",
) -> Path:
    """중복 제거 키워드 리스트를 JSON으로 저장."""
    serialized = _serialize_deduped(deduped, pool)
    return _save_json("stage1_deduped", serialized, run_date, snapshot_dir)


def save_stage1_sub(
    name: str,
    cluster_drafts: list[ClusterDraft],
    orphan_keywords: list[str],
    pool: RawKeywordPool,
    run_date: str,
    snapshot_dir: str = "snapshots",
) -> Path:
    """Stage 1 서브스텝 중간 상태를 Stage1Output 형태로 저장."""
    interim = Stage1Output(
        cluster_drafts=cluster_drafts,
        orphan_keywords=orphan_keywords,
        paa_questions=pool.paa_questions,
        volumes=pool.volumes,
        volumes_pc=pool.volumes_pc,
        volumes_mobile=pool.volumes_mobile,
    )
    return save_snapshot(name, interim, run_date, snapshot_dir)


# ── 역직렬화 ─────────────────────────────────────────────────────


def _restore_cluster_draft(raw: dict) -> ClusterDraft:
    """dict → ClusterDraft. keywords dict→tuple 복원."""
    raw_keywords = raw.pop("keywords", [])
    cd = ClusterDraft(**raw)
    cd.keywords = [
        (d["keyword"], d["source"]) if isinstance(d, dict) else (d[0], d[1])
        for d in raw_keywords
    ]
    return cd


def _load_json(name: str, run_date: str, snapshot_dir: str) -> dict:
    """스냅샷 JSON 파일 로드."""
    path = Path(snapshot_dir) / f"{run_date}_{name}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_stage1_output(name: str, run_date: str, snapshot_dir: str) -> Stage1Output:
    """Stage1Output 형태의 스냅샷 로드 (공통)."""
    raw = _load_json(name, run_date, snapshot_dir)
    cluster_drafts = [
        _restore_cluster_draft(cd) for cd in raw.pop("cluster_drafts", [])
    ]
    out = Stage1Output(**raw)
    out.cluster_drafts = cluster_drafts
    return out


def load_input(
    run_date: str,
    snapshot_dir: str = "snapshots",
) -> ParsedInput:
    """input 스냅샷 → ParsedInput."""
    raw = _load_json("input", run_date, snapshot_dir)
    return ParsedInput(**raw)


def load_pool(
    run_date: str,
    snapshot_dir: str = "snapshots",
) -> RawKeywordPool:
    """stage1_keywords 스냅샷 → RawKeywordPool."""
    raw = _load_json("stage1_keywords", run_date, snapshot_dir)
    return RawKeywordPool(**raw)


def load_deduped(
    run_date: str,
    snapshot_dir: str = "snapshots",
) -> tuple[list[tuple[str, str]], dict]:
    """stage1_deduped 스냅샷 → (deduped 리스트, pool 메타 dict).

    Returns:
        deduped: list[tuple[keyword, source]]
        pool_meta: {"volumes", "volumes_pc", "volumes_mobile", "paa_questions"}
    """
    raw = _load_json("stage1_deduped", run_date, snapshot_dir)
    deduped = [
        (d["keyword"], d["source"]) for d in raw.get("deduped", [])
    ]
    pool_meta = {
        "volumes": raw.get("volumes", {}),
        "volumes_pc": raw.get("volumes_pc", {}),
        "volumes_mobile": raw.get("volumes_mobile", {}),
        "paa_questions": raw.get("paa_questions", {}),
    }
    return deduped, pool_meta


def load_stage1(
    run_date: str,
    snapshot_dir: str = "snapshots",
) -> Stage1Output:
    """stage1_clusters 스냅샷 → Stage1Output."""
    return _load_stage1_output("stage1_clusters", run_date, snapshot_dir)


def load_stage1_sub(
    name: str,
    run_date: str,
    snapshot_dir: str = "snapshots",
) -> Stage1Output:
    """Stage 1 서브스텝 중간 스냅샷 → Stage1Output.

    name: "stage1d_clusters" | "stage1e_clusters" | "stage1f_clusters"
    """
    return _load_stage1_output(name, run_date, snapshot_dir)


def load_stage2(
    run_date: str,
    snapshot_dir: str = "snapshots",
) -> Stage2Output:
    """stage2_serp 스냅샷 → Stage2Output."""
    raw = _load_json("stage2_serp", run_date, snapshot_dir)
    return Stage2Output(**raw)


def load_stage3(
    run_date: str,
    snapshot_dir: str = "snapshots",
) -> Stage3Output:
    """stage3_geo 스냅샷 → Stage3Output."""
    raw = _load_json("stage3_geo", run_date, snapshot_dir)
    return Stage3Output(**raw)


# ── 시드별 저장 + manifest ────────────────────────────────────────


def save_hub_research_per_seed(
    hub_data_list: list[HubResearchData],
    run_date: str,
    output_dir: str,
) -> list[Path]:
    """각 HubResearchData → output_dir/{seed_id}_hub_research.json 저장."""
    d = Path(output_dir)
    d.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for hub in hub_data_list:
        serialized = _serialize(hub)
        fname = f"{hub.seed_id}_hub_research.json"
        path = d / fname
        with open(path, "w", encoding="utf-8") as f:
            json.dump(serialized, f, ensure_ascii=False, indent=2)
        logger.info("시드별 저장: %s", path)
        paths.append(path)
    return paths


def save_manifest(
    hub_data_list: list[HubResearchData],
    run_date: str,
    output_dir: str,
    parsed: ParsedInput | None = None,
) -> Path:
    """manifest.json 생성 — 시드별 출력 파일 인덱스."""
    d = Path(output_dir)
    d.mkdir(parents=True, exist_ok=True)

    seeds_info: list[dict] = []
    for hub in hub_data_list:
        info: dict = {
            "seed_id": hub.seed_id,
            "question": hub.seed_question,
            "output_file": f"{hub.seed_id}_hub_research.json",
            "keyword_count": len(hub.keywords),
        }
        # parsed에서 질문별 intent/direction 추출
        if parsed:
            for sq in parsed.seed_questions:
                if sq.seed_id == hub.seed_id:
                    info["intent"] = sq.intent[0] if sq.intent else ""
                    info["direction"] = sq.content_direction[0] if sq.content_direction else ""
                    break
            else:
                info["intent"] = ""
                info["direction"] = ""
        else:
            info["intent"] = ""
            info["direction"] = ""
        seeds_info.append(info)

    manifest = {
        "run_date": run_date,
        "seed_count": len(hub_data_list),
        "seeds": seeds_info,
    }

    path = d / "manifest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    logger.info("manifest 저장: %s", path)
    return path


def load_manifest(output_dir: str) -> dict:
    """manifest.json 로드 — 플래너가 시드별 출력 파일 경로 발견용."""
    path = Path(output_dir) / "manifest.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)
