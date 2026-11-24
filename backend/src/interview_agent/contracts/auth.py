"""Public account input: normalized email, bounded password, no caller-controlled IDs."""

from typing import Annotated

from pydantic import EmailStr, Field, SecretStr, field_validator

from .common import Contract, TrueFlag
from .practice import UuidString


class SignIn(Contract):
    email: Annotated[EmailStr, Field(max_length=254)]
    password: Annotated[SecretStr, Field(min_length=1, max_length=128)]

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value):
        return str(value).lower()


class SignUp(SignIn):
    password: Annotated[SecretStr, Field(min_length=12, max_length=128)]
    name: Annotated[str, Field(min_length=1, max_length=60)]

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value):
        if not value.strip():
            raise ValueError("name_required")
        return value.strip()

class PublicUser(Contract):
    """The only account fields the browser ever receives."""

    id: UuidString
    email: str
    name: str


class SessionInfo(Contract):
    id: UuidString
    userId: UuidString
    expiresAt: str


class SessionView(Contract):
    user: PublicUser
    session: SessionInfo


class SignOutView(Contract):
    success: TrueFlag
