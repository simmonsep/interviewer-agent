import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { Value } from "typebox/value";
import { LabelsSchema, type Catalog } from "../../contracts/src/catalog.ts";
import { FixtureSchema, RunInputSchema } from "@pi-interview/contracts";
import { loadCatalog, validateCatalog, buildSeedContext, buildAssessmentFixture, requireSemanticReview, questionsForSplit } from "../src/catalog.ts";
import { ScopedFixtureStore } from "../src/scoped-store.ts";

const data = loadCatalog(process.cwd());
function reject(change: (d: Catalog) => void, expected: RegExp) {
  const d = structuredClone(data); change(d); assert.throws(() => validateCatalog(d), expected);
}
test("D01 counts, source locators and two distinct contexts per family", () => {
  assert.deepEqual([data.sources.length, data.questions.length, data.resumes.length, data.jds.length, data.seeds.length, data.answers.length, data.trajectories.length], [20,24,3,3,6,8,3]);
  assert.equal(data.role.capabilities.length, 12);
  for (const q of data.questions) {
    assert.notEqual(q.prompt, data.questions.find(other => other.family_id === q.family_id && other.id !== q.id)!.prompt);
    assert.ok(q.acceptable_answers.length && q.rubric.length && q.followups.length);
    q.source_ids.forEach(id => { const s = data.sources.find(x => x.id === id)!; assert.ok(s.url.startsWith("https://") && s.version && s.locator); });
  }
});
test("D02 four material modes remain independent of mock/practice; role-only has no invented material", () => {
  assert.equal(new Set(data.seeds.map(s => s.context_mode)).size, 4);
  for (const s of data.seeds) for (const mode of ["mock", "practice"] as const) {
    const ctx = buildSeedContext(data, s.id, mode);
    assert.equal(ctx.mode, mode); assert.equal(ctx.seed.context_mode, s.context_mode);
    if (s.context_mode === "role_only") { assert.equal(ctx.resume, null); assert.equal(ctx.jd, null); }
  }
  const base = buildSeedContext(data, "seed-a-a", "mock");
  assert.equal(base.resume!.id, buildSeedContext(data, "seed-a-b", "mock").resume!.id);
  assert.notEqual(base.jd!.id, buildSeedContext(data, "seed-a-b", "mock").jd!.id);
  assert.equal(base.jd!.id, buildSeedContext(data, "seed-b-a", "mock").jd!.id);
  assert.notEqual(base.resume!.id, buildSeedContext(data, "seed-b-a", "mock").resume!.id);
});
test("D03 candidate role package exposes unsupported JD requirements rather than fabricating coverage", () => {
  const jd = data.jds.find(j => j.id === "jd-c")!;
  assert.ok(jd.requirements.some(r => r.coverage === "supported"));
  assert.ok(jd.requirements.some(r => r.coverage === "unsupported"));
  reject(d => { d.jds[2]!.requirements[0]!.coverage = "supported"; }, /jd_coverage_invalid/);
});
test("D04 all eight examples are consumed by existing scoped Store without evaluator labels", () => {
  for (const a of data.answers) {
    const { bundle, input } = buildAssessmentFixture(data, a.id);
    assert.ok(Value.Check(FixtureSchema, bundle)); assert.ok(Value.Check(RunInputSchema, input));
    const store = new ScopedFixtureStore(bundle);
    assert.equal(store.question(input, input.question_id).id, a.prompt_ref.id);
    assert.deepEqual(store.answerWindow(input)!.lines, a.lines);
    assert.equal(store.answer(input)!.hints_used, a.hints.length);
    for (const e of bundle.evidence) assert.deepEqual(store.evidence(input, e.source_id, e.version, 1, e.lines.length).lines, e.lines);
    assert.ok(!JSON.stringify({ bundle, input }).includes("expected_kind"));
    assert.throws(() => store.answer({ ...input, server_bound_user: "another-user" }), /resource_unavailable/);
  }
  const experience = buildAssessmentFixture(data, "answer-07");
  assert.equal(experience.input.context_mode, "role_resume");
  assert.equal(experience.bundle.evidence[0]!.owner_user_id, "candidate-c");
});
test("D05 labels are evaluator-only synthetic drafts; no semantic accuracy is claimed", () => {
  const labels = JSON.parse(readFileSync("evals/pm0-05/labels.json", "utf8"));
  assert.ok(Value.Check(LabelsSchema, labels));
  assert.equal(labels.answers.length, data.answers.length);
  assert.deepEqual(labels.answers.map((a: {answer_id: string}) => a.answer_id).sort(), data.answers.map(a => a.id).sort());
  assert.equal(new Set(labels.answers.map((a: {expected_kind: string}) => a.expected_kind)).size, 8);
  assert.throws(() => requireSemanticReview(data.role.review, ...data.questions.map(q => q.review), labels.review), /semantic_review_required/);
  assert.ok(!JSON.stringify(data).includes("expected_kind"));
  assert.throws(() => validateCatalog({ ...data, labels }), /catalog_schema_invalid/);
  reject(d => { d.role.status = "released"; }, /semantic_review_required/);
  reject(d => { d.questions[0]!.review.eligible_for_semantic_scoring = true; }, /inconsistent_review/);
});
test("D06 duplicate IDs and missing/misbound sources are rejected", () => {
  reject(d => { d.questions[1]!.id = d.questions[0]!.id; }, /duplicate_id/);
  reject(d => { d.questions[0]!.source_ids.push("missing"); }, /missing_ref/);
  reject(d => { d.questions[0]!.rubric[0]!.source_ids = ["http"]; }, /rubric_source_outside_question/);
});
test("D07 split groups prevent question paraphrase and material identity leakage", () => {
  reject(d => { d.questions[1]!.split = "holdout"; }, /split_leak:question-family/);
  reject(d => { d.seeds[0]!.split = "holdout"; }, /split_leak:candidate/);
  reject(d => { d.resumes[1]!.family_id = d.resumes[0]!.family_id; d.resumes[1]!.split = "holdout"; }, /split_leak:experience-family/);
  reject(d => { d.jds[1]!.family_id = d.jds[0]!.family_id; d.jds[1]!.split = "holdout"; }, /split_leak:jd-family/);
  for (const split of ["development", "validation", "holdout"] as const) {
    const questions = questionsForSplit(data, split); assert.equal(questions.length, 8);
    assert.ok(questions.every(q => q.split === split));
  }
  assert.ok(data.seeds.every(s => s.split === "development"));
});
test("D08 ownership, material mode and claim bounds are checked", () => {
  reject(d => { d.seeds[2]!.resume_id = "resume-b"; }, /resume_owner_mismatch/);
  reject(d => { d.seeds[0]!.context_mode = "role_jd"; }, /context_mode_invalid/);
  reject(d => { d.resumes[0]!.claims[0]!.end_line = 99; }, /claim_range_invalid/);
  reject(d => { d.answers[6]!.resume_id = null; }, /experience_requires_resume/);
});
test("D09 hinted answer cannot be marked independent", () => {
  reject(d => { d.answers[5]!.independent = true; }, /hint_independence_invalid/);
});
test("D10 trajectory references, ownership and invalidation order are checked", () => {
  reject(d => { d.trajectories[0]!.events[0]!.refs = ["missing"]; }, /missing_ref/);
  reject(d => { d.trajectories[0]!.events[0]!.refs = ["answer-07"]; }, /trajectory_owner_mismatch/);
  reject(d => { d.trajectories[0]!.events[0]!.invalidates = ["correction-new"]; }, /invalidation_requires_prior_event/);
});
