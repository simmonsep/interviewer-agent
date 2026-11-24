"""Reject false acceptance of incomplete training/review; no real provider or Key."""
from copy import deepcopy
import argparse
from pathlib import Path
import pytest
from test_live_core_entry import entry


def training():
    return {'id':'session','status':'COMPLETED','policy':{'coverage':{'version':'project-technical-v1'}},
        'events':[{'kind':'question','payload':{'topic_id':t}} for t in ('project','sql-isolation')]
                 +[{'kind':'answer','id':a} for a in ('answer-1','answer-2')],
        'observations':[{'id':'obs-'+str(n),'answer_id':'answer-'+str(n),'evidence_quote':'引用','answer_quote':'回答','assisted':False} for n in (1,2)]}


def test_new_stage_caps_only_remaining_roles_and_does_not_reuse_previous_phase():
    m=entry();plan=m['stage_plan']('core-06-07');phase,profile=m['STAGES']['core-06-07']
    assert plan=={'limits':{'career':500_000_000,'interviews':1_000_000_000},'max_cny':'1.5','scripted_preparation':True}
    assert phase=='core-06-07-20261101-cny15'
    policies=[cls.load(m['ROOT']/f'configs/{profile}-{role}.json') for role,cls in [('career',m['CareerPolicy']),('interviews',m['InterviewPolicy'])]]
    assert [p.max_http_attempts for p in policies]==[3,8]
    assert [p.max_operation_attempts for p in policies]==[3,4]


@pytest.mark.parametrize('budget',['0.5','2'])
def test_new_live_stage_refuses_previous_stage_budget_before_config_or_key_read(budget):
    args=argparse.Namespace(stage='core-06-07',mode='live',execute_live=True,max_cny=budget,credentials=Path('must-not-read'),mock_unknown=False)
    with pytest.raises(ValueError,match='live_requires_explicit_stage_approval_flags'):entry()['preflight'](args)


def test_complete_mixed_unassisted_training_passes_machine_gate():
    entry()['validate_training_result'](training())


@pytest.mark.parametrize('kind',['incomplete','coverage','missing_observation','foreign_answer','assisted','empty_reference','explanation'])
def test_machine_gate_does_not_count_incomplete_or_wrong_evidence_as_training_success(kind):
    s=training()
    if kind=='incomplete':s['status']='FAILED'
    elif kind=='coverage':s['events'][0]['payload']['topic_id']='java-concurrency'
    elif kind=='missing_observation':s['observations'].pop()
    elif kind=='foreign_answer':s['observations'][0]['answer_id']='foreign'
    elif kind=='assisted':s['observations'][0]['assisted']=True
    elif kind=='empty_reference':s['observations'][0]['evidence_quote']=''
    else:s['events'].append({'kind':'explanation'})
    with pytest.raises(RuntimeError):entry()['validate_training_result'](s)


def review():
    app={'id':'application','status':'SUBMITTED','version':2,'package':{'frozen':'material'},'current_package_revision':1,'submitted_package_revision':1}
    goal={'status':'WAITING_FOR_USER','application_plan_state':'current','action':{'type':'prepare_application_action','application_plan':{
        'execution':'proposal_only','application_id':app['id'],'evidence_refs':['session:session','observation:obs-1']}}}
    return goal,app,training(),deepcopy(app)


def test_proposal_with_current_session_evidence_and_unchanged_application_passes():
    entry()['validate_application_review'](*review())


@pytest.mark.parametrize('kind',['clarification','outdated','external_action','foreign_application','foreign_session','no_observation','progress_changed','package_changed'])
def test_review_gate_requires_actual_current_evidence_without_application_side_effect(kind):
    goal,app,s,original=review();plan=goal['action']['application_plan']
    if kind=='clarification':goal['action']['type']='ask_user'
    elif kind=='outdated':goal['application_plan_state']='outdated'
    elif kind=='external_action':plan['execution']='submit'
    elif kind=='foreign_application':plan['application_id']='other'
    elif kind=='foreign_session':plan['evidence_refs']=['session:other','observation:obs-1']
    elif kind=='no_observation':plan['evidence_refs']=['session:session']
    elif kind=='progress_changed':app['status']='REJECTED'
    else:app['package']={'frozen':'changed'}
    with pytest.raises(RuntimeError):entry()['validate_application_review'](goal,app,s,original)
