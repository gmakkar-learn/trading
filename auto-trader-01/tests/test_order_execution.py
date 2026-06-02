"""
Order Execution Test Harness — pre-live validation #4

Unit tests for TraderAgent behaviour:
  1. Auto-execute path: score >= auto_threshold → place_order called immediately
  2. Approval path: score < auto_threshold → Telegram request sent, not placed yet
  3. India orders use CNC product type; US orders use DAY
  4. Audit log written BEFORE and AFTER every order attempt
  5. Broker rejection (status != ACCEPTED) → SignalRejectedEvent published
  6. Filled order → OrderPlacedEvent published with correct trade record
  7. Approval timeout → SignalRejectedEvent with reason approval_rejected_or_timed_out

No network, no DB. All broker and Telegram calls are mocked.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from infrastructure.event_bus.bus import EventBus
from infrastructure.event_bus.events import (
    OrderPlacedEvent, OrderProposal, OrderProposalEvent, SignalRejectedEvent,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_proposal(
    ticker="AAPL",
    market_id="us",
    score=85.0,
    quantity=10,
    limit_price=150.0,
) -> OrderProposal:
    return OrderProposal(
        signal_id="sig-001",
        ticker=ticker,
        market_id=market_id,
        side="BUY",
        quantity=quantity,
        order_type="LIMIT",
        limit_price=limit_price,
        stoploss=142.5,
        target=165.0,
        composite_score=score,
        rationale="test rationale",
    )


def _make_order_result(status="ACCEPTED", broker_id="broker-001", fill_price=150.0):
    from infrastructure.event_bus.events import OrderResult
    return OrderResult(
        order_id="ord-001",
        broker_order_id=broker_id,
        status=status,
        fill_price=fill_price,
        message="",
    )


def _make_trader(bus: EventBus, broker, system_cfg: dict | None = None, telegram=None):
    from agents.trader.agent import TraderAgent
    from infrastructure.market_context.loader import load_market_context

    config_dir = Path(__file__).parent.parent / "config"
    ctx = load_market_context("us", config_dir)

    audit = MagicMock()
    audit.log = AsyncMock()

    cfg = system_cfg or {"trader": {"auto_threshold": 80}, "alerts": {"approval_timeout_min": 1}}
    return TraderAgent(bus, broker, ctx, audit, cfg, telegram), audit


# ── 1. Auto-execute path ──────────────────────────────────────────────────────

class TestAutoExecute:
    @pytest.mark.asyncio
    async def test_high_score_places_order_immediately(self):
        bus = EventBus()
        broker = MagicMock()
        broker.place_order = AsyncMock(return_value=_make_order_result())
        trader, _ = _make_trader(bus, broker)

        placed_events = []
        bus.subscribe(OrderPlacedEvent, lambda e: placed_events.append(e))

        proposal = _make_proposal(score=85.0)
        await bus.publish(OrderProposalEvent(proposal=proposal))
        await asyncio.sleep(0.1)  # let background task run

        broker.place_order.assert_called_once()
        assert len(placed_events) == 1
        assert placed_events[0].trade.ticker == "AAPL"

    @pytest.mark.asyncio
    async def test_score_at_threshold_auto_executes(self):
        bus = EventBus()
        broker = MagicMock()
        broker.place_order = AsyncMock(return_value=_make_order_result())
        trader, _ = _make_trader(bus, broker, {"trader": {"auto_threshold": 80}, "alerts": {"approval_timeout_min": 1}})

        proposal = _make_proposal(score=80.0)
        await bus.publish(OrderProposalEvent(proposal=proposal))
        await asyncio.sleep(0.1)

        broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_score_just_below_threshold_does_not_auto_execute(self):
        bus = EventBus()
        broker = MagicMock()
        broker.place_order = AsyncMock(return_value=_make_order_result())
        telegram = MagicMock()
        telegram.send_approval_request = AsyncMock()
        trader, _ = _make_trader(bus, broker, {"trader": {"auto_threshold": 80}, "alerts": {"approval_timeout_min": 1}}, telegram)

        proposal = _make_proposal(score=79.9)
        await bus.publish(OrderProposalEvent(proposal=proposal))
        await asyncio.sleep(0.1)

        broker.place_order.assert_not_called()
        telegram.send_approval_request.assert_called_once()


# ── 2. Product type routing ───────────────────────────────────────────────────

class TestProductType:
    @pytest.mark.asyncio
    async def test_us_order_uses_day_product(self):
        bus = EventBus()
        broker = MagicMock()
        captured_orders = []

        async def capture(order):
            captured_orders.append(order)
            return _make_order_result()

        broker.place_order = capture
        trader, _ = _make_trader(bus, broker)

        await bus.publish(OrderProposalEvent(proposal=_make_proposal(market_id="us", score=85.0)))
        await asyncio.sleep(0.1)

        assert len(captured_orders) == 1
        assert captured_orders[0].product_type == "DAY"

    @pytest.mark.asyncio
    async def test_india_order_uses_cnc_product(self):
        """India orders must always be CNC (delivery), never MIS."""
        from agents.trader.agent import TraderAgent
        from infrastructure.market_context.loader import load_market_context

        config_dir = Path(__file__).parent.parent / "config"
        india_ctx = load_market_context("india", config_dir)

        bus = EventBus()
        broker = MagicMock()
        captured_orders = []

        async def capture(order):
            captured_orders.append(order)
            return _make_order_result()

        broker.place_order = capture

        audit = MagicMock()
        audit.log = AsyncMock()
        cfg = {"trader": {"auto_threshold": 80}, "alerts": {"approval_timeout_min": 1}}
        TraderAgent(bus, broker, india_ctx, audit, cfg)

        proposal = _make_proposal(market_id="india", score=85.0)
        await bus.publish(OrderProposalEvent(proposal=proposal))
        await asyncio.sleep(0.1)

        assert len(captured_orders) == 1
        assert captured_orders[0].product_type == "CNC", (
            f"India order must use CNC, got {captured_orders[0].product_type}"
        )


# ── 3. Audit log discipline ───────────────────────────────────────────────────

class TestAuditLog:
    @pytest.mark.asyncio
    async def test_audit_written_before_and_after_order(self):
        bus = EventBus()
        broker = MagicMock()
        broker.place_order = AsyncMock(return_value=_make_order_result())
        trader, audit = _make_trader(bus, broker)

        await bus.publish(OrderProposalEvent(proposal=_make_proposal(score=85.0)))
        await asyncio.sleep(0.1)

        decisions = [c.kwargs.get("decision") or c.args[0]
                     for c in audit.log.call_args_list
                     if audit.log.call_args_list]
        # Extract decision kwarg from all audit.log calls
        all_decisions = [c.kwargs["decision"] for c in audit.log.call_args_list]
        assert "ORDER_ATTEMPT" in all_decisions, "Audit must record ORDER_ATTEMPT before placing"
        assert "ORDER_RESULT" in all_decisions, "Audit must record ORDER_RESULT after placing"

        # ORDER_ATTEMPT must come before ORDER_RESULT
        attempt_idx = all_decisions.index("ORDER_ATTEMPT")
        result_idx = all_decisions.index("ORDER_RESULT")
        assert attempt_idx < result_idx

    @pytest.mark.asyncio
    async def test_audit_written_even_on_broker_rejection(self):
        bus = EventBus()
        broker = MagicMock()
        broker.place_order = AsyncMock(return_value=_make_order_result(status="REJECTED"))
        trader, audit = _make_trader(bus, broker)

        await bus.publish(OrderProposalEvent(proposal=_make_proposal(score=85.0)))
        await asyncio.sleep(0.1)

        all_decisions = [c.kwargs["decision"] for c in audit.log.call_args_list]
        assert "ORDER_ATTEMPT" in all_decisions
        assert "ORDER_RESULT" in all_decisions


# ── 4. Broker rejection propagates ───────────────────────────────────────────

class TestBrokerRejection:
    @pytest.mark.asyncio
    async def test_broker_rejected_publishes_signal_rejected_event(self):
        bus = EventBus()
        broker = MagicMock()
        broker.place_order = AsyncMock(return_value=_make_order_result(status="REJECTED", broker_id="br-x"))
        trader, _ = _make_trader(bus, broker)

        rejected = []
        bus.subscribe(SignalRejectedEvent, lambda e: rejected.append(e))

        await bus.publish(OrderProposalEvent(proposal=_make_proposal(score=85.0)))
        await asyncio.sleep(0.1)

        assert len(rejected) == 1
        assert "broker_rejected" in rejected[0].reason

    @pytest.mark.asyncio
    async def test_successful_order_publishes_order_placed_event(self):
        bus = EventBus()
        broker = MagicMock()
        broker.place_order = AsyncMock(return_value=_make_order_result(status="ACCEPTED", fill_price=151.5))
        trader, _ = _make_trader(bus, broker)

        placed = []
        bus.subscribe(OrderPlacedEvent, lambda e: placed.append(e))

        await bus.publish(OrderProposalEvent(proposal=_make_proposal(score=85.0)))
        await asyncio.sleep(0.1)

        assert len(placed) == 1
        assert placed[0].trade.fill_price == 151.5
        assert placed[0].trade.broker_order_id == "broker-001"


# ── 5. Approval timeout ───────────────────────────────────────────────────────

class TestApprovalTimeout:
    @pytest.mark.asyncio
    async def test_timeout_publishes_rejection(self):
        bus = EventBus()
        broker = MagicMock()
        broker.place_order = AsyncMock(return_value=_make_order_result())
        telegram = MagicMock()
        telegram.send_approval_request = AsyncMock()

        # Set timeout to 0.1 seconds for fast test
        cfg = {"trader": {"auto_threshold": 80}, "alerts": {"approval_timeout_min": 0.002}}
        trader, _ = _make_trader(bus, broker, cfg, telegram)

        rejected = []
        bus.subscribe(SignalRejectedEvent, lambda e: rejected.append(e))

        proposal = _make_proposal(score=60.0)  # below threshold → approval required
        await bus.publish(OrderProposalEvent(proposal=proposal))
        await asyncio.sleep(0.5)  # wait for timeout to fire

        broker.place_order.assert_not_called()
        assert len(rejected) == 1
        assert "approval_rejected_or_timed_out" in rejected[0].reason
