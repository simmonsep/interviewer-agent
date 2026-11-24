import { Type, type Static } from "typebox";
export type {PracticeQuestion,PracticeFeedback,PracticeSessionView} from "./practice.ts";

const id = () => Type.String({ pattern: "^[a-z0-9-]{1,80}$" });
const text = (maxLength = 8000) => Type.String({ minLength: 1, maxLength });
const object = <T extends Record<string, import("typebox").TSchema>>(properties: T) => Type.Object(properties, { additionalProperties: false });
export const SourceRefSchema = object({ source_id: id(), version: id() });
export const CitationSchema = object({
  kind: Type.Union([Type.Literal("evidence"), Type.Literal("answer")]),
  source_id: id(), version: id(), start_line: Type.Integer({ minimum: 1 }),
  end_line: Type.Integer({ minimum: 1 }), quote: text(),
});
export const AssessmentSchema = object({
  question_id: id(), answer_id: id(),
  verdict: Type.Union([Type.Literal("supported"), Type.Literal("partial"), Type.Literal("insufficient")]),
  summary: text(1200), feedback: text(2400),
  citations: Type.Array(CitationSchema, { minItems: 2, maxItems: 8 }),
});
export type AssessmentCandidate = Static<typeof AssessmentSchema>;
export type Citation = Static<typeof CitationSchema>;
export const TOOL_NAMES = ["read_question", "read_evidence", "read_current_answer", "submit_assessment"] as const;
export type ToolName = typeof TOOL_NAMES[number];

// Internal server-bound input, not an unauthenticated HTTP request body.
export const RunInputSchema = object({
  server_bound_user: id(), session_id: id(), operation_id: id(),
  session_version: Type.Integer({ minimum: 1 }), parent_run_id: Type.Union([id(), Type.Null()]),
  mode: Type.Union([Type.Literal("mock"), Type.Literal("practice")]),
  context_mode: Type.Union([Type.Literal("role_only"), Type.Literal("role_jd"), Type.Literal("role_resume"), Type.Literal("role_resume_jd")]),
  role: Type.Literal("assessment"), target_role: Type.Literal("agent-intern"),
  question_id: id(), context_refs: Type.Array(SourceRefSchema, { maxItems: 16 }),
  allowed_tools: Type.Array(Type.Union([Type.Literal(TOOL_NAMES[0]), Type.Literal(TOOL_NAMES[1]), Type.Literal(TOOL_NAMES[2]), Type.Literal(TOOL_NAMES[3])]), { minItems: 1, maxItems: 4, uniqueItems: true }),
  model_profile: Type.Union([Type.Literal("pm003-scripted"), Type.Literal("pm006-deepseek-flash")]),
  budget: object({ provider_requests: Type.Integer({ minimum: 1, maximum: 8 }), tool_calls: Type.Integer({ minimum: 1, maximum: 12 }), turns: Type.Optional(Type.Integer({ minimum: 1, maximum: 6 })), wall_ms: Type.Optional(Type.Integer({ minimum: 1, maximum: 180000 })) }),
});
export type RunInput = Static<typeof RunInputSchema> & { abort_signal?: AbortSignal };
export type Termination = "candidate_ready" | "awaiting_input" | "needs_review" | "budget_exhausted" | "timeout" | "cancelled" | "provider_error" | "tool_error" | "invalid_output" | "runtime_error";
export interface RunEnvelope {
  operation_id: string;
  run_id: string;
  parent_run_id: string | null;
  session_id: string;
  termination: Termination;
  candidate: AssessmentCandidate | null;
  committed: false;
  error_code: string | null;
  counts: { provider_request_attempts: number; provider_requests_admitted: number; stream_invocations: number; assistant_turns: number; tool_calls_proposed: number; tool_calls_admitted: number; tool_executions: number; tool_errors: number };
  usage: { source: "scripted" | "unavailable"; input_tokens: null; output_tokens: null; estimated_cost: null };
  http_audit: { attempts: null; scope: "not_observed_by_adapter" };
  trace_ref: string | null;
  diagnostic_errors: string[];
  budget_state: { turns: number; provider_requests: number; tool_calls: number; deadline_epoch_ms: number } | null;
}

export const FixtureSchema = object({
  schema_version: Type.Literal("pm003-v1"), origin: Type.Literal("synthetic"),
  sessions: Type.Array(object({ id: id(), user_id: id(), version: Type.Integer({ minimum: 1 }), question_id: id(), answer_id: Type.Union([id(), Type.Null()]) })),
  questions: Type.Array(object({ id: id(), target_role: Type.Literal("agent-intern"), text: text(), rubric: Type.Array(text(1000)), sources: Type.Array(SourceRefSchema) })),
  evidence: Type.Array(object({ source_id: id(), version: id(), owner_user_id: Type.Union([id(), Type.Null()]), lines: Type.Array(text(2000), { minItems: 1, maxItems: 120 }), origin: Type.Literal("synthetic") })),
  answers: Type.Array(object({ id: id(), version: id(), user_id: id(), session_id: id(), question_id: id(), lines: Type.Array(text(2000), { minItems: 1, maxItems: 120 }), hints_used: Type.Integer({ minimum: 0 }), origin: Type.Literal("synthetic_fixture") })),
});
export type FixtureBundle = Static<typeof FixtureSchema>;

// SQL-authorized snapshots retain real user-input provenance; never retag them as fixtures.
export const SqlSnapshotSchema = object({
  ...FixtureSchema.properties,
  schema_version: Type.Literal("sql-snapshot-v1"), origin: Type.Literal("sql_snapshot"),
  evidence: Type.Array(object({ ...FixtureSchema.properties.evidence.items.properties, origin: Type.Literal("authored_paraphrase") })),
  answers: Type.Array(object({ ...FixtureSchema.properties.answers.items.properties,
    lines: Type.Array(Type.String({ maxLength: 2000 }), { minItems: 1, maxItems: 40 }), origin: Type.Literal("user_submitted") })),
});
export type SqlSnapshot = Static<typeof SqlSnapshotSchema>;
