#!/usr/bin/env python3
"""
Signal Quality Backtest — pre-live validation #6

For each historical 8-K earnings filing in the watchlist:
  1. Run the full FundamentalStrategy pipeline (SEC → Claude → score)
  2. For BUY signals: measure actual price change at +5, +10, +30 trading days
  3. Compare win rate against SPY benchmark over the same periods
  4. Report per-signal breakdown and aggregate stats

Pre-live gate: win rate at +10 days must be ≥ 55% (better than random)
compared against SPY return over the same windows.

Usage:
    uv run python scripts/backtest_signals.py
    uv run python scripts/backtest_signals.py --tickers AAPL NVDA MSFT NTAP GOOGL
    uv run python scripts/backtest_signals.py --months 12
    uv run python scripts/backtest_signals.py --months 6 --min-score 70
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
from agents.strategy_engine.strategies.fundamental import composite_scorer
from agents.strategy_engine.strategies.technical.strategy import (
    _score_macd, _score_rsi, _score_sma, _to_dataframe,
)
from infrastructure.config_registry.loader import ConfigRegistry

console = Console()

DEFAULT_TICKERS = ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "NTAP", "NFLX", "JPM", "META"]
DEFAULT_MONTHS  = 12
HORIZONS        = [5, 10, 30]   # trading days


# ── Price data ────────────────────────────────────────────────────────────────

def _get_forward_return(ticker: str, signal_date: date, horizon_days: int) -> Optional[float]:
    """Return % price change from signal_date close to close N trading days later."""
    try:
        # Fetch enough history to cover the horizon + buffer
        end = signal_date + timedelta(days=horizon_days * 2 + 10)
        hist = yf.Ticker(ticker).history(start=signal_date, end=end, interval="1d", auto_adjust=True)
        if hist.empty or len(hist) < 2:
            return None

        # Entry: first available close on or after signal_date
        entry_price = float(hist["Close"].iloc[0])

        # Exit: close at trading day N (or last available if shorter)
        exit_idx = min(horizon_days, len(hist) - 1)
        exit_price = float(hist["Close"].iloc[exit_idx])

        return round((exit_price - entry_price) / entry_price * 100, 2)
    except Exception:
        return None


def _get_spy_return(signal_date: date, horizon_days: int) -> Optional[float]:
    """Return SPY % change over the same window as a benchmark."""
    return _get_forward_return("SPY", signal_date, horizon_days)


def _was_bull_market(check_date: date, benchmark: str = "SPY", ma_window: int = 50) -> bool:
    """Return True if benchmark close on check_date was above its ma_window-day MA. Fail open."""
    try:
        start = check_date - timedelta(days=ma_window * 3)
        end   = check_date + timedelta(days=5)
        hist  = yf.Ticker(benchmark).history(start=start, end=end, interval="1d", auto_adjust=True)
        if hist.empty or len(hist) < ma_window:
            return True
        closes    = hist["Close"]
        idx_dates = [d.date() if hasattr(d, "date") else d for d in closes.index]
        valid     = [(i, d) for i, d in enumerate(idx_dates) if d <= check_date]
        if not valid or len(valid) < ma_window:
            return True
        last_i  = valid[-1][0]
        current = float(closes.iloc[last_i])
        ma      = float(closes.iloc[max(0, last_i - ma_window + 1): last_i + 1].mean())
        return current > ma
    except Exception:
        return True


# ── Technical gate simulation ─────────────────────────────────────────────────

def _get_candles_for_date(ticker: str, as_of_date: date, n: int = 250) -> list[dict]:
    """Fetch n trading-day candles ending on as_of_date for technical gate simulation."""
    try:
        start = as_of_date - timedelta(days=n * 2)
        end   = as_of_date + timedelta(days=2)
        hist  = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=True)
        if hist.empty:
            return []
        idx_dates = [d.date() if hasattr(d, "date") else d for d in hist.index]
        hist = hist.iloc[[d <= as_of_date for d in idx_dates]]
        hist = hist.tail(n)
        return [
            {"open": float(r.Open), "high": float(r.High),
             "low": float(r.Low), "close": float(r.Close), "volume": float(r.Volume)}
            for _, r in hist.iterrows()
        ]
    except Exception:
        return []


def _check_technical_gate(candles: list[dict], hybrid_cfg: dict) -> tuple[bool, float]:
    """Return (passes_gate, tech_score). Requires ≥210 candles."""
    if len(candles) < 210:
        return False, 0.0
    df       = _to_dataframe(candles)
    ind_cfg  = hybrid_cfg.get("indicators", {})
    w        = ind_cfg.get("weights", {"sma": 0.40, "rsi": 0.30, "macd": 0.30})
    sma_s, _ = _score_sma (df, ind_cfg.get("sma_crossover", {}))
    rsi_s, _ = _score_rsi (df, ind_cfg.get("rsi",           {}))
    mac_s, _ = _score_macd(df, ind_cfg.get("macd",          {}))
    tech     = sma_s * w.get("sma", 0.40) + rsi_s * w.get("rsi", 0.30) + mac_s * w.get("macd", 0.30)
    tech_min = float(hybrid_cfg.get("thresholds", {}).get("technical_min_score", 60.0))
    return tech >= tech_min, round(tech, 1)


# ── Signal scoring ────────────────────────────────────────────────────────────

@dataclass
class BacktestRow:
    ticker:        str
    quarter:       str
    filing_date:   date
    score:         float
    action:        str          # BUY / HOLD / SELL
    confidence:    str
    ret_5d:        Optional[float]
    ret_10d:       Optional[float]
    ret_30d:       Optional[float]
    spy_5d:        Optional[float]
    spy_10d:       Optional[float]
    spy_30d:       Optional[float]
    tech_score:    Optional[float] = None   # technical composite on filing date
    hybrid_pass:   bool = False             # True if tech gate also passed


async def process_filing(
    filing: dict,
    extractor: DocumentExtractor,
    claude: ClaudeClient,
    client: httpx.AsyncClient,
    feed: AnnouncementFeed,
    min_score: float,
    fund_config: dict,
    hybrid_cfg: dict,
) -> Optional[BacktestRow]:
    ticker      = filing["ticker"]
    filing_date = date.fromisoformat(filing["filing_date"])
    accession   = filing["accession"]
    q_num       = (filing_date.month - 1) // 3 + 1
    quarter     = f"Q{q_num} FY{filing_date.year}"

    # Skip filings too recent for meaningful forward return measurement
    days_since = (date.today() - filing_date).days
    if days_since < 35:  # need at least 30 trading days of history
        console.print(f"  [dim]{ticker} {quarter} — too recent ({days_since}d ago), skipping[/dim]")
        return None

    console.print(f"  [bold]{ticker}[/bold] {quarter}  (filed {filing_date}, {days_since}d ago)")

    # Press release URL
    doc_url = await feed.get_press_release_url(
        filing["cik_int"], filing["acc_clean"], accession, client
    )
    if not doc_url:
        doc_url = filing.get("document_url", "")
    if not doc_url:
        console.print(f"    [yellow]No press release URL — skipping[/yellow]")
        return None

    # Extract + analyse
    try:
        text = await extractor.extract(doc_url)
    except Exception as exc:
        console.print(f"    [red]Extraction failed: {exc}[/red]")
        return None

    if not text or len(text) < 100:
        return None

    try:
        result_doc = await claude.analyse(text, ticker, quarter, filing_id=accession)
    except Exception as exc:
        console.print(f"    [red]Claude failed: {exc}[/red]")
        return None

    # Score → action
    try:
        scored = composite_scorer.score(result_doc, fund_config)
    except Exception as exc:
        console.print(f"    [red]Scoring failed: {exc}[/red]")
        return None

    console.print(
        f"    score={scored.composite_score:.1f}  action={scored.action}  conf={scored.confidence}"
    )

    if scored.composite_score < min_score:
        console.print(f"    [dim]Below min-score {min_score} — excluded from backtest[/dim]")
        return None

    # Regime filter: skip if SPY was below 50d MA on the filing date
    loop = asyncio.get_event_loop()
    is_bull = await loop.run_in_executor(None, _was_bull_market, filing_date)
    if not is_bull:
        console.print(f"    [dim]Bear market on {filing_date} (SPY < 50d MA) — regime filtered[/dim]")
        return None

    # Hybrid gate simulation — technical confirmation as of filing date
    loop = asyncio.get_event_loop()
    candles = await loop.run_in_executor(None, _get_candles_for_date, ticker, filing_date)
    tech_score:  Optional[float] = None
    hybrid_pass: bool = False
    if not candles:
        console.print(f"    [dim]Hybrid gate: no candle data[/dim]")
    else:
        hybrid_pass, ts = _check_technical_gate(candles, hybrid_cfg)
        tech_score = ts
        gate_color = "green" if hybrid_pass else "yellow"
        gate_word  = "PASS" if hybrid_pass else "FAIL"
        console.print(
            f"    Hybrid tech gate: [{gate_color}]{gate_word}[/{gate_color}]"
            f"  tech_score={tech_score:.1f}  candles={len(candles)}"
        )

    # Forward returns (run in thread to avoid blocking)
    loop = asyncio.get_event_loop()
    r5, r10, r30 = await asyncio.gather(
        loop.run_in_executor(None, _get_forward_return, ticker, filing_date, 5),
        loop.run_in_executor(None, _get_forward_return, ticker, filing_date, 10),
        loop.run_in_executor(None, _get_forward_return, ticker, filing_date, 30),
    )
    s5, s10, s30 = await asyncio.gather(
        loop.run_in_executor(None, _get_spy_return, filing_date, 5),
        loop.run_in_executor(None, _get_spy_return, filing_date, 10),
        loop.run_in_executor(None, _get_spy_return, filing_date, 30),
    )

    return BacktestRow(
        ticker=ticker, quarter=quarter, filing_date=filing_date,
        score=scored.composite_score, action=scored.action, confidence=scored.confidence,
        ret_5d=r5, ret_10d=r10, ret_30d=r30,
        spy_5d=s5, spy_10d=s10, spy_30d=s30,
        tech_score=tech_score, hybrid_pass=hybrid_pass,
    )


# ── Reporting ─────────────────────────────────────────────────────────────────

def _pct(v: Optional[float], spy: Optional[float] = None) -> str:
    if v is None:
        return "[dim]—[/dim]"
    color = "green" if v > 0 else "red"
    s = f"[{color}]{v:+.1f}%[/{color}]"
    if spy is not None:
        alpha = v - spy
        ac = "green" if alpha > 0 else "red"
        s += f" ([{ac}]{alpha:+.1f}[/{ac}])"
    return s


def _win(rows: list[BacktestRow], attr: str) -> tuple[int, int, float]:
    vals = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
    wins = sum(1 for v in vals if v > 0)
    return wins, len(vals), (wins / len(vals) * 100 if vals else 0.0)


def print_report(rows: list[BacktestRow]) -> None:
    buy_rows  = [r for r in rows if r.action == "BUY"]
    hold_rows = [r for r in rows if r.action != "BUY"]

    console.print(f"\n[bold]All signals scored: {len(rows)}[/bold]  "
                  f"([green]BUY: {len(buy_rows)}[/green]  HOLD/SELL: {len(hold_rows)})\n")

    if not buy_rows:
        console.print("[yellow]No BUY signals — nothing to backtest.[/yellow]")
        return

    # ── Per-signal table ──────────────────────────────────────────────────────
    tbl = Table(title="BUY Signal Forward Returns (alpha vs SPY in brackets)",
                box=box.SIMPLE_HEAVY, show_lines=True)
    tbl.add_column("Ticker",   style="bold", width=7)
    tbl.add_column("Quarter",  width=9)
    tbl.add_column("Filed",    width=11)
    tbl.add_column("Score",    justify="right", width=6)
    tbl.add_column("TechGate", justify="right", width=10)
    tbl.add_column("Conf",     width=7)
    tbl.add_column("+5d",      justify="right", width=14)
    tbl.add_column("+10d",     justify="right", width=14)
    tbl.add_column("+30d",     justify="right", width=14)

    for r in sorted(buy_rows, key=lambda x: x.filing_date, reverse=True):
        if r.tech_score is None:
            tech_cell = "[dim]—[/dim]"
        elif r.hybrid_pass:
            tech_cell = f"[green]✓ {r.tech_score:.0f}[/green]"
        else:
            tech_cell = f"[yellow]✗ {r.tech_score:.0f}[/yellow]"
        tbl.add_row(
            r.ticker, r.quarter, str(r.filing_date),
            f"{r.score:.1f}", tech_cell, r.confidence,
            _pct(r.ret_5d,  r.spy_5d),
            _pct(r.ret_10d, r.spy_10d),
            _pct(r.ret_30d, r.spy_30d),
        )
    console.print(tbl)

    # ── Win rate summary ──────────────────────────────────────────────────────
    console.print("[bold]Win Rate (BUY signals only):[/bold]")
    gate_pass = True
    for attr, label, spy_attr in [
        ("ret_5d",  "+5d",  "spy_5d"),
        ("ret_10d", "+10d", "spy_10d"),
        ("ret_30d", "+30d", "spy_30d"),
    ]:
        wins, n, rate = _win(buy_rows, attr)
        spy_vals = [getattr(r, spy_attr) for r in buy_rows if getattr(r, spy_attr) is not None]
        spy_avg  = sum(spy_vals) / len(spy_vals) if spy_vals else None
        stock_vals = [getattr(r, attr) for r in buy_rows if getattr(r, attr) is not None]
        avg_ret  = sum(stock_vals) / len(stock_vals) if stock_vals else None

        color = "green" if rate >= 55 else "red"
        gate_this = rate >= 55 and n >= 5
        if label == "+10d":
            gate_pass = gate_this

        spy_str = f"  SPY avg: {spy_avg:+.1f}%" if spy_avg is not None else ""
        alpha_str = f"  alpha: {(avg_ret - spy_avg):+.1f}%" if avg_ret is not None and spy_avg is not None else ""
        console.print(
            f"  {label:>4}:  [{color}]{rate:.0f}% wins[/{color}]  ({wins}/{n})  "
            f"avg: {avg_ret:+.1f}%" if avg_ret is not None else f"  {label:>4}:  [{color}]{rate:.0f}% wins[/{color}]  ({wins}/{n})"
        )
        if avg_ret is not None:
            console.print(f"         avg ret: {avg_ret:+.1f}%{spy_str}{alpha_str}")

    # ── Score tier breakdown ──────────────────────────────────────────────────
    console.print("\n[bold]By Score Tier:[/bold]")
    tiers = [(85, 100, "≥85 (full pos)"), (70, 85, "70–84 (60% pos)"), (60, 70, "60–69 (30% pos)")]
    for lo, hi, label in tiers:
        tier_rows = [r for r in buy_rows if lo <= r.score < hi or (hi == 100 and r.score >= lo)]
        if not tier_rows:
            continue
        _, _, rate = _win(tier_rows, "ret_10d")
        vals = [r.ret_10d for r in tier_rows if r.ret_10d is not None]
        avg  = sum(vals) / len(vals) if vals else None
        console.print(
            f"  {label}: {len(tier_rows)} signals, "
            f"+10d win rate={rate:.0f}%"
            + (f", avg={avg:+.1f}%" if avg is not None else "")
        )

    # ── Fundamental-only gate verdict ─────────────────────────────────────────
    n_buy    = len(buy_rows)
    n_enough = n_buy >= 10

    console.print(f"\n[bold]Fundamental-Only Pre-live Gate:[/bold]")
    if not n_enough:
        console.print(
            f"  [yellow]⚠  Only {n_buy} BUY signals — need ≥10 for statistical confidence.[/yellow]\n"
            f"  [dim]Run with --months 18 or add more tickers to increase sample size.[/dim]"
        )
    elif gate_pass:
        console.print(f"  [green bold]PASS ✓[/green bold]  +10d win rate ≥ 55% with {n_buy} signals")
    else:
        console.print(f"  [red bold]FAIL ✗[/red bold]  +10d win rate below 55% threshold")

    # ── Hybrid-filtered section ───────────────────────────────────────────────
    hybrid_rows = [r for r in buy_rows if r.hybrid_pass]
    no_data_n   = sum(1 for r in buy_rows if r.tech_score is None)

    console.rule("\n[bold]Hybrid-Filtered Results[/bold]")
    console.print(
        f"Technical gate (SMA/RSI/MACD composite ≥ 60) applied on top of fundamental.\n"
        f"  Fundamental BUY signals : {n_buy}\n"
        f"  Passed technical gate   : {len(hybrid_rows)}"
        + (f"\n  No candle data          : {no_data_n}" if no_data_n else "")
    )

    if not hybrid_rows:
        console.print("\n[yellow]  No signals passed both gates — cannot measure win rate.[/yellow]")
    else:
        console.print("\n[bold]Hybrid Win Rate (BUY signals passing both gates):[/bold]")
        hybrid_gate_pass = False
        for attr, label, spy_attr in [
            ("ret_5d",  "+5d",  "spy_5d"),
            ("ret_10d", "+10d", "spy_10d"),
            ("ret_30d", "+30d", "spy_30d"),
        ]:
            wins, n, rate = _win(hybrid_rows, attr)
            stock_vals = [getattr(r, attr)      for r in hybrid_rows if getattr(r, attr)      is not None]
            spy_vals   = [getattr(r, spy_attr)  for r in hybrid_rows if getattr(r, spy_attr)  is not None]
            avg_ret    = sum(stock_vals) / len(stock_vals) if stock_vals else None
            spy_avg    = sum(spy_vals)   / len(spy_vals)   if spy_vals   else None

            color = "green" if rate >= 55 else "red"
            if label == "+10d":
                hybrid_gate_pass = rate >= 55 and n >= 5

            spy_str   = f"  SPY avg: {spy_avg:+.1f}%" if spy_avg is not None else ""
            alpha_str = f"  alpha: {(avg_ret - spy_avg):+.1f}%" if avg_ret is not None and spy_avg is not None else ""
            console.print(f"  {label:>4}:  [{color}]{rate:.0f}% wins[/{color}]  ({wins}/{n})")
            if avg_ret is not None:
                console.print(f"         avg ret: {avg_ret:+.1f}%{spy_str}{alpha_str}")

        n_hybrid = len(hybrid_rows)
        console.print(f"\n[bold]Hybrid Pre-live Gate:[/bold]")
        if n_hybrid < 10:
            console.print(
                f"  [yellow]⚠  Only {n_hybrid} hybrid signals — need ≥10 for statistical confidence.[/yellow]\n"
                f"  [dim]Run with --months 18 or add more tickers.[/dim]"
            )
        elif hybrid_gate_pass:
            console.print(f"  [green bold]PASS ✓[/green bold]  Hybrid +10d win rate ≥ 55% with {n_hybrid} signals")
        else:
            console.print(f"  [red bold]FAIL ✗[/red bold]  Hybrid +10d win rate below 55% threshold")

    console.print()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(tickers: list[str], months: int, min_score: float) -> None:
    since = date.today() - timedelta(days=months * 30)
    console.print(f"\n[bold]Signal Quality Backtest[/bold]")
    console.print(f"Tickers: {', '.join(tickers)}")
    console.print(f"Period:  {since} → {date.today()} ({months} months)")
    console.print(f"Min score for inclusion: {min_score}\n")

    cfg         = ConfigRegistry(config_dir=Path("config"))
    fund_config = cfg.get("strategies/fundamental")
    hybrid_cfg  = cfg.get("strategies/hybrid")
    feed        = AnnouncementFeed(tickers=tickers, user_agent=os.getenv("SEC_EDGAR_USER_AGENT"))
    extractor   = DocumentExtractor()
    claude      = ClaudeClient()

    all_filings: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        await feed._ensure_ticker_map(client)
        for ticker in tickers:
            console.print(f"Fetching filings for [bold]{ticker}[/bold]...")
            filings = await feed.fetch_8k_filings(ticker, client, since=since)
            console.print(f"  {len(filings)} qualifying 8-K filings")
            for f in filings:
                f["ticker"] = ticker
            all_filings.extend(filings)

    if not all_filings:
        console.print("[yellow]No filings found.[/yellow]")
        return

    console.print(f"\nRunning strategy on {len(all_filings)} filings...\n")
    rows: list[BacktestRow] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for filing in all_filings:
            row = await process_filing(filing, extractor, claude, client, feed, min_score, fund_config, hybrid_cfg)
            if row:
                rows.append(row)

    if not rows:
        console.print("[red]No results (all filings too recent or failed).[/red]")
        console.print("[dim]Try --months 18 to look further back.[/dim]")
        return

    print_report(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest fundamental BUY signals against actual price movement")
    parser.add_argument("--tickers",   nargs="+", default=DEFAULT_TICKERS, metavar="TICKER")
    parser.add_argument("--months",    type=int,  default=DEFAULT_MONTHS,  help="Look-back window in months")
    parser.add_argument("--min-score", type=float, default=60.0, dest="min_score",
                        help="Only include signals scoring above this threshold (default: 60)")
    args = parser.parse_args()
    asyncio.run(main(args.tickers, args.months, args.min_score))
