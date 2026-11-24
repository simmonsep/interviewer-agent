import json
from uuid import uuid4
import pytest
from interview_agent.materials.cli import ResumeClient,parser
from test_postgres import arun,pg_settings
from test_worker import worker_settings
from test_career_research import goal,process

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


def commands(c,g,tmp_path):
    state=tmp_path/'research-session.json';cli=ResumeClient(c,state);cli.saved['goal_id']=g['id'];cli.save()
    def run(*args):return ResumeClient(c,state).execute(parser().parse_args(['--state',str(state),*map(str,args)]))
    return run


def choose(run,item,file,ack=False):
    run('job-choice-file','--run-id',item['run_id'],'--snapshot-id',item['snapshot_id'],'--output',file)
    d=json.loads(file.read_text());d['command']['acknowledge_unknown']=ack;file.write_text(json.dumps(d),encoding='utf-8')


def test_search_select_preserves_conflicts_unknown_ack_and_downstream_target(goal,worker_settings,tmp_path):
    c,g=goal;run=commands(c,g,tmp_path)
    run('research','--kind','search','--keyword','Java');arun(process(worker_settings));v=run('jobs')
    assert v['sources'] and v['runs'][0]['result']['origin']=='offline_fixture'
    conflict=next(i for i in v['items'] if i['city_check']=='conflict');bad=tmp_path/'conflict.json';choose(run,conflict,bad,True)
    with pytest.raises(ValueError,match='hard_requirement_conflict'):run('job-select',bad)
    match=next(i for i in v['items'] if i['city_check']=='match');file=tmp_path/'choice.json';choose(run,match,file)
    with pytest.raises(ValueError,match='unknown_acknowledgement_required'):run('job-select',file)
    d=json.loads(file.read_text());d['command']['acknowledge_unknown']=True;file.write_text(json.dumps(d),encoding='utf-8')
    assert run('job-select',file)['selected']['snapshot_id']==match['snapshot_id']
    cli=ResumeClient(c,tmp_path/'research-session.json')
    assert cli.materials()['target']['kind']=='selected_job'
    s=run('interview-start','--role','java-backend');assert s['target']['content_sha256']==match['content_sha256'];run('interview-stop')


def test_saved_jd_import_no_external_http_and_stale_selection_is_rejected(goal,worker_settings,tmp_path):
    c,g=goal;run=commands(c,g,tmp_path)
    def forbidden(request):pytest.fail('import_jd must not fetch external sources')
    run('research','--kind','import_jd');arun(process(worker_settings,forbidden));v=run('jobs');item=v['items'][0]
    assert item['source_id']=='user-jd' and v['runs'][0]['result']['origin']=='user_provided'
    file=tmp_path/'old-choice.json';choose(run,item,file,True)
    body=json.loads(file.read_text());body['goal_id']=str(uuid4());other=tmp_path/'wrong.json';other.write_text(json.dumps(body),encoding='utf-8')
    with pytest.raises(ValueError,match='不属于'):run('job-select',other)
    c.post(f"/api/career-goals/{g['id']}/messages",json={'expected_version':1,'message':'改为北京Java','conditions':{'direction':'Java','cities':['北京'],'city_policy':'required'}},headers={'Idempotency-Key':uuid4().hex})
    with pytest.raises(ValueError,match='version_conflict'):run('job-select',file)
    with pytest.raises(ValueError,match='unsupported_search'):run('research','--kind','search','--source','xiaomi-intern','--keyword','Java')
