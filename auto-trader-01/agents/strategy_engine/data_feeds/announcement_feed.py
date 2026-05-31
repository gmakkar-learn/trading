"""SEC EDGAR announcement feed for US markets.
Polls for 8-K filings with item 2.02 (Results of Operations) for watchlisted tickers."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime
from typing import AsyncIterator

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from infrastructure.event_bus.events import AnnouncementEvent

logger = logging.getLogger(__name__)

EDGAR_BASE = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class SecEdgarFeed:
    """Fetches 8-K filings with item 2.02 from SEC EDGAR for a list of tickers."""

    def __init__(self, user_agent: str | None = None) -> None:
        self._user_agent = user_agent or os.environ.get(
            "SEC_EDGAR_USER_AGENT", "auto-trader-01 contact@example.com"
        )
        self._edgar_headers = {
            "User-Agent": self._user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov",
        }
        self._sec_headers = {
            "User-Agent": self._user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Host": "www.sec.gov",
        }
        self._ticker_to_cik: dict[str, str] = {}
        # Stay comfortably under SEC's 10 req/s limit
        self._semaphore = asyncio.Semaphore(5)

    # ── Internal HTTP ──────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    async def _get_json(self, client: httpx.AsyncClient, url: str, *, sec_host: bool = False) -> dict:
        headers = self._sec_headers if sec_host else self._edgar_headers
        async with self._semaphore:
            resp = await client.get(url, headers=headers, timeout=30.0)
            resp.raise_for_status()
            return resp.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    async def _get_text(self, client: httpx.AsyncClient, url: str) -> str:
        async with self._semaphore:
            resp = await client.get(url, headers=self._sec_headers, timeout=30.0, follow_redirects=True)
            resp.raise_for_status()
            return resp.text

    # ── CIK lookup ────────────────────────────────────────────────────────────

    async def _ensure_ticker_map(self, client: httpx.AsyncClient) -> None:
        if self._ticker_to_cik:
            return
        data = await self._get_json(client, TICKERS_URL, sec_host=True)
        for entry in data.values():
            ticker = str(entry["ticker"]).upper()
            cik = str(entry["cik_str"]).zfill(10)
            self._ticker_to_cik[ticker] = cik
        logger.info("Loaded %d tickers from SEC EDGAR", len(self._ticker_to_cik))

    def cik_for(self, ticker: str) -> str | None:
        return self._ticker_to_cik.get(ticker.upper())

    # ── Filing discovery ──────────────────────────────────────────────────────

    async def fetch_8k_filings(
        self,
        ticker: str,
        client: httpx.AsyncClient,
        since: date | None = None,
    ) -> list[dict]:
        """Return 8-K filings with item 2.02 for ticker, optionally filtered by date."""
        await self._ensure_ticker_map(client)
        cik = self.cik_for(ticker)
        if not cik:
            logger.warning("CIK not found for %s", ticker)
            return []

        url = f"{EDGAR_BASE}/submissions/CIK{cik}.json"
        try:
            data = await self._get_json(client, url)
        except Exception as exc:
            logger.error("Submissions fetch failed for %s: %s", ticker, exc)
            return []

        recent = data.get("filings", {}).get("recent", {})
        results = []
        for form, items, filing_date, accession, primary_doc in zip(
            recent.get("form", []),
            recent.get("items", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
        ):
            if form != "8-K":
                continue
            if "2.02" not in str(items):
                continue
            dt = date.fromisoformat(filing_date)
            if since and dt < since:
                continue

            acc_clean = accession.replace("-", "")
            cik_int = int(cik)
            results.append({
                "ticker": ticker,
                "cik": cik,
                "cik_int": cik_int,
                "accession": accession,
                "acc_clean": acc_clean,
                "filing_date": filing_date,
                "items": str(items),
                "primary_doc": primary_doc,
                "filing_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=8-K",
            })

        return results

    async def get_press_release_url(
        self, cik_int: int, acc_clean: str, accession: str, client: httpx.AsyncClient
    ) -> str | None:
        """Find the EX-99.1 press release URL from the filing's HTML document index."""
        from bs4 import BeautifulSoup
        index_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{accession}-index.htm"
        )
        try:
            async with self._semaphore:
                resp = await client.get(index_url, headers=self._sec_headers, timeout=30.0)
                resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            for row in soup.select("table.tableFile tr"):
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue
                doc_type = cells[3].get_text(strip=True)
                if doc_type in ("EX-99.1", "EX-99"):
                    link = cells[2].find("a")
                    if link and link.get("href"):
                        href = link["href"]
                        if not href.startswith("http"):
                            href = f"https://www.sec.gov{href}"
                        return href
        except Exception as exc:
            logger.debug("Filing index fetch failed for %s: %s", accession, exc)
        return None

    # ── Public interface ──────────────────────────────────────────────────────

    async def stream_events(
        self,
        tickers: list[str],
        since: date | None = None,
    ) -> AsyncIterator[AnnouncementEvent]:
        """Yield AnnouncementEvents for all qualifying 8-K filings for the given tickers."""
        async with httpx.AsyncClient() as client:
            for ticker in tickers:
                filings = await self.fetch_8k_filings(ticker, client, since=since)
                for f in filings:
                    doc_url = await self.get_press_release_url(
                        f["cik_int"], f["acc_clean"], f["accession"], client
                    )
                    if not doc_url:
                        # Fall back to the primary 8-K document
                        doc_url = (
                            f"https://www.sec.gov/Archives/edgar/data/"
                            f"{f['cik_int']}/{f['acc_clean']}/{f['primary_doc']}"
                        )

                    yield AnnouncementEvent(
                        market_id="us",
                        ticker=ticker,
                        exchange="",
                        filing_id=f["accession"],
                        filing_type="8-K",
                        filing_url=f["filing_url"],
                        document_url=doc_url,
                        items=[i.strip() for i in f["items"].split(",") if i.strip()],
                        published_at=datetime.fromisoformat(f["filing_date"]),
                    )
                # Gentle inter-ticker pause
                await asyncio.sleep(0.5)
