from fastapi import Request, Response
from interview_agent.api.dependencies import IdempotencyKeyDep, PrincipalDep
from interview_agent.contracts.practice import UuidString
from .contracts import CreateCoding, CodingAction, EditCoding, DecideCoding, FinishCoding, CancelCoding
from .repository import CodingRepository
from .catalog import TASKS, task, public_task


def mount(router):
    def repo(request): return CodingRepository(request.app.state.pool, request.app.state.settings.coding_execution_image, assistant_mode=request.app.state.settings.coding_assistant_mode)

    @router.get('/{goal_id}/coding-catalog')
    async def catalog(goal_id: UuidString, request: Request, user:PrincipalDep):
        await repo(request).listing(user, goal_id)
        return {'execution_enabled': request.app.state.settings.coding_execution_image is not None,
                'items': [public_task(task(k)) for k in TASKS]}

    @router.get('/{goal_id}/coding')
    async def listing(goal_id: UuidString, request: Request, user:PrincipalDep):
        return {'items': await repo(request).listing(user, goal_id)}

    @router.post('/{goal_id}/coding')
    async def create(goal_id: UuidString, value: CreateCoding, request: Request, response: Response, user:PrincipalDep,key:IdempotencyKeyDep):
        sid, created = await repo(request).create(user, goal_id, key, value)
        response.status_code = 202 if created else 200
        return await repo(request).get(user, goal_id, sid)

    @router.get('/{goal_id}/coding/{session_id}')
    async def get(goal_id: UuidString, session_id: UuidString, request: Request, user:PrincipalDep):
        return await repo(request).get(user, goal_id, session_id)

    @router.post('/{goal_id}/coding/{session_id}/edits')
    async def edit(goal_id: UuidString, session_id: UuidString, value: EditCoding, request: Request, user:PrincipalDep,key:IdempotencyKeyDep):
        await repo(request).edit(user, goal_id, session_id, key, value)
        return await repo(request).get(user, goal_id, session_id)

    def run_route(path, kind, contract):
        # Annotation is bound explicitly for FastAPI; no client-supplied task kind.
        async def enqueue(goal_id: UuidString, session_id: UuidString, value, request: Request, response: Response, user:PrincipalDep, key: IdempotencyKeyDep):
            rid, created = await repo(request).enqueue(user, goal_id, session_id, key, value, kind)
            response.status_code = 202 if created else 200
            return {'run_id': rid, 'session': await repo(request).get(user, goal_id, session_id)}
        enqueue.__annotations__['value'] = contract
        router.add_api_route('/{goal_id}/coding/{session_id}/' + path, enqueue, methods=['POST'], name='coding_' + kind)
    run_route('public-tests', 'public', CodingAction)
    run_route('assist', 'assist', CodingAction)
    run_route('submit', 'final', FinishCoding)

    @router.post('/{goal_id}/coding/{session_id}/decisions')
    async def decide(goal_id: UuidString, session_id: UuidString, value: DecideCoding, request: Request, user:PrincipalDep,key:IdempotencyKeyDep):
        await repo(request).decide(user, goal_id, session_id, key, value)
        return await repo(request).get(user, goal_id, session_id)

    @router.post('/{goal_id}/coding/{session_id}/cancel')
    async def cancel(goal_id: UuidString, session_id: UuidString, value: CancelCoding, request: Request, user:PrincipalDep,key:IdempotencyKeyDep):
        await repo(request).cancel(user, goal_id, session_id, key, value)
        return await repo(request).get(user, goal_id, session_id)
