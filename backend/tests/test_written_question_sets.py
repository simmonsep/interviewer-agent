import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from pydantic import ValidationError
from psycopg.errors import CheckViolation
from interview_agent.question_banks.contracts import BankDocument,normalized_document
from test_interview_repository import context,worker_settings,pg_settings,post,base
from test_question_banks import imported,preview,commit,listing
from test_postgres import query,client_for,signup
from test_written_repository import written as code_context

DEFAULT='00000000-0000-4000-8000-000000000003'


def document(key=None):
    return {'schema_version':2,'name':'我的笔试库','items':[{'item_id':'pick','kind':'single_choice','roles':['java-backend'],
        'title':'选择题','question':'根据维护者声明选择一个选项。','options':[{'option_id':'A','text':'选项一'},{'option_id':'B','text':'选项二'}],
        'correct_option_ids':key if key is not None else ['B']}]}


def create(c,g,banks=None,**kwargs):
    return post(c,base(g)+'/written',{'expected_version':1,'role':'java-backend',
        'question_banks':banks or [{'bank_id':DEFAULT,'version':1}],**kwargs})
def view(c,g,s):return c.get(base(g)+'/written/'+s['id']).json()
def answer(c,g,s,q,options=None,text='',key=None):
    return post(c,base(g)+f"/written/{s['id']}/answers",{'expected_version':s['version'],'question_id':q['question_id'],
        'option_ids':options or [],'text':text},key)
def end(c,g,s,action='finish'):return post(c,base(g)+f"/written/{s['id']}/{action}",{'expected_version':s['version']})


@pytest.mark.parametrize('change',[
    {'schema_version':1}, {'items':[{'item_id':'x','kind':'single_choice','roles':['java-backend'],'title':'题','question':'问题','options':[]}]},
    {'key':['Z']},{'key':['A','B']},{'key':['B','B']},
],ids=['requires-v2','requires-options','foreign-key','multiple-single','duplicate-key'])
def test_choice_contract_rejects_ambiguous_or_unsupported_keys(change):
    data=document(change['key']) if 'key' in change else document()
    if 'key' not in change:data.update(change)
    with pytest.raises(ValidationError):BankDocument.model_validate(data)


def test_v1_canonical_documents_do_not_gain_choice_fields():
    from test_question_banks import document as text_document
    value=normalized_document(BankDocument.model_validate(text_document()))
    assert 'options' not in value['items'][0] and 'correct_option_ids' not in value['items'][0]


# F07Z-71: reference points are an optional, author-owned field. They are never written into a
# normalized document unless the author declared them, so every existing bank keeps its hash.
def test_reference_points_are_optional_and_stay_out_of_undeclared_documents():
    from test_question_banks import document as text_document
    plain=normalized_document(BankDocument.model_validate(text_document()))
    assert 'reference_points' not in plain['items'][0]
    declared={**text_document(),'items':[{**text_document()['items'][0],
        'reference_points':['先定位竞争点','再说明隔离手段']}]}
    value=normalized_document(BankDocument.model_validate(declared))
    assert value['items'][0]['reference_points']==['先定位竞争点','再说明隔离手段']


def test_reference_points_are_rejected_on_choice_questions():
    data=document();data['items'][0]['reference_points']=['不应存在的第二套标准']
    with pytest.raises(ValidationError):BankDocument.model_validate(data)


def test_builtin_banks_do_not_gain_reference_points_in_their_hashes():
    from interview_agent.question_banks.defaults import defaults
    for bank in defaults().values():
        assert all('reference_points' not in item for item in bank['document']['items']), bank['bank_id']


def test_derive_reference_points_prefers_declared_points_and_filters_product_notes():
    from interview_agent.written.question_sets import derive_reference_points
    assert derive_reference_points({'reference':'旧的写法','reference_points':['作者要点','第二条']})==['作者要点','第二条']
    item={'reference':'先用哈希表记录已见值；再边遍历边查补数。本题不自动进行语义评分。','reference_points':[]}
    assert derive_reference_points(item)==['先用哈希表记录已见值','再边遍历边查补数']
    assert derive_reference_points({'reference':'','reference_points':[]})==[]
    many={'reference':'；'.join(f'要点{i}' for i in range(9)),'reference_points':[]}
    assert len(derive_reference_points(many))==6


