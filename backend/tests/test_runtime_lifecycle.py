import asyncio
import hashlib
import hmac
import json
import time

import pytest

from interview_agent.contracts.runtime import RunInput
from interview_agent.runtime.adapter import InterviewRuntimeAdapter
from interview_agent.runtime.store import ScopedAssessmentStore
from interview_agent.runtime.trace import MemoryTrace
from interview_agent.runtime.types import ScopeError, SealedCheckpoint
from runtime_support import bundle, run_input, ScriptedProvider, call, success_steps, response

KEY = b"synthetic-checkpoint-key-32-bytes!"


class BlockingProvider:
    profile = "pm003-scripted"

    def __init__(self, ignore_cancel=False):
        self.started, self.release, self.closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.ignore_cancel = ignore_cancel

    async def complete(self, messages, tools):
        self.started.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancel:
                        raise
            return response(text="late response")
        finally:
            self.closed.set()


@pytest.mark.parametrize("native", [False, True])
def test_py_p11_event_cancel_returns_and_native_cancel_propagates(native):
    async def scenario():
        provider, cancel = BlockingProvider(), asyncio.Event()
        adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), provider)
        task = asyncio.create_task(adapter.run(run_input(), cancel=cancel))
        await asyncio.wait_for(provider.started.wait(), 1)
        if native:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            cancel.set()
            result = await asyncio.wait_for(task, 1)
            assert result.envelope.termination == "cancelled" and result.cleanup_complete
            assert result.envelope.candidate is None
        assert provider.closed.is_set()
    asyncio.run(scenario())


def test_py_p11_deadline_reaches_provider_and_stops_late_output():
    async def scenario():
        provider = BlockingProvider()
        run = run_input(budget={"provider_requests": 8, "tool_calls": 12, "wall_ms": 30})
        result = await asyncio.wait_for(InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), provider).run(run), 1)
        assert result.envelope.termination == "timeout" and result.envelope.candidate is None
        assert result.cleanup_complete and provider.closed.is_set()
        assert result.envelope.budget_state.provider_requests == 1
    asyncio.run(scenario())


def test_py_p11_cancellation_reaches_tool_hook_before_execution():
    async def scenario():
        entered, closed, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def hook(name):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        provider = ScriptedProvider(success_steps(bundle(), run_input()))
        adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), provider, before_tool=hook)
        task = asyncio.create_task(adapter.run(run_input(), cancel=cancel))
        await asyncio.wait_for(entered.wait(), 1)
        cancel.set()
        result = await asyncio.wait_for(task, 1)
        assert result.envelope.termination == "cancelled" and closed.is_set()
        assert result.envelope.counts.tool_executions == 0 and result.cleanup_complete
    asyncio.run(scenario())


def test_py_p11_noncooperative_provider_is_quarantined_and_released_for_test_cleanup():
    async def scenario():
        provider = BlockingProvider(ignore_cancel=True)
        run = run_input(budget={"provider_requests": 8, "tool_calls": 12, "wall_ms": 30})
        adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), provider, cleanup_grace_s=0.01)
        try:
            result = await asyncio.wait_for(adapter.run(run), 1)
            assert result.envelope.termination == "timeout" and result.envelope.candidate is None
            assert not result.cleanup_complete and "resource_cleanup_failed" in result.envelope.diagnostic_errors
            with pytest.raises(ScopeError, match="adapter_unavailable"):
                await adapter.run(run)
        finally:
            provider.release.set()
            await asyncio.wait_for(provider.closed.wait(), 1)
    asyncio.run(scenario())


def test_py_p12_mutating_caller_objects_does_not_change_running_scope():
    async def scenario():
        data, run = bundle(), run_input()
        provider = ScriptedProvider(success_steps(data, run))
        async def hook(name):
            run.allowed_tools.clear()
            data.answers[0].lines[0] = "outside mutation"
        result = await InterviewRuntimeAdapter(ScopedAssessmentStore(data), provider, before_tool=hook).run(run)
        assert result.envelope.termination == "candidate_ready"
        assert result.envelope.candidate.citations[1].quote != "outside mutation"
    asyncio.run(scenario())


def test_py_p12_hook_failure_cannot_collect_candidate():
    async def scenario():
        async def hook(name):
            raise RuntimeError("HOOK-SECRET-CANARY")
        provider = ScriptedProvider(success_steps(bundle(), run_input()))
        result = await InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), provider, before_tool=hook).run(run_input())
        assert result.envelope.termination == "runtime_error" and result.envelope.candidate is None
        assert result.envelope.counts.tool_executions == 0
        assert "HOOK-SECRET-CANARY" not in result.model_dump_json()
    asyncio.run(scenario())


