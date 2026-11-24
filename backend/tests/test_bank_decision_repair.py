"""Synthetic probes of CORE-06-21 refusal classes, not a replay of missing live args."""
import asyncio
import json
from copy import deepcopy
from uuid import uuid4
import pytest
from interview_agent.interviews.agent import interview, InterviewFailure
from interview_agent.interviews.catalog import topic
from interview_agent.interviews.contracts import question_payload
from interview_agent.runtime.types import AssistantResponse, ToolCall
from test_interview_agent import scope, observation


def bank_scope():
    s=scope(answer="我记得需要开启检查点，但不确定持久化配置。")
    s['policy']['max_questions']=2
    # Use opaque IDs and a base-only private bank, as in explicit bank selection.
    old=s['pack']['topics']['java-concurrency']
    s['pack']['topics']={}
    for tid in ('b-'+uuid4().hex,'b-'+uuid4().hex):
        s['pack']['topics'][tid]={**old,'probe':'','retest':'','bank_item':{'item_id':tid},'has_reference':True}
    current,next_tid=list(s['pack']['topics'])
    s['pack']['topic_order']=[current,next_tid]
    s['current_question'].update(topic_id=current,question_id=current+'-base')
    return s,next_tid


class BankRepair:
    def __init__(self,s,tid,change,repair=True):
        self.s,self.tid,self.change,self.repair=s,tid,change,repair
        self.n=0;self.feedback=None
    async def complete(self,messages,tools):
        self.n+=1
        if self.n==1:name,args='read_interview_scope',{}
        elif self.n==2:name,args='read_topics',{'topic_ids':[self.tid]}
        else:
            args={'action':'ask_question','topic_id':self.tid,'question_id':self.tid+'-base','observation':observation(self.s)}
            if self.n==3:args.update(self.change)
            else:
                self.feedback=json.loads(messages[-1].text)
                if self.repair:
                    options=self.feedback['bank_submission_options']
                    option=next(x for x in options if x['topic_id']==self.tid)
                    args.update(option['questions'][0]['submission'])
                else:args.update({'question_id':'not-a-registered-question'})
            name='submit_interview_action'
        return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=deepcopy(args))])


def test_original_refusal_classes_are_reproducible_without_live_arguments():
    s,tid=bank_scope();p=BankRepair(s,tid,{'question_text':'SYNTHETIC-PRIVATE-CONTENT'},False)
    with pytest.raises(InterviewFailure,match='interview_model_limit') as caught:
        asyncio.run(interview(s,p,asyncio.Event()))
    assert [t['error_code'] for t in caught.value.diagnostic['tool_log'] if t['status']=='error']==['bank_question_text_is_frozen','question_not_in_pack']
    assert 'SYNTHETIC-PRIVATE-CONTENT' not in str(caught.value.diagnostic)


@pytest.mark.parametrize('change',[{'question_text':'SYNTHETIC-PRIVATE-CONTENT'},{'question_id':'invented-probe'},{'fact_id':str(uuid4())}])
def test_bank_rejection_can_be_repaired_in_existing_four_request_budget(change):
    s,tid=bank_scope();p=BankRepair(s,tid,change)
    d=asyncio.run(interview(s,p,asyncio.Event()))
    assert p.n==4 and d.question_id==tid+'-base' and d.fact_id is None and not d.question_text
    assert d.observation.answer_id==s['answer']['id']
    assert question_payload(s,d)['text']==s['pack']['topics'][tid]['base']
    assert 'SYNTHETIC-PRIVATE-CONTENT' not in json.dumps(p.feedback,ensure_ascii=False)
    assert 'bank_submission_options' not in str(d.tool_log)


def test_bank_topic_supplies_exact_submission_without_fabricated_followups():
    s,tid=bank_scope();t=topic(s,tid)
    assert len(t['questions'])==1
    assert t['questions'][0]['submission']=={'action':'ask_question','topic_id':tid,'question_id':tid+'-base','question_text':'','fact_id':None,'explanation':'','summary':''}
    assert t['question_text_policy']=='frozen_use_registered_text'


def test_read_bank_blueprint_can_submit_next_question_without_a_repair():
    s,tid=bank_scope()
    class Reader:
        n=0
        async def complete(self,messages,tools):
            self.n+=1
            if self.n==1:name,args='read_interview_scope',{}
            elif self.n==2:name,args='read_topics',{'topic_ids':[tid]}
            else:
                t=json.loads(messages[-1].text)['topics'][0]
                name,args='submit_interview_action',{**t['questions'][0]['submission'],'observation':observation(s)}
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])
    p=Reader();d=asyncio.run(interview(s,p,asyncio.Event()))
    assert p.n==3 and d.question_id==tid+'-base'


def test_repair_does_not_disclose_unread_bank_or_offer_an_already_asked_question():
    s,tid=bank_scope()
    class Unread:
        n=0;feedback=None
        async def complete(self,messages,tools):
            self.n+=1
            if self.n==1:name,args='read_interview_scope',{}
            else:
                if self.n>=3:self.feedback=json.loads(messages[-1].text)
                name,args='submit_interview_action',{'action':'ask_question','topic_id':tid,'question_id':'invented','observation':observation(s)}
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])
    p=Unread()
    with pytest.raises(InterviewFailure,match='interview_model_limit'):
        asyncio.run(interview(s,p,asyncio.Event()))
    options=p.feedback['bank_submission_options']
    assert options==[{'topic_id':s['current_question']['topic_id'],'questions':[]}]
    assert tid not in json.dumps(options)
