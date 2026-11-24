import assert from "node:assert/strict";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream, getCurrentTools, type AssistantMessage, type JsonObject, type ToolResultMessage, type TranscriptContext } from "@earendil-works/pi-ai";

export interface ScriptCall { id: string; name: string; arguments: JsonObject }
export interface ScriptStep { calls?: ScriptCall[]; text?: string; thinking?: string; error?: boolean; inspect?: (context: TranscriptContext) => void }
export function toolResult(context: TranscriptContext, callId: string): ToolResultMessage {
  const result = context.messages.findLast(m => m.role === "toolResult" && m.toolCallId === callId);
  assert(result?.role === "toolResult", `Missing actual tool result ${callId}`);
  return result;
}
export function payload(context: TranscriptContext, callId: string): unknown {
  const result = toolResult(context, callId);
  const text = result.content.find(c => c.type === "text");
  assert(text?.type === "text");
  return JSON.parse(text.text);
}

// The only model replacement. Agent, schema checks, tools and events remain real Pi.
export function createScriptedStream(steps: ScriptStep[], expectedTools: readonly string[]) {
  const requests: TranscriptContext[] = [];
  const failures: string[] = [];
  const streamFn: StreamFn = (model, context, options) => {
    const stream = createAssistantMessageEventStream();
    const index = requests.length;
    requests.push(structuredClone(context));
    queueMicrotask(() => {
      const message: AssistantMessage = {
        role: "assistant", api: model.api, provider: "pm003-scripted", model: model.id,
        content: [], timestamp: Date.now(), stopReason: "stop",
        // SDK requires numeric usage. These are synthetic placeholders, not measured tokens/cost.
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      };
      try {
        assert(!options?.apiKey, "No credential may be injected into PM0-03 scripted stream");
        assert.deepEqual(getCurrentTools(context.messages).map(t => t.name).sort(), [...expectedTools].sort());
        const step = steps[index];
        assert(step, "Unexpected provider request after script end");
        if (index > 0 && steps[index - 1].calls?.length) {
          const previous = steps[index - 1].calls!;
          const assistantIndex = context.messages.findLastIndex(m => m.role === "assistant");
          const assistant = context.messages[assistantIndex];
          assert(assistant?.role === "assistant");
          assert.deepEqual(assistant.content.filter(c => c.type === "toolCall").map(c => c.id), previous.map(c => c.id));
          const returned = context.messages.slice(assistantIndex + 1).filter(m => m.role === "toolResult");
          assert.deepEqual(returned.map(m => [m.toolCallId, m.toolName]), previous.map(c => [c.id, c.name]));
        }
        step.inspect?.(context);
        if (options?.signal?.aborted || step.error) {
          message.stopReason = options?.signal?.aborted ? "aborted" : "error";
          message.errorMessage = "scripted_failure";
          stream.push({ type: "error", reason: message.stopReason, error: message });
          return;
        }
        stream.push({ type: "start", partial: structuredClone(message) });
        if (step.thinking) {
          const contentIndex = message.content.length;
          message.content.push({ type: "thinking", thinking: step.thinking });
          stream.push({ type: "thinking_start", contentIndex, partial: structuredClone(message) });
          stream.push({ type: "thinking_delta", contentIndex, delta: step.thinking, partial: structuredClone(message) });
          stream.push({ type: "thinking_end", contentIndex, content: step.thinking, partial: structuredClone(message) });
        }
        for (const call of step.calls ?? []) {
          const contentIndex = message.content.length;
          message.content.push({ type: "toolCall", ...call });
          stream.push({ type: "toolcall_start", contentIndex, partial: structuredClone(message) });
          stream.push({ type: "toolcall_delta", contentIndex, delta: JSON.stringify(call.arguments), partial: structuredClone(message) });
          stream.push({ type: "toolcall_end", contentIndex, toolCall: { type: "toolCall", ...call }, partial: structuredClone(message) });
        }
        if (step.text) {
          const contentIndex = message.content.length;
          message.content.push({ type: "text", text: step.text });
          stream.push({ type: "text_start", contentIndex, partial: structuredClone(message) });
          stream.push({ type: "text_delta", contentIndex, delta: step.text, partial: structuredClone(message) });
          stream.push({ type: "text_end", contentIndex, content: step.text, partial: structuredClone(message) });
        }
        message.stopReason = step.calls?.length ? "toolUse" : "stop";
        stream.push({ type: "done", reason: message.stopReason, message });
      } catch (error) {
        failures.push(error instanceof Error ? error.message : "script_failure");
        message.stopReason = "error";
        message.errorMessage = "script_contract_violation";
        stream.push({ type: "error", reason: "error", error: message });
      } finally { stream.end(); }
    });
    return stream;
  };
  return { streamFn, requests, failures };
}
