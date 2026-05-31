"""Runs historical filings through the strategy engine and records results."""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from infrastructure.event_bus.events import AnnouncementEvent, TradingSignal
from infrastructure.market_context.context import MarketContext
from infrastructure.config_registry.loader import ConfigRegistry
from infrastructure.watchlist.provider import WatchlistProvider
from agents.strategy_engine.base_strategy import BaseStrategy
from agents.strategy_engine.strategy_context import StrategyContext

logger = logging.getLogger(__name__)

# Max simultaneous Claude API calls — conservative to stay under rate limits.
# Cache hits don't consume API quota, so this only throttles first-run misses.
_CONCURRENCY = 4


@dataclass
class BacktestResult:
    event: AnnouncementEvent
    signal: TradingSignal | None
    price_at_filing: float | None = None
    price_5d_later: float | None = None
    price_10d_later: float | None = None
    price_20d_later: float | None = None
    actual_move_5d_pct: float | None = None
    actual_move_20d_pct: float | None = None
    error: str | None = None


async def run(
    events: list[AnnouncementEvent],
    strategies: list[BaseStrategy],
    market: MarketContext,
    config_registry: ConfigRegistry,
    watchlist: WatchlistProvider,
    fetch_prices: bool = True,
) -> list[BacktestResult]:
    context = StrategyContext(
        market=market,
        config_registry=config_registry,
        watchlist=watchlist,
    )

    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def _process(event: AnnouncementEvent) -> BacktestResult:
        async with semaphore:
            result = BacktestResult(event=event, signal=None)
            try:
                from agents.strategy_engine.data_feeds.feed_types import DataFeedType
                for strategy in strategies:
                    if DataFeedType.ANNOUNCEMENTS not in strategy.subscriptions:
                        continue
                    sig = await strategy.evaluate(event, context)
                    if sig is not None:
                        result.signal = sig
                        break
            except Exception as exc:
                logger.error("Backtester error for %s %s: %s", event.ticker, event.filing_id, exc)
                result.error = str(exc)

            if fetch_prices and result.signal:
                await _attach_prices(result, event)

            return result

    # asyncio.gather preserves order — results match events index-for-index
    results = list(await asyncio.gather(*[_process(e) for e in events]))
    return results


async def _attach_prices(result: BacktestResult, event: AnnouncementEvent) -> None:
    """Fetch actual price moves after the filing date using yfinance."""
    try:
        import yfinance as yf
        filing_date = event.published_at.date()
        end_date = filing_date + timedelta(days=30)

        loop = asyncio.get_event_loop()
        hist = await loop.run_in_executor(
            None,
            lambda: yf.download(
                event.ticker,
                start=filing_date,
                end=end_date,
                progress=False,
                auto_adjust=True,
            ),
        )

        if hist.empty:
            return

        closes = hist["Close"].squeeze()
        dates = list(closes.index)

        def _price_after(n_days: int) -> float | None:
            target = filing_date + timedelta(days=n_days)
            future = [d for d in dates if d.date() >= target]
            if not future:
                return None
            return float(closes.loc[future[0]])

        p0 = float(closes.iloc[0]) if not closes.empty else None
        p5 = _price_after(5)
        p20 = _price_after(20)

        result.price_at_filing = p0
        result.price_5d_later  = p5
        result.price_20d_later = p20

        if p0 and p0 > 0:
            if p5:
                result.actual_move_5d_pct  = round((p5 - p0) / p0 * 100, 2)
            if p20:
                result.actual_move_20d_pct = round((p20 - p0) / p0 * 100, 2)

    except Exception as exc:
        logger.debug("Price fetch failed for %s: %s", event.ticker, exc)
