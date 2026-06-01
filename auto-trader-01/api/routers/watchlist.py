"""Watchlist API — read and live-edit the per-market ticker lists."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.state import get_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

_CONFIG_DIR = Path(__file__).parent.parent.parent / "config" / "watchlist"


def _extract_static_tickers(data: dict) -> list[str]:
    """Return tickers from the first static source (or legacy flat format)."""
    if "tickers" in data:
        return data["tickers"]
    tickers: list[str] = []
    for src in data.get("sources", []):
        if src.get("type") == "static":
            tickers.extend(src.get("tickers", []))
    return tickers


@router.get("")
async def list_watchlist():
    """Return per-market ticker lists as currently loaded in the provider cache."""
    state = get_state()
    if state.watchlist_provider is not None:
        result = {
            mid: list(state.watchlist_provider._cache.get(mid, []))
            for mid in state.watchlist_provider._cache
        }
        return {"watchlist": result}
    # Fallback: read YAML directly (provider not ready yet)
    result = {}
    for path in sorted(_CONFIG_DIR.glob("*.yaml")):
        market_id = path.stem
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            result[market_id] = _extract_static_tickers(data)
        except Exception:
            result[market_id] = []
    return {"watchlist": result}


class TickerBody(BaseModel):
    ticker: str


@router.post("/{market_id}")
async def add_ticker(market_id: str, body: TickerBody):
    """Add a ticker to the watchlist and hot-reload the running feeds."""
    state = get_state()
    ticker = body.ticker.strip().upper()
    if not ticker:
        raise HTTPException(400, "ticker is required")

    provider = state.watchlist_provider
    if provider is None:
        raise HTTPException(503, "watchlist provider not initialised")

    added = await provider.add_ticker(market_id, ticker)
    if not added:
        raise HTTPException(409, f"{ticker} already in {market_id} watchlist")

    # Propagate to live feeds so the next poll includes the new ticker
    _sync_feeds(state, market_id)

    # Immediately scan the new ticker without waiting for the next scheduled poll
    asyncio.create_task(_immediate_check(market_id, ticker))

    return {"ok": True, "ticker": ticker, "market_id": market_id,
            "scanning": True,
            "watchlist": state.watchlist_provider._cache.get(market_id, [])}


@router.delete("/{market_id}/{ticker}")
async def remove_ticker(market_id: str, ticker: str):
    """Remove a ticker from the watchlist and hot-reload the running feeds."""
    state = get_state()
    ticker = ticker.strip().upper()

    provider = state.watchlist_provider
    if provider is None:
        raise HTTPException(503, "watchlist provider not initialised")

    removed = await provider.remove_ticker(market_id, ticker)
    if not removed:
        raise HTTPException(404, f"{ticker} not found in {market_id} watchlist")

    _sync_feeds(state, market_id)

    return {"ok": True, "ticker": ticker, "market_id": market_id,
            "watchlist": state.watchlist_provider._cache.get(market_id, [])}


def _sync_feeds(state, market_id: str) -> None:
    """Push updated ticker list to the live feed objects so next poll picks it up."""
    tickers = state.watchlist_provider._cache.get(market_id, [])
    feed = state.feeds.get(market_id)
    if feed is not None and hasattr(feed, "_tickers"):
        feed._tickers = list(tickers)
    candle_feed = state.candle_feeds.get(market_id)
    if candle_feed is not None and hasattr(candle_feed, "_tickers"):
        candle_feed._tickers = list(tickers)


async def _immediate_check(market_id: str, ticker: str) -> None:
    """Run announcement + candle scan for a single ticker immediately."""
    state = get_state()
    engine = state.engines.get(market_id)
    feed = state.feeds.get(market_id)
    candle_feed = state.candle_feeds.get(market_id)

    logger.info("Immediate check triggered for %s on %s", ticker, market_id)

    if engine and feed:
        try:
            async for event in feed.stream_events(tickers=[ticker]):
                await engine.handle_announcement(event)
        except Exception as exc:
            logger.error("Immediate announcement check failed for %s: %s", ticker, exc)

    if engine and candle_feed:
        try:
            async for event in candle_feed.stream_events(tickers=[ticker]):
                await engine.handle_candle(event)
        except Exception as exc:
            logger.error("Immediate candle check failed for %s: %s", ticker, exc)
