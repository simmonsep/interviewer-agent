"""S00O: the trace is default-off, metadata-only, bounded and fail-open."""
import asyncio
import json
from time import perf_counter

import pytest

from interview_agent import observability as obs
from interview_agent.interviews.agent import Decision, ScriptedInterviewer, interview
from interview_agent.runtime.types import AssistantResponse, ToolCall
from test_interview_agent import scope


def read(tracer):
    tracer.flush()
    return [json.loads(line) for line in tracer.path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def tracer(tmp_path):
    value = obs.configure("test", enabled=True, directory=tmp_path, mode="test")
    try:
        yield value
    finally:
        obs.reset()


def test_default_off_writes_nothing_and_is_the_noop_tracer(tmp_path, monkeypatch):
    monkeypatch.delenv("INTERVIEW_AGENTOPS", raising=False)
    value = obs.configure("test", directory=tmp_path)
    assert value.enabled is False and value is obs.current()
    with value.span("worker.attempt", stage="worker"):
        value.record("answer.accepted", stage="api", session_id="s-1")
    assert list(tmp_path.iterdir()) == []
    assert value.dropped == 0


def test_exported_record_has_the_declared_fields_and_real_parent_child(tracer):
    with tracer.span("worker.attempt", stage="worker", session_id="s-1", attempt=2):
        tracer.record("interview.tool", stage="interview", tool_name="read_topics", tool_status="ok")
        with tracer.span("provider.http", stage="provider") as span:
            span.set(http_status=200, request_bytes=10)
            span.ok()
    data = {row["event_name"]: row for row in read(tracer)}
    attempt, tool, http = data["worker.attempt"], data["interview.tool"], data["provider.http"]
    contract = {"schema_version", "event_name", "stage", "timestamp", "duration_ms", "status",
                "reason_code", "trace_id", "span_id", "parent_span_id"}
    assert contract <= set(attempt) and attempt["schema_version"] == obs.SCHEMA_VERSION
    assert attempt["stage"] == "worker" and attempt["parent_span_id"] is None
    assert tool["parent_span_id"] == attempt["span_id"] == http["parent_span_id"]
    assert len({attempt["trace_id"], tool["trace_id"], http["trace_id"]}) == 1
    assert http["status"] == "ok" and http["http_status"] == 200 and http["request_bytes"] == 10
    assert attempt["session_id"] == "s-1" and attempt["attempt"] == 2
    assert 0 <= tool["duration_ms"] < 50 and attempt["duration_ms"] >= 0


def test_only_whitelisted_metadata_is_written(tracer):
    secret = "SYNTHETIC-KEY-0123456789"
    with tracer.span("provider.complete", stage="provider", api_key=secret,
                     headers={"authorization": secret}, resume="SYNTHETIC-RESUME",
                     answer="SYNTHETIC-ANSWER", email="synthetic@example.com"):
        pass
    tracer.record("x", stage="s", prompt=secret, tool_arguments={"q": secret})
    tracer.flush()
    text = tracer.path.read_text(encoding="utf-8")
    assert secret not in text and "SYNTHETIC" not in text and "synthetic@example.com" not in text
    assert [row["event_name"] for row in read(tracer)] == ["provider.complete", "x"]


def test_exception_text_is_never_exported_and_the_error_still_propagates(tracer):
    with pytest.raises(RuntimeError):
        with tracer.span("x", stage="s"):
            raise RuntimeError("SYNTHETIC-EXCEPTION-SECRET")
    record = read(tracer)[0]
    assert record["status"] == "error" and record["reason_code"] == "RuntimeError"
    assert "SYNTHETIC" not in tracer.path.read_text(encoding="utf-8")


def test_a_stable_exception_code_is_kept_for_aggregation(tracer):
    class Refused(Exception):
        def __init__(self):
            self.code = "model_budget_exhausted"

    with pytest.raises(Refused):
        with tracer.span("x", stage="s"):
            raise Refused()
    record = read(tracer)[0]
    assert record["reason_code"] == "model_budget_exhausted"
    assert obs.category_of(record["reason_code"]) == "budget"


def test_unwritable_directory_keeps_the_run_alive_and_counts_drops(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    value = obs.configure("test", enabled=True, directory=blocker)
    assert value.enabled is True
    with value.span("x", stage="s"):
        pass
    value.record("y", stage="s")
    value.flush()
    assert value.dropped >= 2
    value.close()


def test_bounded_buffer_drops_the_excess_instead_of_growing(tmp_path):
    value = obs._build("test", tmp_path, {}, max_events=2, flush_every=99)
    for index in range(5):
        value.record("e", stage="s", round_index=index)
    value.flush()
    assert value.dropped == 3
    assert [row["round_index"] for row in read(value)] == [0, 1]
    value.close()


def test_query_selects_one_session_and_summarises_stages(tracer):
    tracer.record("answer.accepted", stage="api", session_id="s-1", operation_id="o-1")
    tracer.record("answer.accepted", stage="api", session_id="s-2", operation_id="o-2")
    with tracer.span("interview.failed", stage="worker") as span:
        span.set(session_id="s-1")
        span.fail("model_budget_exhausted")
        span.set(error_category=obs.category_of("model_budget_exhausted"))
    tracer.flush()
    all_rows, unreadable = obs.load(tracer.path.parent)
    assert unreadable == 0 and len(all_rows) == 3
    chosen = obs.select(all_rows, session_id="s-1")
    assert [row["event_name"] for row in chosen] == ["answer.accepted", "interview.failed"]
    summary = obs.summarize(chosen)
    assert summary["events"] == 2 and summary["stages"] == {"api": 1, "worker": 1}
    assert summary["failures_by_category"] == {"budget": 1}
    assert obs.select(all_rows) == []


def test_categories_are_bounded_and_unknown_codes_stay_unknown():
    assert obs.category_of("model_budget_exhausted") == "budget"
    assert obs.category_of("model_payload_rejected") == "provider"
    assert obs.category_of("read_topics_before_submission") == "tool"
    assert obs.category_of("question_not_in_pack") == "guardrail"
    assert obs.category_of("version_conflict") == "input"
    assert obs.category_of("lease_held") == "queue"
    assert obs.category_of("not_a_real_code") == "unknown"
    assert obs.category_of(None) == "unknown"


def test_wrong_tool_is_traceable_and_the_self_correction_still_succeeds(tracer):
    value = Decision(action="ask_question", topic_id="java-concurrency", question_id="java-concurrency-base")

    class Repair:
        n = 0

        async def complete(self, messages, tools):
            self.n += 1
            name, args = [("read_interview_scope", {}),
                          ("submit_interview_action", value.model_dump()),
                          ("read_topics", {"topic_ids": ["java-concurrency"]}),
                          ("submit_interview_action", value.model_dump())][self.n - 1]
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    with obs.use(tracer):
        result = asyncio.run(interview(scope(), Repair(), asyncio.Event()))
    assert result.action == "ask_question"
    data = read(tracer)
    rounds = [row for row in data if row["event_name"] == "interview.model_round"]
    tools = [row for row in data if row["event_name"] == "interview.tool"]
    assert [row["round_index"] for row in rounds] == [1, 2, 3, 4]
    assert [row["tool_status"] for row in tools] == ["ok", "error", "ok", "ok"]
    assert tools[1]["reason_code"] == "read_topics_before_submission"
    assert tools[1]["error_category"] == "tool"
    assert all(row["status"] != "error" for row in rounds)


def test_tracing_changes_neither_the_decision_nor_the_scope(tracer):
    text = "不知道，只加volatile。"
    off_scope, on_scope = scope(answer=text), scope(answer=text)

    async def admit():
        pass

    started = perf_counter()
    with obs.use(obs.NullTracer()):
        without = asyncio.run(interview(off_scope, ScriptedInterviewer(off_scope, admit), asyncio.Event()))
    untraced_ms = (perf_counter() - started) * 1000

    started = perf_counter()
    with obs.use(tracer):
        with_trace = asyncio.run(interview(on_scope, ScriptedInterviewer(on_scope, admit), asyncio.Event()))
    traced_ms = (perf_counter() - started) * 1000

    assert (without.action, without.question_id) == (with_trace.action, with_trace.question_id)
    assert (without.model_dump(exclude={"observation", "tool_log"})
            == with_trace.model_dump(exclude={"observation", "tool_log"}))
    assert (without.observation.model_dump(exclude={"answer_id"})
            == with_trace.observation.model_dump(exclude={"answer_id"}))
    # The two runs get different generated answer ids, so the content fingerprint of the same
    # submission differs by design (S06B). Compare the log without that derived field.
    def stable(entries):
        return [{key: value for key, value in entry.items() if key != 'submission_hash'}
                for entry in entries]
    assert stable(with_trace.tool_log) == stable(without.tool_log)
    rows = read(tracer)
    assert len(rows) >= 5 and {row["event_name"] for row in rows} >= {"interview.model_round", "interview.tool"}
    # Bounded metadata: one scripted decision cannot produce an unbounded file.
    assert tracer.path.stat().st_size < 16384
    # Measured, not asserted: the report records this pair on the same machine.
    assert traced_ms >= 0 and untraced_ms >= 0
def test_unknown_settlement_is_traced_with_the_ledger_call_id(tracer, project_root):
    """MockTransport: the transport records the call id and UNKNOWN, money stays in the ledger."""
    import httpx
    from interview_agent.billing.ledger import BillingUnavailable
    from interview_agent.career.coordinator import Tool
    from interview_agent.interviews.billing import InterviewPolicy
    from interview_agent.runtime.deepseek import BudgetedTransport, DeepSeekProvider
    from interview_agent.runtime.types import AssistantResponse, ToolCall
    from test_billing import reply

    policy = InterviewPolicy.load(project_root / "configs/ja05-deepseek-flash.json")

    class Ledger:
        def __init__(self):
            self.policy = policy

        async def reserve(self, *args):
            return "call-unknown"

        async def settle(self, *args, **kwargs):
            return False

    async def scenario():
        def handler(request):
            return httpx.Response(200, json=reply(AssistantResponse(
                tool_calls=[ToolCall(id="1", name="read_interview_scope", arguments={})])))

        provider = DeepSeekProvider("offline-not-real",
                                    BudgetedTransport(httpx.MockTransport(handler), Ledger(), None, {}),
                                    system_prompt="synthetic", tracer=tracer)
        try:
            with pytest.raises(BillingUnavailable, match="model_usage_unknown"):
                await provider.complete([], [Tool(name="read_interview_scope", description="d",
                                                 parameters={"type": "object", "properties": {}})])
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    records = {row["event_name"]: row for row in read(tracer)}
    http = records["provider.http"]
    assert http["model_call_id"] == "call-unknown"
    assert http["usage_status"] == "unknown" and http["status"] == "error"
    assert http["reason_code"] == "model_usage_unknown"
    assert http["http_status"] == 200 and http["request_bytes"] > 0
    assert records["provider.complete"]["status"] == "error"
    assert "reserved_nano" in http
def test_citation_rejections_are_traceable_and_self_correction_succeeds(tracer):
    from test_interview_agent import observation

    state = scope(answer="只加volatile，我不清楚复合操作。")
    good = Decision(action="ask_question", topic_id="java-concurrency", question_id="java-concurrency-probe",
                    observation=observation(state)).model_dump()
    bad_answer = {**good, "observation": {**good["observation"], "answer_quote": "不存在的回答"}}
    bad_reference = {**good, "observation": {**good["observation"], "evidence_quote": "凭空补造的标准答案"}}

    class RepairCitations:
        n = 0

        async def complete(self, messages, tools):
            self.n += 1
            name, args = [("read_interview_scope", {}),
                          ("submit_interview_action", bad_answer),
                          ("submit_interview_action", bad_reference),
                          ("submit_interview_action", good)][self.n - 1]
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    with obs.use(tracer):
        result = asyncio.run(interview(state, RepairCitations(), asyncio.Event()))
    assert result.action == "ask_question" and result.question_id == "java-concurrency-probe"
    tools = [row for row in read(tracer) if row["event_name"] == "interview.tool"]
    rejected = [row for row in tools if row["tool_status"] == "error"]
    assert [row["reason_code"] for row in rejected] == ["answer_citation_required", "reference_citation_required"]
    assert {row["error_category"] for row in rejected} == {"guardrail"}
    assert tools[-1]["tool_status"] == "ok"