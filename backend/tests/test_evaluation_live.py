import asyncio,json
from pathlib import Path
from uuid import uuid4
import httpx,pytest
from interview_agent.evaluation.billing import EvaluationPolicy
from interview_agent.evaluation.contracts import Attempt,PublicInput
from interview_agent.evaluation.grading import grade,summarize
from interview_agent.evaluation.reviews import export_reviews,validate_reviews
from interview_agent.evaluation.suite import load_suite
from interview_agent.career.coordinator import coordinate
from interview_agent.runtime.scripted import ScriptedProvider
from interview_agent.runtime.types import AssistantResponse,ToolCall
from test_application_coordinator import unit_context,choice
from test_career_coordinator import context,call
from test_evaluation import oracle,record,attempt
from test_postgres import arun,pg_settings,query
from test_worker import worker_settings

def test_fixed_prefetch_reuses_gate_and_finishes_in_one_model_response():
    c,v=unit_context();seen=[]
    async def read(aid):seen.append(aid);return v
    provider=ScriptedProvider([call('prepare_application_action',{'application':choice(v)})])
    d,trace=asyncio.run(coordinate(c,provider,asyncio.Event(),read,prefetch=True))
    assert d.application.application_id==v['application_id'] and seen==[v['application_id']]
    assert len(provider.requests)==1 and [t['tool'] for t in trace]==['read_goal','read_application','prepare_application_action']
    assert provider.requests[0][-1].role=='tool'

def test_fixed_reads_share_cancel_scope():
    async def scenario():
        c,v=unit_context();started=asyncio.Event();cleaned=asyncio.Event();stop=asyncio.Event()
        async def read(aid):
            started.set()
            try:await asyncio.Event().wait()
            finally:cleaned.set()
        task=asyncio.create_task(coordinate(c,ScriptedProvider([]),stop,read,prefetch=True))
        await asyncio.wait_for(started.wait(),2);stop.set()
        with pytest.raises(ValueError,match='cancelled'):await task
        assert cleaned.is_set()
    asyncio.run(scenario())

def test_review_export_is_pending_content_bound_and_operator_attested(project_root,tmp_path):
    m,t,o=load_suite(project_root/'benchmarks/career-pilot-v0.2');selected=[x for x in t if x.split=='development']
    path=tmp_path/'reviews';r=export_reviews(path,m,selected,o)
    assert r['exported']==24 and r['approved']==0 and not r['human_identity_verified']
    summary=validate_reviews(path/'reviews.jsonl',m,selected,o);assert summary['decisions']=={'pending':24,'approve':0,'reject':0}
    subset=validate_reviews(path/'reviews.jsonl',m,t,o,selected_ids=['G03','G04','A02','M03'])
    assert subset['decisions']['pending']==4 and subset['outside_selected_items']==20 and subset['missing_items']==0
    rows=[json.loads(x) for x in (path/'reviews.jsonl').read_text(encoding='utf-8').splitlines()]
    rows[0].update(decision='approve',reviewer='Synthetic operator',notes='Test attestation, not actual human review')
    (path/'reviews.jsonl').write_text('\n'.join(json.dumps(x) for x in rows),encoding='utf-8')
    summary=validate_reviews(path/'reviews.jsonl',m,selected,o);assert summary['decisions']['approve']==1 and not summary['human_identity_verified']
    rows[0]['task_sha256']='0'*64;(path/'reviews.jsonl').write_text('\n'.join(json.dumps(x) for x in rows),encoding='utf-8')
    with pytest.raises(ValueError,match='content_changed'):validate_reviews(path/'reviews.jsonl',m,selected,o)

def test_v02_extends_without_changing_v01_content(project_root):
    m,t,o=load_suite(project_root/'benchmarks/career-pilot-v0.2');oldm,old,oldo=load_suite(project_root/'benchmarks/career-pilot-v0.1')
    assert len(t)==30 and m['splits']=={'development':24,'reserved_candidate':6}
    for previous in old:assert next(x for x in t if x.id==previous.id)==previous
    for key in oldo:assert o[key]==oldo[key]

