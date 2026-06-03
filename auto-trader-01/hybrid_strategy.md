# HybridStrategy

## Purpose

HybridStrategy emits a BUY signal only when a strong fundamental catalyst and
supportive technical momentum agree on the same ticker. It was added after backtesting
showed that pure fundamental signals had a 37–41% 10-day win rate — losses were
concentrated in macro-driven drawdowns (Q1 2026: DeepSeek shock, tariff volatility)
where technically weak setups dragged down otherwise sound fundamental calls.

The two-gate design means the strategy fires less often but with significantly higher
expected conviction.

---

## Signal Flow

```
CandleEvent arrives
        │
        ▼
Gate 1 — Fundamental
  ├─ Look up signal_cache for (ticker, fundamental_v1)
  ├─ Must exist, action == "BUY", score ≥ fundamental_min_score (70)
  └─ Must be ≤ 168 hours old (fundamental_ttl_hours)
        │
        ▼
Gate 2 — Technical
  ├─ Require ≥ 210 candles (covers 200-day SMA lookback)
  ├─ Score SMA crossover, RSI, MACD (patched in tests; pandas-ta in production)
  └─ Composite technical score ≥ technical_min_score (60)
        │
        ▼
Combined score check
  └─ combined = fundamental × 0.60 + technical × 0.40 ≥ combined_buy (65)
        │
        ▼
Emit TradingSignal (strategy_type="hybrid", strategy_id="hybrid_v1")
```

If any gate fails, the strategy returns `None` and the downstream pipeline never sees
a signal for that event.

---

## Combined Score Formula

```
technical = sma_score × 0.40 + rsi_score × 0.30 + macd_score × 0.30
combined  = fundamental_score × 0.60 + technical × 0.40
```

The fundamental weight is higher (0.60) because the original backtest signal — a
quarterly earnings filing passing the Claude scoring rubric — already implies
significant forward-looking information. Technical confirmation acts as a timing /
macro filter rather than an equal co-signal.

Confidence tiers:

| combined score | confidence |
|---|---|
| ≥ 80 | high |
| ≥ 70 | medium |
| < 70 | low |

---

## Fundamental Signal TTL — 168 hours (7 calendar days)

The fundamental gate uses a 168-hour (7-day) staleness check on `TradingSignal.created_at`.

**Rationale:** Post-earnings price action has a well-documented multi-day consolidation
phase. Strong results are absorbed over 2–5 trading days as institutional repositioning
completes. A fundamental signal is most actionable within this window. Beyond 7 days,
the information edge has been priced in and a new filing cycle has not yet arrived, so
combining a stale fundamental call with fresh technical momentum would be spurious.

**Implementation detail:** `SignalCache` uses an insertion-time TTL of 240 hours
(10 days) so entries are never silently evicted before the strategy can inspect them.
The 168-hour staleness check is applied explicitly inside `HybridStrategy.evaluate()`
using `signal.created_at` (naive UTC), independently of the cache TTL:

```python
ttl_hours = float(self.config.get("fundamental_ttl_hours", 168.0))
age_secs  = (datetime.utcnow() - fund_signal.created_at).total_seconds()
if age_secs > ttl_hours * 3600:
    return None
```

The cache TTL (240h) is intentionally larger than the strategy TTL (168h) so the
strategy — not the cache — is the gatekeeper. This means stale signals remain
inspectable for debugging without silently disappearing.

---

## Configuration (`config/strategies/hybrid.yaml`)

```yaml
fundamental_strategy_id: "fundamental_v1"
fundamental_ttl_hours: 168

weights:
  fundamental: 0.60
  technical:   0.40

thresholds:
  fundamental_min_score: 70   # Gate 1: fundamental BUY must score ≥ this
  technical_min_score:   60   # Gate 2: technical composite must score ≥ this
  combined_buy:          65   # weighted combined must score ≥ this to emit

indicators:
  sma_crossover: { fast: 50, slow: 200 }
  rsi:           { period: 14, oversold: 30, overbought: 70 }
  macd:          { fast: 12, slow: 26, signal: 9 }
  weights:       { sma: 0.40, rsi: 0.30, macd: 0.30 }

execution:
  stoploss_pct: 0.05
  target_pct:   0.10

# Per-market overrides — key must match market_id in events
market_overrides:
  india:
    technical_gate_enabled: false
    fundamental_min_score: 70
```

