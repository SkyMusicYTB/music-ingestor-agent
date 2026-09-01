from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status

from app.repositories.auth import AuthenticatedSession, AuthRepository
from app.services.security import SESSION_COOKIE, validate_mutation_headers


def auth_repository(request: Request) -> AuthRepository:
    return cast(AuthRepository, request.app.state.auth)


def optional_session(
    request: Request, repository: Annotated[AuthRepository, Depends(auth_repository)]
) -> AuthenticatedSession | None:
    return repository.resolve_session(request.cookies.get(SESSION_COOKIE))


def require_session(
    authenticated: Annotated[AuthenticatedSession | None, Depends(optional_session)],
) -> AuthenticatedSession:
    if authenticated is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return authenticated


def require_csrf(
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_session)],
    repository: Annotated[AuthRepository, Depends(auth_repository)],
) -> AuthenticatedSession:
    validate_mutation_headers(request, request.app.state.settings)
    supplied = request.headers.get("x-csrf-token")
    if not repository.csrf_matches(authenticated, supplied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
    return authenticated


CurrentSession = Annotated[AuthenticatedSession, Depends(require_session)]
CsrfSession = Annotated[AuthenticatedSession, Depends(require_csrf)]
