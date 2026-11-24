import json
import os
from uuid import uuid4
from pathlib import Path
import pytest
from pydantic import ValidationError
from interview_agent.question_banks.contracts import BankDocument
from interview_agent.execution.docker_java import DockerJava,DockerProfile
from test_postgres import arun,query,client_for,signup
from test_worker import worker_settings
from test_written_repository import written,pg_settings,post,base,view,submit,end,process,FakeExecutor,IMAGE
from test_question_banks import imported,preview,commit,listing


def document():
    return json.loads((Path(__file__).resolve().parents[2]/'fixtures/question-banks/example-java-private.json').read_text(encoding='utf-8'))


def body(bank):return {'expected_version':1,'role':'java-backend','programming_task':{**bank,'item_id':'sum-two'}}
def validate(c,g,bank,key=None):return post(c,base(g)+'/written-programming-validations',body(bank),key)
def create(c,g,bank,key=None):return post(c,base(g)+'/written',body(bank),key)


@pytest.mark.parametrize('field,value',[
    ('profile','arbitrary-shell'),('image','ubuntu:latest'),('command','cat /etc/passwd'),
    ('cases',[{'id':'public-1','visibility':'public','input':'1 2','output':'3'},
              {'id':'public-2','visibility':'public','input':'2 3','output':'5'}]),
    ('reference_solution',''),
],ids=['profile','image','command','missing-hidden','empty-reference'])
def test_programming_manifest_rejects_execution_authority_and_missing_evidence(field,value):
    data=document();data['items'][0]['programming'][field]=value
    with pytest.raises(ValidationError):BankDocument.model_validate(data)


def test_schema_version_and_duplicate_cases_are_rejected():
    data=document();data['schema_version']=2
    with pytest.raises(ValidationError):BankDocument.model_validate(data)
    data=document();data['items'][0]['programming']['cases']*=2
    with pytest.raises(ValidationError):BankDocument.model_validate(data)


@pytest.mark.postgres
@pytest.mark.redis
def test_import_is_inert_reference_validation_gates_practice_and_reuses_worker(written,worker_settings,pg_settings):
    c,g=written;bank=imported(c,document())
    assert query(pg_settings[1],'SELECT count(*) AS n FROM app.career_written_sessions WHERE goal_id=%s',(g['id'],))[0]['n']==0
    assert create(c,g,bank).json()['code']=='programming_task_validation_required'
    key=uuid4().hex;r=validate(c,g,bank,key);assert r.status_code==202,r.text
    v=r.json();assert v['kind']=='programming_validation' and not v['validation']['passed']
    assert validate(c,g,bank,key).json()['id']==v['id']
    assert submit(c,g,v).json()['code']=='reference_validation_is_immutable'
    arun(process(worker_settings,FakeExecutor()));v=view(c,g,v)
    assert v['status']=='COMPLETED' and v['validation']['passed']
    assert v['task_provenance']['item_id']=='sum-two'
    row=next(x for x in listing(c) if x['bank_id']==bank['bank_id'])
    c.patch('/api/question-banks/'+bank['bank_id'],json={'expected_revision':row['revision'],'enabled':False})
    assert create(c,g,bank).json()['code']=='question_bank_disabled'
    row=next(x for x in listing(c) if x['bank_id']==bank['bank_id'])
    c.patch('/api/question-banks/'+bank['bank_id'],json={'expected_revision':row['revision'],'enabled':True})
    r=create(c,g,bank);assert r.status_code==202,r.text;s=r.json()
    assert s['selection_basis']['validation_submission_id'] and s['kind']=='imported_programming'
    assert 'check-negative' not in json.dumps(s) and json.dumps(document()['items'][0]['programming']['reference_solution']) not in json.dumps(s)
    submit(c,g,s,source='public class Main { public static void main(String[] a) {} }')
    arun(process(worker_settings,FakeExecutor('WRONG_ANSWER')));s=view(c,g,s)
    assert s['submissions'][0]['result']['execution_error'] is None
    s=end(c,g,s).json();assert s['submissions'][0]['result']['verdict']=='WRONG_ANSWER'
    assert 'check-negative' in json.dumps(s)


