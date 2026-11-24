import json
from uuid import uuid4
import httpx
import pytest
from interview_agent.evaluation.calibration import IntentFinding, report_calibration, validate_findings
from interview_agent.evaluation.output_reviews import export_outputs, load_outputs
from interview_agent.evaluation.reviews import export_reviews
from interview_agent.evaluation.billing import CalibrationPolicy, policy_for_stage, calibration_scope, CALIBRATION_AUTHORIZATION_ID, AUTHORIZATION_ID
from test_output_reviews import saved_run
from test_postgres import pg_settings, query, arun
from test_worker import worker_settings


def bundles(saved_run, tmp_path):
    run, manifest, tasks, oracles, _ = saved_run
    task = tmp_path / 'task-review'
    output = tmp_path / 'output-review'
    export_reviews(task, manifest, [t for t in tasks if t.id == 'G03'], oracles)
    export_outputs(output, run, manifest, tasks, oracles)
    return run, manifest, tasks, oracles, task / 'reviews.jsonl', output


def update_jsonl(path, change):
    rows = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()]
    change(rows)
    path.write_bytes((''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows)).encode())


def test_pending_and_scoped_findings_do_not_become_full_reviews(saved_run, tmp_path):
    run, m, t, o, task, output = bundles(saved_run, tmp_path)
    original = (task.read_bytes(), (output / 'reviews.jsonl').read_bytes())
    bound, _, _ = load_outputs(run, m, t, o)
    x = bound[0]
    finding = IntentFinding(task_id='G03', adapter=x['key'][1], repeat=1,
        **{k: x[k] for k in ('attempt_sha256', 'task_sha256', 'oracle_sha256')},
        expected_action_valid=True, action_incorrect=True, reviewer='Synthetic test operator',
        statement='Test-only scoped action judgment', evidence_refs=['output.action_text'])
    path = tmp_path / 'findings.jsonl'
    path.write_bytes((finding.model_dump_json() + '\n').encode())
    result = report_calibration(run, task, output, m, t, o, findings=path)
    assert result['scoped_intent_action_findings'] == 1
    assert result['full_review_comparisons'] == 0 and result['disagreement_rate_eligible'] is None
    assert result['domain_success_rate'] is None and not result['scoped_findings_are_full_reviews']
    assert original == (task.read_bytes(), (output / 'reviews.jsonl').read_bytes())
    update_jsonl(path, lambda rows: rows[0].update(attempt_sha256='0' * 64))
    with pytest.raises(ValueError, match='intent_finding_content_changed'):
        validate_findings(path, run, m, t, o)


@pytest.mark.parametrize('task_decision,eligible', [('approve', True), ('reject', False), ('pending', False)])
def test_only_approved_tasks_and_complete_output_judgments_compare(saved_run, tmp_path, task_decision, eligible):
    run, m, t, o, task, output = bundles(saved_run, tmp_path)
    update_jsonl(task, lambda rows: rows[0].update(decision=task_decision, reviewer='Synthetic operator', notes='Test fixture only'))
    def finish(rows):
        rows[0].update(verdicts=['fail'] * len(rows[0]['verdicts']), reviewer='Synthetic operator',
                       notes='Test-only disagreement', evidence_refs=['output.action_text'])
    update_jsonl(output / 'reviews.jsonl', finish)
    result = report_calibration(run, task, output, m, t, o)
    assert result['full_review_comparisons'] == int(eligible)
    assert result['comparison_counts']['machine_pass_human_fail'] == int(eligible)
    assert result['disagreement_rate_eligible'] == (1 if eligible else None)
    assert result['domain_success_rate'] is None and not result['human_identity_verified']


def test_duplicate_findings_rejected(saved_run, tmp_path):
    run, m, t, o, _, _ = bundles(saved_run, tmp_path)
    x = load_outputs(run, m, t, o)[0][0]
    row = IntentFinding(task_id='G03', adapter=x['key'][1], repeat=1,
        **{k: x[k] for k in ('attempt_sha256', 'task_sha256', 'oracle_sha256')}, expected_action_valid=True,
        action_incorrect=True, reviewer='Synthetic', statement='Test only', evidence_refs=['output.action_text'])
    p = tmp_path / 'finding.jsonl'
    p.write_bytes(((row.model_dump_json() + '\n') * 2).encode())
    with pytest.raises(ValueError, match='duplicate_intent_finding'):
        validate_findings(p, run, m, t, o)