def resumed_input(paused, **patch):
    return run_input(parent_run_id=paused.envelope.run_id, **patch)


async def pause():
    run, data = run_input(), bundle()
    result = await InterviewRuntimeAdapter(ScopedAssessmentStore(data), ScriptedProvider(success_steps(data, run))).run(
        run, pause_after_turns=1, checkpoint_key=KEY)
    assert result.envelope.termination == "needs_review" and result.checkpoint is not None
    return result


def test_py_p13_serialized_checkpoint_reopens_in_new_adapter_without_budget_reset(tmp_path):
    async def scenario():
        paused = await pause()
        path = tmp_path / "synthetic-checkpoint.json"
        path.write_text(paused.checkpoint.model_dump_json(), encoding="utf-8")
        saved = SealedCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
        run = resumed_input(paused)
        provider = ScriptedProvider(success_steps(bundle(), run)[1:])
        result = await InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), provider).run(run, resume=saved, checkpoint_key=KEY)
        assert result.envelope.termination == "candidate_ready"
        assert result.envelope.parent_run_id == paused.envelope.run_id
        assert result.envelope.budget_state.provider_requests == 2
        assert result.envelope.counts.tool_executions == 4
        assert result.envelope.budget_state.deadline_epoch_ms == paused.envelope.budget_state.deadline_epoch_ms
        assert len(provider.requests) == 1
        assert [m.role for m in provider.requests[0]] == ["user", "assistant", "tool", "tool", "tool"]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["tamper", "wrong_key", "scope", "snapshot", "lineage"])
def test_py_p13_checkpoint_integrity_and_scope_fail_before_provider(kind):
    async def scenario():
        paused = await pause()
        saved, key, data = paused.checkpoint, KEY, bundle()
        run = resumed_input(paused)
        if kind == "tamper":
            saved = SealedCheckpoint(payload=saved.payload + " ", signature=saved.signature)
        elif kind == "wrong_key":
            key = b"different-synthetic-key-32-bytes!"
        elif kind == "scope":
            raw = run.model_dump(mode="json")
            raw["operation_id"] = "operation-another"
            run = RunInput.model_validate(raw)
        elif kind == "snapshot":
            data.evidence[0].lines[0] = "changed without version bump"
        else:
            run = run_input()
        provider = ScriptedProvider([])
        result = await InterviewRuntimeAdapter(ScopedAssessmentStore(data), provider).run(run, resume=saved, checkpoint_key=key)
        assert result.envelope.termination == "runtime_error" and not provider.requests
        assert result.checkpoint is None and result.envelope.candidate is None
    asyncio.run(scenario())


def signed(payload):
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return SealedCheckpoint(payload=text, signature=hmac.new(KEY, text.encode(), hashlib.sha256).hexdigest())


@pytest.mark.parametrize("kind", ["pairing", "budget", "expired"])
def test_py_p13_valid_signature_does_not_replace_structural_or_budget_checks(kind):
    async def scenario():
        paused = await pause()
        value = json.loads(paused.checkpoint.payload)
        if kind == "pairing":
            value["messages"][-1]["tool_call_id"] = "not-paired"
        elif kind == "budget":
            value["budget"]["tool_calls"] = 99
            value["counts"]["tool_calls_admitted"] = 99
            value["counts"]["tool_calls_proposed"] = 99
        else:
            value["budget"]["deadline_epoch_ms"] = int(time.time() * 1000) - 1
        provider = ScriptedProvider([])
        result = await InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), provider).run(
            resumed_input(paused), resume=signed(value), checkpoint_key=KEY)
        assert result.envelope.termination == ("timeout" if kind == "expired" else "runtime_error")
        assert not provider.requests and result.envelope.candidate is None
    asyncio.run(scenario())


def test_py_p13_missing_checkpoint_key_does_not_export_unprotected_state():
    async def scenario():
        result = await InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), ScriptedProvider(success_steps(bundle(), run_input()))).run(
            run_input(), pause_after_turns=1)
        assert result.envelope.error_code == "checkpoint_key_required" and result.checkpoint is None
    asyncio.run(scenario())


def test_py_p14_reentry_and_finished_adapter_are_rejected():
    async def scenario():
        provider, cancel = BlockingProvider(), asyncio.Event()
        adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), provider)
        task = asyncio.create_task(adapter.run(run_input(), cancel=cancel))
        await asyncio.wait_for(provider.started.wait(), 1)
        with pytest.raises(ScopeError, match="adapter_unavailable"):
            await adapter.run(run_input())
        cancel.set()
        await task
        with pytest.raises(ScopeError, match="adapter_unavailable"):
            await adapter.run(run_input())
    asyncio.run(scenario())


