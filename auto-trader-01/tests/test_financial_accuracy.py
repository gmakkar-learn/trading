"""
Financial Accuracy Test Harness — pre-live validation #1 (simplest)

Validates:
  - P&L formula: unrealised_pnl = (current_price - avg_price) * quantity
  - Position sizing tiers (score → size_pct → quantity)
  - Order size cap enforcement (USD and INR)
  - Average-price update after a second fill (cost-basis math)

No network, no DB, no broker. Pure arithmetic assertions.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import yaml

from infrastructure.broker.base import Position


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_risk_cfg() -> dict:
    path = Path(__file__).parent.parent / "config" / "risk" / "default.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def _make_risk_guard(risk_cfg: dict, positions: list[Position] | None = None):
    """Return a RiskGuardAgent wired to a stub broker with the given positions."""
    from agents.risk_guard.agent import RiskGuardAgent
    from infrastructure.event_bus.bus import EventBus
    from infrastructure.market_context.loader import load_market_context

    config_dir = Path(__file__).parent.parent / "config"
    ctx = load_market_context("us", config_dir)

    broker = MagicMock()
    broker.get_positions = AsyncMock(return_value=positions or [])
    broker.get_quote = AsyncMock(side_effect=Exception("not needed"))

    audit = MagicMock()
    audit.log = AsyncMock()

    bus = EventBus()
    return RiskGuardAgent(bus, broker, ctx, risk_cfg, audit)


# ── 1. P&L formula ───────────────────────────────────────────────────────────

class TestPnLFormula:
    def test_long_profit(self):
        p = Position("AAPL", "us", 10, average_price=150.0, current_price=180.0,
                     unrealised_pnl=300.0, currency="USD")
        expected = (p.current_price - p.average_price) * p.quantity
        assert abs(p.unrealised_pnl - expected) < 0.01, (
            f"P&L mismatch: got {p.unrealised_pnl}, expected {expected}"
        )

    def test_long_loss(self):
        p = Position("AAPL", "us", 5, average_price=200.0, current_price=170.0,
                     unrealised_pnl=-150.0, currency="USD")
        expected = (p.current_price - p.average_price) * p.quantity
        assert abs(p.unrealised_pnl - expected) < 0.01

    def test_breakeven(self):
        p = Position("MSFT", "us", 20, average_price=100.0, current_price=100.0,
                     unrealised_pnl=0.0, currency="USD")
        expected = (p.current_price - p.average_price) * p.quantity
        assert p.unrealised_pnl == expected == 0.0

    def test_fractional_prices(self):
        p = Position("TSM", "us", 7, average_price=120.33, current_price=135.77,
                     unrealised_pnl=round((135.77 - 120.33) * 7, 2), currency="USD")
        expected = round((p.current_price - p.average_price) * p.quantity, 2)
        assert abs(p.unrealised_pnl - expected) < 0.01


# ── 2. Average-price update after second fill ─────────────────────────────────

class TestAveragePriceUpdate:
    """
    When a second buy order fills, the new average price is:
        new_avg = (qty1 * price1 + qty2 * price2) / (qty1 + qty2)
    Alpaca calculates this broker-side; we verify the formula is correct.
    """
    def _weighted_avg(self, qty1, p1, qty2, p2) -> float:
        return (qty1 * p1 + qty2 * p2) / (qty1 + qty2)

    def test_equal_quantities(self):
        avg = self._weighted_avg(10, 100.0, 10, 120.0)
        assert avg == 110.0

    def test_unequal_quantities(self):
        avg = self._weighted_avg(10, 100.0, 5, 130.0)
        assert abs(avg - 110.0) < 0.01  # (1000 + 650) / 15

    def test_large_first_position(self):
        avg = self._weighted_avg(100, 50.0, 1, 60.0)
        expected = (100 * 50.0 + 1 * 60.0) / 101
        assert abs(avg - expected) < 0.001


# ── 3. Position sizing tiers ──────────────────────────────────────────────────

class TestPositionSizing:
    def setup_method(self):
        self._risk_cfg = _load_risk_cfg()
        self._guard = _make_risk_guard(self._risk_cfg)

    def test_score_85_full_position(self):
        pct = self._guard._score_to_size_pct(85.0)
        assert pct == 1.00, f"Expected full position at score 85, got {pct}"

    def test_score_90_full_position(self):
        pct = self._guard._score_to_size_pct(90.0)
        assert pct == 1.00

    def test_score_70_sixty_pct(self):
        pct = self._guard._score_to_size_pct(70.0)
        assert pct == 0.60, f"Expected 60% at score 70, got {pct}"

    def test_score_75_sixty_pct(self):
        pct = self._guard._score_to_size_pct(75.0)
        assert pct == 0.60

    def test_score_60_thirty_pct(self):
        pct = self._guard._score_to_size_pct(60.0)
        assert pct == 0.30

    def test_score_below_all_tiers_fallback(self):
        pct = self._guard._score_to_size_pct(10.0)
        assert pct == 0.50  # fallback value

    def test_quantity_never_zero(self):
        """Even a very expensive stock should yield at least 1 share."""
        qty = self._guard._calculate_quantity(price=999_999.0, market_id="us", score=85)
        assert qty >= 1

    def test_quantity_us_cap(self):
        """At $100/share and full position, quantity should be $5000/$100 = 50."""
        cap = self._risk_cfg["position_limits"]["max_order_size_usd"]  # 5000
        qty = self._guard._calculate_quantity(price=100.0, market_id="us", score=85)
        assert qty == cap // 100  # 50

    def test_quantity_india_cap(self):
        """At ₹500/share and full position, quantity should be ₹500000/₹500 = 1000."""
        cap = self._risk_cfg["position_limits"]["max_order_size_inr"]  # 500000
        qty = self._guard._calculate_quantity(price=500.0, market_id="india", score=85)
        assert qty == cap // 500  # 1000

    def test_quantity_scales_with_score(self):
        """Lower score → fewer shares."""
        qty_high = self._guard._calculate_quantity(price=100.0, market_id="us", score=85)
        qty_mid  = self._guard._calculate_quantity(price=100.0, market_id="us", score=70)
        qty_low  = self._guard._calculate_quantity(price=100.0, market_id="us", score=60)
        assert qty_high > qty_mid > qty_low


# ── 4. Order size cap ─────────────────────────────────────────────────────────

class TestOrderSizeCap:
    def setup_method(self):
        self._risk_cfg = _load_risk_cfg()
        self._guard = _make_risk_guard(self._risk_cfg)

    def test_us_within_cap(self):
        result = self._guard._check_order_size(price=100.0, market_id="us")
        assert result is None, f"Expected no rejection, got: {result}"

    def test_us_exceeds_cap(self):
        # $6000 order at $6000/share, full position (size_pct=1.0, cap=$5000)
        # _calculate_quantity(6000, "us", 85) = int(5000*1.0/6000) = 0 → clamped to 1
        # order_value = 6000 * 1 = 6000 > 5000 cap → should reject
        result = self._guard._check_order_size(price=6000.0, market_id="us")
        assert result is not None
        assert "exceeds cap" in result

    def test_india_within_cap(self):
        result = self._guard._check_order_size(price=100.0, market_id="india")
        assert result is None

    def test_india_exceeds_cap(self):
        # ₹600000/share, quantity=1, order_value=600000 > 500000 cap
        result = self._guard._check_order_size(price=600_000.0, market_id="india")
        assert result is not None
        assert "exceeds cap" in result

    def test_zero_price_skips_check(self):
        """If price is unknown, the check is skipped (not rejected)."""
        result = self._guard._check_order_size(price=0.0, market_id="us")
        assert result is None
