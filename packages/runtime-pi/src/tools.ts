import { Type, type Static, type TSchema } from "typebox";
import { Value } from "typebox/value";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import type { JsonValue } from "@earendil-works/pi-ai";
import { AssessmentSchema, type AssessmentCandidate, type RunInput, type RunEnvelope, type ToolName } from "@pi-interview/contracts";
import { ScopedAssessmentStore, ScopeError, type ReadWindow } from "./scoped-store.ts";

export interface Collector {
  candidate: AssessmentCandidate | null;
  waiting: boolean;
  reads: ReadWindow[];
  counts: RunEnvelope["counts"];
}
export interface ToolGuard { execute<T>(callId: string, name: ToolName, signal: AbortSignal | undefined, fn: () => T): Promise<T> }
const id = Type.String({ pattern: "^[a-z0-9-]{1,80}$" });
const questionArgs = Type.Object({ question_id: id }, { additionalProperties: false });
const evidenceArgs = Type.Object({ source_id: id, version: id, start_line: Type.Integer({ minimum: 1 }), end_line: Type.Integer({ minimum: 1 }) }, { additionalProperties: false });
const emptyArgs = Type.Object({}, { additionalProperties: false });

export function createAssessmentTools(input: RunInput, store: ScopedAssessmentStore, state: Collector, guard: ToolGuard): AgentTool[] {
  function tool<T extends TSchema>(name: ToolName, parameters: T, description: string, fn: (args: Static<T>, deliveredReads: ReadWindow[]) => JsonValue): AgentTool<T> {
    return {
      name, label: name, description, parameters,
      async execute(callId, args, signal) {
        try {
          return await guard.execute(callId, name, signal, () => {
            signal?.throwIfAborted();
            // Hard boundary remains in execute, independently of event observers/hooks.
            store.assertSession(input);
            if (!input.allowed_tools.includes(name)) throw new ScopeError("tool_not_allowed");
            state.counts.tool_executions++;
            const deliveredReads: ReadWindow[] = [];
            const payload = fn(args, deliveredReads);
            const serialized = JSON.stringify(payload);
            if (serialized.length > 8000) throw new ScopeError("result_too_large");
            state.reads.push(...deliveredReads);
            return { content: [{ type: "text" as const, text: serialized }], details: payload, structuredContent: payload };
          });
        } catch (error) {
          const code = error instanceof ScopeError ? error.code : signal?.aborted ? "cancelled" : "tool_failure";
          return { content: [{ type: "text", text: JSON.stringify({ status: "error", code }) }], details: { code }, isError: true };
        }
      },
    };
  }
  const tools = [
    tool("read_question", questionArgs, "Read only the current question and public rubric; no hidden evaluation labels.", args => ({ status: "ok", question: store.question(input, args.question_id) })),
    tool("read_evidence", evidenceArgs, "Read an allowed source version, 1-based inclusive range, at most 40 lines/8000 serialized characters. No paths or URLs.", (args, deliveredReads) => {
      const window = store.evidence(input, args.source_id, args.version, args.start_line, args.end_line);
      deliveredReads.push(window);
      return { status: "ok", evidence: window };
    }),
    tool("read_current_answer", emptyArgs, "Read the current server-bound answer, or report awaiting_input. Never generate a user's answer.", (_args, deliveredReads): JsonValue => {
      const answer = store.answer(input);
      const window = store.answerWindow(input);
      if (!answer || !window) {
        state.waiting = true;
        return { status: "awaiting_input", answer: null, origin: "no_answer" };
      }
      deliveredReads.push(window);
      return { status: "ok", answer: { ...window, question_id: answer.question_id, hints_used: answer.hints_used, origin: answer.origin } };
    }),
    tool("submit_assessment", AssessmentSchema, "Collect one assessment candidate with exact citations to already-read evidence and current answer. Does not persist an assessment or skill memory.", args => {
      if (!Value.Check(AssessmentSchema, args)) throw new ScopeError("invalid_candidate");
      const answer = store.answer(input);
      if (!answer) { state.waiting = true; throw new ScopeError("answer_required"); }
      if (state.candidate) throw new ScopeError("candidate_already_collected");
      if (args.question_id !== input.question_id || args.answer_id !== answer.id) throw new ScopeError("invalid_candidate");
      if (!args.citations.some(c => c.kind === "answer") || !args.citations.some(c => c.kind === "evidence")) throw new ScopeError("invalid_citation");
      for (const citation of args.citations) store.verifyCitation(input, citation, state.reads);
      state.candidate = structuredClone(args);
      return { status: "candidate_collected", committed: false };
    }),
  ];
  return tools.filter(t => input.allowed_tools.includes(t.name as ToolName));
}
