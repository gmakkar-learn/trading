"""POST /api/debug/* — development-only endpoints for manual pipeline testing.

These endpoints allow injecting synthetic signals into the live pipeline
without waiting for the scheduler or market hours. Remove or gate behind
an env flag before deploying to production.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.state import get_state
from infrastructure.event_bus.events import (
    ApprovalRequest, OrderProposal, OrderProposalEvent, TradingSignal, TradingSignalEvent,
)

router = APIRouter(prefix="/api/debug", tags=["debug"])


class TestSignalRequest(BaseModel):
    ticker: str = "AAPL"
    market_id: str = "us"
    composite_score: float = 75.0
    recommended_action: str = "BUY"
    confidence: str = "high"
    rationale: str = "Debug signal — manual pipeline test"
    skip_session_check: bool = True


@router.post("/push-signal")
async def push_signal(req: TestSignalRequest):
    """Inject a synthetic TradingSignal directly into the event bus.

    skip_session_check=True bypasses market hours (useful outside trading hours).
    The signal still goes through RiskGuard → Trader → Telegram.
    """
    state = get_state()
    bus = state.event_bus
    if bus is None:
        raise HTTPException(status_code=503, detail="Event bus not initialised")

    signal = TradingSignal(
        ticker=req.ticker,
        market_id=req.market_id,
        strategy_type="fundamental",
        strategy_id="fundamental_v1",
        strategy_version="1",
        composite_score=req.composite_score,
        recommended_action=req.recommended_action,
        confidence=req.confidence,
        rationale=req.rationale,
        context={"last_price": 0.0, "debug": True},
        created_at=datetime.now(timezone.utc),
    )

    if req.skip_session_check:
        # Bypass RiskGuard — go straight to Trader via OrderProposalEvent
        proposal = OrderProposal(
            signal_id=signal.signal_id,
            ticker=signal.ticker,
            market_id=signal.market_id,
            side="BUY",
            quantity=1,
            order_type="MARKET",
            limit_price=0.0,
            stoploss=0.0,
            target=0.0,
            composite_score=signal.composite_score,
            rationale=signal.rationale,
        )
        await bus.publish(OrderProposalEvent(proposal=proposal))
        path = "direct→Trader (session check skipped)"
    else:
        # Full path through RiskGuard (will reject if market is closed)
        await bus.publish(TradingSignalEvent(signal=signal))
        path = "full pipeline via RiskGuard"

    # Record in signal history and persist to DB
    import dataclasses
    d = dataclasses.asdict(signal)
    d["created_at"] = signal.created_at.isoformat()
    state.signal_history.append(d)
    if state.audit is not None:
        await state.audit.log(
            decision="SIGNAL",
            market_id=signal.market_id,
            ticker=signal.ticker,
            signal=signal,
        )

    return {
        "ok": True,
        "signal_id": signal.signal_id,
        "path": path,
        "note": "Check Telegram for approval request or auto-execution notice",
    }


@router.post("/trigger-poll")
async def trigger_poll(market_id: str = "us"):
    """Immediately run one poll cycle for the given market.

    Fetches new SEC filings, runs the strategy, and publishes any signals
    to the event bus (which then flows through RiskGuard → Trader → Telegram).
    """
    state = get_state()
    engine = state.engines.get(market_id)
    feed = state.feeds.get(market_id)
    if engine is None or feed is None:
        raise HTTPException(status_code=404, detail=f"No pipeline for market: {market_id}")

    # Run poll in background so the HTTP response returns immediately
    asyncio.create_task(_run_poll(market_id))
    return {"ok": True, "market_id": market_id, "note": "Poll started in background — check logs and Telegram"}


async def _run_poll(market_id: str) -> None:
    from api.main import _poll_market
    await _poll_market(market_id)


@router.post("/trigger-candles")
async def trigger_candles(market_id: str = "us"):
    """Immediately run one candle poll cycle for the given market.

    Fetches daily OHLCV history for all watchlist tickers, runs TechnicalStrategy,
    and publishes any signals to the event bus.
    """
    state = get_state()
    engine = state.engines.get(market_id)
    feed = state.candle_feeds.get(market_id)
    if engine is None or feed is None:
        raise HTTPException(status_code=404, detail=f"No candle feed for market: {market_id}")

    asyncio.create_task(_run_candles(market_id))
    return {"ok": True, "market_id": market_id, "note": "Candle poll started in background — check logs"}


async def _run_candles(market_id: str) -> None:
    from api.main import _poll_candles
    await _poll_candles(market_id)


@router.post("/send-telegram-test")
async def send_telegram_test():
    """Send a formatted sample signal alert directly to Telegram."""
    state = get_state()
    if state.telegram_sender is None:
        raise HTTPException(status_code=503, detail="Telegram sender not initialised")

    req = ApprovalRequest(
        proposal_id="debug-001",
        ticker="AAPL",
        market_id="us",
        side="BUY",
        quantity=10,
        limit_price=189.50,
        composite_score=76.5,
        rationale=(
            "Q2 FY2026: Revenue +5.1% YoY ($95.4B vs est $94.5B). "
            "EPS $1.65 beat by $0.08. Services segment grew 14% YoY. "
            "Guidance maintained. Margins expanding. No exceptional items."
        ),
        expires_at=datetime.now(timezone.utc).replace(second=0, microsecond=0),
    )
    # Override expires_at to 30min from now
    from datetime import timedelta
    req.expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)

    await state.telegram_sender.send_approval_request(req)
    return {"ok": True, "note": "Approval request sent to Telegram — check your bot"}
