"""Visible history, scan diagnostics and independently accounted model budgets.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence
from pathlib import PurePosixPath

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    definitions = {
        "download_jobs": ("dismissed_at DATETIME",),
        "tracks": (
            "parser_version INTEGER NOT NULL DEFAULT 0",
            "file_extension VARCHAR(16)",
            "container VARCHAR(64)",
        ),
        "scan_runs": (
            "parser_version INTEGER NOT NULL DEFAULT 0",
            "service_task_id VARCHAR(36) REFERENCES service_tasks(id)",
            "lease_token VARCHAR(64)",
            "lease_expires_at DATETIME",
            "traversal_complete BOOLEAN",
            "summary_json TEXT NOT NULL DEFAULT '{}'",
        ),
        "requests": (
            "orchestration_attempt_id VARCHAR(36)",
            "model_rounds_used INTEGER",
            "configured_model_rounds INTEGER",
            "configured_tool_calls INTEGER",
            "configured_agent_seconds INTEGER",
            "termination_reason VARCHAR(40)",
        ),
        "openai_calls": (
            "orchestration_attempt_id VARCHAR(36)",
            "model_round INTEGER",
            "phase VARCHAR(40)",
            "configured_model_rounds INTEGER",
            "configured_tool_calls INTEGER",
            "configured_agent_seconds INTEGER",
            "termination_reason VARCHAR(40)",
            "usage_reported BOOLEAN",
        ),
    }
    for table, columns in definitions.items():
        for definition in columns:
            op.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {definition}"))
    connection = op.get_bind()
    last = ""
    while rows := connection.execute(
        sa.text("SELECT id,filepath FROM tracks WHERE id>:last ORDER BY id LIMIT 500"),
        {"last": last},
    ).all():
        for identifier, path in rows:
            extension = PurePosixPath(path).suffix.casefold()[:16] or None
            connection.execute(
                sa.text("UPDATE tracks SET file_extension=:extension WHERE id=:id"),
                {"id": identifier, "extension": extension},
            )
        last = rows[-1][0]
    # A completed response alone cannot prove a real zero-token report: legacy
    # adapters used zero placeholders for missing provider usage.
    op.execute(
        "UPDATE openai_calls SET usage_reported=1 "
        "WHERE total_tokens>0 OR input_tokens>0 OR output_tokens>0"
    )
    # Deployment stopped both services; a legacy running scan cannot own a new lease.
    op.execute(
        "UPDATE scan_runs SET status='failed',error_message='interrupted by schema upgrade' "
        "WHERE status='running'"
    )
    op.create_index(
        "ix_download_jobs_history", "download_jobs", ["dismissed_at", "status", "created_at", "id"]
    )
    op.create_index(
        "uq_scan_runs_running",
        "scan_runs",
        ["status"],
        unique=True,
        sqlite_where=sa.text("status='running'"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Restore the paired pre-update database and release backup; "
        "do not discard history or execution diagnostics"
    )
