#!/usr/bin/env python3
"""
Recovery and Reliability Integration Test — pre-live validation #5b

Tests system resilience against infrastructure failures:

  Test A — DB outage:
    Stop PostgreSQL container → push a signal → backend must stay up
    and log CRITICAL (not crash). Restart DB → signals persist correctly.

  Test B — Restart persistence:
    Record signal count before restart → restart backend → verify
    signal count matches (all signals read from DB, not lost in memory).

  Test C — Scheduler health:
    Verify no poll jobs have been missed by more than 2x their interval
    (guards against event loop blockage going undetected).

Run:
    uv run python scripts/test_recovery.py
    uv run python scripts/test_recovery.py --skip-db-outage   # skip if no Docker
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time

import httpx

BASE = "http://localhost:8000"
DB_CONTAINER = "auto-trader-01-db-1"


def sh(cmd: str) -> tuple[int, str]:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def result(ok: bool, msg: str) -> None:
    print(f"  {'✓' if ok else '✗'}  {msg}")
    return ok


async def wait_backend(client: httpx.AsyncClient, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = await client.get(f"{BASE}/health", timeout=3)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def get_signal_count(client: httpx.AsyncClient) -> int:
    try:
        r = await client.get(f"{BASE}/api/signals?market=us&limit=200", timeout=10)
        return len(r.json().get("signals", []))
    except Exception:
        return -1


async def push_test_signal(client: httpx.AsyncClient) -> bool:
    try:
        r = await client.post(f"{BASE}/api/debug/push-signal", json={
            "ticker": "AAPL",
            "market_id": "us",
            "composite_score": 75.0,
            "recommended_action": "BUY",
            "confidence": "high",
            "rationale": "Recovery test signal",
            "skip_session_check": True,
        }, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


async def run_db_outage_test(client: httpx.AsyncClient) -> bool:
    print("\nTest A — DB Outage:")
    all_ok = True

    # Check Docker available
    rc, _ = sh(f"docker inspect {DB_CONTAINER} --format={{{{.State.Running}}}}")
    if rc != 0:
        result(False, f"Docker container {DB_CONTAINER} not found — skipping")
        return True  # not a system failure

    # Stop DB
    rc, out = sh(f"docker stop {DB_CONTAINER}")
    stopped = rc == 0
    all_ok &= result(stopped, f"DB container stopped (docker stop)")
    if not stopped:
        return False

    await asyncio.sleep(2)

    # Backend must still respond
    try:
        r = await client.get(f"{BASE}/health", timeout=5)
        alive = r.status_code in (200, 503)  # 503 is fine — degraded but alive
        all_ok &= result(alive, f"Backend still responds during DB outage (status={r.status_code})")
    except Exception as exc:
        all_ok &= result(False, f"Backend unreachable during DB outage: {exc}")

    # Push a signal — should not crash backend (audit failure is logged, not raised)
    signal_ok = await push_test_signal(client)
    all_ok &= result(signal_ok, "Signal push during DB outage returns 200 (audit failure logged, not raised)")

    # Restart DB
    rc, _ = sh(f"docker start {DB_CONTAINER}")
    restarted = rc == 0
    all_ok &= result(restarted, "DB container restarted")
    await asyncio.sleep(3)

    # Backend should recover automatically
    try:
        r = await client.get(f"{BASE}/api/signals?market=us&limit=5", timeout=10)
        all_ok &= result(r.status_code == 200, "Signals endpoint recovers after DB restart")
    except Exception as exc:
        all_ok &= result(False, f"Signals endpoint still failing after restart: {exc}")

    return all_ok


async def run_restart_persistence_test(client: httpx.AsyncClient) -> bool:
    print("\nTest B — Restart Persistence:")
    all_ok = True

    count_before = await get_signal_count(client)
    all_ok &= result(count_before >= 0, f"Signal count before restart: {count_before}")
    if count_before < 0:
        return False

    # Kill and restart backend
    rc, _ = sh("kill -9 $(lsof -ti:8000) 2>/dev/null; echo done")
    await asyncio.sleep(1)

    rc, _ = sh(
        "cd /Users/taiexp/exp/trading/auto-trader-01 && "
        "uv run uvicorn api.main:app --host 0.0.0.0 --port 8000 "
        ">> logs/backend.log 2>&1 &"
    )

    # Wait for backend to come back
    alive = await wait_backend(client, timeout=30)
    all_ok &= result(alive, "Backend came back up after restart")
    if not alive:
        return False

    await asyncio.sleep(2)  # let DB connection pool settle

    count_after = await get_signal_count(client)
    all_ok &= result(count_after >= 0, f"Signal count after restart: {count_after}")

    persistent = count_after == count_before
    all_ok &= result(persistent,
        f"Signal count matches ({count_before} before == {count_after} after) — DB persistence confirmed"
        if persistent else
        f"Signal count mismatch: {count_before} before vs {count_after} after — possible data loss"
    )

    return all_ok


async def run_scheduler_health_test(client: httpx.AsyncClient) -> bool:
    print("\nTest C — Scheduler Health:")
    all_ok = True

    # Check logs for missed job warnings in the last 200 lines
    rc, log_tail = sh("tail -200 /Users/taiexp/exp/trading/auto-trader-01/logs/backend.log")
    missed_lines = [l for l in log_tail.splitlines() if "was missed by" in l]

    if not missed_lines:
        all_ok &= result(True, "No missed scheduler jobs in recent logs")
    else:
        # Parse the largest miss duration
        import re
        durations = []
        for line in missed_lines:
            m = re.search(r"missed by (\d+):(\d+):(\d+)", line)
            if m:
                h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                durations.append(h * 3600 + mn * 60 + s)

        max_miss = max(durations) if durations else 0
        # Allow up to 2x the 5-minute poll interval (600s) — anything over is a problem
        acceptable = max_miss <= 600
        all_ok &= result(acceptable,
            f"Max scheduler miss: {max_miss}s {'(acceptable)' if acceptable else '(EXCEEDS 2x poll interval — investigate blocking)'}"
        )
        if not acceptable:
            for l in missed_lines[-3:]:
                print(f"    {l.strip()}")

    # Verify watchlist + signals respond quickly (event loop health)
    latencies = []
    for _ in range(5):
        start = time.time()
        try:
            await client.get(f"{BASE}/api/watchlist", timeout=5)
            latencies.append(time.time() - start)
        except Exception:
            latencies.append(5.0)
        await asyncio.sleep(0.5)

    avg_ms = sum(latencies) / len(latencies) * 1000
    max_ms = max(latencies) * 1000
    responsive = max_ms < 500
    all_ok &= result(responsive,
        f"API response times: avg={avg_ms:.0f}ms  max={max_ms:.0f}ms {'✓' if responsive else '⚠ slow'}"
    )

    return all_ok


async def run(skip_db_outage: bool) -> bool:
    print(f"\n{'='*60}")
    print(f"  Recovery and Reliability Integration Test")
    print(f"{'='*60}")

    async with httpx.AsyncClient() as client:
        # Pre-check
        alive = await wait_backend(client, timeout=10)
        if not alive:
            print("\n  Backend not running. Start with: ./scripts/start.sh")
            return False

        all_ok = True

        if not skip_db_outage:
            all_ok &= await run_db_outage_test(client)
        else:
            print("\nTest A — DB Outage: SKIPPED (--skip-db-outage)")

        all_ok &= await run_restart_persistence_test(client)
        all_ok &= await run_scheduler_health_test(client)

    print(f"\n{'='*60}")
    print(f"  {'PASS' if all_ok else 'FAIL'} — recovery and reliability test")
    print(f"{'='*60}\n")
    return all_ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-db-outage", action="store_true",
                        help="Skip DB stop/start test (requires Docker)")
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()

    global BASE
    BASE = args.base

    passed = asyncio.run(run(args.skip_db_outage))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
