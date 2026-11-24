"""User declarations only; these commands never submit an external application."""
from typing import Annotated, Literal
from urllib.parse import urlsplit
from pydantic import Field, field_validator, model_validator
from interview_agent.contracts.common import Contract, PositiveInt
from interview_agent.contracts.practice import UuidString

Status = Literal['DRAFT','SUBMITTED','INTERVIEW','OFFER','REJECTED','WITHDRAWN','UNKNOWN']
Text = Annotated[str, Field(min_length=1,max_length=600)]

class PackageInput(Contract):
    expected_version: PositiveInt
    expected_material_revision: Annotated[int, Field(ge=0,le=30)]
    company: Annotated[str, Field(min_length=1,max_length=100)]
    title: Annotated[str, Field(min_length=1,max_length=100)]
    application_url: Annotated[str, Field(max_length=2048)] = ''
    draft_id: UuidString | None = None
    source_id: UuidString | None = None

    @field_validator('company','title')
    @classmethod
    def nonblank(cls,v):
        if not v.strip() or '\x00' in v: raise ValueError('nonblank_text_required')
        return v.strip()

    @field_validator('application_url')
    @classmethod
    def entry(cls,v):
        if not v: return v
        u=urlsplit(v)
        if u.scheme!='https' or not u.hostname or u.username or u.password or any(c.isspace() for c in v):
            raise ValueError('https_entry_required')
        return v

    @model_validator(mode='after')
    def exclusive(self):
        if self.draft_id and self.source_id: raise ValueError('choose_draft_or_source')
        return self

class ReplacePackage(PackageInput):
    expected_application_version: PositiveInt

class Progress(Contract):
    expected_application_version: PositiveInt
    status: Status
    note: Text
    evidence_kind: Literal['user_confirmation','receipt_note','site_sync']
    receipt_reference: Annotated[str, Field(max_length=300)] = ''
    is_correction: bool = False
    interview_round: Annotated[str, Field(max_length=80)] = ''

    @model_validator(mode='after')
    def evidence(self):
        if not self.note.strip() or '\x00' in self.note: raise ValueError('note_required')
        if self.evidence_kind in ('receipt_note','site_sync') and not self.receipt_reference.strip(): raise ValueError('receipt_reference_required')
        if self.interview_round and self.status!='INTERVIEW': raise ValueError('interview_status_required')
        return self

class Availability(Contract):
    expected_application_version: PositiveInt
    availability: Literal['open','closed','unknown']
    note: Text

    @field_validator('note')
    @classmethod
    def nonblank(cls,v):
        if not v.strip() or '\x00' in v: raise ValueError('note_required')
        return v

def transition(current,status,correction=False):
    allowed={'DRAFT':{'SUBMITTED','WITHDRAWN','UNKNOWN'},
             'SUBMITTED':{'INTERVIEW','OFFER','REJECTED','WITHDRAWN','UNKNOWN'},
             'INTERVIEW':{'OFFER','REJECTED','WITHDRAWN','UNKNOWN'},
             'UNKNOWN':{'SUBMITTED','INTERVIEW','OFFER','REJECTED','WITHDRAWN'}}
    if current!=status and not correction and status not in allowed.get(current,set()):
        raise ValueError('application_transition_invalid')
    return status
