from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from argon2 import PasswordHasher
from sqlalchemy import func, select

import app.repositories.auth as auth_module
from app.db.models import AuthAttempt, Event, User
from app.db.models import Session as DbSession
from app.repositories.auth import (
    AccountConflict,
    AuthenticationBlocked,
    AuthenticationError,
    AuthorizationError,
    AuthRepository,
    ReauthenticationRequired,
    SetupAlreadyCompleted,
    utc_now,
)

PASSWORD = "synthetic-account-test-password"  # noqa: S105
NEW_PASSWORD = "replacement-account-test-password"  # noqa: S105


@pytest.fixture
def accounts(engine, session_factory, settings):
    repository = AuthRepository(engine, session_factory, settings)
    user_id = repository.create_initial_admin("owner", PASSWORD)
    token = repository.create_session(user_id)
    actor = repository.resolve_session(token.token)
    assert actor is not None
    return repository, actor, token


def _regular(repository, actor, *, username="alice", forced=False):
    result = repository.create_user(
        actor, username=username, password=PASSWORD, must_change_password=forced
    )
    login = repository.authenticate_and_create_session(username, PASSWORD, "127.0.0.1")
    authenticated = repository.resolve_session(login.session.token)
    assert authenticated is not None
    return result.user_id, authenticated, login.session


def _reauth(repository, actor):
    return repository.reauthenticate(actor, PASSWORD, "127.0.0.1")


def test_initial_admin_and_additional_default_role(accounts, session_factory):
    repository, actor, _token = accounts
    result = repository.create_user(actor, username="alice")
    assert actor.role == "admin"
    assert result.generated_password and len(result.generated_password) >= 20
    with session_factory() as session:
        user = session.get(User, result.user_id)
        assert user.role == "user"
        assert user.must_change_password is True
        assert user.created_by_user_id == actor.user_id
        assert repository.password_hasher.verify(user.password_hash, result.generated_password)
        assert result.generated_password not in user.password_hash
        events = list(session.scalars(select(Event)))
        assert events and all(
            event.audience == "admin" and event.user_id is None for event in events
        )
        assert all(result.generated_password not in event.details_json for event in events)
    assert result.generated_password not in repr(result)
    with pytest.raises(SetupAlreadyCompleted):
        repository.create_initial_admin("second", PASSWORD)


def test_creation_duplicate_username_never_resets_password(accounts, session_factory):
    repository, actor, _token = accounts
    first = repository.create_user(
        actor, username="\uff21\uff4c\uff49\uff43\uff45", password=PASSWORD
    )
    with session_factory() as session:
        original_hash = session.get(User, first.user_id).password_hash
    with pytest.raises(AccountConflict, match="already in use"):
        repository.create_user(actor, username=" alice ", password=NEW_PASSWORD)
    with session_factory() as session:
        assert session.get(User, first.user_id).password_hash == original_hash


@pytest.mark.parametrize("username", ["", " ", "a" * 81, "ali\nce", "al\x00ice"])
def test_username_policy(accounts, username):
    repository, actor, _token = accounts
    with pytest.raises(ValueError):
        repository.create_user(actor, username=username, password=PASSWORD)


def test_create_admin_requires_reauthentication(accounts):
    repository, actor, _token = accounts
    with pytest.raises(ReauthenticationRequired):
        repository.create_user(actor, username="second", password=PASSWORD, role="admin")
    _reauth(repository, actor)
    result = repository.create_user(
        actor, username="second", password=PASSWORD, role="admin", must_change_password=False
    )
    assert result.user_id


def test_role_and_active_state_are_read_live(accounts, session_factory):
    repository, actor, token = accounts
    with session_factory.begin() as session:
        session.get(User, actor.user_id).role = "user"
    assert repository.resolve_session(token.token).role == "user"
    with pytest.raises(AuthorizationError):
        repository.create_user(actor, username="forbidden", password=PASSWORD)
    with session_factory.begin() as session:
        session.get(User, actor.user_id).is_active = False
    assert repository.resolve_session(token.token) is None


