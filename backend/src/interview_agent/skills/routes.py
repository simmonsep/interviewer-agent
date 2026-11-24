from fastapi import APIRouter, Request, Response

from interview_agent.api.dependencies import IdempotencyKeyDep, PrincipalDep, errors
from interview_agent.contracts.practice import UuidString

from .contracts import SkillCommit, SkillInstallRequest, SkillSettings
from .repository import SkillRepository


def routes():
    router = APIRouter(prefix='/api/skills')

    def repo(request):
        return SkillRepository(request.app.state.pool)

    @router.get('', responses=errors(401))
    async def listing(request: Request, user: PrincipalDep):
        return await repo(request).listing(user)

    @router.post('/imports', responses=errors(400, 401, 409))
    async def preview(value: SkillInstallRequest, request: Request, response: Response,
                      user: PrincipalDep, key: IdempotencyKeyDep):
        result, created = await repo(request).preview(user, key, value)
        response.status_code = 201 if created else 200
        return result

    @router.post('/imports/{preview_id}/commit', responses=errors(400, 401, 404, 409))
    async def commit(preview_id: UuidString, value: SkillCommit, request: Request, response: Response,
                     user: PrincipalDep, key: IdempotencyKeyDep):
        result, created = await repo(request).commit(user, preview_id, key, value)
        response.status_code = 201 if created else 200
        return result

    @router.get('/{skill_id}/versions/{version}', responses=errors(401, 404))
    async def export(skill_id: str, version: int, request: Request, user: PrincipalDep):
        return await repo(request).export(user, skill_id, version)

    @router.patch('/{skill_id}', responses=errors(401, 404))
    async def settings(skill_id: str, value: SkillSettings, request: Request, user: PrincipalDep):
        return await repo(request).set_enabled(user, skill_id, value)

    @router.delete('/{skill_id}', responses=errors(401, 404))
    async def uninstall(skill_id: str, request: Request, user: PrincipalDep):
        return await repo(request).uninstall(user, skill_id)

    return router
