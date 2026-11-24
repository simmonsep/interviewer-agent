"""F07Z-160: one preinstalled interviewer skill, frozen per session.

Unit tests are free and DB-free. The penultimate test proves the worker hands the frozen body to
the model; the last one proves the API freezes the body and digest at session creation (needs the
private test database).
"""
import asyncio

import pytest

from interview_agent.skills import registry
from interview_agent.skills.registry import PREINSTALLED, select
from interview_agent.interviews.agent import (
    LEGACY_SELECTION_RULES, SYSTEM_PROMPT, system_prompt_for)

from test_postgres import pg_settings  # noqa: F401  (pytest fixture reuse)


def test_the_preinstalled_manifest_is_complete():
    skill = select('interview', 'interview')
    assert skill is not None
    assert skill.id == 'interviewer-selection' and skill.version >= 2
    assert skill.modes == ('interview', 'coaching')
    assert len(skill.sha256) == 64
    assert skill.body.strip() and len(skill.body) < 2000


def test_only_the_matching_agent_and_mode_load_it():
    assert select('interview', 'coaching') is not None
    assert select('interview', 'written') is None
    assert select('materials', 'interview') is None


def test_the_body_carries_guidance_not_questions_or_permissions():
    body = select('interview', 'interview').body
    for forbidden in ('b-', 'fact_id', 'question_id'):
        assert forbidden not in body, 'the skill must not name pack ids or submission fields'
    assert '不授予任何工具权限' in body


def test_a_frozen_skill_is_appended_to_the_base_prompt():
    skill = select('interview', 'interview')
    prompt = system_prompt_for({'skill': skill.model_dump()})
    assert prompt.startswith(SYSTEM_PROMPT)
    assert f'[预装Skill interviewer-selection v{skill.version}]' in prompt
    assert body_marker(skill.body) in prompt
    # The guidance moved out of the base prompt: only the skill body carries it now.
    assert 'preferred_candidates是本场' not in SYSTEM_PROMPT
    assert 'preferred_candidates' in prompt


def test_an_old_session_without_the_field_keeps_the_legacy_rules():
    prompt = system_prompt_for({})
    assert prompt == SYSTEM_PROMPT + '\n' + LEGACY_SELECTION_RULES
    assert 'strategies是本场冻结的追问策略卡' in prompt
    assert '预装Skill' not in prompt


def test_a_newer_version_does_not_change_an_already_frozen_session(monkeypatch):
    old = select('interview', 'interview').model_dump()
    newer = registry.INTERVIEWER_SELECTION.model_copy(update={'version': 2})
    monkeypatch.setattr(registry, 'PREINSTALLED', (newer,))
    monkeypatch.setattr(registry, 'body_for', lambda manifest: '第二版正文：只问一个新的考点。')
    assert select('interview', 'interview').version == 2
    # The reopened session still replays v1 text, not the new body.
    replay = system_prompt_for({'skill': old})
    assert f"[预装Skill interviewer-selection v{old['version']}]" in replay
    assert body_marker(old['body']) in replay
    assert '第二版正文' not in replay


def frozen_version() -> int:
    return select('interview', 'interview').version


def body_marker(body: str) -> str:
    return body.strip().splitlines()[0]


@pytest.mark.postgres
def test_session_creation_freezes_the_skill_body_and_digest(pg_settings):
    import test_interview_repository as helpers

    with helpers.client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        user = helpers.signup(client)['user']['id']
        goal = helpers.post(client, '/api/career-goals/',
                            {'message': '准备Java后端面试，无简历也可以。',
                             'conditions': {'direction': 'Java后端'}}).json()
        response = helpers.post(client, f"/api/career-goals/{goal['id']}/interviews",
                                {'expected_version': 1, 'role': 'java-backend', 'max_questions': 2})
        assert response.status_code == 202, response.text
        session_id = response.json()['id']
        view = client.get(f"/api/career-goals/{goal['id']}/interviews/{session_id}").json()
        assert view['skills'][0]['id'] == 'interviewer-selection'
        assert view['skills'][0]['version'] == frozen_version()
        # The review needs the label and version, never the prompt body.
        assert 'body' not in view['skills'][0]
    rows = helpers.query(pg_settings[1],
                         'SELECT input_payload FROM app.career_interviews WHERE id=%s', (session_id,))
    helpers.query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (user,))
    skill = rows[0]['input_payload']['skills'][0]
    frozen = select('interview', 'interview')
    assert skill['id'] == frozen.id and skill['version'] == frozen.version
    assert skill['sha256'] == frozen.sha256 and skill['body'] == frozen.body


def test_the_worker_hands_the_frozen_skill_to_the_model(monkeypatch):
    """The prompt the provider actually receives is base + this session's frozen body."""
    from pydantic import SecretStr

    from interview_agent.interviews import worker as worker_module

    captured = {}

    class FakeProvider:
        def __init__(self, key, transport, *, system_prompt=None, tracer=None):
            captured['prompt'] = system_prompt

        async def complete(self, messages, tools):
            raise RuntimeError('stop after capturing the prompt')

        async def aclose(self):
            pass

    class Repo:
        pool = object()

        async def cached(self, claim):
            return None

        async def context(self, claim):
            return {'policy': {'role': 'java-backend', 'mode': 'interview', 'max_questions': 2},
                    'facts': [], 'question_history': [], 'current_question': None, 'answer': None,
                    'pack': {'topics': {}, 'topic_order': []},
                    'skill': select('interview', 'interview').model_dump()}

        async def fail_assessment(self, claim, **kwargs):
            captured['failed'] = kwargs

    class Queue:
        pass

    monkeypatch.setattr(worker_module, 'DeepSeekProvider', FakeProvider)
    monkeypatch.setattr(worker_module, 'load_key', lambda path: SecretStr('not-a-real-key'))
    worker = worker_module.worker_for(Repo(), Queue(), provider='live', credentials='ignored')
    asyncio.run(worker.prepare(type('Claim', (), {})(), asyncio.Event()))

    prompt = captured['prompt']
    assert 'failed' in captured, 'the fake provider error must be reported, not swallowed'
    assert prompt.startswith(SYSTEM_PROMPT)
    frozen = select('interview', 'interview')
    assert f'[预装Skill interviewer-selection v{frozen.version}]' in prompt
    assert body_marker(frozen.body) in prompt
