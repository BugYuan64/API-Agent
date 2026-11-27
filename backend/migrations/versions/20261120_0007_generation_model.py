"""Allow document and test generation to override the assistant model."""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0007"
down_revision = "20261119_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep all existing configurations in follow mode."""
    op.add_column("llm_settings", sa.Column("generation_model", sa.String(120), nullable=True))


def downgrade() -> None:
    """Remove the override while retaining the shared assistant model."""
    with op.batch_alter_table("llm_settings") as table:
        table.drop_column("generation_model")
