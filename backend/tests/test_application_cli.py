import json
from uuid import uuid4
import pytest
from interview_agent.materials.cli import ResumeClient,parser
from test_postgres import arun,pg_settings
from test_worker import worker_settings
from test_application_repository import context
from test_interview_repository import process

pytestmark=pytest.mark.postgres


def commands(c,g,tmp_path):
    state=tmp_path/'application-session.json';cli=ResumeClient(c,state);cli.saved['goal_id']=g['id'];cli.save()
    def run(*args):return ResumeClient(c,state).execute(parser().parse_args(['--state',str(state),*map(str,args)]))
    return run


def edit(run,aid,kind,file,**patch):
    run('application-edit-file',aid,'--kind',kind,'--output',file)
    doc=json.loads(file.read_text());doc['command'].update(patch);file.write_text(json.dumps(doc),encoding='utf-8')


def test_progress_feedback_frozen_package_and_stale_file(context,tmp_path):
    c,g=context;run=commands(c,g,tmp_path)
    src=tmp_path/'source.md';src.write_text('我负责SQL事务。',encoding='utf-8');sid=run('upload',src)['source_id']
    a=run('application-create','--company','合成公司','--title','Java','--source-id',sid)
    aid=a['id'];assert run('applications')['items']
    file=tmp_path/'progress.json';edit(run,aid,'progress',file,status='SUBMITTED',note='用户记录：已手动投递，尚无招聘方反馈。')
    updated=run('application-record',file);assert updated['submitted_package_revision']==1
    with pytest.raises(ValueError,match='application_version_conflict'):run('application-record',file)
    package=tmp_path/'package.json';edit(run,aid,'package',package)
    with pytest.raises(ValueError,match='application_package_frozen'):run('application-record',package)
    availability=tmp_path/'availability.json';edit(run,aid,'availability',availability,availability='closed',note='用户记录页面关闭。')
    run('application-record',availability)
    review=tmp_path/'review.json';r=run('application-review',aid,'--output',review)
    assert r['status']=='SUBMITTED' and r['review']['origin']=='rule_based_ja08' and review.exists()
    assert run('application-show',aid)['package']==a['package']


@pytest.mark.redis
def test_application_bound_completed_training_enters_review_without_changing_progress(context,worker_settings,tmp_path):
    c,g=context;run=commands(c,g,tmp_path)
    a=run('application-create','--company','合成公司','--title','Java');aid=a['id']
    s=run('interview-start','--role','java-backend','--application-id',aid,'--package-revision','1','--interview-round','一面训练','--questions','1')
    assert s['application']['id']==aid and s['application']['package_revision']==1
    arun(process(worker_settings));file=tmp_path/'answer.json';run('interview-answer-file','--output',file)
    d=json.loads(file.read_text());d['command']['text']='复合更新要有原子性，并验证并发边界。';file.write_text(json.dumps(d),encoding='utf-8')
    run('interview-submit',file);arun(process(worker_settings))
    review=run('application-review',aid)
    assert review['status']=='DRAFT' and len(review['review']['training_hypotheses'])==1
    h=review['review']['training_hypotheses'][0]
    assert h['binding']['package_revision']==1 and h['verification']=='training_observation_not_rejection_cause'


def test_draft_package_replacement_and_wrong_goal_are_explicit(context,tmp_path):
    c,g=context;run=commands(c,g,tmp_path);a=run('application-create','--company','合成公司','--title','Java')
    src=tmp_path/'source.md';src.write_text('新增Redis实现。',encoding='utf-8');sid=run('upload',src)['source_id']
    file=tmp_path/'package.json';edit(run,a['id'],'package',file,source_id=sid)
    updated=run('application-record',file);assert updated['current_package_revision']==2 and len(updated['packages'])==2
    assert updated['packages'][0]['payload']['material']['kind']=='none'
    doc=json.loads(file.read_text());doc['goal_id']=str(uuid4());file.write_text(json.dumps(doc),encoding='utf-8')
    with pytest.raises(ValueError,match='不属于'):run('application-record',file)
