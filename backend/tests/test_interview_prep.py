"""Offline pieces of the preparation workflow: plan shape, contract bounds, stale codes."""
import pytest

from interview_agent.interview_prep.contracts import ConfirmPreparation, CreatePreparation
from interview_agent.interview_prep.graph import PLAN_VERSION, build_plan
from interview_agent.interview_prep.repository import canonical, plan_sha256


def frozen(**overrides):
    value = {'goal_version': 3, 'goal_status': 'ACTIVE', 'material_revision': 2,
             'target': {'kind': 'direction', 'direction': 'Java 后端'}, 'draft': None,
             'resume_item_indexes': [], 'role': 'java-backend', 'mode': 'interview',
             'max_questions': 2, 'duration_seconds': 1200}
    return value | overrides


def test_plan_is_deterministic_and_only_carries_ids_versions_and_counts():
    plan = build_plan(frozen(draft={'id': 'd-1', 'version': 4, 'fact_count': 3}))
    again = build_plan(frozen(draft={'id': 'd-1', 'version': 4, 'fact_count': 3}))
    assert canonical(plan) == canonical(again) and plan_sha256(plan) == plan_sha256(again)
    assert plan['version'] == PLAN_VERSION and plan['steps'] == ['load_inputs', 'plan', 'review', 'start_interview']
    assert plan['draft'] == {'id': 'd-1', 'version': 4, 'fact_count': 3}
    assert plan['frozen'] == {'goal_version': 3, 'material_revision': 2}
    assert plan['on_approve'].startswith('opens exactly one interview session')
    # A plan is wire data: it must not smuggle free-form resume text.
    assert set(plan) == {'version', 'role', 'mode', 'profile', 'max_questions', 'duration_seconds', 'target', 'draft',
                         'resume_item_indexes', 'use_resume', 'selection', 'frozen', 'steps', 'on_approve', 'on_reject'}


def test_plan_changes_when_the_frozen_scope_changes():
    base = plan_sha256(build_plan(frozen()))
    assert base != plan_sha256(build_plan(frozen(goal_version=4)))
    assert base != plan_sha256(build_plan(frozen(max_questions=3)))
    assert base != plan_sha256(build_plan(frozen(resume_item_indexes=[0])))
    assert base != plan_sha256(build_plan(frozen(use_resume=False)))


def test_explicit_no_resume_cannot_be_combined_with_a_draft():
    assert CreatePreparation(expected_version=1, role='java-backend', use_resume=False).use_resume is False
    with pytest.raises(ValueError):
        CreatePreparation(expected_version=1, role='java-backend', use_resume=False, draft_id='00000000-0000-0000-0000-000000000001')


@pytest.mark.parametrize('body', [
    {'expected_version': 1, 'role': 'java-backend', 'max_questions': 13},
    {'expected_version': 1, 'role': 'java-backend', 'duration_seconds': 5},
    {'expected_version': 1, 'role': 'random'},
    {'expected_version': 0, 'role': 'java-backend'},
])
def test_create_contract_bounds(body):
    with pytest.raises(ValueError):
        CreatePreparation.model_validate(body)


def test_resume_selection_requires_a_draft_and_no_duplicates():
    with pytest.raises(ValueError):
        CreatePreparation.model_validate({'expected_version': 1, 'role': 'java-backend',
                                          'resume_item_indexes': [0]})
    draft = '11111111-1111-1111-1111-111111111111'
    with pytest.raises(ValueError):
        CreatePreparation.model_validate({'expected_version': 1, 'role': 'java-backend', 'draft_id': draft,
                                          'resume_item_indexes': [0, 0]})
    value = CreatePreparation.model_validate({'expected_version': 1, 'role': 'java-backend', 'draft_id': draft,
                                              'resume_item_indexes': [0, 2]})
    assert value.resume_item_indexes == [0, 2]


def test_confirm_requires_a_full_plan_hash_and_a_real_decision():
    with pytest.raises(ValueError):
        ConfirmPreparation.model_validate({'expected_version': 1, 'plan_sha256': 'short', 'decision': 'approve'})
    with pytest.raises(ValueError):
        ConfirmPreparation.model_validate({'expected_version': 1, 'plan_sha256': '0' * 64, 'decision': 'maybe'})
    value = ConfirmPreparation.model_validate({'expected_version': 2, 'plan_sha256': 'a' * 64, 'decision': 'reject'})
    assert value.decision == 'reject' and value.reason == ''


def test_the_preparation_stream_name_is_accepted_and_unknown_suffixes_are_not():
    from interview_agent.worker.queue import QueueTransport

    QueueTransport(None, 'py-interview:test:jobs:v1:preparations')
    QueueTransport(None, 'py-interview:development:jobs:v1:preparations')
    with pytest.raises(ValueError, match='python_stream_required'):
        QueueTransport(None, 'py-interview:test:jobs:v1:unknown')