@pytest.mark.postgres
@pytest.mark.redis
def test_default_roundtrip_revisions_hidden_keys_exact_set_comparison_and_feedback(context):
    c,g=context;r=create(c,g);assert r.status_code==202,r.text
    s=r.json();q=s['questions'];assert len(q)==3 and s['report'] is None
    assert '"correct_option_ids"' not in json.dumps(s) and '"reference"' not in json.dumps(s)
    bad=answer(c,g,s,q[0],['A','B']);assert bad.status_code==400
    r=answer(c,g,s,q[0],['B']);assert r.status_code==201,r.text;s=r.json()['session']
    key=uuid4().hex;old=s
    r=answer(c,g,s,q[1],['C','A'],key=key);assert r.status_code==201,r.text;s=r.json()['session']
    replay=answer(c,g,old,q[1],['A','C'],key=key);assert replay.status_code==200
    assert replay.json()['session']['version']==s['version']
    s=answer(c,g,s,q[1],[]).json()['session']
    s=answer(c,g,s,q[1],['A','C']).json()['session']
    text='我会把请求身份和结果持久化放在同一事务，并验证响应丢失后的重试。'
    s=answer(c,g,s,q[2],text=text).json()['session']
    assert s['report'] is None and len(s['answer_events'])==5
    s=end(c,g,s).json();assert s['status']=='COMPLETED'
    report=s['report'];assert report['declared_key_question_count']==2 and report['declared_key_match_count']==2
    assert report['items'][2]['status']=='requires_human_review' and report['items'][2]['answer']['text']==text
    assert not report['text_semantic_grading_performed'] and not report['ability_certified']
    assert view(c,g,s)['report']==report
    assert answer(c,g,s,q[0],['A']).status_code==409
    feedback=c.get(base(g)+'/workflow').json()['written_tests'][0]
    assert feedback['declared_key_match_count']==2 and feedback['qualification']=='declared_key_comparison_not_verified_ability'


@pytest.mark.postgres
@pytest.mark.redis
def test_report_lists_reference_points_and_keeps_grading_flags_false(context):
    c,g=context;s=create(c,g).json();q=s['questions']
    for item in q[:2]:s=answer(c,g,s,item,['B']).json()['session']
    s=answer(c,g,s,q[2],text='我先说竞争点，再说隔离手段与失败后的恢复。').json()['session']
    s=end(c,g,s).json();report=s['report']
    graded=next(i for i in report['items'] if i['status']=='requires_human_review')
    assert graded['reference_points'] and all(isinstance(p,str) and p for p in graded['reference_points'])
    assert report['text_semantic_grading_performed'] is False and report['ability_certified'] is False
    assert 'reference_points' not in graded['declared_correct_option_ids']


@pytest.mark.postgres
@pytest.mark.redis
def test_missing_key_and_text_reference_are_not_invented_scores(context):
    c,g=context;data=document([]);data['items'].append({'item_id':'text','kind':'short_answer','roles':['java-backend'],'title':'说明','question':'请说明你的思路。'})
    b=imported(c,data);s=create(c,g,[b]).json()
    s=answer(c,g,s,s['questions'][0],['A']).json()['session']
    s=answer(c,g,s,s['questions'][1],text='我的原始思路。').json()['session']
    s=end(c,g,s).json();assert s['report']['declared_key_question_count']==0
    assert [i['status'] for i in s['report']['items']]==['ungraded_missing_key','recorded_without_reference']


@pytest.mark.postgres
@pytest.mark.redis
def test_partial_multiple_answer_has_no_partial_credit_and_unanswered_remains_visible(context):
    c,g=context;s=create(c,g).json();s=answer(c,g,s,s['questions'][1],['A']).json()['session'];s=end(c,g,s).json()
    report=s['report'];assert report['declared_key_question_count']==2 and report['declared_key_match_count']==0
    assert [i['status'] for i in report['items']]==['unanswered','does_not_match_declared_key','unanswered']


