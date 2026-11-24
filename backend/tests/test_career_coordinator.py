import asyncio
from uuid import uuid4
import pytest
from pydantic import ValidationError
from interview_agent.career.contracts import GoalContext,GoalInput,GoalReply,validate_decision
from interview_agent.career.coordinator import coordinate
from interview_agent.runtime.scripted import ScriptedProvider
from interview_agent.runtime.types import AssistantResponse,ToolCall


def context():
    return GoalContext(goal_id=str(uuid4()),user_id=str(uuid4()),version=1,
        messages=[{'id':str(uuid4()),'text':'我想在上海找AI实习。'}],conditions={},jd_text='')


def call(name,args=None,identity=None):
    return AssistantResponse(tool_calls=[ToolCall(id=identity or uuid4().hex,name=name,arguments=args or {})])


def test_real_graph_requires_read_then_grounds_candidate_not_commit():
    c=context(); field={'value':'上海','quote':'上海','message_id':c.messages[0].id}
    provider=ScriptedProvider([call('read_goal'),call('ask_user',{'text':'你偏应用还是算法？','fields':{'cities':[field]}})])
    decision,trace=asyncio.run(coordinate(c,provider,asyncio.Event()))
    assert decision.type=='ask_user' and decision.fields.cities[0].value=='上海'
    assert [t['tool'] for t in trace]==['read_goal','ask_user'] and len(provider.requests)==2


@pytest.mark.parametrize('field',[{'value':'深圳','quote':'上海'}, {'value':'上海','quote':'不存在的原文'}])
def test_forged_extraction_rejected(field):
    c=context()
    with pytest.raises(ValueError):
        validate_decision(c,{'type':'ask_user','text':'问题','fields':{'cities':[{**field,'message_id':c.messages[0].id}]}})


def test_unread_action_is_observation_error_then_can_correct():
    p=ScriptedProvider([call('ask_user',{'text':'问题'}),call('read_goal'),call('ask_user',{'text':'具体方向？'})])
    decision,trace=asyncio.run(coordinate(context(),p,asyncio.Event()))
    assert decision.type=='ask_user' and trace[0]['status']=='error' and len(p.requests)==3


def test_unknown_tools_stop_at_three_requests_and_never_execute_external_action():
    p=ScriptedProvider([call('send_resume') for _ in range(3)])
    with pytest.raises(ValueError,match='request_limit'): asyncio.run(coordinate(context(),p,asyncio.Event()))
    assert len(p.requests)==3


def test_direct_jd_can_prepare_training_without_resume_or_direction():
    c=context().model_copy(update={'jd_text':'合成JD：实现Java接口。'})
    p=ScriptedProvider([call('read_goal'),call('prepare_training',{'text':'准备JD训练'})])
    result,_=asyncio.run(coordinate(c,p,asyncio.Event()))
    assert result.type=='prepare_training'


def test_cancelled_coordinator_cannot_return_candidate():
    async def scenario():
        event=asyncio.Event(); event.set()
        with pytest.raises(ValueError,match='cancelled'):
            await coordinate(context(),ScriptedProvider([call('read_goal')]),event)
    asyncio.run(scenario())


@pytest.mark.parametrize('body',[{'message':'  '},{'message':'hello','user_id':str(uuid4())},
    {'message':'hello','conditions':{'direction':'Java','allowed_tools':['shell']}}])
def test_public_inputs_reject_ambiguous_or_identity_control(body):
    with pytest.raises(ValidationError): GoalInput.model_validate(body)


def test_reply_version_is_strict_and_omitted_conditions_preserve_signal():
    assert GoalReply(message='更新城市',expected_version=1).conditions is None
    with pytest.raises(ValidationError): GoalReply(message='更新城市',expected_version=True)


def test_confirmed_conditions_normalize_whitespace_without_inventing_fields():
    value=GoalInput(message='方向不确定',conditions={'direction':'  ','cities':[' 上海 ','上海']})
    assert value.conditions.direction=='' and value.conditions.cities==['上海']
