"""Explicit synthetic first-question probes, never a replay of CORE-06-05."""
import asyncio
import json
import pytest
from interview_agent.interviews.agent import interview
from interview_agent.interviews.catalog import topic
from interview_agent.interviews.contracts import validate_decision
from interview_agent.runtime.types import AssistantResponse,ToolCall
from test_core_quality import mixed


def test_project_blueprint_discloses_a_valid_submission_without_guessing_ids():
    s=mixed();t=topic(s,'project')
    q=t['questions'][0]
    args={**q['submission'], 'fact_id':t['references'][0]['id']}
    assert validate_decision(s,args).question_id==q['id']=='project-contribution'
    assert args['question_text']==args['explanation']=='' and args['observation'] is None
    for focus in t['project_focuses']:
        assert validate_decision(s,{**args,'project_focus':focus}).project_focus==focus


@pytest.mark.parametrize('invalid_field,value,error',[
    (None,None,None),
    ('question_text','Synthetic private wording','invalid_project_question'),
    ('project_focus','unsupported-focus','invalid_interview_decision_schema'),
    ('fact_id','00000000-0000-0000-0000-000000000001','fact_not_in_scope'),
])
def test_model_can_read_blueprint_and_repair_one_rejection_within_original_cap(invalid_field,value,error):
    s=mixed()
    class Reader:
        n=0
        async def complete(self,messages,tools):
            self.n+=1
            if self.n==1:name,args='read_interview_scope',{}
            elif self.n==2:name,args='read_topics',{'topic_ids':['project']}
            elif self.n==3:
                t=json.loads(messages[-1].text)['topics'][0]
                self.good={**t['questions'][0]['submission'],'fact_id':t['references'][0]['id']}
                name,args='submit_interview_action',dict(self.good)
                if invalid_field:args[invalid_field]=value
            else:
                feedback=json.loads(messages[-1].text)
                assert feedback['error']==error
                name,args='submit_interview_action',self.good
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])
    p=Reader();d=asyncio.run(interview(s,p,asyncio.Event()))
    assert p.n==(4 if invalid_field else 3) and d.topic_id=='project'
    assert [t.get('error_code') for t in d.tool_log if t['status']=='error']==([error] if error else [])


def test_project_question_identifier_is_normalised_not_retried():
    s=mixed();t=topic(s,'project');q=t['questions'][0]
    base={**q['submission'],'fact_id':t['references'][0]['id']}
    for wrong in ('','project','custom-project'):
        assert validate_decision(s,{**base,'question_id':wrong}).question_id=='project-contribution'
    with pytest.raises(ValueError,match='invalid_project_question'):
        validate_decision(s,{**base,'question_text':'自定义文字'})


def test_rejection_feedback_carries_the_project_blueprint():
    s=mixed()
    class Reader:
        n=0
        good=None
        feedback=None
        async def complete(self,messages,tools):
            self.n+=1
            if self.n==1:return AssistantResponse(tool_calls=[ToolCall(id='1',name='read_interview_scope',arguments={})])
            if self.n==2:return AssistantResponse(tool_calls=[ToolCall(id='2',name='read_topics',arguments={'topic_ids':['project']})])
            if self.n==3:
                t=json.loads(messages[-1].text)['topics'][0]
                self.good={**t['questions'][0]['submission'],'fact_id':t['references'][0]['id']}
                return AssistantResponse(tool_calls=[ToolCall(id='3',name='submit_interview_action',
                    arguments={**self.good,'question_text':'模型自己写的题面'})])
            self.feedback=json.loads(messages[-1].text)
            return AssistantResponse(tool_calls=[ToolCall(id='4',name='submit_interview_action',arguments=self.good)])
    p=Reader();d=asyncio.run(interview(s,p,asyncio.Event()))
    blueprint=p.feedback.get('project_blueprint')
    assert blueprint and blueprint['question_id']=='project-contribution'
    assert blueprint['question_text']=='' and blueprint['fact_id']
    assert p.n==4 and d.topic_id=='project' and d.question_id=='project-contribution'
    assert [t.get('error_code') for t in d.tool_log if t['status']=='error']==['invalid_project_question']