def test_new_fee_stage_cannot_load_old_policy(project_root):
    old, old_stage = policy_for_stage(project_root / 'configs/ja09-deepseek-flash.json', AUTHORIZATION_ID)
    new, new_stage = policy_for_stage(project_root / 'configs/ja09-05-deepseek-flash.json', CALIBRATION_AUTHORIZATION_ID)
    assert old_stage == 'ja-09-02' and new_stage == 'ja-09-05'
    assert old.digest != new.digest and new.max_http_attempts == 18
    with pytest.raises(ValueError):
        policy_for_stage(project_root / 'configs/ja09-deepseek-flash.json', CALIBRATION_AUTHORIZATION_ID)
    with pytest.raises(ValueError, match='stage_authorization_required'):
        policy_for_stage(project_root / 'configs/ja09-05-deepseek-flash.json', 'previous-stage')


@pytest.mark.parametrize('ids,adapters,repeats,split', [
    (['G04', 'G09', 'G10', 'G03'], ['system_live', 'fixed_prefetch_live'], 1, 'development'),
    (['G04', 'G09', 'G10'], ['system_live'], 1, 'development'),
    (['G04', 'G09', 'G10'], ['system_live', 'fixed_prefetch_live'], 2, 'development'),
    (['G04', 'G09', 'G10'], ['system_live', 'fixed_prefetch_live'], 1, 'reserved_candidate')])
def test_calibration_paid_matrix_is_frozen(ids, adapters, repeats, split):
    with pytest.raises(ValueError, match='frozen_six_attempt_matrix'):
        calibration_scope(ids, adapters, repeats, split)


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.parametrize('adapter,expected', [('system_live', 2), ('fixed_prefetch_live', 1)])
def test_calibration_policy_real_sdk_reserves_before_mock_http(pg_settings, worker_settings, project_root, tmp_path, adapter, expected):
    from interview_agent.billing.ledger import authorize
    from interview_agent.evaluation.adapters import execute_input
    from interview_agent.evaluation.contracts import PublicInput
    from interview_agent.runtime.types import AssistantResponse, ToolCall
    from test_billing import reply
    policy = CalibrationPolicy.load(project_root / 'configs/ja09-05-deepseek-flash.json')
    identity = 'test-calibration-' + uuid4().hex
    arun(authorize(pg_settings[1], policy, identity))
    key = tmp_path / 'mock.env'
    key.write_text('DEEPSEEK_API_KEY=offline-synthetic\n', encoding='utf-8')
    seen = []
    result = None
    async def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        from psycopg import AsyncConnection
        async with await AsyncConnection.connect(pg_settings[1].conninfo()) as conn:
            count = (await (await conn.execute('SELECT count(*) FROM app.model_http_calls WHERE authorization_id=%s', (identity,))).fetchone())[0]
        assert count == len(seen)
        name, args = ('read_goal', {}) if len(payload['messages']) == 2 else ('finish', {'text': '按用户要求结束。'})
        return httpx.Response(200, json=reply(AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex, name=name, arguments=args)])))
    try:
        result = execute_input(PublicInput(message='结束本轮，不再研究。'), adapter, worker_settings, pg_settings[1],
            live_options={'policy': policy, 'authorization': identity, 'credentials': key,
                          'inner_factory': lambda: httpx.MockTransport(handler)})
        assert result['status'] == 'completed' and result['output']['decision_type'] == 'finish'
        assert result['model_http_calls'] == expected and len(seen) == expected and result['model_usage_status'] == 'known'
    finally:
        query(pg_settings[1], 'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id', (identity,))
        query(pg_settings[1], 'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id', (identity,))
        if result and result.get('output', {}).get('retained_resources'):
            query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (result['output']['retained_resources']['synthetic_user_id'],))
