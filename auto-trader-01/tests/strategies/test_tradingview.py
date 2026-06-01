"""Tests for TradingView webhook adapter and supporting logic."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from agents.strategy_engine.strategies.tradingview.adapter import (
    build_signal, is_stale, resolve_market,
)
from agents.risk_guard.agent import RiskGuardAgent


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _ts(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _payload(**overrides) -> dict:
    base = {
        "strategy_id": "golden_cross_v1",
        "ticker":      "AAPL",
        "exchange":    "NASDAQ",
        "action":      "BUY",
        "close":       189.50,
        "volume":      52_000_000,
        "score":       78.5,
        "timeframe":   "1D",
        "timestamp":   _ts(),
    }
    base.update(overrides)
    return base


_SOURCE_CFG = {
    "signal":    {"default_score": 85.0, "min_score": 60.0},
    "execution": {"stoploss_pct": 0.05, "target_pct": 0.12, "limit_price_slippage": 0.003},
}

_RISK_CFG = {
    "position_limits": {
        "max_order_size_usd": 5_000,
        "max_order_size_inr": 500_000,
        "max_concurrent_positions": 10,
    },
    "position_sizing": {
        "score_tiers": [
            {"min_score": 85, "size_pct": 1.00},
            {"min_score": 70, "size_pct": 0.60},
            {"min_score": 60, "size_pct": 0.30},
        ]
    },
    "sessions": {"allow_continuous": True},
}


# ── resolve_market ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("exchange,expected", [
    ("NSE",    "india"),
    ("BSE",    "india"),
    ("NASDAQ", "us"),
    ("NYSE",   "us"),
    ("ARCA",   "us"),
    ("CBOE",   "us"),
    ("nse",    "india"),   # case-insensitive
])
def test_resolve_market_known(exchange, expected):
    assert resolve_market(exchange) == expected


def test_resolve_market_unknown():
    assert resolve_market("UNKNOWN") is None


# ── is_stale ───────────────────────────────────────────────────────────────────

def test_is_stale_fresh():
    assert not is_stale(_ts(-10))         # 10 seconds old → fresh


def test_is_stale_old():
    assert is_stale(_ts(-400))            # 400 seconds old → stale


def test_is_stale_future():
    assert not is_stale(_ts(+30))         # future timestamp → not stale


def test_is_stale_bad_format():
    assert is_stale("not-a-timestamp")    # unparseable → treated as stale


# ── build_signal — score handling ─────────────────────────────────────────────

def test_build_signal_score_from_payload():
    sig = build_signal(_payload(score=78.5), _SOURCE_CFG)
    assert sig.composite_score == 78.5
    assert sig.confidence == "medium"


def test_build_signal_score_high():
    sig = build_signal(_payload(score=88.0), _SOURCE_CFG)
    assert sig.confidence == "high"


def test_build_signal_score_low():
    sig = build_signal(_payload(score=62.0), _SOURCE_CFG)
    assert sig.confidence == "medium"


def test_build_signal_score_missing_uses_default():
    payload = _payload()
    del payload["score"]
    sig = build_signal(payload, _SOURCE_CFG)
    assert sig.composite_score == 85.0    # config default_score


def test_build_signal_score_zero_uses_default():
    sig = build_signal(_payload(score=0), _SOURCE_CFG)
    assert sig.composite_score == 85.0    # 0 is falsy → falls back to default


# ── build_signal — field mapping ──────────────────────────────────────────────

def test_build_signal_ticker_uppercased():
    sig = build_signal(_payload(ticker="aapl"), _SOURCE_CFG)
    assert sig.ticker == "AAPL"


def test_build_signal_market_id_resolved():
    sig = build_signal(_payload(exchange="NSE", ticker="RELIANCE"), _SOURCE_CFG)
    assert sig.market_id == "india"


def test_build_signal_strategy_id_metadata():
    sig = build_signal(_payload(strategy_id="my_custom_v2"), _SOURCE_CFG)
    assert sig.strategy_id == "my_custom_v2"
    assert sig.context["source"] == "tradingview"


def test_build_signal_strategy_id_defaults_when_absent():
    payload = _payload()
    del payload["strategy_id"]
    sig = build_signal(payload, _SOURCE_CFG)
    assert sig.strategy_id == "tradingview"


# ── build_signal — execution param overrides ──────────────────────────────────

def test_build_signal_execution_defaults():
    sig = build_signal(_payload(), _SOURCE_CFG)
    assert sig.context["stoploss_pct"] == 0.05
    assert sig.context["target_pct"]   == 0.12


def test_build_signal_execution_payload_override():
    sig = build_signal(_payload(stoploss_pct=0.08, target_pct=0.18), _SOURCE_CFG)
    assert sig.context["stoploss_pct"] == 0.08
    assert sig.context["target_pct"]   == 0.18


def test_build_signal_last_price_set():
    sig = build_signal(_payload(close=189.50), _SOURCE_CFG)
    assert sig.context["last_price"] == 189.50


# ── build_signal — stats passthrough ──────────────────────────────────────────

def test_build_signal_stats_stored():
    stats   = {"net_pct": 34.2, "dd_pct": 12.1, "win_pct": 58.3, "pf": 1.87, "rr": 1.92, "trades": 47}
    sig     = build_signal(_payload(stats=stats), _SOURCE_CFG)
    assert sig.context["stats"] == stats


def test_build_signal_stats_absent():
    sig = build_signal(_payload(), _SOURCE_CFG)
    assert sig.context["stats"] == {}


# ── RiskGuardAgent — score-to-size tiers ──────────────────────────────────────

class _FakeGuard:
    """Minimal stand-in to test _score_to_size_pct and _calculate_quantity."""
    def __init__(self, risk_cfg):
        self._risk = risk_cfg

    _score_to_size_pct = RiskGuardAgent._score_to_size_pct
    _calculate_quantity = RiskGuardAgent._calculate_quantity


@pytest.mark.parametrize("score,expected_pct", [
    (90.0, 1.00),
    (85.0, 1.00),
    (80.0, 0.60),
    (70.0, 0.60),
    (65.0, 0.30),
    (60.0, 0.30),
])
def test_score_to_size_pct(score, expected_pct):
    guard = _FakeGuard(_RISK_CFG)
    assert guard._score_to_size_pct(score) == expected_pct


def test_score_to_size_pct_no_tiers_fallback():
    guard = _FakeGuard({"position_sizing": {}})
    assert guard._score_to_size_pct(75.0) == 0.5


def test_calculate_quantity_full_position():
    guard = _FakeGuard(_RISK_CFG)
    # score=90 → size_pct=1.0 → cap=$5000 → qty = 5000/189 ≈ 26
    qty = guard._calculate_quantity(189.0, "us", 90.0)
    assert qty == int(5_000 / 189.0)


def test_calculate_quantity_half_position():
    guard = _FakeGuard(_RISK_CFG)
    qty_full = guard._calculate_quantity(189.0, "us", 90.0)
    qty_60   = guard._calculate_quantity(189.0, "us", 75.0)
    assert qty_60 < qty_full


def test_calculate_quantity_zero_price():
    guard = _FakeGuard(_RISK_CFG)
    assert guard._calculate_quantity(0.0, "us", 90.0) == 1
