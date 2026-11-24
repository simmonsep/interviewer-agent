"""Synthetic missing-current/next patterns, not replay of uncaptured CORE-06-09 args."""
import asyncio,json
import pytest
from interview_agent.interviews.agent import interview,InterviewFailure
from interview_agent.runtime.types import AssistantResponse,ToolCall
from interview_agent.interviews.contracts import Decision
from test_project_summary import answered_project,next_decision


class Plan:
    def __init__(self,pattern):self.s=answered_project();self.d=next_decision(self.s);self.pattern=pattern;self.n=0;self.feedback=[]
    async def complete(self,messages,tools):
        self.n+=1
        if self.n==1:name,args='read_interview_scope',{}
        elif self.n==2:
            self.scope_view=json.loads(messages[-1].text)
            name,args='read_topics',{'topic_ids':['http-idempotency' if self.pattern=='next_only' else 'project']}
        elif self.n==3:name,args='submit_interview_action',self.d.model_dump()
        else:
            self.feedback.append(json.loads(messages[-1].text))
            name,args='read_topics',{'topic_ids':['http-idempotency']}
        return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])


def test_next_topic_read_alone_can_submit_with_current_evidence_prepared_in_scope():
    p=Plan('next_only');d=asyncio.run(interview(p.s,p,asyncio.Event()))
    assert p.n==3 and d.topic_id=='http-idempotency'
    assert p.scope_view['current_topic']['id']=='project'
    assert p.scope_view['current_topic']['references'][0]['id']==p.s['current_question']['fact_id']


def test_missing_next_remains_rejected_but_has_safe_role_diagnostics_and_original_cap():
    p=Plan('current_only')
    with pytest.raises(InterviewFailure,match='interview_model_limit') as err:asyncio.run(interview(p.s,p,asyncio.Event()))
    assert p.n==4
    entry=next(t for t in err.value.diagnostic['tool_log'] if t['status']=='error')
    assert entry['missing_topic_reads']=={'current':False,'next':True}
    assert p.feedback[0]['missing_topic_reads']==entry['missing_topic_reads']
    assert p.s['answer']['text'] not in str(err.value.diagnostic)


def test_early_missing_next_can_read_and_resubmit_within_four_requests():
    p=Plan('next_only')
    class Repair:
        n=0
        async def complete(self,messages,tools):
            self.n+=1
            if self.n==1:name,args='read_interview_scope',{}
            elif self.n==2:name,args='submit_interview_action',p.d.model_dump()
            elif self.n==3:
                assert json.loads(messages[-1].text)['missing_topic_reads']=={'current':False,'next':True}
                name,args='read_topics',{'topic_ids':['http-idempotency']}
            else:name,args='submit_interview_action',p.d.model_dump()
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])
    provider=Repair();d=asyncio.run(interview(p.s,provider,asyncio.Event()))
    assert provider.n==4 and d.topic_id=='http-idempotency'


def test_finish_with_answer_uses_delivered_current_reference_in_two_requests():
    p=Plan('next_only');s=p.s;s['policy']['max_questions']=1;s['policy'].pop('coverage')
    d=p.d.model_copy(update={'action':'finish','topic_id':'','question_id':'','summary':'结束。'})
    class Finish:
        n=0
        async def complete(self,messages,tools):
            self.n+=1
            if self.n==1:name,args='read_interview_scope',{}
            else:
                refs=json.loads(messages[-1].text)['current_topic']['references']
                assert len(refs)==1 and refs[0]['id']==s['current_question']['fact_id']
                name,args='submit_interview_action',d.model_dump()
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])
    provider=Finish();assert asyncio.run(interview(s,provider,asyncio.Event())).action=='finish'
    assert provider.n==2
