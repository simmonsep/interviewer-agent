import json
from pathlib import Path

from interview_agent.contracts.runtime import FixtureBundle, RunInput, TOOL_NAMES
from interview_agent.runtime.scripted import ScriptedProvider, call, candidate_for, success_steps
from interview_agent.runtime.types import AssistantResponse


def bundle():
    path = Path(__file__).resolve().parents[2] / "fixtures/pm0-03/bundle.json"
    return FixtureBundle.model_validate_json(path.read_text(encoding="utf-8"))


def run_input(**patch):
    data = {"server_bound_user": "user-a", "session_id": "session-a", "operation_id": "operation-a", "session_version": 1,
            "parent_run_id": None, "mode": "mock", "context_mode": "role_only", "role": "assessment",
            "target_role": "agent-intern", "question_id": "q-tool-safety",
            "context_refs": [{"source_id": "kb-tool-safety", "version": "v1"}], "allowed_tools": list(TOOL_NAMES),
            "model_profile": "pm003-scripted", "budget": {"provider_requests": 8, "tool_calls": 12}}
    data.update(patch)
    return RunInput.model_validate(data)


def response(*calls, text=""):
    return AssistantResponse(text=text, tool_calls=list(calls))


def payload(messages, call_id):
    return json.loads(next(m.text for m in reversed(messages) if m.role == "tool" and m.tool_call_id == call_id))
