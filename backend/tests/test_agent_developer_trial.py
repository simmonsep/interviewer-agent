import argparse,json,runpy
from pathlib import Path
import pytest
from test_postgres import arun,pg_settings,query
from test_worker import worker_settings

def entry():return runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/agent-developer-trial.py'))

def test_trial_guards_and_budget():
    m=entry();a=argparse.Namespace(mode='live',execute_live=False,max_cny=None,credentials=None)
    with pytest.raises(ValueError,match='approval'):arun(m['run'](a))
    a.mode='scripted';a.credentials=Path('must-not-read')
    with pytest.raises(ValueError,match='credentials'):arun(m['run'](a))
    p=m['InterviewPolicy'].load(m['POLICY']);assert p.max_http_attempts==12 and p.max_operation_attempts==4 and p.reservation_nano*12<=2_000_000_000

@pytest.mark.postgres
@pytest.mark.redis
def test_same_trial_controller_custom_bank_two_questions_and_report(pg_settings,worker_settings,tmp_path,project_root):
    m=entry();out=tmp_path/'trial';a=argparse.Namespace(mode='scripted',execute_live=False,max_cny=None,credentials=None,
        api_config=project_root/'.private/postgres/test.json',worker_config=project_root/'.private/redis/test-worker.json',owner_config=project_root/'.private/postgres/test-migration.json',workspace=out,answer_file=None)
    try:
        arun(m['run'](a));topics=[]
        for n in range(2):
            s=json.loads((out/f'turn-{n}-session.json').read_text());assert s['status']=='WAITING_FOR_USER' and not s['material_fact_ids'] and not s['observations']
            q=next(e for e in reversed(s['events']) if e['kind']=='question');topics.append(q['payload']['bank_item']['item_id'])
            file=out/('answer-'+q['id']+'.json');v=json.loads(file.read_text());v['command']['text']='合成回答，仅验证试运行控制器，不代表用户作答。';file.write_text(json.dumps(v),encoding='utf-8');a.answer_file=file;arun(m['run'](a))
        s=json.loads((out/'turn-2-session.json').read_text());assert s['status']=='COMPLETED' and set(topics)=={'thread-memory','interrupt-side-effects'}
        assert s['report']['answers']==2 and json.loads((out/'fees.json').read_text())['usage_status']=='not_called'
    finally:
        if (out/'trial-user.json').exists():query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(json.loads((out/'trial-user.json').read_text())['user_id'],))
