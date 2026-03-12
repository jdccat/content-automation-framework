"""스냅샷 매니저 — Phase별 결과를 JSON 스냅샷으로 저장/로드."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


class SnapshotManager:
    """Phase별 스냅샷을 snapshots/{run_id}/ 아래에 저장."""

    def __init__(self, run_id: str, root: str = "snapshots") -> None:
        self.run_id = run_id
        self.run_dir = Path(root) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # ── 범용 ─────────────────────────────────────────────────────

    def capture(self, name: str, data: dict) -> Path:
        """스냅샷 저장. timestamp 자동 추가. 반환: 파일 경로."""
        data.setdefault("run_id", self.run_id)
        data.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
        path = self.run_dir / f"{name}.json"
        _write_json(path, data)
        return path

    def capture_with_copy(self, name: str, data: dict, source_path: str) -> Path:
        """스냅샷 + 원본 JSON을 {name}_full.json으로 사본 저장."""
        snap_path = self.capture(name, data)
        full_path = self.run_dir / f"{name}_full.json"
        src = Path(source_path)
        if src.exists():
            shutil.copy2(src, full_path)
        return snap_path

    # ── Phase별 캡처 ─────────────────────────────────────────────

    def capture_seed(
        self,
        step: int,
        question: str,
        researcher_path: str,
        validation: tuple[bool, list[str]],
        duration: float,
    ) -> Path:
        """researcher 완료 후 — seed 영역만 추출하여 스냅샷."""
        data = _load_json_safe(researcher_path)
        seed = data.get("seed", {})

        output: dict[str, Any] = {
            "file_path": researcher_path,
            "seed_keyword": seed.get("keyword", ""),
            "seed_selection": seed.get("seed_selection", {}),
            "volume": seed.get("volume", {}),
            "trend_direction": seed.get("trend_direction", ""),
            "serp_google_count": len(seed.get("serp", {}).get("google", [])),
            "serp_naver_count": len(seed.get("serp", {}).get("naver", [])),
            "h2_topics": seed.get("h2_topics", []),
            "geo_queries_count": len(seed.get("geo_queries", [])),
            "keyword_variants_count": len(seed.get("keyword_variants", [])),
            "paa_questions": seed.get("paa_questions", []),
        }

        v_passed, v_checks = validation
        snap = {
            "phase": "seed",
            "step": step,
            "question_index": step - 1,
            "duration_seconds": round(duration, 1),
            "input": {"question": question, "intent": ""},
            "output": output,
            "validation": {
                "passed": v_passed,
                "checks": v_checks,
                "errors": [] if v_passed else v_checks,
            },
        }

        name = f"q{step:03d}_seed"
        return self.capture(name, snap)

    def capture_fanout(
        self,
        step: int,
        seed_keyword: str,
        researcher_path: str,
        validation: tuple[bool, list[str]],
        duration: float,
    ) -> Path:
        """researcher 완료 후 — fan_outs 영역만 추출하여 스냅샷."""
        data = _load_json_safe(researcher_path)
        fan_outs_raw = data.get("fan_outs", [])

        fan_outs_summary = []
        for fo in fan_outs_raw:
            fan_outs_summary.append({
                "keyword": fo.get("keyword", ""),
                "question": fo.get("question", ""),
                "volume": fo.get("volume", {}),
                "trend_direction": fo.get("trend_direction", ""),
                "top_competitors_count": len(fo.get("top_competitors", [])),
            })

        # fan_out_selection (if meta exists)
        meta = data.get("meta", {})
        fan_out_selection = meta.get("fan_out_selection", {})

        output: dict[str, Any] = {
            "fan_out_count": len(fan_outs_raw),
            "fan_out_selection": fan_out_selection,
            "fan_outs": fan_outs_summary,
        }

        v_passed, v_checks = validation
        snap = {
            "phase": "fanout",
            "step": step,
            "question_index": step - 1,
            "duration_seconds": round(duration, 1),
            "input": {"seed_keyword": seed_keyword},
            "output": output,
            "validation": {
                "passed": v_passed,
                "checks": [f"fan_outs >= 1: {'OK' if len(fan_outs_raw) >= 1 else 'FAIL'} ({len(fan_outs_raw)})"],
                "errors": [] if v_passed else v_checks,
            },
        }

        name = f"q{step:03d}_fanout"
        # Also save full researcher copy
        full_path = self.run_dir / f"q{step:03d}_researcher_full.json"
        src = Path(researcher_path)
        if src.exists():
            shutil.copy2(src, full_path)

        return self.capture(name, snap)

    def capture_designer(
        self,
        step: int,
        researcher_path: str,
        designer_path: str,
        validation: tuple[bool, list[str]],
        duration: float,
    ) -> Path:
        """designer 완료 후 — seed_content + sub_contents 요약 스냅샷."""
        data = _load_json_safe(designer_path)
        seed_content = data.get("seed_content", {})
        sub_contents = data.get("sub_contents", [])

        seed_summary: dict[str, Any] = {
            "keyword": seed_content.get("keyword", ""),
            "funnel": seed_content.get("funnel", ""),
            "geo_type": seed_content.get("geo_type", ""),
            "title_suggestions": seed_content.get("title_suggestions", []),
            "h2_count": len(seed_content.get("h2_structure", [])),
            "funnel_reasoning": seed_content.get("funnel_reasoning", ""),
        }

        sub_summary = [
            {
                "keyword": sc.get("keyword", ""),
                "funnel": sc.get("funnel", ""),
                "expansion_role": sc.get("expansion_role", ""),
            }
            for sc in sub_contents
        ]

        v_passed, v_checks = validation
        snap = {
            "phase": "designer",
            "step": step,
            "question_index": step - 1,
            "duration_seconds": round(duration, 1),
            "input": {"researcher_path": researcher_path},
            "output": {
                "file_path": designer_path,
                "seed_content": seed_summary,
                "sub_contents_count": len(sub_contents),
                "sub_contents_summary": sub_summary,
            },
            "validation": {
                "passed": v_passed,
                "checks": v_checks,
                "errors": [] if v_passed else v_checks,
            },
        }

        name = f"q{step:03d}_designer"
        return self.capture_with_copy(name, snap, designer_path)

    def capture_gate(self, success_count: int, total: int, passed: bool) -> Path:
        """Phase 3 게이트 결과."""
        snap = {
            "phase": "gate",
            "output": {
                "success_count": success_count,
                "total": total,
                "threshold": 2,
                "passed": passed,
            },
            "validation": {
                "passed": passed,
                "checks": [f"success >= 2: {'OK' if passed else 'FAIL'} ({success_count}/{total})"],
                "errors": [] if passed else [f"성공 쌍 부족: {success_count}/{total} (최소 2개 필요)"],
            },
        }
        return self.capture("phase3_gate", snap)

    def capture_planner(
        self,
        schedule_path: str,
        dashboard_path: str,
        validation: tuple[bool, list[str]],
        duration: float,
    ) -> Path:
        """Phase 4 플래너 결과 — schedule 배열 요약 포함."""
        data = _load_json_safe(schedule_path) if schedule_path else {}
        schedule = data.get("schedule", [])

        schedule_summary = [
            {
                "keyword": item.get("keyword", ""),
                "publish_date": item.get("publish_date", ""),
                "priority_score": item.get("priority_score", 0),
                "funnel": item.get("funnel", ""),
            }
            for item in schedule
        ]

        v_passed, v_checks = validation
        snap = {
            "phase": "planner",
            "duration_seconds": round(duration, 1),
            "input": {"designer_outputs_count": len(schedule)},
            "output": {
                "schedule_path": schedule_path,
                "dashboard_path": dashboard_path,
                "schedule_count": len(schedule),
                "schedule_summary": schedule_summary,
            },
            "validation": {
                "passed": v_passed,
                "checks": v_checks,
                "errors": [] if v_passed else v_checks,
            },
        }

        snap_path = self.capture("phase4_planner", snap)
        # Full schedule copy
        if schedule_path:
            full_path = self.run_dir / "phase4_schedule_full.json"
            src = Path(schedule_path)
            if src.exists():
                shutil.copy2(src, full_path)
        return snap_path

    # ── manifest ─────────────────────────────────────────────────

    def save_manifest(self, session: Any) -> Path:
        """manifest.json 갱신 — 세션 상태 + 스냅샷 목록 + 타임라인."""
        snapshots = sorted(
            [p.name for p in self.run_dir.glob("*.json") if p.name != "manifest.json"]
        )

        manifest = {
            "run_id": self.run_id,
            "created_at": getattr(session, "created_at", datetime.now().isoformat()),
            "current_phase": getattr(session, "current_phase", "unknown"),
            "intent": getattr(session, "intent", ""),
            "content_direction": getattr(session, "content_direction", ""),
            "target_month": getattr(session, "target_month", ""),
            "questions": getattr(session, "questions", []),
            "question_tags": getattr(session, "question_tags", []),
            "researcher_outputs": getattr(session, "researcher_outputs", []),
            "designer_outputs": getattr(session, "designer_outputs", []),
            "schedule_output": getattr(session, "schedule_output", ""),
            "dashboard_path": getattr(session, "dashboard_path", ""),
            "dashboard_url": getattr(session, "dashboard_url", ""),
            "processing": getattr(session, "processing", False),
            "feedback_pending": getattr(session, "feedback_pending", {}),
            "snapshots": snapshots,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

        path = self.run_dir / "manifest.json"
        _write_json(path, manifest)
        return path

    # ── 로드 ─────────────────────────────────────────────────────

    def load(self, name: str) -> dict:
        """스냅샷 로드."""
        path = self.run_dir / f"{name}.json"
        return _load_json_safe(str(path))

    def load_manifest(self) -> dict:
        """manifest.json 로드."""
        path = self.run_dir / "manifest.json"
        return _load_json_safe(str(path))

    # ── 클래스 메서드 ────────────────────────────────────────────

    @staticmethod
    def list_runs(root: str = "snapshots") -> list[dict]:
        """전체 런 목록 (run_id, created_at, current_phase, question_count)."""
        root_path = Path(root)
        if not root_path.exists():
            return []

        runs = []
        for d in sorted(root_path.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            manifest_path = d / "manifest.json"
            if manifest_path.exists():
                m = _load_json_safe(str(manifest_path))
                runs.append({
                    "run_id": d.name,
                    "created_at": m.get("created_at", ""),
                    "current_phase": m.get("current_phase", ""),
                    "question_count": len(m.get("questions", [])),
                    "snapshots": len(m.get("snapshots", [])),
                })
            else:
                runs.append({
                    "run_id": d.name,
                    "created_at": "",
                    "current_phase": "unknown",
                    "question_count": 0,
                    "snapshots": 0,
                })
        return runs

    @staticmethod
    def get_active_run_id(root: str = "snapshots") -> str | None:
        """활성 런 ID 반환. .active 파일에서 읽기."""
        active_path = Path(root) / ".active"
        if active_path.exists():
            return active_path.read_text().strip()
        return None

    @staticmethod
    def set_active_run_id(run_id: str, root: str = "snapshots") -> None:
        """활성 런 ID 설정."""
        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)
        (root_path / ".active").write_text(run_id)


# ── 모듈 레벨 헬퍼 ────────────────────────────────────────────────


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_json_safe(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
