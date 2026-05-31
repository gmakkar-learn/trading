"""Backtester CLI.

Usage:
  uv run python -m backtester.cli --strategy fundamental_v1 --market us --quarters last3
  uv run python -m backtester.cli --strategy fundamental_v1 --market us --quarters last3 --csv results.csv
  uv run python -m backtester.cli --strategy fundamental_v1 --market us --quarters 2024-07-01:2025-04-30
"""
from __future__ import annotations
import asyncio
import logging
import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console

console = Console()


@click.command()
@click.option("--strategy", default="fundamental_v1", show_default=True, help="Strategy ID from active.yaml")
@click.option("--market",   default="us",             show_default=True, help="Market ID (us | india)")
@click.option("--quarters", default="last3",          show_default=True,
              help="Quarters to backtest: 'lastN' or 'YYYY-MM-DD:YYYY-MM-DD'")
@click.option("--csv",      default=None,             help="Save results to CSV file")
@click.option("--no-prices", is_flag=True,            help="Skip price fetch (faster, no accuracy stats)")
@click.option("--verbose",  is_flag=True,             help="Debug logging")
def main(strategy: str, market: str, quarters: str, csv: str | None, no_prices: bool, verbose: bool) -> None:
    """Run the backtester against historical SEC filings."""
    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy third-party loggers unless verbose
    if not verbose:
        for noisy in ("httpx", "httpcore", "yfinance", "peewee", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    asyncio.run(_run(strategy, market, quarters, csv, not no_prices))


async def _run(strategy_id: str, market_id: str, quarters: str, csv_path: str | None, fetch_prices: bool) -> None:
    from infrastructure.market_context.loader import load_market_context
    from infrastructure.config_registry.loader import ConfigRegistry
    from infrastructure.watchlist.provider import WatchlistProvider
    from agents.strategy_engine.strategy_registry import load_active_strategies
    from backtester.historical_loader import load_filings
    from backtester.simulation_runner import run
    from backtester import report

    console.print(f"\n[bold]Auto Trader 01 — Backtester[/bold]")
    console.print(f"Strategy: [cyan]{strategy_id}[/cyan]  Market: [cyan]{market_id}[/cyan]  Quarters: [cyan]{quarters}[/cyan]\n")

    config_dir = Path("config")
    config_registry = ConfigRegistry(config_dir)
    market = load_market_context(market_id, config_dir)
    watchlist = WatchlistProvider(config_dir)
    tickers = await watchlist.get_tickers(market_id)

    console.print(f"Watchlist ({len(tickers)} tickers): {', '.join(tickers)}")

    strategies = load_active_strategies(market_id, config_registry, config_dir)
    active = [s for s in strategies if s.strategy_id == strategy_id]
    if not active:
        console.print(f"[red]Strategy '{strategy_id}' not found or not enabled for market '{market_id}'.[/red]")
        sys.exit(1)

    console.print(f"Loading historical filings (quarters={quarters}) …")
    events = await load_filings(tickers, quarters)

    if not events:
        console.print("[yellow]No filings found for the specified period and tickers.[/yellow]")
        return

    console.print(f"Processing {len(events)} filings through {strategy_id} …\n")
    results = await run(events, active, market, config_registry, watchlist, fetch_prices=fetch_prices)

    report.print_report(results, strategy_id, market_id)

    if csv_path:
        report.save_csv(results, csv_path)


if __name__ == "__main__":
    main()
