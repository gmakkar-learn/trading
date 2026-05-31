"""Fetches historical SEC 8-K filings for backtesting. Supports 'lastN' and date-range modes."""
from __future__ import annotations
import logging
from datetime import date, timedelta

from agents.strategy_engine.data_feeds.announcement_feed import SecEdgarFeed
from infrastructure.event_bus.events import AnnouncementEvent

logger = logging.getLogger(__name__)

# US fiscal quarters end roughly at these month boundaries
_QUARTER_ENDS = [
    (3, 31),   # Q1
    (6, 30),   # Q2
    (9, 30),   # Q3
    (12, 31),  # Q4
]


def _last_n_quarter_start(n: int) -> date:
    """Return the start date that covers at least the last n completed earnings seasons."""
    today = date.today()
    # Earnings releases typically come 4–8 weeks after quarter end.
    # Go back n*3 months + 8 weeks buffer to catch all filings.
    months_back = n * 3 + 2
    year = today.year
    month = today.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


async def load_filings(
    tickers: list[str],
    quarters: str = "last3",
) -> list[AnnouncementEvent]:
    """
    Load historical 8-K (item 2.02) filings.

    quarters:
      "last3"  — last 3 completed quarters (default)
      "lastN"  — last N quarters (e.g. "last5")
      "YYYY-MM-DD:YYYY-MM-DD" — explicit date range
    """
    since = _resolve_since(quarters)
    logger.info("Loading historical filings since %s for %d tickers", since, len(tickers))

    feed = SecEdgarFeed()
    events: list[AnnouncementEvent] = []

    async for event in feed.stream_events(tickers, since=since):
        events.append(event)
        logger.debug("Loaded filing: %s %s %s", event.ticker, event.filing_id, event.published_at.date())

    logger.info("Loaded %d filings total", len(events))
    # Sort oldest-first so backtester processes in chronological order
    events.sort(key=lambda e: e.published_at)
    return events


def _resolve_since(quarters: str) -> date:
    if quarters.startswith("last"):
        try:
            n = int(quarters[4:])
        except ValueError:
            n = 3
        return _last_n_quarter_start(n)
    if ":" in quarters:
        start_str, _ = quarters.split(":", 1)
        return date.fromisoformat(start_str)
    # Default
    return _last_n_quarter_start(3)
