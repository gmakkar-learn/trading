"""POST /api/webhooks/tradingview — receives Pine Script alert payloads.

Processing pipeline:
  authenticate → parse → stale check → market resolution → min_score gate
  → build TradingSignal → publish to EventBus → log → 200 OK

Always returns HTTP 200 on successful receipt (including stale/discarded alerts)
because TradingView retries on non-2xx responses, which would cause duplicate orders.

Also exposes:
  GET /api/webhooks/tradingview/log   — last 100 received alerts with outcomes
  GET /api/webhooks/tradingview/setup — pre-filled alert config for TradingView UI
"""
from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Query, Request

from agents.strategy_engine.strategies.tradingview.adapter import (
    build_signal, is_stale, resolve_market,
)
from api.state import get_state
from infrastructure.event_bus.events import TradingSignalEvent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks/tradingview", tags=["tradingview"])

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "strategies" / "tradingview.yaml"
_REQUIRED_FIELDS = {"ticker", "exchange", "action", "close", "timestamp"}


def _load_source_cfg() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("tradingview.yaml not found — using hardcoded defaults")
        return {}


def _check_secret(secret: str | None) -> bool:
    expected = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "")
    if not expected:
        logger.warning("TRADINGVIEW_WEBHOOK_SECRET not set — webhook auth disabled")
        return True
    if not secret:
        return False
    return hmac.compare_digest(expected, secret)


def _log_alert(state, entry: dict) -> None:
    state.alert_log.append(entry)
    if len(state.alert_log) > 100:
        state.alert_log.pop(0)


@router.post("")
async def receive_webhook(request: Request, secret: str | None = Query(default=None)):
    state   = get_state()
    src_ip  = request.client.host if request.client else "unknown"
    recv_at = datetime.now(timezone.utc).isoformat()

    # 1. Authenticate
    if not _check_secret(secret):
        logger.warning("TV webhook auth failure from %s", src_ip)
        _log_alert(state, {"received_at": recv_at, "outcome": "auth_failure", "ip": src_ip})
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    # 2. Parse
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "outcome": "parse_error"}

    missing = _REQUIRED_FIELDS - set(payload.keys())
    if missing:
        _log_alert(state, {"received_at": recv_at, "outcome": "missing_fields", "missing": list(missing)})
        return {"ok": False, "outcome": "missing_fields", "missing": list(missing)}

    ticker      = payload.get("ticker", "?")
    strategy_id = payload.get("strategy_id", "tradingview")

    # 3. Stale check
    if is_stale(payload["timestamp"]):
        logger.info("TV webhook discarded stale alert: %s %s", strategy_id, ticker)
        _log_alert(state, {"received_at": recv_at, "strategy_id": strategy_id,
                            "ticker": ticker, "action": payload.get("action"),
                            "outcome": "discarded_stale"})
        return {"ok": True, "outcome": "discarded_stale"}

    # 4. Market resolution
    market_id = resolve_market(payload["exchange"])
    if market_id is None:
        logger.warning("TV webhook unknown exchange: %s", payload["exchange"])
        _log_alert(state, {"received_at": recv_at, "strategy_id": strategy_id,
                            "ticker": ticker, "outcome": "unknown_exchange",
                            "exchange": payload["exchange"]})
        return {"ok": True, "outcome": "unknown_exchange"}

    # 5. min_score gate
    source_cfg = _load_source_cfg()
    min_score  = float(source_cfg.get("signal", {}).get("min_score", 60.0))
    score      = float(payload.get("score") or source_cfg.get("signal", {}).get("default_score", 85.0))
    if score < min_score:
        logger.info("TV webhook signal discarded: score %.1f < min %.1f (%s %s)", score, min_score, strategy_id, ticker)
        _log_alert(state, {"received_at": recv_at, "strategy_id": strategy_id,
                            "ticker": ticker, "action": payload.get("action"),
                            "score": score, "outcome": "discarded_low_score"})
        return {"ok": True, "outcome": "discarded_low_score", "score": score, "min_score": min_score}

    # 6. Build TradingSignal
    if state.event_bus is None:
        raise HTTPException(status_code=503, detail="Event bus not initialised")

    signal = build_signal(payload, source_cfg)

    # 7. Publish
    await state.event_bus.publish(TradingSignalEvent(signal=signal))

    # 8. Record in signal history (shared with /api/signals endpoint)
    import dataclasses
    d = dataclasses.asdict(signal)
    d["created_at"] = signal.created_at.isoformat()
    state.signal_history.append(d)
    if len(state.signal_history) > 200:
        state.signal_history.pop(0)

    if state.audit:
        await state.audit.log(
            decision="SIGNAL",
            market_id=signal.market_id,
            ticker=signal.ticker,
            signal=signal,
        )

    _log_alert(state, {
        "received_at": recv_at,
        "strategy_id": strategy_id,
        "ticker":      ticker,
        "action":      payload.get("action"),
        "score":       score,
        "outcome":     "signal_published",
        "signal_id":   signal.signal_id,
    })

    logger.info("TV webhook → signal published: %s %s score=%.0f", strategy_id, ticker, score)
    return {"ok": True, "outcome": "signal_published", "signal_id": signal.signal_id}


