"""Rebuild audit_log to match Phase 2 model schema.

The Phase 1 migration created a different audit_log schema (event_type/actor/payload).
This migration drops it and recreates with the columns the AuditLogger actually uses:
market_id, ticker, decision, reason, signal_json, order_id.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("audit_log")
    op.create_table(
        "audit_log",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("market_id", sa.String(20), nullable=True, index=True),
        sa.Column("ticker", sa.String(20), nullable=True, index=True),
        sa.Column("decision", sa.String(50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("signal_json", postgresql.JSON(), nullable=True),
        sa.Column("order_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            index=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("ticker", sa.String(length=16), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