def test_password_change_rotates_and_revokes_sessions(accounts):
    repository, actor, _token = accounts
    user_id, user, original = _regular(repository, actor)
    other = repository.create_session(user_id)
    with pytest.raises(AuthenticationError, match="current password"):
        repository.change_password(
            user,
            current_password="wrong",  # noqa: S106 - deliberately incorrect fixture
            new_password=NEW_PASSWORD,
            confirmation=NEW_PASSWORD,
        )
    with pytest.raises(ValueError, match="match"):
        repository.change_password(
            user, current_password=PASSWORD, new_password=NEW_PASSWORD, confirmation=PASSWORD
        )
    replacement = repository.change_password(
        user, current_password=PASSWORD, new_password=NEW_PASSWORD, confirmation=NEW_PASSWORD
    )
    assert replacement.token != original.token
    assert replacement.csrf_token != original.csrf_token
    assert repository.resolve_session(original.token) is None
    assert repository.resolve_session(other.token) is None
    assert repository.resolve_session(replacement.token).user_id == user_id
    assert repository.resolve_session(replacement.token).reauthenticated_at is None


def test_forced_change_replaces_temporary_password(accounts, session_factory):
    repository, actor, _token = accounts
    user_id, user, original = _regular(repository, actor, forced=True)
    with pytest.raises(ValueError, match="different"):
        repository.change_password(
            user, current_password=None, new_password=PASSWORD, confirmation=PASSWORD
        )
    with pytest.raises(AuthorizationError):
        repository.revoke_other_sessions(user)
    replacement = repository.change_password(
        user, current_password=None, new_password=NEW_PASSWORD, confirmation=NEW_PASSWORD
    )
    assert repository.resolve_session(original.token) is None
    assert not repository.resolve_session(replacement.token).must_change_password
    with session_factory() as session:
        assert session.get(User, user_id).password_changed_at is not None
        assert session.scalar(
            select(Event.id).where(Event.event_type == "user.forced_password_change_completed")
        )


def test_revoke_other_and_target_sessions_are_idempotent(accounts):
    repository, actor, token = accounts
    user_id, user, first = _regular(repository, actor)
    second = repository.create_session(user_id)
    assert repository.revoke_other_sessions(user) == 1
    assert repository.revoke_other_sessions(user) == 0
    assert repository.resolve_session(second.token) is None
    assert repository.resolve_session(first.token) is not None
    with pytest.raises(ReauthenticationRequired):
        repository.revoke_user_sessions(actor, user_id)
    _reauth(repository, actor)
    assert repository.revoke_user_sessions(actor, user_id) == 1
    assert repository.revoke_user_sessions(actor, user_id) == 0
    assert repository.resolve_session(first.token) is None
    assert repository.resolve_session(token.token) is not None


def test_deactivate_activate_never_revives_sessions(accounts, session_factory):
    repository, actor, _token = accounts
    user_id, _user, token = _regular(repository, actor)
    _reauth(repository, actor)
    repository.update_user(actor, user_id, is_active=False)
    repository.update_user(actor, user_id, is_active=False)
    assert repository.resolve_session(token.token) is None
    with session_factory() as session:
        assert session.get(User, user_id).disabled_at is not None
    repository.update_user(actor, user_id, is_active=True)
    assert repository.resolve_session(token.token) is None
    login = repository.authenticate_and_create_session("alice", PASSWORD, "127.0.0.1")
    assert login.user_id == user_id


def test_admin_password_reset_preserves_target_id_and_revokes_only_target(
    accounts, session_factory
):
    repository, actor, owner_token = accounts
    user_id, _user, old_token = _regular(repository, actor)
    _reauth(repository, actor)
    result = repository.reset_user_password(actor, user_id)
    assert result.user_id == user_id
    assert result.generated_password
    assert repository.resolve_session(old_token.token) is None
    assert repository.resolve_session(owner_token.token) is not None
    with session_factory() as session:
        user = session.get(User, user_id)
        assert user.username == "alice"
        assert user.must_change_password
    with pytest.raises(AccountConflict, match="own password"):
        repository.reset_user_password(actor, actor.user_id)


