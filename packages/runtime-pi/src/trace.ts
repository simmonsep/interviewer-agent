import { appendFileSync, closeSync, mkdirSync, openSync } from "node:fs";
import { join, resolve } from "node:path";
import { createHash } from "node:crypto";
import type { AgentEvent } from "@earendil-works/pi-agent-core";
import { TOOL_NAMES, type Termination } from "@pi-interview/contracts";

export interface TraceRecord { seq: number; run_id: string; type: "start" | "event" | "result"; event?: AgentEvent["type"]; tool?: string; call_ref?: string; is_error?: boolean; termination?: Termination }
export interface TraceSink { ref: string | null; append(record: TraceRecord): Promise<void>; close(): Promise<void> }
export class MemoryTraceSink implements TraceSink {
  ref = null;
  records: TraceRecord[] = [];
  async append(record: TraceRecord) { if (this.records.length >= 2048) throw Error("trace_limit"); this.records.push(structuredClone(record)); }
  async close() {}
}
// Explicit server-configured directory only; no model-supplied path, exclusive file creation.
export class JsonlTraceSink implements TraceSink {
  ref: string;
  #fd: number;
  #closed = false;
  constructor(directory: string, runId: string) {
    if (!/^[a-f0-9-]{36}$/.test(runId)) throw Error("invalid_run_id");
    const root = resolve(directory); mkdirSync(root, { recursive: true });
    this.ref = join(root, `${runId}.jsonl`); this.#fd = openSync(this.ref, "wx");
  }
  async append(record: TraceRecord) { if (this.#closed) throw Error("trace_closed"); appendFileSync(this.#fd, JSON.stringify(record) + "\n", "utf8"); }
  async close() { if (!this.#closed) { this.#closed = true; closeSync(this.#fd); } }
}
export function projectEvent(event: AgentEvent, runId: string): Omit<TraceRecord, "seq"> {
  const record: Omit<TraceRecord, "seq"> = { run_id: runId, type: "event", event: event.type };
  if ("toolCallId" in event) {
    record.call_ref = createHash("sha256").update(runId + ":" + event.toolCallId).digest("hex").slice(0, 24);
    record.tool = TOOL_NAMES.includes(event.toolName as typeof TOOL_NAMES[number]) ? event.toolName : "unknown_tool";
  }
  if (event.type === "tool_execution_end") record.is_error = event.isError;
  return record;
}
