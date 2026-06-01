"""GET /api/signals — signal history with dispositions."""
from __future__ import annotations

from fastapi import APIRouter, Query

from api.state import get_state

router = APIRouter(prefix="/api/signals", tags=["signals"])


@router.get("")
async def list_signals(
    market: str | None = Query(None, description="Filter by market_id: us | india"),
    limit: int = Query(100, le=500),
):
    state = get_state()
    if state.audit is not None:
        signals = await state.audit.get_signal_history(market_id=market, limit=limit)
        return {"signals": signals}
    # Fallback: in-memory, no disposition data
    signals = state.signal_history
    if market:
        signals = [s for s in signals if s.get("market_id") == market]
    return {"signals": list(reversed(signals[-limit:]))}
