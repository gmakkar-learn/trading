from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from infrastructure.market_context.context import MarketContext
from infrastructure.config_registry.loader import ConfigRegistry
from infrastructure.watchlist.provider import WatchlistProvider

if TYPE_CHECKING:
    from agents.strategy_engine.signal_cache import SignalCache


@dataclass
class StrategyContext:
    market: MarketContext
    config_registry: ConfigRegistry
    watchlist: WatchlistProvider
    signal_cache: SignalCache | None = None
