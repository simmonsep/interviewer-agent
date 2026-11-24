"""Additional business episodes. Keep the v0.1/v0.2 contracts and review hashes stable."""
from typing import Annotated, Literal
from pydantic import Field
from interview_agent.contracts.common import Contract
from interview_agent.career.contracts import Conditions
from .contracts import Attempt

DomainId = Annotated[str, Field(pattern=r'^[RDIX][0-9]{2}$')]


class DomainInput(Contract):
    workflow: Literal['research', 'materials', 'interview', 'coding', 'written']
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    conditions: Conditions = Field(default_factory=Conditions)
    jd_text: Annotated[str, Field(max_length=2500)] = ''
    source_behavior: Literal['normal', 'partial', 'unavailable'] = 'normal'
    requirements: Annotated[str, Field(max_length=1000)] = '掌握Java与SQL。'
    source_text: Annotated[str, Field(max_length=4000)] = ''
    confirm_count: Annotated[int, Field(ge=0, le=12)] = 0
    append_source: bool = False
    role: Literal['java-backend', 'ai-application'] = 'java-backend'
    mode: Literal['interview', 'coaching'] = 'interview'
    answers: Annotated[list[Annotated[str, Field(min_length=1, max_length=1500)]], Field(max_length=2)] = []
    stop: bool = False
    task_id: Literal['ttl-boundary', 'order-cancel', 'idempotency-record', 'lock-owner-expiry'] = 'ttl-boundary'
    decision: Literal['accept', 'reject', 'none'] = 'none'
    source_code: Annotated[str, Field(max_length=8192)] = ''


class DomainTask(Contract):
    id: DomainId
    family: Annotated[str, Field(min_length=1, max_length=80)]
    split: Literal['development', 'reserved_candidate']
    capability: Literal['job_research', 'grounded_materials', 'interview_training', 'execution_feedback']
    provenance: Literal['project_authored_synthetic'] = 'project_authored_synthetic'
    review: Literal['pending_human_review'] = 'pending_human_review'
    input: DomainInput


class DomainOracle(Contract):
    task_id: DomainId
    expected: Annotated[dict, Field(min_length=1)]
    required_guards: Annotated[list[str], Field(min_length=1)]
    human_rubric: Annotated[list[str], Field(min_length=1)]


class DomainAttempt(Attempt):
    task_id: Annotated[str, Field(pattern=r'^[GAMRDIX][0-9]{2}$')]
