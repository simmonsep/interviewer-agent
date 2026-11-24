"""Historical packages -> interview Worker -> source-bound application reflection."""
from uuid import uuid4
import pytest
from interview_agent.interviews.agent import scope_view
from interview_agent.career.repository import digest
from test_postgres import arun,pg_settings,query,client_for,signup
from test_worker import worker_settings
from test_application_repository import context,create as application,progress,body,url
from test_interview_repository import process,view,question,answer,create,base
from test_material_repository import source,analyze,confirm

pytestmark=[pytest.mark.postgres,pytest.mark.redis]

def post(c,path,b,key=None):return c.post(path,json=b,headers={'Idempotency-Key':key or uuid4().hex})
def stop(c,g,s):return c.post(base(g)+f"/interviews/{s['id']}/stop",json={'expected_version':s['version']}).json()
def getapp(c,g,a):return c.get(url(g)+'/'+a['id']).json()

def test_historical_package_project_question_and_completed_feedback(context,worker_settings,pg_settings):
    c,g=context;src=source(c,g).json();r=analyze(c,g,worker_settings);confirm(c,g,r)
    a=application(c,g,source_id=src['source_id'],expected_material_revision=2)
    a=progress(c,g,a).json();original=a['package'];assert len(original['material']['facts'])==2
    source(c,g,text='新经历不应替换历史材料',revision=2)
    changed=post(c,base(g)+'/messages',{'expected_version':1,'message':'改AI方向','jd_text':'Python新版JD','conditions':{'direction':'AI'}})
    assert changed.status_code==202
    s=create(c,g,expected_version=2,application_id=a['id'],interview_round='一面').json()
    assert s['application']['package_revision']==1 and s['application']['package_goal_version']==1
    assert s['target']==original['target'] and s['draft_id'] is None
    scope=query(pg_settings[1],'SELECT input_payload FROM app.career_interview_runs WHERE session_id=%s',(s['id'],))[0]['input_payload']
    assert scope_view(scope)['application']['interview_round']=='一面'
    assert scope['facts']==original['material']['facts'] and 'Python新版JD' not in str(scope_view(scope))
    arun(process(worker_settings));s=view(c,g,s)
    assert question(s)['payload']['kind']=='project' and original['material']['facts'][0]['quote'] in question(s)['payload']['text']
    # Later material uploads do not invalidate an intentionally historical interview.
    assert post(c,base(g)+'/materials/sources',{'expected_version':2,'expected_revision':3,'name':'later','format':'text','text':'另一新版材料'}).status_code==201
    assert view(c,g,s)['status']=='WAITING_FOR_USER'
    answer(c,g,s,'我负责事务边界与验证，仍需具体场景检验。');arun(process(worker_settings));s=view(c,g,s)
    hidden=getapp(c,g,a);assert not hidden['training_observations'] and not hidden['review']['training_hypotheses']
    assert hidden['training_sessions'][0]['binding']['interview_round']=='一面'
    answer(c,g,s);arun(process(worker_settings));s=view(c,g,s);assert s['status']=='COMPLETED'
    v=getapp(c,g,a);assert len(v['training_observations'])==2 and v['status']=='SUBMITTED'
    assert all(o['binding']['package_sha256']==s['application']['package_sha256'] for o in v['training_observations'])
    assert all(h['verification']=='training_observation_not_rejection_cause' and h['origin']=='scripted_protocol' for h in v['review']['training_hypotheses'])
    assert any(x['kind']=='retest_topic' and 'session:'+s['id'] in x['evidence_refs'] for x in v['review']['actions'])
    oid=v['training_observations'][0]['id']
    assert post(c,base(g)+f'/interview-memory/{oid}/retract',{'reason':'用户撤回这条训练观察'}).status_code==200
    v=getapp(c,g,a);assert len(v['training_observations'])==1
    assert all('observation:'+oid not in h['evidence_refs'] for h in v['review']['training_hypotheses'])
    assert v['package']==original

def test_default_submitted_package_explicit_revision_and_rounds(context):
    c,g=context;a=application(c,g);a=progress(c,g,a).json()
    a=progress(c,g,a,'DRAFT',is_correction=True).json()
    src=source(c,g).json()
    a=post(c,url(g)+'/'+a['id']+'/packages',body(expected_application_version=a['version'],expected_material_revision=1,source_id=src['source_id'])).json()
    assert a['current_package_revision']==2 and a['submitted_package_revision']==1
    b={'expected_version':1,'role':'java-backend','application_id':a['id'],'interview_round':'一面'};key=uuid4().hex
    s=post(c,base(g)+'/interviews',b,key).json();assert s['application']['package_revision']==1;stop(c,g,s)
    replay=post(c,base(g)+'/interviews',b,key);assert replay.status_code==200 and replay.json()['id']==s['id']
    assert post(c,base(g)+'/interviews',{**b,'interview_round':'二面'},key).status_code==409
    new=post(c,base(g)+'/interviews',{**b,'application_package_revision':2,'interview_round':'二面'}).json()
    assert new['application']['package_revision']==2 and new['application']['interview_round']=='二面'
    assert new['material_fact_ids']==[] # Raw text never becomes a confirmed project fact.
    assert {x['binding']['interview_round'] for x in getapp(c,g,a)['training_sessions']}=={'一面','二面'}

