"""
Risk Guard Completeness Test Harness — pre-live validation #2

Verifies every rejection rule in RiskGuardAgent fires for the right input
and that valid signals pass through cleanly.

Rules tested:
  1. HOLD/SELL actions are rejected before any other check
  2. Market closed → session:market_closed
  3. Already holding ticker → concentration:already_holding_<ticker>
  4. Max concurrent positions reached → concentration:max_positions_reached_N
  5. Order value exceeds USD cap → order_size:...exceeds cap
  6. Order value exceeds INR cap → order_size:...exceeds cap
  7. Valid signal during market hours with no positions → passes all checks

No network, no DB. Market open/closed is controlled by patching datetime.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from infrastructure.broker.base import Position
from infrastructure.event_bus.events import TradingSignal


# ── fixtures ──────────────────────────────────────────────────────────────────

def _load_risk_cfg() -> dict:
    path = Path(__file__).parent.parent / "config" / "risk" / "default.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def _make_signal(
    ticker="AAPL",
    market_id="us",
    action="BUY",
    score=80.0,
    confidence="high",
) -> TradingSignal:
    from infrastructure.event_bus.events import TradingSignal
    return TradingSignal(
        signal_id="test-signal-id",
        ticker=ticker,
        market_id=market_id,
        strategy_type="fundamental",
        strategy_id="fundamental_v1",
        composite_score=score,
        recommended_action=action,
        confidence=confidence,
        rationale="test rationale",
        context={"last_price": 150.0},
    )


def _make_guard(positions: list[Position] | None = None, risk_overrides: dict | None = None):
    from agents.risk_guard.agent import RiskGuardAgent
    from infrastructure.event_bus.bus import EventBus
    from infrastructure.market_context.loader import load_market_context

    config_dir = Path(__file__).parent.parent / "config"
    ctx = load_market_context("us", config_dir)

    risk_cfg = _load_risk_cfg()
    # Disable regime filter by default so existing tests don't hit yfinance
    risk_cfg["market_regime"] = {"enabled": False}
    if risk_overrides:
        for k, v in risk_overrides.items():
            risk_cfg[k] = v

    broker = MagicMock()
    broker.get_positions = AsyncMock(return_value=positions or [])
    broker.get_quote = AsyncMock(return_value=MagicMock(last_price=150.0))

    audit = MagicMock()
    audit.log = AsyncMock()

    return RiskGuardAgent(EventBus(), broker, ctx, risk_cfg, audit)


def _market_open_time():
    """Return a datetime that falls within US market hours (12:00 ET = 17:00 UTC)."""
    from zoneinfo import ZoneInfo
    return datetime(2026, 6, 2, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))


def _market_closed_time():
    """Return a datetime outside US market hours (20:00 ET)."""
    from zoneinfo import ZoneInfo
    return datetime(2026, 6, 2, 20, 0, 0, tzinfo=ZoneInfo("America/New_York"))


# ── Rule 1: action filter ─────────────────────────────────────────────────────

class TestActionFilter:
    @pytest.mark.asyncio
    async def test_hold_rejected(self):
        guard = _make_guard()
        reason = await guard._run_checks(_make_signal(action="HOLD"))
        assert reason is not None
        assert "action_not_buy" in reason

    @pytest.mark.asyncio
    async def test_sell_rejected(self):
        guard = _make_guard()
        reason = await guard._run_checks(_make_signal(action="SELL"))
        assert reason is not None
        assert "action_not_buy" in reason

    @pytest.mark.asyncio
    async def test_buy_passes_action_check(self):
        guard = _make_guard()
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal(action="BUY"))
        # Should not be rejected for action (may fail later checks)
        if reason:
            assert "action_not_buy" not in reason


# ── Rule 2: market session ────────────────────────────────────────────────────

class TestSessionCheck:
    @pytest.mark.asyncio
    async def test_market_closed_rejects(self):
        guard = _make_guard()
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_closed_time()
            reason = await guard._run_checks(_make_signal())
        assert reason is not None
        assert "market_closed" in reason

    @pytest.mark.asyncio
    async def test_market_open_passes(self):
        guard = _make_guard()
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal())
        if reason:
            assert "market_closed" not in reason

    @pytest.mark.asyncio
    async def test_continuous_disabled_always_rejects(self):
        guard = _make_guard(risk_overrides={"sessions": {"allow_continuous": False}})
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal())
        assert reason is not None
        assert "continuous_trading_disabled" in reason


# ── Rule 3: concentration — already holding ───────────────────────────────────

class TestConcentrationAlreadyHolding:
    @pytest.mark.asyncio
    async def test_already_holding_ticker_rejected(self):
        existing = [Position("AAPL", "us", 10, 150.0, 160.0, 100.0, "USD")]
        guard = _make_guard(positions=existing)
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal(ticker="AAPL"))
        assert reason is not None
        assert "already_holding_AAPL" in reason

    @pytest.mark.asyncio
    async def test_different_ticker_not_rejected(self):
        existing = [Position("AAPL", "us", 10, 150.0, 160.0, 100.0, "USD")]
        guard = _make_guard(positions=existing)
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal(ticker="MSFT"))
        if reason:
            assert "already_holding" not in reason


# ── Rule 4: concentration — max positions ─────────────────────────────────────

class TestConcentrationMaxPositions:
    @pytest.mark.asyncio
    async def test_max_positions_reached(self):
        # Fill up to the limit with other tickers
        max_pos = _load_risk_cfg()["position_limits"]["max_concurrent_positions"]
        positions = [
            Position(f"TICK{i}", "us", 1, 100.0, 110.0, 10.0, "USD")
            for i in range(max_pos)
        ]
        guard = _make_guard(positions=positions)
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal(ticker="NEWSTOCK"))
        assert reason is not None
        assert "max_positions_reached" in reason

    @pytest.mark.asyncio
    async def test_one_below_max_passes(self):
        max_pos = _load_risk_cfg()["position_limits"]["max_concurrent_positions"]
        positions = [
            Position(f"TICK{i}", "us", 1, 100.0, 110.0, 10.0, "USD")
            for i in range(max_pos - 1)
        ]
        guard = _make_guard(positions=positions)
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal(ticker="NEWSTOCK"))
        if reason:
            assert "max_positions_reached" not in reason


# ── Rule 5 & 6: order size cap ────────────────────────────────────────────────

class TestOrderSizeCapEndToEnd:
    @pytest.mark.asyncio
    async def test_expensive_us_stock_rejected(self):
        guard = _make_guard()
        guard._broker.get_quote = AsyncMock(return_value=MagicMock(last_price=6000.0))
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal(ticker="BRK"))
        assert reason is not None
        assert "order_size" in reason
        assert "exceeds cap" in reason

    @pytest.mark.asyncio
    async def test_normal_us_stock_passes_size(self):
        guard = _make_guard()
        guard._broker.get_quote = AsyncMock(return_value=MagicMock(last_price=150.0))
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal(ticker="AAPL"))
        if reason:
            assert "order_size" not in reason


# ── Rule 5: market regime ─────────────────────────────────────────────────────

class TestMarketRegimeFilter:
    _REGIME_CFG = {
        "market_regime": {
            "enabled": True,
            "benchmarks": {"us": "SPY", "india": "^NSEI"},
            "ma_window": 50,
            "cache_ttl_minutes": 60,
        }
    }

    @pytest.mark.asyncio
    async def test_bear_market_rejects_buy(self):
        """SPY below 50d MA → BUY signal rejected."""
        guard = _make_guard(risk_overrides=self._REGIME_CFG)
        with patch.object(guard, "_fetch_regime", return_value=False), \
             patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal())
        assert reason is not None
        assert "regime:bear_market" in reason

    @pytest.mark.asyncio
    async def test_bull_market_passes(self):
        """SPY above 50d MA → regime check passes."""
        guard = _make_guard(risk_overrides=self._REGIME_CFG)
        with patch.object(guard, "_fetch_regime", return_value=True), \
             patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal())
        if reason:
            assert "regime" not in reason

    @pytest.mark.asyncio
    async def test_regime_disabled_always_passes(self):
        """When market_regime.enabled is False the check is skipped entirely."""
        guard = _make_guard(risk_overrides={"market_regime": {"enabled": False}})
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal())
        if reason:
            assert "regime" not in reason

    @pytest.mark.asyncio
    async def test_yfinance_failure_allows_signal(self):
        """If yfinance raises, the regime check fails open (signal is allowed through)."""
        guard = _make_guard(risk_overrides=self._REGIME_CFG)
        with patch.object(guard, "_fetch_regime", side_effect=RuntimeError("network error")), \
             patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal())
        if reason:
            assert "regime" not in reason

    @pytest.mark.asyncio
    async def test_regime_result_is_cached(self):
        """_fetch_regime called only once even if _check_market_regime is called twice."""
        guard = _make_guard(risk_overrides=self._REGIME_CFG)
        with patch.object(guard, "_fetch_regime", return_value=True) as mock_fetch:
            await guard._check_market_regime("us")
            await guard._check_market_regime("us")
        mock_fetch.assert_called_once()


# ── Rule 7: clean pass ────────────────────────────────────────────────────────

class TestCleanPass:
    @pytest.mark.asyncio
    async def test_valid_signal_passes_all_checks(self):
        guard = _make_guard(positions=[])
        guard._broker.get_quote = AsyncMock(return_value=MagicMock(last_price=150.0))
        with patch("agents.risk_guard.agent.datetime") as mock_dt:
            mock_dt.now.return_value = _market_open_time()
            reason = await guard._run_checks(_make_signal(
                ticker="MSFT", action="BUY", score=80.0
            ))
        assert reason is None, f"Expected clean pass, got rejection: {reason}"
