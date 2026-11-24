from fastapi import Request, Response, Query
from interview_agent.api.dependencies import IdempotencyKeyDep, PrincipalDep
from interview_agent.contracts.practice import UuidString
from interview_agent.storage.errors import ApiError
from .contracts import SourceInput,RunInput,Confirmation,Versioned,FilePreviewInput,ClarificationInput,DraftRevision,ResumeImportInput
from .file_repository import FileRepository
from .repository import MaterialRepository
from .views import (MaterialConfirmationResult, MaterialDraftResult, MaterialListingView,
                    MaterialRunResult, MaterialSourceResult)
from interview_agent.api.dependencies import errors


def mount(router):
    def repo(request):return MaterialRepository(request.app.state.pool)
    @router.post('/{goal_id}/materials/runs/{run_id}/clarifications',response_model=MaterialSourceResult)
    async def clarification(goal_id:UuidString,run_id:UuidString,value:ClarificationInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        from .alignment_repository import AlignmentRepository
        identity,created=await AlignmentRepository(request.app.state.pool).clarify(user,goal_id,run_id,key,value)
        response.status_code=201 if created else 200
        return {'source_id':identity,**await repo(request).listing(user,goal_id)}
    @router.post('/{goal_id}/materials/file-previews')
    async def file_preview(goal_id:UuidString, request:Request, response:Response, user:PrincipalDep, key:IdempotencyKeyDep, filename:str=Query(min_length=1,max_length=80), expected_version:int=Query(ge=1), expected_revision:int=Query(ge=0,le=30)):
        try:value=FilePreviewInput(filename=filename,expected_version=expected_version,expected_revision=expected_revision)
        except ValueError:raise ApiError('invalid_request',400) from None
        preview,created=await FileRepository(request.app.state.pool).preview(user,goal_id,key,value,await request.body())
        response.status_code=201 if created else 200
        return preview
    @router.get('/{goal_id}/materials/file-previews/{preview_id}')
    async def read_file_preview(goal_id:UuidString,preview_id:UuidString,request:Request,user:PrincipalDep):
        return await FileRepository(request.app.state.pool).get(user,goal_id,preview_id)
    @router.post('/{goal_id}/materials/file-previews/{preview_id}/import',response_model=MaterialSourceResult)
    async def import_file(goal_id:UuidString,preview_id:UuidString,value:Versioned,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        identity,created=await FileRepository(request.app.state.pool).import_preview(user,goal_id,preview_id,key,value)
        response.status_code=201 if created else 200
        return {'source_id':identity,**await repo(request).listing(user,goal_id)}
    @router.get('/{goal_id}/materials',response_model=MaterialListingView,responses=errors(401,404))
    async def listing(goal_id:UuidString,request:Request,user:PrincipalDep):
        return await repo(request).listing(user,goal_id)
    @router.post('/{goal_id}/materials/sources',response_model=MaterialSourceResult,
                 responses={201: {"model": MaterialSourceResult, "description": "first upload"},
                            **errors(400,401,404,409)})
    async def upload(goal_id:UuidString,value:SourceInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        identity,created=await repo(request).upload(user,goal_id,key,value)
        response.status_code=201 if created else 200
        return {'source_id':identity,**await repo(request).listing(user,goal_id)}
    @router.post('/{goal_id}/materials/runs',response_model=MaterialRunResult)
    async def enqueue(goal_id:UuidString,value:RunInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        identity,created=await repo(request).enqueue(user,goal_id,key,value)
        response.status_code=202 if created else 200
        return {'run_id':identity,**await repo(request).listing(user,goal_id)}
    @router.post('/{goal_id}/materials/rewrite',response_model=MaterialRunResult,
                 responses={202: {"model": MaterialRunResult, "description": "run accepted"},
                            **errors(400,401,404,409)})
    async def rewrite(goal_id:UuidString,value:RunInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        command=value.model_copy(update={'mode':'draft','draft_style':'rewrite'})
        identity,created=await repo(request).enqueue(user,goal_id,key,command)
        response.status_code=202 if created else 200
        return {'run_id':identity,**await repo(request).listing(user,goal_id)}
    @router.post('/{goal_id}/materials/resume-imports', responses=errors(400,401,404,409))
    async def resume_import(goal_id:UuidString,value:ResumeImportInput,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        # F07Z-152: the user's own text becomes v1 with no model call and no fact promotion.
        from .resume_import import import_resume
        identity,version,created=await import_resume(repo(request),user,goal_id,key,value)
        response.status_code=201 if created else 200
        return {'draft_id':identity,'version':version,**await repo(request).listing(user,goal_id)}
    @router.post('/{goal_id}/materials/confirmations',response_model=MaterialConfirmationResult)
    async def confirm(goal_id:UuidString,value:Confirmation,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        identity,created=await repo(request).confirm(user,goal_id,key,value)
        response.status_code=201 if created else 200
        return {'confirmation_id':identity,**await repo(request).listing(user,goal_id)}
    @router.post('/{goal_id}/materials/runs/{run_id}/cancel',response_model=MaterialListingView)
    async def cancel(goal_id:UuidString,run_id:UuidString,value:Versioned,request:Request,user:PrincipalDep):
        await repo(request).cancel(user,goal_id,run_id,value)
        return await repo(request).listing(user,goal_id)
    @router.get('/{goal_id}/materials/drafts/{draft_id}.md',
                responses={200: {"description": "Draft markdown download",
                                 "content": {"text/markdown": {}}}, **errors(401, 404)})
    async def export(goal_id:UuidString,draft_id:UuidString,request:Request,user:PrincipalDep):
        view=await repo(request).listing(user,goal_id)
        draft=next((d for d in view['drafts'] if d['id']==draft_id),None)
        if not draft:raise ApiError('resource_unavailable',404)
        from .usable import usable_markdown
        body=usable_markdown(draft['payload'],fallback=draft['markdown'])
        return Response(body,media_type='text/markdown',headers={'Content-Disposition':f'attachment; filename="draft-{draft_id}.md"','X-Material-Stale':str(draft['stale']).lower()})
    @router.post('/{goal_id}/materials/drafts/{draft_id}/restore', responses=errors(400,401,404,409))
    async def restore(goal_id:UuidString,draft_id:UuidString,value:Versioned,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        # F07Z-152: restoring an old version appends a new immutable version; history is untouched.
        identity,created=await repo(request).restore(user,goal_id,draft_id,key,value)
        response.status_code=201 if created else 200
        return {'draft_id':identity,**await repo(request).listing(user,goal_id)}
    @router.post('/{goal_id}/materials/drafts/{draft_id}/revisions',response_model=MaterialDraftResult,
                 responses={201: {"model": MaterialDraftResult, "description": "revision saved"},
                            **errors(400,401,404,409)})
    async def revise(goal_id:UuidString,draft_id:UuidString,value:DraftRevision,request:Request,response:Response,user:PrincipalDep,key:IdempotencyKeyDep):
        from .draft_revisions import revise_draft
        identity,created=await revise_draft(repo(request),user,goal_id,draft_id,key,value)
        response.status_code=201 if created else 200
        return {'draft_id':identity,**await repo(request).listing(user,goal_id)}
