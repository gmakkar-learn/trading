"""Starts only the feeds that active strategies have declared subscriptions for."""
import logging

from infrastructure.event_bus.bus import EventBus
from infrastructure.watchlist.provider import WatchlistProvider
from ..base_strategy import BaseStrategy
from .feed_types import DataFeedType

logger = logging.getLogger(__name__)


class FeedManager:
    def __init__(
        self,
        strategies: list[BaseStrategy],
        watchlist: WatchlistProvider,
        event_bus: EventBus,
        market_id: str,
    ) -> None:
        self._strategies = strategies
        self._watchlist = watchlist
        self._bus = event_bus
        self._market_id = market_id
        self._needed: set[DataFeedType] = set()
        for s in strategies:
            self._needed.update(s.subscriptions)
        logger.info("FeedManager needs feeds: %s", [f.name for f in self._needed])

    def needs(self, feed_type: DataFeedType) -> bool:
        return feed_type in self._needed

    async def get_tickers(self) -> list[str]:
        return await self._watchlist.get_tickers(self._market_id)
