"""GET /health — system health check."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter

from api.state import get_state

router = APIRouter(tags=["health"])


async def _check_database(state) -> tuple[str, str]:
    if state.audit is None:
        return "degraded", "audit logger not initialised"
    try:
        from sqlalchemy import text
        async with state.audit._session_factory() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=3.0)
        return "ok", ""
    except Exception as exc:
        return "error", str(exc)


async def _check_telegram(state) -> tuple[str, str]:
    if state.telegram_sender is None:
        return "degraded", "not configured"
    try:
        url = f"{state.telegram_sender._base}/getMe"
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            if r.status_code == 200 and r.json().get("ok"):
                return "ok", ""
            return "error", f"HTTP {r.status_code}"
    except Exception as exc:
        return "error", str(exc)


async def _check_broker(market_id: str, broker) -> tuple[str, str]:
    try:
        ok = await asyncio.wait_for(broker.health_check(), timeout=5.0)
        return ("ok", "") if ok else ("degraded", "health_check returned false")
    except Exception as exc:
        return "error", str(exc)


@router.get("/health")
async def health():
    state = get_state()

    db_task       = asyncio.create_task(_check_database(state))
    telegram_task = asyncio.create_task(_check_telegram(state))
    broker_tasks  = {
        mid: asyncio.create_task(_check_broker(mid, broker))
        for mid, broker in state.brokers.items()
    }

    db_status, db_detail           = await db_task
    telegram_status, telegram_detail = await telegram_task
    broker_results = {mid: await t for mid, t in broker_tasks.items()}

    services: dict[str, dict] = {
        "database": {"status": db_status, "detail": db_detail},
        "telegram": {"status": telegram_status, "detail": telegram_detail},
    }
    for mid, (status, detail) in broker_results.items():
        services[f"broker_{mid}"] = {"status": status, "detail": detail}

    statuses = [v["status"] for v in services.values()]
    if all(s == "ok" for s in statuses):
        overall = "ok"
    elif any(s == "error" for s in statuses):
        overall = "degraded"
    else:
        overall = "degraded"

    return {
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": services,
        # keep legacy fields so existing callers don't break
        "brokers": {mid: v["status"] for mid, v in services.items() if mid.startswith("broker_")},
        "active_markets": list(state.brokers.keys()),
    }
