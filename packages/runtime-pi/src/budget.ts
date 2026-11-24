import { createHash } from "node:crypto";
import type { RunInput } from "@pi-interview/contracts";
import { ScopeError } from "./scoped-store.ts";

export function inputFingerprint(input: RunInput): string {
  return createHash("sha256").update(JSON.stringify({
    runtime: "pm004-v1", user: input.server_bound_user, session: input.session_id, version: input.session_version,
    operation: input.operation_id, mode: input.mode, context_mode: input.context_mode, role: input.role, target: input.target_role,
    question: input.question_id, tools: [...input.allowed_tools].sort(),
    refs: [...input.context_refs].sort((a,b) => `${a.source_id}:${a.version}`.localeCompare(`${b.source_id}:${b.version}`)),
    model: input.model_profile, budget: { requests: input.budget.provider_requests, tools: input.budget.tool_calls, turns: input.budget.turns ?? 6, wall: input.budget.wall_ms ?? 180000 },
  })).digest("hex");
}
export interface BudgetSnapshot { turns: number; provider_requests: number; tool_calls: number; deadline_epoch_ms: number }
export class OperationBudget {
  #fingerprint: string;
  #limits: { turns: number; provider_requests: number; tool_calls: number };
  #used: BudgetSnapshot;
  constructor(input: RunInput) {
    this.#fingerprint = inputFingerprint(input);
    this.#limits = { turns: input.budget.turns ?? 6, provider_requests: input.budget.provider_requests, tool_calls: input.budget.tool_calls };
    this.#used = { turns: 0, provider_requests: 0, tool_calls: 0, deadline_epoch_ms: Date.now() + (input.budget.wall_ms ?? 180000) };
  }
  assertOwner(input: RunInput) { if (inputFingerprint(input) !== this.#fingerprint) throw new ScopeError("budget_scope_mismatch"); }
  remainingMs() { return Math.max(0, this.#used.deadline_epoch_ms - Date.now()); }
  request(): string | null {
    if (this.remainingMs() <= 0) return "wall_deadline";
    if (this.#used.turns >= this.#limits.turns) return "turn_budget";
    if (this.#used.provider_requests >= this.#limits.provider_requests) return "request_budget";
    this.#used.turns++; this.#used.provider_requests++;
    return null;
  }
  // Synchronous reservation is atomic within this Node process. No partial batch debit.
  batch(size: number): boolean {
    if (!Number.isSafeInteger(size) || size < 1 || this.#used.tool_calls + size > this.#limits.tool_calls) return false;
    this.#used.tool_calls += size;
    return true;
  }
  snapshot(): BudgetSnapshot { return { ...this.#used }; }
  restore(snapshot: BudgetSnapshot) {
    for (const name of ["turns", "provider_requests", "tool_calls"] as const) {
      if (!Number.isSafeInteger(snapshot[name]) || snapshot[name] < 0 || snapshot[name] > this.#limits[name]) throw new ScopeError("invalid_checkpoint");
      this.#used[name] = Math.max(this.#used[name], snapshot[name]);
    }
    if (!Number.isSafeInteger(snapshot.deadline_epoch_ms)) throw new ScopeError("invalid_checkpoint");
    this.#used.deadline_epoch_ms = Math.min(this.#used.deadline_epoch_ms, snapshot.deadline_epoch_ms);
  }
}
