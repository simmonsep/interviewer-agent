import json
from uuid import uuid4
import pytest
from interview_agent.materials.resume_evidence import resume_evidence,select_resume_evidence
from interview_agent.storage.errors import ApiError
from test_postgres import arun,pg_settings,query
from test_worker import worker_settings
from test_material_repository import material_context,source,view,process as materials_process
from test_resume_rewrite_repository import begin
from test_interview_repository import create,process,view as session_view,question,answer,post,base


def prepared(c,g,w):
    source(c,g,text='我负责订单服务，使用SQL事务与Redis处理重复请求。');begin(c,g);arun(materials_process(w));return view(c,g)['drafts'][0]


@pytest.mark.postgres
@pytest.mark.redis
def test_selected_rewrite_reaches_project_question_and_observation_without_fact_confirmation(material_context,worker_settings):
    c,g=material_context;d=prepared(c,g,worker_settings)
    s=create(c,g,draft_id=d['id'],max_questions=1).json();assert s['resume_evidence'] and not view(c,g)['facts']
    arun(process(worker_settings));s=session_view(c,g,s);q=question(s)
    assert q['payload']['kind']=='project' and '你选用的简历' in q['payload']['text']
    assert q['payload']['resume_source']['draft_id']==d['id'] and '团队其他人' not in q['payload']['text']
    assert answer(c,g,s,'我用事务保存请求记录，通过重复请求用例验证边界。').status_code==202
    arun(process(worker_settings));s=session_view(c,g,s)
    assert s['status']=='COMPLETED' and s['observations'][0]['evidence_source']['origin']=='user_selected_resume_statement'
    assert s['observations'][0]['evidence_source']['resume_source']['draft_id']==d['id']
    assert not view(c,g)['facts']


@pytest.mark.postgres
@pytest.mark.redis
def test_application_freezes_resume_evidence_and_old_package_shape_is_not_backfilled(material_context,worker_settings,pg_settings):
    from test_application_repository import create as application
    c,g=material_context
    old=application(c,g);assert 'resume_evidence' not in old['package']['material']
    d=prepared(c,g,worker_settings)
    r=post(c,base(g)+'/applications/'+old['id']+'/packages',{'expected_version':1,'expected_material_revision':1,'expected_application_version':1,'company':old['package']['company'],'title':old['package']['title'],'draft_id':d['id']})
    assert r.status_code==200,r.text
    a=r.json();assert 'resume_evidence' not in a['packages'][0]['payload']['material']
    s=create(c,g,application_id=a['id'],application_package_revision=2,max_questions=1).json()
    assert s['resume_evidence'][0]['resume_source']['draft_version']==d['version']
    arun(process(worker_settings));s=session_view(c,g,s);answer(c,g,s);arun(process(worker_settings))
    a=c.get(base(g)+'/applications/'+a['id']).json()
    assert a['review']['training_hypotheses'][0]['resume_source']['draft_id']==d['id']
    assert a['status']=='DRAFT' and not view(c,g)['facts']
    evidence=c.get(f"/api/career-goals/{g['id']}/evidence/applications/{a['id']}").json()
    assert evidence['kind']=='application_action' and evidence['linked_observations']


def test_resume_evidence_ids_limits_and_excerpt_are_explicit():
    draft=str(uuid4());source_id=str(uuid4());p={'draft_style':'rewrite','rewrite_items':[{'text':'x'*500,'source_ids':[source_id]} for _ in range(8)]}
    e=resume_evidence(draft,3,p);assert len({i['id'] for i in e})==8 and e==resume_evidence(draft,3,p)
    assert len(e[0]['quote'])==240 and e[0]['resume_source']['quote_truncated']
    with pytest.raises(ApiError,match='resume_scope_too_large'):select_resume_evidence(e,[])
    assert select_resume_evidence(e,[7,0])==[e[7],e[0]]
    with pytest.raises(ApiError):select_resume_evidence([], [0])


@pytest.mark.postgres
@pytest.mark.redis
def test_imported_verbatim_lines_can_anchor_a_project_question(material_context,worker_settings):
    from test_material_repository import post,url
    c,g=material_context
    assert source(c,g,text='项目经历\n负责订单服务，使用SQL事务与Redis处理重复请求。').status_code==201
    imported=post(c,url(g)+'/resume-imports',{'expected_version':1,'expected_revision':1},uuid4().hex)
    assert imported.status_code==201,imported.text
    draft_id=imported.json()['draft_id']
    s=create(c,g,draft_id=draft_id,max_questions=1).json()
    assert s['resume_evidence'] and s['resume_evidence'][0]['resume_source']['draft_id']==draft_id
    arun(process(worker_settings));s=session_view(c,g,s);q=question(s)
    assert q['payload']['kind']=='project' and '你选用的简历' in q['payload']['text']
    assert q['payload']['resume_source']['draft_id']==draft_id
    assert not view(c,g)['facts'],'verbatim anchors must not become confirmed facts'

