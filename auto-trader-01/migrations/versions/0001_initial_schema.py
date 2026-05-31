"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-31

"""
from __future__ import annotations
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "announcements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filing_id", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("market_id", sa.String(length=16), nullable=False),
        sa.Column("filing_type", sa.String(length=32), nullable=True),
        sa.Column("filing_url", sa.Text(), nullable=True),
        sa.Column("document_url", sa.Text(), nullable=True),
        sa.Column("items", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_text_excerpt", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("filing_id"),
    )
    op.create_index("ix_announcements_ticker", "announcements", ["ticker"])
    op.create_index("ix_announcements_published_at", "announcements", ["published_at"])

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("market_id", sa.String(length=16), nullable=False),
        sa.Column("strategy_type", sa.String(length=32), nullable=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=True),
        sa.Column("strategy_version", sa.String(length=16), nullable=True),
        sa.Column("composite_score", sa.Float(), nullable=True),
        sa.Column("recommended_action", sa.String(length=8), nullable=True),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("context_json", postgresql.JSONB(), nullable=True),
        sa.Column("filing_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("signal_id"),
    )
    op.create_index("ix_signals_ticker", "signals", ["ticker"])
    op.create_index("ix_signals_created_at", "signals", ["created_at"])

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("market_id", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("broker_order_id", sa.String(length=128), nullable=True),
        sa.Column("broker_id", sa.String(length=32), nullable=True),
        sa.Column("product_type", sa.String(length=16), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index("ix_orders_ticker", "orders", ["ticker"])

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("market_id", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("average_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("current_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("unrealised_pnl", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("last_updated", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_positions_ticker", "positions", ["ticker"], unique=True)

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
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_event_type", "audit_log", ["event_type"])

    op.create_table(
        "tax_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("market_id", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("cost_basis", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("realised_pnl", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("holding_days", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "watchlist_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("market_id", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True, default=True),
        sa.Column("max_position_pct", sa.Float(), nullable=True),
        sa.Column("excluded", sa.Boolean(), nullable=True, default=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_watchlist_entries_ticker_market", "watchlist_entries", ["ticker", "market_id"], unique=True)


def downgrade() -> None:
    op.drop_table("watchlist_entries")
    op.drop_table("tax_events")
    op.drop_table("audit_log")
    op.drop_table("positions")
    op.drop_table("orders")
    op.drop_table("signals")
    op.drop_table("announcements")
