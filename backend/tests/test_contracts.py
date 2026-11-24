import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

import pytest
from pydantic import Field, TypeAdapter, ValidationError

from interview_agent.contracts.catalog import Catalog
from interview_agent.contracts.common import FalseFlag
from interview_agent.contracts.practice import CreatePractice, CreateTarget, PracticeSessionView, SubmitAnswer, split_answer_lines, utf16_length
from interview_agent.contracts.runtime import AssessmentCandidate, Citation, FixtureBundle, RunEnvelope, RunInput, SqlSnapshot


@pytest.mark.parametrize("value", [True, "1", 1.0, 0, -1])
def test_public_version_requires_actual_positive_integer(value):
    with pytest.raises(ValidationError):
        SubmitAnswer(expected_version=value, text="a valid answer")


@pytest.mark.parametrize("identity", ["user_id", "server_bound_user", "candidate_id"])
def test_public_commands_reject_client_supplied_identity(identity):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SubmitAnswer.model_validate({"expected_version": 1, "text": "answer", identity: "other-user"})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CreateTarget.model_validate({"role_package_id": "agent-intern", "level": "intern", identity: "other-user"})


@pytest.mark.parametrize("budget", [{"provider_requests": True}, {"provider_requests": "1"}, {"provider_requests": 1.0},
                                    {"tool_calls": 13}, {"turns": None}, {"wall_ms": 180001}, {"user_id": "other"}])
def test_runtime_budget_is_strict_and_nested_extra_fields_rejected(ts_reference, budget):
    data = deepcopy(ts_reference["assessment_fixtures"]["answer-01"]["input"])
    data["budget"].update(budget)
    with pytest.raises(ValidationError):
        RunInput.model_validate(data)


def test_runtime_tools_and_reserved_context_fields(ts_reference):
    data = deepcopy(ts_reference["assessment_fixtures"]["answer-01"]["input"])
    run = RunInput.model_validate_json(json.dumps(data))
    assert run.budget.turns == 6 and run.budget.wall_ms == 180000
    assert run.model_dump(mode="json", exclude_unset=True) == data
    data["allowed_tools"] = ["read_question", "read_question"]
    with pytest.raises(ValidationError, match="duplicate_id"):
        RunInput.model_validate(data)
    data["allowed_tools"] = ["shell"]
    with pytest.raises(ValidationError):
        RunInput.model_validate(data)


@pytest.mark.parametrize("value", [0, 1, "false", True])
def test_false_literal_does_not_accept_python_bool_integer_equality(value):
    with pytest.raises(ValidationError):
        TypeAdapter(FalseFlag).validate_python(value)


@pytest.mark.parametrize("text", ["", " \t\r\n", "\ufeff", "a" * 2001, "😀" * 1001, "a\n" * 40])
def test_answer_bounds_and_whitespace_are_rejected(text):
    with pytest.raises(ValidationError):
        SubmitAnswer(expected_version=1, text=text)


def test_original_answer_is_not_trimmed_or_splitlines_normalized():
    text = " first\r\n\r\nlast\n"
    answer = SubmitAnswer(expected_version=1, text=text)
    assert answer.text == text
    assert split_answer_lines(answer.text) == [" first", "", "last", ""]
    assert split_answer_lines("a\rb\u2028c") == ["a\rb\u2028c"]
    assert SubmitAnswer(expected_version=1, text="😀" * 1000).text == "😀" * 1000
    assert utf16_length("😀") == 2 and len("😀") == 1
    assert len(split_answer_lines("a\n" * 39)) == 40
    SubmitAnswer(expected_version=1, text="a\n" * 39)


def test_unicode_difference_from_typebox_is_explicit(ts_reference):
    assert ts_reference["unicode_probe"] == {"emoji_max_1": True, "combining_max_1": True,
                                              "float_1_is_integer": True, "bool_is_integer": False}
    # New bounded Pydantic strings count code points; old TypeBox counts graphemes.
    bounded = TypeAdapter(Annotated[str, Field(max_length=1)])
    bounded.validate_python("😀")
    with pytest.raises(ValidationError):
        bounded.validate_python("e\u0301")


