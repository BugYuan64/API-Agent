"""Freeze the executed plan on execution records.

Revision ID: 20261102_0004
Revises: 20261102_0003
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261102_0004"
down_revision: str | Sequence[str] | None = "20261102_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable plan columns; existing rows stay NULL and are not diagnosable."""
    op.add_column("executions", sa.Column("plan", sa.JSON(), nullable=True))
    op.add_column("executions", sa.Column("plan_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """移除执行计划冻结列，回退后失去按历史计划诊断的溯源能力。"""
    op.drop_column("executions", "plan_sha256")
    op.drop_column("executions", "plan")
