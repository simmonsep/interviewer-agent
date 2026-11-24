"""Public task projection and minimal Redis notification; no credentials or input text."""

from typing import Annotated, Literal

from pydantic import Field

from .common import Contract, PositiveInt
from .practice import UuidString

ASSESSMENT_WAIT_CODES = {"model_not_enabled", "model_credentials_unavailable", "model_authorization_unavailable",
                         "model_budget_exhausted", "model_usage_unknown"}


class QueueMessage(Contract):
    job_id: UuidString
    operation_id: UuidString
    resource_version: Annotated[int, Field(ge=1, le=2147483647)]


class OperationView(Contract):
    id: UuidString
    user_id: UuidString
    resource_id: UuidString
    kind: Literal["update_target_focus", "assess_answer"]
    expected_version: PositiveInt
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "WAITING_CREDENTIALS"]
    result: dict | None
    error_code: str | None
    created_at: str
    completed_at: str | None
