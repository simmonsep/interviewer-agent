import asyncio
import json

import pytest

from interview_agent.runtime.adapter import InterviewRuntimeAdapter
from interview_agent.runtime.store import ScopedAssessmentStore
from interview_agent.runtime.trace import MemoryTrace
from interview_agent.runtime.types import ScopeError, ToolCall
from runtime_support import bundle, run_input, ScriptedProvider, call, candidate_for, success_steps, response, payload


def execute(steps, run=None, data=None, **options):
    run = run if run is not None else run_input()
    provider = ScriptedProvider(steps)
    adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(data if data is not None else bundle()), provider, **options)
    return asyncio.run(adapter.run(run)), provider


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
def test_py_c01_real_graph_candidate_only_and_actual_results(mode):
    data, run, trace = bundle(), run_input(), MemoryTrace()
    provider = ScriptedProvider(success_steps(data, run))
    result = asyncio.run(InterviewRuntimeAdapter(ScopedAssessmentStore(data), provider, tool_execution=mode).run(run, trace=trace))
    assert result.envelope.termination == "candidate_ready" and result.cleanup_complete
    assert result.envelope.candidate == candidate_for(data, run)
    assert result.envelope.committed is False
    assert result.envelope.counts.provider_requests_admitted == 2
    assert result.envelope.counts.tool_executions == 4
    assert result.envelope.counts.stream_invocations == 0  # complete(), not a fake stream.
    assert payload(provider.requests[1], "answer")["answer"]["lines"] == data.answers[0].lines
    assert payload(provider.requests[1], "evidence")["evidence"]["lines"] == data.evidence[0].lines[:1]
    captured = json.dumps([[m.model_dump(mode="json") for m in r] for r in provider.requests])
    for forbidden in ["PRIVATE-B-CANARY", "ANSWER-B-CANARY", "owner_user_id", "expected_kind"]:
        assert forbidden not in captured
    assert trace.closed and trace.records[-1].event == "result"
    assert "quote" not in json.dumps([t.model_dump(mode="json") for t in trace.records])
    assert result.envelope.usage.input_tokens is None and result.envelope.http_audit.attempts is None


def test_py_c02_missing_answer_is_waiting_not_invented():
    result, provider = execute([response(call("empty", "read_current_answer"))], run_input(session_id="session-empty"))
    assert result.envelope.termination == "awaiting_input" and result.envelope.candidate is None
    assert len(provider.requests) == 1


def test_py_c03_cross_user_session_stops_before_provider():
    result, provider = execute([], run_input(session_id="session-b"))
    assert result.envelope.error_code == "resource_unavailable" and not provider.requests


def test_py_c04_unknown_tool_not_registered_or_executed():
    result, provider = execute([response(call("shell", "shell", command="never-run")), response(text="stop")])
    assert result.envelope.termination == "tool_error"
    assert result.envelope.counts.tool_executions == result.envelope.counts.tool_calls_admitted == 0
    assert payload(provider.requests[1], "shell")["code"] == "tool_not_allowed"
    assert "shell" not in [s.name for s in provider.tool_specs[0]]


def test_py_c05_owner_check_survives_overbroad_allowlist():
    data = bundle().model_dump(mode="json")
    ref = {"source_id": "private-user-b", "version": "v1"}
    data["questions"][0]["sources"].append(ref)
    result, provider = execute([response(call("private", "read_evidence", **ref, start_line=1, end_line=1)), response(text="stop")],
                              run_input(context_refs=[*run_input().model_dump()["context_refs"], ref]), data)
    assert payload(provider.requests[1], "private")["code"] == "resource_unavailable"
    assert "PRIVATE-B-CANARY" not in str(provider.requests)
    assert result.envelope.candidate is None


@pytest.mark.parametrize("args,code", [
    ({"source_id": "kb-tool-safety", "version": "v1", "start_line": 2, "end_line": 99}, "invalid_range"),
    ({"source_id": "kb-tool-safety", "version": "v2", "start_line": 1, "end_line": 1}, "resource_unavailable"),
    ({"source_id": "unknown", "version": "v1", "start_line": 1, "end_line": 1}, "resource_unavailable"),
])
def test_py_c06_bad_ranges_versions_and_sources(args, code):
    result, provider = execute([response(call("bad", "read_evidence", **args)), response(text="stop")])
    assert payload(provider.requests[1], "bad") == {"status": "error", "code": code}
    assert result.envelope.counts.tool_errors == 1


def test_py_c07_forged_quote_is_not_a_candidate():
    data, run = bundle(), run_input()
    candidate = candidate_for(data, run)
    candidate.citations[0].quote = "fabricated quote"
    result, provider = execute(success_steps(data, run, candidate) + [response(text="stop")])
    assert result.envelope.candidate is None and result.envelope.termination == "tool_error"
    assert payload(provider.requests[2], "submit")["code"] == "invalid_citation"


def test_py_c08_known_but_unread_quote_rejected():
    candidate = candidate_for(bundle(), run_input())
    result, provider = execute([response(ToolCall(id="unread", name="submit_assessment", arguments=candidate.model_dump(mode="json"))), response(text="stop")])
    assert result.envelope.candidate is None
    assert payload(provider.requests[1], "unread")["code"] == "invalid_citation"


def test_py_c09_extra_user_id_in_tool_args_rejected():
    result, provider = execute([response(call("inject", "read_current_answer", user_id="user-b")), response(text="stop")])
    assert payload(provider.requests[1], "inject")["code"] == "invalid_tool_arguments"
    assert result.envelope.counts.tool_executions == 0


