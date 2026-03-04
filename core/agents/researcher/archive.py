"""아카이브 I/O 및 토큰 겹침 필터."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.agents.researcher.parser import _STOPWORDS
from core.schemas import ResearchResult, Stage1Output

logger = logging.getLogger(__name__)


def filter_by_token_overlap(
    candidates: list[str],
    reference: list[str],
    min_overlap: int = 1,
) -> list[str]:
    """후보 키워드를 참조 키워드와의 토큰 겹침으로 필터링.

    reference와 토큰을 min_overlap개 이상 공유하는 후보만 유지한다.
    """
    ref_tokens: set[str] = set()
    for seed in reference:
        for t in seed.lower().split():
            if len(t) > 1 and t not in _STOPWORDS:
                ref_tokens.add(t)
    if not ref_tokens:
        return candidates
    result = []
    for kw in candidates:
        tokens = {t for t in kw.lower().split() if len(t) > 1}
        if len(tokens & ref_tokens) >= min_overlap:
            result.append(kw)
    return result


def filter_archive_seeds(
    archive_seeds: list[str],
    reference_seeds: list[str],
) -> list[str]:
    """아카이브 시드를 현재 쿼리와의 토큰 겹침으로 필터링.

    reference_seeds(현재 쿼리에서 추출한 시드)와 토큰을 1개 이상
    공유하는 아카이브 시드만 유지한다.
    """
    return filter_by_token_overlap(
        archive_seeds, reference_seeds, min_overlap=1,
    )


def load_archive_reps(archive_cfg: dict) -> list[str]:
    """이전 실행의 클러스터 대표 키워드를 로드."""
    index_path = archive_cfg.get("index_file", "")
    if not index_path:
        return []
    try:
        with open(index_path) as f:
            index = json.load(f)
        return index.get("cluster_representatives", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def load_archive_clusters(archive_cfg: dict) -> dict[str, list[str]]:
    """아카이브 index.json에서 cluster_data를 로드.

    Returns:
        {대표 키워드: [소속 키워드 목록]} 딕셔너리. 파일 없으면 빈 dict.
        신규 형식(dict with keywords/created/last_seen)과 레거시(plain list) 모두 지원.
    """
    index_path = archive_cfg.get("index_file", "")
    if not index_path:
        return {}
    try:
        with open(index_path) as f:
            index = json.load(f)
        raw = index.get("cluster_data", {})
        result: dict[str, list[str]] = {}
        for rep, val in raw.items():
            if isinstance(val, dict):
                result[rep] = val.get("keywords", [])
            elif isinstance(val, list):
                result[rep] = val
        return result
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_archive(
    archive_cfg: dict,
    result: ResearchResult,
    stage1: Stage1Output | None = None,
) -> None:
    """실행 결과 아카이브 저장 + 인덱스 누적 갱신.

    인덱스에는 포커스 여부와 무관하게 **모든** 클러스터 대표 키워드를 저장한다.
    기존 index.json을 먼저 읽고 verdict에 따라 누적 반영한다.
    - new: cluster_data에 새 항목 추가
    - merge: 기존 항목의 keywords에 신규 키워드 추가, last_seen 갱신
    - duplicate: last_seen만 갱신, 키워드 추가 없음
    """
    runs_dir = archive_cfg.get("runs_dir", "")
    index_path = archive_cfg.get("index_file", "")
    if not runs_dir or not index_path:
        return

    today = result.run_date

    # 현재 실행의 클러스터 정보 수집
    drafts: list = []
    if stage1 is not None:
        drafts = [cd for cd in stage1.cluster_drafts if cd.representative]
    new_cluster_data: dict[str, dict] = {}
    for cd in drafts:
        new_cluster_data[cd.representative] = {
            "keywords": [kw for kw, _ in cd.keywords],
            "verdict": getattr(cd, "archive_verdict", "new"),
            "matched_archive_rep": getattr(cd, "matched_archive_representative", ""),
        }
    if not drafts and result.clusters:
        for c in result.clusters:
            new_cluster_data[c.representative_keyword] = {
                "keywords": [ckw.keyword for ckw in c.keywords],
                "verdict": "new",
                "matched_archive_rep": "",
            }

    try:
        # runs/{date}.json 디버깅용 스냅샷
        Path(runs_dir).mkdir(parents=True, exist_ok=True)
        run_file = Path(runs_dir) / f"{today}.json"
        with open(run_file, "w") as f:
            json.dump(
                result.model_dump(), f, ensure_ascii=False, indent=2,
            )

        # 기존 인덱스 로드
        Path(index_path).parent.mkdir(parents=True, exist_ok=True)
        existing_index: dict = {}
        try:
            with open(index_path) as f:
                existing_index = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        existing_data: dict[str, dict] = existing_index.get("cluster_data", {})

        # verdict별 누적 반영
        for rep, info in new_cluster_data.items():
            verdict = info["verdict"]
            kws = info["keywords"]
            matched = info["matched_archive_rep"]

            if verdict == "new":
                existing_data[rep] = {
                    "keywords": kws,
                    "created": today,
                    "last_seen": today,
                }
            elif verdict == "merge" and matched and matched in existing_data:
                entry = existing_data[matched]
                existing_kws = set(entry.get("keywords", []))
                for kw in kws:
                    if kw not in existing_kws:
                        entry.setdefault("keywords", []).append(kw)
                        existing_kws.add(kw)
                entry["last_seen"] = today
            elif verdict == "duplicate" and matched and matched in existing_data:
                existing_data[matched]["last_seen"] = today
            else:
                # fallback: 매칭 대상 없으면 new 처리
                existing_data[rep] = {
                    "keywords": kws,
                    "created": today,
                    "last_seen": today,
                }

        # 전체 대표 키워드 목록 재구성
        all_reps = list(existing_data.keys())

        index = {
            "last_run": today,
            "cluster_representatives": all_reps,
            "orphan_keywords": result.orphan_keywords,
            "cluster_data": existing_data,
        }
        with open(index_path, "w") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

        logger.info("아카이브 저장 완료: %s", run_file)
    except OSError as e:
        logger.warning("아카이브 저장 실패: %s", e)
