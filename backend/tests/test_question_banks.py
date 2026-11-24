from uuid import uuid4
import pytest
from pydantic import ValidationError
from interview_agent.question_banks.contracts import BankDocument
from interview_agent.question_banks.defaults import defaults
from interview_agent.interviews.contracts import validate_decision
from test_interview_repository import context,worker_settings,pg_settings,post,base,create,process,view,question,answer
from test_postgres import arun,client_for,signup,query


def document(question='请描述如何定位一次并发错误。',reference=''):
    return {'schema_version':1,'name':'我的私有面试库','items':[{'item_id':'concurrency','roles':['java-backend'],
        'kind':'short_answer','title':'并发排查','question':question,'reference':reference}]}


def preview(c,data=None,key=None):return post(c,'/api/question-banks/imports',{'document':data or document()},key)
def commit(c,p,body=None,key=None):return post(c,f"/api/question-banks/imports/{p['preview_id']}/commit",body or {},key)
def listing(c):return c.get('/api/question-banks').json()['banks']
def imported(c,data=None):
    p=preview(c,data);assert p.status_code==201,p.text
    r=commit(c,p.json());assert r.status_code==201,r.text
    return r.json()
def selected(bank):return [{'bank_id':bank['bank_id'],'version':bank['version']}]


@pytest.mark.parametrize('change',[
    {'items':[]},{'schema_version':99},{'owner_id':'invented'},
    {'items':[{'item_id':'x','roles':['java-backend'],'title':' ','question':'题目'}]},
    {'items':[{'item_id':'x','roles':['java-backend'],'title':'标题','question':'题目','shell':'echo no'}]},
],ids=['empty','schema','owner-spoof','blank-title','extra-command'])
def test_invalid_import_shapes_are_rejected(change):
    data=document();data.update(change)
    with pytest.raises(ValidationError):BankDocument.model_validate(data)


def test_duplicate_items_and_default_files_are_valid():
    data=document();data['items']*=2
    with pytest.raises(ValidationError):BankDocument.model_validate(data)
    assert len(defaults())==4
    by_role={b['role']:b for b in defaults().values()}
    assert len(by_role['java-backend']['document']['items'])==4
    assert len(by_role['ai-application']['document']['items'])==4
    assert len(by_role['written']['document']['items'])==3
    # F07Z-73: the aptitude bank is choice-only and every item declares its key.
    aptitude=by_role['aptitude']['document']
    assert aptitude['name']=='行测 · 数量关系与资料分析' and len(aptitude['items'])==20
    assert all(i['kind']=='single_choice' and i['correct_option_ids'] for i in aptitude['items'])
    assert all(i['roles']==['java-backend','ai-application'] for i in aptitude['items'])


@pytest.mark.postgres
@pytest.mark.redis
def test_preview_commit_export_idempotence_and_builtin_readonly(context):
    c,g=context;k=uuid4().hex;p=preview(c,key=k);assert p.status_code==201,p.text
    assert preview(c,key=k).json()['preview_id']==p.json()['preview_id']
    assert preview(c,document('改题'),k).status_code==409
    assert len(listing(c))==4
    p=p.json();assert p['items_without_reference']==['concurrency']
    assert c.get('/api/question-banks/imports/'+p['preview_id']).json()==p
    ck=uuid4().hex;b=commit(c,p,key=ck);assert b.status_code==201,b.text
    b=b.json();assert commit(c,{**p,'preview_id':p['preview_id'].upper()},key=ck).json()==b
    assert len(listing(c))==5 and commit(c,p).status_code==409
    exported=c.get(f"/api/question-banks/{b['bank_id']}/versions/1").json()
    assert exported['document']['items'][0]['question']==document()['items'][0]['question']
    builtin=next(x for x in listing(c) if x['origin']=='builtin')
    r=c.patch('/api/question-banks/'+builtin['bank_id'],json={'expected_revision':1,'enabled':False})
    assert r.status_code==403


@pytest.mark.postgres
@pytest.mark.redis
def test_selected_question_without_reference_records_answer_and_finishes(context,worker_settings):
    c,g=context;b=imported(c);s=create(c,g,question_banks=selected(b))
    assert s.status_code==202,s.text
    arun(process(worker_settings));saved=view(c,g,s.json());q=question(saved)
    assert q['payload']['text']==document()['items'][0]['question'] and q['payload']['has_reference'] is False
    assert q['payload']['bank_item']['bank_id']==b['bank_id']
    assert answer(c,g,saved,'不知道。').status_code==202
    arun(process(worker_settings));saved=view(c,g,s.json())
    assert saved['status']=='COMPLETED',saved
    assert saved['observations'][0]['label']=='inconclusive'
    assert '不作正确性判断' in saved['observations'][0]['summary']
    assert saved['question_banks'][0]['version']==1


