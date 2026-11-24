"""Whole-session interview protocol baseline. Scripted only; no paid provider.

This is a thin entry next to `domain.py`: it reuses the same authenticated API,
test PostgreSQL/Redis, owned Worker pump and synthetic accounts. It differs in one
place that matters for evaluation honesty: answers are selected by question
identity (exact question ID first, then an authored topic rule), not by the order
of a fixed answer list. When no rule matches, the run records whether the Agent
left the declared case scope or the fixture simply did not cover the question.
"""
import argparse
import json
import secrets
import time
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import OperationalError
from pydantic import Field

from interview_agent.api.__main__ import selector_loop
from interview_agent.api.app import create_app
from interview_agent.contracts.common import Contract
from .__main__ import ledger, provenance, write
from .adapters import arun, cleanup, pump
from .candidate_simulation import CandidateProfile, CandidateSimulator
from .domain_adapters import role_pump
from .domain_contracts import DomainInput
from .reviews import Review, item_hash
from .suite import read_jsonl, sha

CaseId = Annotated[str, Field(pattern=r'^IS[0-9]{2}$')]
SUITE_FILES = ('cases.jsonl', 'oracles.jsonl', 'baseline-spec.json')
ACTIVE = ('PENDING', 'RUNNING', 'WAITING_FOR_USER', 'WAITING_CREDENTIALS')
STATUSES = {*ACTIVE, 'COMPLETED', 'FAILED', 'STALE', 'CANCELLED'}
SEVERE = {'answers_preserved', 'no_ability_certification', 'provider_budget', 'single_session',
          'no_project_without_material', 'project_fact_in_scope', 'question_text_quotes_fact',
          'formal_mode_no_explanation', 'duplicate_answer_idempotent',
          'conflicting_payload_rejected', 'stale_version_rejected'}


class CandidateRule(Contract):
    question_id: str | None = None
    topic_id: str | None = None
    kind: Literal['base', 'probe', 'retest', 'project'] | None = None
    branch: Annotated[str, Field(min_length=1, max_length=40)]
    answer: Annotated[str, Field(min_length=1, max_length=1500)]


class CandidatePolicy(Contract):
    allowed_topics: Annotated[list[str], Field(min_length=1, max_length=8)]
    allowed_kinds: Annotated[list[str], Field(min_length=1, max_length=4)]
    rules: Annotated[list[CandidateRule], Field(min_length=1, max_length=12)]


class SessionCase(Contract):
    case_id: CaseId
    family_id: Annotated[str, Field(min_length=1, max_length=80)]
    split: Literal['development', 'reserved_candidate']
    pair_id: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    mode: Literal['interview', 'coaching']
    role: Literal['java-backend', 'ai-application']
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    jd_text: Annotated[str, Field(max_length=2500)] = ''
    source_text: Annotated[str, Field(max_length=4000)] = ''
    confirm_count: Annotated[int, Field(ge=0, le=12)] = 0
    max_questions: Annotated[int, Field(ge=1, le=6)] = 2
    stop_after_questions: Annotated[int, Field(ge=1, le=6)] | None = None
    idempotency_probe: bool = False
    # 收尾 04: retract the first observation after the session ends, then re-read it. Off by
    # default so existing suites are untouched.
    retract_first_observation: bool = False
    provenance: Literal['project_authored_synthetic'] = 'project_authored_synthetic'
    review: Literal['pending_human_review'] = 'pending_human_review'
    candidate: CandidatePolicy


class ExpectedTurn(Contract):
    topic_id: Annotated[str, Field(min_length=1, max_length=40)]
    kind: Literal['base', 'probe', 'retest', 'project']
    question_id: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    assisted: bool = False
    fact_in_scope: bool = False
    question_text_quotes_fact: bool = False


class SessionOracle(Contract):
    case_id: CaseId
    expected_turns: Annotated[list[ExpectedTurn], Field(min_length=1, max_length=6)]
    expected_status: Literal['COMPLETED', 'WAITING_FOR_USER', 'FAILED']
    expected_stop_reason: Annotated[str, Field(min_length=1, max_length=40)]
    protocol_checks: Annotated[list[str], Field(min_length=1, max_length=20)]
    human_rubric: Annotated[list[str], Field(min_length=1, max_length=8)]


