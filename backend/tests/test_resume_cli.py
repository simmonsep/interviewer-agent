import json
from uuid import uuid4
import pytest
from interview_agent.materials.cli import ResumeClient,parser
from test_postgres import arun,pg_settings,client_for,query
from test_worker import worker_settings
from test_material_repository import process
from test_material_files import docx


@pytest.mark.postgres
@pytest.mark.redis
def test_daily_commands_persist_login_import_edit_conflict_and_export(pg_settings,worker_settings,tmp_path):
    state=tmp_path/'session.json';uid=None
    with client_for(pg_settings[0]) as c:
        def run(*args):return ResumeClient(c,state).execute(parser().parse_args(['--state',str(state),*map(str,args)]),password='Synthetic-password-42!')
        try:
            uid=run('register','--email',uuid4().hex+'@example.com','--name','CLI')['user']['id']
            assert 'password' not in state.read_text() and 'token' in json.loads(state.read_text())
            c.cookies.clear();assert run('login','--email',query(pg_settings[1],'SELECT email FROM auth.users WHERE id=%s',(uid,))[0]['email'])['user']['id']==uid
            jd=tmp_path/'jd.txt';jd.write_text('Java SQL Redis',encoding='utf-8')
            g=run('goal-create','--direction','Java','--jd',jd)['goal_id']
            assert run('goal-use',g)['goal_id']==g and run('goals')['items']
            text=tmp_path/'project.md';text.write_text('我负责SQL事务和Redis通知。',encoding='utf-8')
            run('upload',text);run('rewrite');arun(process(worker_settings))
            d=run('status')['drafts'][0]['id'];assert run('show',d)['resume']
            edit=tmp_path/'edit.json';run('edit-file',d,'--output',edit)
            v=json.loads(edit.read_text());v['command']['items'][0]['text']='基于SQL事务与Redis构建通知链路。';edit.write_text(json.dumps(v),encoding='utf-8')
            edited=run('save-edit',edit)['draft_id']
            with pytest.raises(ValueError,match='draft_version_conflict'):run('save-edit',edit)
            accepted=run('accept',edited)['draft_id'];assert run('show',accepted)['version']==3
            restored=run('restore',d)['draft_id'];out=tmp_path/'resume.md';run('export',restored,'--output',out)
            assert out.read_text(encoding='utf-8')==run('show',d)['resume']
            with pytest.raises(FileExistsError):run('export',d,'--output',out)
            file=tmp_path/'resume.docx';file.write_bytes(docx('我负责订单服务。'))
            preview=run('preview',file);assert preview['requires_import_confirmation'] and preview['text']
            assert run('status')['material_revision']==1
            run('import-preview',preview['preview_id']);assert run('status')['material_revision']==2
            # A new API client resumes the locally persisted session and goal.
            with client_for(pg_settings[0]) as other:
                resumed=ResumeClient(other,state);assert resumed.materials()['material_revision']==2
                run('logout')
                with pytest.raises(ValueError,match='401'):resumed.materials()
        finally:
            if uid:query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


def test_state_must_be_private_and_cli_has_no_password_argument(tmp_path):
    import httpx
    with httpx.Client() as c:
        with pytest.raises(ValueError,match='private'):ResumeClient(c,__import__('pathlib').Path.cwd()/'public-session.json')
    with pytest.raises(SystemExit):parser().parse_args(['login','--email','a@example.com','--password','secret'])


