import { createHmac, timingSafeEqual } from "node:crypto";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { RunInput } from "@pi-interview/contracts";
import { inputFingerprint, type BudgetSnapshot } from "./budget.ts";
import type { ReadWindow } from "./scoped-store.ts";
import { ScopeError } from "./scoped-store.ts";

export interface SealedCheckpoint { payload: string; signature: string }
interface Checkpoint { version: "pm004-v1"; fingerprint: string; messages: AgentMessage[]; reads: ReadWindow[]; budget: BudgetSnapshot }
function keyCheck(key: Uint8Array) { if (key.byteLength < 32) throw new ScopeError("checkpoint_key_required"); }
function pairing(messages: AgentMessage[], input: RunInput) {
  const used = new Set<string>(), pending = new Map<string, string>();
  for (const m of messages) {
    if (!m || !["user", "assistant", "toolResult"].includes(m.role)) throw new ScopeError("invalid_checkpoint");
    if (m.role === "assistant") {
      if (pending.size || !Array.isArray(m.content)) throw new ScopeError("invalid_checkpoint");
      for (const c of m.content) {
        if (c.type !== "text" && c.type !== "toolCall") throw new ScopeError("invalid_checkpoint");
        if (c.type === "toolCall") {
          if (typeof c.id !== "string" || used.has(c.id) || !input.allowed_tools.includes(c.name as RunInput["allowed_tools"][number])) throw new ScopeError("invalid_checkpoint");
          used.add(c.id); pending.set(c.id, c.name);
        }
      }
    } else if (m.role === "toolResult") {
      if (pending.get(m.toolCallId) !== m.toolName) throw new ScopeError("invalid_checkpoint");
      pending.delete(m.toolCallId);
    } else if (pending.size) throw new ScopeError("invalid_checkpoint");
  }
  if (pending.size || messages.at(-1)?.role !== "toolResult") throw new ScopeError("invalid_checkpoint");
}
export function sealCheckpoint(input: RunInput, messages: AgentMessage[], reads: ReadWindow[], budget: BudgetSnapshot, key: Uint8Array): SealedCheckpoint {
  keyCheck(key);
  // Recreate our fixed system/tool baseline on restore; never persist thinking/credentials.
  const saved = messages.filter(m => m.role !== "system").map(m => {
    if (m.role === "assistant") return { role: m.role, content: m.content.filter(c => c.type === "text" || c.type === "toolCall"), api: m.api, provider: m.provider, model: m.model, stopReason: m.stopReason, timestamp: m.timestamp,
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
    if (m.role === "toolResult") return { role: m.role, toolCallId: m.toolCallId, toolName: m.toolName, content: m.content, isError: m.isError, timestamp: m.timestamp };
    return m;
  }) as AgentMessage[];
  pairing(saved, input);
  const payload = JSON.stringify({ version: "pm004-v1", fingerprint: inputFingerprint(input), messages: saved, reads, budget } satisfies Checkpoint);
  if (payload.length > 131072) throw new ScopeError("checkpoint_too_large");
  return { payload, signature: createHmac("sha256", key).update(payload).digest("hex") };
}
export function openCheckpoint(sealed: SealedCheckpoint, input: RunInput, key: Uint8Array): Checkpoint {
  keyCheck(key);
  if (typeof sealed.payload !== "string" || sealed.payload.length > 131072 || !/^[a-f0-9]{64}$/.test(sealed.signature)) throw new ScopeError("invalid_checkpoint");
  const expected = createHmac("sha256", key).update(sealed.payload).digest();
  if (!timingSafeEqual(expected, Buffer.from(sealed.signature, "hex"))) throw new ScopeError("invalid_checkpoint");
  const data = JSON.parse(sealed.payload) as Checkpoint;
  if (data.version !== "pm004-v1" || data.fingerprint !== inputFingerprint(input) || !Array.isArray(data.messages) || !Array.isArray(data.reads) || !data.budget) throw new ScopeError("checkpoint_scope_mismatch");
  pairing(data.messages, input);
  return data;
}
