from __future__ import annotations

import hmac
import secrets
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type
from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import AuthAttempt, User
from app.db.models import Session as DbSession
from app.repositories.events import make_event
from app.services.security import hmac_keyed, sha256_text

REAUTHENTICATION_SECONDS = 300
UserRole = Literal["admin", "user"]


def normalize_username(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def utc_now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class NewSession:
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    absolute_expires_at: datetime


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: str
    user_id: str
    username: str
    csrf_hash: str = field(repr=False)
    absolute_expires_at: datetime
    role: str = "user"
    must_change_password: bool = False
    reauthenticated_at: datetime | None = None


@dataclass(frozen=True)
class LoginResult:
    user_id: str
    must_change_password: bool
    session: NewSession


@dataclass(frozen=True)
class PasswordResult:
    user_id: str
    generated_password: str | None = field(default=None, repr=False)


class AuthenticationError(ValueError):
    pass


class AuthenticationBlocked(AuthenticationError):
    pass


class AuthorizationError(PermissionError):
    pass


class ReauthenticationRequired(AuthorizationError):
    pass


class AccountConflict(ValueError):
    pass


class SetupAlreadyCompleted(ValueError):
    pass


class AuthRepository:
    def __init__(
        self, engine: Engine, session_factory: sessionmaker[Session], settings: Settings
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.settings = settings
        self.password_hasher = PasswordHasher(
            time_cost=3, memory_cost=65_536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID
        )
        self._dummy_hash = self.password_hasher.hash(secrets.token_urlsafe(24))

    @contextmanager
    def _write(self) -> Iterator[Session]:
        """Serialize membership/counter changes without holding a lock during Argon2."""
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def has_users(self) -> bool:
        with self.session_factory() as session:
            return session.scalar(select(User.id).limit(1)) is not None

    def create_initial_admin(self, username: str, password: str) -> str:
        display, normalized = self._validate_credentials(username, password)
        password_hash = self.password_hasher.hash(password)
        with self._write() as session:
            if session.scalar(select(User.id).limit(1)) is not None:
                raise SetupAlreadyCompleted("initial setup has already been completed")
            now = utc_now()
            user = User(
                username=display,
                username_normalized=normalized,
                password_hash=password_hash,
                role="admin",
                is_active=True,
                must_change_password=False,
                password_changed_at=now,
                updated_at=now,
            )
            session.add(user)
            session.flush()
            self._audit(session, "user.created", user.id, reason="initial_setup")
            return user.id

    def consume_setup_attempt(self, address: str) -> None:
        with self._write() as session:
            blocked = self._consume_limit(session, self._key("setup", address), utc_now())
        if blocked:
            raise AuthenticationBlocked("too many setup attempts; try again later")

    def clear_setup_attempts(self, address: str) -> None:
        with self._write() as session:
            session.execute(
                delete(AuthAttempt).where(AuthAttempt.key_hash == self._key("setup", address))
            )

    def reset_admin(
        self,
        username: str | None,
        password: str,
        *,
        recover: bool = False,
        must_change_password: bool = False,
    ) -> str:
        """Local recovery only: never rename/select the oldest account or revoke peers."""
        self._validate_password(password)
        identity = self._validate_username(username) if username is not None else None
        password_hash = self.password_hasher.hash(password)
        with self._write() as session:
            now = utc_now()
            user: User | None
            if identity is None:
                admins = list(
                    session.scalars(select(User).where(User.role == "admin", User.is_active))
                )
                if len(admins) != 1:
                    raise AccountConflict(
                        "specify --username for an explicit administrator account"
                    )
                user = admins[0]
            else:
                user = session.scalar(select(User).where(User.username_normalized == identity[1]))
                if user is None:
                    if session.scalar(select(User.id).limit(1)) is not None:
                        raise LookupError("account not found; recovery never creates a replacement")
                    user = User(
                        username=identity[0],
                        username_normalized=identity[1],
                        password_hash=password_hash,
                        role="admin",
                        is_active=True,
                        must_change_password=must_change_password,
                    )
                    session.add(user)
                    session.flush()
            if (user.role != "admin" or not user.is_active) and not recover:
                raise AccountConflict(
                    "use --recover to explicitly restore this account as an active admin"
                )
            user.role = "admin"
            user.is_active = True
            user.disabled_at = None
            user.password_hash = password_hash
            user.must_change_password = must_change_password
            user.password_changed_at = now
            user.updated_at = now
            self._revoke_sessions(session, user.id, now)
            self._audit(
                session, "user.password_reset_by_admin", user.id, reason="local_cli_recovery"
            )
            return user.id

    def authenticate(self, username: str, password: str, address: str) -> User:
        """Compatibility API for local callers; HTTP login uses the atomic method below."""
        user, _new_session = self._authenticate(username, password, address, issue_session=False)
        return user

    def authenticate_and_create_session(
        self, username: str, password: str, address: str
    ) -> LoginResult:
        user, new_session = self._authenticate(username, password, address, issue_session=True)
        assert new_session is not None
        return LoginResult(user.id, user.must_change_password, new_session)

    def _authenticate(
        self, username: str, password: str, address: str, *, issue_session: bool
    ) -> tuple[User, NewSession | None]:
        if len(username) > 80 or len(password) > 1024:
            self._verify(self._dummy_hash, password[:1024])
            raise AuthenticationError("invalid credentials")
        normalized = normalize_username(username)
        key = self._key("login", normalized, address)
        with self.session_factory() as session:
            blocked = self._is_blocked(session.get(AuthAttempt, key), utc_now())
            user = session.scalar(select(User).where(User.username_normalized == normalized))
            candidate = user.password_hash if user is not None else self._dummy_hash
        verified = self._verify(self._dummy_hash if blocked else candidate, password)
        if blocked:
            raise AuthenticationBlocked("too many attempts; try again later")
        replacement = (
            self.password_hasher.hash(password)
            if verified and self.password_hasher.check_needs_rehash(candidate)
            else None
        )
        error: AuthenticationError | None = None
        new_session = None
        with self._write() as session:
            now = utc_now()
            attempt = session.get(AuthAttempt, key)
            # Reset, disable and concurrent failures may have changed while Argon2 ran.
            current = session.scalar(select(User).where(User.username_normalized == normalized))
            if self._is_blocked(attempt, now):
                error = AuthenticationBlocked("too many attempts; try again later")
            elif (
                not verified
                or current is None
                or not current.is_active
                or current.password_hash != candidate
            ):
                self._record_failure(session, attempt, key, now)
                error = AuthenticationError("invalid credentials")
            else:
                if replacement is not None:
                    current.password_hash = replacement
                current.last_login_at = now
                if attempt is not None:
                    session.delete(attempt)
                if issue_session:
                    new_session = self._new_session(session, current.id, now)
                session.flush()
                user = current
        # Failure counters must commit before exceptions leave the repository.
        if error is not None:
            raise error
        assert user is not None
        return user, new_session

    def create_session(self, user_id: str) -> NewSession:
        """Trusted local/test helper; never call after separate HTTP password verification."""
        with self._write() as session:
            user = session.get(User, user_id)
            if user is None or not user.is_active:
                raise AuthenticationError("account is unavailable")
            return self._new_session(session, user_id, utc_now())

    def _new_session(self, session: Session, user_id: str, now: datetime) -> NewSession:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        absolute = now + timedelta(seconds=self.settings.session_absolute_seconds)
        session.add(
            DbSession(
                token_hash=sha256_text(token),
                user_id=user_id,
                csrf_hash=sha256_text(csrf),
                last_activity_at=now,
                idle_expires_at=now + timedelta(seconds=self.settings.session_idle_seconds),
                absolute_expires_at=absolute,
                reauthenticated_at=None,
            )
        )
        session.flush()
        return NewSession(token, csrf, absolute)

    def resolve_session(
        self, raw_token: str | None, *, touch: bool = True
    ) -> AuthenticatedSession | None:
        if not raw_token or len(raw_token) > 200:
            return None
        now = utc_now()
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(DbSession).where(DbSession.token_hash == sha256_text(raw_token))
            )
            if row is None or not self._session_valid(row, now):
                return None
            if touch and (now - _aware(row.last_activity_at)).total_seconds() >= 60:
                row.last_activity_at = now
                row.idle_expires_at = min(
                    now + timedelta(seconds=self.settings.session_idle_seconds),
                    _aware(row.absolute_expires_at),
                )
            return AuthenticatedSession(
                session_id=row.id,
                user_id=row.user_id,
                username=row.user.username,
                csrf_hash=row.csrf_hash,
                absolute_expires_at=_aware(row.absolute_expires_at),
                role=row.user.role,
                must_change_password=row.user.must_change_password,
                reauthenticated_at=_aware(row.reauthenticated_at)
                if row.reauthenticated_at
                else None,
            )

    @staticmethod
    def _session_valid(row: DbSession, now: datetime) -> bool:
        return (
            row.revoked_at is None
            and _aware(row.idle_expires_at) > now
            and _aware(row.absolute_expires_at) > now
            and row.user.is_active
        )

    def _actor(
        self,
        session: Session,
        authenticated: AuthenticatedSession,
        now: datetime,
        *,
        admin: bool = False,
        recent: bool = False,
        allow_forced: bool = False,
    ) -> DbSession:
        row = session.get(DbSession, authenticated.session_id)
        if row is None or row.user_id != authenticated.user_id or not self._session_valid(row, now):
            raise AuthenticationError("authentication required")
        if not hmac.compare_digest(row.csrf_hash, authenticated.csrf_hash):
            raise AuthenticationError("session changed; sign in again")
        if row.user.must_change_password and not allow_forced:
            raise AuthorizationError("password change required")
        if admin and row.user.role != "admin":
            raise AuthorizationError("administrator access required")
        if recent and (
            row.reauthenticated_at is None
            or not 0
            <= (now - _aware(row.reauthenticated_at)).total_seconds()
            < REAUTHENTICATION_SECONDS
        ):
            raise ReauthenticationRequired("confirm your current password to continue")
        return row

    def reauthenticate(
        self, authenticated: AuthenticatedSession, password: str, address: str
    ) -> datetime:
        key = self._key("reauthentication", authenticated.user_id, address)
        with self.session_factory() as session:
            row = self._actor(session, authenticated, utc_now(), admin=True)
            candidate = row.user.password_hash
            blocked = self._is_blocked(session.get(AuthAttempt, key), utc_now())
        verified = (
            self._verify(self._dummy_hash if blocked else candidate, password[:1024])
            and len(password) <= 1024
        )
        if blocked:
            raise AuthenticationBlocked("too many password confirmations; try again later")
        failure = False
        with self._write() as session:
            now = utc_now()
            row = self._actor(session, authenticated, now, admin=True)
            attempt = session.get(AuthAttempt, key)
            if self._is_blocked(attempt, now):
                raise AuthenticationBlocked("too many password confirmations; try again later")
            if not verified or row.user.password_hash != candidate:
                self._record_failure(session, attempt, key, now)
                failure = True
            else:
                row.reauthenticated_at = now
                if attempt is not None:
                    session.delete(attempt)
        if failure:
            raise AuthenticationError("incorrect current password")
        return now + timedelta(seconds=REAUTHENTICATION_SECONDS)

    def _admin_quota(
        self, authenticated: AuthenticatedSession, operation: str, *, recent: bool = True
    ) -> None:
        with self._write() as session:
            self._actor(session, authenticated, utc_now(), admin=True, recent=recent)
            blocked = self._consume_limit(
                session,
                self._key("admin_action", authenticated.user_id, operation),
                utc_now(),
                limit=20,
                window_seconds=3600,
                block_seconds=3600,
            )
        if blocked:
            raise AuthenticationBlocked("too many account changes; try again later")

    def create_user(
        self,
        authenticated: AuthenticatedSession,
        *,
        username: str,
        password: str | None = None,
        role: UserRole = "user",
        must_change_password: bool = True,
    ) -> PasswordResult:
        self._admin_quota(authenticated, "create", recent=role == "admin")
        generated = secrets.token_urlsafe(24) if password is None else None
        actual = generated if generated is not None else password
        assert actual is not None
        display, normalized = self._validate_credentials(username, actual)
        self._validate_role(role)
        password_hash = self.password_hasher.hash(actual)
        try:
            with self._write() as session:
                now = utc_now()
                self._actor(session, authenticated, now, admin=True, recent=role == "admin")
                if session.scalar(select(User.id).where(User.username_normalized == normalized)):
                    raise AccountConflict("that username is already in use")
                user = User(
                    username=display,
                    username_normalized=normalized,
                    password_hash=password_hash,
                    role=role,
                    is_active=True,
                    must_change_password=must_change_password,
                    password_changed_at=now,
                    created_by_user_id=authenticated.user_id,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                self._audit(
                    session, "user.created", user.id, actor=authenticated.user_id, new_role=role
                )
                return PasswordResult(user.id, generated)
        except IntegrityError as error:
            raise AccountConflict("that username is already in use") from error

    def update_user(
        self,
        authenticated: AuthenticatedSession,
        user_id: str,
        *,
        role: UserRole | None = None,
        is_active: bool | None = None,
        must_change_password: bool | None = None,
    ) -> None:
        if role is not None:
            self._validate_role(role)
        if role is None and is_active is None and must_change_password is None:
            raise ValueError("supply at least one account change")
        with self._write() as session:
            now = utc_now()
            self._actor(session, authenticated, now, admin=True, recent=True)
            target = session.get(User, user_id)
            if target is None:
                raise LookupError("account not found")
            if target.id == authenticated.user_id and (role == "user" or is_active is False):
                raise AccountConflict(
                    "another administrator must deactivate or demote your account"
                )
            active = target.is_active if is_active is None else is_active
            target_role = target.role if role is None else role
            if (
                target.role == "admin"
                and target.is_active
                and not (active and target_role == "admin")
            ):
                count = (
                    session.scalar(
                        select(func.count())
                        .select_from(User)
                        .where(User.role == "admin", User.is_active)
                    )
                    or 0
                )
                if count <= 1:
                    raise AccountConflict("at least one active administrator must remain")
            revoke = False
            if role is not None and role != target.role:
                self._audit(
                    session,
                    "user.role_changed",
                    target.id,
                    actor=authenticated.user_id,
                    old_role=target.role,
                    new_role=role,
                )
                target.role = role
                revoke = True
            if is_active is not None and is_active != target.is_active:
                target.is_active = is_active
                target.disabled_at = None if is_active else now
                self._audit(
                    session,
                    "user.activated" if is_active else "user.deactivated",
                    target.id,
                    actor=authenticated.user_id,
                )
                revoke = revoke or not is_active
            if (
                must_change_password is not None
                and must_change_password != target.must_change_password
            ):
                target.must_change_password = must_change_password
                self._audit(
                    session,
                    "user.password_change_required",
                    target.id,
                    actor=authenticated.user_id,
                    required=must_change_password,
                )
                revoke = True
            if revoke:
                self._revoke_sessions(session, target.id, now)
            target.updated_at = now

    def reset_user_password(
        self,
        authenticated: AuthenticatedSession,
        user_id: str,
        *,
        password: str | None = None,
        must_change_password: bool = True,
    ) -> PasswordResult:
        self._admin_quota(authenticated, "reset")
        if user_id == authenticated.user_id:
            raise AccountConflict("change your own password from Account")
        generated = secrets.token_urlsafe(24) if password is None else None
        actual = generated if generated is not None else password
        assert actual is not None
        self._validate_password(actual)
        password_hash = self.password_hasher.hash(actual)
        with self._write() as session:
            now = utc_now()
            self._actor(session, authenticated, now, admin=True, recent=True)
            user = session.get(User, user_id)
            if user is None:
                raise LookupError("account not found")
            user.password_hash = password_hash
            user.must_change_password = must_change_password
            user.password_changed_at = now
            user.updated_at = now
            self._revoke_sessions(session, user.id, now)
            self._audit(
                session, "user.password_reset_by_admin", user.id, actor=authenticated.user_id
            )
        return PasswordResult(user_id, generated)

    def revoke_user_sessions(self, authenticated: AuthenticatedSession, user_id: str) -> int:
        with self._write() as session:
            now = utc_now()
            self._actor(session, authenticated, now, admin=True, recent=True)
            if user_id == authenticated.user_id:
                raise AccountConflict("manage your own sessions from Account")
            if session.get(User, user_id) is None:
                raise LookupError("account not found")
            count = self._revoke_sessions(session, user_id, now)
            if count:
                self._audit(session, "user.sessions_revoked", user_id, actor=authenticated.user_id)
            return count

    def change_password(
        self,
        authenticated: AuthenticatedSession,
        *,
        current_password: str | None,
        new_password: str,
        confirmation: str,
    ) -> NewSession:
        self._validate_password(new_password)
        if not hmac.compare_digest(new_password.encode(), confirmation.encode()):
            raise ValueError("new passwords do not match")
        with self._write() as session:
            self._actor(session, authenticated, utc_now(), allow_forced=True)
            blocked = self._consume_limit(
                session, self._key("password_change", authenticated.user_id), utc_now()
            )
        if blocked:
            raise AuthenticationBlocked("too many password changes; try again later")
        with self.session_factory() as session:
            row = self._actor(session, authenticated, utc_now(), allow_forced=True)
            candidate = row.user.password_hash
            forced = row.user.must_change_password
        if not forced and (
            current_password is None
            or len(current_password) > 1024
            or not self._verify(candidate, current_password)
        ):
            raise AuthenticationError("incorrect current password")
        if self._verify(candidate, new_password):
            raise ValueError("choose a different new password")
        password_hash = self.password_hasher.hash(new_password)
        with self._write() as session:
            now = utc_now()
            row = self._actor(session, authenticated, now, allow_forced=True)
            if row.user.password_hash != candidate or row.user.must_change_password != forced:
                raise AuthenticationError("account changed; sign in again")
            row.user.password_hash = password_hash
            row.user.must_change_password = False
            row.user.password_changed_at = now
            row.user.updated_at = now
            self._revoke_sessions(session, row.user_id, now)
            new_session = self._new_session(session, row.user_id, now)
            session.execute(
                delete(AuthAttempt).where(
                    AuthAttempt.key_hash == self._key("password_change", row.user_id)
                )
            )
            self._audit(
                session,
                "user.forced_password_change_completed" if forced else "user.password_changed",
                row.user_id,
                actor=row.user_id,
            )
            return new_session

    def revoke_other_sessions(self, authenticated: AuthenticatedSession) -> int:
        with self._write() as session:
            now = utc_now()
            row = self._actor(session, authenticated, now)
            count = self._revoke_sessions(session, row.user_id, now, except_id=row.id)
            if count:
                self._audit(
                    session,
                    "user.sessions_revoked",
                    row.user_id,
                    actor=row.user_id,
                    reason="other_sessions",
                )
            return count

    @staticmethod
    def _revoke_sessions(
        session: Session, user_id: str, now: datetime, *, except_id: str | None = None
    ) -> int:
        statement = update(DbSession).where(
            DbSession.user_id == user_id, DbSession.revoked_at.is_(None)
        )
        if except_id is not None:
            statement = statement.where(DbSession.id != except_id)
        result = session.execute(statement.values(revoked_at=now, reauthenticated_at=None))
        return int(result.rowcount or 0)

    def csrf_matches(self, authenticated: AuthenticatedSession, supplied: str | None) -> bool:
        return bool(
            supplied
            and len(supplied) <= 200
            and hmac.compare_digest(authenticated.csrf_hash, sha256_text(supplied))
        )

    def revoke_session(self, raw_token: str | None) -> None:
        if not raw_token or len(raw_token) > 200:
            return
        with self._write() as session:
            session.execute(
                update(DbSession)
                .where(
                    DbSession.token_hash == sha256_text(raw_token), DbSession.revoked_at.is_(None)
                )
                .values(revoked_at=utc_now(), reauthenticated_at=None)
            )

    def purge_expired(self) -> int:
        now = utc_now()
        with self._write() as session:
            result = session.execute(
                delete(DbSession).where(
                    (DbSession.absolute_expires_at <= now) | (DbSession.idle_expires_at <= now)
                )
            )
            return int(result.rowcount or 0)

    def _key(self, *parts: str) -> str:
        return hmac_keyed(self.settings.auth_hmac_key.get_secret_value(), *parts)

    @staticmethod
    def _is_blocked(attempt: AuthAttempt | None, now: datetime) -> bool:
        return bool(attempt and attempt.blocked_until and _aware(attempt.blocked_until) > now)

    def _consume_limit(
        self,
        session: Session,
        key: str,
        now: datetime,
        *,
        limit: int | None = None,
        window_seconds: int | None = None,
        block_seconds: int | None = None,
    ) -> bool:
        attempt = session.get(AuthAttempt, key)
        if self._is_blocked(attempt, now):
            return True
        maximum = limit or self.settings.auth_max_failures
        window = window_seconds or self.settings.auth_window_seconds
        block = block_seconds or self.settings.auth_block_seconds
        if self._reset_attempt(attempt, now, window):
            session.merge(
                AuthAttempt(key_hash=key, failure_count=1, window_started_at=now, updated_at=now)
            )
            return False
        assert attempt is not None
        if attempt.failure_count >= maximum:
            attempt.blocked_until = now + timedelta(seconds=block)
            attempt.updated_at = now
            return True
        attempt.failure_count += 1
        attempt.updated_at = now
        return False

    @staticmethod
    def _reset_attempt(attempt: AuthAttempt | None, now: datetime, window_seconds: int) -> bool:
        return (
            attempt is None
            or (now - _aware(attempt.window_started_at)).total_seconds() >= window_seconds
            or (attempt.blocked_until is not None and _aware(attempt.blocked_until) <= now)
        )

    def _record_failure(
        self, session: Session, attempt: AuthAttempt | None, key: str, now: datetime
    ) -> None:
        if self._reset_attempt(attempt, now, self.settings.auth_window_seconds):
            attempt = AuthAttempt(
                key_hash=key, failure_count=1, window_started_at=now, updated_at=now
            )
            session.merge(attempt)
        else:
            assert attempt is not None
            attempt.failure_count += 1
            attempt.updated_at = now
        if attempt.failure_count >= self.settings.auth_max_failures:
            attempt.blocked_until = now + timedelta(seconds=self.settings.auth_block_seconds)
            session.merge(attempt)

    def _verify(self, candidate: str, password: str) -> bool:
        try:
            return self.password_hasher.verify(candidate, password)
        except (VerificationError, InvalidHashError):
            return False

    @staticmethod
    def _validate_username(username: str) -> tuple[str, str]:
        display = unicodedata.normalize("NFKC", username).strip()
        normalized = normalize_username(display)
        if not (1 <= len(display) <= 80 and 1 <= len(normalized) <= 80) or any(
            unicodedata.category(character).startswith("C") for character in display
        ):
            raise ValueError("username must contain 1-80 printable characters")
        return display, normalized

    @staticmethod
    def _validate_password(password: str) -> None:
        if not 12 <= len(password) <= 1024:
            raise ValueError("password must be between 12 and 1024 characters")

    @classmethod
    def _validate_credentials(cls, username: str, password: str) -> tuple[str, str]:
        cls._validate_password(password)
        return cls._validate_username(username)

    @staticmethod
    def _validate_role(role: str) -> None:
        if role not in {"admin", "user"}:
            raise ValueError("role must be admin or user")

    @staticmethod
    def _audit(
        session: Session,
        event_type: str,
        target: str,
        *,
        actor: str | None = None,
        **details: object,
    ) -> None:
        session.add(
            make_event(
                session,
                entity_type="user",
                entity_id=target,
                event_type=event_type,
                audience="admin",
                user_id=None,
                message=event_type.replace(".", " ").replace("_", " "),
                details={"actor_user_id": actor, "target_user_id": target, **details},
            )
        )