@pytest.mark.postgres
@pytest.mark.redis
def test_executable_cli_over_real_local_http(pg_settings,worker_settings,tmp_path,project_root):
    import socket,subprocess,sys,time,asyncio
    import httpx
    from interview_agent.worker.queue import QueueTransport
    from interview_agent.contracts.operations import QueueMessage
    from interview_agent.materials.repository import MaterialRepository
    from interview_agent.materials.worker import worker_for
    from psycopg_pool import AsyncConnectionPool
    from psycopg.rows import dict_row
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1',3100))==0:pytest.skip('3100 occupied; do not stop unknown API')
    import os
    proc=subprocess.Popen([sys.executable,'-m','interview_agent.api','--config',os.environ['PY_INTERVIEW_TEST_CONFIG']],cwd=project_root,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    state=tmp_path/'network-session.json';uid=None;nonce=uuid4().hex
    async def owned_worker(run_id,kind='materials'):
        pool=AsyncConnectionPool(worker_settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row});redis=worker_settings.redis.connect()
        stream=f'py-interview:test:{nonce}:jobs:v1:{kind}'
        try:
            await pool.open(wait=True)
            async with pool.connection() as conn:
                if kind=='materials':row=await (await conn.execute('SELECT * FROM app.career_material_runs WHERE id=%s',(run_id,))).fetchone()
                elif kind=='research':row=await (await conn.execute('SELECT * FROM app.career_research_runs WHERE id=%s',(run_id,))).fetchone()
                else:row=await (await conn.execute("SELECT * FROM app.career_interview_runs WHERE session_id=%s AND status='PENDING' ORDER BY created_at,id LIMIT 1",(run_id,))).fetchone()
            q=QueueTransport(redis,stream)
            await q.publish(QueueMessage(operation_id=str(row['id']),job_id=str(row['job_id']),resource_version=row['goal_version']))
            if kind=='materials':worker=worker_for(MaterialRepository(pool),q,provider='scripted')
            elif kind=='research':
                from interview_agent.career.research_repository import ResearchRepository
                from interview_agent.career.research_worker import worker_for as research_worker
                def forbidden(request):pytest.fail('JD import must not perform external HTTP')
                worker=research_worker(ResearchRepository(pool),q,inner_factory=lambda:httpx.MockTransport(forbidden),origin='offline_fixture')
            else:
                from interview_agent.interviews.repository import InterviewRepository
                from interview_agent.interviews.worker import worker_for as interview_worker
                worker=interview_worker(InterviewRepository(pool),q,provider='scripted')
            assert await worker.once()
        finally:
            await redis.delete(stream);await redis.aclose();await pool.close()
    try:
        with httpx.Client(base_url='http://127.0.0.1:3100',trust_env=False,timeout=10) as c:
            for _ in range(100):
                try:
                    if c.get('/health').status_code<500:break
                except httpx.TransportError:pass
                if proc.poll() is not None:pytest.fail('test API exited during startup')
                time.sleep(.1)
            else:pytest.fail('test API startup timeout')
            cli=ResumeClient(c,state)
            uid=cli.execute(parser().parse_args(['register','--email',nonce+'@example.com','--name','Network']),password='Synthetic-password-42!')['user']['id']
            def command(*args):
                r=subprocess.run([sys.executable,'-X','utf8','-m','interview_agent.materials.cli','--state',str(state),*map(str,args)],cwd=project_root,capture_output=True,text=True,encoding='utf-8',timeout=30)
                assert r.returncode==0,r.stdout+r.stderr
                return json.loads(r.stdout)
            jd=tmp_path/'network-jd.txt';jd.write_text('Java后端实习，SQL与Redis。',encoding='utf-8')
            command('goal-create','--direction','Java','--intent','research','--jd',jd)
            research=command('research','--kind','import_jd');arun(owned_worker(research['run_id'],'research'))
            item=command('jobs')['items'][0];choice=tmp_path/'network-choice.json'
            command('job-choice-file','--run-id',item['run_id'],'--snapshot-id',item['snapshot_id'],'--output',choice)
            selected=json.loads(choice.read_text());selected['command']['acknowledge_unknown']=True;choice.write_text(json.dumps(selected),encoding='utf-8')
            command('job-select',choice)
            file=tmp_path/'network.md';file.write_text('我负责订单服务，使用SQL和Redis。',encoding='utf-8')
            command('upload',file);run=command('rewrite');arun(owned_worker(run['run_id']))
            d=command('status')['drafts'][0]['id'];assert command('show',d)['resume']
            out=tmp_path/'network-export.md';command('export',d,'--output',out);assert out.exists()
            app=command('application-create','--company','合成公司','--title','Java实习','--draft-id',d)
            progress=tmp_path/'network-progress.json';command('application-edit-file',app['id'],'--kind','progress','--output',progress)
            v=json.loads(progress.read_text());v['command'].update(status='SUBMITTED',note='合成用户手动记录投递。');progress.write_text(json.dumps(v),encoding='utf-8')
            command('application-record',progress)
            interview=command('interview-start','--role','java-backend','--questions','1','--application-id',app['id'],'--interview-round','模拟一面')
            arun(owned_worker(interview['id'],'interviews'))
            shown=command('interview-show')
            assert shown['resume_evidence'][0]['resume_source']['draft_id']==d
            assert any(e['kind']=='question' and e['payload']['kind']=='project' for e in shown['events'])
            answer=tmp_path/'network-answer.json';command('interview-answer-file','--output',answer)
            doc=json.loads(answer.read_text());doc['command']['text']='我会使用原子操作。';answer.write_text(json.dumps(doc),encoding='utf-8')
            command('interview-submit',answer);arun(owned_worker(interview['id'],'interviews'))
            assert command('interview-report')['status']=='COMPLETED'
            review=command('application-review',app['id']);assert review['status']=='SUBMITTED' and review['review']['training_hypotheses']
            command('goal-create','--direction','Python Agent 开发','--intent','training')
            fixture=project_root/'fixtures/question-banks/agent-developer-trial.json'
            original=json.loads(fixture.read_text(encoding='utf-8'))
            bank_preview=command('question-bank-preview',fixture)
            assert bank_preview['requires_confirmation']
            assert all(bank['origin']=='builtin' for bank in command('question-banks')['banks'])
            bank=command('question-bank-commit',bank_preview['preview_id'])
            ref=f"{bank['bank_id']}@{bank['version']}"
            bank_interview=command('interview-start','--role','ai-application','--questions','2','--question-bank',ref)
            revised=tmp_path/'bank-v2.json'
            original['items'][0]['question']='新版：怎样保存会话检查点？'
            revised.write_text(json.dumps(original,ensure_ascii=False),encoding='utf-8')
            preview_v2=command('question-bank-preview',revised)
            bank_v2=command('question-bank-commit',preview_v2['preview_id'],'--update',ref)
            assert bank_v2=={'bank_id':bank['bank_id'],'version':2}
            assert any(row['bank_id']==bank['bank_id'] and row['version']==2 for row in command('question-banks')['banks'])

            arun(owned_worker(bank_interview['id'],'interviews'))
            frozen=command('interview-show')
            first=next(e['payload'] for e in frozen['events'] if e['kind']=='question')
            assert frozen['question_banks'][0]['version']==1
            assert first['bank_item']['bank_id']==bank['bank_id']
            assert first['text']==bank_preview['document']['items'][0]['question']
            bank_answer=tmp_path/'bank-answer.json'
            command('interview-answer-file','--output',bank_answer)
            doc=json.loads(bank_answer.read_text(encoding='utf-8'))
            doc['command']['text']='用数据库保存检查点，并沿用会话 ID。'
            bank_answer.write_text(json.dumps(doc,ensure_ascii=False),encoding='utf-8')
            command('interview-submit',bank_answer)
            arun(owned_worker(bank_interview['id'],'interviews'))
            second=command('interview-show')
            questions=[e['payload'] for e in second['events'] if e['kind']=='question']
            assert len(questions)==2 and all(q['bank_item']['version']==1 for q in questions)
            assert questions[-1]['text']==bank_preview['document']['items'][1]['question']
            assert second['status']=='WAITING_FOR_USER' and second['observations']==[]
            command('interview-stop')
            command('logout')
    finally:
        proc.terminate();proc.wait(timeout=15)
        if uid:query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


@pytest.mark.parametrize('terminal', [False, True], ids=['pipe', 'terminal'])
def test_cli_preserves_chinese_json_for_pipe_and_terminal(monkeypatch, terminal):
    from io import StringIO
    from interview_agent.materials import cli

    expected = {'document': {'question': '如何保存检查点？请说明。'}}
    output = StringIO()
    monkeypatch.setattr(output, 'isatty', lambda: terminal)
    monkeypatch.setattr(cli.sys, 'stdout', output)
    monkeypatch.setattr(cli.ResumeClient, 'execute', lambda self, args: expected)

    assert cli.main(['--state', '.private/unused-encoding-test.json', 'question-banks']) == 0
    raw_json = output.getvalue()
    assert json.loads(raw_json) == expected
    if terminal:
        assert expected['document']['question'] in raw_json
    else:
        assert raw_json.isascii()
