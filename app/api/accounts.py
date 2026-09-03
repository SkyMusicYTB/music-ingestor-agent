from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import Response

from app.api.auth import _set_session_cookies
from app.api.dependencies import (
    AdminCsrfSession,
    CsrfSession,
    CurrentAdmin,
    CurrentSession,
    PasswordChangeCsrfSession,
    PasswordChangeSession,
)
from app.db.models import DownloadJob, OpenAICall, RequestTrack, User
from app.db.models import Request as DbRequest
from app.db.models import Session as DbSession
from app.repositories.auth import (
    AccountConflict,
    AuthenticatedSession,
    AuthenticationBlocked,
    AuthenticationError,
    AuthorizationError,
    PasswordResult,
    ReauthenticationRequired,
    normalize_username,
)
from app.services.security import CSRF_COOKIE, client_ip

router = APIRouter(tags=["accounts"])


class AccountBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class CreateUserBody(AccountBody):
    username: str = Field(min_length=1, max_length=80)
    role: Literal["admin", "user"] = "user"
    temporary_password: SecretStr | None = Field(default=None, min_length=12, max_length=1024)
    must_change_password: bool = True


class UpdateUserBody(AccountBody):
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None
    must_change_password: bool | None = None


class ResetPasswordBody(AccountBody):
    temporary_password: SecretStr | None = Field(default=None, min_length=12, max_length=1024)
    must_change_password: bool = True


class ReauthenticateBody(AccountBody):
    current_password: SecretStr = Field(min_length=1, max_length=1024)


class ChangePasswordBody(AccountBody):
    current_password: SecretStr | None = Field(default=None, max_length=1024)
    new_password: SecretStr = Field(min_length=12, max_length=1024)
    confirmation: SecretStr = Field(min_length=12, max_length=1024)


class AccountProfile(AccountBody):
    id: str
    username: str
    role: Literal["admin", "user"]
    is_active: bool
    must_change_password: bool
    password_changed_at: datetime | None
    disabled_at: datetime | None
    created_at: datetime
    last_login_at: datetime | None


class AccountUsage(AccountBody):
    calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    estimated_cost_microusd: int | None = Field(ge=0)


class AdminUserProfile(AccountProfile):
    creator: str | None
    active_sessions: int = Field(ge=0)
    request_count: int = Field(ge=0)
    download_count: int = Field(ge=0)
    usage: AccountUsage


class UserPageResponse(AccountBody):
    items: list[AdminUserProfile] = Field(max_length=100)
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: Literal[25, 50, 100]


class PasswordResponse(AccountBody):
    user_id: str
    temporary_password: str | None = Field(repr=False)
    password_visible_once: bool


class UpdatedResponse(AccountBody):
    status: Literal["updated"] = "updated"


class ChangedPasswordResponse(AccountBody):
    status: Literal["password_changed"] = "password_changed"
    redirect: Literal["/"] = "/"


class RevokedSessionsResponse(AccountBody):
    revoked_sessions: int = Field(ge=0)


class ReauthenticatedResponse(AccountBody):
    reauthenticated_until: datetime


async def _json_body(request: Request) -> None:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415, "use application/json for account actions")
    if len(await request.body()) > 16_384:
        raise HTTPException(413, "account request body is too large")


