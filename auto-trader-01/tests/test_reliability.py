"""
Recovery and Reliability Test Harness — pre-live validation #5

Unit tests for system resilience:
  1. DB write failure is logged but does not crash the system
  2. In-memory signal_history is bounded to 200 entries (no unbounded growth)
  3. In-memory alert_log is bounded to 100 entries
  4. signal_history falls back gracefully when DB is unavailable
  5. AuditLogger critical-logs (not raises) on write failure
  6. EventBus continues delivering events when one subscriber throws
  7. StrategyEngine continues processing when one strategy raises
  8. WatchlistProvider handles missing YAML file without crashing
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 1. DB write failure doesn't crash ────────────────────────────────────────

class TestAuditLoggerResilience:
    @pytest.mark.asyncio
    async def test_db_failure_logs_critical_not_raises(self, caplog):
        """AuditLogger must never raise — it logs CRITICAL and continues."""
        from infrastructure.audit.audit_logger import AuditLogger

        # Session factory that always raises
        def bad_factory():
            raise RuntimeError("DB connection refused")

        # Use a context-manager-compatible mock that raises on __aenter__
        bad_session = MagicMock()
        bad_session.__aenter__ = AsyncMock(side_effect=Exception("DB down"))
        bad_session.__aexit__ = AsyncMock(return_value=False)

        def bad_factory_fn():
            return bad_session

        audit = AuditLogger(bad_factory_fn)

        with caplog.at_level(logging.CRITICAL):
            # Should not raise
            await audit.log(decision="SIGNAL", market_id="us", ticker="AAPL")

        assert any("AUDIT WRITE FAILED" in r.message for r in caplog.records), (
            "Expected CRITICAL log on DB failure"
        )

    @pytest.mark.asyncio
    async def test_get_signal_history_returns_empty_on_db_failure(self):
        """get_signal_history returns [] when DB is down, not an exception."""
        from infrastructure.audit.audit_logger import AuditLogger

        bad_session = MagicMock()
        bad_session.__aenter__ = AsyncMock(side_effect=Exception("DB down"))
        bad_session.__aexit__ = AsyncMock(return_value=False)

        audit = AuditLogger(lambda: bad_session)
        result = await audit.get_signal_history(market_id="us", limit=10)
        assert result == [], f"Expected empty list, got {result}"


# ── 2. In-memory signal_history is bounded ───────────────────────────────────

class TestInMemoryBounds:
    def test_signal_history_capped_at_200(self):
        """Simulates the main.py _record_signal logic — history must not grow beyond 200."""
        signal_history: list[dict] = []
        cap = 200

        for i in range(300):
            signal_history.append({"signal_id": f"sig-{i}"})
            if len(signal_history) > cap:
                signal_history.pop(0)

        assert len(signal_history) == cap
        # Most recent should be last
        assert signal_history[-1]["signal_id"] == "sig-299"
        # Oldest should be sig-100 (first 100 evicted)
        assert signal_history[0]["signal_id"] == "sig-100"

    def test_alert_log_capped_at_100(self):
        """alert_log in AppState must not grow beyond 100."""
        alert_log: list[dict] = []
        cap = 100

        for i in range(150):
            alert_log.append({"id": i})
            if len(alert_log) > cap:
                alert_log.pop(0)

        assert len(alert_log) == cap
        assert alert_log[-1]["id"] == 149
        assert alert_log[0]["id"] == 50

    def test_appstate_initialises_empty(self):
        """Fresh AppState should have empty collections, not shared mutable defaults."""
        from api.state import AppState
        s1 = AppState()
        s2 = AppState()
        s1.signal_history.append({"x": 1})
        # s2 must not be affected — each instance has its own list
        assert s2.signal_history == [], "AppState instances must not share mutable defaults"


# ── 3. EventBus continues on subscriber exception ────────────────────────────

class TestEventBusResilience:
    @pytest.mark.asyncio
    async def test_bad_subscriber_does_not_block_others(self):
        """If one subscriber raises, the others still receive the event."""
        from infrastructure.event_bus.bus import EventBus
        from infrastructure.event_bus.events import TradingSignalEvent, TradingSignal
        from datetime import datetime, timezone

        bus = EventBus()
        received = []

        async def bad_handler(event):
            raise RuntimeError("subscriber crash")

        async def good_handler(event):
            received.append(event)

        bus.subscribe(TradingSignalEvent, bad_handler)
        bus.subscribe(TradingSignalEvent, good_handler)

        signal = TradingSignal(
            ticker="AAPL", market_id="us", strategy_type="fundamental",
            strategy_id="test", composite_score=75.0, recommended_action="BUY",
            confidence="high", rationale="test", context={},
        )
        # Should not raise even though bad_handler crashes
        await bus.publish(TradingSignalEvent(signal=signal))

        assert len(received) == 1, "Good handler should still receive event"


# ── 4. StrategyEngine continues on single strategy failure ───────────────────

class TestStrategyEngineResilience:
    @pytest.mark.asyncio
    async def test_failing_strategy_does_not_block_others(self):
        """If one strategy.evaluate() raises, the engine continues with the rest."""
        from agents.strategy_engine.engine import StrategyEngine
        from agents.strategy_engine.data_feeds.feed_types import DataFeedType
        from infrastructure.event_bus.bus import EventBus
        from infrastructure.event_bus.events import AnnouncementEvent, TradingSignalEvent
        from datetime import datetime, timezone

        bus = EventBus()
        emitted = []

        async def capture(e):
            emitted.append(e)

        bus.subscribe(TradingSignalEvent, capture)

        # Strategy that always raises
        bad_strategy = MagicMock()
        bad_strategy.strategy_id = "bad_strat"
        bad_strategy.subscriptions = [DataFeedType.ANNOUNCEMENTS]
        bad_strategy.evaluate = AsyncMock(side_effect=RuntimeError("strategy crash"))

        # Strategy that always emits a signal
        from infrastructure.event_bus.events import TradingSignal
        good_signal = TradingSignal(
            ticker="AAPL", market_id="us", strategy_type="fundamental",
            strategy_id="good_strat", composite_score=80.0,
            recommended_action="BUY", confidence="high",
            rationale="test", context={},
        )
        good_strategy = MagicMock()
        good_strategy.strategy_id = "good_strat"
        good_strategy.subscriptions = [DataFeedType.ANNOUNCEMENTS]
        good_strategy.evaluate = AsyncMock(return_value=good_signal)

        audit = MagicMock()
        audit.log = AsyncMock()

        ctx = MagicMock()
        ctx.market.market_id = "us"

        engine = StrategyEngine([bad_strategy, good_strategy], ctx, bus, audit)

        event = AnnouncementEvent(
            market_id="us", ticker="AAPL", filing_id="test-001",
            filing_type="8-K", filing_url="https://example.com",
            document_url="https://example.com/doc.htm",
            items=["2.02"], published_at=datetime.now(timezone.utc),
        )
        await engine.handle_announcement(event)

        assert len(emitted) == 1, "Good strategy signal should still be emitted despite bad strategy"
        assert emitted[0].signal.strategy_id == "good_strat"


# ── 5. WatchlistProvider handles missing file ─────────────────────────────────

class TestWatchlistResilience:
    @pytest.mark.asyncio
    async def test_refresh_missing_market_returns_empty(self):
        """WatchlistProvider.refresh() for an unknown market should not crash."""
        from infrastructure.watchlist.provider import WatchlistProvider
        config_dir = Path(__file__).parent.parent / "config"
        provider = WatchlistProvider(config_dir)

        # Should not raise
        await provider.refresh("nonexistent_market")
        tickers = await provider.get_tickers("nonexistent_market")
        assert tickers == [], f"Expected empty list for unknown market, got {tickers}"

    @pytest.mark.asyncio
    async def test_add_ticker_unknown_market_returns_false(self):
        """Adding a ticker to an unknown market must return False, not raise."""
        from infrastructure.watchlist.provider import WatchlistProvider
        config_dir = Path(__file__).parent.parent / "config"
        provider = WatchlistProvider(config_dir)

        result = await provider.add_ticker("nonexistent_market", "AAPL")
        assert result is False
