"""Convert a TradingView webhook payload into a TradingSignal.

The app has no knowledge of individual Pine Script strategies.
strategy_id from the payload is metadata stored in the audit log — never
used for routing or config lookup. All execution parameters come from the
single tradingview.yaml source config, with payload values taking precedence.
"""
from __future__ import annotations

from datetime import datetime, timezone

from infrastructure.event_bus.events import TradingSignal

_EXCHANGE_MAP: dict[str, str] = {
    "NSE": "india", "BSE": "india",
    "NASDAQ": "us", "NYSE": "us", "ARCA": "us", "CBOE": "us", "BATS": "us",
}

_DEFAULTS = {
    "default_score": 85.0,
    "min_score":     60.0,
    "stoploss_pct":  0.05,
    "target_pct":    0.12,
    "limit_price_slippage": 0.003,
}


def resolve_market(exchange: str) -> str | None:
    return _EXCHANGE_MAP.get(exchange.upper())


def build_signal(payload: dict, source_cfg: dict) -> TradingSignal:
    action = payload["action"].upper()

    sig_cfg  = source_cfg.get("signal", {})
    exec_cfg = source_cfg.get("execution", {})

    # Score: payload → config default → hardcoded fallback
    score = float(
        payload.get("score")
        or sig_cfg.get("default_score", _DEFAULTS["default_score"])
    )

    if score >= 80:
        confidence = "high"
    elif score >= 60:
        confidence = "medium"
    else:
        confidence = "low"

    # Execution params: payload → config → hardcoded fallback
    stoploss_pct = float(payload.get("stoploss_pct") or exec_cfg.get("stoploss_pct",  _DEFAULTS["stoploss_pct"]))
    target_pct   = float(payload.get("target_pct")   or exec_cfg.get("target_pct",    _DEFAULTS["target_pct"]))
    slippage_pct = float(payload.get("slippage_pct") or exec_cfg.get("limit_price_slippage", _DEFAULTS["limit_price_slippage"]))

    stats       = payload.get("stats") or {}
    strategy_id = payload.get("strategy_id") or "tradingview"
    close       = payload.get("close", 0.0)

    return TradingSignal(
        ticker             = payload["ticker"].upper(),
        market_id          = resolve_market(payload["exchange"]),
        strategy_type      = "technical",
        strategy_id        = strategy_id,
        composite_score    = score,
        recommended_action = action,
        confidence         = confidence,
        rationale          = (
            f"TradingView {strategy_id} — "
            f"{action} @ {close} "
            f"(score={score:.0f}, tf={payload.get('timeframe', '?')}, "
            f"pf={stats.get('pf', '?')}, trades={stats.get('trades', '?')})"
        ),
        context={
            "source":       "tradingview",
            "close":        close,
            "last_price":   float(close) if close else 0.0,
            "volume":       payload.get("volume"),
            "timeframe":    payload.get("timeframe"),
            "exchange":     payload["exchange"],
            "score":        score,
            "stoploss_pct": stoploss_pct,
            "target_pct":   target_pct,
            "slippage_pct": slippage_pct,
            "tv_timestamp": payload.get("timestamp"),
            "stats":        stats,
        },
    )


def is_stale(timestamp_str: str, max_age_seconds: int = 300) -> bool:
    """Return True if the alert timestamp is older than max_age_seconds.

    Accepts ISO 8601 strings or Unix millisecond strings (Pine Script timenow).
    Unresolved TradingView template variables (e.g. {{timenow}}) are treated as
    fresh — the HTTP delivery itself is the best freshness signal in that case.
    """
    if not timestamp_str or "{{" in str(timestamp_str):
        return False  # unresolved template → assume fresh
    try:
        # ISO 8601: "2026-06-02T00:32:03Z" or "2026-06-02T00:32:03.490Z"
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        try:
            # Unix milliseconds: "1748824323000" (Pine Script timenow)
            ts = datetime.fromtimestamp(int(timestamp_str) / 1000, tz=timezone.utc)
        except (ValueError, TypeError):
            return False  # unparseable → assume fresh, delivery time is the guard
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age > max_age_seconds
