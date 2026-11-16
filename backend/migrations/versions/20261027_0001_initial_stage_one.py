"""Initial project, environment and simulation-run schema.

Revision ID: 20261027_0001
Revises:
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261027_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立项目、环境版本、模拟运行及预算事件的初始表和约束；由 Alembic 事务上下文执行。"""
    op.create_table(
        "projects",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "environments",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name"),
    )
    op.create_index("ix_environments_project_id", "environments", ["project_id"])
    op.create_table(
        "environment_versions",
        sa.Column("environment_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=255), nullable=False),
        sa.Column("allowed_methods", sa.JSON(), nullable=False),
        sa.Column("path_templates", sa.JSON(), nullable=False),
        sa.Column("auth_ref", sa.JSON(), nullable=True),
        sa.Column("replay_policy", sa.JSON(), nullable=False),
        sa.Column("sensitive_field_rules", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("environment_id", "version"),
    )
    op.create_index(
        "ix_environment_versions_environment_id", "environment_versions", ["environment_id"]
    )
    op.create_table(
        "run_snapshots",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("run_mode", sa.String(length=30), nullable=False),
        sa.Column("generator_version", sa.String(length=80), nullable=False),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("resource_limits", sa.JSON(), nullable=False),
        sa.Column("environment_version_id", sa.String(length=36), nullable=True),
        sa.Column("specification_id", sa.String(length=36), nullable=True),
        sa.Column("rule_set_id", sa.String(length=36), nullable=True),
        sa.Column("chain_template_id", sa.String(length=36), nullable=True),
        sa.Column("random_seed", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "runs",
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("run_mode", sa.String(length=30), nullable=False),
        sa.Column("configured_llm_mode", sa.String(length=20), nullable=False),
        sa.Column("actual_llm_status", sa.String(length=20), nullable=False),
        sa.Column("degradation_reason", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stop_reason", sa.String(length=40), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_status", sa.String(length=20), nullable=False),
        sa.Column("workflow_error", sa.String(length=500), nullable=True),
        sa.Column("current_action", sa.Integer(), nullable=False),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["run_snapshots.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_table(
        "run_events",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("node", sa.String(length=40), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("tool_name", sa.String(length=80), nullable=True),
        sa.Column("input_summary", sa.JSON(), nullable=False),
        sa.Column("observation_summary", sa.JSON(), nullable=False),
        sa.Column("decision_reason", sa.String(length=500), nullable=True),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("budget_summary", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_table(
        "budget_ledger",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("resource", sa.String(length=30), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("related_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_budget_ledger_run_id", "budget_ledger", ["run_id"])
    op.create_table(
        "dispatch_attempts",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_kind", sa.String(length=80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence"),
    )
    op.create_index("ix_dispatch_attempts_run_id", "dispatch_attempts", ["run_id"])


def downgrade() -> None:
    """按外键依赖逆序删除首版表；这会丢失相应业务数据，不是数据恢复操作。"""
    for table in (
        "dispatch_attempts",
        "budget_ledger",
        "run_events",
        "runs",
        "run_snapshots",
        "environment_versions",
        "environments",
        "projects",
    ):
        op.drop_table(table)
