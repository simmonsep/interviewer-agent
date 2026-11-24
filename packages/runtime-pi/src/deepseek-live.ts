import { readFileSync, writeFileSync, renameSync, existsSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { parseEnv } from "node:util";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import { deepseekProvider } from "@earendil-works/pi-ai/providers/deepseek";
import { stream } from "@earendil-works/pi-ai/api/openai-completions";
import { createAssistantMessageEventStream, type Model } from "@earendil-works/pi-ai";

export interface LiveConfig {
  profile: "pm006-deepseek-flash"; provider: "deepseek"; model: "deepseek-flash"; observed_model_version: string; endpoint: string;
  thinking: "disabled"; max_output_tokens: number; max_payload_bytes: number; input_reservation_tokens: number;
  max_http_attempts: number; max_retries: number; timeout_ms: number;
  authorization: { id: string; max_cny: number; basis: string };
  pricing: { currency: "CNY"; input_miss_per_million: number; input_hit_per_million: number; output_per_million: number; basis: string; checked_at: string; url: string };
  official_sources: string[];
}
function check(ok: unknown, code: string): asserts ok { if (!ok) throw new Error(code); }
export function loadLiveConfig(path: string): LiveConfig {
  const c = JSON.parse(readFileSync(path, "utf8")) as LiveConfig;
  check(c.profile === "pm006-deepseek-flash" && c.provider === "deepseek" && c.model === "deepseek-flash" && c.endpoint === "https://api.deepseek.com/chat/completions", "unsupported_live_profile");
  check(c.thinking === "disabled" && c.max_retries === 0 && c.max_output_tokens === 2048 && c.max_payload_bytes === 16384 && c.input_reservation_tokens === 32768 && c.max_http_attempts === 24 && c.timeout_ms === 45000, "invalid_live_limits");
  check(c.authorization.id === "pm006-20261022-cny2" && c.authorization.max_cny === 2 && c.pricing.currency === "CNY" && c.pricing.input_miss_per_million === 2 && c.pricing.input_hit_per_million === 0.04 && c.pricing.output_per_million === 8, "invalid_spend_authorization");
  return c;
}
export function loadExplicitKey(path: string): string {
  const key = parseEnv(readFileSync(path, "utf8").replace(/^\uFEFF/, "")).DEEPSEEK_API_KEY?.trim();
  check(key && !/\s/.test(key) && key.length >= 10, "credentials_required"); return key;
}
export function resolveLiveModel(): Model<"openai-completions"> {
  const m = deepseekProvider().getModels().find(m => m.id === "deepseek-flash");
  check(m?.api === "openai-completions" && m.baseUrl === "https://api.deepseek.com", "model_not_in_pinned_sdk");
  // Current CNY prices are accounted separately; bundled SDK costs are not a CNY bill.
  return { ...structuredClone(m), compat: { ...m.compat, supportsStrictMode: false } };
}
type Spend = { authorization_id: string; attempts: number; reserved_cny: number };
export class SpendLedger {
  #state: Spend;
  #path: string;
  #config: LiveConfig;
  constructor(path: string, config: LiveConfig) {
    this.#path = path; this.#config = config;
    this.#state = existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) as Spend : { authorization_id: config.authorization.id, attempts: 0, reserved_cny: 0 };
    check(this.#state.authorization_id === config.authorization.id && Number.isSafeInteger(this.#state.attempts) && this.#state.attempts >= 0 && this.#state.attempts <= config.max_http_attempts && this.#state.reserved_cny === this.#cost(this.#state.attempts), "invalid_spend_ledger");
  }
  #cost(n: number) { return Math.round(n * (this.#config.input_reservation_tokens * this.#config.pricing.input_miss_per_million + this.#config.max_output_tokens * this.#config.pricing.output_per_million)) / 1e6; }
  reserve() {
    const next = this.#state.attempts + 1;
    check(next <= this.#config.max_http_attempts && this.#cost(next) <= this.#config.authorization.max_cny, "spend_budget_exhausted");
    const state = { authorization_id: this.#state.authorization_id, attempts: next, reserved_cny: this.#cost(next) };
    const temp = `${this.#path}.${randomUUID()}.tmp`;
    writeFileSync(temp, JSON.stringify(state) + "\n", { flag: "wx" }); renameSync(temp, this.#path);
    this.#state = state;
  }
  snapshot() { return { ...this.#state }; }
}
type Usage = { input_tokens: number; output_tokens: number; cache_hit_tokens: number | null; estimated_peak_cost_cny: number };
type Call = { index: number; http_status: number | null; outcome: string; usage: Usage | null; provider_model: string | null; error_category: string | null };
function record(value: unknown): value is Record<string, unknown> { return Boolean(value && typeof value === "object" && !Array.isArray(value)); }
export function readProviderUsage(event: unknown, c: LiveConfig): Usage | null {
  if (!record(event) || !record(event.usage)) return null;
  const u = event.usage;
  if (!Number.isSafeInteger(u.prompt_tokens) || !Number.isSafeInteger(u.completion_tokens) || Number(u.prompt_tokens) < 0 || Number(u.completion_tokens) < 0) return null;
  const input = Number(u.prompt_tokens), output = Number(u.completion_tokens);
  const hit = Number.isSafeInteger(u.prompt_cache_hit_tokens) && Number(u.prompt_cache_hit_tokens) >= 0 && Number(u.prompt_cache_hit_tokens) <= input ? Number(u.prompt_cache_hit_tokens) : null;
  const cost = ((input - (hit ?? 0)) * c.pricing.input_miss_per_million + (hit ?? 0) * c.pricing.input_hit_per_million + output * c.pricing.output_per_million) / 1e6;
  return { input_tokens: input, output_tokens: output, cache_hit_tokens: hit, estimated_peak_cost_cny: cost };
}
// Real Pi/OpenAI-compatible serializer; injected fetch is the only test replacement.
export function createDeepSeekTransport(c: LiveConfig, key: string, ledger: SpendLedger, fetchImpl: typeof fetch = globalThis.fetch) {
  const model = resolveLiveModel();
  const calls: Call[] = [];
  const rawMessages: unknown[] = [];
  let inFlight = false, stopAfterUncertain = false;
  const streamFn: StreamFn = (_model, context, options) => {
    check(!inFlight && !stopAfterUncertain, "live_transport_not_available");
    check(_model.id === model.id && _model.provider === model.provider, "live_model_mismatch");
    inFlight = true;
    const call: Call = { index: calls.length + 1, http_status: null, outcome: "started", usage: null, provider_model: null, error_category: null };
    calls.push(call);
    const output = createAssistantMessageEventStream();
    const guardedFetch: typeof fetch = async (input, init) => {
      const request = new Request(input, init);
      check(request.url === c.endpoint && request.method === "POST", "unapproved_endpoint");
      const body = await request.clone().text();
      check(Buffer.byteLength(body, "utf8") <= c.max_payload_bytes, "live_payload_too_large");
      const parsed = JSON.parse(body) as Record<string, unknown>;
      check(parsed.model === c.model && parsed.max_tokens === c.max_output_tokens && record(parsed.thinking) && parsed.thinking.type === "disabled", "live_payload_limits_mismatch");
      request.signal.throwIfAborted(); ledger.reserve();
      // Charged once before every actual fetch. No release on timeout/unknown effect.
      const response = await fetchImpl(request, { redirect: "error" });
      call.http_status = response.status;
      return response;
    };
    const pump = async () => {
      try {
        const source = stream(model, context, { apiKey: key, signal: options?.signal, maxTokens: c.max_output_tokens,
          maxRetries: 0, timeoutMs: c.timeout_ms, fetch: guardedFetch, cacheRetention: "none",
          onResponse(response) { call.http_status = response.status; },
          onProviderStreamEvent(event) {
            const usage = readProviderUsage(event, c); if (usage) call.usage = usage;
            if (record(event) && typeof event.model === "string") call.provider_model = event.model;
          } });
        for await (const event of source) {
          if (event.type === "done" || event.type === "error") {
            call.outcome = event.type === "done" ? event.reason : event.reason;
            if (event.type === "error") call.error_category = event.reason === "aborted" ? "aborted" : call.http_status ? `http_${call.http_status}` : "network_or_local_dispatch_failure";
            if (event.type === "done") rawMessages.push(structuredClone(event.message));
          }
          output.push(event);
        }
      } finally {
        inFlight = false;
        if (!call.usage || call.outcome === "error" || call.outcome === "aborted" || call.usage.input_tokens > c.input_reservation_tokens || call.usage.output_tokens > c.max_output_tokens) stopAfterUncertain = true;
        output.end();
      }
    };
    void pump().catch(() => { call.outcome = "transport_error"; stopAfterUncertain = true; output.end(); });
    return output;
  };
  return { model, streamFn, calls, rawMessages, summary: () => ({ currency: "CNY", calls: structuredClone(calls),
    http_attempts: ledger.snapshot().attempts,
    estimated_peak_cost_cny: calls.every(x => x.usage) ? calls.reduce((s, x) => s + x.usage!.estimated_peak_cost_cny, 0) : null,
    known_reported_cost_cny: calls.reduce((s, x) => s + (x.usage?.estimated_peak_cost_cny ?? 0), 0),
    usage_complete: calls.every(x => x.usage !== null), reserved: ledger.snapshot() }) };
}
