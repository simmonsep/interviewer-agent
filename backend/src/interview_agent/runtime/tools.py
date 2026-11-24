"""Four fixed tools; all batch slots are reserved before any authorized execution."""

import json
from pydantic import ValidationError

from interview_agent.contracts.common import Contract, Id, PositiveInt
from interview_agent.contracts.practice import utf16_length
from interview_agent.contracts.runtime import AssessmentCandidate, TOOL_NAMES

from .store import ReadWindow
from .types import Message, ScopeError, ToolCall, ToolSpec


class QuestionArgs(Contract):
    question_id: Id


class EvidenceArgs(Contract):
    source_id: Id
    version: Id
    start_line: PositiveInt
    end_line: PositiveInt


class EmptyArgs(Contract):
    pass


SCHEMAS = {"read_question": QuestionArgs, "read_evidence": EvidenceArgs,
           "read_current_answer": EmptyArgs, "submit_assessment": AssessmentCandidate}
DESCRIPTIONS = {
    "read_question": "Read the current question and public rubric; no hidden labels.",
    "read_evidence": "Read an allowed source version and 1-based inclusive range, at most 40 lines. No URLs or paths.",
    "read_current_answer": "Read the server-bound answer, or wait for input. Never invent an answer.",
    "submit_assessment": "Collect one candidate with exact quotes from previously delivered answer/evidence windows. Never commits.",
}


def specifications(allowed: list[str]) -> list[ToolSpec]:
    return [ToolSpec(name=name, description=DESCRIPTIONS[name], parameters=SCHEMAS[name].model_json_schema())
            for name in TOOL_NAMES if name in allowed]


def execute(ctx, call: ToolCall, prior_reads: list[ReadWindow]) -> tuple[Message, list[ReadWindow]]:
    delivered: list[ReadWindow] = []
    try:
        ctx.store.session(ctx.input)
        if call.name not in ctx.input.allowed_tools or call.name not in SCHEMAS:
            raise ScopeError("tool_not_allowed")
        try:
            args = SCHEMAS[call.name].model_validate(call.arguments)
        except ValidationError as exc:
            raise ScopeError("invalid_tool_arguments") from exc
        ctx.counts.tool_executions += 1
        if call.name == "read_question":
            payload = {"status": "ok", "question": ctx.store.question(ctx.input, args.question_id).model_dump(mode="json")}
        elif call.name == "read_evidence":
            window = ctx.store.evidence(ctx.input, args.source_id, args.version, args.start_line, args.end_line)
            delivered.append(window)
            payload = {"status": "ok", "evidence": window.model_dump(mode="json")}
        elif call.name == "read_current_answer":
            answer = ctx.store.answer(ctx.input)
            window = ctx.store.answer_window(ctx.input)
            if answer is None or window is None:
                ctx.termination = "awaiting_input"
                payload = {"status": "awaiting_input", "answer": None, "origin": "no_answer"}
            else:
                delivered.append(window)
                payload = {"status": "ok", "answer": {**window.model_dump(mode="json"), "question_id": answer.question_id,
                           "hints_used": answer.hints_used, "origin": answer.origin}}
        else:
            answer = ctx.store.answer(ctx.input)
            if answer is None:
                ctx.termination = "awaiting_input"
                raise ScopeError("answer_required")
            if ctx.candidate is not None:
                raise ScopeError("candidate_already_collected")
            if args.question_id != ctx.input.question_id or args.answer_id != answer.id:
                raise ScopeError("invalid_candidate")
            if {c.kind for c in args.citations} != {"answer", "evidence"}:
                raise ScopeError("invalid_citation")
            for citation in args.citations:
                ctx.store.verify_citation(ctx.input, citation, prior_reads)
            ctx.candidate = args.model_copy(deep=True)
            payload = {"status": "candidate_collected", "committed": False}
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if utf16_length(serialized) > 8000:
            raise ScopeError("result_too_large")
        return Message(role="tool", text=serialized, tool_call_id=call.id, tool_name=call.name), delivered
    except ScopeError as exc:
        ctx.counts.tool_errors += 1
        return Message(role="tool", text=json.dumps({"status": "error", "code": exc.code}),
                       tool_call_id=call.id, tool_name=call.name, is_error=True), []
