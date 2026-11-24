"""CORE-06-07 captured synthetic wording; regenerated IDs, no provider replay."""
from uuid import uuid4
import pytest
from interview_agent.interviews.contracts import Decision,validate_decision,question_payload
from test_core_quality import mixed,project
from test_interview_agent import scope
from interview_agent.interviews.catalog import topic
from interview_agent.interviews.agent import scope_view

ANSWER='我亲自实现SQL事务中的请求记录，用唯一约束和事务提交验证重复请求；不确定的边界仍需测试。'
UNSUPPORTED='本回答说明亲自实现SQL事务内请求记录、唯一约束与提交前验证重复请求，并指出边界需进一步测试；个人贡献表述具体但边界测试仅为待办。'


def answered_project(text=ANSWER,*,enabled=True):
    s=mixed();q={'id':str(uuid4()),**question_payload(s,project(s))}
    s.update(question_history=[q],current_question=q,answer={'id':str(uuid4()),'text':text,'assisted':False})
    if enabled:s['policy']['feedback_summary']={'version':'project-extractive-v1'}
    return s


def next_decision(s,summary=UNSUPPORTED):
    return Decision(action='ask_question',topic_id='http-idempotency',question_id='http-idempotency-base',observation={
        'answer_id':s['answer']['id'],'label':'supported_in_this_answer','summary':summary,
        'answer_quote':s['answer']['text'][:250],'evidence_id':s['facts'][0]['id'],'evidence_quote':s['facts'][0]['quote']})


def test_captured_unsupported_timing_is_not_committed_as_project_fact_summary():
    s=answered_project();d=validate_decision(s,next_decision(s))
    assert '提交前' not in d.observation.summary
    assert ANSWER in d.observation.summary and '未核验' in d.observation.summary
    assert d.observation.label=='supported_in_this_answer'


def test_true_precommit_words_in_original_answer_are_preserved_not_keyword_filtered():
    text='我在提交前做了验证，但没有给出验证结果。';s=answered_project(text)
    d=validate_decision(s,next_decision(s,'我获得了99%的提升。'))
    assert text in d.observation.summary and '99%' not in d.observation.summary


def test_excerpt_comes_from_original_answer_not_a_model_selected_fragment_and_marks_truncation():
    text='我没有确认测试成功。'+('边界仍待验证。'*50);s=answered_project(text)
    value=next_decision(s);value.observation.answer_quote='确认测试成功'
    summary=validate_decision(s,value).observation.summary
    assert '我没有确认测试成功。' in summary and '节选' in summary and '…' in summary
    assert len(summary)<=200


def test_legacy_policy_keeps_old_summary_semantics_without_rewriting_history():
    s=answered_project(enabled=False)
    assert validate_decision(s,next_decision(s)).observation.summary==UNSUPPORTED


def test_new_project_policy_finish_summary_reports_counts_without_free_experience_claims():
    s=answered_project();s['policy']['max_questions']=1;s['policy'].pop('coverage')
    d=next_decision(s);d.action='finish';d.topic_id=d.question_id='';d.summary='已经证明提交前验证成功。'
    out=validate_decision(s,d)
    assert '已经证明' not in out.summary and '提交前' not in out.summary
    assert '1' in out.summary and '未核验' in out.summary


def test_project_quote_and_reference_checks_still_reject_bad_evidence():
    s=answered_project();d=next_decision(s);d.observation.answer_quote='我成功验证了提交前。'
    with pytest.raises(ValueError,match='answer_citation_required'):validate_decision(s,d)


def test_missing_reference_notice_survives_new_session_count_summary():
    s=scope(answer='我只记录自己的思路。');s['policy'].update(max_questions=1,feedback_summary={'version':'project-extractive-v1'})
    item=s['pack']['topics']['java-concurrency'];item.update(bank_item={'id':'synthetic'},has_reference=False)
    s['current_question']['has_reference']=False
    ref=topic(s,'java-concurrency')['references'][0]
    d=Decision(action='finish',summary='旧模型自由概括。',observation={'answer_id':s['answer']['id'],'label':'inconclusive',
        'summary':'暂不判断','answer_quote':s['answer']['text'],'evidence_id':ref['id'],'evidence_quote':ref['text']})
    result=validate_decision(s,d)
    assert result.observation.summary=='本题未提供参考；仅保存原回答，不作正确性判断。'
    assert '未提供参考' in result.summary and '不作正确性判断' in result.summary


def test_memory_view_truncation_keeps_unverified_notice_before_excerpt():
    s=answered_project('个人回答。'*90);d=validate_decision(s,next_decision(s))
    s['prior_observations']=[{'id':'synthetic-observation','summary':d.observation.summary,'answer_quote':s['answer']['text']}]
    view=scope_view(s)
    assert '未核验' in view['prior_observations'][0]['summary']
    assert 'prior_observations.synthetic-observation.summary' in view['model_view_truncation']
