from fastapi import APIRouter, Request, Response
from interview_agent.api.dependencies import IdempotencyKeyDep, PrincipalDep, errors
from interview_agent.contracts.practice import UuidString
from .contracts import PreviewInput,CommitInput,SetEnabled
from .repository import BankRepository


def routes():
    router=APIRouter(prefix='/api/question-banks')
    def repo(request):return BankRepository(request.app.state.pool)
    @router.get('', responses=errors(401))
    async def listing(request:Request,user:PrincipalDep):return await repo(request).listing(user)
    @router.post('/imports')
    async def preview(value:PreviewInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        result,created=await repo(request).preview(user,key,value)
        response.status_code=201 if created else 200
        return result
    @router.get('/imports/{preview_id}')
    async def get_preview(preview_id:UuidString,request:Request,user:PrincipalDep):
        return await repo(request).get_preview(user,preview_id)
    @router.post('/imports/{preview_id}/commit')
    async def commit(preview_id:UuidString,value:CommitInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        result,created=await repo(request).commit(user,preview_id,key,value)
        response.status_code=201 if created else 200
        return result
    @router.get('/{bank_id}/versions/{version}')
    async def export(bank_id:UuidString,version:int,request:Request,user:PrincipalDep):
        return await repo(request).export(user,bank_id,version)
    @router.patch('/{bank_id}')
    async def enabled(bank_id:UuidString,value:SetEnabled,request:Request,user:PrincipalDep):
        await repo(request).set_enabled(user,bank_id,value)
        return await repo(request).listing(user)
    return router
