"""Drop the simulation-run tables.

Revision ID: 20261105_0005
Revises: 20261102_0004
Create Date: 2026-11-05

The simulation run module (`runs/`) was removed: its actions were local
``asyncio.sleep`` calls that never contacted a target, so it produced no
testing evidence while still owning five tables and a background worker.
Real API testing goes through `/api/executions`; AI orchestration through
`/api/agent/runs` (JSON artifacts, no SQL tables).

`projects`, `environments` and `environment_versions` are kept, so the
`project_id` foreign keys on `executions`, `console_requests` and
`stream_sessions` remain valid and are left untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261105_0005"
down_revision: str | Sequence[str] | None = "20261102_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Reverse dependency order: children before parents. `runs` references
# `run_snapshots` with RESTRICT, so `runs` must go first.
TABLES = ("dispatch_attempts", "budget_ledger", "run_events", "runs", "run_snapshots")


def upgrade() -> None:
    """Drop the five simulation-run tables; existing simulation history is lost."""
    for table in TABLES:
        op.drop_table(table)


def downgrade() -> None:
    """Rebuild the simulation-run schema exactly as `20261027_0001` created it.

    Structure only: dropped rows cannot be restored, so a downgrade yields
    empty tables. Indexes and unique constraints are recreated so the
    schema matches the original revision and a later upgrade stays clean.
    """
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