class SessionAttempt(Contract):
    case_id: CaseId
    adapter: Literal['system_scripted'] = 'system_scripted'
    repeat: Annotated[int, Field(ge=1, le=3)] = 1
    status: Literal['completed', 'product_failure', 'infrastructure_failure', 'not_run',
                    'evaluation_incomplete']
    # Failure classification requires both: where it broke, and why. `stage` is None on success.
    stage: Literal['setup', 'material', 'interview', 'grading'] | None = None
    output: dict = Field(default_factory=dict)
    provider_steps: Annotated[int, Field(ge=0)] = 0
    setup_provider_steps: Annotated[int, Field(ge=0)] = 0
    decision_elapsed_ms: Annotated[float, Field(ge=0)] = 0
    error_code: str | None = None
    model_http_calls: Annotated[int, Field(ge=0)] | None = 0
    model_cost_cny: Literal[0] | None = 0
    model_cost_upper_nano: Annotated[int, Field(ge=0)] | None = 0
    model_usage_status: Literal['not_called', 'known', 'unknown'] = 'not_called'


def load_session_suite(directory):
    root = Path(directory)
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if set(manifest['sha256']) != set(SUITE_FILES):
        raise ValueError('session_suite_files')
    for name in SUITE_FILES:
        if sha(root / name) != manifest['sha256'][name]:
            raise ValueError('session_suite_checksum')
    baseline = json.loads((root / 'baseline-spec.json').read_text(encoding='utf-8'))
    cases = read_jsonl(root / 'cases.jsonl', SessionCase)
    oracles = read_jsonl(root / 'oracles.jsonl', SessionOracle)
    if len({c.case_id for c in cases}) != len(cases) or len({o.case_id for o in oracles}) != len(oracles):
        raise ValueError('session_duplicate_case')
    if {c.case_id for c in cases} != {o.case_id for o in oracles}:
        raise ValueError('session_oracle_missing')
    families = {}
    for case in cases:
        if case.family_id in families and families[case.family_id] != case.split:
            raise ValueError('session_family_crosses_split')
        families[case.family_id] = case.split
    counts = {s: sum(c.split == s for c in cases) for s in ('development', 'reserved_candidate')}
    if len(cases) != manifest['cases'] or counts != manifest['splits']:
        raise ValueError('session_suite_counts')
    declared = set(baseline['protocol_checks'])
    indexed = cases_by_id(cases)
    for oracle in oracles:
        if not set(oracle.protocol_checks) <= declared:
            raise ValueError('session_oracle_check_unknown')
        if len(oracle.expected_turns) > indexed[oracle.case_id].max_questions:
            raise ValueError('session_oracle_turn_count')
    by_pair = {}
    for case in cases:
        if case.pair_id:
            by_pair.setdefault(case.pair_id, []).append(case)
    for pair_id, members in by_pair.items():
        if len(members) != 2 or {m.mode for m in members} != {'interview', 'coaching'}:
            raise ValueError('session_pair_mismatch')
        first, second = members
        shared = ('family_id', 'role', 'message', 'jd_text', 'source_text', 'confirm_count',
                  'max_questions', 'stop_after_questions', 'candidate')
        if any(getattr(first, field) != getattr(second, field) for field in shared):
            raise ValueError('session_pair_mismatch')
    return manifest, cases, {o.case_id: o for o in oracles}, baseline


def cases_by_id(cases):
    return {c.case_id: c for c in cases}


def load_candidate_profiles(directory, cases):
    """S00C: load the truth side and check it against the cases it claims to serve.

    Validation happens here, at load time: a rule that names an unknown fact, an ambiguous exact
    rule or a profile for a case that does not exist fails before any session starts.
    """
    root = Path(directory)
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    declared = manifest.get('sha256') or {}
    if 'profiles.jsonl' not in declared or declared['profiles.jsonl'] != sha(root / 'profiles.jsonl'):
        raise ValueError('candidate_profile_checksum')
    profiles = read_jsonl(root / 'profiles.jsonl', CandidateProfile)
    by_case = {profile.case_id: profile for profile in profiles}
    if len(by_case) != len(profiles):
        raise ValueError('candidate_profile_duplicate')
    known = {case.case_id for case in cases}
    if set(by_case) - known:
        raise ValueError('candidate_profile_unknown_case')
    if set(declared) and set(manifest.get('case_ids') or []) != set(by_case):
        raise ValueError('candidate_profile_case_list')
    for profile in by_case.values():
        CandidateSimulator(profile)  # rules/facts/ambiguity are checked before a run, not during
    return by_case