@router.get("/log")
async def alert_log(limit: int = Query(default=20, le=100)):
    """Return the last N received webhook alerts with their processing outcome."""
    state  = get_state()
    alerts = list(reversed(state.alert_log))[:limit]
    return {"count": len(alerts), "alerts": alerts}


@router.get("/setup")
async def setup_helper(
    market: str = Query(default="us", description="Market to generate alerts for: 'us' or 'india'"),
    strategy_id: str = Query(default="my_strategy_v1", description="Pine Script strategy_id — metadata only"),
):
    """Return pre-filled webhook URL and alert message JSON for every watchlist ticker.

    Paste each alert_message directly into TradingView's alert Message field.
    The webhook_url goes into the Webhook URL field.
    """
    state      = get_state()
    secret     = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "<your-secret>")
    ngrok_host = os.getenv("NGROK_HOST", "<your-ngrok-subdomain>.ngrok-free.app")

    webhook_url = f"https://{ngrok_host}/api/webhooks/tradingview?secret={secret}"

    # Get tickers from watchlist cache; fall back to config files
    tickers = []
    watchlist_path = Path(__file__).parent.parent.parent / "config" / "watchlist" / f"{market}.yaml"
    if watchlist_path.exists():
        with open(watchlist_path) as f:
            wl = yaml.safe_load(f) or {}
        for src in wl.get("sources", []):
            if src.get("type") == "static":
                tickers.extend([t.upper() for t in src.get("tickers", [])])

    exchange_map = {"us": "NASDAQ", "india": "NSE"}
    exchange     = exchange_map.get(market, "NASDAQ")

    alerts = []
    for ticker in tickers:
        msg = (
            '{"strategy_id":"' + strategy_id + '"'
            + ',"ticker":"' + ticker + '"'
            + ',"exchange":"' + exchange + '"'
            + ',"action":"BUY"'
            + ',"close":{{close}}'
            + ',"volume":{{volume}}'
            + ',"score":{{plot_0}}'
            + ',"timeframe":"{{interval}}"'
            + ',"timestamp":"{{timenow}}"}'
        )
        alerts.append({
            "ticker":        ticker,
            "tv_symbol":     f"{exchange}:{ticker}",
            "alert_message": msg,
        })

    return {
        "webhook_url":    webhook_url,
        "market":         market,
        "strategy_id":    strategy_id,
        "ticker_count":   len(alerts),
        "note":           "Paste alert_message into TradingView alert Message field. Replace {{plot_0}} with your Pine Script composite variable name.",
        "alerts":         alerts,
    }
