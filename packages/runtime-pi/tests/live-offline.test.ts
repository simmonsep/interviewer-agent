import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { normalizeContext } from "@earendil-works/pi-ai";
import { loadLiveConfig, loadExplicitKey, SpendLedger, createDeepSeekTransport, readProviderUsage } from "../src/deepseek-live.ts";
import { loadCatalog, buildAssessmentFixture } from "../src/catalog.ts";
import { PiRuntimeAdapter } from "../src/adapter.ts";
import { ScopedFixtureStore } from "../src/scoped-store.ts";
const config = loadLiveConfig("configs/pm006-deepseek-flash.json");
const key = "synthetic-local-test-key";
function ledger() { const path = join(mkdtempSync(join(tmpdir(), "pi-pm006-")), "ledger.json"); return { value: new SpendLedger(path, config), path }; }
function sse(delta: unknown, usage = true, stop = "stop") {
  const base = { id: "synthetic-1", object: "chat.completion.chunk", created: 0, model: "deepseek-flash" };
  const chunks = [{ ...base, choices: [{ index: 0, delta, finish_reason: null }] }, { ...base, choices: [{ index: 0, delta: {}, finish_reason: stop }] },
    ...(usage ? [{ ...base, choices: [], usage: { prompt_tokens: 120, completion_tokens: 30, prompt_cache_hit_tokens: 20 } }] : [])];
  return new Response(chunks.map(x => `data: ${JSON.stringify(x)}\n\n`).join("") + "data: [DONE]\n\n", { headers: { "content-type": "text/event-stream" } });
}
const context = normalizeContext({ messages: [{ role: "user", content: "synthetic hello", timestamp: 0 }] });
test("L01 explicit private key only; missing or blank input rejected without env fallback", () => {
  const path = join(mkdtempSync(join(tmpdir(), "pi-key-")), "key.env");
  writeFileSync(path, "DEEPSEEK_API_KEY=\n"); assert.throws(() => loadExplicitKey(path), /credentials_required/);
  writeFileSync(path, `DEEPSEEK_API_KEY=${key}\n`); assert.equal(loadExplicitKey(path), key);
});
test("L02 monetary reservation persists across restarts, keeps failed attempt reservations and refuses attempt 25", () => {
  const l = ledger();
  for (let i = 0; i < 24; i++) l.value.reserve();
  assert.equal(l.value.snapshot().reserved_cny, 1.96608);
  assert.throws(() => new SpendLedger(l.path, config).reserve(), /spend_budget_exhausted/);
});
test("L03 real pinned Pi serializer/parser and Agent complete assessment via mocked HTTP SSE", async () => {
  const { input, bundle } = buildAssessmentFixture(loadCatalog(process.cwd()), "answer-01");
  input.model_profile = "pm006-deepseek-flash";
  const l = ledger(); let requests = 0;
  const fetcher: typeof fetch = async (request) => {
    const r = new Request(request); const body = JSON.parse(await r.text());
    assert.equal(r.url, config.endpoint); assert.equal(r.headers.get("authorization"), `Bearer ${key}`);
    assert.deepEqual(body.thinking, { type: "disabled" }); assert.equal(body.max_tokens, 2048);
    assert.ok(body.tools.every((t: {function: {strict?: boolean}}) => t.function.strict === undefined));
    assert.ok(!JSON.stringify(body).includes("expected_kind"));
    const call = (index: number, name: string, args: unknown) => ({ index, id: `call-${requests}-${index}`, type: "function", function: { name, arguments: JSON.stringify(args) } });
    let calls;
    if (requests === 0) calls = [call(0, "read_question", { question_id: input.question_id }), call(1, "read_current_answer", {})];
    else if (requests === 1) {
      assert.equal(body.messages.filter((m: {role:string}) => m.role === "tool").length, 2);
      calls = bundle.evidence.map((e, i) => call(i, "read_evidence", { source_id:e.source_id, version:e.version, start_line:1, end_line:e.lines.length }));
    } else calls = [call(0, "submit_assessment", { question_id:input.question_id, answer_id:"answer-01", verdict:"supported", summary:"合成检查", feedback:"合成反馈",
      citations:[{kind:"answer", source_id:"answer-01", version:"v1", start_line:1, end_line:1, quote:bundle.answers[0]!.lines[0]}, {kind:"evidence", source_id:bundle.evidence[0]!.source_id, version:bundle.evidence[0]!.version, start_line:1, end_line:1, quote:bundle.evidence[0]!.lines[0]}] })];
    requests++; return sse({ role:"assistant", tool_calls:calls }, true, "tool_calls");
  };
  const transport = createDeepSeekTransport(config, key, l.value, fetcher);
  const result = await new PiRuntimeAdapter(new ScopedFixtureStore(bundle), { modelProfile:{id:config.profile, model:transport.model} }).run(input, transport.streamFn);
  assert.equal(result.envelope.termination, "candidate_ready"); assert.equal(result.envelope.committed, false);
  assert.equal(result.cleanup_complete, true); assert.equal(requests, 3);
  assert.equal(transport.summary().usage_complete, true); assert.equal(transport.calls[0]!.usage!.input_tokens, 120);
  assert.equal(transport.calls[0]!.usage!.cache_hit_tokens, 20);
});
test("L04 429 is not retried and its cost reservation is retained", async () => {
  const l = ledger(); let attempts = 0;
  const transport = createDeepSeekTransport(config, key, l.value, async () => { attempts++; return new Response(JSON.stringify({ error:{message:"synthetic rate limit",type:"rate_limit"} }), {status:429, headers:{"content-type":"application/json"}}); });
  for await (const _ of await transport.streamFn(transport.model, context)) {}
  assert.equal(attempts, 1); assert.equal(l.value.snapshot().attempts, 1);
  assert.equal(transport.calls[0]!.http_status, 429); assert.equal(transport.summary().estimated_peak_cost_cny, null);
  assert.throws(() => transport.streamFn(transport.model, context), /live_transport_not_available/);
});
test("L05 missing raw usage stays unknown rather than normalized SDK zeros", async () => {
  assert.equal(readProviderUsage({usage:{}}, config), null);
  const t = createDeepSeekTransport(config, key, ledger().value, async () => sse({role:"assistant",content:"ok"}, false));
  for await (const _ of await t.streamFn(t.model, context)) {}
  assert.equal(t.summary().usage_complete, false); assert.equal(t.summary().estimated_peak_cost_cny, null);
});
test("L06 oversized payload is rejected before HTTP and monetary debit", async () => {
  const l = ledger(); let attempts = 0;
  const t = createDeepSeekTransport(config, key, l.value, async () => { attempts++; return sse({content:"unused"}); });
  for await (const _ of await t.streamFn(t.model, normalizeContext({messages:[{role:"user",content:"x".repeat(20000),timestamp:0}]}))) {}
  assert.equal(attempts, 0); assert.equal(l.value.snapshot().attempts, 0);
});
test("L07 model profile mismatch is rejected before any provider invocation", async () => {
  const {input,bundle} = buildAssessmentFixture(loadCatalog(process.cwd()), "answer-01"); input.model_profile = "pm006-deepseek-flash";
  let invoked = 0;
  const t = createDeepSeekTransport(config, key, ledger().value, async () => {invoked++;return sse({content:"unused"});});
  const result = await new PiRuntimeAdapter(new ScopedFixtureStore(bundle)).run(input,t.streamFn);
  assert.equal(result.envelope.error_code,"model_profile_mismatch");assert.equal(invoked,0);
});
test("L08 cancellation reaches injected HTTP and prevents further requests", async () => {
  const l = ledger(), control = new AbortController(); let reached = false;
  const t = createDeepSeekTransport(config, key, l.value, async request => {
    const r = new Request(request);
    return await new Promise<Response>((_resolve,reject) => {r.signal.addEventListener("abort", () => { reached = true; reject(new DOMException("synthetic cancel","AbortError")); }, {once:true}); setTimeout(()=>control.abort(),5);});
  });
  let aborted = false;
  for await (const event of await t.streamFn(t.model,context,{signal:control.signal})) if(event.type==="error"&&event.reason==="aborted") aborted=true;
  assert.ok(reached && aborted); assert.equal(l.value.snapshot().attempts,1);
});
test("L09 repeated SDK partial snapshots do not consume the logical output budget quadratically", async () => {
  const {input,bundle} = buildAssessmentFixture(loadCatalog(process.cwd()), "answer-01"); input.model_profile="pm006-deepseek-flash";
  const t = createDeepSeekTransport(config,key,ledger().value,async () => {
    const base={id:"long-synthetic",object:"chat.completion.chunk",created:0,model:"deepseek-flash"};
    const chunks=Array.from({length:400},()=>({...base,choices:[{index:0,delta:{content:"abcdefgh"},finish_reason:null}]}));
    return new Response(chunks.map(x=>`data: ${JSON.stringify(x)}\n\n`).join("")+`data: ${JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:"stop"}],usage:{prompt_tokens:100,completion_tokens:800}})}\n\ndata: [DONE]\n\n`,{headers:{"content-type":"text/event-stream"}});
  });
  const result=await new PiRuntimeAdapter(new ScopedFixtureStore(bundle),{modelProfile:{id:config.profile,model:t.model}}).run(input,t.streamFn);
  assert.equal(result.envelope.termination,"invalid_output", "plain text must reach business validation, not a false stream limit");
  assert.equal(t.summary().usage_complete,true);
});
