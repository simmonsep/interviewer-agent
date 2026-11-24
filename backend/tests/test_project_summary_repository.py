"""Original Worker/SQL/cache/API/memory/application consumers; synthetic test accounts."""
from uuid import uuid4
import pytest
from psycopg.types.json import Jsonb
import interview_agent.interviews.worker as worker_module
from interview_agent.interviews.agent import ScriptedInterviewer
from interview_agent.interviews.repository import InterviewRepository
from test_postgres import arun,pg_settings,query
from test_worker import worker_settings,resources
from test_material_repository import material_context,source,analyze,confirm,view as materials
from test_interview_repository import create,view,answer,process,notification,base
from test_application_repository import create as application,progress,url
from test_project_summary import ANSWER,UNSUPPORTED

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


def prepared(c,g,settings):
    src=source(c,g).json();run=analyze(c,g,settings);assert confirm(c,g,run).status_code==201
    return src['source_id'],materials(c,g)['facts'][0]['id']


def test_new_policy_freezes_for_project_capable_sessions_without_changing_request_replay(material_context,worker_settings):
    c,g=material_context;_,fid=prepared(c,g,worker_settings)
    for extra,expected in [({},True),({'max_questions':1},True),({'mode':'coaching'},True),
                           ({'allow_project_questions':False},False),({'fact_ids':[]},False)]:
        body={'expected_version':1,'role':'java-backend','max_questions':2,'fact_ids':[fid],**extra};key=uuid4().hex
        path=base(g)+'/interviews';r=c.post(path,json=body,headers={'Idempotency-Key':key});assert r.status_code==202,r.text
        s=r.json();assert ('feedback_summary' in s['policy'])==expected
        assert c.post(path,json=body,headers={'Idempotency-Key':key}).json()['policy']==s['policy']
        assert c.post(path+'/'+s['id']+'/stop',json={'expected_version':s['version']}).status_code==200


def test_worker_result_memory_next_scope_and_application_use_normalized_project_summary(material_context,worker_settings,pg_settings,monkeypatch):
    c,g=material_context;src,fid=prepared(c,g,worker_settings)
    a=application(c,g,source_id=src,expected_material_revision=2);a=progress(c,g,a).json()
    class Unfaithful(ScriptedInterviewer):
        def choose(self):
            d=super().choose()
            if self.scope['answer'] and self.scope['current_question']['topic_id']=='project':d.observation.summary=UNSUPPORTED
            if d.action=='finish':d.summary='已经证明提交前验证成功。'
            return d
    monkeypatch.setattr(worker_module,'ScriptedInterviewer',Unfaithful)
    s=create(c,g,application_id=a['id']).json();arun(process(worker_settings));s=view(c,g,s)
    assert answer(c,g,s,ANSWER).status_code==202;arun(process(worker_settings));s=view(c,g,s)
    saved=query(pg_settings[1],'SELECT payload FROM app.career_interview_observations WHERE session_id=%s',(s['id'],))[0]['payload']
    assert ANSWER in saved['summary'] and '提交前' not in saved['summary'] and saved['summary_origin']=='server_answer_excerpt'
    assert not s['observations'] # Interview feedback remains hidden until completion.
    assert answer(c,g,s,'不知道，只加volatile。').status_code==202
    frozen=query(pg_settings[1],'SELECT input_payload FROM app.career_interview_runs WHERE session_id=%s ORDER BY session_version DESC LIMIT 1',(s['id'],))[0]['input_payload']
    assert frozen['prior_observations'][-1]['summary']==saved['summary']
    assert frozen['prior_observations'][-1]['summary_origin']=='server_answer_excerpt'
    arun(process(worker_settings));s=view(c,g,s)
    assert s['status']=='COMPLETED' and s['report']['summary_origin']=='server_session_counts'
    assert '已经证明' not in s['report']['model_summary'] and '提交前' not in s['report']['model_summary']
    assert len(s['observations'])==2 and next(o for o in s['observations'] if o['topic_id']=='project')['summary']==saved['summary']
    technical=next(o for o in s['observations'] if o['topic_id']!='project')
    assert technical['summary']=='本回答仍需通过具体情景核验，脚本不做语义判分。' and 'summary_origin' not in technical
    memory=c.get(base(g)+'/interview-memory').json()['items'];item=next(o for o in memory if o['payload']['topic_id']=='project')
    assert item['payload']['summary']==saved['summary']
    a2=c.get(url(g)+'/'+a['id']).json();o=next(o for o in a2['training_observations'] if o['payload']['topic_id']=='project')
    assert o['payload']['summary']==saved['summary'] and o['payload']['summary_origin']=='server_answer_excerpt'
    assert a2['status']=='SUBMITTED' and a2['package']==a['package']
    hypothesis=next(h for h in a2['review']['training_hypotheses'] if h['topic_id']=='project')
    assert hypothesis['summary']==saved['summary'] and hypothesis['summary_origin']=='server_answer_excerpt'
    # Cross-session reuse is a coaching behaviour: a formal interview no longer freezes it (S02b).
    later=create(c,g,application_id=a['id'],mode='coaching').json()
    frozen=query(pg_settings[1],'SELECT input_payload FROM app.career_interviews WHERE id=%s',(later['id'],))[0]['input_payload']
    prior=next(o for o in frozen['prior_observations'] if o['topic_id']=='project')
    assert prior['summary_origin']=='server_answer_excerpt' and prior['summary']==saved['summary']
    assert c.post(base(g)+'/interviews/'+later['id']+'/stop',json={'expected_version':later['version']}).status_code==200


@pytest.mark.parametrize('path',['save','cached_complete'])
def test_sql_save_and_cached_complete_normalize_without_graph_trust(material_context,worker_settings,pg_settings,path):
    c,g=material_context;_,fid=prepared(c,g,worker_settings)
    s=create(c,g,fact_ids=[fid],max_questions=1).json();arun(process(worker_settings));s=view(c,g,s)
    assert answer(c,g,s,ANSWER).status_code==202
    note=notification(pg_settings[1],s['id'])
    async def exercise():
        async with resources(worker_settings) as (old,q):
            repo=InterviewRepository(old.pool);claim=await repo.claim(note);scope=await repo.context(claim)
            async def noop():pass
            d=ScriptedInterviewer(scope,noop).decision;d.observation.summary=UNSUPPORTED;d.summary='已经证明提交前验证成功。'
            if path=='save':assert await repo.save(claim,d,'scripted_protocol')
            else:
                async with old.pool.connection() as conn:
                    await conn.execute('UPDATE app.career_interview_runs SET candidate=%s WHERE id=%s',(Jsonb(d.model_dump(mode='json')),claim.message.operation_id))
            assert await repo.complete(claim)
    arun(exercise());s=view(c,g,s)
    assert s['status']=='COMPLETED' and ANSWER in s['observations'][0]['summary']
    assert '提交前' not in s['observations'][0]['summary'] and '已经证明' not in s['report']['model_summary']