def test_admin_role_change_revokes_target_sessions(accounts):
    repository, actor, _token = accounts
    user_id, _user, token = _regular(repository, actor)
    _reauth(repository, actor)
    repository.update_user(actor, user_id, role="admin")
    assert repository.resolve_session(token.token) is None
    assert repository.authenticate("alice", PASSWORD, "127.0.0.1").role == "admin"


def test_self_demotion_and_deactivation_are_forbidden(accounts):
    repository, actor, _token = accounts
    _reauth(repository, actor)
    for change in ({"role": "user"}, {"is_active": False}):
        with pytest.raises(AccountConflict, match="another administrator"):
            repository.update_user(actor, actor.user_id, **change)


def test_concurrent_admin_changes_preserve_one_active_admin(accounts, session_factory):
    repository, first, _token = accounts
    _reauth(repository, first)
    second_id = repository.create_user(
        first, username="second", password=PASSWORD, role="admin", must_change_password=False
    ).user_id
    second = repository.resolve_session(repository.create_session(second_id).token)
    _reauth(repository, second)
    barrier = Barrier(2)

    def demote(actor, target):
        barrier.wait()
        try:
            repository.update_user(actor, target, role="user")
            return "changed"
        except (AuthenticationError, AccountConflict, AuthorizationError):
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(demote, first, second_id),
            pool.submit(demote, second, first.user_id),
        ]
        assert sorted(future.result() for future in futures) == ["blocked", "changed"]
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(User).where(User.role == "admin", User.is_active)
            )
            == 1
        )


def test_reauthentication_expires_without_sliding_and_is_session_scoped(
    accounts, session_factory, monkeypatch
):
    repository, actor, _token = accounts
    user_id, _user, _user_token = _regular(repository, actor)
    expiry = _reauth(repository, actor)
    other = repository.resolve_session(repository.create_session(actor.user_id).token)
    with pytest.raises(ReauthenticationRequired):
        repository.update_user(other, user_id, is_active=False)
    with session_factory() as session:
        original = session.get(DbSession, actor.session_id).reauthenticated_at
    repository.update_user(actor, user_id, is_active=True)
    with session_factory() as session:
        assert session.get(DbSession, actor.session_id).reauthenticated_at == original
    monkeypatch.setattr(auth_module, "utc_now", lambda: expiry)
    with pytest.raises(ReauthenticationRequired):
        repository.update_user(actor, user_id, is_active=False)


def test_login_failures_and_block_survive_transaction(accounts, session_factory, monkeypatch):
    repository, _actor, _token = accounts
    repository.settings.auth_max_failures = 2
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            repository.authenticate("owner", "incorrect", "127.0.0.1")
    with session_factory() as session:
        attempt = session.scalar(select(AuthAttempt))
        assert attempt.failure_count == 2
        assert attempt.blocked_until is not None
    with pytest.raises(AuthenticationBlocked):
        repository.authenticate("owner", PASSWORD, "127.0.0.1")
    future = utc_now() + timedelta(seconds=repository.settings.auth_block_seconds + 1)
    monkeypatch.setattr(auth_module, "utc_now", lambda: future)
    assert repository.authenticate("owner", PASSWORD, "127.0.0.1").username == "owner"


def test_setup_limit_commits_block_timestamp(accounts, session_factory):
    repository, _actor, _token = accounts
    repository.settings.auth_max_failures = 2
    repository.consume_setup_attempt("127.0.0.1")
    repository.consume_setup_attempt("127.0.0.1")
    with pytest.raises(AuthenticationBlocked):
        repository.consume_setup_attempt("127.0.0.1")
    with session_factory() as session:
        assert session.scalar(select(AuthAttempt)).blocked_until is not None


