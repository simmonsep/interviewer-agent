from fastapi import Request, Response
from interview_agent.api.dependencies import IdempotencyKeyDep, PrincipalDep
from interview_agent.contracts.practice import UuidString
from .contracts import PackageInput,ReplacePackage,Progress,Availability
from .repository import ApplicationRepository

def mount(router):
    def repo(request): return ApplicationRepository(request.app.state.pool)
    @router.get('/{goal_id}/applications')
    async def listing(goal_id:UuidString,request:Request,user:PrincipalDep):
        return await repo(request).listing(user,goal_id)
    @router.post('/{goal_id}/applications')
    async def create(goal_id:UuidString,value:PackageInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        aid,created=await repo(request).create(user,goal_id,key,value)
        response.status_code=201 if created else 200
        return await repo(request).get(user,goal_id,aid)
    @router.get('/{goal_id}/applications/{application_id}')
    async def get(goal_id:UuidString,application_id:UuidString,request:Request,user:PrincipalDep):
        return await repo(request).get(user,goal_id,application_id)
    @router.post('/{goal_id}/applications/{application_id}/packages')
    async def package(goal_id:UuidString,application_id:UuidString,value:ReplacePackage,request:Request,user:PrincipalDep,key:IdempotencyKeyDep):
        await repo(request).command(user,goal_id,application_id,key,value,'package')
        return await repo(request).get(user,goal_id,application_id)
    @router.post('/{goal_id}/applications/{application_id}/progress')
    async def progress(goal_id:UuidString,application_id:UuidString,value:Progress,request:Request,user:PrincipalDep,key:IdempotencyKeyDep):
        await repo(request).command(user,goal_id,application_id,key,value,'progress')
        return await repo(request).get(user,goal_id,application_id)
    @router.post('/{goal_id}/applications/{application_id}/availability')
    async def availability(goal_id:UuidString,application_id:UuidString,value:Availability,request:Request,user:PrincipalDep,key:IdempotencyKeyDep):
        await repo(request).command(user,goal_id,application_id,key,value,'availability')
        return await repo(request).get(user,goal_id,application_id)
