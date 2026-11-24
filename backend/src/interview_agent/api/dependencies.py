"""Shared request dependencies (S04A A3).

Authentication and idempotency-key parsing had three byte-identical local copies (`api/routes.py`,
`career/routes.py`, `question_banks/routes.py`). They are merged here. New or modified routes use
the `Annotated[...]` aliases; older routes may keep calling the same functions through
`Depends(...)` until they are touched for another reason (no mass syntax rewrite).

The behaviour is unchanged on purpose: missing/expired session -> 401 `authentication_required`;
missing or malformed key -> 400 `idempotency_key_required`.
"""
from typing import Annotated
import re

from fastapi import Depends, Request

from interview_agent.storage.auth import Principal
from interview_agent.storage.errors import ApiError
from interview_agent.contracts.common import Contract

IDEMPOTENCY_KEY = re.compile(r"[a-zA-Z0-9_-]{8,128}")

ERROR_DESCRIPTIONS = {
    400: "invalid_request or a route-specific input error",
    401: "authentication_required",
    404: "resource_unavailable",
    409: "version_conflict, idempotency_conflict or a route-specific conflict",
    422: "a route-specific semantic rejection (for example email_already_registered)",
}


class ErrorCode(Contract):
    """The only shape the API error handlers emit."""

    code: str


def errors(*statuses):
    return {status: {"model": ErrorCode, "description": ERROR_DESCRIPTIONS[status]}
            for status in statuses}


async def current_principal(request: Request) -> Principal:
    value = await request.app.state.auth.resolve(
        request.cookies.get(request.app.state.settings.cookie_name))
    if value is None:
        raise ApiError("authentication_required", 401)
    return value[0]


def idempotency_key(request: Request) -> str:
    value = request.headers.get("idempotency-key", "")
    if not IDEMPOTENCY_KEY.fullmatch(value):
        raise ApiError("idempotency_key_required", 400)
    return value


PrincipalDep = Annotated[Principal, Depends(current_principal)]
IdempotencyKeyDep = Annotated[str, Depends(idempotency_key)]
