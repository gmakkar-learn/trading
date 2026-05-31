"""GET /api/watchlist — ticker lists per market."""
from __future__ import annotations

from fastapi import APIRouter
from pathlib import Path

import yaml

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

_CONFIG_DIR = Path(__file__).parent.parent.parent / "config" / "watchlist"


@router.get("")
async def list_watchlist():
    result = {}
    for path in sorted(_CONFIG_DIR.glob("*.yaml")):
        market_id = path.stem
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            result[market_id] = data.get("tickers", [])
        except Exception:
            result[market_id] = []
    return {"watchlist": result}
