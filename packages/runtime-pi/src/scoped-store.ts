import { Value } from "typebox/value";
import { FixtureSchema, SqlSnapshotSchema, type Citation, type FixtureBundle, type SqlSnapshot, type RunInput } from "@pi-interview/contracts";

export class ScopeError extends Error {
  code: string;
  constructor(code = "resource_unavailable") { super(code); this.code = code; }
}
export type ReadWindow = { kind: Citation["kind"]; source_id: string; version: string; start_line: number; end_line: number; lines: string[] };

// Bounded immutable read context. SQL ownership must be checked before building a SQL snapshot.
export class ScopedAssessmentStore {
  #data: FixtureBundle | SqlSnapshot;
  constructor(data: unknown) {
    if (!Value.Check(FixtureSchema, data) && !Value.Check(SqlSnapshotSchema, data)) throw new ScopeError("invalid_snapshot");
    this.#data = structuredClone(data) as FixtureBundle | SqlSnapshot;
    for (const values of [this.#data.sessions, this.#data.questions, this.#data.answers]) {
      if (new Set(values.map(x => x.id)).size !== values.length) throw new ScopeError("duplicate_fixture_id");
    }
    if (new Set(this.#data.evidence.map(x => `${x.source_id}:${x.version}`)).size !== this.#data.evidence.length) throw new ScopeError("duplicate_fixture_id");
  }
  assertSession(input: RunInput) {
    const session = this.#data.sessions.find(s => s.id === input.session_id && s.user_id === input.server_bound_user);
    if (!session || session.version !== input.session_version || session.question_id !== input.question_id) throw new ScopeError();
    return structuredClone(session);
  }
  question(input: RunInput, questionId: string) {
    this.assertSession(input);
    if (questionId !== input.question_id) throw new ScopeError();
    const question = this.#data.questions.find(q => q.id === questionId && q.target_role === input.target_role);
    if (!question) throw new ScopeError();
    return structuredClone(question);
  }
  answer(input: RunInput) {
    const session = this.assertSession(input);
    if (session.answer_id === null) return null;
    const answer = this.#data.answers.find(a => a.id === session.answer_id && a.user_id === input.server_bound_user && a.session_id === input.session_id && a.question_id === input.question_id);
    if (!answer) throw new ScopeError();
    return structuredClone(answer);
  }
  evidence(input: RunInput, sourceId: string, version: string, start: number, end: number): ReadWindow {
    this.assertSession(input);
    const question = this.question(input, input.question_id);
    const allowed = (refs: { source_id: string; version: string }[]) => refs.some(r => r.source_id === sourceId && r.version === version);
    const source = this.#data.evidence.find(e => e.source_id === sourceId && e.version === version && (e.owner_user_id === null || e.owner_user_id === input.server_bound_user));
    if (!source || !allowed(input.context_refs) || !allowed(question.sources)) throw new ScopeError();
    return this.window("evidence", sourceId, version, source.lines, start, end);
  }
  answerWindow(input: RunInput): ReadWindow | null {
    const answer = this.answer(input);
    return answer ? this.window("answer", answer.id, answer.version, answer.lines, 1, answer.lines.length) : null;
  }
  private window(kind: Citation["kind"], source_id: string, version: string, lines: string[], start_line: number, end_line: number): ReadWindow {
    if (!Number.isInteger(start_line) || !Number.isInteger(end_line) || start_line < 1 || end_line < start_line || end_line > lines.length || end_line - start_line + 1 > 40) throw new ScopeError("invalid_range");
    const selected = lines.slice(start_line - 1, end_line);
    if (selected.join("\n").length > 8000) throw new ScopeError("result_too_large");
    return { kind, source_id, version, start_line, end_line, lines: selected };
  }
  verifyCitation(input: RunInput, citation: Citation, reads: ReadWindow[]) {
    const current = citation.kind === "evidence"
      ? this.evidence(input, citation.source_id, citation.version, citation.start_line, citation.end_line)
      : this.answerWindow(input);
    if (!current || current.source_id !== citation.source_id || current.version !== citation.version) throw new ScopeError("invalid_citation");
    const priorRead = reads.find(r => r.kind === citation.kind && r.source_id === citation.source_id && r.version === citation.version && r.start_line <= citation.start_line && r.end_line >= citation.end_line);
    if (!priorRead || citation.end_line < citation.start_line || citation.start_line < current.start_line || citation.end_line > current.end_line) throw new ScopeError("invalid_citation");
    const expected = current.lines.slice(citation.start_line - current.start_line, citation.end_line - current.start_line + 1).join("\n");
    if (citation.quote !== expected) throw new ScopeError("invalid_citation");
  }
}

// Keep the historical test-only constructor strict: user submissions cannot enter as fixtures.
export class ScopedFixtureStore extends ScopedAssessmentStore {
  constructor(data: unknown) {
    if (!Value.Check(FixtureSchema, data)) throw new ScopeError("invalid_fixture");
    super(data);
  }
}
