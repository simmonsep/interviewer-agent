from uuid import uuid4
import json
import httpx,pytest
from interview_agent.materials.agent import ScriptedMaterials
import interview_agent.materials.worker as worker_module
from interview_agent.materials.billing import MaterialPolicy
from interview_agent.billing.ledger import authorize
from test_postgres import arun,pg_settings,query,client_for,signup
from test_worker import worker_settings
from test_material_repository import material_context,source,view,post,process,start
from test_billing import reply
from test_interview_repository import cleanup
from test_resume_rewrite import POLISHED,SUGGESTION

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


def begin(c,g,revision=1,key=None):return post(c,f"/api/career-goals/{g['id']}/materials/rewrite",{'expected_version':1,'expected_revision':revision},key)


class Rewriter(ScriptedMaterials):
    async def complete(self,messages,specs):
        v=await super().complete(messages,specs)
        if v.tool_calls[0].name=='submit_rewrite':
            v.tool_calls[0].arguments={'items':[{'text':POLISHED,'source_ids':[self.scope['sources'][0]['id']],'change_note':'优化技术/行动表达。'}],'education':[],'experience':[],'projects':[],'skills':[],'suggestions':[SUGGESTION]}
        return v


def test_rewrite_without_fact_confirmation_persists_replay_exports_and_keeps_versions(material_context,worker_settings,monkeypatch):
    c,g=material_context;assert source(c,g,text='我做订单服务，用Spring Boot和Redis处理重复请求。').status_code==201
    monkeypatch.setattr(worker_module,'ScriptedMaterials',Rewriter)
    key=uuid4().hex;r=begin(c,g,key=key);assert r.status_code==202
    assert begin(c,g,key=key).json()['run_id']==r.json()['run_id']
    arun(process(worker_settings));v=view(c,g);assert not v['facts'] and len(v['drafts'])==1
    d=v['drafts'][0];assert POLISHED in d['markdown'] and d['payload']['suggestions']==[SUGGESTION]
    assert c.get(f"/api/career-goals/{g['id']}/materials/drafts/{d['id']}.md").text==d['markdown']
    assert source(c,g,text='补充订单流程',revision=1).status_code==201
    old=next(x for x in view(c,g)['drafts'] if x['id']==d['id']);assert old['stale'] and old['markdown']==d['markdown']
    assert begin(c,g,revision=2).status_code==202;arun(process(worker_settings));assert len(view(c,g)['drafts'])==2


def test_rewrite_endpoint_is_owner_scoped_and_missing_source_is_friendly_clarification(material_context,worker_settings,pg_settings):
    c,g=material_context;assert begin(c,g,revision=0).status_code==202;arun(process(worker_settings))
    v=view(c,g);assert v['runs'][0]['result']['action']=='ask_user' and not v['drafts']
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin;uid=signup(other)['user']['id']
        assert begin(other,g,revision=0).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


def test_batched_rewrite_is_not_capped_below_its_batch_plan(material_context,worker_settings):
    """F07Z-51/ENG-24: four batches need 3 reads + 4 submits; the stored fuse is 12, not 6."""
    c,g=material_context
    text='求职意向：AI Agent 研发\n\n技能\n\n'+'\n'.join(f'- 负责模块{i:02d}的接口设计与联调，补充单元测试' for i in range(1,33))
    assert source(c,g,text=text).status_code==201
    assert begin(c,g).status_code==202
    arun(process(worker_settings));v=view(c,g)
    assert v['runs'][0]['status']=='SUCCEEDED',v['runs'][0]
    assert len(v['drafts'])==1 and v['material_model_requests']==7


def test_coordinator_confirmation_can_rewrite_without_fact_confirmation(material_context,worker_settings):
    from test_application_coordinator import process as career_process
    c,g=material_context;source(c,g);arun(career_process(worker_settings))
    current=c.get('/api/career-goals/'+g['id']).json();step=current['steps'][-1]['id']
    assert current['action']['type']=='prepare_materials'
    assert '可直接生成简历优化初稿' in current['action']['text']
    r=post(c,f"/api/career-goals/{g['id']}/steps/{step}/confirm-materials",
           {'expected_version':1,'expected_revision':1,'mode':'draft','draft_style':'rewrite'})
    assert r.status_code==202,r.text
    arun(process(worker_settings));v=view(c,g)
    assert not v['facts'] and v['drafts'][0]['payload']['draft_style']=='rewrite'


def test_legacy_run_hash_and_explicit_source_style_replay_are_compatible(material_context,pg_settings):
    from interview_agent.career.repository import digest
    c,g=material_context;source(c,g);key=uuid4().hex
    body={'expected_version':1,'expected_revision':1,'mode':'analyze','source_ids':[],'fact_ids':[]}
    r=post(c,f"/api/career-goals/{g['id']}/materials/runs",body,key);assert r.status_code==202
    rows=query(pg_settings[1],'SELECT request_hash FROM app.career_material_runs WHERE id=%s',(r.json()['run_id'],))
    assert rows[0]['request_hash']==digest(body)
    again=post(c,f"/api/career-goals/{g['id']}/materials/runs",{**body,'draft_style':'source'},key)
    assert again.status_code==200 and again.json()['run_id']==r.json()['run_id']


def test_rewrite_runs_through_real_sdk_and_budget_transport_using_mock_only(material_context,worker_settings,pg_settings,project_root,tmp_path):
    c,g=material_context;source(c,g,text='我做订单服务，用Spring Boot和Redis处理重复请求。');assert begin(c,g).status_code==202
    p=MaterialPolicy.load(project_root/'configs/ja04-deepseek-flash.json');identity='test-rewrite-'+uuid4().hex;arun(authorize(pg_settings[1],p,identity))
    credentials=tmp_path/'mock.env';credentials.write_bytes(b'DEEPSEEK_API_KEY=offline-not-real\n');seen=[]
    async def mock(request):
        body=json.loads(request.content);seen.append(body)
        if len(seen)==1:name,args='read_material_scope',{}
        elif len(seen)==2:
            assert json.loads(body['messages'][-1]['content'])['draft_style']=='rewrite'
            name,args='read_target',{}
        elif len(seen)==3:
            src=view(c,g)['sources'][0]['id'];name,args='read_source',{'source_id':src}
        else:name,args='submit_rewrite',{'items':[{'text':POLISHED,'source_ids':[view(c,g)['sources'][0]['id']],'change_note':'优化表达。'}],'education':[],'experience':[],'projects':[],'skills':[],'suggestions':[SUGGESTION]}
        from interview_agent.runtime.types import AssistantResponse,ToolCall
        return httpx.Response(200,json=reply(AssistantResponse(tool_calls=[ToolCall(id=str(len(seen)),name=name,arguments=args)])))
    try:
        arun(process(worker_settings,provider='live',policy=p,authorization=identity,credentials=credentials,inner_factory=lambda:httpx.MockTransport(mock)))
        v=view(c,g);assert len(seen)==4 and v['runs'][0]['status']=='SUCCEEDED' and POLISHED in v['drafts'][0]['markdown']
    finally:cleanup(pg_settings[1],identity)
