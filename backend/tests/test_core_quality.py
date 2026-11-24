"""Regressions for the two user-reviewed CORE-06-03 quality gaps; no paid provider."""
import asyncio
import json
from uuid import uuid4
import pytest
from interview_agent.career.contracts import validate_decision as goal_decision
from interview_agent.career.coordinator import coordinate
from interview_agent.interviews.contracts import Decision,validate_decision,question_payload,question_coverage
from interview_agent.interviews.agent import interview,scope_view
from interview_agent.runtime.scripted import ScriptedProvider
from interview_agent.runtime.types import AssistantResponse,ToolCall
from test_career_coordinator import context as goal_context
from test_interview_agent import scope,observation


def context():return goal_context().model_copy(update={'jd_text':'Java实习：SQL事务与接口幂等。'})


def mixed(answer=None,max_questions=2):
    s=scope(answer=answer);s['policy'].update(max_questions=max_questions,coverage={'version':'project-technical-v1'})
    s['facts']=[{'id':str(uuid4()),'quote':'我实现SQL事务中的幂等请求记录。','kind':'contribution'}]
    return s


def project(s,**extra):
    return Decision(action='ask_question',topic_id='project',question_id='project-contribution',fact_id=s['facts'][0]['id'],**extra)


@pytest.mark.parametrize('sources,confirmed',[(0,0),(1,0),(2,1)])
def test_material_status_comes_from_server_counts_and_distinguishes_confirmation(sources,confirmed):
    c=context();c.workflow_feedback={'materials':{'source_count':sources,'confirmed_fact_count':confirmed}}
    d=goal_decision(c,{'type':'prepare_materials','text':'需要你上传简历/项目材料后才能执行。'})
    if sources:
        assert f'已上传{sources}份' in d.text and f'{confirmed}条用户确认' in d.text
        assert '无需重复上传已有材料' in d.text and '新提议事实仍需另行确认' in d.text
    else:assert '尚未上传' in d.text and '可以不上传简历' in d.text
    assert d.type=='prepare_materials'


def test_clarification_and_missing_legacy_feedback_are_not_rewritten():
    c=context();c.workflow_feedback={'materials':{'source_count':1,'confirmed_fact_count':0}}
    assert goal_decision(c,{'type':'ask_user','text':'你亲自做了哪部分？'}).text=='你亲自做了哪部分？'
    c.workflow_feedback={}
    assert goal_decision(c,{'type':'prepare_materials','text':'旧上下文提议'}).text=='旧上下文提议'


def test_coordinator_graph_cannot_publish_stale_upload_instruction():
    c=context();c.workflow_feedback={'materials':{'source_count':2,'confirmed_fact_count':0}}
    def call(name,args):return AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments=args)])
    p=ScriptedProvider([call('read_goal',{}),call('prepare_materials',{'text':'请先上传简历。'})])
    d,trace=asyncio.run(coordinate(c,p,asyncio.Event()))
    assert '已上传2份' in d.text and '无需重复上传' in d.text and len(trace)==2


def test_two_slots_allow_either_first_kind_but_reserve_last_for_missing_kind():
    s=mixed();technical=Decision(action='ask_question',topic_id='java-concurrency',question_id='java-concurrency-base')
    assert validate_decision(s,technical).topic_id=='java-concurrency'
    assert validate_decision(s,project(s)).topic_id=='project'
    # Technical first: a weak answer cannot spend the last slot on another technical probe.
    s=mixed('只加volatile。');bad=Decision(action='ask_question',topic_id='java-concurrency',question_id='java-concurrency-probe',observation=observation(s))
    with pytest.raises(ValueError,match='question_coverage_required'):validate_decision(s,bad)
    assert validate_decision(s,project(s,observation=observation(s))).topic_id=='project'


def test_project_first_reserves_technical_and_larger_interview_can_still_probe():
    s=mixed();q={'id':str(uuid4()),**question_payload(s,project(s))}
    s.update(question_history=[q],current_question=q,answer={'id':str(uuid4()),'text':'我亲自实现了幂等请求记录。'})
    with pytest.raises(ValueError,match='question_coverage_required'):
        validate_decision(s,project(s,project_focus='verification',observation=observation(s)))
    assert validate_decision(s,Decision(action='ask_question',topic_id='sql-isolation',question_id='sql-isolation-base',observation=observation(s)))
    roomy=mixed('不知道。',max_questions=4)
    assert validate_decision(roomy,Decision(action='ask_question',topic_id='java-concurrency',question_id='java-concurrency-probe',observation=observation(roomy)))


def test_legacy_two_technical_questions_can_finish_but_new_policy_cannot_claim_coverage():
    s=mixed('不知道。');q=dict(s['current_question'],id=str(uuid4()),question_id='java-concurrency-probe')
    s['question_history'].append(q);s['current_question']=q
    d=Decision(action='finish',observation=observation(s),summary='本轮结束。')
    with pytest.raises(ValueError,match='question_coverage_required'):validate_decision(s,d)
    s['policy'].pop('coverage');assert question_coverage(s) is None and validate_decision(s,d)


def test_coverage_rejection_is_tool_feedback_and_can_be_corrected_within_four_requests():
    s=mixed('不知道。');o=observation(s)
    class Repair:
        n=0
        async def complete(self,messages,tools):
            self.n+=1
            if self.n==1:name,args='read_interview_scope',{}
            elif self.n==2:
                delivered=json.loads(messages[-1].text)
                assert delivered['question_coverage']['missing']==['project']
                name,args='read_topics',{'topic_ids':['java-concurrency','project']}
            elif self.n==3:name,args='submit_interview_action',Decision(action='ask_question',topic_id='java-concurrency',question_id='java-concurrency-probe',observation=o).model_dump()
            else:
                assert json.loads(messages[-1].text)['error']=='question_coverage_required'
                name,args='submit_interview_action',project(s,observation=o).model_dump()
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])
    provider=Repair();d=asyncio.run(interview(s,provider,asyncio.Event()))
    assert d.topic_id=='project' and provider.n==4 and d.tool_log[-2]['status']=='error'
    assert scope_view(s)['question_coverage']['remaining_questions']==1