def test_application_binding_validation_and_owner(context,pg_settings):
    c,g=context;a=application(c,g)
    for extra in [{'application_package_revision':1},{'interview_round':'一面'},
                  {'application_id':a['id'],'draft_id':str(uuid4())},
                  {'application_id':a['id'],'interview_round':' '},
                  {'application_id':a['id'],'application_package_revision':0}]:
        assert create(c,g,**extra).status_code==400
    assert create(c,g,application_id=a['id'],application_package_revision=2).status_code==404
    assert create(c,g,application_id=a['id'],fact_ids=[str(uuid4())]).json()['code']=='fact_not_in_application_package'
    other=post(c,'/api/career-goals/',{'message':'另一目标'}).json()
    assert create(c,other,application_id=a['id']).status_code==404
    with client_for(pg_settings[0]) as outsider:
        outsider.headers['Origin']=pg_settings[0].origin;uid=signup(outsider)['user']['id']
        assert create(outsider,g,application_id=a['id']).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))

def test_large_package_requires_explicit_facts_subset(context,worker_settings):
    c,g=context;src=source(c,g,text='\n'.join('我负责实现模块'+str(n)+'并验证结果。' for n in range(7))).json()
    r=analyze(c,g,worker_settings);confirm(c,g,r)
    a=application(c,g,source_id=src['source_id'],expected_material_revision=2)
    ids=[f['id'] for f in a['package']['material']['facts']];assert len(ids)==7
    assert create(c,g,application_id=a['id']).json()['code']=='material_scope_too_large_choose_fact_ids'
    s=create(c,g,application_id=a['id'],fact_ids=ids[:2]).json();assert set(s['material_fact_ids'])==set(ids[:2])

def test_application_binding_still_fences_goal_change(context,worker_settings):
    c,g=context;a=application(c,g);s=create(c,g,application_id=a['id']).json();arun(process(worker_settings))
    assert view(c,g,s)['status']=='WAITING_FOR_USER'
    post(c,base(g)+'/messages',{'expected_version':1,'message':'用户更改目标'})
    assert view(c,g,s)['status']=='STALE'

def test_legacy_request_hash_and_independent_training_remain_compatible(context,pg_settings):
    c,g=context;b={'expected_version':1,'role':'java-backend'};key=uuid4().hex
    s=post(c,base(g)+'/interviews',b,key).json()
    # A historical request must not be derived from the current, extensible contract.
    old={'expected_version':1,'role':'java-backend','mode':'interview','max_questions':4,
         'duration_seconds':1200,'fact_ids':[],'draft_id':None}
    row=query(pg_settings[1],'SELECT request_hash FROM app.career_interviews WHERE id=%s',(s['id'],))[0]
    assert row['request_hash']==digest(old) and s['application'] is None
    assert post(c,base(g)+'/interviews',b,key).status_code==200

def test_application_draft_uses_frozen_facts_after_current_draft_stales(context,worker_settings):
    from test_material_repository import start,process as material_process
    c,g=context;source(c,g);r=analyze(c,g,worker_settings);confirm(c,g,r)
    start(c,g,2,'draft');arun(material_process(worker_settings))
    d=c.get(base(g)+'/materials').json()['drafts'][0]
    a=application(c,g,draft_id=d['id'],expected_material_revision=2)
    source(c,g,text='草稿之后新增材料',revision=2)
    s=create(c,g,application_id=a['id']).json()
    assert s['draft_id']==d['id'] and s['material_fact_ids']==[f['id'] for f in d['payload']['facts']]
    arun(process(worker_settings));assert question(view(c,g,s))['payload']['kind']=='project'

def test_reflection_excludes_other_application_and_marks_assistance(context,worker_settings):
    c,g=context;a=application(c,g);s=create(c,g,application_id=a['id'],mode='coaching').json()
    arun(process(worker_settings));s=view(c,g,s)
    for text in ('不知道，只加volatile。','我会验证同步边界。'):
        answer(c,g,s,text);arun(process(worker_settings));s=view(c,g,s)
    v=getapp(c,g,a);assert any(h['assisted'] for h in v['review']['training_hypotheses'])
    post(c,base(g)+'/messages',{'expected_version':1,'message':'换岗位','jd_text':'Python AI新职位'})
    other=application(c,g,expected_version=2,title='AI实习')
    assert not other['training_sessions'] and not other['review']['training_hypotheses']

def test_sdk_mock_delivers_historical_binding_and_provenance(context,worker_settings,pg_settings,project_root,tmp_path):
    import json,httpx
    from interview_agent.interviews.billing import InterviewPolicy
    from interview_agent.billing.ledger import authorize
    from interview_agent.runtime.types import AssistantResponse,ToolCall
    from test_billing import reply
    from test_interview_repository import cleanup
    c,g=context;a=application(c,g)
    create(c,g,application_id=a['id'],interview_round='技术一面')
    policy=InterviewPolicy.load(project_root/'configs/ja05-deepseek-flash.json');identity='test-'+uuid4().hex
    arun(authorize(pg_settings[1],policy,identity));key=tmp_path/'mock.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n');seen=[]
    def handler(request):
        payload=json.loads(request.content);seen.append(payload)
        if len(seen)==1:name,args='read_interview_scope',{}
        elif len(seen)==2:
            delivered=json.loads(payload['messages'][-1]['content'])
            assert delivered['application']['id']==a['id'] and delivered['application']['interview_round']=='技术一面'
            assert delivered['target']['text']=='Java SQL原始岗位' and 'pack' not in delivered
            name,args='read_topics',{'topic_ids':['java-concurrency']}
        else:name,args='submit_interview_action',{'action':'ask_question','topic_id':'java-concurrency','question_id':'java-concurrency-base'}
        return httpx.Response(200,json=reply(AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments=args)])))
    try:
        arun(process(worker_settings,provider='live',policy=policy,authorization=identity,credentials=key,inner_factory=lambda:httpx.MockTransport(handler)))
        assert len(seen)==3
        s=c.get(base(g)+'/interviews').json()['items'][0];assert s['status']=='WAITING_FOR_USER'
    finally:cleanup(pg_settings[1],identity)
