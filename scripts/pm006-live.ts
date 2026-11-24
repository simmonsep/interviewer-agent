import { readFileSync, writeFileSync, mkdirSync, openSync, closeSync, unlinkSync, existsSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash, randomUUID } from "node:crypto";
import { normalizeContext } from "@earendil-works/pi-ai";
import { loadLiveConfig, loadExplicitKey, SpendLedger, createDeepSeekTransport } from "../packages/runtime-pi/src/deepseek-live.ts";
import { loadCatalog, buildAssessmentFixture } from "../packages/runtime-pi/src/catalog.ts";
import { PiRuntimeAdapter } from "../packages/runtime-pi/src/adapter.ts";
import { ScopedFixtureStore } from "../packages/runtime-pi/src/scoped-store.ts";
import { JsonlTraceSink } from "../packages/runtime-pi/src/trace.ts";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const privateDir = join(root, ".private/pm006");
mkdirSync(privateDir, { recursive: true });
const config = loadLiveConfig(join(root, "configs/pm006-deepseek-flash.json"));
// Read only the explicit file the user configured. No process.env credential fallback.
let key: string;
try { key = loadExplicitKey(join(privateDir, "credentials.env")); }
catch { console.log(JSON.stringify({ status: "WAITING_CREDENTIALS", profile: config.profile, model_calls: 0 })); process.exit(2); }
if (!process.argv.includes("--run")) {
  const ledger = new SpendLedger(join(privateDir, "spend-ledger.json"), config);
  console.log(JSON.stringify({ status: "ready", profile: config.profile, credential_present: true, spend: ledger.snapshot(), max_cny: config.authorization.max_cny, model_calls: 0 })); process.exit(0);
}
let lock: number;
try { lock = openSync(join(privateDir, "run.lock"), "wx"); }
catch { console.log(JSON.stringify({ status: "local_run_locked", model_calls: 0 })); process.exit(2); }
const ledger = new SpendLedger(join(privateDir, "spend-ledger.json"), config);
const output = join(privateDir, "runs", randomUUID());
mkdirSync(output, { recursive: true });
const started = new Date().toISOString();
const reports: Record<string, unknown>[] = [];
const prompt = readFileSync(join(root, "prompts/assessment-baseline-v1.md"), "utf8");
const before = ledger.snapshot();
let passed = true;
const assert = (condition: unknown, code: string) => { if (!condition) throw new Error(code); };
const write = (name: string, data: unknown) => writeFileSync(join(output, name), JSON.stringify(data, null, 2) + "\n");
try {
  const text = createDeepSeekTransport(config, key, ledger);
  let receivedText = "", textDone = false;
  for await (const event of await text.streamFn(text.model, normalizeContext({ messages: [{ role: "user", content: "请仅回复：PM0连接正常。", timestamp: Date.now() }] }), { signal: AbortSignal.timeout(45000) })) {
    if (event.type === "text_delta") receivedText += event.delta;
    if (event.type === "done") textDone = true;
  }
  write("text-smoke.json", { text: receivedText, transport: text.summary() });
  assert(textDone && receivedText.length > 0 && text.summary().usage_complete, "text_or_usage_smoke_failed");
  reports.push({ case: "text", status: "passed", transport: text.summary() });

  const catalog = loadCatalog(root);
  async function assess(answerId: string, missing = false) {
    const { input, bundle } = buildAssessmentFixture(catalog, answerId);
    input.model_profile = "pm006-deepseek-flash";
    input.operation_id = `live-${missing ? "waiting" : answerId}`;
    input.budget = { provider_requests: 6, tool_calls: 12, turns: 6, wall_ms: 180000 };
    if (missing) { bundle.sessions[0]!.answer_id = null; bundle.answers = []; }
    const transport = createDeepSeekTransport(config, key, ledger);
    const index = { question_id: input.question_id, mode: input.mode, context_mode: input.context_mode,
      sources: bundle.evidence.map(e => ({ source_id: e.source_id, version: e.version, start_line: 1, end_line: e.lines.length })) };
    const adapter = new PiRuntimeAdapter(new ScopedFixtureStore(bundle), {
      modelProfile: { id: config.profile, model: transport.model }, systemPrompt: `${prompt}\nServer-bound index:\n${JSON.stringify(index)}`,
      traceFactory: async runId => new JsonlTraceSink(output, runId), cleanupGraceMs: 1000,
    });
    const result = await adapter.run(input, transport.streamFn);
    const name = missing ? "missing-answer" : answerId;
    const ok = result.cleanup_complete && result.envelope.termination === (missing ? "awaiting_input" : "candidate_ready") && result.envelope.committed === false && transport.summary().usage_complete;
    const report = { case: name, status: ok ? "passed" : "failed", result, transport: transport.summary() };
    write(`${name}.json`, report); write(`${name}-assistant-messages.json`, transport.rawMessages);
    reports.push(report); passed &&= ok;
    assert(transport.summary().usage_complete, "unknown_usage_stop_batch");
    assert(result.cleanup_complete, "cleanup_incomplete_stop_batch");
  }
  // Development-only inputs. No hidden expected_kind is loaded or supplied.
  for (const answerId of ["answer-01", "answer-04", "answer-07"]) await assess(answerId);
  const inputs = ["prompts/assessment-baseline-v1.md", "configs/pm006-deepseek-flash.json", "package-lock.json", "packages/contracts/src/index.ts", "packages/runtime-pi/src/adapter.ts", "packages/runtime-pi/src/deepseek-live.ts", "packages/runtime-pi/src/catalog.ts", "scripts/pm006-live.ts", ...["sources", "questions", "role-package", "resumes", "jds", "seeds", "answers", "trajectories"].map(n => `fixtures/pm0-05/${n}.json`)];
  write("baseline-freeze.json", { status: passed ? "development_protocol_frozen" : "development_failures_retained", frozenAt: new Date().toISOString(), profile: config.profile,
    requested_model: config.model, documented_version: config.observed_model_version, alias_may_change: true,
    development_cases: ["answer-01", "answer-04", "answer-07"], semantic_accuracy: null, human_reviewed: false,
    history_policy: "same-run full Pi history; no dedicated skill-memory retrieval", max_cny: config.authorization.max_cny,
    sourceSha256: Object.fromEntries(inputs.map(p => [p, createHash("sha256").update(readFileSync(join(root, p))).digest("hex")])) });
  await assess("answer-01", true);

  // Last request: an incomplete stream may have unknown billed usage, so stop after it.
  const cancellation = createDeepSeekTransport(config, key, ledger);
  const control = new AbortController();
  const timer = setTimeout(() => control.abort(), 45000);
  let responseStarted = false, sawAbort = false;
  try {
    for await (const event of await cancellation.streamFn(cancellation.model, normalizeContext({ messages: [{ role: "user", content: "请用中文详细解释一次HTTP请求经过的步骤。", timestamp: Date.now() }] }), { signal: control.signal })) {
      if (event.type === "start") { responseStarted = true; control.abort(); }
      if (event.type === "error" && event.reason === "aborted") sawAbort = true;
    }
  } finally { clearTimeout(timer); }
  const cancelOk = responseStarted && sawAbort;
  passed &&= cancelOk;
  reports.push({ case: "cancel", status: cancelOk ? "passed" : "failed", response_started: responseStarted, aborted: sawAbort, transport: cancellation.summary() });
} catch (error) {
  passed = false;
  // Only local fixed codes; arbitrary SDK errors never go to console/public records.
  const code = error instanceof Error && /^[a-z_]{1,80}$/.test(error.message) ? error.message : "live_batch_failed";
  reports.push({ case: "batch", status: "failed", code });
} finally {
  const receipt = { step: "PM0-06 / PI-06", status: passed ? "passed_protocol_only" : "failed", startedAt: started, completedAt: new Date().toISOString(), profile: config.profile,
    model: config.model, authorization_id: config.authorization.id, max_cny: config.authorization.max_cny, spend_before: before, spend_after: ledger.snapshot(),
    http_attempts_this_run: ledger.snapshot().attempts - before.attempts, reports, semantic_accuracy: null, human_reviewed: false,
    billing_note: "Peak-price estimates and conservative reservations, not an account invoice or provider-side hard cap. Interrupted/unknown requests keep their full reservation.",
    next: passed ? "PM0 live protocol available; production business work and semantic review remain" : "resolve recorded failure before marking PM0-06 passed" };
  write("receipt.json", receipt);
  closeSync(lock!); if (existsSync(join(privateDir, "run.lock"))) unlinkSync(join(privateDir, "run.lock"));
  console.log(JSON.stringify({ status: receipt.status, receipt: join(output, "receipt.json"), http_attempts: receipt.http_attempts_this_run, reserved_cny_total: ledger.snapshot().reserved_cny, max_cny: config.authorization.max_cny }));
  process.exitCode = passed ? 0 : 1;
}
