"""Shared JSON primitives; identifiers remain strings across the wire boundary."""

from typing import Annotated, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", validate_default=True)


Id = Annotated[str, Field(pattern=r"^[a-z0-9-]{1,80}$")]
Text = Annotated[str, Field(min_length=1)]
Line = Annotated[str, Field(min_length=1, max_length=2000)]
PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
Mode = Literal["mock", "practice"]
ContextMode = Literal["role_only", "role_jd", "role_resume", "role_resume_jd"]
Split = Literal["development", "validation", "holdout"]

T = TypeVar("T")


def unique(items: list[T]) -> list[T]:
    if len(set(items)) != len(items):
        raise ValueError("duplicate_id")
    return items


Ids = Annotated[list[Id], Field(min_length=1, json_schema_extra={"uniqueItems": True}), AfterValidator(unique)]
Lines = Annotated[list[Line], Field(min_length=1, max_length=40)]


def strict_boolean(value: object) -> object:
    # Python considers 0 == False; Literal alone must not admit numeric JSON here.
    if type(value) is not bool:
        raise ValueError("boolean_required")
    return value


FalseFlag = Annotated[Literal[False], BeforeValidator(strict_boolean)]
TrueFlag = Annotated[Literal[True], BeforeValidator(strict_boolean)]
