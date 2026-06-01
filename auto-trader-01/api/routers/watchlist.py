"""GET /api/watchlist — ticker lists per market."""
from __future__ import annotations

from fastapi import APIRouter
from pathlib import Path

import yaml

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

_CONFIG_DIR = Path(__file__).parent.parent.parent / "config" / "watchlist"


def _extract_tickers(data: dict) -> list[str]:
    """Support both legacy flat 'tickers:' and current 'sources:' format."""
    if "tickers" in data:
        return data["tickers"]
    tickers: list[str] = []
    for src in data.get("sources", []):
        if src.get("type") == "static":
            tickers.extend(src.get("tickers", []))
    return tickers


@router.get("")
async def list_watchlist():
    result = {}
    for path in sorted(_CONFIG_DIR.glob("*.yaml")):
        market_id = path.stem
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            result[market_id] = _extract_tickers(data)
        except Exception:
            result[market_id] = []
    return {"watchlist": result}
