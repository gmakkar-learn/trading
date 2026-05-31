"""GET /api/positions — live positions and holdings across all markets."""
from __future__ import annotations

from fastapi import APIRouter

from api.state import get_state

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("")
async def list_positions():
    state = get_state()
    all_positions = []
    all_holdings = []

    for market_id, broker in state.brokers.items():
        try:
            positions = await broker.get_positions()
            all_positions.extend([
                {
                    "ticker": p.ticker,
                    "market_id": p.market_id,
                    "quantity": p.quantity,
                    "average_price": p.average_price,
                    "current_price": p.current_price,
                    "unrealised_pnl": p.unrealised_pnl,
                    "currency": p.currency,
                }
                for p in positions
            ])
        except Exception:
            pass

        try:
            holdings = await broker.get_holdings()
            all_holdings.extend([
                {
                    "ticker": h.ticker,
                    "market_id": h.market_id,
                    "quantity": h.quantity,
                    "average_price": h.average_price,
                    "current_price": h.current_price,
                    "currency": h.currency,
                }
                for h in holdings
            ])
        except Exception:
            pass

    return {"positions": all_positions, "holdings": all_holdings}
