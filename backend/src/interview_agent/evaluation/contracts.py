from typing import Annotated,Literal
from pydantic import Field
from interview_agent.contracts.common import Contract
from interview_agent.career.contracts import Conditions

TaskId=Annotated[str,Field(pattern=r'^(G|A|M)[0-9]{2}$')]

class ApplicationSetup(Contract):
    status:Literal['DRAFT','SUBMITTED','INTERVIEW','OFFER','REJECTED','UNKNOWN']='DRAFT'
    closed:bool=False
    training:Literal['none','interview','coaching']='none'
    retract:Literal['none','first','all']='none'
    change_goal:bool=False
    multiple:bool=False
    progress_note:Annotated[str,Field(max_length=600)]|None=None

class PublicInput(Contract):
    message:Annotated[str,Field(min_length=1,max_length=2000)]
    conditions:Conditions=Field(default_factory=Conditions)
    jd_text:Annotated[str,Field(max_length=2500)]=''
    application:ApplicationSetup|None=None

class Task(Contract):
    id:TaskId
    family:Annotated[str,Field(min_length=1,max_length=80)]
    split:Literal['development','reserved_candidate']
    capability:Literal['goal_selection','application_action','evidence_selection']
    provenance:Literal['project_authored_synthetic']='project_authored_synthetic'
    review:Literal['pending_human_review']='pending_human_review'
    input:PublicInput

class Oracle(Contract):
    task_id:TaskId
    decision_types:Annotated[list[str],Field(min_length=1)]
    action_kinds:list[str]=[]
    training_evidence_required:bool=False
    assisted_evidence_required:bool=False
    human_rubric:Annotated[list[str],Field(min_length=1)]

class Attempt(Contract):
    task_id:TaskId
    adapter:Literal['system_scripted','fixed_rules','system_live','fixed_prefetch_live']
    repeat:Annotated[int,Field(ge=1,le=3)]
    status:Literal['completed','product_failure','infrastructure_failure','authorization_stopped','not_run']
    output:dict=Field(default_factory=dict)
    provider_steps:Annotated[int,Field(ge=0)]=0
    setup_provider_steps:Annotated[int,Field(ge=0)]=0
    decision_elapsed_ms:Annotated[float,Field(ge=0)]=0
    error_code:str|None=None
    model_http_calls:Annotated[int,Field(ge=0)]|None=0
    model_cost_cny:Literal[0]|None=0
    model_cost_upper_nano:Annotated[int,Field(ge=0)]|None=0
    model_usage_status:Literal['not_called','known','unknown']='not_called'
