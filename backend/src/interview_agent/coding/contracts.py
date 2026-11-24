from typing import Annotated, Literal
from pydantic import Field, field_validator
from interview_agent.contracts.common import Contract
from interview_agent.contracts.practice import UuidString

Version = Annotated[int, Field(ge=1)]
Hash = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Explanation = Annotated[str, Field(min_length=1, max_length=1200)]


class CreateCoding(Contract):
    expected_version: Version
    task_id: Literal['ttl-boundary', 'order-cancel'] = 'ttl-boundary'
    mode: Literal['practice', 'mock'] = 'practice'
    duration_seconds: Annotated[int, Field(ge=60, le=3600)] = 1800


class CodingAction(Contract):
    expected_version: Version
    expected_workspace_sha256: Hash


class EditCoding(CodingAction):
    path: Annotated[str, Field(min_length=1, max_length=80)]
    expected_sha256: Hash
    text: Annotated[str, Field(min_length=1, max_length=8192)]
    explanation: Explanation

    @field_validator('text')
    @classmethod
    def bounded(cls, value):
        if not value.strip() or '\x00' in value or len(value.encode()) > 8192: raise ValueError('invalid_file_content')
        return value


class DecideCoding(CodingAction):
    run_id: UuidString
    decision: Literal['accept', 'reject']
    explanation: Explanation


class FinishCoding(CodingAction):
    explanation: Explanation


class CancelCoding(Contract):
    expected_version: Version
