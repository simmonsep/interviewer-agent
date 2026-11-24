from fastapi import APIRouter, Request, Response

from interview_agent.api.dependencies import IdempotencyKeyDep, PrincipalDep, errors
from interview_agent.contracts.practice import UuidString

from .contracts import StrategyCommit, StrategyImportRequest, StrategySettings
from .repository import StrategyRepository


def routes():
    router = APIRouter(prefix='/api/strategies')

    def repo(request):
        return StrategyRepository(request.app.state.pool)

    @router.get('', responses=errors(401))
    async def listing(request: Request, user: PrincipalDep):
        return await repo(request).listing(user)

    @router.post('/imports', responses=errors(400, 401, 409))
    async def preview(value: StrategyImportRequest, request: Request, response: Response,
                      user: PrincipalDep, key: IdempotencyKeyDep):
        result, created = await repo(request).preview(user, key, value)
        response.status_code = 201 if created else 200
        return result

    @router.post('/imports/{preview_id}/commit', responses=errors(400, 401, 404, 409))
    async def commit(preview_id: UuidString, value: StrategyCommit, request: Request,
                     response: Response, user: PrincipalDep, key: IdempotencyKeyDep):
        result, created = await repo(request).commit(user, preview_id, key, value)
        response.status_code = 201 if created else 200
        return result

    @router.patch('/{set_id}', responses=errors(401, 404, 409))
    async def settings(set_id: str, value: StrategySettings, request: Request, user: PrincipalDep):
        return await repo(request).set_enabled(user, set_id, value)

    @router.delete('/{set_id}', responses=errors(401, 404))
    async def uninstall(set_id: str, request: Request, user: PrincipalDep):
        return await repo(request).uninstall(user, set_id)

    return router