---

## Market-Aware Technical Gate

`market_overrides` in `hybrid.yaml` lets individual markets disable the technical gate.
When `technical_gate_enabled: false`:

- Gates 2 and 3 (SMA/RSI/MACD + combined score) are skipped entirely.
- `composite_score` = `fundamental_score` (no blending, effective weight = 1.0).
- `context["technical_gate"]` = `"disabled"` (audit trail).
- `fundamental_min_score` can also be overridden per market.

**Why India disables the gate:** A 36-month backtest on 20 NSE mid/small-caps
(`^NSMIDCP` regime filter, ≥70 fundamental score, 39 signals) showed:

| Path | Signals | +10d win rate |
|---|---|---|
| Fundamental-only (≥70) | 39 | **56%** ✓ |
| Hybrid (tech gate on, ≥60) | 6 | 33% ✗ |

The SMA/RSI/MACD gate selected for stocks that had already moved post-earnings
("already-moved" bias). For Indian mid/small-caps, the fundamental edge exists precisely
when technicals are still weak — institutional repositioning takes longer than in US
large-caps. Applying the technical gate removed the edge rather than reinforcing it.

The `us` market retains the technical gate (default `technical_gate_enabled: true`).

---

## Market Regime Interaction

The market regime filter in `RiskGuard` provides an additional upstream layer:

- RiskGuard checks SPY vs its 50-day MA (cached 60 min) before any signal reaches
  order placement.
- A bear regime rejection (`regime:bear_market`) blocks the order regardless of which
  strategy produced the signal.
- HybridStrategy itself does not check the regime — it remains stateless and
  purely signal-producing. Regime is a risk/execution concern, not a signal concern.

In a deep bear market, hybrid signals may still be emitted (both gates can pass) but
RiskGuard will hold them. This is by design: the audit log records the blocked signals
so they can be reviewed if the regime reverses quickly.

---

## Position in the System Pipeline

```
FundamentalStrategy fires → TradingSignal cached in SignalCache
                                        │
                      (next candle event for same ticker)
                                        ▼
                            HybridStrategy reads cache
                            + evaluates SMA/RSI/MACD
                            + emits hybrid TradingSignal
                                        │
                                        ▼
                              RiskGuard (regime + position checks)
                                        │
                                        ▼
                              Trader → BrokerAdapter
```

Both `fundamental_v1` and `hybrid_v1` are enabled for US and India markets. If both
fire on the same ticker in the same cycle, RiskGuard's concentration check
(`already_holding_TICKER`) will accept the first signal and reject the second,
preventing double-entry.

For India, `fundamental_v1` fires on announcement events (populates the cache) and
`hybrid_v1` fires on the next candle event (reads cache, skips tech gate, re-emits).
The hybrid signal carries `strategy_type="hybrid"` so the audit log distinguishes it
from a raw fundamental signal.

---

## Test Coverage

`tests/strategies/test_hybrid.py` — 16 tests:

| Test | What it verifies |
|---|---|
| `test_no_fundamental_signal_no_hybrid` | Cache miss → None |
| `test_fundamental_hold_no_hybrid` | HOLD action → None |
| `test_fundamental_below_min_score_no_hybrid` | score < 70 → None |
| `test_technical_below_min_score_no_hybrid` | tech < 60 → None |
| `test_not_enough_candles_no_hybrid` | < 210 candles → None |
| `test_stale_fundamental_signal_no_hybrid` | signal age > 168h → None |
| `test_combined_score_below_threshold_no_hybrid` | combined < combined_buy → None |
| `test_both_gates_pass_emits_hybrid` | both pass → BUY emitted |
| `test_combined_score_is_weighted_average` | score math correct |
| `test_context_carries_both_scores` | context dict has required fields |
| `test_ticker_mismatch_in_cache_no_signal` | MSFT signal ≠ AAPL event |
| `test_confidence_high_when_combined_ge_80` | confidence tier assignment |
| `test_india_tech_gate_disabled_emits_on_fundamental_alone` | India: weak tech → BUY still emitted |
| `test_india_tech_gate_disabled_uses_fundamental_score_as_combined` | India: combined = fundamental score |
| `test_india_tech_gate_disabled_still_enforces_fund_min_score` | India: score < 70 → None |
| `test_us_market_still_requires_technical_gate` | US unaffected by India override |
