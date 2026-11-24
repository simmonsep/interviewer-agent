"""Public preparation projection (S04A A2). Plan and decision are the stored public documents."""
from typing import Literal

from interview_agent.contracts.common import Contract, PositiveInt
from interview_agent.contracts.practice import UuidString

PreparationStatus = Literal["PENDING", "RUNNING", "WAITING_FOR_USER", "SUCCEEDED",
                            "FAILED", "REJECTED", "STALE", "CANCELLED"]


class PreparationView(Contract):
    id: UuidString
    status: PreparationStatus
    goal_version: PositiveInt
    version: PositiveInt
    role: Literal["java-backend", "ai-application"]
    mode: Literal["interview", "coaching"]
    profile: Literal["quick", "full"] | None = None
    max_questions: PositiveInt
    duration_seconds: PositiveInt
    draft_id: UuidString | None
    use_resume: bool = True
    plan: dict | None
    plan_sha256: str | None
    decision: dict | None
    session_id: UuidString | None
    # S05 L3-4: the cross-session evidence this preparation was allowed to use. Read-only and
    # deliberately narrow (id/topic/label/summary), so "which memory did this plan see" is
    # answerable without exposing the frozen snapshot itself.
    frozen_memory: list[dict] = []
    error_code: str | None
    attempt: int
    created_at: str