def resolve_rule(case, question):
    """Exact question ID first, then topic+kind, then topic. Never invent an answer."""
    question_id = question.get('question_id')
    topic_id = question.get('topic_id')
    kind = question.get('kind')
    for rule in case.candidate.rules:
        if rule.question_id and rule.question_id == question_id:
            return rule, None
    for rule in case.candidate.rules:
        if rule.topic_id == topic_id and rule.kind == kind:
            return rule, None
    for rule in case.candidate.rules:
        if rule.topic_id == topic_id and rule.kind is None:
            return rule, None
    if topic_id not in case.candidate.allowed_topics or kind not in case.candidate.allowed_kinds:
        return None, 'agent_out_of_scope'
    return None, 'fixture_uncovered'



def _application_signature(view):
    """收尾 04: the interview must not create or advance an application by itself."""
    return sorted((str(item.get('id')), str(item.get('status')))
                  for item in ((view or {}).get('items') or []))

def _execute(case, settings, owner, root, profile=None):
    if settings.mode != 'test' or settings.database.dbname != 'py_interview_test' or owner.dbname != 'py_interview_test':
        raise ValueError('evaluation_test_environment_required')
    if (owner.host, owner.port) != (settings.database.host, settings.database.port):
        raise ValueError('evaluation_database_mismatch')
    uid = None
    started = time.perf_counter()
    view = {}
    status = 'completed'
    error = None
    stage = None
    requests = []
    steps = 0
    setup_steps = 0
    turns = []
    statuses = []
    probes = {}
    output = {}
    try:
        with TestClient(create_app(settings), base_url=settings.origin, raise_server_exceptions=False,
                        backend_options={'loop_factory': selector_loop}) as client:
            client.headers['Origin'] = settings.origin

            def call(path, body=None, key=None):
                headers = {'Idempotency-Key': key or uuid4().hex} if body is not None else None
                response = client.get(path) if body is None else client.post(path, json=body, headers=headers)
                requests.append({'method': 'GET' if body is None else 'POST', 'path': path,
                                 'status': response.status_code})
                return response

            def request(path, body=None, key=None):
                response = call(path, body, key)
                if response.status_code >= 400:
                    raise RuntimeError('session_http_' + str(response.status_code) + '_'
                                       + str(response.json().get('code', 'unknown')))
                return response.json()

            user = request('/api/auth/sign-up/email', {'email': 'session-' + uuid4().hex + '@example.com',
                'name': 'Synthetic interview session', 'password': secrets.token_urlsafe(24)})
            uid = user['user']['id']
            goal = request('/api/career-goals/', {'message': case.message, 'conditions': {}, 'jd_text': case.jd_text})
            gid = goal['id']
            base = '/api/career-goals/' + gid
            goal_version = goal['version']
            material_facts = []
            draft_id = None
            if case.source_text:
                stage = 'material'
                path = base + '/materials'
                view = request(path)
                view = request(path + '/sources', {'expected_version': goal_version,
                    'expected_revision': view['material_revision'], 'name': 'synthetic-session.md',
                    'format': 'markdown', 'text': case.source_text})
                view = request(path + '/runs', {'expected_version': goal_version,
                    'expected_revision': view['material_revision'], 'mode': 'analyze'})
                analysis_id = view['run_id']
                arun(role_pump(settings, gid, 'materials', root,
                               DomainInput(workflow='materials', message=case.message), None))
                view = request(path)
                if case.confirm_count:
                    view = request(path + '/confirmations', {'expected_version': goal_version,
                        'expected_revision': view['material_revision'], 'run_id': analysis_id,
                        'proposal_ids': list(range(case.confirm_count))})
                view = request(path + '/runs', {'expected_version': goal_version,
                    'expected_revision': view['material_revision'], 'mode': 'draft'})
                arun(role_pump(settings, gid, 'materials', root,
                               DomainInput(workflow='materials', message=case.message), None))
                view = request(path)
                setup_steps = view['material_model_requests']
                material_facts = view['facts']
                draft_id = view['drafts'][0]['id'] if view['drafts'] else None
                if draft_id is None:
                    raise RuntimeError('session_material_draft_missing')
                goal_version = request(base)['version']
            body = {'expected_version': goal_version, 'role': case.role, 'mode': case.mode,
                    'max_questions': case.max_questions}
            if draft_id:
                body['draft_id'] = draft_id
            applications_before = request(base + '/applications')
            stage = 'interview'
            view = request(base + '/interviews', body)
            sid = view['id']
            path = base + '/interviews/' + sid
            statuses.append(view['status'])
            answered = set()
            index = 0
            simulator = CandidateSimulator(profile) if profile is not None else None
            state = simulator.begin() if simulator is not None else None
            while True:
                status_before = view['status']
                arun(pump(settings, gid, 'interview', sid=sid))
                current = request(path)
                run = current.get('run') or {}
                steps += run.get('model_requests') or 0
                statuses.append(current['status'])
                if current['status'] not in ACTIVE:
                    view = current
                    break
                questions = [e for e in current['events'] if e['kind'] == 'question']
                question = questions[-1] if questions else None
                if question is None:
                    status, error, view = 'product_failure', 'session_question_missing', current
                    stage = 'interview'
                    break
                if question['id'] in answered:
                    status, error, view = 'product_failure', 'session_duplicate_question', current
                    stage = 'interview'
                    break
                payload = question['payload']
                if simulator is not None:
                    reply = simulator.next_answer(payload, state)
                    if reply.status != 'answered':
                        status = 'evaluation_incomplete'
                        error = 'candidate_' + (reply.reason or reply.status)
                        stage = 'interview'
                        probes['unmatched'] = {'reason': reply.reason, 'question': payload}
                        view = current
                        break
                    answer_text, branch = reply.text, reply.branch
                    behavior, revealed = reply.behavior, list(reply.revealed_fact_ids)
                else:
                    rule, reason = resolve_rule(case, payload)
                    if rule is None:
                        status, error = 'evaluation_incomplete', 'candidate_' + reason
                        stage = 'interview'
                        probes['unmatched'] = {'reason': reason, 'question': payload}
                        view = current
                        break
                    answer_text, branch, behavior, revealed = rule.answer, rule.branch, None, []
                key = uuid4().hex
                answer_body = {'expected_version': current['version'], 'question_id': question['id'],
                               'text': answer_text}
                turn_started = time.perf_counter()
                answered_view = request(path + '/answers', answer_body, key=key)
                elapsed = (time.perf_counter() - turn_started) * 1000
                statuses.append(answered_view['status'])
                events = answered_view['events']
                answer_event = [e for e in events if e['kind'] == 'answer'][-1]
                turns.append({'turn_id': index + 1, 'run_id': run.get('id'),
                              'question_event_id': question['id'],
                              'question_id': payload['question_id'],
                              'topic_id': payload['topic_id'],
                              'kind': payload['kind'], 'question_text': payload['text'],
                              'answer_text': answer_text,
                              'fact_id': payload.get('fact_id'), 'assisted': payload.get('assisted'),
                              'branch': branch, 'behavior': behavior,
                              'revealed_fact_ids': revealed,
                              'answer_event_id': answer_event['id'],
                              'model_requests': run.get('model_requests') or 0,
                              'elapsed_ms': elapsed,
                              'tool_names': [t.get('tool') for t in run.get('tool_log') or []]})
                answered.add(question['id'])
                index += 1
                if case.idempotency_probe and index == 1:
                    probes.update(idempotency_probes(call, path, answer_body, key, events))
                view = answered_view
                if case.stop_after_questions and index >= case.stop_after_questions:
                    view = request(path + '/stop', {'expected_version': view['version']})
                    statuses.append(view['status'])
                    break
            listing = request(base + '/interviews')
            applications_after = request(base + '/applications')
            observations = view.get('observations') or []
            if case.retract_first_observation and observations:
                retracted_id = str(observations[0]['id'])
                request(base + '/interview-memory/' + retracted_id + '/retract',
                        {'reason': 'synthetic retraction for the evaluation case'}, key=uuid4().hex)
                # Re-read: the retraction must be visible to the report the user actually sees,
                # and the suggestion derived from that observation must be gone.
                view = request(path)
                observations = view.get('observations') or []
            report = view.get('report') or {}
            events = view.get('events') or []
            answer_events = [e for e in events if e['kind'] == 'answer']
            question_events = [e for e in events if e['kind'] == 'question']
            explanation_events = [e for e in events if e['kind'] == 'explanation']
            fact_ids = [f['id'] for f in material_facts]
            quotes = {f['id']: f['quote'] for f in material_facts}
            project_questions = [e for e in question_events if e['payload']['topic_id'] == 'project']
            project_in_scope = all(
                e['payload'].get('fact_id') in fact_ids for e in project_questions)
            project_quotes = all(
                e['payload'].get('fact_id') in quotes
                and quotes[e['payload']['fact_id']] in e['payload']['text'] for e in project_questions)
            retracted_ids = {str(o['id']) for o in observations if o.get('correction')}
            retracted_ids |= ({str(observations[0]['id'])} if case.retract_first_observation and observations else set())
            protocol = {
                'answers_preserved': [e['payload']['text'] for e in answer_events] == [t['answer_text'] for t in turns],
                'observations_cite_session_answers': all(
                    o['answer_id'] in {e['id'] for e in answer_events} for o in observations),
                'status_legal': all(s in STATUSES for s in statuses),
                'no_ability_certification': report.get('semantic_scoring_eligible') is False,
                'provider_budget': all(t['model_requests'] <= 4 for t in turns),
                'single_session': len(listing['items']) == 1,
                'no_project_without_material': bool(fact_ids) or not project_questions,
                'project_fact_in_scope': project_in_scope,
                'question_text_quotes_fact': project_quotes,
                'coaching_explanation_present': bool(explanation_events) and any(
                    e['payload'].get('assisted') for e in question_events),
                'formal_mode_no_explanation': not explanation_events and not any(
                    e['payload'].get('assisted') for e in question_events),
                'duplicate_answer_idempotent': probes.get('duplicate', {}).get('status') == 202
                    and probes.get('duplicate', {}).get('events_before') == probes.get('duplicate', {}).get('events_after'),
                'conflicting_payload_rejected': probes.get('conflict', {}).get('status') == 409
                    and probes.get('conflict', {}).get('code') == 'idempotency_conflict',
                'stale_version_rejected': probes.get('stale', {}).get('status') == 409
                    and probes.get('stale', {}).get('code') in ('version_conflict', 'question_changed'),
                'stop_honoured': view['status'] == 'COMPLETED' and view['report'] is not None
                    and len(question_events) == len(turns),
                'retest_suggestion_matches_observation': retest_matches_observations(report, observations),
                'application_progress_unchanged': _application_signature(applications_before)
                    == _application_signature(applications_after),
                'retracted_observation_not_suggested': all(
                    str(item.get('observation_id')) not in retracted_ids
                    for item in ((report or {}).get('retest') or [])),
            }
            output = {
                'sql_committed': status == 'completed',
                'session_id': sid,
                'final_status': view['status'],
                'stop_reason': report.get('stop_reason'),
                'target_kind': (view.get('target') or {}).get('kind'),
                'policy_version': (view.get('policy') or {}).get('version'),
                'pack_version': view.get('pack_version'),
                'material_fact_ids': fact_ids,
                'draft_id': draft_id,
                'turns': turns,
                'observations': [{k: o.get(k) for k in ('id', 'answer_id', 'label', 'evidence_id',
                                                        'assisted', 'correction')}
                                 for o in observations],
                'report': {k: report.get(k) for k in ('questions', 'answers', 'assisted_answers',
                                                      'semantic_scoring_eligible', 'unreviewed_answer_ids')}
                          | {'retest': report.get('retest') or []},
                'protocol': protocol,
                'probes': probes,
                'api_requests': requests,
                'provider': 'scripted_protocol',
                'semantic_scoring_eligible': False,
            }
    except OperationalError:
        status, error = 'infrastructure_failure', 'session_database_unavailable'
        stage = stage or 'setup'
    except (RuntimeError, KeyError, ValueError) as exc:
        status, error = 'product_failure', str(exc)[:180]
        stage = stage or 'setup'
        output = output or {'last_view_status': view.get('status')}
    finally:
        if uid:
            arun(cleanup(owner, uid))
    return {'status': status, 'stage': stage if status != 'completed' else None, 'error_code': error,
            'provider_steps': steps,
            'setup_provider_steps': setup_steps,
            'decision_elapsed_ms': (time.perf_counter() - started) * 1000, 'output': output}


