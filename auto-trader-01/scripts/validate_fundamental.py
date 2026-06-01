"""Automated accuracy validation for FundamentalStrategy extraction.

For each recent 8-K earnings filing in the US watchlist:
  1. Fetch the press release via SEC EDGAR
  2. Run DocumentExtractor + ClaudeClient to get a ResultDocument
  3. Fetch the actual reported numbers from yfinance
  4. Compare extracted vs actual for revenue YoY, EPS YoY, and margin direction
  5. Print a per-filing breakdown and a summary agreement rate

Usage:
    uv run python scripts/validate_fundamental.py
    uv run python scripts/validate_fundamental.py --tickers AAPL NVDA MSFT
    uv run python scripts/validate_fundamental.py --months 6 --tickers AAPL
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import yfinance as yf
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box

load_dotenv()

from agents.strategy_engine.data_feeds.announcement_feed import SecEdgarFeed as AnnouncementFeed
from agents.strategy_engine.strategies.fundamental.claude_client import ClaudeClient
from agents.strategy_engine.strategies.fundamental.pdf_extractor import DocumentExtractor
from agents.strategy_engine.strategies.fundamental.result_document import ResultDocument
from agents.strategy_engine.strategies.fundamental import composite_scorer
from infrastructure.config_registry.loader import ConfigRegistry

console = Console()

DEFAULT_TICKERS = ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN"]
DEFAULT_MONTHS  = 12


# ── Ground truth from yfinance ─────────────────────────────────────────────────

@dataclass
class Actuals:
    quarter_end: date
    revenue_actual_m: Optional[float]
    revenue_yoy_pct:  Optional[float]
    eps_actual:       Optional[float]
    eps_yoy_pct:      Optional[float]
    net_income_yoy_pct: Optional[float]


def _pct_change(new: float, old: float) -> Optional[float]:
    if old and old != 0:
        return round((new - old) / abs(old) * 100, 2)
    return None


def get_actuals(ticker: str, filing_date: date) -> Optional[Actuals]:
    """Return yfinance actual metrics for the quarter reported by this filing."""
    try:
        t = yf.Ticker(ticker)
        # quarterly_income_stmt is the current yfinance API (quarterly_financials is deprecated)
        fin = getattr(t, "quarterly_income_stmt", None)
        if fin is None or (hasattr(fin, "empty") and fin.empty):
            fin = t.quarterly_financials
    except Exception as exc:
        console.print(f"  [yellow]yfinance fetch failed for {ticker}: {exc}[/yellow]")
        return None

    if fin is None or fin.empty:
        return None

    # Find the quarter whose end date is closest to (and ≤) the filing date + 90-day tolerance
    cols = sorted(fin.columns, reverse=True)  # newest first
    target_col = None
    for col in cols:
        q_end = col.date() if hasattr(col, "date") else col
        days_after = (filing_date - q_end).days
        if 0 <= days_after <= 90:
            target_col = col
            break

    if target_col is None:
        return None

    idx = list(cols).index(target_col)
    prior_col = cols[idx + 4] if idx + 4 < len(cols) else None  # same quarter one year ago

    def _row(label: str):
        for key in fin.index:
            if label.lower() in str(key).lower():
                return fin.loc[key]
        return None

    rev_series = _row("Total Revenue")
    if rev_series is None:
        rev_series = _row("Revenue")
    ni_series = _row("Net Income")

    def _val(series, col):
        if series is None or col is None:
            return None
        try:
            v = series[col]
            return float(v) / 1e6 if v is not None and str(v) != "nan" else None
        except Exception:
            return None

    revenue_now   = _val(rev_series, target_col)
    revenue_prior = _val(rev_series, prior_col)
    ni_now        = _val(ni_series, target_col)
    ni_prior      = _val(ni_series, prior_col)

    revenue_yoy = _pct_change(revenue_now, revenue_prior) if revenue_now and revenue_prior else None
    ni_yoy      = _pct_change(ni_now, ni_prior)           if ni_now and ni_prior else None

    # EPS: derive from net income / shares, or use income stmt EPS line
    eps_actual = None
    eps_yoy    = None
    try:
        eps_series = _row("Diluted EPS") or _row("Basic EPS") or _row("EPS")
        if eps_series is not None:
            eps_now   = _val(eps_series, target_col)
            eps_prior_v = _val(eps_series, prior_col)
            # EPS is per-share, not in millions — undo the /1e6
            if eps_now is not None:
                eps_actual = eps_now * 1e6
            if eps_now and eps_prior_v:
                eps_yoy = _pct_change(eps_now, eps_prior_v)
    except Exception:
        pass

    q_end = target_col.date() if hasattr(target_col, "date") else target_col
    return Actuals(
        quarter_end=q_end,
        revenue_actual_m=revenue_now,
        revenue_yoy_pct=revenue_yoy,
        eps_actual=eps_actual,
        eps_yoy_pct=eps_yoy,
        net_income_yoy_pct=ni_yoy,
    )


# ── Agreement logic ────────────────────────────────────────────────────────────

def _direction(pct: Optional[float]) -> str:
    if pct is None:     return "unknown"
    if pct > 2:         return "positive"
    if pct < -2:        return "negative"
    return "flat"


def _agree(extracted: Optional[float], actual: Optional[float]) -> str:
    """Returns PASS / FAIL / SKIP based on direction agreement."""
    if extracted is None or actual is None:
        return "SKIP"
    return "PASS" if _direction(extracted) == _direction(actual) else "FAIL"


@dataclass
class ComparisonResult:
    ticker: str
    quarter: str
    filing_date: str
    doc_url: str
    result_doc: ResultDocument
    actuals: Optional[Actuals]
    rev_agree:   str   # PASS / FAIL / SKIP
    eps_agree:   str
    ni_agree:    str
    overall:     str   # PASS / FAIL / SKIP
    error:       Optional[str] = None


def compare(ticker: str, quarter: str, filing_date: str, doc_url: str,
            result_doc: ResultDocument, actuals: Optional[Actuals]) -> ComparisonResult:
    if actuals is None:
        return ComparisonResult(ticker, quarter, filing_date, doc_url,
                                result_doc, actuals, "SKIP", "SKIP", "SKIP", "SKIP")

    rev_agree = _agree(result_doc.revenue.yoy_growth_pct, actuals.revenue_yoy_pct)
    eps_agree = _agree(
        result_doc.earnings.eps_yoy_growth_pct or result_doc.earnings.net_income_yoy_growth_pct,
        actuals.eps_yoy_pct or actuals.net_income_yoy_pct,
    )
    ni_agree = _agree(result_doc.earnings.net_income_yoy_growth_pct, actuals.net_income_yoy_pct)

    scored = [r for r in [rev_agree, eps_agree] if r != "SKIP"]
    if not scored:
        overall = "SKIP"
    elif all(r == "PASS" for r in scored):
        overall = "PASS"
    else:
        overall = "FAIL"

    return ComparisonResult(ticker, quarter, filing_date, doc_url,
                            result_doc, actuals, rev_agree, eps_agree, ni_agree, overall)


# ── Fetch + run strategy ───────────────────────────────────────────────────────

async def process_filing(
    filing: dict,
    extractor: DocumentExtractor,
    claude: ClaudeClient,
    client: httpx.AsyncClient,
    feed: AnnouncementFeed,
) -> Optional[ComparisonResult]:
    ticker      = filing["ticker"]
    filing_date = date.fromisoformat(filing["filing_date"])
    accession   = filing["accession"]
    q_num       = (filing_date.month - 1) // 3 + 1
    quarter     = f"Q{q_num} FY{filing_date.year}"

    console.print(f"  Processing [bold]{ticker}[/bold] {quarter} (filed {filing_date})")

    # Get press release URL
    doc_url = await feed.get_press_release_url(
        filing["cik_int"], filing["acc_clean"], accession, client
    )
    if not doc_url:
        doc_url = filing.get("document_url", "")
    if not doc_url:
        console.print(f"    [yellow]No press release URL found — skipping[/yellow]")
        return None

    # Extract text
    try:
        text = await extractor.extract(doc_url)
    except Exception as exc:
        console.print(f"    [red]Extraction failed: {exc}[/red]")
        return None

    if not text or len(text) < 100:
        console.print(f"    [yellow]Insufficient text ({len(text) if text else 0} chars) — skipping[/yellow]")
        return None

    # Claude analysis (cached by accession)
    try:
        result_doc = await claude.analyse(text, ticker, quarter, filing_id=accession)
    except Exception as exc:
        console.print(f"    [red]Claude analysis failed: {exc}[/red]")
        return None

    # yfinance actuals
    actuals = get_actuals(ticker, filing_date)

    return compare(ticker, quarter, filing["filing_date"], doc_url, result_doc, actuals)


# ── Report rendering ───────────────────────────────────────────────────────────

def _fmt_pct(v: Optional[float]) -> str:
    return f"{v:+.1f}%" if v is not None else "—"


def _verdict(s: str) -> str:
    return {"PASS": "[green]PASS[/green]", "FAIL": "[red]FAIL[/red]", "SKIP": "[dim]SKIP[/dim]"}.get(s, s)


def print_report(results: list[ComparisonResult]) -> None:
    console.print()

    # Per-filing detail table
    tbl = Table(title="Fundamental Strategy — Extraction Accuracy", box=box.SIMPLE_HEAVY, show_lines=True)
    tbl.add_column("Ticker",   style="bold", width=7)
    tbl.add_column("Quarter",  width=9)
    tbl.add_column("Filed",    width=11)
    tbl.add_column("Rev YoY\n(Claude)", justify="right", width=12)
    tbl.add_column("Rev YoY\n(Actual)", justify="right", width=12)
    tbl.add_column("Rev\nAgree", justify="center", width=8)
    tbl.add_column("EPS YoY\n(Claude)", justify="right", width=12)
    tbl.add_column("EPS YoY\n(Actual)", justify="right", width=12)
    tbl.add_column("EPS\nAgree", justify="center", width=8)
    tbl.add_column("Overall",  justify="center", width=8)
    tbl.add_column("Claude\nConf.", justify="center", width=8)

    for r in results:
        rd = r.result_doc
        ac = r.actuals
        eps_extracted = rd.earnings.eps_yoy_growth_pct or rd.earnings.net_income_yoy_growth_pct
        eps_actual    = (ac.eps_yoy_pct or ac.net_income_yoy_pct) if ac else None

        tbl.add_row(
            r.ticker,
            r.quarter,
            r.filing_date,
            _fmt_pct(rd.revenue.yoy_growth_pct),
            _fmt_pct(ac.revenue_yoy_pct if ac else None),
            _verdict(r.rev_agree),
            _fmt_pct(eps_extracted),
            _fmt_pct(eps_actual),
            _verdict(r.eps_agree),
            _verdict(r.overall),
            rd.confidence,
        )

    console.print(tbl)

    # Summary
    scored   = [r for r in results if r.overall != "SKIP"]
    passed   = sum(1 for r in scored if r.overall == "PASS")
    failed   = sum(1 for r in scored if r.overall == "FAIL")
    skipped  = sum(1 for r in results if r.overall == "SKIP")
    rate     = passed / len(scored) * 100 if scored else 0.0

    gate = rate >= 80.0

    console.print(f"[bold]Summary:[/bold] {len(results)} filings — "
                  f"[green]{passed} PASS[/green] / [red]{failed} FAIL[/red] / [dim]{skipped} SKIP[/dim]")
    console.print(f"[bold]Agreement rate:[/bold] [{'green' if gate else 'red'}]{rate:.1f}%[/{'green' if gate else 'red'}] "
                  f"(Phase 1 gate: ≥80%) — [{'green bold' if gate else 'red bold'}]{'PASS ✓' if gate else 'FAIL ✗'}[/{'green bold' if gate else 'red bold'}]")
    console.print()


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(tickers: list[str], months: int) -> None:
    since = date.today() - timedelta(days=months * 30)
    console.print(f"\n[bold]Fundamental Strategy Validation[/bold]")
    console.print(f"Tickers: {', '.join(tickers)} | Since: {since}\n")

    # Load fundamental config
    config_registry = ConfigRegistry(config_dir=Path("config"))
    fund_config = config_registry.get("strategies/fundamental")

    feed      = AnnouncementFeed(tickers=tickers, user_agent=os.getenv("SEC_EDGAR_USER_AGENT"))
    extractor = DocumentExtractor()
    claude    = ClaudeClient()

    all_filings: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        await feed._ensure_ticker_map(client)
        for ticker in tickers:
            console.print(f"Fetching filings for [bold]{ticker}[/bold]...")
            filings = await feed.fetch_8k_filings(ticker, client, since=since)
            console.print(f"  Found {len(filings)} qualifying 8-K filings")
            for f in filings:
                f["ticker"] = ticker
            all_filings.extend(filings)

    if not all_filings:
        console.print("[yellow]No filings found for the given tickers/period.[/yellow]")
        return

    console.print(f"\nRunning strategy on {len(all_filings)} filings...\n")
    results: list[ComparisonResult] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for filing in all_filings:
            result = await process_filing(filing, extractor, claude, client, feed)
            if result:
                results.append(result)

    if not results:
        console.print("[red]No results produced.[/red]")
        return

    print_report(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate FundamentalStrategy extraction accuracy")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS, metavar="TICKER")
    parser.add_argument("--months",  type=int, default=DEFAULT_MONTHS,   help="Look back N months")
    args = parser.parse_args()

    asyncio.run(main(args.tickers, args.months))
