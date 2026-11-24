"""Wire contracts for the interview preparation workflow (S01).

The plan is frozen before the user sees it: `plan_sha256` is what the confirmation binds to,
so a stale tab cannot approve a plan the server no longer holds.
"""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from interview_agent.contracts.common import Contract, PositiveInt
from interview_agent.contracts.practice import UuidString
from interview_agent.question_banks.contracts import BankRef

ResumeIndex = Annotated[int, Field(ge=0, le=7)]


class CreatePreparation(Contract):
    expected_version: PositiveInt
    # F07Z-166: the workbench no longer hard-codes a role; omitting it lets the server
    # resolve one deterministically from the goal's direction/selected job.
    role: Literal['java-backend', 'ai-application'] | None = None
    mode: Literal['interview', 'coaching'] = 'interview'
    # F07Z-167: 'full' is the 45-minute, 10-12 round simulation; the server owns its numbers.
    # Omitting it (old clients) or 'quick' keeps the caller's own max_questions/duration.
    profile: Literal['quick', 'full'] | None = None
    max_questions: Annotated[int, Field(ge=1, le=12)] = 4
    duration_seconds: Annotated[int, Field(ge=60, le=3600)] = 1200
    draft_id: UuidString | None = None
    resume_item_indexes: Annotated[list[ResumeIndex], Field(max_length=6)] = []
    allow_project_questions: bool | None = None
    question_banks: Annotated[list[BankRef], Field(max_length=2)] = []
    # F07Z-166: omitting draft_id means "use the bound resume", so an explicit "no resume" needs
    # its own flag; both this repository and the session repository honour it.
    use_resume: bool = True

    @model_validator(mode='after')
    def resume_scope(self):
        if len({ref.bank_id for ref in self.question_banks}) != len(self.question_banks):
            raise ValueError('duplicate_bank_selection')
        if len(set(self.resume_item_indexes)) != len(self.resume_item_indexes):
            raise ValueError('duplicate_resume_item')
        if self.resume_item_indexes and not self.draft_id:
            raise ValueError('resume_selection_requires_draft')
        if not self.use_resume and (self.draft_id or self.resume_item_indexes):
            raise ValueError('resume_excluded')
        if self.profile == 'full' and self.mode != 'interview':
            raise ValueError('full_requires_interview')
        return self


class ConfirmPreparation(Contract):
    expected_version: PositiveInt
    plan_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    decision: Literal['approve', 'reject']
    reason: Annotated[str, Field(max_length=200)] = ''
