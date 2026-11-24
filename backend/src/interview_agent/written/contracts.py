from typing import Annotated, Literal
from pydantic import Field, field_validator,model_validator
from interview_agent.contracts.common import Contract
from interview_agent.question_banks.contracts import BankRef,Role,ProgrammingRef
from interview_agent.contracts.practice import UuidString


class CreateWritten(Contract):
    application_id:UuidString|None=None
    application_package_revision:Annotated[int,Field(ge=1,le=30)]|None=None
    interview_round:Annotated[str,Field(max_length=80)]=''
    retest_observation_id:UuidString|None=None
    programming_task:ProgrammingRef|None=None
    question_banks:Annotated[list[BankRef],Field(max_length=2)]=[]
    role:Role|None=None
    max_questions:Annotated[int,Field(ge=1,le=20)]=10
    # F07Z-89: pick the questions instead of taking the first N. The list is the order the user
    # chose; `max_questions` must agree with it so a request cannot mean two things.
    item_ids:Annotated[list[Annotated[str,Field(min_length=1,max_length=40)]],Field(max_length=20)]|None=None
    # F07Z-91: build a set by asking for question kinds instead of naming every item. `shuffle`
    # samples from the filtered pool; the seed and the filter are recorded in the session so the
    # choice can be audited afterwards.
    item_kinds:Annotated[list[Literal['single_choice','multiple_choice','short_answer','knowledge']],Field(max_length=4)]|None=None
    shuffle:bool=False
    expected_version: Annotated[int, Field(ge=1)]
    task_id: Literal['idempotency-record', 'lock-owner-expiry'] | None = None
    duration_seconds: Annotated[int, Field(ge=60, le=3600)] = 900

    @model_validator(mode='after')
    def mode_scope(self):
        if not self.application_id and (self.application_package_revision is not None or self.interview_round):raise ValueError('application_required')
        if self.interview_round and (not self.interview_round.strip() or '\x00' in self.interview_round):raise ValueError('invalid_interview_round')
        if self.programming_task:
            if self.question_banks or self.task_id or not self.role or self.max_questions!=10:raise ValueError('programming_task_role_required_without_other_modes')
        elif self.question_banks:
            if self.task_id or not self.role:raise ValueError('question_bank_role_required_without_code_task')
            if len({r.bank_id for r in self.question_banks})!=len(self.question_banks):raise ValueError('duplicate_bank_selection')
            if self.item_ids is not None:
                if not self.item_ids:raise ValueError('item_selection_empty')
                if len(set(self.item_ids))!=len(self.item_ids):raise ValueError('duplicate_item_selection')
                if len(self.item_ids)!=self.max_questions:raise ValueError('item_selection_count_mismatch')
                if self.item_kinds is not None or self.shuffle:raise ValueError('item_selection_conflict')
            if self.item_kinds is not None:
                if not self.item_kinds:raise ValueError('item_kinds_empty')
                if len(set(self.item_kinds))!=len(self.item_kinds):raise ValueError('duplicate_item_kind')
        elif self.role or self.max_questions!=10:raise ValueError('question_banks_required')
        return self


def creation_body(value):
    body=value.model_dump(mode='json')
    if not value.application_id:
        for key in ('application_id','application_package_revision','interview_round'):body.pop(key)
    if value.retest_observation_id is None:body.pop('retest_observation_id')
    if value.programming_task is None:body.pop('programming_task')
    if not value.question_banks:
        body.pop('question_banks');body.pop('max_questions')
        if value.programming_task is None:body.pop('role')
    if value.item_ids is None:
        body.pop('item_ids')
    if value.item_kinds is None:
        body.pop('item_kinds')
    if not value.shuffle:
        body.pop('shuffle')
    return body


class WrittenAnswer(Contract):
    expected_version:Annotated[int,Field(ge=1)]
    question_id:Annotated[str,Field(min_length=1,max_length=40)]
    option_ids:Annotated[list[Annotated[str,Field(min_length=1,max_length=16)]],Field(max_length=8)]=[]
    text:Annotated[str,Field(max_length=1500)]=''

    @field_validator('option_ids')
    @classmethod
    def choices(cls,value):
        if len(set(value))!=len(value):raise ValueError('duplicate_answer_option')
        return sorted(value)

    @field_validator('text')
    @classmethod
    def readable(cls,value):
        if any(ord(c)<32 and c not in '\n\r\t' for c in value) or '\ufffd' in value:raise ValueError('invalid_answer_text')
        return value.replace('\r\n','\n').replace('\r','\n')


class SubmitCode(Contract):
    expected_version: Annotated[int, Field(ge=1)]
    source: Annotated[str, Field(min_length=1, max_length=8192)]

    @field_validator('source')
    @classmethod
    def valid_source(cls, value):
        if not value.strip() or '\x00' in value or len(value.encode('utf-8'))>8192:
            raise ValueError('invalid_source')
        return value


class EndWritten(Contract):
    expected_version: Annotated[int, Field(ge=1)]
