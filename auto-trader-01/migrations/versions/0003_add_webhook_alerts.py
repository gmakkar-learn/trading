"""Add webhook_alerts table for persistent TradingView alert log.

Replaces the in-memory alert_log list in AppState (capped at 100, lost on restart)
with a durable DB table. The API /log endpoint reads from DB with in-memory fallback.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_alerts",
        sa.Column("alert_id", sa.String(), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("outcome", sa.String(50), nullable=False, index=True),
        sa.Column("ticker", sa.String(20), nullable=True, index=True),
        sa.Column("strategy_id", sa.String(100), nullable=True),
        sa.Column("action", sa.String(10), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("signal_id", sa.String(), nullable=True),
        sa.Column("ip", sa.String(50), nullable=True),
        sa.Column("details", postgresql.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("webhook_alerts")
