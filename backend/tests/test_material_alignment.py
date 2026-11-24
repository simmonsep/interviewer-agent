import asyncio
from uuid import uuid4
import pytest
from interview_agent.materials.alignment import Requirement,ground_requirement
from interview_agent.materials.contracts import MaterialResult,validate_result
from interview_agent.materials.agent import ScriptedMaterials,run_material_agent
from test_material_agent import Calls
from test_material_repository import material_context,worker_settings,pg_settings,post,url,view,source,analyze,confirm,start,process
from test_interview_repository import create,process as interview_process,view as interview_view,answer,question
from test_postgres import arun,client_for,signup,query


def scope():
    return {'mode':'align','goal_version':1,'material_revision':0,'target':{'kind':'user_jd','text':'要求SQL事务能力。'},'sources':[],
            'facts':[{'id':str(uuid4()),'quote':'我实现了事务回滚。','source_id':str(uuid4()),'kind':'contribution'}]}


def item(s):
    return Requirement(requirement_quote='要求SQL事务能力。',evidence_status='needs_clarification',
        fact_ids=[s['facts'][0]['id']],question='你如何验证回滚？',topic_ids=['sql-isolation'])


def test_grounded_alignment_revalidates_offsets_and_does_not_certify_ability():
    s=scope();ground=ground_requirement(item(s),s)
    result=MaterialResult(action='alignment',alignment_items=[ground])
    assert validate_result(s,result)==result
    bad=result.model_dump();bad['alignment_items'][0]['end_char']+=1
    with pytest.raises(ValueError):validate_result(s,bad)
    with pytest.raises(ValueError):validate_result(s,MaterialResult(action='alignment',alignment_items=[ground,ground]))


@pytest.mark.parametrize('change',[
    {'requirement_quote':'虚构的岗位要求'}, {'fact_ids':[str(uuid4())]}, {'topic_ids':['invented-topic']},
    {'evidence_status':'evidence_present','fact_ids':[],'question':''}, {'evidence_status':'needs_clarification','question':''},
    {'evidence_status':'not_covered','question':'','fact_ids':[str(uuid4())]},
],ids=['false-jd','foreign-fact','unknown-topic','missing-evidence','missing-question','uncovered-with-fact'])
def test_invalid_alignment_cannot_be_submitted(change):
    s=scope();value=item(s).model_dump();value.update(change)
    with pytest.raises(ValueError):ground_requirement(Requirement.model_validate(value),s)


def test_alignment_reads_target_and_facts_before_submission_and_can_repair():
    s=scope();args={'requirements':[item(s).model_dump()]}
    provider=Calls([('read_material_scope',{}),('submit_alignment',args),('read_target',{}),('read_confirmed_facts',{}),('submit_alignment',args)])
    result=asyncio.run(run_material_agent(s,provider,asyncio.Event()))
    assert result.action=='alignment' and len(provider.errors)==1
    async def admit():pass
    empty={**s,'target':{'kind':'direction','direction':'Java'},'facts':[]}
    r=asyncio.run(run_material_agent(empty,ScriptedMaterials(empty,admit),asyncio.Event()))
    assert r.action=='ask_user' and not r.alignment_items


def align(c,g,settings,revision):
    response=start(c,g,revision,'align');assert response.status_code==202,response.text
    arun(process(settings));r=next(r for r in view(c,g)['runs'] if r['id']==response.json()['run_id'])
    assert r['status']=='SUCCEEDED',r
    return r


def clarify(c,g,r,revision,key=None):
    return post(c,url(g)+f"/runs/{r['id']}/clarifications",{'expected_version':1,'expected_revision':revision,
        'answers':[{'requirement_index':0,'text':'我亲自编写了事务回滚测试，并通过独立数据库验证取消后旧结果不能提交。'}]},key)


@pytest.mark.postgres
@pytest.mark.redis
def test_requirement_clarification_confirmation_realignment_and_training_without_web(material_context,worker_settings):
    c,g=material_context;source(c,g);r=analyze(c,g,worker_settings);confirm(c,g,r)
    first=align(c,g,worker_settings,2);assert first['result']['alignment_items'][0]['question']
    workflow=c.get(f"/api/career-goals/{g['id']}/workflow").json()
    assert workflow['material_alignment']['run_id']==first['id']
    key=uuid4().hex;reply=clarify(c,g,first,2,key)
    assert reply.status_code==201,reply.text
    assert clarify(c,g,{**first,'id':first['id'].upper()},2,key).status_code==200
    v=view(c,g);assert v['material_revision']==3 and len(v['facts'])==2
    assert c.get(f"/api/career-goals/{g['id']}/workflow").json()['material_alignment'] is None
    assert v['sources'][0]['clarification_context']['qualification']=='unconfirmed_user_answers'
    assert create(c,g,alignment_run_id=first['id'],fact_ids=[f['id'] for f in v['facts']]).json()['code']=='material_alignment_stale'
    ar=start(c,g,3).json();arun(process(worker_settings))
    run=next(r for r in view(c,g)['runs'] if r['id']==ar['run_id'])
    assert confirm(c,g,run,revision=3).status_code==201
    second=align(c,g,worker_settings,4)
    new_fact=next(f for f in view(c,g)['facts'] if f['source_id']==reply.json()['source_id'])
    assert second['result']['alignment_items'][0]['fact_ids']==[new_fact['id']]
    s=create(c,g,alignment_run_id=second['id'],fact_ids=[new_fact['id']])
    assert s.status_code==202,s.text
    arun(interview_process(worker_settings));saved=interview_view(c,g,s.json())
    assert question(saved)['payload']['topic_id']=='project'
    assert saved['material_alignment']['run_id']==second['id']
    assert answer(c,g,saved,'我会说明实际负责的边界与独立验证方法。').status_code==202
    arun(interview_process(worker_settings));saved=interview_view(c,g,s.json())
    assert question(saved)['payload']['topic_id']=='sql-isolation'
    assert view(c,g)['material_model_requests']==12


@pytest.mark.postgres
@pytest.mark.redis
def test_no_resume_alignment_guides_topic_and_new_material_invalidates_session(material_context,worker_settings):
    c,g=material_context;r=align(c,g,worker_settings,0)
    s=create(c,g,alignment_run_id=r['id']);assert s.status_code==202,s.text
    arun(interview_process(worker_settings));saved=interview_view(c,g,s.json())
    assert question(saved)['payload']['topic_id']=='sql-isolation'
    assert source(c,g).status_code==201
    assert interview_view(c,g,s.json())['status']=='STALE'


@pytest.mark.postgres
@pytest.mark.redis
def test_alignment_refs_answers_and_training_are_owned_and_current(material_context,worker_settings,pg_settings):
    c,g=material_context;source(c,g);r=analyze(c,g,worker_settings);confirm(c,g,r);a=align(c,g,worker_settings,2)
    assert create(c,g,alignment_run_id=a['id']).json()['code']=='alignment_fact_scope_mismatch'
    bad=post(c,url(g)+f"/runs/{a['id']}/clarifications",{'expected_version':1,'expected_revision':2,'answers':[{'requirement_index':5,'text':'回答'}]})
    assert bad.status_code==400 and view(c,g)['material_revision']==2
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin;uid=signup(other)['user']['id']
        assert clarify(other,g,a,2).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))
    assert source(c,g,revision=2).status_code==201
    assert clarify(c,g,a,3).json()['code']=='material_alignment_stale'
