import json
import pytest
from pydantic import ValidationError
from interview_agent.evaluation.output_reviews import export_outputs,validate_outputs,OutputReview
from interview_agent.evaluation.suite import load_suite
from interview_agent.storage.config import DatabaseSettings
from interview_agent.worker.config import RedisSettings


@pytest.fixture
def saved_run(project_root,tmp_path):
    manifest,tasks,oracles=load_suite(project_root/'benchmarks/career-pilot-v0.2')
    selected=[t for t in tasks if t.id=='G03'];run=tmp_path/'run';run.mkdir()
    meta={'suite':manifest,'selected_ids':['G03'],'adapters':['system_live','fixed_prefetch_live'],'repeats':1}
    (run/'run.json').write_text(json.dumps(meta));(run/'report.json').write_text('{}')
    rows=[{'attempt':{'task_id':'G03','adapter':a,'repeat':1,'status':'completed','output':{
        'action_text':'Synthetic final answer','billing':{'private':'omit'},'retained_resources':{'synthetic_user_id':'omit'}},'model_cost_cny':None},
        'grade':{'machine_pass':True}} for a in meta['adapters']]
    (run/'attempts.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    return run,manifest,tasks,oracles,rows


def test_output_bundle_pending_then_attested_and_keeps_domain_rate_unknown(saved_run,tmp_path):
    run,m,t,o,_=saved_run;bundle=tmp_path/'bundle'
    r=export_outputs(bundle,run,m,t,o);assert r['pending']==2 and not r['human_identity_verified']
    guide=(bundle/'guide.md').read_text(encoding='utf-8')
    assert 'system_live' not in guide and 'machine_pass' not in guide and '"billing"' not in guide
    assert 'Synthetic final answer' in guide
    before=validate_outputs(bundle,run,m,t,o);assert before['decisions']['pending']==2
    rows=[json.loads(x) for x in (bundle/'reviews.jsonl').read_text().splitlines()]
    rows[0].update(verdicts=['pass']*len(rows[0]['verdicts']),reviewer='Synthetic operator',notes='Unit fixture only',evidence_refs=['output.action_text'])
    (bundle/'reviews.jsonl').write_text('\n'.join(json.dumps(x) for x in rows))
    result=validate_outputs(bundle,run,m,t,o)
    assert result['decisions']=={'pending':1,'pass':1,'fail':0,'unjudgeable':0}
    assert not result['human_identity_verified'] and result['domain_success_rate'] is None


def test_output_bundle_rejects_source_drift_and_missing_review(saved_run,tmp_path):
    run,m,t,o,rows=saved_run;bundle=tmp_path/'bundle';export_outputs(bundle,run,m,t,o)
    original=(run/'attempts.jsonl').read_bytes();rows[0]['attempt']['output']['action_text']='changed'
    (run/'attempts.jsonl').write_text('\n'.join(json.dumps(x) for x in rows))
    with pytest.raises(ValueError,match='sources_changed'):validate_outputs(bundle,run,m,t,o)
    (run/'attempts.jsonl').write_bytes(original)
    p=bundle/'reviews.jsonl';p.write_text(p.read_text().splitlines()[0])
    with pytest.raises(ValueError,match='missing_or_duplicate'):validate_outputs(bundle,run,m,t,o)


def test_completed_review_requires_all_criteria_and_references(saved_run,tmp_path):
    run,m,t,o,_=saved_run;bundle=tmp_path/'bundle';export_outputs(bundle,run,m,t,o)
    row=json.loads((bundle/'reviews.jsonl').read_text().splitlines()[0])
    row['verdicts']=['pass']*len(row['verdicts'])
    with pytest.raises(ValidationError,match='reason_and_evidence'):OutputReview.model_validate(row)
    row['verdicts']=['pending','fail']
    with pytest.raises(ValidationError,match='partial_output_review'):OutputReview.model_validate(row)


def test_loopback_services_can_use_independent_ports_without_expanding_host_scope():
    db={'host':'127.0.0.1','port':32187,'dbname':'py_interview_test','user':'py_interview_app','password':'synthetic'}
    assert DatabaseSettings.model_validate(db).port==32187
    assert RedisSettings.model_validate({'host':'127.0.0.1','port':32188,'db':1,'password':'synthetic'}).port==32188
    for port in [0,1023,65536]:
        with pytest.raises(ValidationError):DatabaseSettings.model_validate(db|{'port':port})
    with pytest.raises(ValidationError):DatabaseSettings.model_validate(db|{'host':'example.com'})


def test_output_bundle_rejects_incomplete_or_duplicate_attempt_matrix(saved_run,tmp_path):
    run,m,t,o,rows=saved_run
    (run/'attempts.jsonl').write_text(json.dumps(rows[0]))
    with pytest.raises(ValueError,match='incomplete_matrix'):export_outputs(tmp_path/'missing',run,m,t,o)
    (run/'attempts.jsonl').write_text('\n'.join(json.dumps(rows[0]) for _ in range(2)))
    with pytest.raises(ValueError,match='attempt_scope'):export_outputs(tmp_path/'duplicate',run,m,t,o)
