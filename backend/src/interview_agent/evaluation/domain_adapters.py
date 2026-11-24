"""Free domain episodes through auth API, SQL, Redis and production role Workers.

Only public input enters this module. Fixture source HTTP and scripted providers
exercise protocols; they are not claims about real sites or LLM competence.
"""
import json
import secrets
import time
from pathlib import Path
from uuid import uuid4
import httpx
from fastapi.testclient import TestClient
from psycopg import OperationalError
from interview_agent.api.app import create_app
from interview_agent.api.__main__ import selector_loop
from interview_agent.contracts.operations import QueueMessage
from .adapters import arun, resources, cleanup, pump


async def role_pump(settings, gid, role, root, public, profile):
    async with resources(settings) as (pool, queue):
        if role == 'research':
            from interview_agent.career.research_repository import ResearchRepository
            from interview_agent.career.research_worker import worker_for
            fixture = json.loads((root / 'fixtures/career-research/http.json').read_text(encoding='utf-8'))
            for detail in fixture['details'].values():
                detail['data']['requirement'] = public.requirements

            def replay(request):
                if public.source_behavior == 'unavailable':
                    return httpx.Response(429, json={'error': 'synthetic rate limit'})
                if request.method == 'POST':
                    return httpx.Response(200, json=fixture['list'])
                identity = request.url.params.get('id')
                if public.source_behavior == 'partial' and identity == '81002':
                    return httpx.Response(404, json={'error': 'synthetic missing detail'})
                return httpx.Response(200, json=fixture['details'][identity])

            repo = ResearchRepository(pool)
            worker = worker_for(repo, queue, provider='scripted', origin='offline_fixture',
                                inner_factory=lambda: httpx.MockTransport(replay))
            table = 'career_research_runs'
        elif role == 'materials':
            from interview_agent.materials.repository import MaterialRepository
            from interview_agent.materials.worker import worker_for
            repo = MaterialRepository(pool)
            worker = worker_for(repo, queue, provider='scripted')
            table = 'career_material_runs'
        elif role == 'coding':
            from interview_agent.coding.repository import CodingRepository
            from interview_agent.coding.worker import worker_for
            from interview_agent.execution.java_workspace import JavaWorkspaceExecutor
            repo = CodingRepository(pool)
            solutions = json.loads((root / 'fixtures/ja07/scripted-solutions.json').read_text(encoding='utf-8'))
            worker = worker_for(repo, queue, JavaWorkspaceExecutor(profile), provider='scripted', solutions=solutions)
            table = 'career_coding_runs'
        elif role == 'written':
            from interview_agent.written.repository import WrittenRepository
            from interview_agent.written.worker import worker_for
            from interview_agent.execution.docker_java import DockerJava
            repo = WrittenRepository(pool)
            worker = worker_for(repo, queue, DockerJava(profile))
            table = 'career_written_submissions'
        else:
            raise ValueError('unsupported_domain_role')
        # Table is selected above, never supplied by a task. Only notify this episode.
        async with pool.connection() as conn:
            row = await (await conn.execute(
                f'SELECT id,job_id,goal_version FROM app.{table} WHERE goal_id=%s ORDER BY created_at DESC LIMIT 1', (gid,))).fetchone()
        if row is None:
            raise RuntimeError('domain_owned_task_missing')
        await queue.publish(QueueMessage(operation_id=str(row['id']), job_id=str(row['job_id']), resource_version=row['goal_version']))
        if not await worker.once():
            raise RuntimeError('domain_owned_task_not_received')


