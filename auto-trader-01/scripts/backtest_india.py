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
import json
import math
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
from rich.panel import Panel
from rich.rule import Rule
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


# ── Statistical helpers (no scipy — stdlib math only) ─────────────────────────

def _wilson_ci(wins: int, n: int, z: float = 1.645) -> tuple[float, float]:
    """Wilson score interval. Default z=1.645 → 90% two-sided CI."""
    if n == 0:
        return 0.0, 1.0
    p      = wins / n
    denom  = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _binom_pvalue(wins: int, n: int, p0: float = 0.5) -> float:
    """One-sided p-value P(X ≥ wins | Binomial(n, p0)). Normal approximation with continuity correction."""
    if n == 0:
        return 1.0
    z = (wins - 0.5 - n * p0) / math.sqrt(n * p0 * (1.0 - p0))
    return 1.0 - _norm_cdf(z)


def _info_ratio(alphas: list[float]) -> Optional[float]:
    """Mean alpha / std(alpha). Returns None if insufficient data."""
    if len(alphas) < 2:
        return None
    mean = sum(alphas) / len(alphas)
    var  = sum((a - mean) ** 2 for a in alphas) / (len(alphas) - 1)
    std  = math.sqrt(var)
    return round(mean / std, 2) if std > 0 else None


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
    stats:        dict,
) -> Optional[BacktestRow]:
    filing_date = date.fromisoformat(filing["filing_date"])
    quarter     = filing["quarter"]
    xbrl_url    = filing["xbrl_url"]

    days_since = (date.today() - filing_date).days
    if days_since < 35:
        stats["too_recent"] = stats.get("too_recent", 0) + 1
        console.print(f"  [dim]{ticker} {quarter} — too recent ({days_since}d ago), skipping[/dim]")
        return None

    console.print(f"  [bold]{ticker}[/bold] {quarter}  (filed {filing_date}, {days_since}d ago)")

    loop = asyncio.get_event_loop()

    # Fetch current quarter XBRL
    try:
        xml_text = await loop.run_in_executor(None, _fetch_xbrl_sync, xbrl_url)
        curr_metrics = _parse_xbrl(xml_text)
    except Exception as exc:
        stats["no_xbrl"] = stats.get("no_xbrl", 0) + 1
        console.print(f"    [red]XBRL fetch/parse failed: {exc}[/red]")
        return None

    if not curr_metrics.get("revenue") and not curr_metrics.get("pat"):
        stats["no_xbrl"] = stats.get("no_xbrl", 0) + 1
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
        stats["no_xbrl"] = stats.get("no_xbrl", 0) + 1
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
        stats["below_score"] = stats.get("below_score", 0) + 1
        console.print(f"    [dim]Below min-score {min_score} — excluded[/dim]")
        return None

    stats["buy_pre_regime"] = stats.get("buy_pre_regime", 0) + 1

    # Regime filter
    is_bull = await loop.run_in_executor(None, _was_nifty_bull, filing_date)
    if not is_bull:
        stats["regime_filtered"] = stats.get("regime_filtered", 0) + 1
        console.print(f"    [dim]Bear market on {filing_date} (Midcap100 < 50d MA) — regime filtered[/dim]")
        return None

    stats["passed"] = stats.get("passed", 0) + 1

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


# ── Formal evaluation scorecard ───────────────────────────────────────────────

