#!/usr/bin/env python3
"""
India Small-Cap Hybrid Strategy Backtest — pre-live validation

Uses NSE quarterly financial results (XBRL) for fundamental scoring.
BSE Results API was deprecated (now requires auth); NSE XBRL archives are
publicly accessible and contain full P&L + management notes.

For each NSE quarterly result in the target small-cap universe:
  1. Fetch quarterly results list from NSE API (session-based)
  2. Download XBRL, extract Revenue, PAT, EPS, operating margin
  3. Compute YoY growth vs same quarter prior year (4 quarters back)
  4. Score using the same composite_scorer rubric as production
  5. For BUY signals: simulate hybrid technical gate (SMA/RSI/MACD)
     using yfinance .NS candle data as-of filing date
  6. Measure actual price change at +5, +10, +30 trading days vs Nifty 50

Pre-live gate: hybrid-filtered +10d win rate ≥ 55% on ≥10 signals.

XBRL context convention (Ind-AS):
  FourD = Consolidated quarterly duration (preferred)
  OneD  = Standalone quarterly duration (fallback)

Usage:
    uv run python scripts/backtest_india.py
    uv run python scripts/backtest_india.py --months 24
    uv run python scripts/backtest_india.py --min-score 65
    uv run python scripts/backtest_india.py --tickers CAPLIPOINT POLYMED KPITTECH
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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

from agents.strategy_engine.strategies.fundamental import composite_scorer
from agents.strategy_engine.strategies.fundamental.result_document import (
    DividendData, EarningsData, ExceptionalItems,
    GuidanceData, MarginData, ResultDocument, RevenueData,
)
from agents.strategy_engine.strategies.technical.strategy import (
    _score_macd, _score_rsi, _score_sma, _to_dataframe,
)
from infrastructure.config_registry.loader import ConfigRegistry

console = Console()

# ── Target universe ───────────────────────────────────────────────────────────
# NSE symbol → (unused, display name)  — only NSE symbols needed now
INDIA_SMALLCAP: dict[str, str] = {
    # Original 10
    "CAPLIPOINT": "Caplin Point Labs",     # pharma exports
    "POLYMED":    "Poly Medicure",          # medical devices
    "KPITTECH":   "KPIT Technologies",      # automotive software
    "LALPATHLAB": "Dr. Lal PathLabs",       # diagnostics
    "BRIGADE":    "Brigade Enterprises",    # real estate
    "CLEAN":      "Clean Science Tech",     # specialty chemicals
    "JKPAPER":    "JK Paper",               # paper manufacturing
    "HAPPSTMNDS": "Happiest Minds",         # IT services
    "AAVAS":      "AAVAS Financiers",       # housing finance
    "FINEORG":    "Fine Organics",          # specialty chemicals
    # Expansion batch — quality small/mid-caps with consistent XBRL history
    "SYNGENE":    "Syngene International",  # biotech CRO/CDMO
    "METROPOLIS": "Metropolis Healthcare",  # diagnostics (like LalPath)
    "ELGIEQUIP":  "Elgi Equipments",        # industrial compressors
    "PRAJ":       "Praj Industries",        # ethanol / bioenergy
    "GRINDWELL":  "Grindwell Norton",       # abrasives / ceramics
    "DIXON":      "Dixon Technologies",     # electronics manufacturing
    "MASTEK":     "Mastek",                 # IT services (UK-focused)
    "GARFIBRES":  "Garware Technical Fibres",  # technical textiles
    "DEEPAKNTR":  "Deepak Nitrite",         # specialty chemicals
    "ASTRAL":     "Astral Ltd",             # pipes and adhesives
}

NIFTY_BENCHMARK = "^NSMIDCP"  # Nifty Midcap 100 — closest yfinance-available proxy for this universe
DEFAULT_MONTHS  = 36
HORIZONS        = [5, 10, 30]

_NSE_BASE = "https://www.nseindia.com"
_NSE_HDRS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
}


# ── NSE filing fetcher ────────────────────────────────────────────────────────

def _parse_nse_date(s: str | None) -> Optional[date]:
    """Parse NSE date strings like '30-Jan-2025 13:12:27' or '01-Oct-2024'."""
    if not s:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.strip()[:20], fmt).date()
        except (ValueError, TypeError):
            pass
    return None


def _nse_item_to_quarter(item: dict) -> str:
    """Convert NSE result item to 'Q3 FY2025' label using fromDate."""
    from_date = item.get("fromDate", "")
    try:
        d     = datetime.strptime(from_date.strip(), "%d-%b-%Y")
        month = d.month
        year  = d.year
        if   month in (4, 5, 6):    q, fy = 1, year + 1
        elif month in (7, 8, 9):    q, fy = 2, year + 1
        elif month in (10, 11, 12): q, fy = 3, year + 1
        else:                        q, fy = 4, year
        return f"Q{q} FY{fy}"
    except (ValueError, TypeError):
        return item.get("relatingTo", "?")


async def _fetch_nse_filings_all(ticker: str, client: httpx.AsyncClient) -> list[dict]:
    """
    Return ALL quarterly result filings for ticker, sorted by quarter start descending.
    Deduplicates by quarter start date, preferring Consolidated over Standalone.
    """
    try:
        r = await client.get(
            f"{_NSE_BASE}/api/corporates-financial-results",
            params={"index": "equities", "symbol": ticker, "period": "Quarterly"},
            headers=_NSE_HDRS,
        )
        if r.status_code != 200:
            console.print(f"  [yellow]NSE returned {r.status_code} for {ticker}[/yellow]")
            return []
        all_items = r.json()
    except Exception as exc:
        console.print(f"  [yellow]NSE API error for {ticker}: {exc}[/yellow]")
        return []

    # Deduplicate by fromDate, preferring Consolidated
    by_period: dict[str, dict] = {}
    for item in all_items:
        if not item.get("xbrl"):
            continue
        from_date_str = item.get("fromDate", "").strip()
        if not from_date_str:
            continue
        try:
            from_dt = datetime.strptime(from_date_str, "%d-%b-%Y").date()
        except ValueError:
            continue

        broadcast_str = item.get("broadCastDate") or ""
        filed_at = _parse_nse_date(broadcast_str) or from_dt

        existing     = by_period.get(from_date_str)
        is_consol    = item.get("consolidated", "") == "Consolidated"
        existing_ok  = existing and existing.get("consolidated") == "Consolidated"

        if existing is None or (is_consol and not existing_ok):
            by_period[from_date_str] = {
                "filing_date": filed_at.isoformat(),
                "from_date":   from_date_str,
                "from_dt":     from_dt,
                "to_date":     item.get("toDate", ""),
                "quarter":     _nse_item_to_quarter(item),
                "xbrl_url":    item["xbrl"],
                "consolidated": item.get("consolidated", "Standalone"),
                "filing_id":   hashlib.sha256(
                    f"nse_{ticker}_{from_date_str}_{item.get('consolidated','')}".encode()
                ).hexdigest()[:16],
            }

    return sorted(by_period.values(), key=lambda x: x["from_dt"], reverse=True)


# ── XBRL parser ───────────────────────────────────────────────────────────────

def _fetch_xbrl_sync(url: str) -> str:
    """Blocking XBRL fetch — call via run_in_executor."""
    r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _xbrl_get(root: ET.Element, tag: str, ctx: str) -> Optional[float]:
    for el in root.iter():
        t = el.tag.split("}")[1] if "}" in el.tag else el.tag
        if t == tag and el.get("contextRef") == ctx:
            try:
                return float(el.text or "")
            except (ValueError, TypeError):
                return None
    return None


def _xbrl_text(root: ET.Element, tag: str, ctx: str) -> Optional[str]:
    for el in root.iter():
        t = el.tag.split("}")[1] if "}" in el.tag else el.tag
        if t == tag and el.get("contextRef") == ctx:
            return (el.text or "").strip() or None
    return None


def _parse_xbrl(xml_text: str) -> dict:
    """
    Extract key P&L metrics from Ind-AS XBRL filing.
    Tries FourD (Consolidated quarter) first, then OneD (Standalone quarter).
    """
    root = ET.fromstring(xml_text)

    def _get(tag: str) -> Optional[float]:
        return (_xbrl_get(root, tag, "FourD")
                or _xbrl_get(root, tag, "OneD"))

    def _text(tag: str) -> Optional[str]:
        return (_xbrl_text(root, tag, "OneD")
                or _xbrl_text(root, tag, "FourD"))

    return {
        "revenue":   _get("RevenueFromOperations"),
        "income":    _get("Income"),
        "expenses":  _get("Expenses"),
        "pbt":       _get("ProfitBeforeTax"),
        "pat":       _get("ProfitLossForPeriod"),
        "eps":       _get("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations")
                     or _get("BasicEarningsLossPerShareFromContinuingOperations"),
        "notes":     _text("DisclosureOfNotesOnFinancialResultsExplanatoryTextBlock") or "",
    }


def _xbrl_to_result_doc(
    ticker: str,
    quarter: str,
    curr: dict,
    prior: dict | None,
) -> ResultDocument:
    """Build ResultDocument from current + prior-year XBRL metrics."""

    def _yoy(c: Optional[float], p: Optional[float]) -> Optional[float]:
        if c is not None and p and p != 0:
            return round((c - p) / abs(p) * 100, 1)
        return None

    rev_yoy  = _yoy(curr["revenue"], prior["revenue"] if prior else None)
    pat_yoy  = _yoy(curr["pat"],     prior["pat"]     if prior else None)
    eps_yoy  = _yoy(curr["eps"],     prior["eps"]     if prior else None)

    net_margin = op_margin = margin_dir = None
    if curr["revenue"] and curr["revenue"] > 0:
        if curr["pat"] is not None:
            net_margin = round(curr["pat"] / curr["revenue"] * 100, 1)
        if curr["income"] and curr["expenses"]:
            op_margin = round((curr["income"] - curr["expenses"]) / curr["revenue"] * 100, 1)

    if prior and prior.get("revenue") and prior["revenue"] > 0 and prior.get("pat") is not None:
        prior_nm = prior["pat"] / prior["revenue"] * 100
        if net_margin is not None:
            diff       = net_margin - prior_nm
            margin_dir = "expanding" if diff > 0.5 else "contracting" if diff < -0.5 else "stable"

    return ResultDocument(
        ticker=ticker,
        quarter=quarter,
        revenue=RevenueData(
            actual_millions=curr["revenue"] / 1_000_000 if curr["revenue"] else None,
            yoy_growth_pct=rev_yoy,
        ),
        earnings=EarningsData(
            eps_actual=curr["eps"],
            eps_yoy_growth_pct=eps_yoy,
            net_income_millions=curr["pat"] / 1_000_000 if curr["pat"] else None,
            net_income_yoy_growth_pct=pat_yoy,
        ),
        margins=MarginData(
            operating_margin_pct=op_margin,
            operating_margin_direction=margin_dir,
        ),
        guidance=GuidanceData(provided=False),
        dividend=DividendData(declared=False),
        exceptional_items=ExceptionalItems(present=False),
        confidence="medium",
        notes=curr.get("notes", ""),
    )


# ── Price / regime / technical helpers ───────────────────────────────────────

def _get_forward_return(ticker: str, signal_date: date, horizon_days: int) -> Optional[float]:
    try:
        end  = signal_date + timedelta(days=horizon_days * 2 + 10)
        hist = yf.Ticker(ticker).history(start=signal_date, end=end, interval="1d", auto_adjust=True)
        if hist.empty or len(hist) < 2:
            return None
        entry = float(hist["Close"].iloc[0])
        exit_ = float(hist["Close"].iloc[min(horizon_days, len(hist) - 1)])
        return round((exit_ - entry) / entry * 100, 2)
    except Exception:
        return None


def _get_nifty_return(signal_date: date, horizon_days: int) -> Optional[float]:
    return _get_forward_return(NIFTY_BENCHMARK, signal_date, horizon_days)


def _was_nifty_bull(check_date: date, ma_window: int = 50) -> bool:
    """True if Nifty Midcap 100 was above its ma_window-day MA on check_date. Fail open."""
    try:
        start     = check_date - timedelta(days=ma_window * 3)
        end       = check_date + timedelta(days=5)
        hist      = yf.Ticker(NIFTY_BENCHMARK).history(start=start, end=end, interval="1d", auto_adjust=True)
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


def _get_candles_for_date(ticker: str, as_of_date: date, n: int = 250) -> list[dict]:
    try:
        start     = as_of_date - timedelta(days=n * 2)
        end       = as_of_date + timedelta(days=2)
        hist      = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=True)
        if hist.empty:
            return []
        idx_dates = [d.date() if hasattr(d, "date") else d for d in hist.index]
        hist      = hist.iloc[[d <= as_of_date for d in idx_dates]].tail(n)
        return [
            {"open": float(r.Open), "high": float(r.High),
             "low": float(r.Low), "close": float(r.Close), "volume": float(r.Volume)}
            for _, r in hist.iterrows()
        ]
    except Exception:
        return []


def _check_technical_gate(candles: list[dict], hybrid_cfg: dict) -> tuple[bool, float]:
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


# ── BacktestRow ───────────────────────────────────────────────────────────────

@dataclass
class BacktestRow:
    ticker:      str
    quarter:     str
    filing_date: date
    score:       float
    action:      str
    confidence:  str
    ret_5d:      Optional[float]
    ret_10d:     Optional[float]
    ret_30d:     Optional[float]
    nifty_5d:    Optional[float]
    nifty_10d:   Optional[float]
    nifty_30d:   Optional[float]
    tech_score:  Optional[float] = None
    hybrid_pass: bool = False


# ── Per-filing analysis ───────────────────────────────────────────────────────

async def process_filing(
    ticker:       str,
    filing:       dict,
    all_filings:  list[dict],
    filing_index: int,
    min_score:    float,
    fund_config:  dict,
    hybrid_cfg:   dict,
) -> Optional[BacktestRow]:
    filing_date = date.fromisoformat(filing["filing_date"])
    quarter     = filing["quarter"]
    xbrl_url    = filing["xbrl_url"]

    days_since = (date.today() - filing_date).days
    if days_since < 35:
        console.print(f"  [dim]{ticker} {quarter} — too recent ({days_since}d ago), skipping[/dim]")
        return None

    console.print(f"  [bold]{ticker}[/bold] {quarter}  (filed {filing_date}, {days_since}d ago)")

    loop = asyncio.get_event_loop()

    # Fetch current quarter XBRL
    try:
        xml_text = await loop.run_in_executor(None, _fetch_xbrl_sync, xbrl_url)
        curr_metrics = _parse_xbrl(xml_text)
    except Exception as exc:
        console.print(f"    [red]XBRL fetch/parse failed: {exc}[/red]")
        return None

    if not curr_metrics.get("revenue") and not curr_metrics.get("pat"):
        console.print(f"    [yellow]No financial data in XBRL — skipping[/yellow]")
        return None

    # Fetch prior year same quarter (4 entries back in sorted list)
    prior_metrics: dict | None = None
    prior_idx = filing_index + 4
    if prior_idx < len(all_filings):
        try:
            prior_xml    = await loop.run_in_executor(
                None, _fetch_xbrl_sync, all_filings[prior_idx]["xbrl_url"]
            )
            prior_metrics = _parse_xbrl(prior_xml)
        except Exception:
            pass   # proceed without YoY — scorer will use 50 for that dimension

    # Build ResultDocument and score
    try:
        result_doc = _xbrl_to_result_doc(ticker, quarter, curr_metrics, prior_metrics)
        scored     = composite_scorer.score(result_doc, fund_config)
    except Exception as exc:
        console.print(f"    [red]Scoring failed: {exc}[/red]")
        return None

    yoy_str = ""
    if result_doc.revenue.yoy_growth_pct is not None:
        yoy_str = f"  rev_yoy={result_doc.revenue.yoy_growth_pct:+.1f}%"
    console.print(
        f"    score={scored.composite_score:.1f}  action={scored.action}"
        f"  conf={scored.confidence}{yoy_str}"
    )

    if scored.composite_score < min_score:
        console.print(f"    [dim]Below min-score {min_score} — excluded[/dim]")
        return None

    # Regime filter
    is_bull = await loop.run_in_executor(None, _was_nifty_bull, filing_date)
    if not is_bull:
        console.print(f"    [dim]Bear market on {filing_date} (Midcap100 < 50d MA) — regime filtered[/dim]")
        return None

    # Hybrid gate simulation
    nse_ticker   = ticker + ".NS"
    candles      = await loop.run_in_executor(None, _get_candles_for_date, nse_ticker, filing_date)
    tech_score:  Optional[float] = None
    hybrid_pass: bool = False
    if not candles:
        console.print(f"    [dim]Hybrid gate: no candle data[/dim]")
    else:
        hybrid_pass, ts = _check_technical_gate(candles, hybrid_cfg)
        tech_score = ts
        gc = "green" if hybrid_pass else "yellow"
        gw = "PASS" if hybrid_pass else "FAIL"
        console.print(
            f"    Hybrid tech gate: [{gc}]{gw}[/{gc}]"
            f"  tech_score={tech_score:.1f}  candles={len(candles)}"
        )

    # Forward returns
    r5, r10, r30 = await asyncio.gather(
        loop.run_in_executor(None, _get_forward_return, nse_ticker, filing_date, 5),
        loop.run_in_executor(None, _get_forward_return, nse_ticker, filing_date, 10),
        loop.run_in_executor(None, _get_forward_return, nse_ticker, filing_date, 30),
    )
    n5, n10, n30 = await asyncio.gather(
        loop.run_in_executor(None, _get_nifty_return, filing_date, 5),
        loop.run_in_executor(None, _get_nifty_return, filing_date, 10),
        loop.run_in_executor(None, _get_nifty_return, filing_date, 30),
    )

    return BacktestRow(
        ticker=ticker, quarter=quarter, filing_date=filing_date,
        score=scored.composite_score, action=scored.action, confidence=scored.confidence,
        ret_5d=r5, ret_10d=r10, ret_30d=r30,
        nifty_5d=n5, nifty_10d=n10, nifty_30d=n30,
        tech_score=tech_score, hybrid_pass=hybrid_pass,
    )


# ── Reporting ─────────────────────────────────────────────────────────────────

def _pct(v: Optional[float], bench: Optional[float] = None) -> str:
    if v is None:
        return "[dim]—[/dim]"
    color = "green" if v > 0 else "red"
    s = f"[{color}]{v:+.1f}%[/{color}]"
    if bench is not None:
        alpha = v - bench
        ac    = "green" if alpha > 0 else "red"
        s    += f" ([{ac}]{alpha:+.1f}[/{ac}])"
    return s


def _win(rows: list[BacktestRow], attr: str) -> tuple[int, int, float]:
    vals = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
    wins = sum(1 for v in vals if v > 0)
    return wins, len(vals), (wins / len(vals) * 100 if vals else 0.0)


def _win_rate_block(
    rows: list[BacktestRow],
    bench_map: list[tuple[str, str, str]],
) -> bool:
    gate_pass = False
    for attr, disp, bench_attr in bench_map:
        wins, n, rate = _win(rows, attr)
        stock_vals = [getattr(r, attr)       for r in rows if getattr(r, attr)       is not None]
        bench_vals = [getattr(r, bench_attr) for r in rows if getattr(r, bench_attr) is not None]
        avg_ret   = sum(stock_vals) / len(stock_vals) if stock_vals else None
        bench_avg = sum(bench_vals) / len(bench_vals) if bench_vals else None

        color = "green" if rate >= 55 else "red"
        if disp == "+10d" and n >= 5:
            gate_pass = rate >= 55

        bench_str = f"  MidCap100 avg: {bench_avg:+.1f}%" if bench_avg is not None else ""
        alpha_str = (
            f"  alpha: {(avg_ret - bench_avg):+.1f}%"
            if avg_ret is not None and bench_avg is not None else ""
        )
        console.print(f"  {disp:>4}:  [{color}]{rate:.0f}% wins[/{color}]  ({wins}/{n})")
        if avg_ret is not None:
            console.print(f"         avg ret: {avg_ret:+.1f}%{bench_str}{alpha_str}")
    return gate_pass


def print_report(rows: list[BacktestRow]) -> None:
    buy_rows  = [r for r in rows if r.action == "BUY"]
    hold_rows = [r for r in rows if r.action != "BUY"]

    console.print(
        f"\n[bold]All signals scored: {len(rows)}[/bold]  "
        f"([green]BUY: {len(buy_rows)}[/green]  HOLD/SELL: {len(hold_rows)})\n"
    )
    if not buy_rows:
        console.print("[yellow]No BUY signals — nothing to backtest.[/yellow]")
        return

    bench_map = [
        ("ret_5d",  "+5d",  "nifty_5d"),
        ("ret_10d", "+10d", "nifty_10d"),
        ("ret_30d", "+30d", "nifty_30d"),
    ]

    # ── Per-signal table ──────────────────────────────────────────────────────
    tbl = Table(
        title="BUY Signal Forward Returns (alpha vs Nifty Midcap 100 in brackets)",
        box=box.SIMPLE_HEAVY, show_lines=True,
    )
    tbl.add_column("Ticker",   style="bold", width=12)
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
            _pct(r.ret_5d,  r.nifty_5d),
            _pct(r.ret_10d, r.nifty_10d),
            _pct(r.ret_30d, r.nifty_30d),
        )
    console.print(tbl)

    # ── Fundamental-only win rates ────────────────────────────────────────────
    console.print("[bold]Fundamental-Only Win Rate (BUY signals):[/bold]")
    gate_pass = _win_rate_block(buy_rows, bench_map)

    # ── Score tier breakdown ──────────────────────────────────────────────────
    console.print("\n[bold]By Score Tier:[/bold]")
    for lo, hi, label in [(85, 101, "≥85"), (70, 85, "70–84"), (60, 70, "60–69")]:
        tier_rows = [r for r in buy_rows if lo <= r.score < hi]
        if not tier_rows:
            continue
        _, _, rate = _win(tier_rows, "ret_10d")
        vals = [r.ret_10d for r in tier_rows if r.ret_10d is not None]
        avg  = sum(vals) / len(vals) if vals else None
        console.print(
            f"  {label}: {len(tier_rows)} signals, +10d win rate={rate:.0f}%"
            + (f", avg={avg:+.1f}%" if avg is not None else "")
        )

    # ── Fundamental gate verdict ──────────────────────────────────────────────
    n_buy = len(buy_rows)
    console.print(f"\n[bold]Fundamental-Only Pre-live Gate:[/bold]")
    if n_buy < 10:
        console.print(
            f"  [yellow]⚠  Only {n_buy} BUY signals — need ≥10 for statistical confidence.[/yellow]\n"
            f"  [dim]Run with --months 24 or add more tickers.[/dim]"
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
        console.print("\n[yellow]  No signals passed both gates.[/yellow]")
    else:
        console.print("\n[bold]Hybrid Win Rate (both gates passed):[/bold]")
        hybrid_gate_pass = _win_rate_block(hybrid_rows, bench_map)

        n_hybrid = len(hybrid_rows)
        console.print(f"\n[bold]Hybrid Pre-live Gate:[/bold]")
        if n_hybrid < 10:
            console.print(
                f"  [yellow]⚠  Only {n_hybrid} hybrid signals — need ≥10 for statistical confidence.[/yellow]\n"
                f"  [dim]Run with --months 24 or add more tickers.[/dim]"
            )
        elif hybrid_gate_pass:
            console.print(
                f"  [green bold]PASS ✓[/green bold]  Hybrid +10d win rate ≥ 55% with {n_hybrid} signals"
            )
        else:
            console.print(f"  [red bold]FAIL ✗[/red bold]  Hybrid +10d win rate below 55% threshold")

    console.print()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(tickers: list[str], months: int, min_score: float) -> None:
    since = date.today() - timedelta(days=months * 30)

    console.print(f"\n[bold]India Small-Cap Hybrid Strategy Backtest[/bold]")
    console.print(f"Universe : {', '.join(tickers)}")
    console.print(f"Period   : {since} → {date.today()} ({months} months)")
    console.print(f"Benchmark: Nifty Midcap 100 (^NSMIDCP)  |  Source: NSE XBRL archives")
    console.print(f"Min score: {min_score}\n")

    cfg         = ConfigRegistry(config_dir=Path("config"))
    fund_config = cfg.get("strategies/fundamental")
    hybrid_cfg  = cfg.get("strategies/hybrid")

    # (ticker, filing, all_filings_for_ticker, index_in_all)
    target_filings: list[tuple[str, dict, list[dict], int]] = []

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        # Establish NSE session cookie
        try:
            await client.get(_NSE_BASE, headers=_NSE_HDRS)
            await asyncio.sleep(0.5)
        except Exception:
            pass

        for ticker in tickers:
            display = INDIA_SMALLCAP.get(ticker, ticker)
            console.print(f"Fetching NSE filings for [bold]{ticker}[/bold] ({display})...")
            all_filings = await _fetch_nse_filings_all(ticker, client)
            in_window   = [f for f in all_filings if date.fromisoformat(f["filing_date"]) >= since]
            console.print(
                f"  {len(in_window)} filings in {months}m window "
                f"(of {len(all_filings)} total historical)"
            )
            for f in all_filings:
                if date.fromisoformat(f["filing_date"]) >= since:
                    idx = all_filings.index(f)
                    target_filings.append((ticker, f, all_filings, idx))
            await asyncio.sleep(1.0)   # conservative NSE rate limiting

    if not target_filings:
        console.print(
            "[yellow]No filings found.[/yellow]\n"
            "[dim]NSE may be blocking the session. Try again in a few seconds.[/dim]"
        )
        return

    console.print(f"\nRunning strategy on {len(target_filings)} filings...\n")
    rows: list[BacktestRow] = []

    for ticker, filing, all_filings, filing_idx in target_filings:
        row = await process_filing(
            ticker, filing, all_filings, filing_idx,
            min_score, fund_config, hybrid_cfg,
        )
        if row:
            rows.append(row)

    if not rows:
        console.print(
            "[red]No results.[/red]\n"
            "[dim]All filings may be too recent, XBRL parse failed, or below min-score.[/dim]\n"
            "[dim]Try --months 24 to extend the lookback window.[/dim]"
        )
        return

    print_report(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backtest hybrid strategy on India small-cap NSE quarterly results (XBRL)"
    )
    parser.add_argument(
        "--tickers", nargs="+",
        default=list(INDIA_SMALLCAP.keys()),
        choices=list(INDIA_SMALLCAP.keys()),
        metavar="TICKER",
        help=f"NSE symbols to include (default: all {len(INDIA_SMALLCAP)})",
    )
    parser.add_argument(
        "--months", type=int, default=DEFAULT_MONTHS,
        help="Look-back window in months (default: 36 — NSE XBRL for small-caps typically goes back 3-4 years)",
    )
    parser.add_argument(
        "--min-score", type=float, default=60.0, dest="min_score",
        help="Minimum fundamental score to include (default: 60)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.tickers, args.months, args.min_score))