def test_fixture_and_user_submission_provenance_cannot_be_interchanged(project_root, ts_reference):
    original = json.loads((project_root / "fixtures/pm0-03/bundle.json").read_text(encoding="utf-8"))
    assert FixtureBundle.model_validate(original).model_dump(mode="json") == original
    data = deepcopy(ts_reference["assessment_fixtures"]["answer-01"]["bundle"])
    data.update(schema_version="sql-snapshot-v1", origin="sql_snapshot")
    for evidence in data["evidence"]:
        evidence["origin"] = "authored_paraphrase"
    data["answers"][0].update(origin="user_submitted", lines=["first", "", "last", ""])
    assert SqlSnapshot.model_validate(data).answers[0].lines == ["first", "", "last", ""]
    with pytest.raises(ValidationError):
        FixtureBundle.model_validate(data)
    data["answers"][0]["origin"] = "synthetic_fixture"
    with pytest.raises(ValidationError):
        SqlSnapshot.model_validate(data)


@pytest.mark.parametrize("patch", [{"start_line": 0}, {"start_line": True}, {"start_line": 2, "end_line": 1},
                                  {"quote": 123}, {"owner_user_id": "other"}])
def test_citation_shape_and_order_rejected_without_claiming_quote_authentication(patch):
    data = {"kind": "answer", "source_id": "answer-a", "version": "v1", "start_line": 1, "end_line": 1, "quote": "text"}
    data.update(patch)
    with pytest.raises(ValidationError):
        Citation.model_validate(data)


def test_uuid_and_timestamp_remain_json_strings(ts_reference):
    value = "00000000-0000-0000-0000-000000000001"
    assert CreatePractice(target_id=value, question_id="q-tool-use-1").target_id == value
    with pytest.raises(ValidationError):
        CreatePractice(target_id=UUID(value), question_id="q-tool-use-1")
    question = {key: ts_reference["practice_questions"][0][key] for key in (
        "id", "text", "topic", "version", "review", "semantic_scoring_eligible")}
    data = {"id": value, "version": 1, "status": "WAITING_FOR_ANSWER", "mode": "practice", "context_mode": "role_only",
            "target_id": value, "question": question, "created_at": "2026-10-23T09:00:00+08:00",
            "answer": None, "operation": None, "feedback": None}
    assert PracticeSessionView.model_validate(data).model_dump(mode="json") == data
    for timestamp in ["2026-10-23", "2026-10-23T09:00:00", datetime.now(timezone.utc)]:
        with pytest.raises(ValidationError):
            PracticeSessionView.model_validate({**data, "created_at": timestamp})


def test_json_schema_exposes_closed_objects_and_unique_tools():
    for model in [Catalog, RunInput, RunEnvelope, AssessmentCandidate, SubmitAnswer, PracticeSessionView]:
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        for definition in schema.get("$defs", {}).values():
            if definition.get("type") == "object":
                assert definition["additionalProperties"] is False
    assert RunInput.model_json_schema()["properties"]["allowed_tools"]["uniqueItems"] is True


def test_envelope_cannot_claim_commit_or_known_unobserved_usage():
    data = {"operation_id": "op-a", "run_id": "run-a", "parent_run_id": None, "session_id": "session-a",
            "termination": "awaiting_input", "candidate": None, "committed": False, "error_code": None,
            "counts": {"provider_request_attempts": 0, "provider_requests_admitted": 0, "stream_invocations": 0,
                       "assistant_turns": 0, "tool_calls_proposed": 0, "tool_calls_admitted": 0,
                       "tool_executions": 0, "tool_errors": 0},
            "usage": {"source": "unavailable", "input_tokens": None, "output_tokens": None, "estimated_cost": None},
            "http_audit": {"attempts": None, "scope": "not_observed_by_adapter"},
            "trace_ref": None, "diagnostic_errors": [], "budget_state": None}
    assert RunEnvelope.model_validate_json(json.dumps(data)).model_dump(mode="json") == data
    with pytest.raises(ValidationError):
        RunEnvelope.model_validate({**data, "committed": True})
    changed = deepcopy(data)
    changed["usage"]["input_tokens"] = 0
    with pytest.raises(ValidationError):
        RunEnvelope.model_validate(changed)
