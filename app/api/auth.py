from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.dependencies import optional_session
from app.repositories.auth import (
    AuthenticationBlocked,
    AuthenticationError,
    SetupAlreadyCompleted,
)
from app.services.security import (
    CSRF_COOKIE,
    PREAUTH_COOKIE,
    SESSION_COOKIE,
    client_ip,
    issue_preauth_token,
    validate_mutation_headers,
    validate_preauth_token,
)

router = APIRouter()


def _set_session_cookies(
    response: RedirectResponse, request: Request, token: str, csrf: str
) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_absolute_seconds,
        secure=settings.https_enabled,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=settings.session_absolute_seconds,
        secure=settings.https_enabled,
        httponly=False,
        samesite="strict",
        path="/",
    )
    response.delete_cookie(PREAUTH_COOKIE, path="/")


def _preauth_response(
    request: Request, template: str, purpose: str, error: str | None = None
) -> Response:
    settings = request.app.state.settings
    token = issue_preauth_token(settings, purpose, client_ip(request))
    response = cast(
        Response,
        request.app.state.templates.TemplateResponse(
            request=request,
            name=template,
            context={"csrf_token": token.raw, "error": error},
            status_code=400 if error else 200,
        ),
    )
    response.set_cookie(
        PREAUTH_COOKIE,
        token.raw,
        max_age=1200,
        secure=settings.https_enabled,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@router.get("/setup")
def setup_page(request: Request) -> Response:
    if request.app.state.auth.has_users():
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _preauth_response(request, "setup.html", "setup")


@router.post("/setup")
def setup_submit(
    request: Request,
    username: str = Form(max_length=80),
    password: str = Form(max_length=1024),
    csrf_token: str = Form(max_length=200),
    acknowledge_rights: str | None = Form(default=None),
) -> Response:
    settings = request.app.state.settings
    validate_mutation_headers(request, settings)
    if not validate_preauth_token(
        settings,
        "setup",
        client_ip(request),
        request.cookies.get(PREAUTH_COOKIE, ""),
        csrf_token,
    ):
        return _preauth_response(request, "setup.html", "setup", "The form expired. Try again.")
    if acknowledge_rights != "yes":
        return _preauth_response(
            request, "setup.html", "setup", "You must acknowledge the rights notice."
        )
    try:
        request.app.state.auth.consume_setup_attempt(client_ip(request))
        user_id = request.app.state.auth.create_initial_admin(username, password)
        request.app.state.auth.clear_setup_attempts(client_ip(request))
    except AuthenticationBlocked as error:
        response = _preauth_response(request, "setup.html", "setup", str(error))
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        return response
    except SetupAlreadyCompleted:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    except ValueError as error:
        return _preauth_response(request, "setup.html", "setup", str(error))
    session = request.app.state.auth.create_session(user_id)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookies(response, request, session.token, session.csrf_token)
    return response


@router.get("/login")
def login_page(request: Request) -> Response:
    if not request.app.state.auth.has_users():
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    if optional_session(request, request.app.state.auth) is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return _preauth_response(request, "login.html", "login")


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(max_length=80),
    password: str = Form(max_length=1024),
    csrf_token: str = Form(max_length=200),
) -> Response:
    settings = request.app.state.settings
    validate_mutation_headers(request, settings)
    if not validate_preauth_token(
        settings,
        "login",
        client_ip(request),
        request.cookies.get(PREAUTH_COOKIE, ""),
        csrf_token,
    ):
        return _preauth_response(request, "login.html", "login", "The form expired. Try again.")
    try:
        user = request.app.state.auth.authenticate(username, password, client_ip(request))
    except AuthenticationBlocked as error:
        response = _preauth_response(request, "login.html", "login", str(error))
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        return response
    except AuthenticationError:
        return _preauth_response(request, "login.html", "login", "Invalid username or password.")
    session = request.app.state.auth.create_session(user.id)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookies(response, request, session.token, session.csrf_token)
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    authenticated = request.app.state.auth.resolve_session(request.cookies.get(SESSION_COOKIE))
    if authenticated is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    validate_mutation_headers(request, request.app.state.settings)
    form = await request.form()
    supplied = str(form.get("csrf_token", ""))
    if not request.app.state.auth.csrf_matches(authenticated, supplied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
    request.app.state.auth.revoke_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response
