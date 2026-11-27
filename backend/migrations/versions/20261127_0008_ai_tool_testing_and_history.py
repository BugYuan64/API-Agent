"""Add AI multi-round testing history, model test runs, and assistant thread tables.

Revision ID: 20261127_0008
Revises: 20261120_0007
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261127_0008"
down_revision: str | Sequence[str] | None = "20261120_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展控制台与长连接历史表，并新增子用例、模型测试任务及助手会话表。"""
    with op.batch_alter_table("console_requests") as table:
        table.add_column(
            sa.Column("source", sa.String(length=20), nullable=False, server_default="manual")
        )
        table.add_column(sa.Column("title", sa.String(length=200), nullable=True))
        table.add_column(sa.Column("badge", sa.String(length=40), nullable=True))
        table.add_column(
            sa.Column("status", sa.String(length=32), nullable=False, server_default="completed")
        )
        table.add_column(sa.Column("summary", sa.Text(), nullable=True))
        table.add_column(
            sa.Column("total_rounds", sa.Integer(), nullable=False, server_default="1")
        )
        table.add_column(
            sa.Column("cooldown_ms", sa.Integer(), nullable=False, server_default="500")
        )
        table.add_column(sa.Column("http_version", sa.String(length=20), nullable=True))
        table.add_column(sa.Column("response_reason", sa.String(length=120), nullable=True))
        table.add_column(sa.Column("redirects_json", sa.JSON(), nullable=True))
        table.add_column(sa.Column("sse_json", sa.JSON(), nullable=True))

    op.create_table(
        "console_request_cases",
        sa.Column("parent_id", sa.String(length=36), nullable=False),
        sa.Column("round_index", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("case_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("label", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("request_headers", sa.JSON(), nullable=False),
        sa.Column("body_mode", sa.String(length=20), nullable=False, server_default="none"),
        sa.Column("request_body", sa.Text(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("error_kind", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("response_headers", sa.JSON(), nullable=True),
        sa.Column("response_preview", sa.Text(), nullable=True),
        sa.Column("response_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("http_version", sa.String(length=20), nullable=True),
        sa.Column("response_reason", sa.String(length=120), nullable=True),
        sa.Column("redirects_json", sa.JSON(), nullable=True),
        sa.Column("sse_json", sa.JSON(), nullable=True),
        sa.Column("assertion_summary", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["console_requests.id"],
            name="fk_console_request_cases_parent_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_console_request_cases_parent_id", "console_request_cases", ["parent_id"])

    with op.batch_alter_table("stream_sessions") as table:
        table.add_column(
            sa.Column("source", sa.String(length=20), nullable=False, server_default="manual")
        )
        table.add_column(sa.Column("title", sa.String(length=200), nullable=True))
        table.add_column(sa.Column("badge", sa.String(length=40), nullable=True))
        table.add_column(sa.Column("parent_session_id", sa.String(length=36), nullable=True))
        table.add_column(sa.Column("round_index", sa.Integer(), nullable=False, server_default="1"))
        table.add_column(sa.Column("case_index", sa.Integer(), nullable=False, server_default="0"))
        table.add_column(sa.Column("case_label", sa.String(length=200), nullable=True))
        table.add_column(
            sa.Column("cooldown_ms", sa.Integer(), nullable=False, server_default="500")
        )
        table.add_column(
            sa.Column("total_rounds", sa.Integer(), nullable=False, server_default="1")
        )
        table.add_column(sa.Column("summary", sa.Text(), nullable=True))
        table.create_foreign_key(
            "fk_stream_sessions_parent_session_id",
            "stream_sessions",
            ["parent_session_id"],
            ["id"],
            ondelete="CASCADE",
        )
        table.create_index("ix_stream_sessions_parent_session_id", ["parent_session_id"])

    op.create_table(
        "model_test_runs",
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("badge", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("base_url", sa.String(length=2000), nullable=False),
        sa.Column("protocol", sa.String(length=20), nullable=False, server_default="auto"),
        sa.Column("resolved_protocol", sa.String(length=20), nullable=True),
        sa.Column("used_saved_key", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_custom_api_key", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="20.0"),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("target_models", sa.JSON(), nullable=False),
        sa.Column("cooldown_ms", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("total_rounds", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("completed_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discover_meta", sa.JSON(), nullable=True),
        sa.Column("models_snapshot", sa.JSON(), nullable=False),
        sa.Column("rounds_json", sa.JSON(), nullable=False),
        sa.Column("sub_cases_json", sa.JSON(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "assistant_threads",
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("history_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "history_id", name="uq_assistant_thread_scope_history"),
    )
    op.create_index("ix_assistant_threads_scope", "assistant_threads", ["scope"])
    op.create_index("ix_assistant_threads_history_id", "assistant_threads", ["history_id"])

    op.create_table(
        "assistant_messages",
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("blocks_json", sa.JSON(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["assistant_threads.id"],
            name="fk_assistant_messages_thread_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_messages_thread_id", "assistant_messages", ["thread_id"])


def downgrade() -> None:
    """回滚本版新增表及新增列；使用 SQLite batch_alter_table 兼容安全回退。"""
    op.drop_index("ix_assistant_messages_thread_id", table_name="assistant_messages")
    op.drop_table("assistant_messages")

    op.drop_index("ix_assistant_threads_history_id", table_name="assistant_threads")
    op.drop_index("ix_assistant_threads_scope", table_name="assistant_threads")
    op.drop_table("assistant_threads")

    op.drop_table("model_test_runs")

    with op.batch_alter_table("stream_sessions") as table:
        table.drop_index("ix_stream_sessions_parent_session_id")
        table.drop_constraint("fk_stream_sessions_parent_session_id", type_="foreignkey")
        for column in (
            "summary",
            "total_rounds",
            "cooldown_ms",
            "case_label",
            "case_index",
            "round_index",
            "parent_session_id",
            "badge",
            "title",
            "source",
        ):
            table.drop_column(column)

    op.drop_index("ix_console_request_cases_parent_id", table_name="console_request_cases")
    op.drop_table("console_request_cases")

    with op.batch_alter_table("console_requests") as table:
        for column in (
            "sse_json",
            "redirects_json",
            "response_reason",
            "http_version",
            "cooldown_ms",
            "total_rounds",
            "summary",
            "status",
            "badge",
            "title",
            "source",
        ):
            table.drop_column(column)
