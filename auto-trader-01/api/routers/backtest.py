"""Backtest results router — serves pre-computed backtest JSON from disk."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

_DATA_FILE = Path(__file__).parent.parent.parent / "data" / "backtest_india.json"


@router.get("/india")
async def get_india_backtest() -> dict:
    if not _DATA_FILE.exists():
        raise HTTPException(
            status_code=404,
            detail="Backtest data not found. Run: uv run python scripts/backtest_india.py --export data/backtest_india.json",
        )
    with open(_DATA_FILE) as f:
        return json.load(f)