@pytest.mark.postgres
@pytest.mark.redis
def test_versions_and_disable_preserve_existing_session_snapshot(context,worker_settings):
    c,g=context;b=imported(c,document(reference='排查并发问题需要复现交错、定位共享状态并验证同步边界。'))
    s=create(c,g,question_banks=selected(b),max_questions=1).json();arun(process(worker_settings));saved=view(c,g,s)
    p=preview(c,document('新版题干','新版用户参考。')).json()
    b2=commit(c,p,{'bank_id':b['bank_id'],'expected_version':1});assert b2.status_code==201,b2.text
    assert b2.json()['version']==2
    row=next(x for x in listing(c) if x['bank_id']==b['bank_id'])
    assert c.patch('/api/question-banks/'+b['bank_id'],json={'expected_revision':row['revision'],'enabled':False}).status_code==200
    assert question(view(c,g,s))['payload']['text']==document()['items'][0]['question']
    assert answer(c,g,saved,'我会构造并发交错并检查共享状态。').status_code==202
    arun(process(worker_settings));assert view(c,g,s)['status']=='COMPLETED'
    assert create(c,g,question_banks=selected(b2.json())).json()['code']=='question_bank_disabled'
    current=next(x for x in listing(c) if x['bank_id']==b['bank_id'])
    c.patch('/api/question-banks/'+b['bank_id'],json={'expected_revision':current['revision'],'enabled':True})
    new=create(c,g,question_banks=selected(b2.json()));assert new.status_code==202,new.text
    arun(process(worker_settings));assert question(view(c,g,new.json()))['payload']['text']=='新版题干'
    assert c.get(f"/api/question-banks/{b['bank_id']}/versions/1").json()['document']['items'][0]['question']==document()['items'][0]['question']
    assert question(view(c,g,new.json()))['payload']['topic_id']!=question(saved)['payload']['topic_id']


@pytest.mark.postgres
@pytest.mark.redis
def test_combining_default_and_private_namespaces_and_owner_scope(context,pg_settings,worker_settings):
    c,g=context;b=imported(c);builtin=next(x for x in listing(c) if x['origin']=='builtin')
    s=create(c,g,question_banks=[{'bank_id':builtin['bank_id'],'version':1},*selected(b)])
    assert s.status_code==202,s.text
    assert len(s.json()['question_banks'])==2
    arun(process(worker_settings));assert question(view(c,g,s.json()))['payload']['bank_item']['origin']=='builtin'
    with client_for(pg_settings[0]) as foreign:
        foreign.headers['Origin']=pg_settings[0].origin;uid=signup(foreign)['user']['id']
        fg=post(foreign,'/api/career-goals/',{'message':'另一个用户的目标'}).json()
        assert len(listing(foreign))==4
        assert foreign.get(f"/api/question-banks/{b['bank_id']}/versions/1").status_code==404
        assert create(foreign,fg,question_banks=selected(b)).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


@pytest.mark.postgres
@pytest.mark.redis
def test_bank_text_and_no_reference_cannot_be_used_to_invent_verdict(context,worker_settings,pg_settings):
    c,g=context;b=imported(c);s=create(c,g,question_banks=selected(b)).json();arun(process(worker_settings))
    row=query(pg_settings[1],'SELECT input_payload FROM app.career_interview_runs WHERE session_id=%s',(s['id'],))[0]['input_payload']
    tid=row['pack']['topic_order'][0]
    with pytest.raises(ValueError,match='bank_question_text_is_frozen'):
        validate_decision(row,{'action':'ask_question','topic_id':tid,'question_id':tid+'-base','question_text':'模型另造题'})
    with pytest.raises(ValueError,match='project_question_not_enabled'):
        validate_decision(row,{'action':'ask_question','topic_id':'project','question_id':'project-contribution','fact_id':str(uuid4())})
    saved=view(c,g,s);answer(c,g,saved)
    row=query(pg_settings[1],'SELECT input_payload FROM app.career_interview_runs WHERE session_id=%s ORDER BY session_version DESC',(s['id'],))[0]['input_payload']
    bad={'action':'finish','summary':'结束','observation':{'answer_id':row['answer']['id'],'answer_quote':row['answer']['text'],
         'label':'supported_in_this_answer','summary':'判断正确','evidence_id':tid+'-reference','evidence_quote':document()['items'][0]['question']}}
    # A+ contract: a missing reference forces the label instead of rejecting the decision.
    normalised=validate_decision(row,bad)
    assert normalised.observation.label=='inconclusive'
    coaching={**bad,'action':'teach_and_retest','explanation':'讲解'}
    with pytest.raises(ValueError,match='question_has_no_reference_for_assessment'):
        validate_decision(row,coaching)


@pytest.mark.postgres
@pytest.mark.redis
def test_same_item_ids_in_two_private_banks_do_not_override_each_other(context,worker_settings):
    c,g=context;b1=imported(c,document('第一题'));b2=imported(c,document('第二题'))
    s=create(c,g,question_banks=[*selected(b1),*selected(b2)]).json();arun(process(worker_settings))
    first=view(c,g,s);assert question(first)['payload']['text']=='第一题'
    answer(c,g,first,'我先记录自己的思路。');arun(process(worker_settings));second=view(c,g,s)
    assert question(second)['payload']['text']=='第二题'
    assert question(first)['payload']['topic_id']!=question(second)['payload']['topic_id']


@pytest.mark.postgres
@pytest.mark.redis
def test_preview_delete_and_version_update_remain_protected(context,pg_settings):
    from psycopg.errors import ForeignKeyViolation,CheckViolation
    c,g=context;p=preview(c).json();b=commit(c,p).json()
    with pytest.raises(ForeignKeyViolation):
        query(pg_settings[1],'DELETE FROM app.question_bank_imports WHERE id=%s RETURNING id',(p['preview_id'],))
    with pytest.raises(CheckViolation):
        query(pg_settings[1],'UPDATE app.question_bank_versions SET content_sha256=%s WHERE bank_id=%s RETURNING version',('changed',b['bank_id']))
    assert c.get(f"/api/question-banks/{b['bank_id']}/versions/1").status_code==200