def idempotency_probes(call, path, answer_body, key, events):
    before = len([e for e in events if e['kind'] == 'answer'])
    replay = call(path + '/answers', answer_body, key=key)
    after = len([e for e in (replay.json().get('events') or []) if e['kind'] == 'answer'])
    conflict = call(path + '/answers', {**answer_body, 'text': answer_body['text'] + '（冲突正文）'},
                    key=key)
    stale = call(path + '/answers', answer_body, key=uuid4().hex)
    return {'duplicate': {'status': replay.status_code, 'events_before': before, 'events_after': after},
            'conflict': {'status': conflict.status_code, 'code': conflict.json().get('code')},
            'stale': {'status': stale.status_code, 'code': stale.json().get('code')}}



def retest_matches_observations(report, observations):
    """收尾 04 check: a retest suggestion may only point at a `needs_probe` observation of this
    session and must carry a real question.

    Property, not content: the suite does not hard-code the authored text (that lives in the
    question pack), it only refuses suggestions that invent an observation, cite a supported or
    retracted one, or arrive without a question.
    """
    by_id = {str(item.get('id')): item for item in observations}
    suggestions = (report or {}).get('retest') or []
    for suggestion in suggestions:
        target = by_id.get(str(suggestion.get('observation_id')))
        if target is None:
            return False
        if target.get('label') != 'needs_probe' or target.get('correction'):
            return False
        if not str(suggestion.get('question') or '').strip():
            return False
    return True

