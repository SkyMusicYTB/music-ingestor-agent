"""Explicit accounts and private activity ownership, preserving all legacy rows.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Native additive DDL avoids dropping tables referenced by preserved history.
    definitions = {
        "users": (
            "role VARCHAR(16) NOT NULL DEFAULT 'user' "
            "CONSTRAINT ck_users_role CHECK (role IN ('admin','user'))",
            "must_change_password BOOLEAN NOT NULL DEFAULT 0",
            "password_changed_at DATETIME",
            "disabled_at DATETIME",
            "created_by_user_id VARCHAR(36) REFERENCES users(id)",
            "updated_at DATETIME",
        ),
        "sessions": ("reauthenticated_at DATETIME",),
        "openai_calls": ("owner_user_id VARCHAR(36) REFERENCES users(id)",),
        "events": (
            "user_id VARCHAR(36) REFERENCES users(id)",
            "audience VARCHAR(24) NOT NULL DEFAULT 'admin' "
            "CONSTRAINT ck_events_audience "
            "CHECK (audience IN ('user','all_authenticated','admin')) "
            "CONSTRAINT ck_events_owner CHECK ((audience = 'user' AND user_id IS NOT NULL) "
            "OR (audience != 'user' AND user_id IS NULL))",
        ),
    }
    for table, columns in definitions.items():
        for definition in columns:
            op.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {definition}"))
    # Legacy rows recorded the active flag, not when it changed. Leave the
    # unknown disable date NULL rather than inventing a timestamp from creation.
    op.execute("UPDATE users SET updated_at=created_at")
    op.execute(
        "UPDATE users SET role='admin' "
        "WHERE id=(SELECT id FROM users ORDER BY created_at,id LIMIT 1)"
    )
    op.execute(
        "UPDATE openai_calls SET owner_user_id=(SELECT user_id FROM requests "
        "WHERE requests.id=openai_calls.request_id)"
    )
    op.execute("""UPDATE openai_calls SET owner_user_id=(
        SELECT MIN(r.user_id) FROM job_decisions d
        JOIN download_jobs j ON j.id=d.job_id
        JOIN request_tracks t ON t.id=j.request_track_id
        JOIN requests r ON r.id=t.request_id
        WHERE d.openai_call_id=openai_calls.id HAVING COUNT(DISTINCT r.user_id)=1
    ) WHERE owner_user_id IS NULL""")
    # Only relationally attributable events become private user events. Unknown
    # and old library payloads stay admin-only; do not trust their JSON contents.
    op.execute("""UPDATE events SET audience='user',user_id=(
        SELECT user_id FROM requests WHERE requests.id=events.entity_id
    ) WHERE entity_type='request' AND EXISTS (
        SELECT 1 FROM requests WHERE requests.id=events.entity_id)""")
    op.execute("""UPDATE events SET audience='user',user_id=(
        SELECT r.user_id FROM download_jobs j JOIN request_tracks t ON t.id=j.request_track_id
        JOIN requests r ON r.id=t.request_id WHERE j.id=events.entity_id
    ) WHERE entity_type='job' AND EXISTS (SELECT 1 FROM download_jobs WHERE id=events.entity_id)""")
    op.create_index("ix_users_role_active", "users", ["role", "is_active"])
    op.create_index(
        "ix_openai_calls_owner_created", "openai_calls", ["owner_user_id", "created_at"]
    )
    op.create_index("ix_events_audience_owner_id", "events", ["audience", "user_id", "id"])


def downgrade() -> None:
    raise RuntimeError(
        "Account isolation cannot be downgraded safely; "
        "restore the paired pre-update database and release backup"
    )
