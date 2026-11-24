"""Preparation endpoints: create, read, confirm. Same idempotency/version/404/409 contract."""
from fastapi import Request, Response
from interview_agent.api.dependencies import IdempotencyKeyDep, PrincipalDep

from interview_agent.contracts.practice import UuidString

from .contracts import ConfirmPreparation, CreatePreparation
from .repository import PreparationRepository
from .views import PreparationView
from interview_agent.api.dependencies import errors


def mount(router):
    def repo(request):
        return PreparationRepository(request.app.state.pool)

    @router.post('/{goal_id}/interview-preparations', response_model=PreparationView,
                 responses={202: {"model": PreparationView, "description": "first creation"},
                            **errors(400, 401, 404, 409)})
    async def create(goal_id: UuidString, value: CreatePreparation, request: Request,
                     response: Response, user:PrincipalDep,key:IdempotencyKeyDep):
        pid, created = await repo(request).create(user, goal_id, key, value)
        response.status_code = 202 if created else 200
        return await repo(request).get(user, goal_id, pid)

    @router.get('/{goal_id}/interview-preparations/current', response_model=PreparationView,
                responses=errors(401, 404))
    async def current(goal_id: UuidString, request: Request, user:PrincipalDep):
        return await repo(request).current(user, goal_id)

    @router.get('/{goal_id}/interview-preparations/{prep_id}', response_model=PreparationView,
                responses=errors(401, 404))
    async def get(goal_id: UuidString, prep_id: UuidString, request: Request, user:PrincipalDep):
        return await repo(request).get(user, goal_id, prep_id)

    @router.post('/{goal_id}/interview-preparations/{prep_id}/confirm', response_model=PreparationView,
                 responses=errors(400, 401, 404, 409))
    async def confirm(goal_id: UuidString, prep_id: UuidString, value: ConfirmPreparation,
                      request: Request, user:PrincipalDep,key:IdempotencyKeyDep):
        return await repo(request).confirm(user, goal_id, prep_id, key, value)