def test_unknown_cost_and_stopped_rows_do_not_become_zero_or_model_failure():
    a=attempt().model_copy(update={'adapter':'system_live','status':'authorization_stopped','model_http_calls':None,'model_cost_upper_nano':None,'model_usage_status':'unknown'})
    r=record(a);assert r['grade']['machine_pass'] is None
    report=summarize([r],['G01'],['system_live'],1)['metrics']['system_live']
    assert report['valid_attempts']==0 and report['authorization_stopped']==1 and report['product_failures']==0
    assert report['model_http_calls'] is None and report['model_cost_upper_nano'] is None and report['model_cost_cny'] is None

def test_live_cli_stops_remaining_matrix_after_uncertain_usage(project_root,tmp_path,monkeypatch):
    from types import SimpleNamespace
    from interview_agent.evaluation.__main__ import main
    import interview_agent.evaluation.__main__ as cli
    import interview_agent.evaluation.adapters as adapters
    import interview_agent.evaluation.billing as billing
    from interview_agent.worker.config import WorkerSettings
    owner=tmp_path/'owner.json';owner.write_text(json.dumps({'host':'127.0.0.1','port':25432,'dbname':'py_interview','user':'py_interview_owner','password':'synthetic-not-real'}),encoding='utf-8')
    monkeypatch.setattr(WorkerSettings,'load',lambda p:SimpleNamespace(mode='development',database=SimpleNamespace(dbname='py_interview')))
    calls=[]
    async def authorized(*args):return {'max_nano':2000000000,'state':'ACTIVE'}
    async def ledger(*args):return []
    async def summary(*args):return {'usage_status':'unknown' if calls else 'not_called','authorization_state':'PAUSED' if calls else 'ACTIVE','http_attempts':len(calls),'accounted_upper_nano':147456000 if calls else 0}
    def execute(*args,**kwargs):
        calls.append(1);return {'status':'authorization_stopped','error_code':'model_usage_unknown','model_http_calls':1,'model_cost_cny':None,'model_cost_upper_nano':147456000,'model_usage_status':'unknown'}
    monkeypatch.setattr(billing,'check_authorization',authorized);monkeypatch.setattr(cli,'ledger',ledger)
    monkeypatch.setattr(adapters,'billing_summary',summary);monkeypatch.setattr(adapters,'execute_input',execute)
    code=main(['run-live','--root',str(tmp_path),'--suite',str(project_root/'benchmarks/career-pilot-v0.2'),
        '--worker-config','mock-not-opened','--owner-config',str(owner),'--policy',str(project_root/'configs/ja09-deepseek-flash.json'),
        '--authorization',billing.AUTHORIZATION_ID,'--credentials','mock-not-opened','--tasks','G03','G04','A02','M03'])
    assert code==1 and len(calls)==1
    folder=next((tmp_path/'.private/ja-09-02/benchmark').iterdir());report=json.loads((folder/'report.json').read_text())
    assert sum(x['not_run'] for x in report['metrics'].values())==7
    assert sum(x['authorization_stopped'] for x in report['metrics'].values())==1 and report['stop_reason']=='model_usage_unknown'

