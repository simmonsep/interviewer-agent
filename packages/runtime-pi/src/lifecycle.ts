import { createAssistantMessageEventStream, type AssistantMessage, type Model, type Api } from "@earendil-works/pi-ai";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import { ScopeError } from "./scoped-store.ts";

export class OwnedTasks {
  #pending = new Set<Promise<void>>();
  track<T>(promise: Promise<T>): Promise<T> {
    const settled = promise.then(() => { this.#pending.delete(settled); }, () => { this.#pending.delete(settled); });
    this.#pending.add(settled);
    return promise;
  }
  async invoke<T>(fn: () => T | Promise<T>, signal: AbortSignal): Promise<T> {
    if (signal.aborted) throw new ScopeError("operation_cancelled");
    const task = this.track(Promise.resolve().then(fn));
    let cancel: () => void = () => {};
    const aborted = new Promise<never>((_, reject) => {
      cancel = () => reject(new ScopeError("operation_cancelled"));
      signal.addEventListener("abort", cancel, { once: true });
    });
    try { return await Promise.race([task, aborted]); }
    finally { signal.removeEventListener("abort", cancel); }
  }
  async settle(graceMs: number): Promise<boolean> {
    if (!this.#pending.size) return true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<false>(resolve => { timer = setTimeout(() => resolve(false), graceMs); });
    try { return await Promise.race([Promise.all([...this.#pending]).then(() => true), timeout]); }
    finally { clearTimeout(timer); }
  }
}
function failure(model: Model<Api>, aborted: boolean): AssistantMessage {
  return { role: "assistant", api: model.api, provider: model.provider, model: model.id, content: [], timestamp: Date.now(),
    stopReason: aborted ? "aborted" : "error", errorMessage: aborted ? "operation_cancelled" : "provider_stream_failure",
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
}
// Close our protocol stream promptly on abort while retaining ownership of the
// upstream task. Unsettled upstream work is reported/quarantined, never called stopped.
export function guardStream(sourceFn: StreamFn, owned: OwnedTasks, lifecycleSignal: AbortSignal, providerError: () => void): StreamFn {
  return (model, context, options) => {
    const output = createAssistantMessageEventStream();
    const internalAbort = new AbortController();
    const signal = AbortSignal.any([lifecycleSignal, internalAbort.signal, ...(options?.signal ? [options.signal] : [])]);
    let closed = false;
    let iterator: AsyncIterator<import("@earendil-works/pi-ai").AssistantMessageEvent> | undefined;
    const fail = (aborted: boolean) => {
      if (closed) return;
      closed = true;
      if (!aborted) providerError();
      output.push({ type: "error", reason: aborted ? "aborted" : "error", error: failure(model, aborted) }); output.end();
    };
    const cancel = () => {
      fail(true);
      if (iterator?.return) void owned.track(Promise.resolve().then(() => iterator!.return!())).catch(() => {});
    };
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) { cancel(); signal.removeEventListener("abort", cancel); return output; }
    const pump = async () => {
      try {
        const source = await sourceFn(model, context, { ...options, signal });
        iterator = source[Symbol.asyncIterator]();
        let events = 0, characters = 0;
        while (true) {
          const next = await iterator.next();
          if (closed) break;
          if (next.done) { fail(false); break; }
          const event = next.value;
          const serializedSize = JSON.stringify(event).length;
          // Pi delta events repeat the full partial message. Charge the new delta,
          // while still bounding each complete event and non-delta message.
          characters += "delta" in event ? event.delta.length : serializedSize;
          if (++events > 2048 || serializedSize > 262144 || characters > 262144) { fail(false); internalAbort.abort(); break; }
          if (event.type === "error") {
            if (event.reason === "error") providerError();
            output.push({ ...event, error: { ...event.error, errorMessage: event.reason === "aborted" ? "operation_cancelled" : "provider_stream_failure", content: [] } });
            closed = true; output.end(); break;
          }
          output.push(event);
          if (event.type === "done") { closed = true; output.end(); break; }
        }
      } catch { fail(signal.aborted); }
      finally { signal.removeEventListener("abort", cancel); }
    };
    void owned.track(pump()).catch(() => fail(false));
    return output;
  };
}
