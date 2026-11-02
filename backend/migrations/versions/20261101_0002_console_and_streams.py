"""Console request history, plan executions and long-connection sessions.

Revision ID: 20261101_0002
Revises: 20261027_0001
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261101_0002"
down_revision: str | Sequence[str] | None = "20261027_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "console_requests",
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("request_headers", sa.JSON(), nullable=False),
        sa.Column("body_mode", sa.String(length=10), nullable=False),
        sa.Column("body_preview", sa.Text(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("elapsed_ms", sa.Float(), nullable=False),
        sa.Column("error_kind", sa.String(length=60), nullable=True),
        sa.Column("error_message", sa.String(length=300), nullable=True),
        sa.Column("response_headers", sa.JSON(), nullable=False),
        sa.Column("response_preview", sa.Text(), nullable=True),
        sa.Column("response_bytes", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_console_requests_project_id", "console_requests", ["project_id"])
    op.create_table(
        "executions",
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("engine", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_kind", sa.String(length=80), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("reserved_requests", sa.Integer(), nullable=False),
        sa.Column("observed_attempts", sa.Integer(), nullable=False),
        sa.Column("unknown_requests", sa.Integer(), nullable=False),
        sa.Column("executions", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_executions_project_id", "executions", ["project_id"])
    op.create_table(
        "stream_sessions",
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("request_headers", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stop_reason", sa.String(length=40), nullable=True),
        sa.Column("error_kind", sa.String(length=60), nullable=True),
        sa.Column("error_message", sa.String(length=300), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(length=200), nullable=True),
        sa.Column("subprotocol", sa.String(length=100), nullable=True),
        sa.Column("close_code", sa.Integer(), nullable=True),
        sa.Column("close_reason", sa.String(length=300), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("truncated_count", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stream_sessions_project_id", "stream_sessions", ["project_id"])
    op.create_index("ix_stream_sessions_status", "stream_sessions", ["status"])
    op.create_table(
        "stream_events",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("event_name", sa.String(length=200), nullable=True),
        sa.Column("data", sa.Text(), nullable=True),
        sa.Column("data_bytes", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["stream_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "sequence"),
    )
    op.create_index("ix_stream_events_session_id", "stream_events", ["session_id"])


def downgrade() -> None:
    for table in ("stream_events", "stream_sessions", "executions", "console_requests"):
        op.drop_table(table)
