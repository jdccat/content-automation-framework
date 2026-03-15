"""파이프라인 세션 상태.

스케줄러가 게시한 정규 요청 메시지의 thread_ts를 핸들러가 감지하기 위해 공유.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 봇이 게시한 정규 입력 요청 메시지 ts 목록.
# 스케줄러 → 게시 후 등록, 핸들러 → 스레드 응답 감지 시 조회.
scheduled_thread_ts: set[str] = set()


@dataclass
class PipelineSession:
    """풀 파이프라인 multi-turn 세션 상태."""

    run_id: str
    intent: str
    content_direction: str
    target_month: str
    questions: list[str] = field(default_factory=list)
    question_tags: list[dict] = field(default_factory=list)       # [{question, intent, direction}]
    researcher_outputs: list[str] = field(default_factory=list)   # seed_*.json 경로
    tagger_outputs: list[str] = field(default_factory=list)       # tagged_*.json 경로
    designer_outputs: list[str] = field(default_factory=list)     # plan_*.json 경로
    schedule_output: str = ""
    dashboard_path: str = ""
    dashboard_url: str = ""
    processing: bool = False
    current_phase: str = "input"    # input → research → tagging → design → gate → planning → feedback
    feedback_pending: dict[int, str] = field(default_factory=dict)  # idx → 피드백 유형
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


# thread_ts → PipelineSession
pipeline_sessions: dict[str, PipelineSession] = {}
