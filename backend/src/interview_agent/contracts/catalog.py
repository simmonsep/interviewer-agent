"""Public candidate catalog: shape validation plus cross-record business rules."""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from .common import Contract, ContextMode, Id, Ids, Lines, Mode, PositiveInt, Split, Text, TrueFlag, unique


class Review(Contract):
    status: Literal["source_checked_draft", "human_reviewed"]
    human_reviewer: Text | None
    reviewed_at: Text | None
    eligible_for_semantic_scoring: bool


class Source(Contract):
    id: Id
    title: Text
    url: Annotated[str, Field(pattern="^https://")]
    version: Id
    locator: Text
    checked_at: Text
    kind: Literal["official_documentation", "author_paper"]
    summary_kind: Literal["authored_paraphrase"]
    summary_lines: Lines


class RubricItem(Contract):
    id: Id
    criterion: Text
    source_ids: Ids


class Question(Contract):
    id: Id
    family_id: Id
    variant: Literal["foundation", "scenario"]
    split: Split
    role_id: Literal["agent-intern"]
    level: Literal["intern"]
    difficulty: Literal["foundation", "applied"]
    prompt: Text
    rubric: Annotated[list[RubricItem], Field(min_length=1)]
    acceptable_answers: Annotated[list[Text], Field(min_length=1)]
    misconceptions: Annotated[list[Text], Field(min_length=1)]
    followups: Annotated[list[Text], Field(min_length=1)]
    source_ids: Ids
    content_version: Id
    origin: Literal["authored_synthetic"]
    review: Review


class RoleMix(Contract):
    foundation: PositiveInt
    scenario: PositiveInt
    code_discussion: PositiveInt


class DefaultPlan(Contract):
    minutes: PositiveInt
    main_questions: PositiveInt
    max_followups_per_question: PositiveInt
    role_only_mix: RoleMix


class Capability(Contract):
    id: Id
    name: Text
    question_family_ids: Ids


class RolePackage(Contract):
    id: Literal["agent-intern"]
    version: Id
    title: Text
    level: Literal["intern"]
    status: Literal["candidate", "released"]
    review: Review
    supported_topics: Ids
    unsupported_topics: Ids
    default_plan: DefaultPlan
    capabilities: Annotated[list[Capability], Field(min_length=1)]
    source_ids: Ids
    provenance: Literal["authored_from_primary_sources"]
    origin: Literal["authored_synthetic"]


class Claim(Contract):
    id: Id
    start_line: PositiveInt
    end_line: PositiveInt
    status: Literal["self_reported"]
    clarification_needed: bool


class ExperiencePrompt(Contract):
    id: Id
    claim_id: Id
    text: Text


class Resume(Contract):
    id: Id
    version: Id
    candidate_id: Id
    family_id: Id
    split: Split
    origin: Literal["synthetic"]
    lines: Lines
    claims: Annotated[list[Claim], Field(min_length=1)]
    experience_prompts: Annotated[list[ExperiencePrompt], Field(min_length=1)]


class Requirement(Contract):
    id: Id
    line: PositiveInt
    topic_ids: Ids
    coverage: Literal["supported", "unsupported"]


class JobDescription(Contract):
    id: Id
    version: Id
    family_id: Id
    split: Split
    origin: Literal["synthetic"]
    lines: Lines
    requirements: Annotated[list[Requirement], Field(min_length=1)]


class Seed(Contract):
    id: Id
    candidate_id: Id
    role_id: Literal["agent-intern"]
    resume_id: Id | None
    jd_id: Id | None
    context_mode: ContextMode
    split: Split
    origin: Literal["synthetic"]
    modes: Annotated[list[Mode], Field(min_length=1, json_schema_extra={"uniqueItems": True}), AfterValidator(unique)]


class PromptRef(Contract):
    kind: Literal["question", "experience"]
    id: Id


class Hint(Contract):
    text: Text
    before_answer: TrueFlag


class Answer(Contract):
    id: Id
    candidate_id: Id
    prompt_ref: PromptRef
    resume_id: Id | None
    jd_id: Id | None
    split: Split
    origin: Literal["synthetic"]
    lines: Lines
    hints: list[Hint]
    independent: bool
    version: Id


class TrajectoryEvent(Contract):
    id: Id
    kind: Literal["observation", "correction", "retest", "target", "practice"]
    refs: Ids
    text: Text
    invalidates: Annotated[list[Id], Field(json_schema_extra={"uniqueItems": True}), AfterValidator(unique)]


class Trajectory(Contract):
    id: Id
    candidate_id: Id
    split: Split
    origin: Literal["synthetic"]
    purpose: Literal["correction_invalidation", "jd_target_change", "new_scenario_retest"]
    events: Annotated[list[TrajectoryEvent], Field(min_length=1)]


class Catalog(Contract):
    sources: Annotated[list[Source], Field(min_length=1)]
    questions: Annotated[list[Question], Field(min_length=1)]
    role: RolePackage
    resumes: Annotated[list[Resume], Field(min_length=1)]
    jds: Annotated[list[JobDescription], Field(min_length=1)]
    seeds: Annotated[list[Seed], Field(min_length=1)]
    answers: Annotated[list[Answer], Field(min_length=1)]
    trajectories: Annotated[list[Trajectory], Field(min_length=1)]

    @model_validator(mode="after")
    def relationships(self) -> Self:
        from .catalog_rules import validate_relationships

        validate_relationships(self)
        return self