@contextmanager
def _account_errors() -> Iterator[None]:
    try:
        yield
    except ReauthenticationRequired as error:
        raise HTTPException(
            403, {"code": "reauthentication_required", "message": str(error)}
        ) from error
    except AuthenticationBlocked as error:
        raise HTTPException(429, str(error), headers={"Retry-After": "60"}) from error
    except AuthorizationError as error:
        raise HTTPException(403, str(error)) from error
    except AuthenticationError as error:
        raise HTTPException(403, str(error)) from error
    except AccountConflict as error:
        raise HTTPException(409, str(error)) from error
    except LookupError as error:
        raise HTTPException(404, "account not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


def _no_store(payload: BaseModel, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload.model_dump(mode="json"),
        status_code=status_code,
        headers={"Cache-Control": "private, no-store"},
    )


def _safe_user(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "password_changed_at": user.password_changed_at,
        "disabled_at": user.disabled_at,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


def _user_page(
    factory: sessionmaker[Session],
    *,
    query: str,
    page: int,
    page_size: int,
) -> dict[str, object]:
    if page_size not in {25, 50, 100}:
        raise HTTPException(422, "page_size must be 25, 50 or 100")
    now = datetime.now(UTC)
    with factory() as session:
        predicate = User.username_normalized.contains(normalize_username(query), autoescape=True)
        total = session.scalar(select(func.count()).select_from(User).where(predicate)) or 0
        users = list(
            session.scalars(
                select(User)
                .where(predicate)
                .order_by(User.username_normalized, User.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        identifiers = [user.id for user in users]
        creator_ids = [user.created_by_user_id for user in users if user.created_by_user_id]
        creators = {
            row[0]: row[1]
            for row in session.execute(
                select(User.id, User.username).where(User.id.in_(creator_ids))
            )
        }
        sessions = {
            row[0]: row[1]
            for row in session.execute(
                select(DbSession.user_id, func.count(DbSession.id))
                .where(
                    DbSession.user_id.in_(identifiers),
                    DbSession.revoked_at.is_(None),
                    DbSession.absolute_expires_at > now,
                    DbSession.idle_expires_at > now,
                )
                .group_by(DbSession.user_id)
            )
        }
        requests = {
            row[0]: row[1]
            for row in session.execute(
                select(DbRequest.user_id, func.count(DbRequest.id))
                .where(DbRequest.user_id.in_(identifiers))
                .group_by(DbRequest.user_id)
            )
        }
        jobs = {
            row[0]: row[1]
            for row in session.execute(
                select(DbRequest.user_id, func.count(DownloadJob.id))
                .join(RequestTrack, RequestTrack.request_id == DbRequest.id)
                .join(DownloadJob, DownloadJob.request_track_id == RequestTrack.id)
                .where(DbRequest.user_id.in_(identifiers))
                .group_by(DbRequest.user_id)
            )
        }
        accounted = func.sum(case((OpenAICall.usage_reported.is_(True), 1), else_=0))
        cost = case(
            (
                (accounted == func.count(OpenAICall.estimated_cost_microusd))
                & (accounted == func.count(OpenAICall.id)),
                func.sum(OpenAICall.estimated_cost_microusd),
            ),
            else_=None,
        )
        usage_rows = session.execute(
            select(
                OpenAICall.owner_user_id,
                func.count(OpenAICall.id),
                func.sum(OpenAICall.total_tokens),
                cost,
            )
            .where(OpenAICall.owner_user_id.in_(identifiers))
            .group_by(OpenAICall.owner_user_id)
        ).all()
        usage = {
            row[0]: {"calls": row[1], "tokens": row[2] or 0, "estimated_cost_microusd": row[3]}
            for row in usage_rows
        }
        items = [
            {
                **_safe_user(user),
                "creator": creators.get(user.created_by_user_id)
                if user.created_by_user_id
                else None,
                "active_sessions": sessions.get(user.id, 0),
                "request_count": requests.get(user.id, 0),
                "download_count": jobs.get(user.id, 0),
                "usage": usage.get(
                    user.id, {"calls": 0, "tokens": 0, "estimated_cost_microusd": 0}
                ),
            }
            for user in users
        ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _account_snapshot(request: Request, authenticated: AuthenticatedSession) -> dict[str, object]:
    with request.app.state.session_factory() as session:
        user = session.get(User, authenticated.user_id)
        if user is None:
            raise HTTPException(401, "authentication required")
        return _safe_user(user)


def _render(
    request: Request,
    authenticated: AuthenticatedSession,
    name: str,
    **values: object,
) -> Response:
    response = cast(
        Response,
        request.app.state.templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "user": authenticated,
                "csrf_token": request.cookies.get(CSRF_COOKIE, ""),
                "app_version": request.app.state.settings.app_version,
                **values,
            },
        ),
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("/admin/users")
def users_page(
    request: Request,
    authenticated: CurrentAdmin,
    q: str = Query(default="", max_length=80),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=25, ge=1, le=100),
) -> Response:
    return _render(
        request,
        authenticated,
        "admin_users.html",
        result=_user_page(
            request.app.state.session_factory, query=q, page=page, page_size=page_size
        ),
        q=q,
    )


@router.get("/account")
def account_page(request: Request, authenticated: CurrentSession) -> Response:
    return _render(
        request, authenticated, "account.html", account=_account_snapshot(request, authenticated)
    )


@router.get("/account/change-password")
def password_change_page(request: Request, authenticated: PasswordChangeSession) -> Response:
    return _render(
        request, authenticated, "change_password.html", forced=authenticated.must_change_password
    )


@router.get("/api/v1/admin/users", response_model=UserPageResponse)
def list_users(
    request: Request,
    authenticated: CurrentAdmin,
    q: str = Query(default="", max_length=80),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=25, ge=1, le=100),
) -> Response:
    return _no_store(
        UserPageResponse.model_validate(
            _user_page(request.app.state.session_factory, query=q, page=page, page_size=page_size)
        )
    )


@router.post(
    "/api/v1/admin/users",
    dependencies=[Depends(_json_body)],
    response_model=PasswordResponse,
    status_code=201,
)
def create_user(
    body: CreateUserBody, request: Request, authenticated: AdminCsrfSession
) -> Response:
    with _account_errors():
        result = request.app.state.auth.create_user(
            authenticated,
            username=body.username,
            role=body.role,
            password=body.temporary_password.get_secret_value()
            if body.temporary_password
            else None,
            must_change_password=body.must_change_password,
        )
    return _password_response(result, status_code=201)


def _password_response(result: PasswordResult, *, status_code: int = 200) -> Response:
    return _no_store(
        PasswordResponse(
            user_id=result.user_id,
            temporary_password=result.generated_password,
            password_visible_once=result.generated_password is not None,
        ),
        status_code=status_code,
    )


@router.patch(
    "/api/v1/admin/users/{user_id}",
    dependencies=[Depends(_json_body)],
    response_model=UpdatedResponse,
)
def update_user(
    user_id: UUID, body: UpdateUserBody, request: Request, authenticated: AdminCsrfSession
) -> Response:
    with _account_errors():
        request.app.state.auth.update_user(
            authenticated,
            str(user_id),
            role=body.role,
            is_active=body.is_active,
            must_change_password=body.must_change_password,
        )
    return _no_store(UpdatedResponse())


@router.post(
    "/api/v1/admin/users/{user_id}/reset-password",
    dependencies=[Depends(_json_body)],
    response_model=PasswordResponse,
)
def reset_password(
    user_id: UUID, body: ResetPasswordBody, request: Request, authenticated: AdminCsrfSession
) -> Response:
    with _account_errors():
        result = request.app.state.auth.reset_user_password(
            authenticated,
            str(user_id),
            password=body.temporary_password.get_secret_value()
            if body.temporary_password
            else None,
            must_change_password=body.must_change_password,
        )
    return _password_response(result)


@router.post(
    "/api/v1/admin/users/{user_id}/revoke-sessions",
    dependencies=[Depends(_json_body)],
    response_model=RevokedSessionsResponse,
)
def revoke_sessions(
    user_id: UUID, body: AccountBody, request: Request, authenticated: AdminCsrfSession
) -> Response:
    with _account_errors():
        count = request.app.state.auth.revoke_user_sessions(authenticated, str(user_id))
    return _no_store(RevokedSessionsResponse(revoked_sessions=count))


@router.post(
    "/api/v1/admin/reauthenticate",
    dependencies=[Depends(_json_body)],
    response_model=ReauthenticatedResponse,
)
def reauthenticate(
    body: ReauthenticateBody, request: Request, authenticated: AdminCsrfSession
) -> Response:
    with _account_errors():
        expires = request.app.state.auth.reauthenticate(
            authenticated, body.current_password.get_secret_value(), client_ip(request)
        )
    return _no_store(ReauthenticatedResponse(reauthenticated_until=expires))


@router.get("/api/v1/account", response_model=AccountProfile)
def get_account(request: Request, authenticated: CurrentSession) -> Response:
    return _no_store(AccountProfile.model_validate(_account_snapshot(request, authenticated)))


@router.post(
    "/api/v1/account/change-password",
    dependencies=[Depends(_json_body)],
    response_model=ChangedPasswordResponse,
)
def change_password(
    body: ChangePasswordBody, request: Request, authenticated: PasswordChangeCsrfSession
) -> Response:
    with _account_errors():
        new_session = request.app.state.auth.change_password(
            authenticated,
            current_password=body.current_password.get_secret_value()
            if body.current_password
            else None,
            new_password=body.new_password.get_secret_value(),
            confirmation=body.confirmation.get_secret_value(),
        )
    response = _no_store(ChangedPasswordResponse())
    _set_session_cookies(response, request, new_session.token, new_session.csrf_token)
    return response


@router.post(
    "/api/v1/account/revoke-other-sessions",
    dependencies=[Depends(_json_body)],
    response_model=RevokedSessionsResponse,
)
def revoke_other_sessions(
    body: AccountBody, request: Request, authenticated: CsrfSession
) -> Response:
    with _account_errors():
        count = request.app.state.auth.revoke_other_sessions(authenticated)
    return _no_store(RevokedSessionsResponse(revoked_sessions=count))
