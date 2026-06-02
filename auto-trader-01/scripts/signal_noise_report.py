#!/usr/bin/env python3
"""
Signal-to-Noise Ratio Report — pre-live validation #3

Queries the audit log and prints:
  - Total signals generated per market
  - Rejection rate and breakdown by reason
  - Approval rate
  - Order placement rate
  - Distribution by strategy type, action, confidence
  - Signals that passed risk guard but never got an order (stuck?)

Run:
    uv run python scripts/signal_noise_report.py
    uv run python scripts/signal_noise_report.py --market india
    uv run python scripts/signal_noise_report.py --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def run(market_id: str | None, limit: int) -> None:
    from dotenv import load_dotenv
    load_dotenv()

    from infrastructure.database.connection import get_session_factory
    from infrastructure.audit.audit_logger import AuditLogger

    session_factory = get_session_factory()
    audit = AuditLogger(session_factory)

    print(f"\n{'='*60}")
    print(f"  Signal-to-Noise Report  |  market={market_id or 'all'}  limit={limit}")
    print(f"{'='*60}\n")

    history = await audit.get_signal_history(market_id=market_id, limit=limit)

    if not history:
        print("No signals found in audit log.")
        return

    total = len(history)
    dispositions = Counter(s["disposition"] for s in history)
    rejection_reasons = Counter(
        s["rejection_reason"] for s in history if s["disposition"] == "rejected"
    )
    strategies = Counter(s.get("strategy_type", "unknown") for s in history)
    actions = Counter(s.get("recommended_action", "unknown") for s in history)
    confidences = Counter(s.get("confidence", "unknown") for s in history)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"Total signals analysed: {total}\n")

    print("Disposition breakdown:")
    for disposition, count in sorted(dispositions.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        bar = "█" * int(pct / 2)
        status = "✓" if disposition in ("approved", "order_placed") else "✗" if disposition == "rejected" else "·"
        print(f"  {status} {disposition:<15} {count:>4}  ({pct:5.1f}%)  {bar}")

    approved = dispositions.get("approved", 0) + dispositions.get("order_placed", 0)
    rejected = dispositions.get("rejected", 0)
    pass_rate = approved / total * 100 if total else 0
    print(f"\n  Signal pass rate: {pass_rate:.1f}%  ({approved}/{total})")

    order_rate = dispositions.get("order_placed", 0) / total * 100 if total else 0
    print(f"  Order placement rate: {order_rate:.1f}%  ({dispositions.get('order_placed',0)}/{total})")

    # ── Rejection breakdown ───────────────────────────────────────────────────
    if rejection_reasons:
        print(f"\nRejection reasons ({rejected} total):")
        for reason, count in sorted(rejection_reasons.items(), key=lambda x: -x[1]):
            pct = count / rejected * 100
            print(f"  {reason:<55} {count:>4}  ({pct:5.1f}%)")

    # ── Strategy / action / confidence breakdown ──────────────────────────────
    print(f"\nBy strategy:    {dict(strategies)}")
    print(f"By action:      {dict(actions)}")
    print(f"By confidence:  {dict(confidences)}")

    # ── Score distribution ────────────────────────────────────────────────────
    scores = [s.get("composite_score", 0) for s in history if s.get("composite_score")]
    if scores:
        buckets = Counter(int(s // 10) * 10 for s in scores)
        print("\nScore distribution (10-point buckets):")
        for bucket in sorted(buckets):
            count = buckets[bucket]
            bar = "█" * count
            print(f"  {bucket:>3}-{bucket+9}  {bar}  ({count})")

    # ── Stuck signals: approved but no order ─────────────────────────────────
    stuck = [s for s in history if s["disposition"] == "approved"]
    if stuck:
        print(f"\n⚠  {len(stuck)} signal(s) approved but no order placed:")
        for s in stuck[:10]:
            print(f"   {s.get('created_at','?')[:19]}  {s['ticker']:<8} {s.get('recommended_action')}  score={s.get('composite_score')}")
        if len(stuck) > 10:
            print(f"   ... and {len(stuck)-10} more")

    # ── Recent high-score signals ─────────────────────────────────────────────
    high_score = sorted(
        [s for s in history if (s.get("composite_score") or 0) >= 70],
        key=lambda x: x.get("composite_score", 0), reverse=True
    )[:10]
    if high_score:
        print(f"\nTop {len(high_score)} high-score signals (score ≥ 70):")
        print(f"  {'Time':<20} {'Ticker':<8} {'Action':<6} {'Score':>5}  {'Conf':<7} {'Disposition'}")
        print(f"  {'-'*65}")
        for s in high_score:
            ts = (s.get("created_at") or s.get("received_at") or "?")[:19]
            print(
                f"  {ts:<20} {s['ticker']:<8} {s.get('recommended_action','?'):<6} "
                f"{s.get('composite_score',0):>5.1f}  {s.get('confidence','?'):<7} "
                f"{s['disposition']}"
            )

    print(f"\n{'='*60}\n")

    # ── Pass/fail verdict for pre-live gate ───────────────────────────────────
    print("Pre-live gate assessment:")
    issues = []

    if total < 20:
        issues.append(f"  ✗ Only {total} signals observed — need ≥20 before going live")
    else:
        print(f"  ✓ Signal volume: {total} signals observed")

    if pass_rate > 90:
        issues.append(f"  ✗ Pass rate {pass_rate:.0f}% is suspiciously high — risk guard may be too loose")
    elif pass_rate == 0 and total > 5:
        issues.append(f"  ✗ Pass rate is 0% — all signals rejected, system will never trade")
    else:
        print(f"  ✓ Pass rate {pass_rate:.1f}% looks reasonable")

    if stuck:
        issues.append(f"  ✗ {len(stuck)} approved signal(s) with no order — investigate TraderAgent")

    if issues:
        print("\nIssues to resolve before going live:")
        for issue in issues:
            print(issue)
    else:
        print("  ✓ No blocking issues found")

    print()


def main():
    parser = argparse.ArgumentParser(description="Signal-to-noise ratio report")
    parser.add_argument("--market", default=None, help="Filter by market id (us/india)")
    parser.add_argument("--limit", type=int, default=200, help="Max signals to analyse")
    args = parser.parse_args()
    asyncio.run(run(args.market, args.limit))


if __name__ == "__main__":
    main()
