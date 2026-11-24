"""Load and project reviewed-status-preserving, synthetic catalog data."""

import json
from pathlib import Path

from interview_agent.contracts.catalog import Catalog, JobDescription, Question, Resume, RolePackage, Seed
from interview_agent.contracts.common import Contract, Mode, Split
from interview_agent.contracts.practice import PracticeQuestion, QuestionSnapshot, QuestionSource
from interview_agent.contracts.runtime import FixtureBundle, RunInput

from interview_agent.contracts.catalog_rules import infer_context

# No globbing or implicit evaluator-label loading.
CATALOG_FILES = {
    "sources": "sources", "questions": "questions", "role": "role-package", "resumes": "resumes",
    "jds": "jds", "seeds": "seeds", "answers": "answers", "trajectories": "trajectories",
}


def load_catalog(root: Path | str) -> Catalog:
    directory = Path(root) / "fixtures" / "pm0-05"
    data = {key: json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
            for key, name in CATALOG_FILES.items()}
    return Catalog.model_validate(data)


def find(items, item_id: str):
    for item in items:
        if item.id == item_id:
            return item
    raise ValueError(f"missing_ref:{item_id}")


class SeedContext(Contract):
    seed: Seed
    mode: Mode
    role: RolePackage
    resume: Resume | None
    jd: JobDescription | None


def build_seed_context(catalog: Catalog, seed_id: str, mode: Mode) -> SeedContext:
    seed = find(catalog.seeds, seed_id)
    if mode not in seed.modes:
        raise ValueError("unsupported_mode")
    context = SeedContext(seed=seed, mode=mode, role=catalog.role,
                          resume=find(catalog.resumes, seed.resume_id) if seed.resume_id is not None else None,
                          jd=find(catalog.jds, seed.jd_id) if seed.jd_id is not None else None)
    return context.model_copy(deep=True)


def questions_for_split(catalog: Catalog, split: Split) -> list[Question]:
    if split not in ("development", "validation", "holdout"):
        raise ValueError("unknown_split")
    return [q.model_copy(deep=True) for q in catalog.questions if q.split == split]


class AssessmentFixture(Contract):
    bundle: FixtureBundle
    input: RunInput


def build_assessment_fixture(catalog: Catalog, answer_id: str) -> AssessmentFixture:
    """Trusted test-fixture preparation, never a public caller authorization path."""
    answer = find(catalog.answers, answer_id)
    resume = find(catalog.resumes, answer.resume_id) if answer.resume_id is not None else None
    question = find(catalog.questions, answer.prompt_ref.id) if answer.prompt_ref.kind == "question" else None
    prompt = find(resume.experience_prompts, answer.prompt_ref.id) if question is None and resume else None
    if question is None and prompt is None:
        raise ValueError("prompt_missing")
    if question:
        evidence = [{"source_id": s.id, "version": s.version, "owner_user_id": None,
                     "lines": list(s.summary_lines), "origin": "synthetic"}
                    for s in (find(catalog.sources, source_id) for source_id in question.source_ids)]
    else:
        evidence = [{"source_id": resume.id, "version": resume.version, "owner_user_id": answer.candidate_id,
                     "lines": list(resume.lines), "origin": "synthetic"}]
    sources = [{"source_id": e["source_id"], "version": e["version"]} for e in evidence]
    session_id = f"session-{answer.id}"
    bundle = FixtureBundle.model_validate({
        "schema_version": "pm003-v1", "origin": "synthetic",
        "sessions": [{"id": session_id, "user_id": answer.candidate_id, "version": 1,
                      "question_id": answer.prompt_ref.id, "answer_id": answer.id}],
        "questions": [{"id": answer.prompt_ref.id, "target_role": "agent-intern",
                       "text": question.prompt if question else prompt.text,
                       "rubric": [r.criterion for r in question.rubric] if question else [
                           "仅确认自述的个人贡献边界；指标无独立证据时仍待核验，不据此确认技术掌握。"], "sources": sources}],
        "evidence": evidence,
        "answers": [{"id": answer.id, "version": answer.version, "user_id": answer.candidate_id,
                     "session_id": session_id, "question_id": answer.prompt_ref.id, "lines": list(answer.lines),
                     "hints_used": len(answer.hints), "origin": "synthetic_fixture"}],
    })
    run_input = RunInput.model_validate({
        "server_bound_user": answer.candidate_id, "session_id": session_id, "operation_id": f"operation-{answer.id}",
        "session_version": 1, "parent_run_id": None, "mode": "practice",
        "context_mode": infer_context(answer.resume_id, answer.jd_id), "role": "assessment", "target_role": "agent-intern",
        "question_id": answer.prompt_ref.id, "context_refs": sources,
        "allowed_tools": ["read_question", "read_evidence", "read_current_answer", "submit_assessment"],
        "model_profile": "pm003-scripted", "budget": {"provider_requests": 8, "tool_calls": 12},
    })
    return AssessmentFixture(bundle=bundle, input=run_input)


def practice_questions(catalog: Catalog) -> list[QuestionSnapshot]:
    result = []
    for question in questions_for_split(catalog, "development"):
        if question.review.status != "source_checked_draft" or question.review.eligible_for_semantic_scoring:
            raise ValueError("practice_catalog_review_changed")
        result.append(QuestionSnapshot(
            id=question.id, text=question.prompt, topic=question.family_id, version=question.content_version,
            review="source_checked_draft", semantic_scoring_eligible=False,
            rubric=[r.criterion for r in question.rubric],
            sources=[QuestionSource(id=s.id, version=s.version, title=s.title, url=s.url, locator=s.locator,
                                    lines=list(s.summary_lines))
                     for s in (find(catalog.sources, source_id) for source_id in question.source_ids)],
        ))
    return result


def public_question(question: QuestionSnapshot) -> PracticeQuestion:
    return PracticeQuestion.model_validate(question.model_dump(exclude={"rubric", "sources"}))
