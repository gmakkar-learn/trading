"""Formats and displays backtester results."""
from __future__ import annotations
from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich import box

from .simulation_runner import BacktestResult

console = Console()


def print_report(results: list[BacktestResult], strategy_id: str, market_id: str) -> None:
    _print_summary(results, strategy_id, market_id)
    _print_signal_table(results)
    _print_accuracy(results)


def _print_summary(results: list[BacktestResult], strategy_id: str, market_id: str) -> None:
    total = len(results)
    signaled = sum(1 for r in results if r.signal)
    no_signal = total - signaled
    errors = sum(1 for r in results if r.error)

    console.print(f"\n[bold cyan]Backtester Report — {strategy_id} / {market_id}[/bold cyan]")
    console.print(f"Run at: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    console.print(f"  Total filings processed : {total}")
    console.print(f"  Signals generated       : {signaled}")
    console.print(f"  No signal (below threshold) : {no_signal}")
    console.print(f"  Errors                  : {errors}\n")


def _print_signal_table(results: list[BacktestResult]) -> None:
    signaled = [r for r in results if r.signal]
    if not signaled:
        console.print("[yellow]No signals generated.[/yellow]")
        return

    table = Table(title="Signals Generated", box=box.SIMPLE_HEAVY, show_lines=False)
    table.add_column("Date",     style="dim",    width=12)
    table.add_column("Ticker",   style="bold",   width=8)
    table.add_column("Action",   width=6)
    table.add_column("Score",    width=7)
    table.add_column("Conf.",    width=8)
    table.add_column("5d Move%", width=10)
    table.add_column("20d Move%",width=10)
    table.add_column("Notes",    width=40)

    for r in signaled:
        sig = r.signal
        action_style = "green" if sig.recommended_action == "BUY" else "red" if sig.recommended_action == "SELL" else "yellow"
        move5  = _fmt_pct(r.actual_move_5d_pct)
        move20 = _fmt_pct(r.actual_move_20d_pct)
        notes = sig.context.get("guidance_direction") or ""
        if sig.context.get("exceptional_items"):
            notes = (notes + " [exceptional items]").strip()

        table.add_row(
            r.event.published_at.strftime("%Y-%m-%d"),
            sig.ticker,
            f"[{action_style}]{sig.recommended_action}[/{action_style}]",
            f"{sig.composite_score:.1f}",
            sig.confidence,
            move5,
            move20,
            notes,
        )

    console.print(table)


def _print_accuracy(results: list[BacktestResult]) -> None:
    buy_signals = [r for r in results if r.signal and r.signal.recommended_action == "BUY"]
    if not buy_signals or not any(r.actual_move_20d_pct is not None for r in buy_signals):
        console.print("[dim]Price data not available for accuracy calculation.[/dim]")
        return

    with_prices = [r for r in buy_signals if r.actual_move_20d_pct is not None]
    correct = sum(1 for r in with_prices if r.actual_move_20d_pct and r.actual_move_20d_pct > 0)
    pct = correct / len(with_prices) * 100 if with_prices else 0

    avg_move = sum(r.actual_move_20d_pct for r in with_prices if r.actual_move_20d_pct) / len(with_prices)

    console.print(f"\n[bold]BUY Signal Accuracy (20-day):[/bold]")
    console.print(f"  Correct (positive return) : {correct}/{len(with_prices)} ({pct:.0f}%)")
    console.print(f"  Average 20d move          : {avg_move:+.2f}%")
    console.print()


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "[dim]—[/dim]"
    color = "green" if v > 0 else "red"
    return f"[{color}]{v:+.1f}%[/{color}]"


def save_csv(results: list[BacktestResult], path: str) -> None:
    import csv
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date", "ticker", "filing_id", "action", "score", "confidence",
            "price_at_filing", "price_5d", "price_20d",
            "move_5d_pct", "move_20d_pct",
            "revenue_yoy_pct", "eps_yoy_pct", "margin_direction",
            "guidance_direction", "exceptional_items", "error",
        ])
        for r in results:
            sig = r.signal
            ctx = sig.context if sig else {}
            writer.writerow([
                r.event.published_at.date(),
                r.event.ticker,
                r.event.filing_id,
                sig.recommended_action if sig else "",
                f"{sig.composite_score:.2f}" if sig else "",
                sig.confidence if sig else "",
                r.price_at_filing,
                r.price_5d_later,
                r.price_20d_later,
                r.actual_move_5d_pct,
                r.actual_move_20d_pct,
                ctx.get("revenue_yoy_pct"),
                ctx.get("eps_yoy_pct"),
                ctx.get("margin_direction"),
                ctx.get("guidance_direction"),
                ctx.get("exceptional_items"),
                r.error or "",
            ])
    console.print(f"[dim]Results saved to {path}[/dim]")
