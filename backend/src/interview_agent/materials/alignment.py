"""Grounded requirement assessments are model proposals, not ability certificates."""
from typing import Annotated,Literal
from pydantic import Field,field_validator
from interview_agent.contracts.common import Contract
from interview_agent.contracts.practice import UuidString
from interview_agent.interviews.catalog import TOPICS


class Requirement(Contract):
    requirement_quote:Annotated[str,Field(min_length=1,max_length=300)]
    evidence_status:Literal['evidence_present','needs_clarification','not_covered']
    fact_ids:Annotated[list[UuidString],Field(max_length=6)]=[]
    question:Annotated[str,Field(max_length=300)]=''
    topic_ids:Annotated[list[Annotated[str,Field(min_length=1,max_length=40)]],Field(max_length=3)]=[]


class AlignmentArgs(Contract):
    requirements:Annotated[list[Requirement],Field(min_length=1,max_length=6)]


def target_text(scope):
    target=scope['target']
    return target.get('text') if target['kind']=='user_jd' else target.get('requirements','') if target['kind']=='selected_job' else ''


def ground_requirement(item,scope):
    text=target_text(scope) or '';quote=item.requirement_quote
    start=text.find(quote)
    if not quote.strip() or start<0 or text.find(quote,start+1)>=0:raise ValueError('exact_unique_requirement_required')
    if len(set(item.fact_ids))!=len(item.fact_ids) or not set(item.fact_ids)<={f['id'] for f in scope['facts']}:raise ValueError('confirmed_fact_scope_required')
    if len(set(item.topic_ids))!=len(item.topic_ids) or not set(item.topic_ids)<=set(TOPICS):raise ValueError('registered_training_topic_required')
    if item.evidence_status=='evidence_present' and (not item.fact_ids or item.question):raise ValueError('evidence_required')
    if item.evidence_status=='needs_clarification' and not item.question.strip():raise ValueError('clarification_question_required')
    if item.evidence_status=='not_covered' and (item.fact_ids or item.question):raise ValueError('uncovered_has_no_evidence')
    return {**item.model_dump(mode='json'),'start_char':start,'end_char':start+len(quote)}


def validate_alignment(scope,result):
    if scope['mode']!='align' or not result.alignment_items or result.proposals or result.fact_ids or result.question:
        raise ValueError('invalid_alignment')
    positions=set()
    for value in result.alignment_items:
        item=Requirement.model_validate({k:value[k] for k in Requirement.model_fields})
        checked=ground_requirement(item,scope)
        if checked!=value or checked['start_char'] in positions:raise ValueError('alignment_evidence_changed')
        positions.add(checked['start_char'])


class ClarificationAnswer(Contract):
    requirement_index:Annotated[int,Field(ge=0,le=5)]
    text:Annotated[str,Field(min_length=1,max_length=600)]

    @field_validator('text')
    @classmethod
    def readable(cls,value):
        value=value.replace('\r\n','\n').replace('\r','\n')
        if not value.strip() or any(ord(c)<32 and c not in '\n\t' for c in value) or '\ufffd' in value:
            raise ValueError('readable_answer_required')
        return value