def execute_domain(public, settings, owner, root, profile=None):
    if settings.mode != 'test' or settings.database.dbname != 'py_interview_test' or owner.dbname != 'py_interview_test':
        raise ValueError('evaluation_test_environment_required')
    if (owner.host, owner.port) != (settings.database.host, settings.database.port):
        raise ValueError('evaluation_database_mismatch')
    if public.workflow in ('written', 'coding') and profile is None:
        raise ValueError('evaluation_executor_profile_required')
    if profile:
        settings = settings.model_copy(update={'written_execution_image': profile.image,
            'coding_execution_image': profile.image, 'coding_assistant_mode': 'scripted'})
    uid = None
    started = time.perf_counter()
    view = {}
    status = 'completed'
    error = None
    observed = {}
    guards = {}
    steps = 0
    requests = []
    artifacts = {}
    try:
        with TestClient(create_app(settings), base_url=settings.origin, raise_server_exceptions=False,
                        backend_options={'loop_factory': selector_loop}) as client:
            client.headers['Origin'] = settings.origin

            def request(path, body=None):
                response = client.get(path) if body is None else client.post(path, json=body, headers={'Idempotency-Key': uuid4().hex})
                requests.append({'method': 'GET' if body is None else 'POST', 'path': path, 'status': response.status_code})
                if response.status_code >= 400:
                    raise RuntimeError('domain_http_' + str(response.status_code) + '_' + str(response.json().get('code', 'unknown')))
                return response.json()

            user = request('/api/auth/sign-up/email', {'email': 'domain-' + uuid4().hex + '@example.com',
                'name': 'Synthetic domain episode', 'password': secrets.token_urlsafe(24)})
            uid = user['user']['id']
            goal = request('/api/career-goals/', {'message': public.message, 'conditions': public.conditions.model_dump(), 'jd_text': public.jd_text})
            gid = goal['id']
            base = '/api/career-goals/' + gid

            def work(role):
                arun(role_pump(settings, gid, role, root, public, profile))

            if public.workflow == 'research':
                # auto_research is an explicit user condition; coordinator delegates to research Agent.
                arun(pump(settings, gid, 'career'))
                artifacts['coordinator'] = request(base)
                work('research')
                view = request(base + '/jobs')
                run = view['runs'][0]
                result = run['result'] or {}
                by_snapshot = {x['snapshot_id']: x for x in view['items']}
                observed = {'status': run['status'], 'readable': len(view['items']),
                    'included_ids': sorted(x['source_job_id'] for x in view['items'] if x['included']),
                    'availability': result.get('availability'), 'failure_count': len(result.get('failures', []))}
                for match in result.get('matches', []):
                    job = by_snapshot[match['snapshot_id']]['source_job_id']
                    observed[job + '.outcome'] = match['outcome']
                    for check in match['checks']:
                        observed[job + '.' + check['field']] = check['status']
                guards = {'source_origin_explicit': result.get('origin') == 'offline_fixture',
                    'availability_not_invented': result.get('availability') == 'unknown',
                    'agent_delegated': run['mode'] == 'agent' and bool(run['parent_step_id']),
                    'source_citations_present': all(x.get('source_url') and x.get('response_sha256') for x in view['items'])}
                steps = run['model_requests'] + artifacts['coordinator']['model_requests']
            elif public.workflow == 'materials':
                path = base + '/materials'
                view = request(path)
                if public.source_text:
                    view = request(path + '/sources', {'expected_version': 1, 'expected_revision': view['material_revision'],
                        'name': 'synthetic-project.md', 'format': 'markdown', 'text': public.source_text})
                view = request(path + '/runs', {'expected_version': 1, 'expected_revision': view['material_revision'], 'mode': 'analyze'})
                analysis_id = view['run_id']
                work('materials')
                view = request(path)
                artifacts['analysis'] = view
                if public.confirm_count:
                    view = request(path + '/confirmations', {'expected_version': 1, 'expected_revision': view['material_revision'],
                        'run_id': analysis_id, 'proposal_ids': list(range(public.confirm_count))})
                view = request(path + '/runs', {'expected_version': 1, 'expected_revision': view['material_revision'], 'mode': 'draft'})
                work('materials')
                view = request(path)
                if public.append_source:
                    view = request(path + '/sources', {'expected_version': 1, 'expected_revision': view['material_revision'],
                        'name': 'supplement.md', 'format': 'markdown', 'text': '新增声明：后续补充测试。'})
                facts = view['facts']
                drafts = view['drafts']
                draft = drafts[0] if drafts else None
                observed = {'facts': len(facts), 'drafts': len(drafts), 'draft_stale': draft['stale'] if draft else None,
                    'last_action': view['runs'][0]['result']['action'], 'target_kind': view['target']['kind']}
                used = (draft or {}).get('payload', {}).get('facts', [])
                guards = {'quotes_grounded': all(f['quote'] == next(s for s in view['sources'] if s['id'] == f['source_id'])['text'][f['start_char']:f['end_char']] for f in facts),
                    'draft_only_confirmed': all(f['id'] in {x['id'] for x in facts} for f in used),
                    'confirmation_not_verification': all(f['verification'] == 'user_confirmed' for f in facts)}
                steps = view['material_model_requests']
            elif public.workflow == 'interview':
                view = request(base + '/interviews', {'expected_version': 1, 'role': public.role, 'mode': public.mode, 'max_questions': 2})
                sid = view['id']
                path = base + '/interviews/' + sid

                def turn():
                    nonlocal steps
                    arun(pump(settings, gid, 'interview', sid=sid))
                    current = request(path)
                    steps += current['run']['model_requests']
                    return current

                view = turn()
                if public.stop:
                    view = request(path + '/stop', {'expected_version': view['version']})
                else:
                    for answer in public.answers:
                        question = next(e for e in reversed(view['events']) if e['kind'] == 'question')
                        request(path + '/answers', {'expected_version': view['version'], 'question_id': question['id'], 'text': answer})
                        view = turn()
                report = view['report'] or {}
                answers = [e for e in view['events'] if e['kind'] == 'answer']
                observed = {k: report.get(k) for k in ('questions', 'answers', 'assisted_answers', 'semantic_scoring_eligible')}
                observed.update(status=view['status'], target_kind=view['target']['kind'],
                    labels=[o['label'] for o in view['observations']])
                guards = {'answers_preserved': [e['payload']['text'] for e in answers] == public.answers,
                    'no_ability_certification': report.get('semantic_scoring_eligible') is False,
                    'observations_cite_answers': all(o['answer_id'] in {e['id'] for e in answers} for o in view['observations'])}
            elif public.workflow == 'coding':
                view = request(base + '/coding', {'expected_version': 1, 'task_id': public.task_id, 'mode': 'practice'})
                path = base + '/coding/' + view['id']
                original = view['workspace']

                def binding():
                    return {'expected_version': view['version'], 'expected_workspace_sha256': view['workspace']['sha256']}

                request(path + '/public-tests', binding())
                work('coding')
                view = request(path)
                artifacts['baseline'] = view
                proposal_unchanged = True
                if public.decision != 'none':
                    request(path + '/assist', binding())
                    work('coding')
                    view = request(path)
                    artifacts['proposal'] = view
                    proposal_unchanged = view['workspace'] == original
                    proposal = view['runs'][-1]
                    steps = proposal['assistant_responses']
                    view = request(path + '/decisions', {**binding(), 'run_id': proposal['id'], 'decision': public.decision,
                        'explanation': '合成用户已审阅，明确' + public.decision})
                view = request(path + '/submit', {**binding(), 'explanation': '合成用户提交当前版本，检查边界行为。'})['session']
                work('coding')
                view = request(path)
                final = view['runs'][-1]['result'] or {}
                observed = {'status': view['status'], 'verdict': (final.get('grade') or {}).get('verdict'),
                    'version_origins': [v['origin'] for v in view['versions']],
                    'baseline_verdict': artifacts['baseline']['runs'][-1]['result']['grade']['verdict']}
                guards = {'proposal_did_not_mutate_code': proposal_unchanged,
                    'user_decision_respected': (view['workspace'] != original) if public.decision == 'accept' else view['workspace'] == original,
                    'no_ability_certification': view['ability_certified'] is False,
                    'actual_executor_result': bool(final.get('grade', {}).get('cases'))}
            else:
                view = request(base + '/written', {'expected_version': 1, 'task_id': public.task_id})
                path = base + '/written/' + view['id']
                view = request(path + '/submissions', {'expected_version': view['version'], 'source': public.source_code})['session']
                work('written')
                view = request(path)
                view = request(path + '/finish', {'expected_version': view['version']})
                result = view['submissions'][-1]['result'] or {}
                observed = {'status': view['status'], 'verdict': result.get('verdict')}
                guards = {'source_preserved': view['submissions'][-1]['source'] == public.source_code,
                    'no_ability_certification': view['report']['ability_certified'] is False,
                    'actual_executor_result': view['submissions'][-1]['status'] == 'SUCCEEDED' and bool(result)}
            artifacts['final'] = view
            if observed.get('verdict') == 'ENVIRONMENT_ERROR':
                status = 'infrastructure_failure'
                error = 'domain_executor_environment_error'
    except OperationalError:
        status = 'infrastructure_failure'
        error = 'domain_database_unavailable'
    except (RuntimeError, KeyError, ValueError) as exc:
        status = 'product_failure'
        error = str(exc)[:180]
        artifacts['last_view'] = view
    finally:
        if uid:
            arun(cleanup(owner, uid))
    return {'status': status, 'error_code': error, 'provider_steps': steps,
        'decision_elapsed_ms': (time.perf_counter() - started) * 1000,
        'output': {'sql_committed': status == 'completed', 'observed': observed, 'guards': guards,
            'decision_type': public.workflow, 'action_text': json.dumps(observed, ensure_ascii=False),
            'artifacts': artifacts, 'api_requests': requests, 'provider': 'scripted_protocol',
            'source_transport': 'synthetic_http_replay' if public.workflow == 'research' else None}}
