"""Synthetic rejection probes, not replays of the uncaptured CORE-06-05 live arguments."""
import asyncio
from copy import deepcopy
import pytest
from interview_agent.interviews.agent import interview,InterviewFailure
from interview_agent.runtime.types import AssistantResponse,ToolCall
from test_core_quality import mixed,project


class InvalidSubmission:
    def __init__(self,payload):self.payload=payload;self.n=0;self.feedback=[]
    async def complete(self,messages,tools):
        self.n+=1
        if self.n==1:name,args='read_interview_scope',{}
        elif self.n==2:name,args='read_topics',{'topic_ids':['project']}
        else:
            if self.n==5:self.feedback.append(messages[-1].text)
            name,args='submit_interview_action',deepcopy(self.payload)
        return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])


def failed(payload,s):
    provider=InvalidSubmission(payload)
    with pytest.raises(InterviewFailure,match='interview_model_limit') as failure:
        asyncio.run(interview(s,provider,asyncio.Event()))
    assert provider.n==5
    return failure.value.diagnostic,provider.feedback

def test_known_project_rejection_keeps_code_for_feedback_and_persistent_diagnostic():
    s=mixed();payload=project(s).model_dump();payload['question_text']='SYNTHETIC-PRIVATE-TEXT'
    diagnostic,feedback=failed(payload,s)
    assert [t['error_code'] for t in diagnostic['tool_log'] if t['status']=='error']==['invalid_project_question']*3
    assert 'invalid_project_question' in feedback[0]
    assert 'SYNTHETIC-PRIVATE-TEXT' not in str(diagnostic)


def test_schema_diagnostics_show_only_registered_field_paths_never_input_or_unknown_names():
    s=mixed();payload=project(s).model_dump();payload['project_focus']='SYNTHETIC-SECRET-VALUE'
    payload['SYNTHETIC-SECRET-FIELD']='SYNTHETIC-SECRET-EXTRA'
    diagnostic,feedback=failed(payload,s)
    rejected=[t for t in diagnostic['tool_log'] if t['status']=='error']
    assert all(t['error_code']=='invalid_interview_decision_schema' for t in rejected)
    assert all(t['error_fields']==['project_focus','unknown'] for t in rejected)
    assert 'SYNTHETIC-SECRET' not in str(diagnostic)+str(feedback)


# ---------------------------------------------------------------- S06A: stage + termination

def test_a_round_limit_carries_stage_reason_and_bounded_counts():
    s=mixed();payload=project(s).model_dump();payload['question_text']='SYNTHETIC-PRIVATE-TEXT'
    diagnostic,_=failed(payload,s)
    assert diagnostic['error_code']=='interview_model_limit'
    assert diagnostic['stage']=='model_round'
    assert diagnostic['termination_reason']=='model_request_limit'
    assert diagnostic['model_rounds']==5
    assert diagnostic['submission_attempts']==3
    assert diagnostic['distinct_error_codes']==['invalid_project_question']
    assert diagnostic['distinct_read_topics']>=1


class Never:
    async def complete(self,messages,tools):
        await asyncio.sleep(30)
        raise AssertionError('the timeout must fire first')


def test_cancel_before_the_first_round_is_reported_as_cancelled():
    # A pre-set or mid-round cancel is observed at the outer wait, not in the model node: the
    # graph task and the cancel task race, so the public code stays `interview_cancelled_or_timeout`
    # and the new diagnostic is what separates cancel from timeout.
    cancel=asyncio.Event();cancel.set()
    with pytest.raises(InterviewFailure,match='interview_cancelled_or_timeout') as caught:
        asyncio.run(interview(mixed(),Never(),cancel))
    diagnostic=caught.value.diagnostic
    assert (diagnostic['stage'],diagnostic['termination_reason'])==('wait','cancelled')
    assert diagnostic['model_rounds']==0


def test_a_slow_round_is_reported_as_timeout(monkeypatch):
    from interview_agent.interviews import agent
    monkeypatch.setattr(agent,'RUN_TIMEOUT_SECONDS',0.05)
    with pytest.raises(InterviewFailure,match='interview_cancelled_or_timeout') as caught:
        asyncio.run(agent.interview(mixed(),Never(),asyncio.Event()))
    diagnostic=caught.value.diagnostic
    assert (diagnostic['stage'],diagnostic['termination_reason'])==('wait','timeout')


def test_a_ledger_refusal_is_not_rewrapped_as_an_interview_failure():
    from interview_agent.billing.ledger import BillingUnavailable
    class Refused:
        async def complete(self,messages,tools):
            raise BillingUnavailable('model_authorization_unavailable')
    with pytest.raises(BillingUnavailable):
        asyncio.run(interview(mixed(),Refused(),asyncio.Event()))
