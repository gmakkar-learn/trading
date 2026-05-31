"""India announcement feed — polls BSE and NSE for corporate results filings.

Uses two free, official sources:
- BSE Corp Results API: https://api.bseindia.com/BseIndiaAPI/api/Results/w
- NSE EQ results: https://www.nseindia.com/api/corporates-financial-results

Emits AnnouncementEvent for each quarterly result filing found.
Documents (PDF/HTML) are fetched and passed through to the strategy engine.

Rate limits: no official published limits. Applies conservative 1s delay between
requests and a 5-request semaphore to avoid hitting informal limits.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx

from agents.strategy_engine.data_feeds.feed_types import DataFeedType
from infrastructure.event_bus.events import AnnouncementEvent

logger = logging.getLogger(__name__)

_BSE_BASE = "https://api.bseindia.com/BseIndiaAPI/api"
_NSE_BASE = "https://www.nseindia.com"
_REQUEST_DELAY = 1.0       # seconds between requests
_MAX_CONCURRENT = 3

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; auto-trader-01/1.0)",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

_BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; auto-trader-01/1.0)",
    "Accept": "application/json",
}


class IndiaAnnouncementFeed:
    """Polls BSE and NSE for new quarterly result filings.

    Yields AnnouncementEvent objects for new filings not seen since last poll.
    """

    feed_type = DataFeedType.ANNOUNCEMENTS

    def __init__(self, tickers: list[str], lookback_days: int = 7):
        self._tickers = tickers
        self._lookback_days = lookback_days
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        self._seen_ids: set[str] = set()

    async def stream_events(self) -> AsyncIterator[AnnouncementEvent]:
        """Poll both NSE and BSE for results; yield new ones."""
        since = datetime.now(timezone.utc) - timedelta(days=self._lookback_days)

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # NSE session cookie (required for API access)
            try:
                await client.get(_NSE_BASE, headers=_NSE_HEADERS)
            except Exception as exc:
                logger.warning("NSE session init failed: %s", exc)

            for ticker in self._tickers:
                events = []
                try:
                    nse_events = await self._fetch_nse(ticker, since, client)
                    events.extend(nse_events)
                except Exception as exc:
                    logger.warning("NSE fetch failed for %s: %s", ticker, exc)

                try:
                    bse_events = await self._fetch_bse(ticker, since, client)
                    events.extend(bse_events)
                except Exception as exc:
                    logger.warning("BSE fetch failed for %s: %s", ticker, exc)

                for event in events:
                    if event.filing_id not in self._seen_ids:
                        self._seen_ids.add(event.filing_id)
                        yield event

                await asyncio.sleep(_REQUEST_DELAY)

    async def _fetch_nse(
        self, ticker: str, since: datetime, client: httpx.AsyncClient
    ) -> list[AnnouncementEvent]:
        """Fetch financial results from NSE corporate results API."""
        url = f"{_NSE_BASE}/api/corporates-financial-results"
        params = {"index": "equities", "symbol": ticker, "period": "Quarterly"}

        async with self._semaphore:
            resp = await client.get(url, params=params, headers=_NSE_HEADERS)

        if resp.status_code != 200:
            logger.debug("NSE returned %d for %s", resp.status_code, ticker)
            return []

        try:
            data = resp.json()
        except Exception:
            return []

        events = []
        for item in data if isinstance(data, list) else []:
            try:
                filed_str = item.get("xbrl", "") or item.get("consolidated", {}).get("dtofFiling", "")
                filed_at = _parse_nse_date(item.get("resDt", ""))
                if filed_at and filed_at < since:
                    continue

                # Build a stable filing_id from ticker + period + source
                period = item.get("period", "")
                filing_id = _stable_id(f"nse_{ticker}_{period}")

                doc_url = self._nse_doc_url(item)
                events.append(AnnouncementEvent(
                    market_id="india",
                    ticker=ticker,
                    exchange="NSE",
                    filing_id=filing_id,
                    filing_type="FinancialResults",
                    filing_url=f"{_NSE_BASE}/api/corporates-financial-results?symbol={ticker}",
                    document_url=doc_url,
                    items=["FinancialResults"],
                    published_at=filed_at or datetime.now(timezone.utc),
                ))
            except Exception as exc:
                logger.debug("NSE item parse error for %s: %s", ticker, exc)

        return events

    async def _fetch_bse(
        self, ticker: str, since: datetime, client: httpx.AsyncClient
    ) -> list[AnnouncementEvent]:
        """Fetch financial results from BSE Corp Results API."""
        # BSE uses scrip codes, not symbols — try a symbol→code lookup
        scrip_code = await self._bse_scrip_code(ticker, client)
        if not scrip_code:
            logger.debug("No BSE scrip code found for %s", ticker)
            return []

        url = f"{_BSE_BASE}/Results/w"
        params = {"scripcode": scrip_code, "segment": "C", "type": "Q"}

        async with self._semaphore:
            resp = await client.get(url, params=params, headers=_BSE_HEADERS)

        if resp.status_code != 200:
            return []

        try:
            data = resp.json()
        except Exception:
            return []

        results = data.get("Table", [])
        events = []
        for item in results:
            try:
                news_dt = item.get("NEWSDATE", "")
                filed_at = _parse_bse_date(news_dt)
                if filed_at and filed_at < since:
                    continue

                quarter = item.get("PERIOD", "")
                filing_id = _stable_id(f"bse_{ticker}_{quarter}_{news_dt}")

                attachment = item.get("ATTACHMENTNAME", "")
                doc_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{attachment}" if attachment else ""

                events.append(AnnouncementEvent(
                    market_id="india",
                    ticker=ticker,
                    exchange="BSE",
                    filing_id=filing_id,
                    filing_type="FinancialResults",
                    filing_url=f"https://www.bseindia.com/corporates/ann.html?scrip={scrip_code}",
                    document_url=doc_url,
                    items=["FinancialResults"],
                    published_at=filed_at or datetime.now(timezone.utc),
                ))
            except Exception as exc:
                logger.debug("BSE item parse error for %s: %s", ticker, exc)

        return events

    async def _bse_scrip_code(self, ticker: str, client: httpx.AsyncClient) -> str | None:
        """Look up BSE scrip code by NSE symbol via BSE search API."""
        url = f"{_BSE_BASE}/getScripHeaderData/w"
        params = {"Debtflag": "", "scripcode": "", "seriesid": ""}
        # BSE search by company name
        search_url = f"https://api.bseindia.com/BseIndiaAPI/api/fetchcomp/w"
        try:
            async with self._semaphore:
                resp = await client.get(
                    search_url,
                    params={"mktcap": "", "industry": "", "sector": ""},
                    headers=_BSE_HEADERS,
                )
            # This endpoint returns all companies; filter by symbol
            for co in resp.json() if resp.status_code == 200 else []:
                if co.get("SCRIP_ID") == ticker or co.get("short_name", "").upper() == ticker.upper():
                    return str(co.get("SCRIP_CD", ""))
        except Exception as exc:
            logger.debug("BSE scrip lookup failed for %s: %s", ticker, exc)
        return None

    @staticmethod
    def _nse_doc_url(item: dict) -> str:
        """Extract the best document URL from an NSE result item."""
        # NSE results items may have a PDF link field
        for key in ("xbrl", "na_dt", "pdf"):
            val = item.get(key, "")
            if val and val.startswith("http"):
                return val
        return ""


def _parse_nse_date(s: str) -> datetime | None:
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    return None


def _parse_bse_date(s: str) -> datetime | None:
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    return None


def _stable_id(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]
