import assert from "node:assert/strict";
import { after, test } from "node:test";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createHmac } from "node:crypto";
import { Agent, type StreamFn } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream, type AssistantMessage } from "@earendil-works/pi-ai";
import { TOOL_NAMES, type RunInput, type AssessmentCandidate, type FixtureBundle } from "@pi-interview/contracts";
import { PiRuntimeAdapter, runAssessment, SCRIPT_MODEL, type RuntimeOptions, type RunResult } from "../src/adapter.ts";
import { ScopedFixtureStore } from "../src/scoped-store.ts";
import { OperationBudget } from "../src/budget.ts";
import { JsonlTraceSink, MemoryTraceSink, type TraceSink } from "../src/trace.ts";
import { createScriptedStream, payload, toolResult, type ScriptStep, type ScriptCall } from "../test-support/scripted-stream.ts";

const fixture: FixtureBundle = JSON.parse(readFileSync(new URL("../../../fixtures/pm0-03/bundle.json", import.meta.url), "utf8"));
const directory = process.env.PI_PM004_DIRECTORY!;
const cases: { name: string; status: string; evidence?: unknown; error?: string }[] = [];
after(() => { if (process.env.PI_PM003_RECEIPT_PATH) writeFileSync(process.env.PI_PM003_RECEIPT_PATH, JSON.stringify(cases, null, 2)); });
function check(name: string, fn: () => Promise<unknown>) {
  test(name, { timeout: 6000 }, async () => {
    try { cases.push({ name, status: "passed", evidence: await fn() }); }
    catch (e) { cases.push({ name, status: "failed", error: e instanceof Error ? e.message : "failure" }); throw e; }
  });
}
function input(patch: Partial<RunInput> = {}): RunInput {
  return { server_bound_user: "user-a", session_id: "session-a", operation_id: "operation-a", session_version: 1, parent_run_id: null,
    mode: "mock", context_mode: "role_only", role: "assessment", target_role: "agent-intern", question_id: "q-tool-safety",
    context_refs: [{ source_id: "kb-tool-safety", version: "v1" }], allowed_tools: [...TOOL_NAMES], model_profile: "pm003-scripted",
    budget: { provider_requests: 8, tool_calls: 12, turns: 6, wall_ms: 10000 }, ...patch };
}
const call = (id: string, name: string, args: ScriptCall["arguments"] = {}): ScriptCall => ({ id, name, arguments: args });
const q = (id = "question-call") => call(id, "read_question", { question_id: "q-tool-safety" });
function candidate(i = input()): AssessmentCandidate {
  const session = fixture.sessions.find(s => s.id === i.session_id)!;
  const a = fixture.answers.find(a => a.id === session.answer_id) ?? fixture.answers[0];
  return { question_id: i.question_id, answer_id: a.id, verdict: "partial", summary: "合成协议候选，语义未评测", feedback: "讨论授权边界。", citations: [
    { kind: "evidence", source_id: "kb-tool-safety", version: "v1", start_line: 1, end_line: 2, quote: fixture.evidence[0].lines.slice(0, 2).join("\n") },
    { kind: "answer", source_id: a.id, version: a.version, start_line: 1, end_line: a.lines.length, quote: a.lines.join("\n") },
  ] };
}
function steps(i = input(), assessment = candidate(i)): ScriptStep[] {
  return [
    { calls: [q()] },
    { calls: [call("evidence-call", "read_evidence", { source_id: "kb-tool-safety", version: "v1", start_line: 1, end_line: 2 })], inspect: c => assert.equal(toolResult(c, "question-call").isError, false) },
    { calls: [call("answer-call", "read_current_answer")], inspect: c => assert.equal(toolResult(c, "evidence-call").isError, false) },
    { calls: [call("submit-call", "submit_assessment", assessment)], inspect: c => assert.equal(toolResult(c, "answer-call").isError, false) },
  ];
}
async function execute(scriptSteps: ScriptStep[], i = input(), options: RuntimeOptions = {}) {
  const script = createScriptedStream(scriptSteps, i.allowed_tools);
  const result = await runAssessment(i, new ScopedFixtureStore(fixture), script.streamFn, options);
  assert.deepEqual(script.failures, []);
  return { result, script };
}
function info(r: RunResult) { return { envelope: r.envelope, cleanup_complete: r.cleanup_complete }; }
function message(aborted = false): AssistantMessage {
  return { role: "assistant", api: SCRIPT_MODEL.api, provider: SCRIPT_MODEL.provider, model: SCRIPT_MODEL.id, content: [], stopReason: aborted ? "aborted" : "error", timestamp: Date.now(),
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
}
function blocker() {
  let started!: () => void, active = 0, aborted = false;
  const ready = new Promise<void>(resolve => { started = resolve; });
  const stream: StreamFn = (_m, _c, options) => {
    const s = createAssistantMessageEventStream(); active++;
    const interval = setInterval(() => {}, 100);
    const stop = () => { aborted = true; active--; clearInterval(interval); s.push({ type: "error", reason: "aborted", error: message(true) }); s.end(); };
    options?.signal?.addEventListener("abort", stop, { once: true }); started();
    return s;
  };
  return { stream, ready, stats: () => ({ active, aborted }) };
}

check("P01 normal Pi loop, candidate only, persisted metadata trace", async () => {
  const { result, script } = await execute(steps(), input(), { traceFactory: id => new JsonlTraceSink(directory, id) });
  assert.equal(result.envelope.termination, "candidate_ready"); assert.equal(result.envelope.committed, false);
  assert.equal(script.requests.length, 4); assert.equal(result.tool_results.length, 4); assert(result.cleanup_complete);
  const records = readFileSync(result.envelope.trace_ref!, "utf8").trim().split("\n").map(l => JSON.parse(l));
  assert.deepEqual(records.map(r => r.seq), records.map((_, i) => i + 1));
  assert.equal(records.at(-1).termination, "candidate_ready");
  return info(result);
});
check("P02 actual whitelist and unknown tool cannot execute", async () => {
  const { result } = await execute([{ calls: [call("shell-call", "shell", { command: "never-run" })] }, { text: "stop", inspect: c => assert(toolResult(c, "shell-call").isError) }]);
  assert.equal(result.envelope.counts.tool_executions, 0); assert.equal(result.envelope.termination, "tool_error"); return info(result);
});
check("P03 cross-user and hidden-label sources inaccessible", async () => {
  const hidden = "SYNTHETIC-HIDDEN-LABEL-CANARY";
  writeFileSync(join(directory, "hidden-labels.json"), JSON.stringify({ label: hidden }), { flag: "wx" });
  const denied = await execute([], input({ session_id: "session-b" })); assert.equal(denied.script.requests.length, 0);
  const { result, script } = await execute([{ calls: [
    call("private", "read_evidence", { source_id: "private-user-b", version: "v1", start_line: 1, end_line: 1 }),
    call("label", "read_evidence", { source_id: "hidden-labels", version: "v1", start_line: 1, end_line: 1 }),
  ] }, { text: "stop", inspect(c) { assert(toolResult(c, "private").isError); assert(toolResult(c, "label").isError); } }]);
  const requests = JSON.stringify(script.requests); assert(!requests.includes(hidden)); assert(!requests.includes("PRIVATE-B-CANARY")); return info(result);
});
check("P04 bad ranges, references and duplicate call IDs rejected", async () => {
  const bad = candidate(); bad.citations[0].quote = "forged quote";
  const chain = steps(input(), bad); chain.push({ text: "stop", inspect: c => assert(toolResult(c, "submit-call").isError) });
  const forged = await execute(chain); assert.equal(forged.result.envelope.termination, "invalid_output");
  const range = await execute([{ calls: [call("range", "read_evidence", { source_id: "kb-tool-safety", version: "v1", start_line: 2, end_line: 99 })] }, { text: "stop", inspect: c => assert(toolResult(c, "range").isError) }]);
  const duplicate = await execute([{ calls: [q("same"), q("same")] }]);
  assert.equal(duplicate.result.envelope.termination, "invalid_output"); assert.equal(duplicate.result.envelope.counts.tool_executions, 0);
  return { forged: info(forged.result), range: info(range.result), duplicate: info(duplicate.result) };
});
check("P05 missing answer stops without synthetic user reply", async () => {
  const output = [];
  for (const tool of [call("read", "read_current_answer"), call("submit", "submit_assessment", candidate())]) {
    const { result, script } = await execute([{ calls: [tool] }], input({ session_id: "session-empty" }));
    assert.equal(result.envelope.termination, "awaiting_input"); assert.equal(script.requests.length, 1); assert.equal(result.envelope.candidate, null); output.push(info(result));
  }
  return output;
});
check("P06 SDK text completion cannot submit a business assessment", async () => {
  const { result } = await execute([{ text: "Assessment complete" }]); assert.equal(result.envelope.termination, "invalid_output"); assert.equal(result.envelope.candidate, null); return info(result);
});
check("P07 thrown/rejected/partial-stream errors become protocol provider_error", async () => {
  const partial: StreamFn = () => {
    const s = createAssistantMessageEventStream(), m = message(); m.content = [{ type: "text", text: "partial" }];
    s.push({ type: "start", partial: m }); s.push({ type: "text_delta", contentIndex: 0, delta: "partial", partial: m });
    s.push({ type: "error", reason: "error", error: { ...m, errorMessage: "SYNTHETIC-KEY-CANARY" } }); s.end(); return s;
  };
  const sources: StreamFn[] = [() => { throw Error("SYNTHETIC-KEY-CANARY"); }, async () => { throw Error("SYNTHETIC-KEY-CANARY"); }, partial, createScriptedStream([{ error: true }], TOOL_NAMES).streamFn];
  const output = [];
  for (const source of sources) {
    const r = await runAssessment(input(), new ScopedFixtureStore(fixture), source);
    assert.equal(r.envelope.termination, "provider_error"); assert.equal(r.envelope.candidate, null); assert(r.cleanup_complete);
    assert(!JSON.stringify(r.envelope).includes("SYNTHETIC-KEY-CANARY")); output.push(info(r));
  }
  return output;
});
check("P08 failed tool observation is returned then corrected, history retained", async () => {
  const trace = new MemoryTraceSink();
  const scriptSteps = steps();
  scriptSteps.unshift({ calls: [call("bad-source", "read_evidence", { source_id: "missing", version: "v1", start_line: 1, end_line: 1 })] });
  scriptSteps[1] = { ...scriptSteps[1], inspect: c => assert(toolResult(c, "bad-source").isError) };
  const { result } = await execute(scriptSteps, input(), { traceFactory: () => trace });
  assert.equal(result.envelope.termination, "candidate_ready"); assert.equal(result.envelope.counts.tool_errors, 1);
  assert(trace.records.some(r => r.is_error === true)); return info(result);
});
check("P09 turn/request budgets independent, parent runs share operation debit", async () => {
  const output = [];
  for (const [limits, code] of [[{ provider_requests: 1, tool_calls: 12, turns: 6 }, "request_budget"], [{ provider_requests: 8, tool_calls: 12, turns: 1 }, "turn_budget"]] as const) {
    const { result, script } = await execute([{ calls: [q()] }], input({ budget: limits }));
    assert.equal(result.envelope.termination, "budget_exhausted"); assert.equal(result.envelope.error_code, code);
    assert.equal(script.requests.length, 1); assert.equal(result.envelope.counts.provider_request_attempts, 2); output.push(info(result));
  }
  const i = input({ budget: { provider_requests: 2, tool_calls: 12, turns: 6 } });
  const ledger = new OperationBudget(i);
  const first = await execute([{ text: "stop" }], i, { operationBudget: ledger });
  const second = await execute([{ calls: [q("child-q")] }], { ...i, parent_run_id: first.result.envelope.run_id }, { operationBudget: ledger });
  assert.equal(second.result.envelope.termination, "budget_exhausted"); assert.equal(ledger.snapshot().provider_requests, 2);
  const foreign = await execute([], input({ server_bound_user: "user-b", session_id: "session-b" }), { operationBudget: ledger });
  assert.equal(foreign.result.envelope.error_code, "budget_scope_mismatch"); return { independent: output, shared: info(second.result) };
});
check("P10 over-budget batch executes zero in sequential and parallel modes", async () => {
  const output = [];
  for (const mode of ["sequential", "parallel"] as const) {
    const { result } = await execute([{ calls: [q("batch-a"), q("batch-b"), q("batch-c")] }], input({ budget: { provider_requests: 8, tool_calls: 1 } }), { toolExecution: mode });
    assert.equal(result.envelope.termination, "budget_exhausted"); assert.equal(result.envelope.counts.tool_executions, 0);
    assert.equal(result.envelope.counts.tool_calls_admitted, 0); assert.equal(result.envelope.budget_state?.tool_calls, 0); assert.equal(result.envelope.counts.tool_errors, 3);
    const fit = await execute([{ calls: [q("fit-a"), q("fit-b"), q("fit-c")] }, { text: "stop" }], input({ budget: { provider_requests: 8, tool_calls: 3 } }), { toolExecution: mode });
    assert.equal(fit.result.envelope.counts.tool_executions, 3); assert.equal(fit.result.envelope.budget_state?.tool_calls, 3); output.push(info(result));
    const mixed = await execute([{ calls: [call("unknown-first", "unknown_tool"), q("mixed-a"), q("mixed-b")] }], input({ budget: { provider_requests: 8, tool_calls: 1 } }), { toolExecution: mode });
    assert.equal(mixed.result.envelope.counts.tool_executions, 0); assert.equal(mixed.result.envelope.termination, "budget_exhausted");
  }
  return output;
});
check("P11 cancel/deadline reach provider and tool, non-cooperation quarantined", async () => {
  const output = [];
  for (const timeout of [false, true]) {
    const b = blocker(), control = new AbortController();
    const pending = runAssessment(input({ abort_signal: control.signal, budget: { provider_requests: 8, tool_calls: 12, wall_ms: timeout ? 80 : 10000 } }), new ScopedFixtureStore(fixture), b.stream);
    await b.ready; if (!timeout) control.abort(); const r = await pending;
    assert.equal(r.envelope.termination, timeout ? "timeout" : "cancelled"); assert.deepEqual(b.stats(), { active: 0, aborted: true }); assert(r.cleanup_complete); output.push(info(r));
  }
  const control = new AbortController(); let started!: () => void, released = false;
  const ready = new Promise<void>(resolve => { started = resolve; });
  const tool = execute([{ calls: [q()] }], input({ abort_signal: control.signal }), { beforeToolExecute: async (_name, signal) => {
    await new Promise<void>(resolve => { signal.addEventListener("abort", () => { released = true; resolve(); }, { once: true }); started(); });
  } });
  await ready; control.abort(); const aborted = (await tool).result;
  assert.equal(aborted.envelope.termination, "cancelled"); assert(released); assert(aborted.cleanup_complete); assert.equal(aborted.envelope.counts.tool_executions, 0);
  const bad = new PiRuntimeAdapter(new ScopedFixtureStore(fixture), { cleanupGraceMs: 20 });
  const stuck = await bad.run(input({ budget: { provider_requests: 8, tool_calls: 12, wall_ms: 30 } }), async () => await new Promise<never>(() => {}));
  assert.equal(stuck.envelope.termination, "timeout"); assert(!stuck.cleanup_complete); assert(stuck.envelope.diagnostic_errors.includes("resource_cleanup_failed"));
  const retry = await bad.run(input(), createScriptedStream([], TOOL_NAMES).streamFn); assert.equal(retry.envelope.error_code, "adapter_quarantined");
  const duringOpen = new AbortController(); let lateSinkClosed = false;
  const late = await execute([], input({ abort_signal: duringOpen.signal }), { cleanupGraceMs: 30, traceFactory: async () => {
    duringOpen.abort(); await new Promise(resolve => setTimeout(resolve, 5));
    return { ref: null, async append() {}, async close() { lateSinkClosed = true; } };
  } });
  assert.equal(late.result.envelope.termination, "cancelled"); assert(lateSinkClosed, "a trace sink resolving after abort must still be closed");
  return { provider: output, tool: info(aborted), nonCooperative: info(stuck), lateTraceClosed: lateSinkClosed };
});
check("P12 hook/listener faults cannot bypass hard permissions or candidate gate", async () => {
  const before = await execute([{ calls: [q()] }], input(), { hooks: { beforeToolCall: async () => { throw Error("injected_hook_failure"); } } });
  assert.equal(before.result.envelope.termination, "runtime_error"); assert.equal(before.result.envelope.counts.tool_executions, 0);
  const after = await execute(steps(), input(), { hooks: { afterToolCall: async c => { if (c.toolCall.name === "submit_assessment") throw Error("injected_after_failure"); return undefined; } } });
  assert.equal(after.result.envelope.termination, "runtime_error"); assert.equal(after.result.envelope.candidate, null);
  const observed = await execute(steps(), input(), { observer: e => { if (e.type === "tool_execution_end" && e.toolName === "submit_assessment") throw Error("injected_observer_failure"); } });
  assert.equal(observed.result.envelope.termination, "runtime_error"); assert.equal(observed.result.envelope.candidate, null);
  const tampered = await execute([{ calls: [call("tamper", "read_evidence", { source_id: "kb-tool-safety", version: "v1", start_line: 1, end_line: 1 })] }, { text: "stop", inspect: c => assert(toolResult(c, "tamper").isError) }], input(), { hooks: { beforeToolCall: async c => { (c.args as { source_id: string }).source_id = "private-user-b"; return undefined; } } });
  assert.equal(tampered.result.envelope.candidate, null);
  const sdk = new Agent({ streamFn: createScriptedStream([], []).streamFn });
  sdk.subscribe(e => { if (e.type === "agent_start") throw Error("one_listener_fault"); });
  await sdk.prompt("fixture"); assert.equal(sdk.state.errorMessage, "one_listener_fault"); await sdk.waitForIdle();
  const persistent = new Agent({ streamFn: createScriptedStream([], []).streamFn });
  persistent.subscribe(() => { throw Error("persistent_listener_fault"); });
  await assert.rejects(persistent.prompt("fixture"), /persistent_listener_fault/); await persistent.waitForIdle(); assert(!persistent.state.isStreaming);
  return { before: info(before.result), after: info(after.result), observer: info(observed.result), sdkFaultsSettled: true };
});
check("P13 authenticated checkpoint persisted and reopened with pairing/scope checks", async () => {
  const key = Buffer.alloc(32, 17); // synthetic test key; production must supply a server-managed secret.
  const first = await execute(steps().slice(0, 2), input(), { pauseAfterTurns: 2, checkpointKey: key });
  assert.equal(first.result.envelope.termination, "needs_review"); assert(first.result.checkpoint);
  const path = join(directory, "checkpoint.json"); writeFileSync(path, JSON.stringify(first.result.checkpoint), { flag: "wx" });
  const sealed = JSON.parse(readFileSync(path, "utf8"));
  const script = createScriptedStream(steps().slice(2), TOOL_NAMES);
  const next = await new PiRuntimeAdapter(new ScopedFixtureStore(fixture), { checkpointKey: key }).run(input(), script.streamFn, sealed);
  assert.deepEqual(script.failures, []); assert.equal(next.envelope.termination, "candidate_ready");
  assert.equal(next.envelope.budget_state?.provider_requests, 4); assert.equal(script.requests.length, 2);
  const badSignature = { ...sealed, payload: sealed.payload + " " };
  const rejected = await new PiRuntimeAdapter(new ScopedFixtureStore(fixture), { checkpointKey: key }).run(input(), createScriptedStream([], TOOL_NAMES).streamFn, badSignature);
  assert.equal(rejected.envelope.error_code, "invalid_checkpoint");
  const crossed = await new PiRuntimeAdapter(new ScopedFixtureStore(fixture), { checkpointKey: key }).run(input({ server_bound_user: "user-b", session_id: "session-b" }), createScriptedStream([], TOOL_NAMES).streamFn, sealed);
  assert.equal(crossed.envelope.error_code, "checkpoint_scope_mismatch");
  const data = JSON.parse(sealed.payload); data.messages.pop();
  const payloadText = JSON.stringify(data), signature = createHmac("sha256", key).update(payloadText).digest("hex");
  const unpaired = await new PiRuntimeAdapter(new ScopedFixtureStore(fixture), { checkpointKey: key }).run(input(), createScriptedStream([], TOOL_NAMES).streamFn, { payload: payloadText, signature });
  assert.equal(unpaired.envelope.error_code, "invalid_checkpoint"); return { reopened: info(next), signatureRejected: true, scopeRejected: true, pairingRejected: true };
});
check("P14 adapter rejects reentry; distinct instances and later runs isolated", async () => {
  const b = blocker(), c = new AbortController(), adapter = new PiRuntimeAdapter(new ScopedFixtureStore(fixture));
  const pending = adapter.run(input({ abort_signal: c.signal }), b.stream); await b.ready;
  const busy = await adapter.run(input(), createScriptedStream([], TOOL_NAMES).streamFn); assert.equal(busy.envelope.error_code, "adapter_busy");
  c.abort(); const settled = await pending; assert(settled.cleanup_complete);
  const later = await adapter.run(input({ operation_id: "later-operation" }), createScriptedStream([{ text: "stop" }], TOOL_NAMES).streamFn); assert.equal(later.envelope.termination, "invalid_output");
  const forB = input({ server_bound_user: "user-b", session_id: "session-b", operation_id: "operation-b" });
  const [a, bb] = await Promise.all([execute(steps()), execute(steps(forB), forB)]);
  assert.equal(a.result.envelope.candidate?.answer_id, "answer-a"); assert.equal(bb.result.envelope.candidate?.answer_id, "answer-b");
  assert(!JSON.stringify(a.script.requests).includes("ANSWER-B-CANARY")); return { busyRejected: true, laterReusable: true, a: info(a.result), b: info(bb.result) };
});
check("P15 trace failures visible, primary provider/cancel cause preserved", async () => {
  const failing = (at: string): TraceSink => ({ ref: null, async append(r) { if (at === "all" || r.type === at) throw Error("injected_trace_failure"); }, async close() { if (at === "close") throw Error("injected_close_failure"); } });
  const start = await execute([], input(), { traceFactory: () => failing("all") }); assert.equal(start.result.envelope.termination, "runtime_error");
  const final = await execute(steps(), input(), { traceFactory: () => failing("result") }); assert.equal(final.result.envelope.candidate, null); assert.equal(final.result.envelope.termination, "runtime_error");
  const provider = await execute([{ error: true }], input(), { traceFactory: () => failing("result") }); assert.equal(provider.result.envelope.termination, "provider_error"); assert(provider.result.envelope.diagnostic_errors.includes("trace_write_failed"));
  const closed = await execute(steps(), input(), { traceFactory: () => failing("close") }); assert.equal(closed.result.envelope.termination, "runtime_error"); assert.equal(closed.result.envelope.candidate, null);
  const c = new AbortController(), b = blocker();
  const pending = runAssessment(input({ abort_signal: c.signal }), new ScopedFixtureStore(fixture), b.stream, { traceFactory: () => failing("result") });
  await b.ready; c.abort(); const cancel = await pending;
  assert.equal(cancel.envelope.termination, "cancelled"); assert(cancel.envelope.diagnostic_errors.includes("trace_write_failed"));
  const opening = new AbortController();
  const lateCloseFailure = await execute([], input({ abort_signal: opening.signal }), { traceFactory: async () => {
    opening.abort(); await new Promise(resolve => setTimeout(resolve, 5));
    return { ref: null, async append() {}, async close() { throw Error("late_close_failure"); } };
  } });
  assert.equal(lateCloseFailure.result.envelope.termination, "cancelled");
  assert(lateCloseFailure.result.envelope.diagnostic_errors.includes("trace_close_failed"));
  assert(!lateCloseFailure.result.cleanup_complete);
  return { start: info(start.result), final: info(final.result), provider: info(provider.result), close: info(closed.result), cancel: info(cancel), lateClose: info(lateCloseFailure.result) };
});
check("P16 unknown usage stays null; metadata trace excludes synthetic sensitive markers", async () => {
  const marker = "SYNTHETIC-KEY-AND-THINKING-CANARY";
  const { result } = await execute([{ calls: [q(marker)] }, { thinking: marker, text: marker }], input(), { traceFactory: id => new JsonlTraceSink(directory, id) });
  const text = readFileSync(result.envelope.trace_ref!, "utf8"); assert(!text.includes(marker));
  for (const line of text.trim().split("\n")) {
    const row = JSON.parse(line);
    for (const key of Object.keys(row)) assert(["seq", "run_id", "type", "event", "tool", "call_ref", "is_error", "termination"].includes(key));
  }
  assert.equal(result.envelope.usage.source, "scripted"); assert.equal(result.envelope.usage.input_tokens, null); assert.equal(result.envelope.usage.estimated_cost, null); assert.equal(result.envelope.http_audit.attempts, null);
  return info(result);
});
