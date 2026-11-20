"""Keep scrubbed HTTP bodies and WebSocket messages for replay."""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0006"
down_revision = "20261105_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Leave old rows readable through the existing preview."""
    op.add_column("console_requests", sa.Column("request_body", sa.Text(), nullable=True))
    op.add_column("stream_sessions", sa.Column("scheduled_messages", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Remove the two replay columns."""
    with op.batch_alter_table("stream_sessions") as table:
        table.drop_column("scheduled_messages")
    with op.batch_alter_table("console_requests") as table:
        table.drop_column("request_body")
