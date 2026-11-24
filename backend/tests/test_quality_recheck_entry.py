"""Current policy/output provenance guards, distinct new authorization stage."""
import argparse
from pathlib import Path
import pytest
from test_live_core_entry import entry
from test_remaining_core_entry import training


def current():
    s=training();s['policy']['feedback_summary']={'version':'project-extractive-v1'}
    s['events'][2]['payload']={'text':'我描述了事务提交，但未说明验证时点。'}
    s['events'][3]['payload']={'text':'技术回答'}
    o=s['observations'][0];o.update(topic_id='project',summary='个人经历未核验。原回答节选：「我描述了事务提交，但未说明验证时点。」。',summary_origin='server_answer_excerpt')
    s['observations'][1]['topic_id']='sql-isolation'
    s['report']={'summary_origin':'server_session_counts','model_summary':'本轮结束，已出题2道。个人经历未核验。'}
    app={'review':{'training_hypotheses':[{'summary':o['summary'],'summary_origin':o['summary_origin'],'evidence_refs':['observation:'+o['id']]}]}}
    return s,app


def test_new_stage_has_distinct_ids_same_bounded_remaining_tasks():
    m=entry();assert m['STAGES']['core-06-09']==('core-06-09-20261102-cny15','core0609')
    p=m['stage_plan']('core-06-09');assert p['scripted_preparation'] and p['max_cny']=='1.5'
    assert sum(p['limits'].values())==1_500_000_000 and 'materials' not in p['limits']
    ps=[cls.load(m['ROOT']/f'configs/core0609-{role}.json') for role,cls in [('career',m['CareerPolicy']),('interviews',m['InterviewPolicy'])]]
    assert [p.max_http_attempts for p in ps]==[3,8]


def test_complete_current_source_metadata_passes():entry()['validate_summary_result'](*current())


@pytest.mark.parametrize('kind',['policy','excerpt','answer','origin','report','application_origin','application_summary'])
def test_missing_or_changed_grounding_evidence_is_not_machine_accepted(kind):
    s,a=current();o=s['observations'][0]
    if kind=='policy':s['policy'].pop('feedback_summary')
    elif kind=='excerpt':o['summary']='模型说我提交前已验证成功。'
    elif kind=='answer':o['answer_id']='foreign'
    elif kind=='origin':o.pop('summary_origin')
    elif kind=='report':s['report'].pop('summary_origin')
    elif kind=='application_origin':a['review']['training_hypotheses'][0].pop('summary_origin')
    else:a['review']['training_hypotheses'][0]['summary']='其它摘要'
    with pytest.raises(RuntimeError):entry()['validate_summary_result'](s,a)


def test_new_stage_still_requires_its_explicit_budget_before_configuration_reads():
    args=argparse.Namespace(stage='core-06-09',mode='live',execute_live=True,max_cny='2',credentials=Path('must-not-read'),mock_unknown=False)
    with pytest.raises(ValueError,match='live_requires_explicit_stage_approval_flags'):entry()['preflight'](args)
