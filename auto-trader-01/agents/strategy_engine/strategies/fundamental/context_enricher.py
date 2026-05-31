"""Enriches signals with current price context from yfinance."""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def enrich(ticker: str) -> dict:
    """Fetch 52-week range and current price to add market context to the signal."""
    try:
        import yfinance as yf
        loop = asyncio.get_event_loop()
        fast = await loop.run_in_executor(None, lambda: yf.Ticker(ticker).fast_info)
        return {
            "current_price": getattr(fast, "last_price", None),
            "week_52_high":  getattr(fast, "year_high", None),
            "week_52_low":   getattr(fast, "year_low", None),
        }
    except Exception as exc:
        logger.debug("Context enrichment failed for %s: %s", ticker, exc)
        return {}
