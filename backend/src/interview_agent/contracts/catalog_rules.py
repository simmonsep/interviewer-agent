"""Catalog consistency is distinct from field shapes and runtime resource authorization."""

from typing import TYPE_CHECKING

from .common import unique

if TYPE_CHECKING:
    from .catalog import Catalog, Review


def require(condition: object, code: str) -> None:
    if not condition:
        raise ValueError(code)


def infer_context(resume_id: str | None, jd_id: str | None) -> str:
    if resume_id is not None:
        return "role_resume_jd" if jd_id is not None else "role_resume"
    return "role_jd" if jd_id is not None else "role_only"


def require_semantic_review(*reviews: "Review") -> None:
    require(reviews and all(
        r.status == "human_reviewed" and r.human_reviewer and r.reviewed_at
        and r.eligible_for_semantic_scoring for r in reviews
    ), "semantic_review_required")


def validate_relationships(d: "Catalog") -> None:
    collections = [d.sources, d.questions, d.resumes, d.jds, d.seeds, d.answers, d.trajectories]
    unique([d.role.id] + [x.id for items in collections for x in items]
           + [p.id for r in d.resumes for p in r.experience_prompts])
    sources = {s.id: s for s in d.sources}
    questions = {q.id: q for q in d.questions}
    resumes = {r.id: r for r in d.resumes}
    jds = {j.id: j for j in d.jds}

    def lookup(items: dict, key: str):
        require(key in items, f"missing_ref:{key}")
        return items[key]

    for review in [d.role.review] + [q.review for q in d.questions]:
        require(bool(review.human_reviewer and review.reviewed_at) if review.status == "human_reviewed"
                else review.human_reviewer is None and review.reviewed_at is None
                and not review.eligible_for_semantic_scoring, "inconsistent_review")
    if d.role.status == "released":
        require_semantic_review(d.role.review, *(q.review for q in d.questions))

    def source_refs(ids: list[str]) -> None:
        for source_id in ids:
            lookup(sources, source_id)
            require(source_id in d.role.source_ids, "source_outside_role")

    source_refs(d.role.source_ids)
    groups: dict[str, str] = {}

    def bind(group: str, split: str) -> None:
        require(group not in groups or groups[group] == split, f"split_leak:{group}")
        groups[group] = split

    for q in d.questions:
        source_refs(q.source_ids)
        unique([r.id for r in q.rubric])
        for rubric in q.rubric:
            source_refs(rubric.source_ids)
            require(set(rubric.source_ids) <= set(q.source_ids), "rubric_source_outside_question")
        require(q.family_id in d.role.supported_topics, "question_outside_role")
        bind(f"question-family:{q.family_id}", q.split)
    for family in d.role.supported_topics:
        qs = [q for q in d.questions if q.family_id == family]
        require(len(qs) == 2 and len({q.variant for q in qs}) == 2, f"family_coverage:{family}")
    unique([c.id for c in d.role.capabilities])
    require(all(any(t in c.question_family_ids for c in d.role.capabilities) for t in d.role.supported_topics)
            and not set(d.role.supported_topics) & set(d.role.unsupported_topics), "role_coverage_invalid")
    for capability in d.role.capabilities:
        for family in capability.question_family_ids:
            require(any(q.family_id == family for q in d.questions), "unknown_family")
    mix = d.role.default_plan.role_only_mix
    require(mix.foundation + mix.scenario + mix.code_discussion == d.role.default_plan.main_questions,
            "plan_mix_invalid")
    for resume in d.resumes:
        bind(f"candidate:{resume.candidate_id}", resume.split)
        bind(f"experience-family:{resume.family_id}", resume.split)
        claims = {c.id: c for c in resume.claims}
        unique([c.id for c in resume.claims])
        for claim in resume.claims:
            require(claim.start_line <= claim.end_line <= len(resume.lines), "claim_range_invalid")
        for prompt in resume.experience_prompts:
            lookup(claims, prompt.claim_id)
    for jd in d.jds:
        bind(f"jd-family:{jd.family_id}", jd.split)
        unique([r.id for r in jd.requirements])
        for requirement in jd.requirements:
            require(requirement.line <= len(jd.lines), "jd_range_invalid")
            topics = d.role.supported_topics if requirement.coverage == "supported" else d.role.unsupported_topics
            require(set(requirement.topic_ids) <= set(topics), "jd_coverage_invalid")

    def materials(item) -> None:
        bind(f"candidate:{item.candidate_id}", item.split)
        if item.resume_id is not None:
            resume = lookup(resumes, item.resume_id)
            require(resume.candidate_id == item.candidate_id, "resume_owner_mismatch")
            bind(f"experience-family:{resume.family_id}", item.split)
        if item.jd_id is not None:
            bind(f"jd-family:{lookup(jds, item.jd_id).family_id}", item.split)

    for seed in d.seeds:
        materials(seed)
        require(seed.context_mode == infer_context(seed.resume_id, seed.jd_id), "context_mode_invalid")
    for answer in d.answers:
        materials(answer)
        require(answer.independent == (len(answer.hints) == 0), "hint_independence_invalid")
        if answer.prompt_ref.kind == "question":
            require(lookup(questions, answer.prompt_ref.id).split == answer.split, "answer_question_split_mismatch")
        else:
            require(answer.resume_id is not None, "experience_requires_resume")
            prompts = {p.id: p for p in lookup(resumes, answer.resume_id).experience_prompts}
            lookup(prompts, answer.prompt_ref.id)
    refs = {item.id: item for items in [d.questions, d.answers, d.seeds, d.jds, d.resumes] for item in items}
    for trajectory in d.trajectories:
        bind(f"candidate:{trajectory.candidate_id}", trajectory.split)
        unique([e.id for e in trajectory.events])
        prior: set[str] = set()
        for event in trajectory.events:
            for ref_id in event.refs:
                ref = lookup(refs, ref_id)
                require(ref.split == trajectory.split, "trajectory_split_mismatch")
                if hasattr(ref, "candidate_id"):
                    require(ref.candidate_id == trajectory.candidate_id, "trajectory_owner_mismatch")
            require(set(event.invalidates) <= prior, "invalidation_requires_prior_event")
            prior.add(event.id)
