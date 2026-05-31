"""Tests for WatchlistProvider."""
from __future__ import annotations
import pytest
from pathlib import Path

from infrastructure.watchlist.provider import WatchlistProvider


@pytest.mark.asyncio
async def test_get_us_tickers(config_dir: Path):
    provider = WatchlistProvider(config_dir)
    tickers = await provider.get_tickers("us")
    assert isinstance(tickers, list)
    assert len(tickers) > 0
    assert all(isinstance(t, str) for t in tickers)
    # Sanity: known tickers from config/watchlist/us.yaml
    assert "AAPL" in tickers
    assert "MSFT" in tickers


@pytest.mark.asyncio
async def test_unknown_market_returns_empty(config_dir: Path):
    provider = WatchlistProvider(config_dir)
    tickers = await provider.get_tickers("nonexistent_market")
    assert tickers == []
