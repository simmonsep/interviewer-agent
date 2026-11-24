import assert from "node:assert/strict";
import { after, test } from "node:test";
import { readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { TOOL_NAMES, type AssessmentCandidate, type FixtureBundle, type RunInput } from "@pi-interview/contracts";
import { runAssessment, type RunResult } from "../src/adapter.ts";
import { ScopedFixtureStore } from "../src/scoped-store.ts";
import { createScriptedStream, payload, toolResult, type ScriptCall, type ScriptStep } from "../test-support/scripted-stream.ts";

const fixture: FixtureBundle = JSON.parse(readFileSync(new URL("../../../fixtures/pm0-03/bundle.json", import.meta.url), "utf8"));
const cases: { name: string; status: string; evidence?: unknown; error?: string }[] = [];
after(() => {
  if (process.env.PI_PM003_RECEIPT_PATH) writeFileSync(process.env.PI_PM003_RECEIPT_PATH, JSON.stringify(cases, null, 2));
});
function check(name: string, fn: () => Promise<unknown> | unknown) {
  test(name, { timeout: 3000 }, async () => {
    try { const evidence = await fn(); cases.push({ name, status: "passed", evidence }); }
    catch (error) { cases.push({ name, status: "failed", error: error instanceof Error ? error.message : "test_failure" }); throw error; }
  });
}
function input(patch: Partial<RunInput> = {}): RunInput {
  return {
    server_bound_user: "user-a", session_id: "session-a", operation_id: "operation-a", session_version: 1, parent_run_id: null,
    mode: "mock", context_mode: "role_only", role: "assessment", target_role: "agent-intern", question_id: "q-tool-safety",
    context_refs: [{ source_id: "kb-tool-safety", version: "v1" }], allowed_tools: [...TOOL_NAMES], model_profile: "pm003-scripted",
    budget: { provider_requests: 8, tool_calls: 12 }, ...patch,
  };
}
const call = (id: string, name: string, args: ScriptCall["arguments"] = {}): ScriptCall => ({ id, name, arguments: args });
function candidate(forInput = input()): AssessmentCandidate {
  const session = fixture.sessions.find(s => s.id === forInput.session_id)!;
  const answer = fixture.answers.find(a => a.id === session.answer_id) ?? fixture.answers[0];
  return {
    question_id: forInput.question_id, answer_id: answer.id, verdict: "partial",
    summary: "协议验收使用的合成评价候选，尚未验证评价质量。", feedback: "讨论参数校验与资源授权的区别。",
    citations: [
      { kind: "evidence", source_id: "kb-tool-safety", version: "v1", start_line: 1, end_line: 2, quote: fixture.evidence[0].lines.slice(0, 2).join("\n") },
      { kind: "answer", source_id: answer.id, version: answer.version, start_line: 1, end_line: answer.lines.length, quote: answer.lines.join("\n") },
    ],
  };
}
function successSteps(forInput = input(), assessment = candidate(forInput)): ScriptStep[] {
  const answer = fixture.answers.find(a => a.id === assessment.answer_id)!;
  return [
    { calls: [call("question-call", "read_question", { question_id: forInput.question_id })] },
    { calls: [call("evidence-call", "read_evidence", { source_id: "kb-tool-safety", version: "v1", start_line: 1, end_line: 2 })], inspect(context) {
      assert.deepEqual(payload(context, "question-call"), { status: "ok", question: fixture.questions[0] });
    } },
    { calls: [call("answer-call", "read_current_answer")], inspect(context) {
      assert.deepEqual(payload(context, "evidence-call"), { status: "ok", evidence: { kind: "evidence", source_id: "kb-tool-safety", version: "v1", start_line: 1, end_line: 2, lines: fixture.evidence[0].lines.slice(0, 2) } });
    } },
    { calls: [call("submit-call", "submit_assessment", assessment)], inspect(context) {
      assert.deepEqual(payload(context, "answer-call"), { status: "ok", answer: { kind: "answer", source_id: answer.id, version: answer.version, start_line: 1, end_line: answer.lines.length, lines: answer.lines, question_id: answer.question_id, hints_used: answer.hints_used, origin: "synthetic_fixture" } });
    } },
  ];
}
async function execute(steps: ScriptStep[], forInput = input(), data: unknown = fixture) {
  const script = createScriptedStream(steps, forInput.allowed_tools);
  const result = await runAssessment(forInput, new ScopedFixtureStore(data), script.streamFn);
  assert.deepEqual(script.failures, [], "The scripted provider must verify actual Pi request/result pairing");
  return { script, result };
}
function evidence(result: RunResult) { return { envelope: result.envelope, events: result.events, tool_results: result.tool_results }; }

check("C01 role_only: real Pi question/evidence/answer/submit chain", async () => {
  const { result, script } = await execute(successSteps());
  assert.equal(result.envelope.termination, "candidate_ready");
  assert.deepEqual(result.envelope.candidate, candidate());
  assert.equal(result.envelope.committed, false);
  assert.equal(script.requests.length, 4);
  assert.deepEqual(result.tool_results.map(r => [r.tool_call_id, r.is_error]), [["question-call", false], ["evidence-call", false], ["answer-call", false], ["submit-call", false]]);
  assert.deepEqual(result.envelope.counts, { provider_request_attempts: 4, provider_requests_admitted: 4, stream_invocations: 4, assistant_turns: 4, tool_calls_proposed: 4, tool_calls_admitted: 4, tool_executions: 4, tool_errors: 0 });
  const requests = JSON.stringify(script.requests);
  for (const forbidden of ["PRIVATE-B-CANARY", "ANSWER-B-CANARY", "hidden_gold", "owner_user_id"]) assert(!requests.includes(forbidden));
  assert.equal(result.envelope.http_audit.attempts, null);
  assert.equal(result.envelope.usage.input_tokens, null);
  return { ...evidence(result), actualRequestSha256: createHash("sha256").update(requests).digest("hex") };
});
check("C02 missing answer ends after the real read tool, no invented answer", async () => {
  const { result, script } = await execute([{ calls: [call("missing-answer", "read_current_answer")] }], input({ session_id: "session-empty" }));
  assert.equal(result.envelope.termination, "awaiting_input");
  assert.equal(result.envelope.candidate, null);
  assert.equal(script.requests.length, 1);
  assert.equal(result.envelope.counts.tool_executions, 1);
  assert.equal(result.events.at(-1)?.type, "agent_end");
  return evidence(result);
});
check("C03 cross-user session rejected before any provider request", async () => {
  const { result, script } = await execute([], input({ session_id: "session-b" }));
  assert.equal(result.envelope.termination, "runtime_error");
  assert.equal(result.envelope.error_code, "resource_unavailable");
  assert.equal(script.requests.length, 0);
  return evidence(result);
});
check("C04 unknown shell tool is not registered or executed", async () => {
  const { result } = await execute([
    { calls: [call("unknown-call", "shell", { command: "never-run" })] },
    { text: "stop", inspect: c => assert.equal(toolResult(c, "unknown-call").isError, true) },
  ]);
  assert.equal(result.envelope.termination, "tool_error");
  assert.equal(result.envelope.counts.tool_executions, 0);
  assert.equal(result.envelope.counts.tool_calls_admitted, 0);
  return evidence(result);
});
check("C05 owner check survives mistakenly broad source allowlists", async () => {
  const data = structuredClone(fixture);
  data.questions[0].sources.push({ source_id: "private-user-b", version: "v1" });
  const forInput = input({ context_refs: [...input().context_refs, { source_id: "private-user-b", version: "v1" }] });
  const { result, script } = await execute([
    { calls: [call("private-call", "read_evidence", { source_id: "private-user-b", version: "v1", start_line: 1, end_line: 1 })] },
    { text: "stop", inspect: c => assert.deepEqual(payload(c, "private-call"), { status: "error", code: "resource_unavailable" }) },
  ], forInput, data);
  assert(!JSON.stringify(script.requests).includes("PRIVATE-B-CANARY"));
  assert.equal(result.envelope.candidate, null);
  return evidence(result);
});
check("C06 invalid range/version/source become bounded error observations", async () => {
  const { result } = await execute([
    { calls: [
      call("bad-range", "read_evidence", { source_id: "kb-tool-safety", version: "v1", start_line: 2, end_line: 99 }),
      call("bad-version", "read_evidence", { source_id: "kb-tool-safety", version: "v2", start_line: 1, end_line: 1 }),
      call("bad-source", "read_evidence", { source_id: "unknown-source", version: "v1", start_line: 1, end_line: 1 }),
    ] },
    { text: "stop", inspect(c) {
      assert.deepEqual(payload(c, "bad-range"), { status: "error", code: "invalid_range" });
      for (const id of ["bad-version", "bad-source"]) assert.deepEqual(payload(c, id), { status: "error", code: "resource_unavailable" });
    } },
  ]);
  assert.equal(result.envelope.counts.tool_errors, 3);
  return evidence(result);
});
check("C07 forged quote rejected even after authorized reads", async () => {
  const forged = candidate(); forged.citations[0].quote = "a fabricated quotation";
  const steps = successSteps(input(), forged);
  steps.push({ text: "stop", inspect: c => assert.deepEqual(payload(c, "submit-call"), { status: "error", code: "invalid_citation" }) });
  const { result } = await execute(steps);
  assert.equal(result.envelope.termination, "invalid_output");
  assert.equal(result.envelope.candidate, null);
  return evidence(result);
});
check("C08 valid-looking references must have been actually read", async () => {
  const { result } = await execute([
    { calls: [call("unread-submit", "submit_assessment", candidate())] },
    { text: "stop", inspect: c => assert.deepEqual(payload(c, "unread-submit"), { status: "error", code: "invalid_citation" }) },
  ]);
  assert.equal(result.envelope.termination, "invalid_output");
  assert.equal(result.envelope.candidate, null);
  return evidence(result);
});
check("C09 injected user_id argument rejected by actual Pi schema validation", async () => {
  const { result } = await execute([
    { calls: [call("extra-arg", "read_current_answer", { user_id: "user-b" })] },
    { text: "stop", inspect: c => assert.equal(toolResult(c, "extra-arg").isError, true) },
  ]);
  assert.equal(result.envelope.counts.tool_calls_admitted, 0);
  assert.equal(result.envelope.counts.tool_executions, 0);
  assert.equal(result.envelope.counts.tool_errors, 1);
  return evidence(result);
});
check("C10 missing answer cannot be bypassed by direct candidate submission", async () => {
  const { result, script } = await execute([{ calls: [call("no-answer-submit", "submit_assessment", candidate())] }], input({ session_id: "session-empty" }));
  assert.equal(result.envelope.termination, "awaiting_input");
  assert.equal(result.envelope.candidate, null);
  assert.equal(script.requests.length, 1);
  assert.equal(result.envelope.counts.tool_errors, 1);
  return evidence(result);
});
check("C11 ordinary assistant text is not a submitted assessment", async () => {
  const { result } = await execute([{ text: "The user is excellent. Assessment complete." }]);
  assert.equal(result.envelope.termination, "invalid_output");
  assert.equal(result.envelope.candidate, null);
  return evidence(result);
});
check("C12 separate concurrent Agent instances keep user context isolated", async () => {
  const forB = input({ server_bound_user: "user-b", session_id: "session-b", operation_id: "operation-b", mode: "practice" });
  const [a, b] = await Promise.all([execute(successSteps()), execute(successSteps(forB), forB)]);
  assert.equal(a.result.envelope.candidate?.answer_id, "answer-a");
  assert.equal(b.result.envelope.candidate?.answer_id, "answer-b");
  assert.notEqual(a.result.envelope.run_id, b.result.envelope.run_id);
  assert(!JSON.stringify(a.script.requests).includes("ANSWER-B-CANARY"));
  assert(!JSON.stringify(b.script.requests).includes(fixture.answers[0].lines[0]));
  return { a: evidence(a.result), b: evidence(b.result) };
});
check("C13 stream protocol error is not mistaken for successful SDK completion", async () => {
  const { result } = await execute([{ error: true }]);
  assert.equal(result.envelope.termination, "provider_error");
  assert.equal(result.envelope.candidate, null);
  return evidence(result);
});
check("C14 source fixture is copied and duplicate identities rejected", () => {
  const data = structuredClone(fixture);
  const store = new ScopedFixtureStore(data);
  data.answers[0].lines[0] = "mutated-by-caller";
  assert.equal(store.answer(input())?.lines[0], fixture.answers[0].lines[0]);
  const sessionView = store.assertSession(input());
  sessionView.user_id = "user-b";
  assert.equal(store.assertSession(input()).user_id, "user-a");
  data.sessions.push(data.sessions[0]);
  assert.throws(() => new ScopedFixtureStore(data), /duplicate_fixture_id/);
  return { callerMutationIsolated: true, duplicateRejected: true };
});
check("C15 actual tool declarations follow the server subset", async () => {
  const { result } = await execute([
    { calls: [call("only-question", "read_question", { question_id: "q-tool-safety" })] },
    { text: "stop", inspect: c => assert.equal(toolResult(c, "only-question").isError, false) },
  ], input({ allowed_tools: ["read_question"] }));
  assert.equal(result.envelope.candidate, null);
  assert.equal(result.envelope.counts.tool_executions, 1);
  return evidence(result);
});
check("C16 oversized observations are rejected and do not authorize citations", async () => {
  const data = structuredClone(fixture);
  // Raw text is below 8000 but JSON metadata puts the observation over its bound.
  data.evidence[0].lines = Array.from({ length: 4 }, () => "x".repeat(1980));
  assert(data.evidence[0].lines.join("\n").length < 8000);
  assert(JSON.stringify({ status: "ok", evidence: { kind: "evidence", source_id: "kb-tool-safety", version: "v1", start_line: 1, end_line: 4, lines: data.evidence[0].lines } }).length > 8000);
  const forged = candidate();
  forged.citations[0] = { kind: "evidence", source_id: "kb-tool-safety", version: "v1", start_line: 1, end_line: 4, quote: data.evidence[0].lines.join("\n") };
  const { result } = await execute([
    { calls: [call("large-read", "read_evidence", { source_id: "kb-tool-safety", version: "v1", start_line: 1, end_line: 4 })] },
    { calls: [call("small-answer", "read_current_answer")], inspect: c => assert.deepEqual(payload(c, "large-read"), { status: "error", code: "result_too_large" }) },
    { calls: [call("large-submit", "submit_assessment", forged)] },
    { text: "stop", inspect: c => assert.deepEqual(payload(c, "large-submit"), { status: "error", code: "invalid_citation" }) },
  ], input(), data);
  assert.equal(result.envelope.candidate, null);
  return evidence(result);
});
