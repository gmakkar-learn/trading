"""Shared pytest fixtures."""
from __future__ import annotations
import pytest
from datetime import datetime, timezone
from pathlib import Path

from infrastructure.event_bus.events import AnnouncementEvent
from infrastructure.market_context.loader import load_market_context
from infrastructure.config_registry.loader import ConfigRegistry
from infrastructure.watchlist.provider import WatchlistProvider
from agents.strategy_engine.strategy_context import StrategyContext


@pytest.fixture
def config_dir() -> Path:
    return Path(__file__).parent.parent / "config"


@pytest.fixture
def config_registry(config_dir: Path) -> ConfigRegistry:
    return ConfigRegistry(config_dir)


@pytest.fixture
def market_context(config_dir: Path):
    return load_market_context("us", config_dir)


@pytest.fixture
def watchlist_provider(config_dir: Path) -> WatchlistProvider:
    return WatchlistProvider(config_dir)


@pytest.fixture
def strategy_context(market_context, config_registry, watchlist_provider) -> StrategyContext:
    return StrategyContext(
        market=market_context,
        config_registry=config_registry,
        watchlist=watchlist_provider,
    )


@pytest.fixture
def sample_announcement_event() -> AnnouncementEvent:
    return AnnouncementEvent(
        market_id="us",
        ticker="AAPL",
        filing_id="0000320193-24-000123",
        filing_type="8-K",
        filing_url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm",
        document_url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240201.htm",
        items=["2.02", "9.01"],
        published_at=datetime(2024, 2, 1, 21, 30, tzinfo=timezone.utc),
    )
