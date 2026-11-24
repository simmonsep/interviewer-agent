"""Deterministic protocol fixture, not a real model or a semantic-quality evaluator."""

from collections.abc import Callable
from typing import Any

from interview_agent.contracts.runtime import AssessmentCandidate, FixtureBundle, RunInput

from .types import AssistantResponse, Message, ToolCall, ToolSpec


class ScriptedProvider:
    profile = "pm003-scripted"

    def __init__(self, steps: list[AssistantResponse | Callable]):
        self.steps = list(steps)
        self.requests: list[list[Message]] = []
        self.tool_specs: list[list[ToolSpec]] = []

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantResponse:
        index = len(self.requests)
        self.requests.append([m.model_copy(deep=True) for m in messages])
        self.tool_specs.append([t.model_copy(deep=True) for t in tools])
        if index >= len(self.steps):
            raise RuntimeError("script_exhausted")
        step = self.steps[index]
        return step(messages, tools) if callable(step) else step.model_copy(deep=True)


def call(call_id: str, name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def candidate_for(bundle: FixtureBundle, run: RunInput) -> AssessmentCandidate:
    session = next(s for s in bundle.sessions if s.id == run.session_id)
    answer = next(a for a in bundle.answers if a.id == session.answer_id)
    source = next(s for s in bundle.evidence if any(r.source_id == s.source_id and r.version == s.version for r in run.context_refs))
    return AssessmentCandidate.model_validate({
        "question_id": run.question_id, "answer_id": answer.id, "verdict": "partial",
        "summary": "确定性协议演示候选；没有验证评价质量。", "feedback": "请结合已读资料解释自己的回答。",
        "citations": [
            {"kind": "evidence", "source_id": source.source_id, "version": source.version, "start_line": 1,
             "end_line": 1, "quote": source.lines[0]},
            {"kind": "answer", "source_id": answer.id, "version": answer.version, "start_line": 1,
             "end_line": len(answer.lines), "quote": "\n".join(answer.lines)},
        ],
    })


def success_steps(bundle: FixtureBundle, run: RunInput, candidate: AssessmentCandidate | None = None) -> list[AssistantResponse]:
    candidate = candidate if candidate is not None else candidate_for(bundle, run)
    source = run.context_refs[0]
    return [
        AssistantResponse(tool_calls=[call("question", "read_question", question_id=run.question_id),
            call("evidence", "read_evidence", source_id=source.source_id, version=source.version, start_line=1, end_line=1),
            call("answer", "read_current_answer")]),
        AssistantResponse(tool_calls=[ToolCall(id="submit", name="submit_assessment", arguments=candidate.model_dump(mode="json"))]),
    ]
