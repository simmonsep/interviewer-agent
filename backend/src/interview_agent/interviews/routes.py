from fastapi import Request, Response
from interview_agent.api.dependencies import IdempotencyKeyDep, PrincipalDep
from interview_agent.contracts.practice import UuidString
from .contracts import CreateInterview,Answer,Stop,Correction,SkipQuestion,ClarifyQuestion
from .repository import InterviewRepository
from .views import InterviewListView, InterviewView
from interview_agent.api.dependencies import errors


def mount(router):
    def repo(request):return InterviewRepository(request.app.state.pool,
                                                projector=getattr(request.app.state,'memory',None))
    @router.get('/{goal_id}/interviews',response_model=InterviewListView,responses=errors(401,404))
    async def listing(goal_id:UuidString,request:Request,user:PrincipalDep):
        return {'items':await repo(request).listing(user,goal_id)}
    @router.post('/{goal_id}/interviews',response_model=InterviewView,
                 response_model_exclude_unset=True,
                 responses={202: {"model": InterviewView, "description": "first creation"},
                            **errors(400,401,404,409)})
    async def create(goal_id:UuidString,value:CreateInterview,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        sid,created=await repo(request).create(user,goal_id,key,value)
        response.status_code=202 if created else 200
        return await repo(request).get(user,goal_id,sid)
    @router.get('/{goal_id}/interviews/{session_id}',response_model=InterviewView,
                response_model_exclude_unset=True,responses=errors(401,404))
    async def get(goal_id:UuidString,session_id:UuidString,request:Request,user:PrincipalDep):
        return await repo(request).get(user,goal_id,session_id)
    @router.post('/{goal_id}/interviews/{session_id}/answers',status_code=202,
                 response_model=InterviewView,response_model_exclude_unset=True,
                 responses=errors(400,401,404,409))
    async def answer(goal_id:UuidString,session_id:UuidString,value:Answer,request:Request,user:PrincipalDep,key:IdempotencyKeyDep):
        await repo(request).answer(user,goal_id,session_id,key,value)
        return await repo(request).get(user,goal_id,session_id)
    @router.post('/{goal_id}/interviews/{session_id}/skip',status_code=202,
                 response_model=InterviewView,response_model_exclude_unset=True,
                 responses=errors(400,401,404,409))
    async def skip(goal_id:UuidString,session_id:UuidString,value:SkipQuestion,request:Request,user:PrincipalDep,key:IdempotencyKeyDep):
        await repo(request).skip(user,goal_id,session_id,key,value)
        return await repo(request).get(user,goal_id,session_id)
    @router.post('/{goal_id}/interviews/{session_id}/clarify',status_code=202,
                 response_model=InterviewView,response_model_exclude_unset=True,
                 responses=errors(400,401,404,409))
    async def clarify(goal_id:UuidString,session_id:UuidString,value:ClarifyQuestion,request:Request,user:PrincipalDep,key:IdempotencyKeyDep):
        await repo(request).clarify(user,goal_id,session_id,key,value)
        return await repo(request).get(user,goal_id,session_id)
    @router.post('/{goal_id}/interviews/{session_id}/stop',response_model=InterviewView,
                 response_model_exclude_unset=True,responses=errors(400,401,404,409))
    async def stop(goal_id:UuidString,session_id:UuidString,value:Stop,request:Request,user:PrincipalDep):
        await repo(request).stop(user,goal_id,session_id,value)
        return await repo(request).get(user,goal_id,session_id)
    @router.get('/{goal_id}/gap-report')
    async def gap_report(goal_id:UuidString,request:Request,user:PrincipalDep):
        # Derived from observations the interviews already wrote. No model call.
        return await repo(request).gap_report(user,goal_id)
    @router.get('/{goal_id}/interview-memory')
    async def memory(goal_id:UuidString,request:Request,user:PrincipalDep):
        return await repo(request).memory(user,goal_id)
    @router.post('/{goal_id}/interview-memory/{observation_id}/retract')
    async def correct(goal_id:UuidString,observation_id:UuidString,value:Correction,request:Request,user:PrincipalDep,key:IdempotencyKeyDep):
        await repo(request).correct(user,goal_id,observation_id,key,value)
        return await repo(request).memory(user,goal_id)
