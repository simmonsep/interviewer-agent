"""Actual API/PG/Redis gates for newly frozen coverage, without a real model Key."""
from uuid import uuid4
import pytest
from psycopg.types.json import Jsonb
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.interviews.agent import ScriptedInterviewer
from interview_agent.career.repository import CareerRepository
from test_postgres import arun,pg_settings,query
from test_worker import worker_settings,resources
from test_material_repository import material_context,source,analyze,confirm,view as materials
from test_interview_repository import create,view,answer,question,process,notification
from test_career_goals import message_for

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


def prepared(c,g,settings):
    assert source(c,g).status_code==201
    run=analyze(c,g,settings);assert confirm(c,g,run).status_code==201
    return materials(c,g)['facts'][0]['id']


def test_new_mixed_policy_is_frozen_only_for_supported_interview_scope(material_context,worker_settings,pg_settings):
    c,g=material_context;fid=prepared(c,g,worker_settings)
    cases=[({},True),({'max_questions':1},False),({'mode':'coaching'},False),
           ({'allow_project_questions':False},False),({'fact_ids':[]},False)]
    for extra,expected in cases:
        body={'fact_ids':[fid],**extra};key=uuid4().hex
        path='/api/career-goals/'+g['id']+'/interviews'
        request={'expected_version':1,'role':'java-backend','max_questions':2,**body}
        r=c.post(path,json=request,headers={'Idempotency-Key':key});assert r.status_code==202,r.text
        s=r.json();frozen=query(pg_settings[1],'SELECT input_payload FROM app.career_interviews WHERE id=%s',(s['id'],))[0]['input_payload']
        assert ('coverage' in frozen['policy'])==expected
        again=c.post(path,json=request,headers={'Idempotency-Key':key});assert again.status_code==200 and again.json()['id']==s['id']
        assert c.post(path+'/'+s['id']+'/stop',json={'expected_version':s['version']}).status_code==200


def test_save_and_cached_complete_both_reject_spending_last_slot_on_project_again(material_context,worker_settings,pg_settings):
    c,g=material_context;fid=prepared(c,g,worker_settings)
    s=create(c,g,fact_ids=[fid]).json();arun(process(worker_settings));s=view(c,g,s)
    assert question(s)['payload']['kind']=='project'
    assert answer(c,g,s,'我亲自实现SQL事务。').status_code==202
    note=notification(pg_settings[1],s['id'])
    async def exercise():
        async with resources(worker_settings) as (old,q):
            repo=InterviewRepository(old.pool);claim=await repo.claim(note);assert claim
            scope=await repo.context(claim)
            async def noop():pass
            good=ScriptedInterviewer(scope,noop).decision
            assert good.topic_id!='project'
            bad=good.model_copy(update={'topic_id':'project','question_id':'project-contribution','fact_id':fid,'project_focus':'verification'})
            with pytest.raises(ValueError,match='question_coverage_required'):await repo.save(claim,bad,'scripted_protocol')
            async with old.pool.connection() as conn:
                await conn.execute('UPDATE app.career_interview_runs SET candidate=%s WHERE id=%s',(Jsonb(bad.model_dump(mode='json')),claim.message.operation_id))
            with pytest.raises(ValueError,match='question_coverage_required'):await repo.complete(claim)
            assert await repo.save(claim,good,'scripted_protocol') and await repo.complete(claim)
    arun(exercise());s=view(c,g,s);assert question(s)['payload']['kind']=='base'
    assert answer(c,g,s,'需要通过具体情景继续验证。').status_code==202
    arun(process(worker_settings));s=view(c,g,s)
    assert s['status']=='COMPLETED' and s['report']['questions']==2
    assert {e['payload']['kind'] for e in s['events'] if e['kind']=='question'}=={'project','base'}


def test_material_candidate_rechecks_current_upload_count_at_save_and_complete(material_context,worker_settings,pg_settings):
    c,g=material_context;note=message_for(pg_settings[1],g)
    async def take():
        async with resources(worker_settings) as (old,q):return await CareerRepository(old.pool).claim(note)
    claim=arun(take());assert claim
    # The model read an empty state; uploading while it works must update the saved/displayed text.
    assert source(c,g).status_code==201
    candidate={'type':'prepare_materials','text':'请先上传简历。'}
    async def save():
        async with resources(worker_settings) as (old,q):return await CareerRepository(old.pool).save(claim,candidate,'scripted_protocol')
    assert arun(save())
    assert source(c,g,text='补充：我验证过提交后响应丢失。',revision=1).status_code==201
    async def finish():
        async with resources(worker_settings) as (old,q):return await CareerRepository(old.pool).complete(claim)
    assert arun(finish())
    v=c.get('/api/career-goals/'+g['id']).json()
    assert '已上传2份' in v['action']['text'] and '0条用户确认' in v['action']['text']
