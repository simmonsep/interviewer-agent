from typing import Annotated,Literal
from uuid import UUID
from pydantic import Field,field_validator,model_validator
from interview_agent.contracts.common import Contract
from interview_agent.contracts.practice import UuidString
from .programming import JavaProgramming

Role=Literal['java-backend','ai-application']
Slug=Annotated[str,Field(min_length=1,max_length=40,pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')]


class ChoiceOption(Contract):
    option_id:Annotated[str,Field(min_length=1,max_length=16,pattern=r'^[a-zA-Z0-9_-]+$')]
    text:Annotated[str,Field(min_length=1,max_length=500)]

    @field_validator('text')
    @classmethod
    def nonblank(cls,value):
        if not value.strip() or any(ord(c)<32 and c not in '\n\t' for c in value):raise ValueError('invalid_option_text')
        return value.strip()


class BankItem(Contract):
    item_id:Slug
    kind:Literal['knowledge','short_answer','single_choice','multiple_choice','java_programming']='knowledge'
    programming:JavaProgramming|None=None
    roles:Annotated[list[Role],Field(min_length=1,max_length=2)]
    title:Annotated[str,Field(min_length=1,max_length=80)]
    question:Annotated[str,Field(min_length=1,max_length=1000)]
    reference:Annotated[str,Field(max_length=1200)]=''
    # F07Z-71: optional, author-declared points for subjective items. Left empty by default so
    # existing v1/v2 documents keep their content_sha256; normalized_document drops the key when
    # it is empty and the reading side derives points from `reference` instead.
    reference_points:Annotated[list[Annotated[str,Field(min_length=1,max_length=200)]],Field(max_length=6)]=[]
    probe:Annotated[str,Field(max_length=1000)]=''
    retest:Annotated[str,Field(max_length=1000)]=''
    source_url:Annotated[str,Field(max_length=300)]=''
    source_note:Annotated[str,Field(max_length=300)]=''
    tags:Annotated[list[Annotated[str,Field(min_length=1,max_length=30)]],Field(max_length=8)]=[]
    difficulty:Literal['unknown','basic','intermediate','advanced']='unknown'
    options:Annotated[list[ChoiceOption],Field(max_length=8)]=[]
    correct_option_ids:Annotated[list[Annotated[str,Field(min_length=1,max_length=16)]],Field(max_length=8)]=[]

    @field_validator('correct_option_ids')
    @classmethod
    def unique_key(cls,value):
        if len(set(value))!=len(value):raise ValueError('duplicate_answer_key')
        return sorted(value)

    @model_validator(mode='after')
    def choice_shape(self):
        if self.kind=='java_programming':
            if self.programming is None or self.options or self.correct_option_ids or self.probe or self.retest:raise ValueError('java_programming_definition_required')
        elif self.programming is not None:raise ValueError('programming_fields_not_allowed')
        if self.kind in ('single_choice','multiple_choice'):
            ids=[o.option_id for o in self.options]
            if len(ids)<2 or len(set(ids))!=len(ids) or not set(self.correct_option_ids)<=set(ids):raise ValueError('invalid_choice_options')
            if self.kind=='single_choice' and len(self.correct_option_ids)>1:raise ValueError('single_answer_required')
            if self.probe or self.retest:raise ValueError('choice_followup_not_supported')
        elif self.options or self.correct_option_ids:raise ValueError('choice_fields_not_allowed')
        return self

    @field_validator('title','question','reference','probe','retest','source_note','source_url')
    @classmethod
    def readable(cls,value,info):
        value=value.replace('\r\n','\n').replace('\r','\n').strip()
        if '\ufffd' in value or any(ord(c)<32 and c not in '\n\t' for c in value):raise ValueError('readable_text_required')
        if info.field_name in ('title','question') and not value:raise ValueError('nonblank_required')
        return value

    @field_validator('roles')
    @classmethod
    def distinct_roles(cls,value):
        if len(set(value))!=len(value):raise ValueError('duplicate_role')
        return value

    @field_validator('reference_points')
    @classmethod
    def readable_points(cls,value):
        cleaned=[]
        for point in value:
            text=point.replace('\r\n','\n').replace('\r','\n').strip()
            if not text or '\ufffd' in text or any(ord(c)<32 and c not in '\n\t' for c in text):
                raise ValueError('invalid_reference_point')
            if text not in cleaned:cleaned.append(text)
        return cleaned

    @model_validator(mode='after')
    def points_only_for_text(self):
        # A choice question has a declared key; extra prose points would be a second, silent
        # standard, so they are rejected rather than ignored.
        if self.reference_points and self.kind not in ('knowledge','short_answer'):
            raise ValueError('reference_points_not_supported')
        return self


class BankDocument(Contract):
    schema_version:Literal[1,2,3]=1
    name:Annotated[str,Field(min_length=1,max_length=80)]
    items:Annotated[list[BankItem],Field(min_length=1,max_length=20)]

    @model_validator(mode='after')
    def schema_for_choices(self):
        if self.schema_version!=3 and any(i.kind=='java_programming' for i in self.items):raise ValueError('programming_requires_schema_v3')
        if self.schema_version==1 and any(i.kind in ('single_choice','multiple_choice') for i in self.items):raise ValueError('choice_requires_schema_v2')
        return self

    @field_validator('name')
    @classmethod
    def nonblank(cls,value):
        if not value.strip() or any(ord(c)<32 for c in value):raise ValueError('invalid_bank_name')
        return value.strip()

    @field_validator('items')
    @classmethod
    def distinct_items(cls,value):
        if len({i.item_id for i in value})!=len(value):raise ValueError('duplicate_bank_item')
        return value


class PreviewInput(Contract):
    document:BankDocument


def normalized_document(value):
    document=value.model_dump(mode='json')
    for item in document['items']:
        if item['kind']!='java_programming':item.pop('programming')
        if item['kind'] not in ('single_choice','multiple_choice'):
            item.pop('options');item.pop('correct_option_ids')
        # F07Z-71: derived points are never written back into the document. Dropping the empty
        # field keeps every existing v1/v2 bank's content_sha256 byte-identical.
        if not item.get('reference_points'):item.pop('reference_points',None)
    return document


class BankRef(Contract):
    bank_id:UuidString
    version:Annotated[int,Field(ge=1,le=30)]

    @field_validator('bank_id')
    @classmethod
    def canonical_id(cls,value):return str(UUID(value))


class ProgrammingRef(BankRef):
    item_id:Slug


class CommitInput(Contract):
    bank_id:UuidString|None=None
    expected_version:Annotated[int,Field(ge=0,le=30)]=0

    @field_validator('bank_id')
    @classmethod
    def canonical_id(cls,value):return str(UUID(value)) if value else None

    @model_validator(mode='after')
    def update_requires_version(self):
        if bool(self.bank_id)!=(self.expected_version>0):raise ValueError('bank_version_required')
        return self


class SetEnabled(Contract):
    expected_revision:Annotated[int,Field(ge=1)]
    enabled:bool
