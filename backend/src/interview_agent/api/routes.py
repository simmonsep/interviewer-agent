from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from interview_agent.contracts.auth import SessionView, SignIn, SignOutView, SignUp
from interview_agent.contracts.practice import CreateTarget, UuidString
from interview_agent.contracts.resources import DeleteTarget, TargetView, UpdateFocus
from interview_agent.contracts.operations import OperationView
from interview_agent.contracts.practice import EmptyCommand, CreatePractice, SubmitAnswer, PracticeSessionView
from interview_agent.data.catalog import public_question
from interview_agent.storage.errors import ApiError
from .dependencies import IdempotencyKeyDep, PrincipalDep, errors
from .events import event_cursor, operation_stream


def routes():
    router = APIRouter(prefix="/api")

    def cookie(response: Response, token: str, request: Request):
        settings = request.app.state.settings
        response.set_cookie(settings.cookie_name, token, max_age=settings.session_ttl_seconds,
                            httponly=True, secure=False, samesite="lax", path="/")

    @router.post("/auth/sign-up/email", response_model=SessionView, responses=errors(400, 422))
    async def signup(value: SignUp, request: Request, response: Response):
        result, token = await request.app.state.auth.register(value)
        cookie(response, token, request)
        return result

    @router.post("/auth/sign-in/email", response_model=SessionView, responses=errors(400, 401))
    async def signin(value: SignIn, request: Request, response: Response):
        result, token = await request.app.state.auth.login(value)
        cookie(response, token, request)
        return result

    @router.get("/auth/get-session", response_model=SessionView | None)
    async def session(request: Request):
        value = await request.app.state.auth.resolve(request.cookies.get(request.app.state.settings.cookie_name))
        return value[1] if value else None

    @router.post("/auth/sign-out", response_model=SignOutView)
    async def signout(request: Request, response: Response):
        name = request.app.state.settings.cookie_name
        await request.app.state.auth.logout(request.cookies.get(name))
        response.delete_cookie(name, path="/", httponly=True, samesite="lax")
        return {"success": True}

    @router.post("/training-targets/", response_model=TargetView, responses=errors(400, 401, 409))
    async def create_target(value: CreateTarget, request: Request, response: Response,
                            key: IdempotencyKeyDep, user: PrincipalDep):
        result, created = await request.app.state.targets.create(user, key, value)
        response.status_code = 201 if created else 200
        return result

    @router.get("/training-targets/", responses=errors(401))
    async def list_targets(request: Request, user: PrincipalDep):
        return {"items": await request.app.state.targets.list(user)}

    @router.get("/training-targets/{resource_id}", response_model=TargetView, responses=errors(401, 404))
    async def get_target(resource_id: UuidString, request: Request, user: PrincipalDep):
        return await request.app.state.targets.get(user, resource_id)

    @router.patch("/training-targets/{resource_id}", response_model=TargetView, responses=errors(400, 401, 404, 409))
    async def update_target(resource_id: UuidString, value: UpdateFocus, request: Request, user: PrincipalDep):
        return await request.app.state.targets.change(user, resource_id, value.expected_version, focus=value.focus)

    @router.delete("/training-targets/{resource_id}", responses=errors(400, 401, 404, 409))
    async def delete_target(resource_id: UuidString, value: DeleteTarget, request: Request, user: PrincipalDep):
        target = await request.app.state.targets.change(user, resource_id, value.expected_version, delete=True)
        return {"id": target.id, "version": target.version, "deleted": True}

    @router.post("/training-targets/{resource_id}/focus-operations", response_model=OperationView, responses=errors(400, 401, 404, 409))
    async def enqueue_focus(resource_id: UuidString, value: UpdateFocus, request: Request, response: Response,
                            key: IdempotencyKeyDep, user: PrincipalDep):
        result, created = await request.app.state.operations.enqueue(user, resource_id, key, value)
        response.status_code = 202 if created else 200
        return result

    @router.get("/operations/{operation_id}", response_model=OperationView, responses=errors(401, 404))
    async def get_operation(operation_id: UuidString, request: Request, user: PrincipalDep):
        return await request.app.state.operations.get(user, operation_id)

    @router.post("/operations/{operation_id}/cancel", response_model=OperationView, responses=errors(401, 404, 409))
    async def cancel_operation(operation_id: UuidString, value: EmptyCommand, request: Request, user: PrincipalDep):
        return await request.app.state.operations.cancel(user, operation_id)

    @router.get("/catalog/questions", responses=errors(401))
    async def questions(request: Request, user: PrincipalDep):
        return {"role_id": "agent-intern", "level": "intern", "assessment_mode": request.app.state.settings.assessment_mode,
                "items": [public_question(q) for q in request.app.state.questions]}

    @router.post("/practice-sessions", response_model=PracticeSessionView, responses=errors(400, 401, 404, 409))
    async def create_practice(value: CreatePractice, request: Request, response: Response,
                              key: IdempotencyKeyDep, user: PrincipalDep):
        question = next((q for q in request.app.state.questions if q.id == value.question_id), None)
        if question is None:
            raise ApiError("question_unavailable", 400)
        session_id, created = await request.app.state.training.create(user, value.target_id, key, question)
        response.status_code = 201 if created else 200
        return await request.app.state.training.get(user, session_id)

    @router.get("/practice-sessions", responses=errors(401))
    async def list_practice(request: Request, user: PrincipalDep):
        return {"items": await request.app.state.training.list(user)}

    @router.get("/practice-sessions/{session_id}", response_model=PracticeSessionView, responses=errors(401, 404))
    async def get_practice(session_id: UuidString, request: Request, user: PrincipalDep):
        return await request.app.state.training.get(user, session_id)

    @router.post("/practice-sessions/{session_id}/answers", response_model=OperationView, status_code=202, responses=errors(400, 401, 404, 409))
    async def submit_answer(session_id: UuidString, value: SubmitAnswer, request: Request,
                            key: IdempotencyKeyDep, user: PrincipalDep):
        return await request.app.state.training.submit(user, session_id, key, value)

    @router.delete("/practice-sessions/{session_id}", responses=errors(400, 401, 404, 409))
    async def delete_practice(session_id: UuidString, value: DeleteTarget, request: Request, user: PrincipalDep):
        return await request.app.state.training.delete(user, session_id, value.expected_version)

    @router.post("/operations/{operation_id}/resume", response_model=OperationView, responses=errors(401, 404, 409))
    async def resume_operation(operation_id: UuidString, value: EmptyCommand, request: Request, user: PrincipalDep):
        return await request.app.state.operations.resume(user, operation_id)

    @router.get("/operations/{operation_id}/event-log", responses=errors(401, 404))
    async def operation_events(operation_id: UuidString, request: Request, user: PrincipalDep,
                               after: str = "0"):
        return await request.app.state.operations.events(user, operation_id, event_cursor(after))

    @router.get("/operations/{operation_id}/events",
                responses={200: {"description": "Server-sent events; Last-Event-ID resumes the stream",
                                 "content": {"text/event-stream": {}}}, **errors(401, 404)})
    async def stream_operation(operation_id: UuidString, request: Request, user: PrincipalDep,
                               after: str = "0"):
        cursor = event_cursor(request.headers.get("last-event-id", after))
        repository = request.app.state.operations
        # Reject missing / foreign / deleted resources before sending HTTP 200 stream headers.
        await repository.get(user, operation_id)
        return StreamingResponse(operation_stream(repository, user, operation_id, cursor),
                                 media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    return router
