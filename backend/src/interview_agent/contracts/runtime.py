"""Server-bound runtime inputs and candidate-only outputs; no provider is instantiated."""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from .common import Contract, ContextMode, FalseFlag, Id, Line, Mode, NonNegativeInt, PositiveInt, Text, unique

TOOL_NAMES = ("read_question", "read_evidence", "read_current_answer", "submit_assessment")
ToolName = Literal["read_question", "read_evidence", "read_current_answer", "submit_assessment"]
ModelProfile = Literal["pm003-scripted", "pm006-deepseek-flash", "py008-deepseek-flash"]
Termination = Literal[
    "candidate_ready", "awaiting_input", "needs_review", "budget_exhausted", "timeout",
    "cancelled", "provider_error", "tool_error", "invalid_output", "runtime_error",
]


class SourceRef(Contract):
    source_id: Id
    version: Id


class Citation(SourceRef):
    kind: Literal["evidence", "answer"]
    start_line: PositiveInt
    end_line: PositiveInt
    quote: Annotated[str, Field(min_length=1, max_length=8000)]

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("invalid_citation_range")
        return self


class AssessmentCandidate(Contract):
    question_id: Id
    answer_id: Id
    verdict: Literal["supported", "partial", "insufficient"]
    summary: Annotated[str, Field(min_length=1, max_length=1200)]
    feedback: Annotated[str, Field(min_length=1, max_length=2400)]
    citations: Annotated[list[Citation], Field(min_length=2, max_length=8)]


class RunBudget(Contract):
    provider_requests: Annotated[int, Field(ge=1, le=8)]
    tool_calls: Annotated[int, Field(ge=1, le=12)]
    turns: Annotated[int, Field(ge=1, le=6)] = 6
    wall_ms: Annotated[int, Field(ge=1, le=180000)] = 180000


class RunInput(Contract):
    """Built after server authorization; never use as a public request-body model."""

    server_bound_user: Id
    session_id: Id
    operation_id: Id
    session_version: PositiveInt
    parent_run_id: Id | None
    mode: Mode
    context_mode: ContextMode
    role: Literal["assessment"]
    target_role: Literal["agent-intern"]
    question_id: Id
    context_refs: Annotated[list[SourceRef], Field(max_length=16)]
    allowed_tools: Annotated[list[ToolName], Field(min_length=1, max_length=4, json_schema_extra={"uniqueItems": True}), AfterValidator(unique)]
    model_profile: ModelProfile
    budget: RunBudget


class Session(Contract):
    id: Id
    user_id: Id
    version: PositiveInt
    question_id: Id
    answer_id: Id | None


class RuntimeQuestion(Contract):
    id: Id
    target_role: Literal["agent-intern"]
    text: Annotated[str, Field(min_length=1, max_length=8000)]
    rubric: list[Annotated[str, Field(min_length=1, max_length=1000)]]
    sources: list[SourceRef]


class FixtureEvidence(Contract):
    source_id: Id
    version: Id
    owner_user_id: Id | None
    lines: Annotated[list[Line], Field(min_length=1, max_length=120)]
    origin: Literal["synthetic"]


class FixtureAnswer(Contract):
    id: Id
    version: Id
    user_id: Id
    session_id: Id
    question_id: Id
    lines: Annotated[list[Line], Field(min_length=1, max_length=120)]
    hints_used: NonNegativeInt
    origin: Literal["synthetic_fixture"]


class FixtureBundle(Contract):
    schema_version: Literal["pm003-v1"]
    origin: Literal["synthetic"]
    sessions: list[Session]
    questions: list[RuntimeQuestion]
    evidence: list[FixtureEvidence]
    answers: list[FixtureAnswer]


class SqlEvidence(FixtureEvidence):
    origin: Literal["authored_paraphrase"]


class SqlAnswer(FixtureAnswer):
    # Real submissions preserve empty lines and remain distinct from fixtures.
    lines: Annotated[list[Annotated[str, Field(max_length=2000)]], Field(min_length=1, max_length=40)]
    origin: Literal["user_submitted"]


class SqlSnapshot(Contract):
    schema_version: Literal["sql-snapshot-v1"]
    origin: Literal["sql_snapshot"]
    sessions: list[Session]
    questions: list[RuntimeQuestion]
    evidence: list[SqlEvidence]
    answers: list[SqlAnswer]


class RunCounts(Contract):
    provider_request_attempts: NonNegativeInt
    provider_requests_admitted: NonNegativeInt
    stream_invocations: NonNegativeInt
    assistant_turns: NonNegativeInt
    tool_calls_proposed: NonNegativeInt
    tool_calls_admitted: NonNegativeInt
    tool_executions: NonNegativeInt
    tool_errors: NonNegativeInt


class RunUsage(Contract):
    source: Literal["scripted", "unavailable"]
    input_tokens: None
    output_tokens: None
    estimated_cost: None


class HttpAudit(Contract):
    attempts: None
    scope: Literal["not_observed_by_adapter"]


class BudgetState(Contract):
    turns: NonNegativeInt
    provider_requests: NonNegativeInt
    tool_calls: NonNegativeInt
    deadline_epoch_ms: PositiveInt


class RunEnvelope(Contract):
    operation_id: Id
    run_id: Id
    parent_run_id: Id | None
    session_id: Id
    termination: Termination
    candidate: AssessmentCandidate | None
    committed: FalseFlag
    error_code: Text | None
    counts: RunCounts
    usage: RunUsage
    http_audit: HttpAudit
    trace_ref: Text | None
    diagnostic_errors: list[Text]
    budget_state: BudgetState | None
