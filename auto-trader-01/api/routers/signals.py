"""GET /api/signals — recent trading signals."""
from __future__ import annotations

from fastapi import APIRouter, Query

from api.state import get_state

router = APIRouter(prefix="/api/signals", tags=["signals"])


@router.get("")
async def list_signals(
    market: str | None = Query(None, description="Filter by market_id: us | india"),
    action: str | None = Query(None, description="Filter by action: BUY | HOLD | SELL"),
    limit: int = Query(50, le=200),
):
    signals = get_state().signal_history
    if market:
        signals = [s for s in signals if s.get("market_id") == market]
    if action:
        signals = [s for s in signals if s.get("recommended_action") == action.upper()]
    return {"signals": signals[-limit:]}
