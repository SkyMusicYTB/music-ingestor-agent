from __future__ import annotations

import hmac
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type
from sqlalchemy import Engine, delete, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.ids import uuid7
from app.db.models import AuthAttempt, User
from app.db.models import Session as DbSession
from app.services.security import hmac_keyed, sha256_text


def normalize_username(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def utc_now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class NewSession:
    token: str
    csrf_token: str
    absolute_expires_at: datetime


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: str
    user_id: str
    username: str
    csrf_hash: str
    absolute_expires_at: datetime


class AuthenticationError(ValueError):
    pass


class AuthenticationBlocked(AuthenticationError):
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
            time_cost=3,
            memory_cost=65_536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_hash = self.password_hasher.hash(secrets.token_urlsafe(24))

    def has_users(self) -> bool:
        with self.session_factory() as session:
            return session.scalar(select(User.id).limit(1)) is not None

    def create_initial_admin(self, username: str, password: str) -> str:
        display, normalized = self._validate_credentials(username, password)
        password_hash = self.password_hasher.hash(password)
        user_id = uuid7()
        connection = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        transaction_active = False
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            transaction_active = True
            existing = connection.execute(select(User.id).limit(1)).scalar_one_or_none()
            if existing is not None:
                raise SetupAlreadyCompleted("initial setup has already been completed")
            connection.execute(
                insert(User).values(
                    id=user_id,
                    username=display,
                    username_normalized=normalized,
                    password_hash=password_hash,
                    is_active=True,
                    created_at=utc_now(),
                )
            )
            connection.exec_driver_sql("COMMIT")
            transaction_active = False
        except Exception:
            if transaction_active:
                connection.exec_driver_sql("ROLLBACK")
            raise
        finally:
            connection.close()
        return user_id

    def consume_setup_attempt(self, address: str) -> None:
        """Persistently throttle expensive password hashing during first-run setup."""
        key = hmac_keyed(self.settings.auth_hmac_key.get_secret_value(), "setup", address)
        now = utc_now()
        window = timedelta(seconds=self.settings.auth_window_seconds)
        with self.session_factory.begin() as session:
            attempt = session.get(AuthAttempt, key)
            if attempt and attempt.blocked_until and _aware(attempt.blocked_until) > now:
                raise AuthenticationBlocked("too many setup attempts; try again later")
            if attempt is None or now - _aware(attempt.window_started_at) > window:
                session.merge(
                    AuthAttempt(
                        key_hash=key,
                        failure_count=1,
                        window_started_at=now,
                        updated_at=now,
                    )
                )
                return
            if attempt.failure_count >= self.settings.auth_max_failures:
                attempt.blocked_until = now + timedelta(seconds=self.settings.auth_block_seconds)
                raise AuthenticationBlocked("too many setup attempts; try again later")
            attempt.failure_count += 1
            attempt.updated_at = now

    def clear_setup_attempts(self, address: str) -> None:
        key = hmac_keyed(self.settings.auth_hmac_key.get_secret_value(), "setup", address)
        with self.session_factory.begin() as session:
            session.execute(delete(AuthAttempt).where(AuthAttempt.key_hash == key))

    def reset_admin(self, username: str, password: str) -> str:
        display, normalized = self._validate_credentials(username, password)
        password_hash = self.password_hasher.hash(password)
        now = utc_now()
        with self.session_factory.begin() as session:
            user = session.scalar(select(User).order_by(User.created_at).limit(1))
            if user is None:
                user = User(
                    username=display,
                    username_normalized=normalized,
                    password_hash=password_hash,
                )
                session.add(user)
            else:
                user.username = display
                user.username_normalized = normalized
                user.password_hash = password_hash
                user.is_active = True
            session.execute(update(DbSession).values(revoked_at=now))
            session.flush()
            return user.id

    def authenticate(self, username: str, password: str, address: str) -> User:
        if len(username) > 80 or len(password) > 1024:
            self._dummy_verify(password[:1024])
            raise AuthenticationError("invalid credentials")
        normalized = normalize_username(username)
        key = self._attempt_key(normalized, address)
        now = utc_now()
        with self.session_factory.begin() as session:
            attempt = session.get(AuthAttempt, key)
            if attempt and attempt.blocked_until and _aware(attempt.blocked_until) > now:
                self._dummy_verify(password)
                raise AuthenticationBlocked("too many attempts; try again later")
            user = session.scalar(select(User).where(User.username_normalized == normalized))
            candidate = user.password_hash if user is not None else self._dummy_hash
            verified = False
            try:
                verified = self.password_hasher.verify(candidate, password)
            except (VerifyMismatchError, InvalidHashError):
                verified = False
            if not verified or user is None or not user.is_active:
                self._record_failure(session, attempt, key, now)
                raise AuthenticationError("invalid credentials")
            if self.password_hasher.check_needs_rehash(user.password_hash):
                user.password_hash = self.password_hasher.hash(password)
            user.last_login_at = now
            if attempt is not None:
                session.delete(attempt)
            session.flush()
            return user

    def create_session(self, user_id: str) -> NewSession:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = utc_now()
        absolute = now + timedelta(seconds=self.settings.session_absolute_seconds)
        with self.session_factory.begin() as session:
            session.add(
                DbSession(
                    token_hash=sha256_text(token),
                    user_id=user_id,
                    csrf_hash=sha256_text(csrf),
                    last_activity_at=now,
                    idle_expires_at=now + timedelta(seconds=self.settings.session_idle_seconds),
                    absolute_expires_at=absolute,
                )
            )
        return NewSession(token=token, csrf_token=csrf, absolute_expires_at=absolute)

    def resolve_session(self, raw_token: str | None) -> AuthenticatedSession | None:
        if raw_token is None or len(raw_token) > 200:
            return None
        now = utc_now()
        token_hash = sha256_text(raw_token)
        with self.session_factory.begin() as session:
            db_session = session.scalar(select(DbSession).where(DbSession.token_hash == token_hash))
            if db_session is None or db_session.revoked_at is not None:
                return None
            if (
                _aware(db_session.idle_expires_at) <= now
                or _aware(db_session.absolute_expires_at) <= now
                or not db_session.user.is_active
            ):
                db_session.revoked_at = now
                return None
            if (now - _aware(db_session.last_activity_at)).total_seconds() >= 60:
                db_session.last_activity_at = now
                db_session.idle_expires_at = min(
                    now + timedelta(seconds=self.settings.session_idle_seconds),
                    _aware(db_session.absolute_expires_at),
                )
            return AuthenticatedSession(
                session_id=db_session.id,
                user_id=db_session.user_id,
                username=db_session.user.username,
                csrf_hash=db_session.csrf_hash,
                absolute_expires_at=_aware(db_session.absolute_expires_at),
            )

    def csrf_matches(self, authenticated: AuthenticatedSession, supplied: str | None) -> bool:
        if supplied is None or len(supplied) > 200:
            return False
        return hmac.compare_digest(authenticated.csrf_hash, sha256_text(supplied))

    def revoke_session(self, raw_token: str | None) -> None:
        if raw_token is None or len(raw_token) > 200:
            return
        with self.session_factory.begin() as session:
            session.execute(
                update(DbSession)
                .where(
                    DbSession.token_hash == sha256_text(raw_token), DbSession.revoked_at.is_(None)
                )
                .values(revoked_at=utc_now())
            )

    def purge_expired(self) -> int:
        now = utc_now()
        with self.session_factory.begin() as session:
            result = session.execute(
                delete(DbSession).where(
                    (DbSession.absolute_expires_at <= now) | (DbSession.idle_expires_at <= now)
                )
            )
            return int(result.rowcount or 0)

    def _record_failure(
        self, session: Session, attempt: AuthAttempt | None, key: str, now: datetime
    ) -> None:
        window = timedelta(seconds=self.settings.auth_window_seconds)
        if attempt is None or now - _aware(attempt.window_started_at) > window:
            attempt = AuthAttempt(
                key_hash=key, failure_count=1, window_started_at=now, updated_at=now
            )
            session.merge(attempt)
            return
        attempt.failure_count += 1
        attempt.updated_at = now
        if attempt.failure_count >= self.settings.auth_max_failures:
            attempt.blocked_until = now + timedelta(seconds=self.settings.auth_block_seconds)

    def _attempt_key(self, normalized_username: str, address: str) -> str:
        return hmac_keyed(
            self.settings.auth_hmac_key.get_secret_value(), "login", normalized_username, address
        )

    def _dummy_verify(self, password: str) -> None:
        try:
            self.password_hasher.verify(self._dummy_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            pass

    @staticmethod
    def _validate_credentials(username: str, password: str) -> tuple[str, str]:
        display = unicodedata.normalize("NFKC", username).strip()
        normalized = normalize_username(display)
        if not (1 <= len(display) <= 80) or not normalized:
            raise ValueError("username must be between 1 and 80 characters")
        if len(password) < 12 or len(password) > 1024:
            raise ValueError("password must be between 12 and 1024 characters")
        return display, normalized