def execute_session(case, settings, owner, root, profile=None):
    """Public producer entry: no oracle, rubric or expected action enters here.

    `profile` is the S00C truth side for this case. Without it the older inline candidate rules
    keep working unchanged, so the frozen suite's bytes and behaviour stay identical.
    """
    return _execute(case, settings, owner, root, profile)


def grade_session(attempt, oracle, case):
    if attempt.status in ('infrastructure_failure', 'not_run', 'evaluation_incomplete'):
        return {'machine_pass': None, 'checks': {}, 'turn_matches': [], 'severe_violations': [],
                'human_review': 'pending', 'domain_success': None}
    if attempt.case_id != oracle.case_id:
        raise ValueError('oracle_case_mismatch')
    out = attempt.output
    checks = {name: out.get('protocol', {}).get(name) is True for name in oracle.protocol_checks}
    observed = out.get('turns') or []
    matches = []
    for index, expected in enumerate(oracle.expected_turns):
        if index >= len(observed):
            matches.append({'index': index, 'expected': expected.model_dump(mode='json'),
                            'observed': None, 'match': False})
            continue
        turn = observed[index]
        match = (turn.get('topic_id') == expected.topic_id and turn.get('kind') == expected.kind
                 and (expected.question_id is None or turn.get('question_id') == expected.question_id)
                 and turn.get('assisted') == expected.assisted)
        if expected.fact_in_scope:
            match = match and out.get('protocol', {}).get('project_fact_in_scope') is True
        if expected.question_text_quotes_fact:
            match = match and out.get('protocol', {}).get('question_text_quotes_fact') is True
        matches.append({'index': index, 'expected': expected.model_dump(mode='json'),
                        'observed': {k: turn.get(k) for k in ('topic_id', 'kind', 'question_id', 'assisted')},
                        'match': match})
    if len(observed) != len(oracle.expected_turns):
        matches.append({'index': len(observed), 'expected': None, 'observed': len(observed), 'match': False})
    checks['expected_turn_sequence'] = all(item['match'] for item in matches)
    checks['stop_reason_matches'] = out.get('stop_reason') == oracle.expected_stop_reason
    checks['expected_status_matches'] = out.get('final_status') == oracle.expected_status
    violations = [name + '_not_verified' for name, ok in checks.items() if not ok and name in SEVERE]
    return {'machine_pass': all(checks.values()), 'checks': checks, 'turn_matches': matches,
            'severe_violations': violations, 'human_review': 'pending', 'domain_success': None}


