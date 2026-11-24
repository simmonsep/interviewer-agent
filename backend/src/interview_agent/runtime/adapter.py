"""A real two-node LangGraph, supervised by operation-owned limits and cleanup."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from interview_agent.contracts.runtime import RunCounts, RunEnvelope, RunInput, RunUsage, HttpAudit, TOOL_NAMES

from .budget import OperationBudget, fingerprint
from .checkpoint import SavedState, open_checkpoint, seal
from .lifecycle import OwnedTasks
from .store import ReadWindow, ScopedAssessmentStore, canonical
from .tools import execute, specifications
from .trace import MemoryTrace, TraceRecord, TraceSink
from .types import AssistantResponse, Message, Provider, RunResult, ScopeError, SealedCheckpoint


class GraphState(TypedDict):
    messages: list[Message]
    response: AssistantResponse | None


@dataclass
class Context:
    input: RunInput
    store: ScopedAssessmentStore
    budget: OperationBudget
    counts: RunCounts
    owned: OwnedTasks
    trace: TraceSink
    reads: list[ReadWindow] = field(default_factory=list)
    candidate: object | None = None
    termination: str | None = None
    error_code: str | None = None
    sequence: int = 0
    turns_in_run: int = 0
    cancel: asyncio.Event | None = None

    async def emit(self, event: str, **fields) -> None:
        self.sequence += 1
        try:
            await self.owned.call(self.trace.append(TraceRecord(sequence=self.sequence, event=event, **fields)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ScopeError("trace_write_failed") from exc


class InterviewRuntimeAdapter:
    """One operation per adapter. Resume preserves counters/deadline; no implicit retries."""

    def __init__(self, store: ScopedAssessmentStore, provider: Provider, *, cleanup_grace_s: float = 0.1,
                 before_tool: Callable[[str], Awaitable[None]] | None = None,
                 tool_execution: Literal["sequential", "parallel"] = "sequential"):
        if not 0.01 <= cleanup_grace_s <= 1:
            raise ScopeError("invalid_runtime_options")
        self.store, self.provider = store, provider
        self.grace, self.before_tool = cleanup_grace_s, before_tool
        if tool_execution not in ("sequential", "parallel"):
            raise ScopeError("invalid_runtime_options")
        self.tool_execution = tool_execution
        self._active = self._quarantined = self._finished = False
        self._budget: OperationBudget | None = None
        self._counts = RunCounts(**{name: 0 for name in RunCounts.model_fields})
        self._used_checkpoints: set[str] = set()

    def _graph(self, ctx: Context, pause_after_turns: int | None):
        async def model(state: GraphState):
            ctx.counts.provider_request_attempts += 1
            refusal = ctx.budget.request()
            if refusal:
                ctx.termination = "timeout" if refusal == "wall_deadline" else "budget_exhausted"
                ctx.error_code = refusal
                return {}
            ctx.counts.provider_requests_admitted += 1
            await ctx.emit("model")
            try:
                response = await ctx.owned.call(self.provider.complete(
                    [m.model_copy(deep=True) for m in state["messages"]], specifications(ctx.input.allowed_tools)))
            except asyncio.CancelledError:
                raise
            except Exception:
                ctx.termination, ctx.error_code = "provider_error", "provider_failure"
                return {}
            try:
                raw = response.model_dump(mode="json") if isinstance(response, AssistantResponse) else response
                response = AssistantResponse.model_validate(raw)
                if len(canonical(response.model_dump(mode="json")).encode()) > 131072:
                    raise ValueError("oversized response")
            except (ValidationError, ValueError, TypeError, UnicodeError):
                ctx.termination, ctx.error_code = "invalid_output", "invalid_provider_output"
                return {}
            ctx.counts.assistant_turns += 1
            ctx.turns_in_run += 1
            if not response.tool_calls:
                ctx.termination = "tool_error" if ctx.counts.tool_errors else "invalid_output"
                ctx.error_code = "no_valid_candidate"
            return {"messages": state["messages"] + [Message(role="assistant", text=response.text,
                    tool_calls=response.tool_calls)], "response": response}

        async def tools(state: GraphState):
            calls = state["response"].tool_calls
            ctx.counts.tool_calls_proposed += len(calls)
            # Only allowlisted calls consume execution slots; the complete admitted
            # batch is reserved once before any member runs (no partial overflow).
            admitted = [c for c in calls if c.name in ctx.input.allowed_tools and c.name in TOOL_NAMES]
            used = {c.id for m in state["messages"][:-1] for c in m.tool_calls}
            if len({c.id for c in calls}) != len(calls) or any(c.id in used for c in calls):
                ctx.termination, ctx.error_code = "invalid_output", "duplicate_tool_call_id"
                return {}
            if admitted and not ctx.budget.batch(len(admitted)):
                ctx.termination, ctx.error_code = "budget_exhausted", "tool_budget"
                return {}
            ctx.counts.tool_calls_admitted += len(admitted)
            prior_reads = [r.model_copy(deep=True) for r in ctx.reads]
            messages = list(state["messages"])
            delivered: list[ReadWindow] = []
            async def perform(call):
                if self.before_tool and call.name in ctx.input.allowed_tools:
                    await ctx.owned.call(self.before_tool(call.name))
                if ctx.cancel is not None and ctx.cancel.is_set():
                    ctx.termination, ctx.error_code = "cancelled", "caller_cancelled"
                    return None
                if ctx.budget.remaining() <= 0:
                    ctx.termination, ctx.error_code = "timeout", "wall_deadline"
                    return None
                result, windows = execute(ctx, call, prior_reads)
                await ctx.emit("tool", tool_name=call.name if call.name in TOOL_NAMES else None, is_error=result.is_error)
                return result, windows
            if self.tool_execution == "parallel":
                outcomes = await ctx.owned.call(asyncio.gather(*(perform(call) for call in calls)))
            else:
                outcomes = []
                for call in calls:
                    outcome = await perform(call)
                    outcomes.append(outcome)
                    if outcome is None:
                        break
            for outcome in outcomes:
                if outcome is not None:
                    result, windows = outcome
                    messages.append(result)
                    delivered.extend(windows)
            ctx.reads.extend(delivered)
            if ctx.candidate is not None and ctx.termination not in ("cancelled", "timeout"):
                ctx.termination = "candidate_ready"
            elif ctx.termination is None and pause_after_turns and ctx.turns_in_run >= pause_after_turns:
                ctx.termination = "needs_review"
            return {"messages": messages, "response": None}

        graph = StateGraph(GraphState)
        graph.add_node("model", model, retry_policy=None)
        graph.add_node("tools", tools, retry_policy=None)
        graph.add_edge(START, "model")
        graph.add_conditional_edges("model", lambda _: END if ctx.termination else "tools")
        graph.add_conditional_edges("tools", lambda _: END if ctx.termination else "model")
        return graph.compile(name="interview-assessment")

    async def _cleanup(self, ctx: Context, owned: OwnedTasks, diagnostics: list[str]) -> bool:
        complete = await owned.settle(self.grace)

        async def finish_trace():
            try:
                await ctx.trace.append(TraceRecord(sequence=ctx.sequence + 1, event="result",
                                                  termination=ctx.termination or "runtime_error"))
            finally:
                await ctx.trace.close()

        closing = owned.track(finish_trace())
        done, _ = await asyncio.wait({closing}, timeout=self.grace)
        if closing in done:
            try:
                closing.result()
            except (Exception, asyncio.CancelledError):
                complete = False
                diagnostics.append("trace_close_failed")
        else:
            complete = False
            diagnostics.append("trace_close_timeout")
        complete = await owned.settle(self.grace) and complete
        if not complete:
            self._quarantined = True
            diagnostics.append("resource_cleanup_failed")
            if ctx.termination not in ("provider_error", "cancelled", "timeout", "runtime_error"):
                ctx.termination, ctx.error_code = "runtime_error", "resource_cleanup_failed"
        return complete

    async def run(self, run: RunInput, *, cancel: asyncio.Event | None = None, trace: TraceSink | None = None,
                  pause_after_turns: int | None = None, checkpoint_key: bytes | None = None,
                  resume: SealedCheckpoint | None = None) -> RunResult:
        if self._active or self._quarantined or self._finished:
            raise ScopeError("adapter_unavailable")
        if pause_after_turns is not None and (type(pause_after_turns) is not int or not 1 <= pause_after_turns <= 6):
            raise ScopeError("invalid_runtime_options")
        run = RunInput.model_validate(run.model_dump(mode="json"))
        if self._budget is None:
            self._budget = OperationBudget(run)
        elif self._budget.binding != fingerprint(run):
            raise ScopeError("budget_scope_mismatch")
        self._active = True
        owned = OwnedTasks()
        ctx = Context(run, self.store, self._budget, self._counts, owned, trace if trace is not None else MemoryTrace())
        ctx.cancel = cancel
        run_id = str(uuid4())
        messages = [Message(role="user", text=canonical({"task": "assess_current_answer", "question_id": run.question_id,
                    "sources": [r.model_dump(mode="json") for r in run.context_refs]}))]
        exported = None
        cleanup_complete = True
        diagnostics: list[str] = []
        try:
            self.store.question(run, run.question_id)
            if self.provider.profile != run.model_profile:
                raise ScopeError("provider_profile_mismatch")
            if resume is not None:
                saved = open_checkpoint(resume, run, self.store, checkpoint_key)
                if resume.signature in self._used_checkpoints:
                    raise ScopeError("checkpoint_already_used")
                self._used_checkpoints.add(resume.signature)
                ctx.budget.restore(saved.budget)
                for name in RunCounts.model_fields:
                    setattr(ctx.counts, name, max(getattr(ctx.counts, name), getattr(saved.counts, name)))
                messages, ctx.reads = saved.messages, saved.reads
            graph = self._graph(ctx, pause_after_turns)

            async def invoke():
                await ctx.emit("start")
                return await graph.ainvoke({"messages": messages, "response": None},
                                          config={"recursion_limit": 2 * run.budget.turns + 4})

            if cancel is not None and cancel.is_set():
                ctx.termination, ctx.error_code = "cancelled", "caller_cancelled"
            elif ctx.budget.remaining() <= 0:
                ctx.termination, ctx.error_code = "timeout", "wall_deadline"
            else:
                graph_task = owned.track(invoke())
                cancel_task = owned.track(cancel.wait()) if cancel is not None else None
                watched = {graph_task} | ({cancel_task} if cancel_task is not None else set())
                done, _ = await asyncio.wait(watched, timeout=ctx.budget.remaining(), return_when=asyncio.FIRST_COMPLETED)
                if cancel_task is not None and cancel_task in done:
                    ctx.termination, ctx.error_code = "cancelled", "caller_cancelled"
                elif ctx.budget.remaining() <= 0 or graph_task not in done:
                    ctx.termination, ctx.error_code = "timeout", "wall_deadline"
                else:
                    state = graph_task.result()
                    messages = state["messages"]
            if ctx.termination == "needs_review":
                exported = seal(SavedState(version="py-runtime-v1", binding=fingerprint(run), snapshot_digest=self.store.digest,
                    run_id=run_id, messages=messages, reads=ctx.reads, budget=ctx.budget.used.model_copy(deep=True),
                    counts=ctx.counts.model_copy(deep=True)), run, checkpoint_key)
        except asyncio.CancelledError:
            ctx.termination, ctx.error_code = "cancelled", "caller_cancelled"
            raise
        except ScopeError as exc:
            ctx.termination, ctx.error_code = "runtime_error", exc.code
        except Exception:
            ctx.termination, ctx.error_code = "runtime_error", "runtime_failure"
        finally:
            # finally can itself be cancelled at an await. Keep ownership of the
            # bounded cleanup task, then propagate every caller cancellation.
            cleanup = asyncio.create_task(self._cleanup(ctx, owned, diagnostics))
            interrupted = False
            try:
                while True:
                    try:
                        cleanup_complete = await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        if cleanup.cancelled():
                            cleanup_complete = False
                            self._quarantined = True
                            diagnostics.append("resource_cleanup_cancelled")
                            if ctx.termination not in ("cancelled", "timeout", "provider_error"):
                                ctx.termination, ctx.error_code = "runtime_error", "resource_cleanup_failed"
                            break
                        interrupted = True
                        ctx.termination, ctx.error_code = "cancelled", "caller_cancelled"
                if cancel is not None and cancel.is_set():
                    ctx.termination, ctx.error_code = "cancelled", "caller_cancelled"
            finally:
                self._active = False
                self._finished = ctx.termination != "needs_review"
            if interrupted:
                raise asyncio.CancelledError
        termination = ctx.termination or "invalid_output"
        candidate = ctx.candidate if termination == "candidate_ready" and cleanup_complete else None
        if termination != "needs_review" or not cleanup_complete:
            exported = None
        envelope = RunEnvelope(operation_id=run.operation_id, run_id=run_id, parent_run_id=run.parent_run_id,
            session_id=run.session_id, termination=termination, candidate=candidate, committed=False,
            error_code=ctx.error_code, counts=ctx.counts.model_copy(deep=True),
            usage=RunUsage(source="scripted" if run.model_profile == "pm003-scripted" else "unavailable",
                           input_tokens=None, output_tokens=None, estimated_cost=None),
            http_audit=HttpAudit(attempts=None, scope="not_observed_by_adapter"), trace_ref=None,
            diagnostic_errors=diagnostics, budget_state=ctx.budget.used.model_copy(deep=True))
        return RunResult(envelope=envelope, checkpoint=exported, cleanup_complete=cleanup_complete)
