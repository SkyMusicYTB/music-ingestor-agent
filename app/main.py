from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import Response
from starlette.types import Scope

from app import __version__
from app.api import accounts, auth, events, health, jobs, library, pages, requests, usage
from app.config import Settings, get_settings
from app.db.engine import (
    assert_database_pragmas,
    assert_schema_current,
    create_database_engine,
    make_session_factory,
)
from app.logging import configure_logging
from app.middleware import (
    AllowedClientMiddleware,
    BodyLimitMiddleware,
    SecurityHeadersMiddleware,
    TrustedHostMiddleware,
    TrustedProxyMiddleware,
)
from app.repositories.auth import AuthRepository
from app.repositories.events import EventRepository
from app.repositories.jobs import JobRepository
from app.repositories.library import LibraryRepository
from app.repositories.requests import RequestRepository
from app.services.supervisor import WebOrchestration, WebTaskSupervisor


class FingerprintedStaticFiles(StaticFiles):
    def __init__(self, *, directory: Path, fingerprints: dict[str, str]) -> None:
        super().__init__(directory=directory)
        self.fingerprints = fingerprints

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        query = parse_qs(bytes(scope.get("query_string", b"")).decode("ascii", errors="ignore"))
        supplied = query.get("v", [])
        if supplied == [self.fingerprints.get(path)]:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def _static_fingerprints(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _build_orchestration(settings: Settings, factory: sessionmaker[Session]) -> WebOrchestration:
    from app.services.orchestration import OrchestrationService
    from app.tools.media_sources import build_media_source_tools
    from app.tools.registry import build_default_registry

    registry = build_default_registry(
        settings,
        factory,
        media_source_tools=build_media_source_tools(
            factory,
            enabled_providers=settings.enabled_media_providers,
        ),
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

    static_path = Path(__file__).parent / "static"
    fingerprints = _static_fingerprints(static_path)

    def asset_url(name: str) -> str:
        fingerprint = fingerprints.get(name)
        if fingerprint is None:
            raise ValueError(f"unknown static asset: {name}")
        return f"/static/{quote(name, safe='/')}?v={fingerprint}"

    app.state.asset_fingerprints = fingerprints
    app.state.templates.env.globals["asset_url"] = asset_url

    app.add_middleware(BodyLimitMiddleware, max_bytes=1_048_576)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.effective_trusted_hosts)
    app.add_middleware(AllowedClientMiddleware, settings=settings)
    app.add_middleware(TrustedProxyMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)

    app.mount(
        "/static",
        FingerprintedStaticFiles(directory=static_path, fingerprints=fingerprints),
        name="static",
    )
    for router in (
        auth.router,
        accounts.router,
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
        if (
            error.status_code == 403
            and isinstance(error.detail, dict)
            and error.detail.get("code") == "password_change_required"
            and "text/html" in request.headers.get("accept", "")
        ):
            return RedirectResponse("/account/change-password", status_code=303)
        if error.status_code == status.HTTP_401_UNAUTHORIZED and "text/html" in request.headers.get(
            "accept", ""
        ):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse(
            {"detail": error.detail}, status_code=error.status_code, headers=error.headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> Response:
        # Pydantic's default includes the original input, including passwords.
        return JSONResponse(
            {
                "detail": [
                    {"loc": list(item["loc"]), "type": item["type"], "msg": "Invalid request value"}
                    for item in error.errors()
                ]
            },
            status_code=422,
            headers={"Cache-Control": "no-store"},
        )

    return app
