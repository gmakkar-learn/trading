"""StrategyEngine: routes DataEvents to registered strategies, emits TradingSignals."""
import asyncio
import logging

from infrastructure.event_bus.bus import EventBus
from infrastructure.event_bus.events import (
    AnnouncementEvent,
    CandleEvent,
    DataEvent,
    TradingSignal,
    TradingSignalEvent,
)
from infrastructure.audit.audit_logger import AuditLogger
from .base_strategy import BaseStrategy
from .data_feeds.feed_types import DataFeedType
from .strategy_context import StrategyContext
from .signal_cache import SignalCache

logger = logging.getLogger(__name__)


class StrategyEngine:
    def __init__(
        self,
        strategies: list[BaseStrategy],
        context: StrategyContext,
        event_bus: EventBus,
        audit_logger: AuditLogger,
    ) -> None:
        self._strategies = strategies
        self._context = context
        self._bus = event_bus
        self._audit = audit_logger
        self._signal_cache = SignalCache()
        self._context.signal_cache = self._signal_cache

    def _strategies_for(self, feed_type: DataFeedType) -> list[BaseStrategy]:
        return [s for s in self._strategies if feed_type in s.subscriptions]

    async def handle_announcement(self, event: AnnouncementEvent) -> None:
        await self._route(event, DataFeedType.ANNOUNCEMENTS)

    async def handle_candle(self, event: CandleEvent) -> None:
        await self._route(event, DataFeedType.OHLCV_CANDLES)

    async def _route(self, event: DataEvent, feed_type: DataFeedType) -> None:
        relevant = self._strategies_for(feed_type)
        if not relevant:
            return
        results = await asyncio.gather(
            *[self._safe_evaluate(s, event) for s in relevant],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error("Strategy evaluation raised: %s", result)
            elif result is not None:
                await self._emit(result)

    async def _safe_evaluate(self, strategy: BaseStrategy, event: DataEvent) -> TradingSignal | None:
        try:
            return await strategy.evaluate(event, self._context)
        except Exception as exc:
            logger.error("Strategy %s raised: %s", strategy.strategy_id, exc, exc_info=True)
            return None

    async def _emit(self, signal: TradingSignal) -> None:
        self._signal_cache.put(signal)
        await self._bus.publish(TradingSignalEvent(signal=signal))
        logger.info(
            "Signal emitted: %s %s score=%.1f action=%s confidence=%s",
            signal.ticker, signal.strategy_id,
            signal.composite_score, signal.recommended_action, signal.confidence,
        )