class CloseFailure(MemoryTrace):
    async def close(self):
        raise RuntimeError("TRACE-SECRET-CANARY")


def test_py_p15_trace_close_failure_invalidates_success_candidate():
    async def scenario():
        adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), ScriptedProvider(success_steps(bundle(), run_input())))
        result = await adapter.run(run_input(), trace=CloseFailure())
        assert not result.cleanup_complete and result.envelope.candidate is None
        assert result.envelope.termination == "runtime_error"
        assert "trace_close_failed" in result.envelope.diagnostic_errors
        assert "TRACE-SECRET-CANARY" not in result.model_dump_json()
    asyncio.run(scenario())


def test_py_p15_primary_provider_failure_survives_trace_close_failure():
    async def scenario():
        def fail(*_):
            raise RuntimeError("provider failure")
        result = await InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), ScriptedProvider([fail])).run(run_input(), trace=CloseFailure())
        assert result.envelope.termination == "provider_error" and result.envelope.error_code == "provider_failure"
        assert not result.cleanup_complete
    asyncio.run(scenario())


def test_py_p15_trace_append_failure_still_attempts_close():
    class AppendFailure(MemoryTrace):
        async def append(self, record):
            raise RuntimeError("append failure")
    async def scenario():
        sink = AppendFailure()
        result = await InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), ScriptedProvider([])).run(run_input(), trace=sink)
        assert sink.closed and result.envelope.error_code == "trace_write_failed"
        assert result.envelope.candidate is None
    asyncio.run(scenario())


def test_native_cancel_during_trace_close_waits_for_owned_cleanup():
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        class DelayedClose(MemoryTrace):
            async def close(self):
                entered.set()
                try:
                    await asyncio.sleep(0.03)
                finally:
                    closed.set()
                    await super().close()
        adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), ScriptedProvider(success_steps(bundle(), run_input())))
        task = asyncio.create_task(adapter.run(run_input(), trace=DelayedClose()))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set(), "native cancellation escaped before trace cleanup"
        finally:
            await asyncio.wait_for(closed.wait(), 1)
    asyncio.run(scenario())


def test_checkpoint_cannot_rewind_same_adapter_and_restore_counters():
    async def scenario():
        run, data = run_input(), bundle()
        provider = ScriptedProvider([success_steps(data, run)[0], response(call("again", "read_question", question_id=run.question_id))])
        adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(data), provider)
        first = await adapter.run(run, pause_after_turns=1, checkpoint_key=KEY)
        followup = resumed_input(first)
        second = await adapter.run(followup, resume=first.checkpoint, pause_after_turns=1, checkpoint_key=KEY)
        assert second.envelope.termination == "needs_review"
        replay = await adapter.run(followup, resume=first.checkpoint, checkpoint_key=KEY)
        assert replay.envelope.error_code == "checkpoint_already_used"
        assert replay.envelope.budget_state.provider_requests == 2 and len(provider.requests) == 2
    asyncio.run(scenario())


def test_hanging_trace_close_is_bounded_and_cannot_return_success():
    async def scenario():
        closed = asyncio.Event()
        class HangingClose(MemoryTrace):
            async def close(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()
        adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), ScriptedProvider(success_steps(bundle(), run_input())), cleanup_grace_s=0.01)
        result = await asyncio.wait_for(adapter.run(run_input(), trace=HangingClose()), 1)
        assert not result.cleanup_complete and result.envelope.candidate is None
        assert "trace_close_timeout" in result.envelope.diagnostic_errors and closed.is_set()
    asyncio.run(scenario())


def test_trace_close_self_cancellation_is_a_cleanup_failure_not_an_infinite_wait():
    async def scenario():
        class CancelledClose(MemoryTrace):
            async def close(self):
                raise asyncio.CancelledError
        adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), ScriptedProvider(success_steps(bundle(), run_input())))
        result = await asyncio.wait_for(adapter.run(run_input(), trace=CancelledClose()), 1)
        assert not result.cleanup_complete and result.envelope.candidate is None
        assert "trace_close_failed" in result.envelope.diagnostic_errors
    asyncio.run(scenario())


def test_event_cancel_during_cleanup_invalidates_already_collected_candidate():
    async def scenario():
        cancel = asyncio.Event()
        class CancelOnClose(MemoryTrace):
            async def close(self):
                cancel.set()
                await super().close()
        adapter = InterviewRuntimeAdapter(ScopedAssessmentStore(bundle()), ScriptedProvider(success_steps(bundle(), run_input())))
        result = await adapter.run(run_input(), cancel=cancel, trace=CancelOnClose())
        assert result.envelope.termination == "cancelled" and result.envelope.candidate is None
    asyncio.run(scenario())
