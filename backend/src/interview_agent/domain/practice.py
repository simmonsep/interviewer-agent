import hashlib
from typing import Literal

from interview_agent.contracts.common import Contract, Id, PositiveInt, NonNegativeInt
from pydantic import model_validator
from interview_agent.contracts.practice import AnswerText, PracticeFeedback, QuestionSnapshot, UuidString, split_answer_lines
from interview_agent.contracts.runtime import AssessmentCandidate, RunCounts, RunInput, SqlSnapshot, TOOL_NAMES
from interview_agent.runtime.store import ScopedAssessmentStore, canonical
from interview_agent.runtime.types import ScopeError


class AssessmentContext(Contract):
    user_id: UuidString
    session_id: UuidString
    session_version: PositiveInt
    target_id: UuidString
    target_version: PositiveInt
    operation_id: UuidString
    answer_id: UuidString
    answer_text: AnswerText
    question: QuestionSnapshot


class AssessmentMetadata(Contract):
    origin: Literal["scripted_protocol", "real_model"]
    provider: Literal["pm003-scripted", "deepseek-flash"]
    run_id: Id
    counts: RunCounts
    authorization_id: Id | None = None
    http_attempts: NonNegativeInt = 0
    accounted_upper_nano: NonNegativeInt = 0

    @model_validator(mode="after")
    def consistent_origin(self):
        if self.origin == "scripted_protocol":
            if self.provider != "pm003-scripted" or self.authorization_id is not None or self.http_attempts != 0 or self.accounted_upper_nano != 0:
                raise ValueError("invalid_scripted_metadata")
        elif self.provider != "deepseek-flash" or self.authorization_id is None or self.http_attempts < 1:
            raise ValueError("invalid_live_metadata")
        return self


class AssessmentOutput(Contract):
    candidate: AssessmentCandidate
    metadata: AssessmentMetadata


def context_hash(context: AssessmentContext):
    return hashlib.sha256(canonical(context.model_dump(mode="json")).encode()).hexdigest()


def build_snapshot(context: AssessmentContext):
    q = context.question
    refs = [{"source_id": s.id, "version": s.version} for s in q.sources]
    snapshot = SqlSnapshot.model_validate({
        "schema_version": "sql-snapshot-v1", "origin": "sql_snapshot",
        "sessions": [{"id": context.session_id, "user_id": context.user_id, "version": context.session_version,
                      "question_id": q.id, "answer_id": context.answer_id}],
        "questions": [{"id": q.id, "text": q.text, "target_role": "agent-intern", "rubric": q.rubric, "sources": refs}],
        "evidence": [{"source_id": s.id, "version": s.version, "owner_user_id": None, "lines": s.lines, "origin": "authored_paraphrase"} for s in q.sources],
        "answers": [{"id": context.answer_id, "version": "answer-v1", "user_id": context.user_id, "session_id": context.session_id,
                     "question_id": q.id, "lines": split_answer_lines(context.answer_text), "hints_used": 0, "origin": "user_submitted"}],
    })
    run = RunInput.model_validate({
        "server_bound_user": context.user_id, "session_id": context.session_id, "operation_id": context.operation_id,
        "session_version": context.session_version, "parent_run_id": None, "mode": "practice", "context_mode": "role_only",
        "role": "assessment", "target_role": "agent-intern", "question_id": q.id, "context_refs": refs,
        "allowed_tools": list(TOOL_NAMES), "model_profile": "pm003-scripted",
        "budget": {"provider_requests": 8, "tool_calls": 12},
    })
    return snapshot, run


def gate_candidate(context: AssessmentContext, value):
    candidate = AssessmentCandidate.model_validate(value.model_dump(mode="json") if isinstance(value, AssessmentCandidate) else value)
    if candidate.question_id != context.question.id or candidate.answer_id != context.answer_id or {c.kind for c in candidate.citations} != {"answer", "evidence"}:
        raise ScopeError("invalid_candidate")
    snapshot, run = build_snapshot(context)
    store = ScopedAssessmentStore(snapshot)
    # SQL revalidates exact snapshot quotes; runtime separately enforces actually delivered windows.
    for citation in candidate.citations:
        window = (store.evidence(run, citation.source_id, citation.version, citation.start_line, citation.end_line)
                  if citation.kind == "evidence" else store.answer_window(run))
        if window is None:
            raise ScopeError("invalid_citation")
        store.verify_citation(run, citation, [window])
    return candidate


def feedback_view(question: QuestionSnapshot, candidate, metadata):
    items = []
    for citation in AssessmentCandidate.model_validate(candidate).citations:
        source = next((s for s in question.sources if s.id == citation.source_id and s.version == citation.version), None) if citation.kind == "evidence" else None
        items.append({**citation.model_dump(mode="json"), "source": {"title": source.title, "url": source.url, "locator": source.locator, "summary_kind": "authored_paraphrase"} if source else None})
    return PracticeFeedback(summary=candidate["summary"], feedback=candidate["feedback"], semantic_scoring_eligible=False,
                            origin=metadata["origin"], provider=metadata["provider"], citations=items)
