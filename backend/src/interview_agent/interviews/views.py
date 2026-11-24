"""Public interview projections (S04A A2).

The outer object is strict: every top-level key the route returns is declared here. Nested
structures that are intentionally open — the frozen policy, the target snapshot, event payloads
and observation payloads — stay as typed core fields plus `extra="allow"` models or plain
objects, because their producers already project them and the browser renders them field by
field. Nothing here copies internal rows: the repository has already selected these columns.
"""
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict

from interview_agent.contracts.common import Contract, PositiveInt
from interview_agent.contracts.practice import UuidString

InterviewStatus = Literal["PENDING", "RUNNING", "WAITING_FOR_USER", "WAITING_CREDENTIALS",
                          "COMPLETED", "FAILED", "CANCELLED", "STALE"]


class InterviewPolicyView(Contract):
    model_config = ConfigDict(strict=True, extra="allow")

    role: Literal["java-backend", "ai-application"]
    mode: Literal["interview", "coaching"]
    version: str
    max_questions: PositiveInt
    duration_seconds: PositiveInt


class InterviewEventView(Contract):
    model_config = ConfigDict(strict=True, extra="allow")

    id: UuidString
    seq: PositiveInt
    kind: str
    payload: dict


class InterviewObservationView(Contract):
    model_config = ConfigDict(strict=True, extra="allow")

    id: UuidString
    answer_id: UuidString
    label: str
    summary: str
    correction: str | None


class InterviewReportView(Contract):
    model_config = ConfigDict(strict=True, extra="allow")

    stop_reason: str | None
    model_summary: str | None
    questions: int
    answers: int
    assisted_answers: int
    observations: list[InterviewObservationView]
    semantic_scoring_eligible: bool
    unreviewed_answer_ids: list[UuidString]
    next_actions: list[str]


class InterviewRunView(Contract):
    model_config = ConfigDict(strict=True, extra="allow")

    id: UuidString
    status: str
    error_code: str | None
    model_requests: int
    origin: str | None
    tool_log: list[dict]
    diagnostic: dict | None


class InterviewView(Contract):
    id: UuidString
    goal_version: PositiveInt
    material_revision: int
    version: PositiveInt
    revision: PositiveInt
    status: InterviewStatus
    policy: InterviewPolicyView
    pack_version: str
    target: dict
    application: dict | None
    material_fact_ids: list[UuidString]
    draft_id: UuidString | None
    deadline_at: datetime | None
    resume_evidence: list[dict] | None = None
    material_alignment: dict | None = None
    question_banks: list[dict]
    # F07Z-162: label/version of the skills this session froze; never the prompt body.
    skills: list[dict] | None = None
    # F07Z-163: frozen strategy id -> title, so the review can name the card that was used.
    strategies: list[dict] | None = None
    events: list[InterviewEventView]
    observations: list[InterviewObservationView]
    report: InterviewReportView | None
    run: InterviewRunView | None


class InterviewListView(Contract):
    items: list[InterviewView]
