import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Value } from "typebox/value";
import { CatalogSchema, type Catalog, type Review } from "../../contracts/src/catalog.ts";
import type { FixtureBundle, RunInput } from "@pi-interview/contracts";

function requireData(ok: unknown, code: string): asserts ok { if (!ok) throw new Error(code); }
function unique(ids: string[]) { requireData(new Set(ids).size === ids.length, "duplicate_id"); }
function find<T extends { id: string }>(items: T[], id: string): T {
  const item = items.find(v => v.id === id); requireData(item, `missing_ref:${id}`); return item;
}
export function inferContext(resume: unknown, jd: unknown): RunInput["context_mode"] {
  return resume ? (jd ? "role_resume_jd" : "role_resume") : (jd ? "role_jd" : "role_only");
}
export function requireSemanticReview(...reviews: Review[]) {
  requireData(reviews.length > 0 && reviews.every(r => r.status === "human_reviewed" && r.human_reviewer && r.reviewed_at && r.eligible_for_semantic_scoring), "semantic_review_required");
}

// Explicit list: evaluator labels are never part of this loader or its returned context.
export function loadCatalog(root: string): Catalog {
  const files = { sources: "sources", questions: "questions", role: "role-package", resumes: "resumes", jds: "jds", seeds: "seeds", answers: "answers", trajectories: "trajectories" };
  const data = Object.fromEntries(Object.entries(files).map(([key, name]) => [key, JSON.parse(readFileSync(join(root, "fixtures/pm0-05", `${name}.json`), "utf8"))]));
  validateCatalog(data); return data;
}

export function validateCatalog(data: unknown): asserts data is Catalog {
  requireData(Value.Check(CatalogSchema, data), "catalog_schema_invalid");
  const d = data as Catalog;
  // IDs used by trajectory refs are globally unique, including nested experience prompts.
  unique([d.role.id, ...[d.sources, d.questions, d.resumes, d.jds, d.seeds, d.answers, d.trajectories].flatMap(items => items.map(x => x.id)), ...d.resumes.flatMap(r => r.experience_prompts.map(x => x.id))]);
  const review = (r: Review) => requireData(r.status === "human_reviewed" ? Boolean(r.human_reviewer && r.reviewed_at) : (!r.human_reviewer && !r.reviewed_at && !r.eligible_for_semantic_scoring), "inconsistent_review");
  review(d.role.review); d.questions.forEach(q => review(q.review));
  if (d.role.status === "released") requireSemanticReview(d.role.review, ...d.questions.map(q => q.review));
  const sources = (ids: string[]) => ids.forEach(id => { find(d.sources, id); requireData(d.role.source_ids.includes(id), "source_outside_role"); });
  sources(d.role.source_ids);
  const groups = new Map<string, string>();
  const bind = (key: string, value: string) => {
    requireData(!groups.has(key) || groups.get(key) === value, `split_leak:${key}`); groups.set(key, value);
  };
  for (const q of d.questions) {
    sources(q.source_ids); unique(q.rubric.map(r => r.id));
    q.rubric.forEach(r => { sources(r.source_ids); requireData(r.source_ids.every(s => q.source_ids.includes(s)), "rubric_source_outside_question"); });
    requireData(d.role.supported_topics.includes(q.family_id), "question_outside_role");
    bind(`question-family:${q.family_id}`, q.split);
  }
  for (const family of d.role.supported_topics) {
    const qs = d.questions.filter(q => q.family_id === family);
    requireData(qs.length === 2 && new Set(qs.map(q => q.variant)).size === 2, `family_coverage:${family}`);
  }
  unique(d.role.capabilities.map(c => c.id));
  requireData(d.role.supported_topics.every(t => d.role.capabilities.some(c => c.question_family_ids.includes(t))) && d.role.unsupported_topics.every(t => !d.role.supported_topics.includes(t)), "role_coverage_invalid");
  d.role.capabilities.forEach(c => c.question_family_ids.forEach(f => requireData(d.questions.some(q => q.family_id === f), "unknown_family")));
  const mix = d.role.default_plan.role_only_mix;
  requireData(mix.foundation + mix.scenario + mix.code_discussion === d.role.default_plan.main_questions, "plan_mix_invalid");
  for (const r of d.resumes) {
    bind(`candidate:${r.candidate_id}`, r.split); bind(`experience-family:${r.family_id}`, r.split);
    unique(r.claims.map(c => c.id));
    r.claims.forEach(c => requireData(c.start_line <= c.end_line && c.end_line <= r.lines.length, "claim_range_invalid"));
    r.experience_prompts.forEach(p => find(r.claims, p.claim_id));
  }
  for (const jd of d.jds) {
    bind(`jd-family:${jd.family_id}`, jd.split); unique(jd.requirements.map(r => r.id));
    jd.requirements.forEach(r => {
      requireData(r.line <= jd.lines.length, "jd_range_invalid");
      const topics = r.coverage === "supported" ? d.role.supported_topics : d.role.unsupported_topics;
      requireData(r.topic_ids.every(t => topics.includes(t)), "jd_coverage_invalid");
    });
  }
  const materials = (s: { candidate_id: string; resume_id: string | null; jd_id: string | null; split: string }) => {
    bind(`candidate:${s.candidate_id}`, s.split);
    if (s.resume_id) {
      const r = find(d.resumes, s.resume_id); requireData(r.candidate_id === s.candidate_id, "resume_owner_mismatch");
      bind(`experience-family:${r.family_id}`, s.split);
    }
    if (s.jd_id) bind(`jd-family:${find(d.jds, s.jd_id).family_id}`, s.split);
  };
  d.seeds.forEach(s => { materials(s); requireData(s.context_mode === inferContext(s.resume_id, s.jd_id), "context_mode_invalid"); });
  for (const a of d.answers) {
    materials(a); requireData(a.independent === (a.hints.length === 0), "hint_independence_invalid");
    if (a.prompt_ref.kind === "question") requireData(find(d.questions, a.prompt_ref.id).split === a.split, "answer_question_split_mismatch");
    else {
      requireData(a.resume_id, "experience_requires_resume");
      find(find(d.resumes, a.resume_id).experience_prompts, a.prompt_ref.id);
    }
  }
  const refs = [...d.questions, ...d.answers, ...d.seeds, ...d.jds, ...d.resumes];
  for (const t of d.trajectories) {
    bind(`candidate:${t.candidate_id}`, t.split); unique(t.events.map(e => e.id));
    const prior = new Set<string>();
    for (const event of t.events) {
      event.refs.forEach(id => {
        const ref = find(refs, id); requireData(ref.split === t.split, "trajectory_split_mismatch");
        if ("candidate_id" in ref) requireData(ref.candidate_id === t.candidate_id, "trajectory_owner_mismatch");
      });
      requireData(event.invalidates.every(id => prior.has(id)), "invalidation_requires_prior_event"); prior.add(event.id);
    }
  }
}