def test_py_c10_direct_submit_cannot_invent_missing_answer():
    candidate = candidate_for(bundle(), run_input())
    result, _ = execute([response(ToolCall(id="submit", name="submit_assessment", arguments=candidate.model_dump(mode="json")))],
                        run_input(session_id="session-empty"))
    assert result.envelope.termination == "awaiting_input" and result.envelope.candidate is None


def test_py_c11_plain_text_is_not_feedback_submission():
    result, _ = execute([response(text="Everything is correct")])
    assert result.envelope.termination == "invalid_output" and result.envelope.candidate is None


def test_py_c12_separate_concurrent_instances_isolate_context():
    async def scenario():
        data = bundle()
        runs = [run_input(), run_input(server_bound_user="user-b", session_id="session-b", operation_id="operation-b")]
        providers = [ScriptedProvider(success_steps(data, r)) for r in runs]
        results = await asyncio.gather(*(InterviewRuntimeAdapter(ScopedAssessmentStore(data), p).run(r) for p, r in zip(providers, runs)))
        assert [r.envelope.candidate.answer_id for r in results] == ["answer-a", "answer-b"]
        assert "ANSWER-B-CANARY" not in str(providers[0].requests)
        assert data.answers[0].lines[0] not in str(providers[1].requests)
    asyncio.run(scenario())


def test_py_c13_provider_exception_is_one_attempt_no_retry_or_leak():
    def fail(*_):
        raise RuntimeError("PROVIDER-SECRET-CANARY")
    result, provider = execute([fail])
    assert result.envelope.termination == "provider_error" and len(provider.requests) == 1
    assert "PROVIDER-SECRET-CANARY" not in result.model_dump_json()


def test_py_c14_snapshot_is_copy_owned_and_duplicates_fail():
    data, run = bundle(), run_input()
    store = ScopedAssessmentStore(data)
    data.answers[0].lines[0] = "mutated outside"
    assert store.answer(run).lines[0] != "mutated outside"
    answer = store.answer(run)
    answer.lines.clear()
    assert store.answer(run).lines
    data.sessions.append(data.sessions[0])
    with pytest.raises(ScopeError, match="duplicate_fixture_id"):
        ScopedAssessmentStore(data)


def test_py_c15_actual_tool_specs_match_server_subset():
    result, provider = execute([response(call("forbidden", "read_evidence", source_id="kb-tool-safety", version="v1", start_line=1, end_line=1)), response(text="stop")],
                              run_input(allowed_tools=["read_question", "read_current_answer"]))
    assert [s.name for s in provider.tool_specs[0]] == ["read_question", "read_current_answer"]
    assert result.envelope.counts.tool_executions == 0


def test_py_c16_oversized_serialized_read_grants_no_citation():
    data, run = bundle(), run_input()
    data.evidence[0].lines = ["x" * 1980] * 4
    candidate = candidate_for(data, run)
    steps = [response(call("large", "read_evidence", source_id="kb-tool-safety", version="v1", start_line=1, end_line=4), call("answer", "read_current_answer")),
             response(ToolCall(id="submit", name="submit_assessment", arguments=candidate.model_dump(mode="json"))), response(text="stop")]
    result, provider = execute(steps, run, data)
    assert payload(provider.requests[1], "large")["code"] == "result_too_large"
    assert payload(provider.requests[2], "submit")["code"] == "invalid_citation"
    assert result.envelope.candidate is None


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
def test_py_p10_entire_over_budget_batch_executes_zero(mode):
    run = run_input(budget={"provider_requests": 8, "tool_calls": 1})
    result, _ = execute(success_steps(bundle(), run), run, tool_execution=mode)
    assert result.envelope.termination == "budget_exhausted"
    assert result.envelope.counts.tool_calls_proposed == 3
    assert result.envelope.counts.tool_executions == result.envelope.counts.tool_calls_admitted == 0
    assert result.envelope.budget_state.tool_calls == 0


@pytest.mark.parametrize("budget,code", [({"provider_requests": 1, "tool_calls": 12}, "request_budget"),
                                        ({"provider_requests": 8, "tool_calls": 12, "turns": 1}, "turn_budget")])
def test_py_p09_request_and_turn_limits_are_independent(budget, code):
    result, provider = execute([response(call("question", "read_question", question_id="q-tool-safety"))], run_input(budget=budget))
    assert result.envelope.termination == "budget_exhausted" and result.envelope.error_code == code
    assert len(provider.requests) == 1 and result.envelope.counts.provider_request_attempts == 2


def test_py_p08_failed_observation_returned_then_corrected():
    data, run = bundle(), run_input()
    steps = [response(call("bad", "read_evidence", source_id="kb-tool-safety", version="wrong", start_line=1, end_line=1)), *success_steps(data, run)]
    result, provider = execute(steps)
    assert result.envelope.termination == "candidate_ready" and result.envelope.counts.tool_errors == 1
    assert payload(provider.requests[1], "bad")["code"] == "resource_unavailable"
    assert payload(provider.requests[-1], "bad")["code"] == "resource_unavailable"


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
def test_same_batch_reads_cannot_authorize_submit(mode):
    data, run = bundle(), run_input()
    initial, submit = success_steps(data, run)
    result, provider = execute([response(*initial.tool_calls, *submit.tool_calls), response(text="stop")], tool_execution=mode)
    assert result.envelope.candidate is None
    assert payload(provider.requests[1], "submit")["code"] == "invalid_citation"


def test_py_p04_duplicate_call_ids_rejected_before_execution():
    result, _ = execute([response(call("same", "read_current_answer"), call("same", "read_current_answer"))])
    assert result.envelope.error_code == "duplicate_tool_call_id" and result.envelope.counts.tool_executions == 0