@pytest.mark.postgres
@pytest.mark.redis
def test_failed_reference_and_new_version_never_reuse_old_validation(written,worker_settings):
    c,g=written;bank=imported(c,document());v=validate(c,g,bank).json()
    arun(process(worker_settings,FakeExecutor('WRONG_ANSWER')));v=view(c,g,v)
    assert v['status']=='COMPLETED' and not v['validation']['passed']
    assert create(c,g,bank).json()['code']=='programming_task_validation_required'
    v=validate(c,g,bank).json();arun(process(worker_settings,FakeExecutor()));assert view(c,g,v)['validation']['passed']
    updated=document();updated['items'][0]['question']='第二版题意；需要重新验证。'
    p=preview(c,updated).json();b2=commit(c,p,{'bank_id':bank['bank_id'],'expected_version':1}).json()
    assert create(c,g,b2).json()['code']=='programming_task_validation_required'
    assert create(c,g,bank).status_code==202


@pytest.mark.postgres
@pytest.mark.redis
def test_owner_image_and_cancel_boundaries_do_not_qualify(written,worker_settings,pg_settings):
    c,g=written;bank=imported(c,document());v=validate(c,g,bank).json();end(c,g,v,'cancel')
    assert create(c,g,bank).json()['code']=='programming_task_validation_required'
    v=validate(c,g,bank).json();arun(process(worker_settings,FakeExecutor()));assert view(c,g,v)['validation']['passed']
    # A different server-selected pinned digest cannot borrow this validation.
    other_settings=pg_settings[0].model_copy(update={'written_execution_image':'eclipse-temurin@sha256:'+'b'*64})
    with client_for(other_settings) as other:
        other.headers['Origin']=other_settings.origin;other.cookies.update(c.cookies)
        assert create(other,g,bank).json()['code']=='programming_task_validation_required'
    foreign_settings=pg_settings[0].model_copy(update={'written_execution_image':IMAGE})
    with client_for(foreign_settings) as foreign:
        foreign.headers['Origin']=foreign_settings.origin;uid=signup(foreign)['user']['id']
        fg=post(foreign,'/api/career-goals/',{'message':'另一个用户'}).json()
        assert validate(foreign,fg,bank).status_code==404
        assert foreign.get(base(g)+f"/written/{v['id']}").status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


@pytest.mark.postgres
@pytest.mark.redis
def test_unknown_reference_execution_is_terminal_and_does_not_qualify(written,worker_settings,pg_settings):
    from interview_agent.written.repository import WrittenRepository
    from test_written_repository import notification
    from test_worker import resources
    c,g=written;bank=imported(c,document());v=validate(c,g,bank).json();message=notification(pg_settings[1],v['id'])
    async def started():
        async with resources(worker_settings) as (old,q):
            repo=WrittenRepository(old.pool);claim=await repo.claim(message)
            assert await repo.begin(claim)
    arun(started())
    query(pg_settings[1],"UPDATE app.career_written_submissions SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(message.operation_id,))
    async def reclaim():
        async with resources(worker_settings) as (old,q):assert await WrittenRepository(old.pool).claim(message) is None
    arun(reclaim());v=view(c,g,v)
    assert v['status']=='COMPLETED' and not v['validation']['passed']
    assert v['submissions'][0]['error_code']=='execution_outcome_unknown'
    assert create(c,g,bank).json()['code']=='programming_task_validation_required'


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.docker
def test_real_imported_java_validation_and_candidate_fail_then_pass(pg_settings,worker_settings):
    profile_path=os.environ.get('PY_INTERVIEW_EXECUTOR_PROFILE')
    if not profile_path:pytest.skip('Set the existing pinned local Docker profile')
    profile=DockerProfile.load(profile_path);executor=DockerJava(profile)
    arun(executor.readiness())
    settings=pg_settings[0].model_copy(update={'written_execution_image':profile.image})
    with client_for(settings) as c:
        c.headers['Origin']=settings.origin;uid=signup(c)['user']['id']
        g=post(c,'/api/career-goals/',{'message':'导入Java题的真实隔离验证'}).json()
        bank=imported(c,document());v=validate(c,g,bank).json()
        arun(process(worker_settings,executor));v=view(c,g,v)
        assert v['validation']['passed'] and v['submissions'][0]['result']['verdict']=='ACCEPTED',v
        s=create(c,g,bank).json()
        submit(c,g,s,source='public class Main { public static void main(String[] args) { System.out.println(0); } }')
        arun(process(worker_settings,executor));s=view(c,g,s)
        assert s['submissions'][0]['result']['execution_error'] is None
        submit(c,g,s,source=document()['items'][0]['programming']['reference_solution'])
        arun(process(worker_settings,executor));s=end(c,g,view(c,g,s)).json()
        assert [x['result']['verdict'] for x in s['submissions']]==['WRONG_ANSWER','ACCEPTED'],s
        assert all(x['result']['evidence']['image']==profile.image for x in s['submissions'])
        Path('.private/core03b-01/real-docker-result.json').write_text(json.dumps(s,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))
