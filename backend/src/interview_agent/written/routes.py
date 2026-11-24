from fastapi import Request, Response
from interview_agent.api.dependencies import IdempotencyKeyDep, PrincipalDep
from interview_agent.contracts.practice import UuidString
from interview_agent.storage.errors import ApiError
from .contracts import CreateWritten, SubmitCode, EndWritten,WrittenAnswer
from .catalog import catalog
from .repository import WrittenRepository


def mount(router):
    def repo(request): return WrittenRepository(request.app.state.pool,request.app.state.settings.written_execution_image)
    def enabled(request):
        if request.app.state.settings.written_execution_image is None: raise ApiError('written_executor_disabled',503)

    from .memory import Publish
    from interview_agent.interviews.contracts import Correction

    @router.get('/{goal_id}/written-memory')
    async def memory(goal_id:UuidString,request:Request,user:PrincipalDep):
        from .memory import listing
        return await listing(repo(request),user,goal_id)

    @router.post('/{goal_id}/written/{session_id}/observations')
    async def publish(goal_id:UuidString,session_id:UuidString,value:Publish,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        from .memory import publish as save
        identity,created=await save(repo(request),user,goal_id,session_id,key,value)
        response.status_code=201 if created else 200
        return {'observation_id':identity}

    @router.post('/{goal_id}/written-memory/{observation_id}/retract')
    async def retract(goal_id:UuidString,observation_id:UuidString,value:Correction,request:Request,user:PrincipalDep):
        from .memory import retract as save
        await save(repo(request),user,goal_id,observation_id,value)
        return {'retracted':True}

    @router.get('/{goal_id}/written-catalog')
    async def tasks(goal_id:UuidString,request:Request,user:PrincipalDep):
        # Check ownership even though these task statements are authored public data.
        await repo(request).listing(user,goal_id)
        return {'execution_enabled':request.app.state.settings.written_execution_image is not None,'items':catalog(),
                'question_bank_mode_available':True,'question_bank_catalog':'/api/question-banks'}

    @router.get('/{goal_id}/written')
    async def listing(goal_id:UuidString,request:Request,user:PrincipalDep):
        return {'items':await repo(request).listing(user,goal_id)}

    @router.post('/{goal_id}/written')
    async def create(goal_id:UuidString,value:CreateWritten,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        sid,created=await repo(request).create(user,goal_id,key,value)
        response.status_code=202 if created else 200
        return await repo(request).get(user,goal_id,sid)

    @router.get('/{goal_id}/written/{session_id}')
    async def get(goal_id:UuidString,session_id:UuidString,request:Request,user:PrincipalDep):
        return await repo(request).get(user,goal_id,session_id)

    @router.post('/{goal_id}/written-programming-validations')
    async def validate_programming(goal_id:UuidString,value:CreateWritten,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        from .programming import create as create_validation
        sid,created=await create_validation(repo(request),user,goal_id,key,value,validate=True)
        response.status_code=202 if created else 200
        return await repo(request).get(user,goal_id,sid)

    @router.post('/{goal_id}/written/{session_id}/submissions')
    async def submit(goal_id:UuidString,session_id:UuidString,value:SubmitCode,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        enabled(request)
        identity,created=await repo(request).submit(user,goal_id,session_id,key,value)
        response.status_code=202 if created else 200
        return {'submission_id':identity,'session':await repo(request).get(user,goal_id,session_id)}

    @router.post('/{goal_id}/written/{session_id}/answers')
    async def answer(goal_id:UuidString,session_id:UuidString,value:WrittenAnswer,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        from .question_sets import answer as save_answer
        repository=repo(request)
        identity,created=await save_answer(repository,user,goal_id,session_id,key,value)
        response.status_code=201 if created else 200
        return {'answer_event_id':identity,'session':await repository.get(user,goal_id,session_id)}

    @router.post('/{goal_id}/written/{session_id}/finish')
    async def finish(goal_id:UuidString,session_id:UuidString,value:EndWritten,request:Request,user:PrincipalDep):
        await repo(request).end(user,goal_id,session_id,value)
        return await repo(request).get(user,goal_id,session_id)

    @router.post('/{goal_id}/written/{session_id}/cancel')
    async def cancel(goal_id:UuidString,session_id:UuidString,value:EndWritten,request:Request,user:PrincipalDep):
        await repo(request).end(user,goal_id,session_id,value,cancel=True)
        return await repo(request).get(user,goal_id,session_id)