def summarize_session(records, case_ids, oracles, baseline):
    attempts = len(records)
    valid = [r for r in records if r['grade']['machine_pass'] is not None]
    passed = sum(r['grade']['machine_pass'] is True for r in valid)
    planned = sum(len(oracles[cid].protocol_checks) for cid in case_ids)
    applicable = sum(len(oracles[r['attempt']['case_id']].protocol_checks) for r in valid)
    checks = [{'case_id': r['attempt']['case_id'], 'check': name, 'ok': ok}
              for r in valid for name, ok in r['grade']['checks'].items()]
    failed = [c for c in checks if not c['ok']]
    turns = [{'case_id': r['attempt']['case_id'], **turn} for r in records
             for turn in (r['attempt']['output'].get('turns') or [])]
    unmatched = [{'case_id': r['attempt']['case_id'], 'stage': r['attempt']['stage'],
                  'error_code': r['attempt']['error_code'],
                  'probes': r['attempt']['output'].get('probes', {})} for r in records
                 if r['attempt']['status'] == 'evaluation_incomplete']
    # Every non-success attempt is classified by where it broke (`stage`) and why (`reason`).
    failures = [{'case_id': r['attempt']['case_id'], 'status': r['attempt']['status'],
                 'stage': r['attempt']['stage'], 'reason': r['attempt']['error_code']}
                for r in records if r['attempt']['status'] != 'completed']
    return {
        'suite': baseline['suite_version'],
        'cases': attempts,
        'selected_ids': list(case_ids),
        'metrics': {
            'attempts': attempts,
            'completed': sum(r['attempt']['status'] == 'completed' for r in records),
            'machine_passes': passed,
            'machine_pass_rate_valid': passed / len(valid) if valid else None,
            'machine_pass_rate_all_attempts': passed / attempts if attempts else None,
            'infrastructure_failures': sum(r['attempt']['status'] == 'infrastructure_failure' for r in records),
            'product_failures': sum(r['attempt']['status'] == 'product_failure' for r in records),
            'evaluation_incomplete': sum(r['attempt']['status'] == 'evaluation_incomplete' for r in records),
            'not_run': sum(r['attempt']['status'] == 'not_run' for r in records),
            'severe_violation_cases': sum(bool(r['grade']['severe_violations']) for r in records),
            'provider_steps': sum(r['attempt']['provider_steps'] for r in records),
            'setup_provider_steps': sum(r['attempt']['setup_provider_steps'] for r in records),
            'median_decision_ms': sorted(r['attempt']['decision_elapsed_ms'] for r in records)[len(records) // 2]
                if records else 0,
            'model_http_calls': 0,
            'model_cost_upper_nano': 0,
            'model_cost_cny': 0,
            'cost_per_success': 0 if passed else None,
            'cost_success_denominator': passed,
        },
        'protocol_checks': {
            'planned': planned,
            'applicable': applicable,
            'passed': sum(c['ok'] for c in checks),
            'failed': failed,
            'not_run': planned - applicable,
        },
        'semantic': {
            'human_reviewed_cases': 0,
            'human_review': 'pending',
            'semantic_pass_rate': None,
            'domain_success_rate': None,
            'semantic_scoring_eligible': False,
        },
        'per_turn': turns,
        'unmatched': unmatched,
        'failures': failures,
        'failed_cases': [r['attempt']['case_id'] for r in records if r['grade']['machine_pass'] is False],
        'provider': 'scripted_protocol',
        'evaluation_scope': 'protocol_and_fixture_coverage_not_llm_quality',
        'human_task_approval': 0,
        'human_output_approval': 0,
        'reserved_executed': False,
        'task_independence': 'case IDs counted; shared catalog/templates, not statistically independent samples',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'run', 'review-export', 'review-validate'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--suite', type=Path)
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--worker-config', type=Path)
    parser.add_argument('--owner-config', type=Path)
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--review-file', type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    manifest, cases, oracles, baseline = load_session_suite(
        args.suite or root / 'benchmarks/interview-session-v0.1')
    selected = [c for c in cases if c.split == 'development']
    if args.cases:
        if len(args.cases) != len(set(args.cases)) or not set(args.cases) <= {c.case_id for c in selected}:
            parser.error('unique development case IDs required; reserved candidates are not executable by this entry')
        selected = [c for c in selected if c.case_id in args.cases]
    private = root / '.private/s00e'
    if args.command == 'validate':
        print(json.dumps({'suite': manifest['version'], 'cases': len(cases),
            'splits': manifest['splits'],
            'families': {f: sum(c.family_id == f for c in cases) for f in manifest['families']},
            'protocol_checks': len(baseline['protocol_checks']),
            'provider': baseline['provider'], 'human_review': 'pending',
            'semantic_scoring_eligible': False, 'model_http_calls': 0}, ensure_ascii=False))
        return 0
    if args.command == 'review-export':
        out = private / 'reviews' / uuid4().hex
        out.mkdir(parents=True, exist_ok=False)
        rows = []
        sections = ['# 整场面试评测人工审核包', '',
                    '本包由程序导出，所有状态 pending。请逐条审核公共输入、候选人规则、预期动作集合与 rubric；'
                    '填写 reviewer/decision/notes。程序不能证明填写者是人，记录仅为 operator_attested。',
                    '审核样本正确性不等于已审核模型输出；不要把本包提交为真实用户或语义质量证据。', '']
        for case in selected:
            oracle = oracles[case.case_id]
            row = Review(task_id=case.case_id, suite_version=manifest['version'],
                         task_sha256=item_hash(case.model_dump(mode='json')),
                         oracle_sha256=item_hash(oracle.model_dump(mode='json')))
            rows.append(row.model_dump(mode='json'))
            sections.extend([f'## {case.case_id} / {case.split} / {case.family_id}', '',
                             '```json',
                             json.dumps({'case': case.model_dump(mode='json'),
                                         'oracle': oracle.model_dump(mode='json')},
                                        ensure_ascii=False, indent=2),
                             '```', '',
                             '检查：预期动作集合是否允许合理替代；候选人规则是否只匹配本场景；'
                             'rubric 是否能由人工独立判定；是否泄漏隐藏经历或分数。', ''])
        (out / 'reviews.jsonl').write_text(
            '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8')
        (out / 'review-guide.md').write_text('\n'.join(sections) + '\n', encoding='utf-8')
        print(json.dumps({'output': str(out), 'exported': len(rows), 'approved': 0,
                          'human_identity_verified': False}, ensure_ascii=False))
        return 0
    if args.command == 'review-validate':
        if not args.review_file:
            parser.error('--review-file required')
        rows = [Review.model_validate_json(line) for line in
                args.review_file.read_text(encoding='utf-8').splitlines() if line.strip()]
        if len({r.task_id for r in rows}) != len(rows):
            raise ValueError('duplicate_review')
        by_id = cases_by_id(selected)
        for row in rows:
            if row.task_id not in by_id or row.suite_version != manifest['version']:
                raise ValueError('review_scope_mismatch')
            if row.task_sha256 != item_hash(by_id[row.task_id].model_dump(mode='json')) \
                    or row.oracle_sha256 != item_hash(oracles[row.task_id].model_dump(mode='json')):
                raise ValueError('review_content_changed')
        print(json.dumps({'selected_total': len(selected), 'reviewed_items': len(rows),
            'missing_items': len(selected) - len(rows),
            'decisions': {d: sum(r.decision == d for r in rows) for d in ('pending', 'approve', 'reject')},
            'human_identity_verified': False, 'source': 'operator_attested',
            'semantic_pass_rate': None}, ensure_ascii=False))
        return 0
    if not args.worker_config or not args.owner_config:
        parser.error('run requires explicit test worker/owner configs')
    from interview_agent.storage.config import DatabaseSettings
    from interview_agent.worker.config import WorkerSettings
    settings = WorkerSettings.load(args.worker_config)
    owner = DatabaseSettings.model_validate_json(args.owner_config.read_text(encoding='utf-8'))
    if settings.mode != 'test' or owner.dbname != 'py_interview_test' \
            or (owner.host, owner.port) != (settings.database.host, settings.database.port):
        parser.error('matching test environment required')
    run_dir = args.run_dir or private / 'benchmark' / uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    ids = [c.case_id for c in selected]
    meta = {'suite': manifest, 'baseline': baseline, 'selected_ids': ids,
            'adapters': ['system_scripted'], 'repeats': 1, 'provenance': provenance(root),
            'reserved_executed': False, 'human_review': 'pending', 'provider': 'scripted_protocol',
            'ledger_before': arun(ledger(owner))}
    write(run_dir / 'run.json', meta)
    records = []
    for case in selected:
        result = execute_session(case, settings, owner, root)
        attempt = SessionAttempt(case_id=case.case_id, **result)
        row = {'attempt': attempt.model_dump(mode='json'),
               'grade': grade_session(attempt, oracles[case.case_id], case)}
        records.append(row)
        with (run_dir / 'attempts.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        print(json.dumps({'case_id': case.case_id, 'status': attempt.status,
                          'machine_pass': row['grade']['machine_pass']}, ensure_ascii=False), flush=True)
    after = arun(ledger(owner))
    report = summarize_session(records, ids, oracles, baseline)
    report['ledger_unchanged'] = meta['ledger_before'] == after
    write(run_dir / 'report.json', report)
    print(json.dumps({'output': str(run_dir), **report}, ensure_ascii=False))
    blocked = (report['metrics']['infrastructure_failures'] or report['metrics']['evaluation_incomplete']
               or not report['ledger_unchanged'])
    return 1 if blocked else 0


if __name__ == '__main__':
    raise SystemExit(main())