@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.parametrize('adapter,expected',[('system_live',3),('fixed_prefetch_live',1)])
def test_real_sdk_same_prompt_tools_and_budget_reservation_with_mock_provider(pg_settings,worker_settings,project_root,tmp_path,adapter,expected):
    from interview_agent.billing.ledger import authorize
    from interview_agent.evaluation.adapters import execute_input
    from test_billing import reply
    policy=EvaluationPolicy.load(project_root/'configs/ja09-deepseek-flash.json');identity='test-'+uuid4().hex
    arun(authorize(pg_settings[1],policy,identity));key=tmp_path/'mock.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n');seen=[];result=None
    public=PublicInput(message='已提交但未回复，请保留事实给出下一步。',jd_text='合成Java岗位',application={'status':'SUBMITTED'})
    async def handler(request):
        payload=json.loads(request.content);seen.append(payload)
        assert payload['model']==policy.model and payload['thinking']=={'type':'disabled'}
        from interview_agent.career.coordinator import SYSTEM_PROMPT,tools
        assert payload['messages'][0]['content']==SYSTEM_PROMPT
        assert {t['function']['name'] for t in payload['tools']}=={t.name for t in tools()}
        from psycopg import AsyncConnection
        async with await AsyncConnection.connect(pg_settings[1].conninfo()) as conn:
            assert (await (await conn.execute('SELECT count(*) FROM app.model_http_calls WHERE authorization_id=%s',(identity,))).fetchone())[0]==len(seen)
        if len(payload['messages'])==2:name,args='read_goal',{}
        else:
            view=json.loads(payload['messages'][-1]['content'])
            if 'applications' in view:name,args='read_application',{'application_id':view['applications'][0]['id']}
            else:name,args='prepare_application_action',{'application':choice(view)}
        return httpx.Response(200,json=reply(AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments=args)])))
    try:
        result=execute_input(public,adapter,worker_settings,pg_settings[1],live_options={'policy':policy,'authorization':identity,'credentials':key,'inner_factory':lambda:httpx.MockTransport(handler)})
        assert result['status']=='completed',result
        assert len(seen)==expected and result['model_http_calls']==expected and result['model_usage_status']=='known'
        assert result['output']['action_kind']=='wait_for_update' and result['output']['retained_resources']['synthetic_user_id']
    finally:
        assert pg_settings[1].dbname=='py_interview_test'
        query(pg_settings[1],'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',(identity,))
        query(pg_settings[1],'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(identity,))
        if result and result.get('output',{}).get('retained_resources'):
            query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(result['output']['retained_resources']['synthetic_user_id'],))

@pytest.mark.postgres
@pytest.mark.redis
def test_unknown_mock_http_preserves_reservation_and_pauses_without_retry(pg_settings,worker_settings,project_root,tmp_path):
    from interview_agent.billing.ledger import authorize
    from interview_agent.evaluation.adapters import execute_input
    from test_billing import reply
    policy=EvaluationPolicy.load(project_root/'configs/ja09-deepseek-flash.json');identity='test-'+uuid4().hex
    arun(authorize(pg_settings[1],policy,identity));key=tmp_path/'mock.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n');seen=[];result=None
    def handler(request):
        seen.append(1);body=reply(AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name='read_goal',arguments={})]));body.pop('usage')
        return httpx.Response(200,json=body)
    try:
        result=execute_input(PublicInput(message='方向尚不明确。'),'system_live',worker_settings,pg_settings[1],
            live_options={'policy':policy,'authorization':identity,'credentials':key,'inner_factory':lambda:httpx.MockTransport(handler)})
        assert result['status']=='authorization_stopped' and result['error_code']=='model_usage_unknown' and len(seen)==1
        assert result['model_cost_upper_nano']==policy.reservation_nano and result['model_usage_status']=='unknown'
        assert query(pg_settings[1],'SELECT state FROM app.model_authorizations WHERE id=%s',(identity,))[0]['state']=='PAUSED'
        row=query(pg_settings[1],'SELECT state,reserved_nano,accounted_nano FROM app.model_http_calls WHERE authorization_id=%s',(identity,))[0]
        assert row=={'state':'UNKNOWN','reserved_nano':policy.reservation_nano,'accounted_nano':None}
    finally:
        assert pg_settings[1].dbname=='py_interview_test'
        query(pg_settings[1],'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',(identity,))
        query(pg_settings[1],'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(identity,))
        if result and result.get('output',{}).get('retained_resources'):
            query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(result['output']['retained_resources']['synthetic_user_id'],))