def print_formal_evaluation(
    rows:       list[BacktestRow],
    stats:      dict,
    total_filings: int,
    months:     int,
    min_score:  float,
    universe:   list[str],
) -> None:
    buy_rows     = [r for r in rows if r.action == "BUY"]
    hybrid_rows  = [r for r in rows if r.action == "BUY" and r.hybrid_pass]
    n            = len(buy_rows)

    console.print(Rule("[bold]FORMAL STRATEGY EVALUATION SCORECARD[/bold]"))
    console.print()

    # ── Section 1: Signal pipeline ────────────────────────────────────────────
    console.print("[bold underline]SECTION 1 — SIGNAL PIPELINE[/bold underline]")
    pipeline_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    pipeline_tbl.add_column("Label", style="dim")
    pipeline_tbl.add_column("Count", justify="right")
    pipeline_tbl.add_row("Universe",            f"{len(universe)} tickers")
    pipeline_tbl.add_row("Lookback",            f"{months} months")
    pipeline_tbl.add_row("Total filings",        str(total_filings))
    pipeline_tbl.add_row("Too recent (<35d)",    str(stats.get("too_recent", 0)))
    pipeline_tbl.add_row("XBRL / scoring error", str(stats.get("no_xbrl", 0)))
    pipeline_tbl.add_row(f"Below score {min_score:.0f}", str(stats.get("below_score", 0)))
    pre  = stats.get("buy_pre_regime", 0)
    rflt = stats.get("regime_filtered", 0)
    pipeline_tbl.add_row(f"BUY ≥{min_score:.0f} before regime filter", str(pre))
    pipeline_tbl.add_row("Regime filtered (Midcap100 < 50d MA)", f"[yellow]{rflt}[/yellow]")
    pipeline_tbl.add_row("[bold]BUY signals (final)[/bold]",
                         f"[bold green]{n}[/bold green]")
    console.print(pipeline_tbl)

    if n == 0:
        console.print("[red]No BUY signals — cannot evaluate.[/red]")
        return

    # ── Section 2: Win rate scorecard ─────────────────────────────────────────
    console.print("[bold underline]SECTION 2 — WIN RATE SCORECARD[/bold underline]")
    console.print("  Phase 3 gate: +10d win rate ≥ 55%  ·  minimum n = 10\n")

    wr_tbl = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    wr_tbl.add_column("Horizon", width=8)
    wr_tbl.add_column("Wins/n",  justify="right", width=8)
    wr_tbl.add_column("Win %",   justify="right", width=8)
    wr_tbl.add_column("90% CI",  justify="center", width=18)
    wr_tbl.add_column("Gate",    width=10)

    gate_result: Optional[bool] = None
    for attr, label in [("ret_5d", "+5d"), ("ret_10d", "+10d"), ("ret_30d", "+30d")]:
        vals  = [getattr(r, attr) for r in buy_rows if getattr(r, attr) is not None]
        wins  = sum(1 for v in vals if v > 0)
        ni    = len(vals)
        rate  = wins / ni if ni else 0.0
        lo, hi = _wilson_ci(wins, ni)
        ci_str = f"[{lo*100:.1f}%, {hi*100:.1f}%]"
        color  = "green" if rate >= 0.55 else "red"

        if label == "+10d":
            if ni >= 10:
                gate_result = rate >= 0.55
                gate_str = ("[green bold]PASS ✓[/green bold]" if gate_result
                            else "[red bold]FAIL ✗[/red bold]")
            else:
                gate_str = f"[yellow]n={ni} < 10[/yellow]"
        else:
            gate_str = "—"

        wr_tbl.add_row(
            label,
            f"{wins}/{ni}",
            f"[{color}]{rate*100:.1f}%[/{color}]",
            f"[dim]{ci_str}[/dim]",
            gate_str,
        )
    console.print(wr_tbl)

    # ── Section 3: Statistical significance ───────────────────────────────────
    console.print("[bold underline]SECTION 3 — STATISTICAL SIGNIFICANCE[/bold underline]")
    td_vals = [r.ret_10d for r in buy_rows if r.ret_10d is not None]
    td_wins = sum(1 for v in td_vals if v > 0)
    ni      = len(td_vals)
    p_val   = _binom_pvalue(td_wins, ni)
    z_score = (td_wins - 0.5 - ni * 0.5) / math.sqrt(ni * 0.25) if ni else 0.0
    sig     = p_val <= 0.10

    sig_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    sig_tbl.add_column("Label", style="dim")
    sig_tbl.add_column("Value")
    sig_tbl.add_row("H₀", "win_rate(+10d) ≤ 0.50  (no edge over coin flip)")
    sig_tbl.add_row("H₁", "win_rate(+10d) > 0.50")
    sig_tbl.add_row("Method", "Normal approx to Binomial (continuity corrected)")
    sig_tbl.add_row("z-score", f"{z_score:.2f}")
    sig_tbl.add_row("p-value (one-sided)", f"{p_val:.3f}")
    verdict = (
        "[green]Reject H₀ at α=0.10 — edge statistically supported[/green]"
        if sig else
        "[yellow]Cannot reject H₀ at α=0.10 — edge not yet significant[/yellow]"
    )
    sig_tbl.add_row("Verdict", verdict)
    console.print(sig_tbl)
    if not sig:
        console.print(
            f"  [dim]Note: need ~{math.ceil(0.5 + 1.282 * math.sqrt(ni * 0.25) + ni * 0.5)} wins from {ni} signals "
            f"(or larger n) for 90% significance.[/dim]\n"
        )

    # ── Section 4: Alpha vs benchmark ─────────────────────────────────────────
    console.print("[bold underline]SECTION 4 — ALPHA vs NIFTY MIDCAP 100[/bold underline]")

    alpha_tbl = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    alpha_tbl.add_column("Horizon",      width=8)
    alpha_tbl.add_column("Avg Return",   justify="right", width=12)
    alpha_tbl.add_column("Bench Avg",    justify="right", width=12)
    alpha_tbl.add_column("Mean Alpha",   justify="right", width=12)
    alpha_tbl.add_column("Alpha Rate",   justify="right", width=12)
    alpha_tbl.add_column("Info Ratio",   justify="right", width=12)

    for attr, battr, label in [
        ("ret_5d",  "nifty_5d",  "+5d"),
        ("ret_10d", "nifty_10d", "+10d"),
        ("ret_30d", "nifty_30d", "+30d"),
    ]:
        pairs   = [(getattr(r, attr), getattr(r, battr))
                   for r in buy_rows
                   if getattr(r, attr) is not None and getattr(r, battr) is not None]
        if not pairs:
            alpha_tbl.add_row(label, "—", "—", "—", "—", "—")
            continue
        sigs   = [s for s, _ in pairs]
        bnchs  = [b for _, b in pairs]
        alphas = [s - b for s, b in pairs]
        ma     = sum(alphas) / len(alphas)
        ar     = sum(1 for a in alphas if a > 0) / len(alphas)
        ir     = _info_ratio(alphas)
        ac     = "green" if ma > 0 else "red"
        alpha_tbl.add_row(
            label,
            f"{sum(sigs)/len(sigs):+.1f}%",
            f"{sum(bnchs)/len(bnchs):+.1f}%",
            f"[{ac}]{ma:+.1f}%[/{ac}]",
            f"{ar*100:.0f}%",
            f"{ir:+.2f}" if ir is not None else "—",
        )
    console.print(alpha_tbl)

    # ── Section 5: Return profile (+10d) ──────────────────────────────────────
    console.print("[bold underline]SECTION 5 — RETURN PROFILE  (+10d)[/bold underline]")

    td_all  = [r.ret_10d for r in buy_rows if r.ret_10d is not None]
    winners = [v for v in td_all if v > 0]
    losers  = [v for v in td_all if v <= 0]
    avg_win = sum(winners) / len(winners) if winners else 0.0
    avg_los = sum(losers)  / len(losers)  if losers  else 0.0
    wl_ratio = abs(avg_win / avg_los) if avg_los != 0 else float("inf")
    wr_rate  = len(winners) / len(td_all) if td_all else 0.0
    ev       = wr_rate * avg_win + (1 - wr_rate) * avg_los

    rp_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    rp_tbl.add_column("Label", style="dim")
    rp_tbl.add_column("Value", justify="right")
    rp_tbl.add_row("Average win",    f"[green]{avg_win:+.2f}%[/green]  ({len(winners)} trades)")
    rp_tbl.add_row("Average loss",   f"[red]{avg_los:+.2f}%[/red]  ({len(losers)} trades)")
    rp_tbl.add_row("Win/loss ratio", f"{wl_ratio:.2f}x")
    ev_color = "green" if ev > 0 else "red"
    rp_tbl.add_row("Expected value per signal",
                   f"[{ev_color}]{ev:+.2f}%[/{ev_color}]")
    console.print(rp_tbl)

    # ── Section 6: Score tier breakdown ───────────────────────────────────────
    console.print("[bold underline]SECTION 6 — SCORE TIER BREAKDOWN  (+10d)[/bold underline]")

    tier_tbl = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    tier_tbl.add_column("Tier",        width=8)
    tier_tbl.add_column("n",           justify="right", width=5)
    tier_tbl.add_column("Win Rate",    justify="right", width=10)
    tier_tbl.add_column("90% CI",      justify="center", width=18)
    tier_tbl.add_column("Avg Return",  justify="right", width=12)
    tier_tbl.add_column("Avg Alpha",   justify="right", width=12)

    for lo_s, hi_s, label in [(85, 101, "≥85"), (70, 85, "70–84"), (60, 70, "60–69")]:
        tier = [r for r in buy_rows if lo_s <= r.score < hi_s]
        if not tier:
            continue
        tv    = [r.ret_10d for r in tier if r.ret_10d is not None]
        bv    = [r.nifty_10d for r in tier if r.nifty_10d is not None]
        tw    = sum(1 for v in tv if v > 0)
        tni   = len(tv)
        rate  = tw / tni if tni else 0.0
        lo_ci, hi_ci = _wilson_ci(tw, tni)
        avg_r = sum(tv) / len(tv) if tv else 0.0
        pairs = [(r.ret_10d, r.nifty_10d) for r in tier
                 if r.ret_10d is not None and r.nifty_10d is not None]
        avg_a = sum(s - b for s, b in pairs) / len(pairs) if pairs else 0.0
        c     = "green" if rate >= 0.55 else "red"
        ac    = "green" if avg_a > 0 else "red"
        tier_tbl.add_row(
            label, str(len(tier)),
            f"[{c}]{rate*100:.1f}%[/{c}]",
            f"[dim][{lo_ci*100:.1f}%, {hi_ci*100:.1f}%][/dim]",
            f"{avg_r:+.1f}%",
            f"[{ac}]{avg_a:+.1f}%[/{ac}]",
        )
    console.print(tier_tbl)

    # ── Section 7: Gate comparison ────────────────────────────────────────────
    console.print("[bold underline]SECTION 7 — GATE COMPARISON[/bold underline]")

    gc_tbl = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    gc_tbl.add_column("Gate",             width=32)
    gc_tbl.add_column("n",                justify="right", width=5)
    gc_tbl.add_column("+10d Win Rate",    justify="right", width=14)
    gc_tbl.add_column("Avg Alpha",        justify="right", width=12)
    gc_tbl.add_column("Verdict",          width=12)

    def _gate_row(label, subset):
        sv  = [r.ret_10d for r in subset if r.ret_10d is not None]
        bv  = [r.nifty_10d for r in subset if r.nifty_10d is not None]
        sw  = sum(1 for v in sv if v > 0)
        sni = len(sv)
        rate = sw / sni if sni else 0.0
        pairs = list(zip(sv, bv[:len(sv)]))
        avg_a = sum(s - b for s, b in pairs) / len(pairs) if pairs else 0.0
        c = "green" if rate >= 0.55 else "red"
        ac = "green" if avg_a > 0 else "red"
        if sni < 10:
            verdict = f"[yellow]n={sni} < 10[/yellow]"
        elif rate >= 0.55:
            verdict = "[green bold]PASS ✓[/green bold]"
        else:
            verdict = "[red bold]FAIL ✗[/red bold]"
        gc_tbl.add_row(
            label, str(len(subset)),
            f"[{c}]{rate*100:.1f}%[/{c}]",
            f"[{ac}]{avg_a:+.1f}%[/{ac}]",
            verdict,
        )

    _gate_row("Fundamental-only (tech gate off)", buy_rows)
    _gate_row("Hybrid (SMA/RSI/MACD ≥ 60)",      hybrid_rows)
    console.print(gc_tbl)

    # ── Section 8: Regime filter value ────────────────────────────────────────
    console.print("[bold underline]SECTION 8 — REGIME FILTER VALUE[/bold underline]")

    rflt = stats.get("regime_filtered", 0)
    pre  = stats.get("buy_pre_regime",  0)
    pct  = rflt / pre * 100 if pre else 0.0
    console.print(
        f"  BUY signals before regime filter : {pre}\n"
        f"  Removed by Midcap100 < 50d MA   : {rflt}  ({pct:.0f}% of pre-filter BUYs)\n"
        f"  BUY signals after regime filter  : {n}\n"
    )

    # ── Section 9: Verdict ────────────────────────────────────────────────────
    td_v  = [r.ret_10d for r in buy_rows if r.ret_10d is not None]
    td_w  = sum(1 for v in td_v if v > 0)
    td_ni = len(td_v)
    wr    = td_w / td_ni if td_ni else 0.0

    if td_ni < 10:
        gate_color, gate_text = "yellow", f"INCONCLUSIVE — only {td_ni} signals"
    elif wr >= 0.55:
        gate_color, gate_text = "green", f"PASS ✓  {wr*100:.1f}% +10d win rate  ·  n={td_ni}"
    else:
        gate_color, gate_text = "red", f"FAIL ✗  {wr*100:.1f}% +10d win rate  ·  n={td_ni}"

    sig_note = (
        "Edge statistically supported (p≤0.10)"
        if p_val <= 0.10 else
        f"Edge not yet significant (p={p_val:.2f}) — monitor first 20 live signals before scaling"
    )

    console.print(Panel(
        f"[{gate_color} bold]Phase 3 Pre-Live Gate:  {gate_text}[/{gate_color} bold]\n\n"
        f"  Strategy      : Fundamental-only  ·  score ≥ {min_score:.0f}  ·  Midcap100 regime filter\n"
        f"  Universe      : {len(universe)} NSE mid/small-caps  ·  {months}-month backtest\n"
        f"  Expected value: {ev:+.2f}% per signal\n"
        f"  Significance  : {sig_note}",
        title="[bold]SECTION 9 — VERDICT[/bold]",
        border_style=gate_color,
    ))
    console.print()


