import { Type, type Static, type TSchema } from "typebox";

const obj = <T extends Record<string, TSchema>>(p: T) => Type.Object(p, { additionalProperties: false });
const str = () => Type.String({ minLength: 1 });
const id = () => Type.String({ pattern: "^[a-z0-9-]{1,80}$" });
const list = <T extends TSchema>(s: T) => Type.Array(s, { minItems: 1 });
const ids = () => Type.Array(id(), { uniqueItems: true, minItems: 1 });
const choice = <const T extends string[]>(...values: T) => Type.Enum(values);
const nullableId = () => Type.Union([id(), Type.Null()]);
const split = () => choice("development", "validation", "holdout");
const lines = () => Type.Array(Type.String({ minLength: 1, maxLength: 2000 }), { minItems: 1, maxItems: 40 });
const positive = () => Type.Integer({ minimum: 1 });
export const ReviewSchema = obj({
  status: choice("source_checked_draft", "human_reviewed"),
  human_reviewer: Type.Union([str(), Type.Null()]), reviewed_at: Type.Union([str(), Type.Null()]),
  eligible_for_semantic_scoring: Type.Boolean(),
});
export const SourcesSchema = list(obj({
  id: id(), title: str(), url: Type.String({ pattern: "^https://" }), version: id(),
  locator: str(), checked_at: str(), kind: choice("official_documentation", "author_paper"),
  summary_kind: Type.Literal("authored_paraphrase"), summary_lines: lines(),
}));
export const QuestionsSchema = list(obj({
  id: id(), family_id: id(), variant: choice("foundation", "scenario"), split: split(),
  role_id: Type.Literal("agent-intern"), level: Type.Literal("intern"),
  difficulty: choice("foundation", "applied"), prompt: str(),
  rubric: list(obj({ id: id(), criterion: str(), source_ids: ids() })),
  acceptable_answers: list(str()), misconceptions: list(str()), followups: list(str()),
  source_ids: ids(), content_version: id(), origin: Type.Literal("authored_synthetic"), review: ReviewSchema,
}));
export const RolePackageSchema = obj({
  id: Type.Literal("agent-intern"), version: id(), title: str(), level: Type.Literal("intern"),
  status: choice("candidate", "released"), review: ReviewSchema, supported_topics: ids(), unsupported_topics: ids(),
  default_plan: obj({ minutes: positive(), main_questions: positive(), max_followups_per_question: positive(),
    role_only_mix: obj({ foundation: positive(), scenario: positive(), code_discussion: positive() }) }),
  capabilities: list(obj({ id: id(), name: str(), question_family_ids: ids() })), source_ids: ids(),
  provenance: Type.Literal("authored_from_primary_sources"), origin: Type.Literal("authored_synthetic"),
});
export const ResumesSchema = list(obj({
  id: id(), version: id(), candidate_id: id(), family_id: id(), split: split(), origin: Type.Literal("synthetic"), lines: lines(),
  claims: list(obj({ id: id(), start_line: positive(), end_line: positive(), status: Type.Literal("self_reported"), clarification_needed: Type.Boolean() })),
  experience_prompts: list(obj({ id: id(), claim_id: id(), text: str() })),
}));
export const JdsSchema = list(obj({
  id: id(), version: id(), family_id: id(), split: split(), origin: Type.Literal("synthetic"), lines: lines(),
  requirements: list(obj({ id: id(), line: positive(), topic_ids: ids(), coverage: choice("supported", "unsupported") })),
}));
export const SeedsSchema = list(obj({
  id: id(), candidate_id: id(), role_id: Type.Literal("agent-intern"), resume_id: nullableId(), jd_id: nullableId(),
  context_mode: choice("role_only", "role_jd", "role_resume", "role_resume_jd"), split: split(), origin: Type.Literal("synthetic"),
  modes: Type.Array(choice("mock", "practice"), { minItems: 1, uniqueItems: true }),
}));
export const AnswersSchema = list(obj({
  id: id(), candidate_id: id(), prompt_ref: obj({ kind: choice("question", "experience"), id: id() }),
  resume_id: nullableId(), jd_id: nullableId(), split: split(), origin: Type.Literal("synthetic"), lines: lines(),
  hints: Type.Array(obj({ text: str(), before_answer: Type.Literal(true) })), independent: Type.Boolean(), version: id(),
}));
export const TrajectoriesSchema = list(obj({
  id: id(), candidate_id: id(), split: split(), origin: Type.Literal("synthetic"),
  purpose: choice("correction_invalidation", "jd_target_change", "new_scenario_retest"),
  events: list(obj({ id: id(), kind: choice("observation", "correction", "retest", "target", "practice"),
    refs: ids(), text: str(), invalidates: Type.Array(id(), { uniqueItems: true }) })),
}));
export const CatalogSchema = obj({
  sources: SourcesSchema, questions: QuestionsSchema, role: RolePackageSchema, resumes: ResumesSchema,
  jds: JdsSchema, seeds: SeedsSchema, answers: AnswersSchema, trajectories: TrajectoriesSchema,
});
export const LabelsSchema = obj({
  version: id(), origin: Type.Literal("authored_synthetic"), review: ReviewSchema,
  visibility: Type.Literal("evaluator_only_not_secret"),
  answers: list(obj({ answer_id: id(), expected_kind: choice("correct", "partial", "incorrect", "acceptable_alternative", "unknown", "hinted", "contribution_clarification", "insufficient_evidence"), rationale: str() })),
});
export type Catalog = Static<typeof CatalogSchema>;
export type Review = Static<typeof ReviewSchema>;
export type Labels = Static<typeof LabelsSchema>;
