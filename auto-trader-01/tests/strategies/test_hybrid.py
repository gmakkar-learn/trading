"""HybridStrategy unit tests.

Verifies the two-gate logic:
  1. No fundamental signal in cache → no hybrid signal
  2. Fundamental below threshold → no hybrid signal
  3. Fundamental BUY present but technical below threshold → no hybrid signal
  4. Both gates pass → hybrid BUY emitted with weighted combined score
  5. Not enough candles → no hybrid signal
  6. Combined score below combined_buy threshold → no hybrid signal
  7. Fundamental HOLD in cache → no hybrid signal
  8. Fundamental signal older than 168h TTL → no hybrid signal

Technical scorer functions are patched so tests don't depend on
pandas-ta indicator math.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from agents.strategy_engine.signal_cache import SignalCache
from agents.strategy_engine.strategy_context import StrategyContext
from agents.strategy_engine.strategies.hybrid.strategy import HybridStrategy
from infrastructure.event_bus.events import CandleEvent, TradingSignal


# ── helpers ───────────────────────────────────────────────────────────────────

_CFG = {
    "fundamental_strategy_id": "fundamental_v1",
    "weights": {"fundamental": 0.60, "technical": 0.40},
    "thresholds": {
        "fundamental_min_score": 70.0,
        "technical_min_score":   60.0,
        "combined_buy":          65.0,
    },
    "indicators": {
        "weights": {"sma": 0.40, "rsi": 0.30, "macd": 0.30},
    },
    "market_overrides": {
        "india": {
            "technical_gate_enabled": False,
            "fundamental_min_score":  70.0,
        },
    },
}


def _make_candles(n: int = 210) -> list[dict]:
    """Minimal OHLCV candle list for testing."""
    return [
        {"open": 150.0, "high": 152.0, "low": 149.0, "close": 151.0, "volume": 1_000_000}
        for _ in range(n)
    ]


def _make_fund_signal(
    score: float = 75.0,
    action: str = "BUY",
    created_at: datetime | None = None,
) -> TradingSignal:
    kwargs: dict = dict(
        ticker="AAPL", market_id="us", strategy_type="fundamental",
        strategy_id="fundamental_v1", composite_score=score,
        recommended_action=action, confidence="high",
        rationale="strong earnings", context={},
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    return TradingSignal(**kwargs)


def _make_candle_event(ticker: str = "AAPL", n_candles: int = 210, market_id: str = "us") -> CandleEvent:
    return CandleEvent(
        ticker=ticker, market_id=market_id, timeframe="1d",
        close=151.0, candles=_make_candles(n_candles),
    )


def _make_context(fund_signal: TradingSignal | None = None) -> StrategyContext:
    cache = SignalCache()
    if fund_signal:
        cache.put(fund_signal)
    ctx = MagicMock(spec=StrategyContext)
    ctx.signal_cache = cache
    return ctx


def _make_strategy() -> HybridStrategy:
    return HybridStrategy(config=_CFG)


# ── patch target ─────────────────────────────────────────────────────────────

_SCORE_PATH = "agents.strategy_engine.strategies.hybrid.strategy"


# ── tests ─────────────────────────────────────────────────────────────────────

class TestHybridGating:
    @pytest.mark.asyncio
    async def test_no_fundamental_signal_no_hybrid(self):
        """Cache miss → hybrid stays silent."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=None)
        with patch(f"{_SCORE_PATH}._score_sma", return_value=(80.0, "above")), \
             patch(f"{_SCORE_PATH}._score_rsi", return_value=(70.0, "42")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(75.0, "above")):
            result = await strategy.evaluate(_make_candle_event(), ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_fundamental_hold_no_hybrid(self):
        """Fundamental HOLD in cache → no hybrid signal."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=_make_fund_signal(score=80.0, action="HOLD"))
        with patch(f"{_SCORE_PATH}._score_sma", return_value=(80.0, "above")), \
             patch(f"{_SCORE_PATH}._score_rsi", return_value=(70.0, "42")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(75.0, "above")):
            result = await strategy.evaluate(_make_candle_event(), ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_fundamental_below_min_score_no_hybrid(self):
        """Fundamental BUY but score < fundamental_min_score → blocked."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=_make_fund_signal(score=65.0, action="BUY"))
        with patch(f"{_SCORE_PATH}._score_sma", return_value=(80.0, "above")), \
             patch(f"{_SCORE_PATH}._score_rsi", return_value=(70.0, "42")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(75.0, "above")):
            result = await strategy.evaluate(_make_candle_event(), ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_technical_below_min_score_no_hybrid(self):
        """Fundamental passes but technical composite < technical_min_score → blocked."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=_make_fund_signal(score=80.0))
        # Tech scores that produce composite ≈ 40 (below 60)
        with patch(f"{_SCORE_PATH}._score_sma", return_value=(40.0, "below")), \
             patch(f"{_SCORE_PATH}._score_rsi", return_value=(40.0, "65")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(40.0, "below")):
            result = await strategy.evaluate(_make_candle_event(), ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_not_enough_candles_no_hybrid(self):
        """< 210 candles → no hybrid signal (same guard as TechnicalStrategy)."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=_make_fund_signal(score=80.0))
        result = await strategy.evaluate(_make_candle_event(n_candles=100), ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_stale_fundamental_signal_no_hybrid(self):
        """Fundamental signal older than 168h TTL → no hybrid signal."""
        strategy = _make_strategy()
        stale = _make_fund_signal(
            score=80.0,
            created_at=datetime.utcnow() - timedelta(hours=169),
        )
        ctx = _make_context(fund_signal=stale)
        with patch(f"{_SCORE_PATH}._score_sma", return_value=(80.0, "above")), \
             patch(f"{_SCORE_PATH}._score_rsi", return_value=(70.0, "42")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(75.0, "above")):
            result = await strategy.evaluate(_make_candle_event(), ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_combined_score_below_threshold_no_hybrid(self):
        """Both gates pass individually but weighted combined < combined_buy → blocked."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=_make_fund_signal(score=70.0))
        # tech composite = 60.0, combined = 70*0.6 + 60*0.4 = 42+24 = 66 → passes 65 threshold
        # Force lower: fund=70, tech=60 → combined=66 > 65 → would pass
        # Use fund=70, tech=60 with combined_buy=68 override
        cfg_strict = dict(_CFG)
        cfg_strict["thresholds"] = {**_CFG["thresholds"], "combined_buy": 68.0}
        strategy = HybridStrategy(config=cfg_strict)
        with patch(f"{_SCORE_PATH}._score_sma", return_value=(60.0, "above")), \
             patch(f"{_SCORE_PATH}._score_rsi", return_value=(60.0, "50")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(60.0, "above")):
            result = await strategy.evaluate(_make_candle_event(), ctx)
        assert result is None


class TestHybridSignalEmission:
    @pytest.mark.asyncio
    async def test_both_gates_pass_emits_hybrid(self):
        """Fundamental BUY ≥70 + technical ≥60 → hybrid BUY emitted."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=_make_fund_signal(score=80.0))
        with patch(f"{_SCORE_PATH}._score_sma", return_value=(75.0, "above")), \
             patch(f"{_SCORE_PATH}._score_rsi", return_value=(65.0, "42")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(70.0, "above")):
            result = await strategy.evaluate(_make_candle_event(), ctx)
        assert result is not None
        assert result.recommended_action == "BUY"
        assert result.strategy_type == "hybrid"
        assert result.strategy_id == "hybrid_v1"

    @pytest.mark.asyncio
    async def test_combined_score_is_weighted_average(self):
        """combined = fundamental*0.6 + technical*0.4."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=_make_fund_signal(score=80.0))
        # tech composite = 70*0.4 + 65*0.3 + 68*0.3 = 28 + 19.5 + 20.4 = 67.9
        with patch(f"{_SCORE_PATH}._score_sma",  return_value=(70.0, "above")), \
             patch(f"{_SCORE_PATH}._score_rsi",  return_value=(65.0, "42")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(68.0, "above")):
            result = await strategy.evaluate(_make_candle_event(), ctx)
        assert result is not None
        tech = 70.0 * 0.40 + 65.0 * 0.30 + 68.0 * 0.30
        expected = round(80.0 * 0.60 + tech * 0.40, 1)
        assert result.composite_score == expected

    @pytest.mark.asyncio
    async def test_context_carries_both_scores(self):
        """Signal context must include fundamental_score and technical_score."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=_make_fund_signal(score=76.0))
        with patch(f"{_SCORE_PATH}._score_sma",  return_value=(80.0, "above")), \
             patch(f"{_SCORE_PATH}._score_rsi",  return_value=(70.0, "38")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(75.0, "above")):
            result = await strategy.evaluate(_make_candle_event(), ctx)
        assert result is not None
        assert result.context["fundamental_score"] == 76.0
        assert "technical_score" in result.context
        assert result.context["last_price"] == 151.0

    @pytest.mark.asyncio
    async def test_ticker_mismatch_in_cache_no_signal(self):
        """Fundamental signal cached for MSFT should not trigger hybrid for AAPL."""
        strategy = _make_strategy()
        msft_signal = TradingSignal(
            ticker="MSFT", market_id="us", strategy_type="fundamental",
            strategy_id="fundamental_v1", composite_score=80.0,
            recommended_action="BUY", confidence="high",
            rationale="msft earnings", context={},
        )
        ctx = _make_context(fund_signal=msft_signal)
        with patch(f"{_SCORE_PATH}._score_sma",  return_value=(80.0, "above")), \
             patch(f"{_SCORE_PATH}._score_rsi",  return_value=(70.0, "42")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(75.0, "above")):
            result = await strategy.evaluate(_make_candle_event(ticker="AAPL"), ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_confidence_high_when_combined_ge_80(self):
        """confidence=high when combined score ≥ 80."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=_make_fund_signal(score=90.0))
        # tech = 80*0.4 + 75*0.3 + 78*0.3 = 32 + 22.5 + 23.4 = 77.9
        # combined = 90*0.6 + 77.9*0.4 = 54 + 31.2 = 85.2
        with patch(f"{_SCORE_PATH}._score_sma",  return_value=(80.0, "above")), \
             patch(f"{_SCORE_PATH}._score_rsi",  return_value=(75.0, "38")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(78.0, "above")):
            result = await strategy.evaluate(_make_candle_event(), ctx)
        assert result is not None
        assert result.confidence == "high"


class TestHybridMarketOverrides:
    """Technical gate can be disabled per market via market_overrides config."""

    @pytest.mark.asyncio
    async def test_india_tech_gate_disabled_emits_on_fundamental_alone(self):
        """India market: tech gate disabled → BUY emitted even when tech scores are weak."""
        strategy = _make_strategy()
        fund_signal = TradingSignal(
            ticker="CAPLIPOINT", market_id="india", strategy_type="fundamental",
            strategy_id="fundamental_v1", composite_score=76.0,
            recommended_action="BUY", confidence="medium",
            rationale="strong earnings", context={},
        )
        ctx = _make_context(fund_signal=fund_signal)
        # Tech scores that would fail the US gate (composite ≈ 40 < 60)
        with patch(f"{_SCORE_PATH}._score_sma",  return_value=(40.0, "below")), \
             patch(f"{_SCORE_PATH}._score_rsi",  return_value=(40.0, "65")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(40.0, "below")):
            result = await strategy.evaluate(
                _make_candle_event(ticker="CAPLIPOINT", market_id="india"), ctx
            )
        assert result is not None
        assert result.recommended_action == "BUY"
        assert result.composite_score == 76.0
        assert result.context["technical_gate"] == "disabled"

    @pytest.mark.asyncio
    async def test_india_tech_gate_disabled_uses_fundamental_score_as_combined(self):
        """India: combined score equals the fundamental score (no blending)."""
        strategy = _make_strategy()
        fund_signal = TradingSignal(
            ticker="POLYMED", market_id="india", strategy_type="fundamental",
            strategy_id="fundamental_v1", composite_score=82.0,
            recommended_action="BUY", confidence="high",
            rationale="revenue beat", context={},
        )
        ctx = _make_context(fund_signal=fund_signal)
        result = await strategy.evaluate(
            _make_candle_event(ticker="POLYMED", market_id="india"), ctx
        )
        assert result is not None
        assert result.composite_score == 82.0
        assert result.confidence == "high"   # ≥80

    @pytest.mark.asyncio
    async def test_india_tech_gate_disabled_still_enforces_fund_min_score(self):
        """India: fundamental_min_score override still gates low-scoring signals."""
        strategy = _make_strategy()
        fund_signal = TradingSignal(
            ticker="JKPAPER", market_id="india", strategy_type="fundamental",
            strategy_id="fundamental_v1", composite_score=65.0,  # below India min of 70
            recommended_action="BUY", confidence="low",
            rationale="moderate earnings", context={},
        )
        ctx = _make_context(fund_signal=fund_signal)
        result = await strategy.evaluate(
            _make_candle_event(ticker="JKPAPER", market_id="india"), ctx
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_us_market_still_requires_technical_gate(self):
        """US market is unaffected — technical gate remains enforced."""
        strategy = _make_strategy()
        ctx = _make_context(fund_signal=_make_fund_signal(score=80.0))
        # Weak tech scores that fail the US gate
        with patch(f"{_SCORE_PATH}._score_sma",  return_value=(40.0, "below")), \
             patch(f"{_SCORE_PATH}._score_rsi",  return_value=(40.0, "65")), \
             patch(f"{_SCORE_PATH}._score_macd", return_value=(40.0, "below")):
            result = await strategy.evaluate(_make_candle_event(market_id="us"), ctx)
        assert result is None