@pytest.mark.postgres
@pytest.mark.redis
def test_deadline_cancel_and_goal_change_have_distinct_outcomes(context,pg_settings):
    c,g=context;s=create(c,g).json()
    query(pg_settings[1],"UPDATE app.career_written_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(s['id'],))
    expired=view(c,g,s);assert expired['status']=='COMPLETED' and expired['stop_reason']=='deadline' and expired['report']
    assert answer(c,g,expired,expired['questions'][0],['B']).status_code==409
    s=create(c,g).json();cancelled=end(c,g,s,'cancel').json()
    assert cancelled['status']=='CANCELLED' and cancelled['report'] is None
    s=create(c,g).json();post(c,base(g)+'/messages',{'expected_version':1,'message':'更改本轮目标'})
    stale=view(c,g,s);assert stale['status']=='STALE' and stale['report'] is None


@pytest.mark.postgres
@pytest.mark.redis
def test_user_versions_do_not_change_started_set_and_disabled_blocks_new_session(context):
    c,g=context;b=imported(c,document());s=create(c,g,[b]).json()
    data=document(['A']);data['items'][0]['question']='新版题干'
    p=preview(c,data).json();updated=commit(c,p,{'bank_id':b['bank_id'],'expected_version':1}).json()
    row=next(b0 for b0 in listing(c) if b0['bank_id']==b['bank_id'])
    c.patch('/api/question-banks/'+b['bank_id'],json={'expected_revision':row['revision'],'enabled':False})
    assert view(c,g,s)['questions'][0]['question']==document()['items'][0]['question']
    s=answer(c,g,s,s['questions'][0],['B']).json()['session'];s=end(c,g,s).json()
    assert s['report']['declared_key_match_count']==1
    assert create(c,g,[updated]).json()['code']=='question_bank_disabled'


@pytest.mark.postgres
@pytest.mark.redis
def test_owned_answers_report_immutable_and_untrusted_scope_rejected(context,pg_settings):
    c,g=context;s=create(c,g).json()
    fake={**s['questions'][0],'question_id':'not-in-this-session'}
    assert answer(c,g,s,fake,['A']).status_code==400
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin;uid=signup(other)['user']['id']
        assert other.get(base(g)+f"/written/{s['id']}").status_code==404
        assert answer(other,g,s,s['questions'][0],['B']).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))
    s=answer(c,g,s,s['questions'][0],['B']).json()['session'];s=end(c,g,s).json()
    with pytest.raises(CheckViolation):query(pg_settings[1],'UPDATE app.career_written_reports SET content_sha256=%s WHERE session_id=%s RETURNING session_id',('changed',s['id']))
    event=s['answer_events'][0]
    with pytest.raises(CheckViolation):query(pg_settings[1],'UPDATE app.career_written_answers SET question_id=%s WHERE id=%s RETURNING id',('changed',event['id']))


@pytest.mark.postgres
@pytest.mark.redis
def test_answer_racing_finish_cannot_modify_frozen_report(context):
    c,g=context;s=create(c,g).json()
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(answer,c,g,s,s['questions'][0],['B']);f=pool.submit(end,c,g,s)
        ar,fr=a.result(),f.result()
    assert sorted([ar.status_code,fr.status_code]) in ([200,409],[201,409])
    saved=view(c,g,s)
    if saved['status']=='ACTIVE':saved=end(c,g,saved).json()
    report=saved['report'];assert report['declared_key_match_count']==(1 if ar.status_code==201 else 0)
    assert answer(c,g,saved,saved['questions'][0],['A']).status_code==409
    assert view(c,g,saved)['report']==report


@pytest.mark.postgres
@pytest.mark.redis
def test_code_and_question_answers_cannot_cross_modes_and_legacy_hash_is_preserved(code_context,pg_settings):
    from test_written_repository import submit as code_submit,create as code_create
    from interview_agent.career.repository import digest
    c,g=code_context;s=create(c,g).json()
    assert code_submit(c,g,s).json()['code']=='written_code_session_required'
    assert query(pg_settings[1],'SELECT count(*) AS n FROM app.career_written_submissions WHERE session_id=%s',(s['id'],))[0]['n']==0
    end(c,g,s,'cancel')
    code=code_create(c,g).json()
    assert answer(c,g,code,{'question_id':'irrelevant'},text='文本回答').json()['code']=='written_question_set_required'
    row=query(pg_settings[1],'SELECT request_hash FROM app.career_written_sessions WHERE id=%s',(code['id'],))[0]
    assert row['request_hash']==digest({'expected_version':1,'task_id':None,'duration_seconds':900})
