"""POST /api/orders — manual order entry; GET /api/orders for recent orders."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.state import get_state
from infrastructure.event_bus.events import Order

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("")
async def list_orders(market_id: str = "us", status: str = "all"):
    """Return orders from the broker. status: open | closed | all."""
    state = get_state()
    broker = state.brokers.get(market_id)
    if broker is None:
        raise HTTPException(status_code=404, detail=f"No broker for market: {market_id}")
    orders = await broker.get_orders(status=status)
    return {
        "orders": [
            {
                "broker_order_id": o.broker_order_id,
                "ticker": o.ticker,
                "side": o.side,
                "quantity": o.quantity,
                "order_type": o.order_type,
                "status": o.status,
                "limit_price": o.limit_price,
                "fill_price": o.fill_price,
                "filled_qty": o.filled_qty,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in orders
        ]
    }


class ManualOrderRequest(BaseModel):
    ticker: str
    market_id: str
    side: str           # "BUY" | "SELL"
    quantity: int
    order_type: str = "LIMIT"
    limit_price: float = 0.0


@router.post("")
async def place_manual_order(req: ManualOrderRequest):
    state = get_state()
    broker = state.brokers.get(req.market_id)
    if broker is None:
        raise HTTPException(status_code=404, detail=f"No broker for market: {req.market_id}")

    product_type = "CNC" if req.market_id == "india" else "DAY"
    order = Order(
        order_id=str(uuid.uuid4()),
        proposal_id="manual",
        signal_id="manual",
        ticker=req.ticker,
        market_id=req.market_id,
        side=req.side.upper(),
        quantity=req.quantity,
        order_type=req.order_type.upper(),
        limit_price=req.limit_price,
        product_type=product_type,
    )

    result = await broker.place_order(order)
    return {
        "order_id": result.order_id,
        "broker_order_id": result.broker_order_id,
        "status": result.status,
        "message": result.message,
    }
