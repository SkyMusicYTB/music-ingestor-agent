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


def require_authenticated_session(
    authenticated: Annotated[AuthenticatedSession | None, Depends(optional_session)],
) -> AuthenticatedSession:
    if authenticated is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return authenticated


def require_session(
    authenticated: Annotated[AuthenticatedSession, Depends(require_authenticated_session)],
) -> AuthenticatedSession:
    if authenticated.must_change_password:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "password_change_required", "message": "Change your password to continue."},
        )
    return authenticated


def require_background_session(
    request: Request,
    repository: Annotated[AuthRepository, Depends(auth_repository)],
) -> AuthenticatedSession:
    """Automatic browser traffic must not extend the interactive idle deadline."""
    authenticated = repository.resolve_session(request.cookies.get(SESSION_COOKIE), touch=False)
    return require_session(require_authenticated_session(authenticated))


def require_fragment_session(
    request: Request,
    repository: Annotated[AuthRepository, Depends(auth_repository)],
    fragment: bool = False,
) -> AuthenticatedSession:
    """Full-page browsing is activity; live HTML fragments are background polling."""
    authenticated = repository.resolve_session(
        request.cookies.get(SESSION_COOKIE), touch=not fragment
    )
    return require_session(require_authenticated_session(authenticated))


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
BackgroundSession = Annotated[AuthenticatedSession, Depends(require_background_session)]
FragmentSession = Annotated[AuthenticatedSession, Depends(require_fragment_session)]
CsrfSession = Annotated[AuthenticatedSession, Depends(require_csrf)]


def require_admin(authenticated: CurrentSession) -> AuthenticatedSession:
    if authenticated.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator access required")
    return authenticated


def require_admin_csrf(
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_admin)],
    repository: Annotated[AuthRepository, Depends(auth_repository)],
) -> AuthenticatedSession:
    validate_mutation_headers(request, request.app.state.settings)
    if not repository.csrf_matches(authenticated, request.headers.get("x-csrf-token")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
    return authenticated


def require_password_change_csrf(
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_authenticated_session)],
    repository: Annotated[AuthRepository, Depends(auth_repository)],
) -> AuthenticatedSession:
    validate_mutation_headers(request, request.app.state.settings)
    if not repository.csrf_matches(authenticated, request.headers.get("x-csrf-token")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
    return authenticated


CurrentAdmin = Annotated[AuthenticatedSession, Depends(require_admin)]
AdminCsrfSession = Annotated[AuthenticatedSession, Depends(require_admin_csrf)]
PasswordChangeSession = Annotated[AuthenticatedSession, Depends(require_authenticated_session)]
PasswordChangeCsrfSession = Annotated[AuthenticatedSession, Depends(require_password_change_csrf)]
