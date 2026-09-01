from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from app import __version__
from app.api import auth, events, health, jobs, library, pages, requests, usage
from app.config import Settings, get_settings
from app.db.engine import (
    assert_database_pragmas,
    assert_schema_current,
    create_database_engine,
    make_session_factory,
)
from app.logging import configure_logging
from app.middleware import AllowedClientMiddleware, BodyLimitMiddleware, SecurityHeadersMiddleware
from app.repositories.auth import AuthRepository
from app.repositories.events import EventRepository
from app.repositories.jobs import JobRepository
from app.repositories.library import LibraryRepository
from app.repositories.requests import RequestRepository
from app.services.supervisor import WebOrchestration, WebTaskSupervisor


def _build_orchestration(settings: Settings, factory: sessionmaker[Session]) -> WebOrchestration:
    from app.services.orchestration import OrchestrationService
    from app.tools.registry import build_default_registry
    from app.tools.youtube import build_youtube_search_tool

    registry = build_default_registry(
        settings,
        factory,
        youtube_search_tool=build_youtube_search_tool(factory),
    )
    service = OrchestrationService(
        settings=settings,
        session_factory=factory,
        tool_registry=registry,
    )
    return cast(WebOrchestration, service)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.app_version = __version__
    configure_logging(settings.log_level)
    engine = create_database_engine(settings)
    factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        assert_schema_current(engine)
        assert_database_pragmas(engine)
        for path in (
            settings.artwork_path,
            settings.downloads_path,
            settings.music_path,
            settings.backup_path,
        ):
            path.mkdir(parents=True, exist_ok=True)
        supervisor = WebTaskSupervisor(
            engine=engine,
            factory=factory,
            settings=settings,
            orchestration=app.state.orchestration,
            jobs=app.state.jobs,
            events=app.state.events,
        )
        app.state.supervisor = supervisor
        supervisor_task = asyncio.create_task(supervisor.run(), name="web-task-supervisor")
        try:
            yield
        finally:
            supervisor.stop()
            try:
                await asyncio.wait_for(supervisor_task, timeout=10)
            except TimeoutError:
                supervisor_task.cancel()
            close_orchestration = getattr(app.state.orchestration, "aclose", None)
            if callable(close_orchestration):
                close_result = close_orchestration()
                if inspect.isawaitable(close_result):
                    await close_result
            engine.dispose()

    app = FastAPI(
        title="Music Agent",
        version=__version__,
        docs_url=None if settings.environment == "production" else "/api/docs",
        redoc_url=None,
        openapi_url=None if settings.environment == "production" else "/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.auth = AuthRepository(engine, factory, settings)
    app.state.requests = RequestRepository(factory)
    app.state.jobs = JobRepository(factory)
    app.state.library = LibraryRepository(factory)
    app.state.events = EventRepository(factory)
    app.state.orchestration = _build_orchestration(settings, factory)
    app.state.templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(BodyLimitMiddleware, max_bytes=1_048_576)
    app.add_middleware(AllowedClientMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)

    static_path = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_path), name="static")
    for router in (
        auth.router,
        health.router,
        requests.router,
        jobs.router,
        library.router,
        usage.router,
        events.router,
        pages.router,
    ):
        app.include_router(router)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> Response:
        if error.status_code == status.HTTP_401_UNAUTHORIZED and "text/html" in request.headers.get(
            "accept", ""
        ):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse(
            {"detail": error.detail}, status_code=error.status_code, headers=error.headers
        )

    return app
