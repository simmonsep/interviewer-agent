from typing import Literal

from fastapi import APIRouter, Request

from interview_agent.api.dependencies import PrincipalDep, errors
from interview_agent.contracts.common import Contract, PositiveInt
from interview_agent.contracts.practice import UuidString

from .repository import WikiRepository


class WikiSourceView(Contract):
    id: str
    title: str
    url: str
    quote: str
    content_sha256: str
    fetched_at: str


class WikiDocumentView(Contract):
    title: str
    question: str
    probe: str
    kind: Literal['knowledge', 'scenario']
    difficulty: Literal['foundation', 'intermediate', 'advanced']
    public_reference: WikiSourceView | None = None
    experience_reference: WikiSourceView | None = None


class WikiVersionView(Contract):
    id: UuidString
    topic_key: str
    version: PositiveInt
    status: Literal['pending', 'active', 'archived', 'withdrawn']
    source_event_id: UuidString
    document: WikiDocumentView


class WikiListView(Contract):
    items: list[WikiVersionView]


class WikiReview(Contract):
    action: Literal['publish', 'restore', 'withdraw']
    expected_active_id: UuidString | None


def routes():
    router = APIRouter(prefix='/api/runtime/interview-wiki')

    @router.get('', response_model=WikiListView, response_model_exclude_none=True, responses=errors(401))
    async def listing(request: Request, user: PrincipalDep):
        return await WikiRepository(request.app.state.pool).listing(user)

    @router.post('/{version_id}/review', response_model=WikiVersionView, response_model_exclude_none=True,
                 responses=errors(400, 401, 404, 409))
    async def review(version_id: UuidString, value: WikiReview, request: Request, user: PrincipalDep):
        return await WikiRepository(request.app.state.pool).review(user, version_id, value)

    return router