// This is trusted fixture preparation, not an interview planner or client payload.
export function buildSeedContext(d: Catalog, seedId: string, mode: "mock" | "practice") {
  const seed = find(d.seeds, seedId); requireData(seed.modes.some(m => m === mode), "unsupported_mode");
  return structuredClone({ seed, mode, role: d.role,
    resume: seed.resume_id ? find(d.resumes, seed.resume_id) : null,
    jd: seed.jd_id ? find(d.jds, seed.jd_id) : null });
}
export function questionsForSplit(d: Catalog, split: Catalog["questions"][number]["split"]) {
  return structuredClone(d.questions.filter(q => q.split === split));
}

// Converts authored examples to the already supported single-question Store contract.
// Source lines are our versioned paraphrases; they are not raw downloaded documents.
export function buildAssessmentFixture(d: Catalog, answerId: string): { bundle: FixtureBundle; input: RunInput } {
  const a = find(d.answers, answerId);
  const r = a.resume_id ? find(d.resumes, a.resume_id) : null;
  const q = a.prompt_ref.kind === "question" ? find(d.questions, a.prompt_ref.id) : null;
  const p = !q && r ? find(r.experience_prompts, a.prompt_ref.id) : null;
  requireData(q || p, "prompt_missing");
  const evidence: FixtureBundle["evidence"] = q ? q.source_ids.map(id => {
    const s = find(d.sources, id);
    return { source_id: s.id, version: s.version, owner_user_id: null, lines: [...s.summary_lines], origin: "synthetic" };
  }) : [{ source_id: r!.id, version: r!.version, owner_user_id: a.candidate_id, lines: [...r!.lines], origin: "synthetic" }];
  const sources = evidence.map(e => ({ source_id: e.source_id, version: e.version }));
  const sessionId = `session-${a.id}`;
  const bundle: FixtureBundle = { schema_version: "pm003-v1", origin: "synthetic",
    sessions: [{ id: sessionId, user_id: a.candidate_id, version: 1, question_id: a.prompt_ref.id, answer_id: a.id }],
    questions: [{ id: a.prompt_ref.id, target_role: "agent-intern", text: q?.prompt ?? p!.text,
      rubric: q ? q.rubric.map(x => x.criterion) : ["仅确认自述的个人贡献边界；指标无独立证据时仍待核验，不据此确认技术掌握。"], sources }],
    evidence, answers: [{ id: a.id, version: a.version, user_id: a.candidate_id, session_id: sessionId,
      question_id: a.prompt_ref.id, lines: [...a.lines], hints_used: a.hints.length, origin: "synthetic_fixture" }] };
  const input: RunInput = { server_bound_user: a.candidate_id, session_id: sessionId, operation_id: `operation-${a.id}`,
    session_version: 1, parent_run_id: null, mode: "practice", context_mode: inferContext(a.resume_id, a.jd_id),
    role: "assessment", target_role: "agent-intern", question_id: a.prompt_ref.id, context_refs: sources,
    allowed_tools: ["read_question", "read_evidence", "read_current_answer", "submit_assessment"],
    model_profile: "pm003-scripted", budget: { provider_requests: 8, tool_calls: 12 } };
  return { bundle, input };
}
