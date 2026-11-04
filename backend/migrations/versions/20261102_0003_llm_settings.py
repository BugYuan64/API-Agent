"""Add llm_settings table for dynamic frontend console configuration.

Revision ID: 20261102_0003
Revises: 20261101_0002
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261102_0003"
down_revision: str | Sequence[str] | None = "20261101_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_settings",
        sa.Column("provider", sa.String(length=40), nullable=False, server_default="openai"),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("api_key", sa.String(length=500), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=False, server_default="gpt-4o"),
        sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="60.0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("llm_settings")
