"""GET /health — system health check."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter

from api.state import get_state

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    state = get_state()
    broker_statuses = {}
    for market_id, broker in state.brokers.items():
        try:
            ok = await asyncio.wait_for(broker.health_check(), timeout=5.0)
            broker_statuses[market_id] = "ok" if ok else "degraded"
        except Exception:
            broker_statuses[market_id] = "unreachable"

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "brokers": broker_statuses,
        "active_markets": list(state.brokers.keys()),
    }
