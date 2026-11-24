"""Health-only by default; explicit Python settings enable the independent SQL API."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from interview_agent.storage.auth import AuthRepository
from interview_agent.storage.config import Settings
from interview_agent import observability
from interview_agent.storage.errors import ApiError
from interview_agent.storage.targets import TargetRepository
from interview_agent.storage.operations import OperationRepository
from interview_agent.storage.training import TrainingRepository
from interview_agent.data.catalog import load_catalog, practice_questions
from interview_agent.resources import resource_root

from .boundary import ApiBoundary
from .routes import routes
from interview_agent.career.repository import CareerRepository
from interview_agent.career.routes import routes as career_routes
from interview_agent.question_banks.routes import routes as bank_routes


def create_app(settings: Settings | None = None, static_dir: Path | None = None, model_config_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        if settings is None:
            yield
            return
        pool = AsyncConnectionPool(settings.database.conninfo(), min_size=1, max_size=8, open=False,
                                   timeout=5, reconnect_timeout=5, kwargs={"row_factory": dict_row})
        try:
            await pool.open(wait=True, timeout=8)
            app.state.settings, app.state.pool = settings, pool
            # S00O: process-local metadata trace. Off unless INTERVIEW_AGENTOPS is set;
            # when off this is the no-op tracer and nothing is written anywhere.
            app.state.tracer = observability.configure('api', mode=settings.mode)
            # The Store projection is optional: a missing/uninitialized Store only means the
            # coaching read path falls back to SQL (and says so in the trace).
            try:
                from interview_agent.interviews.memory_store import MemoryProjector
                app.state.memory = MemoryProjector(settings.database)
            except Exception:
                app.state.memory = None
            app.state.tracer.record('app.started', stage='api')
            app.state.auth = AuthRepository(pool, settings.session_ttl_seconds)
            app.state.targets = TargetRepository(pool)
            app.state.operations = OperationRepository(pool)
            app.state.training = TrainingRepository(pool)
            app.state.career = CareerRepository(pool)
            app.state.questions = practice_questions(load_catalog(resource_root()))
            # Where the user's model-access file lives; None means the surface is off.
            app.state.model_config_path = model_config_path
            yield
        finally:
            await pool.close()

    app = FastAPI(title="Python Interview Agent", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(ApiError)
    async def expected_error(request: Request, error: ApiError):
        return JSONResponse({"code": error.code}, error.status, headers={"Cache-Control": "no-store"})

    # F07Z-131 (F07Z-121 E1): an unmatched route used to fall through to Starlette's default
    # `{"detail": "Not Found"}`. The page reads errors through `describeError`, which only
    # understands `code`, so those responses were invisible. Map the status to the same shape
    # the ApiError handler emits; the mapping is closed and deterministic.
    HTTP_CODES = {404: "not_found", 405: "method_not_allowed", 403: "forbidden",
                  413: "request_too_large", 415: "unsupported_media_type"}

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        code = HTTP_CODES.get(error.status_code, f"http_{error.status_code}")
        return JSONResponse({"code": code}, error.status_code,
                            headers={"Cache-Control": "no-store"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        return JSONResponse({"code": "invalid_request"}, 400, headers={"Cache-Control": "no-store"})

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception):
        return JSONResponse({"code": "internal_error"}, 500, headers={"Cache-Control": "no-store"})

    @app.get("/health", summary="Process liveness only; no dependency readiness check")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "python-interview"}

    if settings is not None:
        app.include_router(routes())
        app.include_router(career_routes())
        app.include_router(bank_routes())
        # Local site progress sync reads a page the user already has open in a
        # browser window on this machine; it needs a signed-in local session.
        from interview_agent.desktop.site_sync import attach_error_messages, routes as site_sync_routes
        app.include_router(site_sync_routes())
        # F07Z-57: algorithm practice opens the user's own logged-in profile on 牛客/力扣 and
        # records what they did by hand; the product never judges algorithm questions.
        from interview_agent.desktop.practice_sites import routes as practice_site_routes, record_routes
        from interview_agent.desktop.learning_resources import routes as learning_resource_routes
        app.include_router(practice_site_routes())
        app.include_router(record_routes())
        app.include_router(learning_resource_routes())
        attach_error_messages(app)
        # Local autonomy policy: how much the Coordinator Agent may do without
        # asking, plus remembered per-tool approval decisions.
        from interview_agent.desktop.agent_policy import routes as agent_policy_routes
        app.include_router(agent_policy_routes())
        # F07Z-161: user-installed declarative skills (text only; never a tool grant).
        from interview_agent.skills.routes import routes as skill_routes
        app.include_router(skill_routes())
        # F07Z-162: allowlisted read-only reference check (off by default; never a model tool).
        from interview_agent.connectors.routes import routes as connector_routes
        app.include_router(connector_routes())
        # F07Z-164: user-imported project follow-up strategy cards.
        from interview_agent.strategies.routes import routes as strategy_routes
        app.include_router(strategy_routes())
        from interview_agent.interview_wiki.routes import routes as wiki_routes
        app.include_router(wiki_routes())
        if model_config_path is not None:
            from interview_agent.model_settings.routes import routes as model_settings_routes
            app.include_router(model_settings_routes())
        app.add_middleware(ApiBoundary, origin=settings.origin)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
        if static_dir is not None:
            # Mounted last so /api/* and /health keep priority. Only the desktop build
            # passes this; the development entry keeps using the Vite server.
            from interview_agent.desktop.static_site import attach_static_site
            attach_static_site(app, static_dir)
    return app
