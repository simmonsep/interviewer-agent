import json

import pytest
from pydantic import ValidationError

from interview_agent.contracts.catalog import Catalog
from interview_agent.contracts.catalog_rules import require_semantic_review
from interview_agent.data.catalog import (
    CATALOG_FILES, build_assessment_fixture, build_seed_context, load_catalog,
    practice_questions, public_question, questions_for_split,
)


def test_public_catalog_roundtrip_and_counts(catalog, catalog_data):
    assert catalog.model_dump(mode="json") == catalog_data
    assert [len(getattr(catalog, key)) for key in ["sources", "questions", "resumes", "jds", "seeds", "answers", "trajectories"]] == [20, 24, 3, 3, 6, 8, 3]
    assert catalog.role.status == "candidate"
    assert catalog.role.review.eligible_for_semantic_scoring is False
    assert all(q.review.status == "source_checked_draft" for q in catalog.questions)
    for split in ("development", "validation", "holdout"):
        selected = questions_for_split(catalog, split)
        assert len(selected) == 8 and all(q.split == split for q in selected)
    with pytest.raises(ValueError, match="unknown_split"):
        questions_for_split(catalog, "typo")


@pytest.mark.parametrize("answer_id", [f"answer-{i:02}" for i in range(1, 9)])
def test_eight_python_assessment_fixtures_match_executed_ts(catalog, ts_reference, answer_id):
    result = build_assessment_fixture(catalog, answer_id)
    assert result.model_dump(mode="json", exclude_unset=True) == ts_reference["assessment_fixtures"][answer_id]
    assert "expected_kind" not in result.model_dump_json()


def test_four_context_modes_and_both_training_modes_match_ts(catalog, ts_reference):
    contexts = []
    for seed in catalog.seeds:
        for mode in ("mock", "practice"):
            context = build_seed_context(catalog, seed.id, mode)
            contexts.append({"seed_id": seed.id, "mode": mode, "context_mode": context.seed.context_mode,
                             "resume_id": context.resume.id if context.resume else None,
                             "jd_id": context.jd.id if context.jd else None})
            if seed.context_mode == "role_only":
                assert context.resume is None and context.jd is None
    assert contexts == ts_reference["seed_contexts"]
    assert len({item["context_mode"] for item in contexts}) == 4
    context = build_seed_context(catalog, "seed-resume-only", "practice")
    context.resume.lines[0] = "changed in returned context"
    context.seed.modes.clear()
    assert catalog.resumes[0].lines[0] != "changed in returned context"
    assert catalog.seeds[2].modes == ["mock", "practice"]
    with pytest.raises(ValueError, match="unsupported_mode"):
        build_seed_context(catalog, "seed-role-only", "other")


def test_practice_projection_matches_ts_and_hides_private_rubric(catalog, ts_reference):
    snapshots = practice_questions(catalog)
    assert [q.model_dump(mode="json") for q in snapshots] == ts_reference["practice_questions"]
    public = public_question(snapshots[0]).model_dump(mode="json")
    assert "rubric" not in public and "sources" not in public
    assert public["semantic_scoring_eligible"] is False
    assert snapshots[0].rubric and snapshots[0].sources


def change(data, path, value):
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize("path,value,error", [
    (["role", "status"], "released", "semantic_review_required"),
    (["questions", 0, "review", "eligible_for_semantic_scoring"], True, "inconsistent_review"),
    (["questions", 1, "id"], "q-tool-use-1", "duplicate_id"),
    (["questions", 0, "source_ids"], ["missing"], "missing_ref"),
    (["questions", 0, "rubric", 0, "source_ids"], ["http"], "rubric_source_outside_question"),
    (["questions", 1, "split"], "holdout", "split_leak:question-family"),
    (["seeds", 0, "split"], "holdout", "split_leak:candidate"),
    (["seeds", 2, "resume_id"], "resume-b", "resume_owner_mismatch"),
    (["seeds", 0, "context_mode"], "role_jd", "context_mode_invalid"),
    (["resumes", 0, "claims", 0, "end_line"], 99, "claim_range_invalid"),
    (["resumes", 0, "experience_prompts", 0, "claim_id"], "missing", "missing_ref"),
    (["jds", 2, "requirements", 0, "coverage"], "supported", "jd_coverage_invalid"),
    (["jds", 0, "requirements", 0, "line"], 99, "jd_range_invalid"),
    (["answers", 6, "resume_id"], None, "experience_requires_resume"),
    (["answers", 5, "independent"], True, "hint_independence_invalid"),
    (["trajectories", 0, "events", 0, "refs"], ["missing"], "missing_ref"),
    (["trajectories", 0, "events", 0, "refs"], ["answer-07"], "trajectory_owner_mismatch"),
    (["trajectories", 0, "events", 0, "invalidates"], ["correction-new"], "invalidation_requires_prior_event"),
    (["role", "default_plan", "role_only_mix", "foundation"], 99, "plan_mix_invalid"),
])
def test_invalid_cross_record_relationships_rejected(catalog_data, path, value, error):
    change(catalog_data, path, value)
    with pytest.raises(ValidationError, match=error):
        Catalog.model_validate(catalog_data)


@pytest.mark.parametrize("kind", ["resumes", "jds"])
def test_material_family_cannot_cross_splits(catalog_data, kind):
    catalog_data[kind][1]["family_id"] = catalog_data[kind][0]["family_id"]
    catalog_data[kind][1]["split"] = "holdout"
    with pytest.raises(ValidationError, match="split_leak"):
        Catalog.model_validate(catalog_data)


def test_labels_are_not_loaded_even_if_present(tmp_path, catalog_data):
    directory = tmp_path / "fixtures/pm0-05"
    directory.mkdir(parents=True)
    for key, name in CATALOG_FILES.items():
        (directory / f"{name}.json").write_text(json.dumps(catalog_data[key]), encoding="utf-8")
    (directory / "labels.json").write_text("not even valid JSON", encoding="utf-8")
    loaded = load_catalog(tmp_path)
    assert loaded.model_dump(mode="json") == catalog_data
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Catalog.model_validate({**catalog_data, "labels": {"expected_kind": "correct"}})
    with pytest.raises(ValueError, match="semantic_review_required"):
        require_semantic_review(loaded.role.review)


def test_released_catalog_requires_all_question_reviews(catalog_data):
    # Synthetic metadata mutation exercises rules; it does not review the actual corpus.
    catalog_data["role"]["status"] = "released"
    review = {"status": "human_reviewed", "human_reviewer": "synthetic-reviewer",
              "reviewed_at": "2026-10-23T10:00:00+08:00", "eligible_for_semantic_scoring": True}
    catalog_data["role"]["review"] = review.copy()
    for question in catalog_data["questions"]:
        question["review"] = review.copy()
    catalog = Catalog.model_validate(catalog_data)
    require_semantic_review(catalog.role.review, *(q.review for q in catalog.questions))
    with pytest.raises(ValueError, match="practice_catalog_review_changed"):
        practice_questions(catalog)
