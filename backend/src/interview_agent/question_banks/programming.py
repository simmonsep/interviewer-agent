from typing import Annotated,Literal
from pydantic import Field,field_validator,model_validator
from interview_agent.contracts.common import Contract


class JavaCase(Contract):
    id:Annotated[str,Field(min_length=1,max_length=40,pattern=r'^[a-zA-Z0-9_-]+$')]
    visibility:Literal['public','hidden']
    input:Annotated[str,Field(max_length=2048)]
    output:Annotated[str,Field(max_length=2048)]

    @field_validator('input','output')
    @classmethod
    def text(cls,value):
        value=value.replace('\r\n','\n').replace('\r','\n')
        if any(ord(c)<32 and c not in '\n\t' for c in value) or '\ufffd' in value:raise ValueError('invalid_case_text')
        return value


class JavaProgramming(Contract):
    profile:Literal['java21-stdio-v1']='java21-stdio-v1'
    starter:Annotated[str,Field(min_length=1,max_length=8192)]
    reference_solution:Annotated[str,Field(min_length=1,max_length=8192)]
    cases:Annotated[list[JavaCase],Field(min_length=2,max_length=6)]

    @field_validator('starter','reference_solution')
    @classmethod
    def source(cls,value):
        value=value.replace('\r\n','\n').replace('\r','\n')
        if not value.strip() or '\x00' in value or len(value.encode('utf-8'))>8192:raise ValueError('invalid_java_source')
        return value

    @model_validator(mode='after')
    def test_sets(self):
        if len({c.id for c in self.cases})!=len(self.cases):raise ValueError('duplicate_java_case')
        if {c.visibility for c in self.cases}!={'public','hidden'}:raise ValueError('public_and_hidden_cases_required')
        return self
