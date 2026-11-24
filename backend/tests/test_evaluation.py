import json,shutil
from pathlib import Path
import pytest
from pydantic import ValidationError
from interview_agent.evaluation.contracts import Attempt,Oracle,PublicInput
from interview_agent.evaluation.suite import load_suite,sha
from interview_agent.evaluation.grading import grade,summarize
from test_postgres import pg_settings
from test_worker import worker_settings

def suite(root):return root/'benchmarks/career-pilot-v0.1'
def copied(project_root,tmp_path):
    out=tmp_path/'suite';shutil.copytree(suite(project_root),out);return out
def rehash(root):
    m=json.loads((root/'manifest.json').read_text(encoding='utf-8'));m['sha256']={p:sha(root/p) for p in m['sha256']}
    (root/'manifest.json').write_text(json.dumps(m),encoding='utf-8')
def oracle(tid='G01'):return Oracle(task_id=tid,decision_types=['ask_user'],human_rubric=['需人工核对追问质量'])
def attempt(tid='G01',adapter='system_scripted',**kw):
    return Attempt(task_id=tid,adapter=adapter,repeat=1,status='completed',output={
        'sql_committed':True,'decision_type':'ask_user','owned_application':True,'application_state_unchanged':True,
        'retracted_evidence_excluded':True,'tool_scope_respected':True},provider_steps=2,**kw)
def record(a):return {'attempt':a.model_dump(),'grade':grade(a,oracle(a.task_id))}

def test_suite_checksums_pairs_and_candidate_partition(project_root):
    m,tasks,o=load_suite(suite(project_root))
    assert len(tasks)==len(o)==18 and m['splits']=={'development':12,'reserved_candidate':6}
    assert all(t.review=='pending_human_review' for t in tasks)
    assert {t.family for t in tasks if t.split=='development'}.isdisjoint(t.family for t in tasks if t.split=='reserved_candidate')

@pytest.mark.parametrize('file',['tasks.jsonl','oracles.jsonl','baseline-spec.json'])
def test_changed_frozen_assets_are_rejected(project_root,tmp_path,file):
    out=copied(project_root,tmp_path);p=out/file;p.write_bytes(p.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='checksum'):load_suite(out)

def test_family_cross_split_and_duplicate_task_rejected(project_root,tmp_path):
    out=copied(project_root,tmp_path);p=out/'tasks.jsonl';rows=[json.loads(x) for x in p.read_text(encoding='utf-8').splitlines()]
    rows[-1]['family']=rows[0]['family'];p.write_text('\n'.join(json.dumps(x) for x in rows),encoding='utf-8');rehash(out)
    with pytest.raises(ValueError,match='family_crosses_split'):load_suite(out)
    rows[-1]=rows[0];p.write_text('\n'.join(json.dumps(x) for x in rows),encoding='utf-8');rehash(out)
    with pytest.raises(ValueError,match='duplicate'):load_suite(out)

def test_public_producer_input_cannot_accept_goldens():
    with pytest.raises(ValidationError):PublicInput.model_validate({'message':'准备面试','expected_action':'ask_user'})

@pytest.mark.parametrize('flag',['owned_application','application_state_unchanged','retracted_evidence_excluded','tool_scope_respected'])
def test_high_outcome_score_cannot_hide_severe_guard_failure(flag):
    a=attempt();a.output[flag]=False;g=grade(a,oracle())
    assert g['machine_pass'] is False and g['severe_violations'] and g['domain_success'] is None

def test_missing_guard_is_not_a_pass_and_human_review_stays_pending():
    a=attempt();del a.output['retracted_evidence_excluded']
    assert not grade(a,oracle())['machine_pass']
    g=grade(attempt(),oracle());assert g['machine_pass'] and g['human_review']=='pending' and g['domain_success'] is None

def test_wrong_business_outcome_is_measured_as_failure():
    a=attempt();a.output['decision_type']='prepare_job_research'
    assert grade(a,oracle())['checks']['completed'] and not grade(a,oracle())['machine_pass']
    with pytest.raises(ValueError,match='oracle_task'):grade(a,oracle('G02'))

def test_infra_and_product_failures_have_explicit_denominators():
    a=attempt();b=attempt('G02').model_copy(update={'status':'product_failure'});c=attempt('G03').model_copy(update={'status':'infrastructure_failure'})
    out=summarize([record(x) for x in (a,b,c)],['G01','G02','G03'],['system_scripted'],1)['metrics']['system_scripted']
    assert out['valid_attempts']==2 and out['infrastructure_failures']==1 and out['product_failures']==1
    assert out['machine_pass_rate_valid']==.5 and out['machine_pass_rate_all_attempts']==1/3 and out['domain_success_rate'] is None

def test_pairing_requires_complete_unique_attempt_matrix():
    a=record(attempt());b=record(attempt(adapter='fixed_rules'))
    out=summarize([b,a],['G01'],['system_scripted','fixed_rules'],1)
    assert len(out['paired'])==1 and out['independent_task_count']==1 and out['domain_success_rate'] is None
    for records in ([a],[a,b,a]):
        with pytest.raises(ValueError,match='attempt_matrix'):summarize(records,['G01'],['system_scripted','fixed_rules'],1)

def test_reserved_partition_requires_explicit_execution_flag(project_root):
    from interview_agent.evaluation.__main__ import main
    with pytest.raises(SystemExit) as e:main(['run','--root',str(project_root),'--split','reserved_candidate'])
    assert e.value.code==2

@pytest.mark.postgres
@pytest.mark.redis
def test_real_asgi_workers_measure_stop_failure_and_fixed_rules_without_leaking_labels(pg_settings,worker_settings,project_root):
    from interview_agent.evaluation.adapters import execute_input
    from test_postgres import query
    _,tasks,oracles=load_suite(suite(project_root));t=next(t for t in tasks if t.id=='G04')
    before=query(pg_settings[1],"SELECT count(*) AS n FROM auth.users WHERE email LIKE 'eval-%%'")[0]['n']
    current=Attempt(task_id=t.id,adapter='system_scripted',repeat=1,**execute_input(t.input,'system_scripted',worker_settings,pg_settings[1]))
    fixed=Attempt(task_id=t.id,adapter='fixed_rules',repeat=1,**execute_input(t.input,'fixed_rules',worker_settings,pg_settings[1]))
    assert current.status==fixed.status=='completed' # SQL success need not be task success.
    assert grade(current,oracles[t.id])['machine_pass'] is False and grade(fixed,oracles[t.id])['machine_pass'] is True
    assert query(pg_settings[1],"SELECT count(*) AS n FROM auth.users WHERE email LIKE 'eval-%%'")[0]['n']==before

@pytest.mark.postgres
@pytest.mark.redis
def test_real_retraction_episode_uses_production_sql_memory_filter(pg_settings,worker_settings,project_root):
    from interview_agent.evaluation.adapters import execute_input
    _,tasks,oracles=load_suite(suite(project_root));t=next(t for t in tasks if t.id=='M02')
    a=Attempt(task_id=t.id,adapter='system_scripted',repeat=1,**execute_input(t.input,'system_scripted',worker_settings,pg_settings[1]))
    assert a.status=='completed',a.error_code
    assert a.output['action_kind']=='wait_for_update' and not a.output['training_evidence_selected']
    assert a.output['retracted_evidence_excluded'] and grade(a,oracles[t.id])['machine_pass']
    assert a.setup_provider_steps==9 and a.provider_steps==3 and a.model_http_calls==0
