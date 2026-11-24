import json
from uuid import uuid4
import pytest
from interview_agent.materials.cli import ResumeClient,parser
from test_postgres import arun,pg_settings
from test_worker import worker_settings
from test_interview_repository import context,process

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


def commands(c,g,tmp_path):
    state=tmp_path/'interview-session.json';cli=ResumeClient(c,state);cli.saved['goal_id']=g['id'];cli.save()
    def run(*args):return ResumeClient(c,state).execute(parser().parse_args(['--state',str(state),*map(str,args)]))
    return run


def test_no_resume_two_question_interview_and_versioned_answer_files(context,worker_settings,tmp_path):
    c,g=context;run=commands(c,g,tmp_path)
    s=run('interview-start','--role','java-backend','--questions','2')
    assert s['material_fact_ids']==[] and run('interviews')['items']
    with pytest.raises(ValueError,match='等待回答'):run('interview-answer-file','--output',tmp_path/'early.json')
    arun(process(worker_settings));assert run('interview-show')['observations']==[]
    with pytest.raises(ValueError,match='报告尚未生成'):run('interview-report','--output',tmp_path/'early-report.json')
    for n in range(2):
        file=tmp_path/f'answer{n}.json';run('interview-answer-file','--output',file)
        doc=json.loads(file.read_text());doc['command']['text']='检查与更新应保证原子性，并验证并发边界。';file.write_text(json.dumps(doc),encoding='utf-8')
        run('interview-submit',file);arun(process(worker_settings))
        with pytest.raises(ValueError,match='409'):run('interview-submit',file)
    out=tmp_path/'report.json';r=run('interview-report','--output',out)
    assert r['status']=='COMPLETED' and r['report']['answers']==2 and out.exists()
    assert run('interview-use',s['id'])['id']==s['id']


def test_coaching_stop_and_answer_target_binding(context,worker_settings,tmp_path):
    c,g=context;run=commands(c,g,tmp_path)
    s=run('interview-start','--role','ai-application','--mode','coaching');arun(process(worker_settings))
    assert run('interview-show')['policy']['mode']=='coaching'
    file=tmp_path/'answer.json';run('interview-answer-file','--output',file)
    doc=json.loads(file.read_text());doc['session_id']=str(uuid4());file.write_text(json.dumps(doc),encoding='utf-8')
    with pytest.raises(ValueError,match='不属于'):run('interview-submit',file)
    stopped=run('interview-stop');assert stopped['report'] is not None and stopped['status']!='WAITING_FOR_USER'


def test_current_interview_is_persisted_per_goal(context,worker_settings,tmp_path):
    c,g=context;run=commands(c,g,tmp_path);s=run('interview-start','--role','java-backend')
    other=run('goal-create','--direction','AI','--intent','training')['goal_id']
    with pytest.raises(ValueError,match='先interview-start'):run('interview-show')
    run('goal-use',g['id']);assert run('interview-show')['id']==s['id'];run('interview-stop')


@pytest.mark.parametrize('text', ['', '   ', 'x' * 1501], ids=['empty', 'blank', 'too_long'])
def test_invalid_answer_text_is_explained_before_http(tmp_path, text):
    import httpx

    goal_id, session_id, question_id = (str(uuid4()) for _ in range(3))
    answer_file = tmp_path / 'answer.json'
    answer_file.write_text(json.dumps({
        'goal_id': goal_id,
        'session_id': session_id,
        'command': {'expected_version': 1, 'question_id': question_id, 'text': text},
    }), encoding='utf-8')

    def no_request(request):
        pytest.fail('invalid answer sent to API')

    with httpx.Client(transport=httpx.MockTransport(no_request)) as client:
        cli = ResumeClient(client, tmp_path / '.private' / 'session.json')
        cli.saved.update(goal_id=goal_id, interviews={goal_id: session_id})
        with pytest.raises(ValueError, match=r'command\.text'):
            cli.execute(parser().parse_args(['interview-submit', str(answer_file)]))
