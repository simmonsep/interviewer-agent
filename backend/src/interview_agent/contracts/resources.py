from typing import Annotated, Literal

from pydantic import Field

from .common import Contract, Id, PositiveInt
from .practice import UuidString


class UpdateFocus(Contract):
    expected_version: PositiveInt
    focus: Annotated[str, Field(max_length=120)]


class DeleteTarget(Contract):
    expected_version: PositiveInt


class TargetView(Contract):
    id: UuidString
    user_id: UuidString
    role_package_id: Literal["agent-intern"]
    role_package_version: Id
    level: Literal["intern"]
    focus: Annotated[str, Field(max_length=120)]
    context_mode: Literal["role_only"]
    version: PositiveInt
    created_at: str