def test_reauthentication_failures_have_separate_persistent_limit(accounts, session_factory):
    repository, actor, _token = accounts
    repository.settings.auth_max_failures = 2
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            repository.reauthenticate(actor, "incorrect", "127.0.0.1")
    with pytest.raises(AuthenticationBlocked):
        _reauth(repository, actor)
    assert repository.authenticate("owner", PASSWORD, "127.0.0.1").id == actor.user_id
    with session_factory() as session:
        assert session.scalar(select(AuthAttempt)).blocked_until is not None


def test_user_creation_and_reset_quotas_are_independent(accounts):
    repository, actor, _token = accounts
    _reauth(repository, actor)
    for _ in range(20):
        repository._admin_quota(actor, "create")
    with pytest.raises(AuthenticationBlocked):
        repository._admin_quota(actor, "create")
    repository._admin_quota(actor, "reset")


def test_password_reset_during_login_cannot_issue_old_credential_session(
    accounts, session_factory, monkeypatch
):
    repository, actor, _token = accounts
    verify = repository._verify
    fired = False

    def racing_verify(candidate, password):
        nonlocal fired
        result = verify(candidate, password)
        if result and not fired:
            fired = True
            repository.reset_admin("owner", NEW_PASSWORD)
        return result

    monkeypatch.setattr(repository, "_verify", racing_verify)
    with pytest.raises(AuthenticationError):
        repository.authenticate_and_create_session("owner", PASSWORD, "127.0.0.1")
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DbSession)
                .where(DbSession.user_id == actor.user_id, DbSession.revoked_at.is_(None))
            )
            == 0
        )


def test_login_rehash_preserves_identity(accounts, session_factory):
    repository, actor, _token = accounts
    legacy_hash = PasswordHasher(time_cost=1).hash(PASSWORD)
    with session_factory.begin() as session:
        session.get(User, actor.user_id).password_hash = legacy_hash
    result = repository.authenticate_and_create_session("owner", PASSWORD, "127.0.0.1")
    assert result.user_id == actor.user_id
    with session_factory() as session:
        assert session.get(User, actor.user_id).password_hash != legacy_hash


def test_sse_session_check_does_not_touch_idle_activity(accounts, session_factory):
    repository, actor, token = accounts
    old = utc_now() - timedelta(minutes=2)
    with session_factory.begin() as session:
        session.get(DbSession, actor.session_id).last_activity_at = old
    assert repository.resolve_session(token.token, touch=False)
    with session_factory() as session:
        assert (
            session.get(DbSession, actor.session_id).last_activity_at.replace(tzinfo=old.tzinfo)
            == old
        )


def test_cli_recovery_targets_admin_not_oldest_standard_account(accounts, session_factory):
    repository, actor, owner_token = accounts
    user_id, _user, user_token = _regular(repository, actor)
    with session_factory.begin() as session:
        session.get(User, user_id).created_at = utc_now() - timedelta(days=365)
    assert repository.reset_admin(None, NEW_PASSWORD) == actor.user_id
    assert repository.resolve_session(owner_token.token) is None
    assert repository.resolve_session(user_token.token) is not None
    with session_factory() as session:
        assert session.get(User, user_id).username == "alice"
    with pytest.raises(AccountConflict, match="recover"):
        repository.reset_admin("alice", NEW_PASSWORD)
    assert repository.reset_admin("alice", NEW_PASSWORD, recover=True) == user_id
    with pytest.raises(AccountConflict, match="username"):
        repository.reset_admin(None, NEW_PASSWORD)
    with pytest.raises(LookupError):
        repository.reset_admin("no-such-account", NEW_PASSWORD, recover=True)


def test_nonexistent_login_uses_dummy_verification(accounts, monkeypatch):
    repository, _actor, _token = accounts
    candidates = []
    verify = repository._verify

    def observed(candidate, password):
        candidates.append(candidate)
        return verify(candidate, password)

    monkeypatch.setattr(repository, "_verify", observed)
    with pytest.raises(AuthenticationError):
        repository.authenticate("nobody", "incorrect", "127.0.0.1")
    assert candidates == [repository._dummy_hash]