# ── JSON export ───────────────────────────────────────────────────────────────

def _export_results(
    rows:          list[BacktestRow],
    stats:         dict,
    total_filings: int,
    months:        int,
    min_score:     float,
    universe:      list[str],
    export_path:   str,
) -> None:
    buy_rows    = [r for r in rows if r.action == "BUY"]
    hybrid_rows = [r for r in rows if r.action == "BUY" and r.hybrid_pass]
    n           = len(buy_rows)

    td_vals  = [r.ret_10d for r in buy_rows if r.ret_10d is not None]
    td_wins  = sum(1 for v in td_vals if v > 0)
    ni       = len(td_vals)
    win_rate = td_wins / ni if ni else 0.0
    lo_ci, hi_ci = _wilson_ci(td_wins, ni)
    p_val    = _binom_pvalue(td_wins, ni)
    z_score  = (td_wins - 0.5 - ni * 0.5) / math.sqrt(ni * 0.25) if ni else 0.0
    gate_pass = ni >= 10 and win_rate >= 0.55

    winners  = [v for v in td_vals if v > 0]
    losers   = [v for v in td_vals if v <= 0]
    avg_win  = sum(winners) / len(winners) if winners else 0.0
    avg_los  = sum(losers)  / len(losers)  if losers  else 0.0
    ev       = win_rate * avg_win + (1 - win_rate) * avg_los

    pairs_10d  = [(r.ret_10d, r.nifty_10d) for r in buy_rows
                  if r.ret_10d is not None and r.nifty_10d is not None]
    mean_alpha = sum(s - b for s, b in pairs_10d) / len(pairs_10d) if pairs_10d else 0.0

    tiers = []
    for lo_s, hi_s, label in [(85, 101, "≥85"), (70, 85, "70–84"), (60, 70, "60–69")]:
        tier = [r for r in buy_rows if lo_s <= r.score < hi_s]
        if not tier:
            continue
        tv   = [r.ret_10d for r in tier if r.ret_10d is not None]
        tw   = sum(1 for v in tv if v > 0)
        tni  = len(tv)
        tp   = [(r.ret_10d, r.nifty_10d) for r in tier
                if r.ret_10d is not None and r.nifty_10d is not None]
        lo_t, hi_t = _wilson_ci(tw, tni)
        tiers.append({
            "tier":       label,
            "n":          len(tier),
            "wins":       tw,
            "win_rate":   round(tw / tni, 3) if tni else 0.0,
            "ci_lo":      round(lo_t, 3),
            "ci_hi":      round(hi_t, 3),
            "avg_return": round(sum(tv) / len(tv), 2) if tv else 0.0,
            "avg_alpha":  round(sum(s - b for s, b in tp) / len(tp), 2) if tp else 0.0,
        })

    def _gate_stats(subset: list[BacktestRow]) -> dict:
        sv   = [r.ret_10d  for r in subset if r.ret_10d  is not None]
        bv   = [r.nifty_10d for r in subset if r.nifty_10d is not None]
        sw   = sum(1 for v in sv if v > 0)
        sni  = len(sv)
        rate = sw / sni if sni else 0.0
        pp   = list(zip(sv, bv[:len(sv)]))
        avg_a = sum(s - b for s, b in pp) / len(pp) if pp else 0.0
        verdict = ("inconclusive" if sni < 10 else "PASS" if rate >= 0.55 else "FAIL")
        return {"n": len(subset), "wins": sw, "win_rate": round(rate, 3),
                "avg_alpha": round(avg_a, 2), "verdict": verdict}

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "config": {
            "months":    months,
            "min_score": min_score,
            "universe":  universe,
            "benchmark": "Nifty Midcap 100 (^NSMIDCP)",
        },
        "pipeline": {
            "total_filings":  total_filings,
            "too_recent":     stats.get("too_recent",      0),
            "no_xbrl":        stats.get("no_xbrl",         0),
            "below_score":    stats.get("below_score",     0),
            "buy_pre_regime": stats.get("buy_pre_regime",  0),
            "regime_filtered":stats.get("regime_filtered", 0),
            "passed":         n,
        },
        "summary": {
            "n_buy":         n,
            "n_hybrid":      len(hybrid_rows),
            "win_rate_10d":  round(win_rate, 3),
            "ci_lo":         round(lo_ci, 3),
            "ci_hi":         round(hi_ci, 3),
            "p_value":       round(p_val, 3),
            "z_score":       round(z_score, 2),
            "gate_pass":     gate_pass,
            "mean_alpha_10d":round(mean_alpha, 2),
            "expected_value":round(ev, 2),
            "avg_win":       round(avg_win, 2),
            "avg_loss":      round(avg_los, 2),
        },
        "tiers":           tiers,
        "gate_comparison": [
            {"gate": "Fundamental-only (tech gate off)", **_gate_stats(buy_rows)},
            {"gate": "Hybrid (SMA/RSI/MACD ≥ 60)",      **_gate_stats(hybrid_rows)},
        ],
        "signals": [
            {
                "ticker":      r.ticker,
                "quarter":     r.quarter,
                "filing_date": str(r.filing_date),
                "score":       r.score,
                "action":      r.action,
                "confidence":  r.confidence,
                "ret_5d":      r.ret_5d,
                "ret_10d":     r.ret_10d,
                "ret_30d":     r.ret_30d,
                "nifty_5d":    r.nifty_5d,
                "nifty_10d":   r.nifty_10d,
                "nifty_30d":   r.nifty_30d,
                "tech_score":  r.tech_score,
                "hybrid_pass": r.hybrid_pass,
            }
            for r in sorted(buy_rows, key=lambda x: x.filing_date, reverse=True)
        ],
    }

    Path(export_path).parent.mkdir(parents=True, exist_ok=True)
    with open(export_path, "w") as fh:
        json.dump(payload, fh, indent=2)
    console.print(f"\n[green]Backtest results exported → {export_path}[/green]")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(tickers: list[str], months: int, min_score: float,
               export_path: str | None = None) -> None:
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
    rows:  list[BacktestRow] = []
    stats: dict              = {}

    for ticker, filing, all_filings, filing_idx in target_filings:
        row = await process_filing(
            ticker, filing, all_filings, filing_idx,
            min_score, fund_config, hybrid_cfg, stats,
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
    print_formal_evaluation(rows, stats, len(target_filings), months, min_score, tickers)

    if export_path:
        _export_results(rows, stats, len(target_filings), months, min_score, tickers, export_path)


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
    parser.add_argument(
        "--export", type=str, default=None, dest="export",
        metavar="PATH",
        help="Export results as JSON (e.g. data/backtest_india.json)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.tickers, args.months, args.min_score, args.export))
