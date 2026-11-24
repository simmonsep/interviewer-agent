from fastapi import APIRouter, Request, Response
from interview_agent.api.dependencies import IdempotencyKeyDep, PrincipalDep, errors
from interview_agent.contracts.practice import UuidString
from interview_agent.contracts.resources import DeleteTarget
from .contracts import GoalInput,GoalReply,ManualJobInput,ManualJobRevision,BindingInput,SelectJob
from .research_repository import ResearchRepository
from .research_tools import CAPABILITIES,ResearchInput


def routes():
    router=APIRouter(prefix='/api/career-goals')
    from interview_agent.materials.contracts import RunInput as MaterialRun
    from interview_agent.interviews.contracts import CreateInterview
    class ConfirmMaterials(MaterialRun):
        continue_on_completion: bool = False
    class ConfirmTraining(CreateInterview):
        continue_on_completion: bool = False
    @router.post('/{goal_id}/steps/{step_id}/confirm-materials')
    async def confirm_materials(goal_id:UuidString,step_id:UuidString,value:ConfirmMaterials,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        from interview_agent.materials.repository import MaterialRepository
        repo=MaterialRepository(request.app.state.pool)
        identity,created=await repo.enqueue(user,goal_id,key,value,confirmation_step=step_id)
        response.status_code=202 if created else 200
        return {'run_id':identity,**await repo.listing(user,goal_id)}
    @router.post('/{goal_id}/steps/{step_id}/confirm-training')
    async def confirm_training(goal_id:UuidString,step_id:UuidString,value:ConfirmTraining,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        from interview_agent.interviews.repository import InterviewRepository
        repo=InterviewRepository(request.app.state.pool)
        identity,created=await repo.create(user,goal_id,key,value,confirmation_step=step_id)
        response.status_code=202 if created else 200
        return await repo.get(user,goal_id,identity)
    @router.post('/')
    async def create(value:GoalInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        identity,created=await request.app.state.career.create(user,key,value)
        response.status_code=202 if created else 200
        return await request.app.state.career.get(user,identity)
    @router.get('/', responses=errors(401))
    async def listing(request:Request,user:PrincipalDep):
        return {'items':await request.app.state.career.list(user)}
    @router.get('/{goal_id}')
    async def get(goal_id:UuidString,request:Request,user:PrincipalDep):
        return await request.app.state.career.get(user,goal_id)
    @router.get('/{goal_id}/workflow')
    async def workflow(goal_id:UuidString,request:Request,user:PrincipalDep):
        return await request.app.state.career.workflow(user,goal_id)
    @router.post('/{goal_id}/messages',status_code=202)
    async def reply(goal_id:UuidString,value:GoalReply,request:Request,user:PrincipalDep,key:IdempotencyKeyDep):
        await request.app.state.career.reply(user,goal_id,key,value)
        return await request.app.state.career.get(user,goal_id)
    @router.post('/{goal_id}/cancel')
    async def cancel(goal_id:UuidString,value:DeleteTarget,request:Request,user:PrincipalDep):
        await request.app.state.career.cancel(user,goal_id,value.expected_version)
        return await request.app.state.career.get(user,goal_id)
    @router.get('/{goal_id}/jobs')
    async def jobs(goal_id:UuidString,request:Request,user:PrincipalDep):
        repo=ResearchRepository(request.app.state.pool)
        return {**await repo.listing(user,goal_id),'sources':CAPABILITIES}
    @router.post('/{goal_id}/jobs/manual',status_code=201)
    async def manual_job(goal_id:UuidString,value:ManualJobInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        # A job the user typed in. No fetch, no worker: the run completes here so the
        # snapshot can be selected and used exactly like a fetched one.
        repo=ResearchRepository(request.app.state.pool)
        snapshot,created=await repo.create_manual(user,goal_id,key,value)
        response.status_code=201 if created else 200
        return {'snapshot_id':snapshot,**await repo.listing(user,goal_id),'sources':CAPABILITIES}
    @router.get('/{goal_id}/positions/{position_id}/binding', responses=errors(401,404))
    async def binding(goal_id:UuidString,position_id:UuidString,request:Request,user:PrincipalDep):
        from .bindings import BindingRepository
        async with request.app.state.pool.connection() as conn:
            return await BindingRepository(request.app.state.pool).view(conn,goal_id,position_id)
    @router.put('/{goal_id}/positions/{position_id}/binding', responses=errors(401,404,409))
    async def set_binding(goal_id:UuidString,position_id:UuidString,value:BindingInput,
                          request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        from .bindings import BindingRepository
        result,created=await BindingRepository(request.app.state.pool).set(user,goal_id,position_id,key,value)
        response.status_code=201 if created else 200
        return result
    @router.get('/{goal_id}/evidence/material-facts/{fact_id}', responses=errors(401,404))
    async def evidence_fact(goal_id:UuidString,fact_id:UuidString,request:Request,user:PrincipalDep):
        from interview_agent.career.repository import CareerRepository
        from .evidence import material_fact
        async with request.app.state.pool.connection() as conn:
            await CareerRepository(request.app.state.pool)._goal(conn,user.user_id,goal_id)
            return await material_fact(conn,goal_id,fact_id)
    @router.get('/{goal_id}/evidence/resume-bullets/{draft_id}/{index}', responses=errors(401,404))
    async def evidence_bullet(goal_id:UuidString,draft_id:UuidString,index:int,request:Request,user:PrincipalDep):
        from interview_agent.career.repository import CareerRepository
        from .evidence import resume_bullet
        async with request.app.state.pool.connection() as conn:
            await CareerRepository(request.app.state.pool)._goal(conn,user.user_id,goal_id)
            return await resume_bullet(conn,goal_id,draft_id,index)
    @router.get('/{goal_id}/evidence/observations/{observation_id}', responses=errors(401,404))
    async def evidence_observation(goal_id:UuidString,observation_id:UuidString,request:Request,user:PrincipalDep):
        from interview_agent.career.repository import CareerRepository
        from .evidence import observation
        async with request.app.state.pool.connection() as conn:
            await CareerRepository(request.app.state.pool)._goal(conn,user.user_id,goal_id)
            return await observation(conn,goal_id,observation_id)
    @router.get('/{goal_id}/evidence/applications/{application_id}', responses=errors(401,404))
    async def evidence_application(goal_id:UuidString,application_id:UuidString,request:Request,user:PrincipalDep):
        from interview_agent.career.repository import CareerRepository
        from .evidence import application_action
        async with request.app.state.pool.connection() as conn:
            await CareerRepository(request.app.state.pool)._goal(conn,user.user_id,goal_id)
            return await application_action(conn,goal_id,application_id)
    @router.get('/{goal_id}/timeline', responses=errors(401,404))
    async def goal_timeline(goal_id:UuidString,request:Request,user:PrincipalDep):
        from interview_agent.career.repository import CareerRepository
        from .timeline import timeline
        async with request.app.state.pool.connection() as conn:
            await CareerRepository(request.app.state.pool)._goal(conn,user.user_id,goal_id)
            return await timeline(conn,goal_id)
    @router.post('/{goal_id}/jobs/{position_id}/revisions',status_code=201)
    async def revise_manual_job(goal_id:UuidString,position_id:UuidString,value:ManualJobRevision,
                                request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        # A typo fix or a JD update stays the same position, so a resume binding survives it.
        repo=ResearchRepository(request.app.state.pool)
        snapshot,created=await repo.revise_manual(user,goal_id,position_id,key,value)
        response.status_code=201 if created else 200
        return {'snapshot_id':snapshot,**await repo.listing(user,goal_id),'sources':CAPABILITIES}
    @router.post('/{goal_id}/research')
    async def research(goal_id:UuidString,value:ResearchInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        repo=ResearchRepository(request.app.state.pool)
        identity,created=await repo.enqueue(user,goal_id,key,value)
        response.status_code=202 if created else 200
        return {'run_id':identity,**await repo.listing(user,goal_id),'sources':CAPABILITIES}
    @router.post('/{goal_id}/selection')
    async def select(goal_id:UuidString,value:SelectJob,request:Request,user:PrincipalDep):
        repo=ResearchRepository(request.app.state.pool)
        await repo.select(user,goal_id,value)
        return {**await repo.listing(user,goal_id),'sources':CAPABILITIES}
    @router.delete('/{goal_id}/selection')
    async def clear_selection(goal_id:UuidString,expected_version:int,request:Request,user:PrincipalDep):
        # F07Z-166 D1: undo a carried selection so the user can choose again.
        repo=ResearchRepository(request.app.state.pool)
        return await repo.clear_selection(user,goal_id,expected_version)
    from interview_agent.materials.routes import mount
    mount(router)
    from interview_agent.interviews.routes import mount as mount_interviews
    mount_interviews(router)
    from interview_agent.interview_prep.routes import mount as mount_preparations
    mount_preparations(router)
    from interview_agent.written.routes import mount as mount_written
    mount_written(router)
    from interview_agent.coding.routes import mount as mount_coding
    mount_coding(router)
    from interview_agent.applications.routes import mount as mount_applications
    mount_applications(router)
    return router
