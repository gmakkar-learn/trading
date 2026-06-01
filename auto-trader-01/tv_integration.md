# TradingView Integration Plan

## Table of Contents

1. [Overview and Design Philosophy](#1-overview-and-design-philosophy)
2. [Responsibility Split](#2-responsibility-split)
3. [End-to-End Data Flow](#3-end-to-end-data-flow)
4. [TradingView Side](#4-tradingview-side)
   - 4.1 [Pine Script Strategy Structure](#41-pine-script-strategy-structure)
   - 4.2 [Webhook Payload Schema](#42-webhook-payload-schema)
   - 4.3 [Alert Configuration in TradingView](#43-alert-configuration-in-tradingview)
   - 4.4 [Backtesting in Strategy Tester](#44-backtesting-in-strategy-tester)
5. [App Side](#5-app-side)
   - 5.1 [New Components](#51-new-components)
   - 5.2 [Webhook Endpoint](#52-webhook-endpoint)
   - 5.3 [Signal Adapter](#53-signal-adapter)
   - 5.4 [TradingView Source Config](#54-tradingview-source-config)
   - 5.5 [Score-Based Position Sizing](#55-score-based-position-sizing)
   - 5.6 [Alert Log Endpoint](#56-alert-log-endpoint)
   - 5.7 [Embedded Chart Widget](#57-embedded-chart-widget)
6. [Security](#6-security)
7. [HTTPS Setup — ngrok (Phase 2) and EC2 (Phase 3)](#7-https-setup--ngrok-phase-2-and-ec2-phase-3)
8. [Backtesting → Paper → Live Promotion Workflow](#8-backtesting--paper--live-promotion-workflow)
9. [Relationship to Existing TechnicalStrategy](#9-relationship-to-existing-technicalstrategy)
10. [Implementation Tasks](#10-implementation-tasks)
11. [Open Questions](#11-open-questions)

---

## 1. Overview and Design Philosophy

TradingView is the best retail-grade platform for technical analysis: deep Pine Script library,
hundreds of community strategies, high-quality historical data for both NSE/BSE and US markets,
and a built-in Strategy Tester that handles slippage and commission modelling.

Rather than re-implementing technical indicators in Python, we use TradingView as a dedicated
**signal generation** layer. Our app becomes a pure **execution infrastructure** layer:
it receives signals, applies risk rules, places orders, tracks fills, arms stoplosses,
and keeps the audit trail.

This is a clean separation of concerns and mirrors how professional systematic trading desks
are structured — alpha generation is separate from order management and execution.

---

## 2. Responsibility Split

| Concern | Owner | Rationale |
|---|---|---|
| Technical indicator logic | TradingView (Pine Script) | Battle-tested library; visual verification on charts |
| Strategy backtesting | TradingView (Strategy Tester) | Realistic fill simulation; built-in performance metrics |
| Signal generation | TradingView (webhook alerts) | Fires on confirmed bar close; no intra-bar noise |
| Signal receipt and validation | Our app | Authenticate, deduplicate, map to internal contract |
| Risk management | Our app | Position limits, drawdown caps, market-aware checks |
| Order placement | Our app (BrokerAdapter) | Alpaca (US), Upstox (India) |
| Approval workflow | Our app (Telegram) | Manual override for below-threshold signals |
| Fill tracking and stoploss | Our app (MonitorAgent) | Polling and arming logic already built |
| Portfolio and P&L | Our app | Holdings, positions, unrealised P&L |
| Audit log | Our app | Append-only DB; required for SEBI compliance |
| Reporting and dashboard | Our app (React) | Orders, signals, positions in one view |

**What TradingView does NOT do in this integration:**
- It does not know about our positions or portfolio state
- It does not manage risk (no position sizing, no drawdown awareness)
- It does not know whether an order was actually placed or rejected
- It does not track fills or manage stoplosses

All of that stays in our app.

---

## 3. End-to-End Data Flow

```
TradingView Chart (Pine Script strategy running on live bar data)
    │
    │  bar closes → entry/exit condition met
    ▼
TradingView Alert Engine
    │
    │  POST https://<our-domain>/api/webhooks/tradingview
    │  Header: X-TV-Secret: <shared_secret>
    │  Body:   JSON payload (see §4.2)
    ▼
POST /api/webhooks/tradingview  (new endpoint)
    │
    ├── authenticate (secret check)
    ├── validate payload schema
    ├── discard if stale (timestamp > 5 min old)
    ├── look up strategy config by strategy_id
    └── construct TradingSignal
    │
    ▼
EventBus.publish(TradingSignalEvent)
    │
    ▼
RiskGuardAgent  ←── position limits, drawdown cap, market session check
    │
    ▼
TraderAgent
    ├── composite_score >= 80 → auto-execute
    └── composite_score < 80  → Telegram approval request
    │
    ▼
BrokerAdapter
    ├── market_id = "us"     → AlpacaAdapter
    └── market_id = "india"  → UpstoxAdapter
    │
    ▼
MonitorAgent  ←── poll for fill, arm stoploss, send Telegram fill alert
    │
    ▼
AuditLogger   ←── every decision recorded, append-only
```

The pipeline from `EventBus` onwards is **identical** to how fundamental signals flow today.
The only new piece is the webhook endpoint and the signal adapter that converts the TradingView
payload into a `TradingSignal`.

---

## 4. TradingView Side

### 4.1 Pine Script Strategy Structure

Every Pine Script strategy intended for this integration follows a consistent pattern.
The key feature is computing a **composite score** (0–100) from component indicators
and embedding it directly in the alert JSON using `str.tostring()`. This score flows
through to our app for position sizing (see §5.5).

```pine
//@version=5
strategy(
    title             = "GoldenCross_RSI_v1",
    overlay           = true,
    default_qty_type  = strategy.fixed,
    default_qty_value = 1
)

// ── Inputs ─────────────────────────────────────────────────────────────────
fast_len  = input.int(50,  "Fast SMA")
slow_len  = input.int(200, "Slow SMA")
rsi_len   = input.int(14,  "RSI period")
rsi_os    = input.int(30,  "RSI oversold")
rsi_ob    = input.int(70,  "RSI overbought")
macd_fast = input.int(12,  "MACD fast")
macd_slow = input.int(26,  "MACD slow")
macd_sig  = input.int(9,   "MACD signal")

// ── Indicators ─────────────────────────────────────────────────────────────
fast                        = ta.sma(close, fast_len)
slow                        = ta.sma(close, slow_len)
rsi                         = ta.rsi(close, rsi_len)
[macdLine, signalLine, hist] = ta.macd(close, macd_fast, macd_slow, macd_sig)

// ── Component scores (0–100 each) ─────────────────────────────────────────
// Scores are discrete — indicators are either confirming or they're not.
// This is a signal quality multiplier, not a continuous probability.

// SMA: trend direction + separation quality
sma_sep = (fast - slow) / slow * 100
float sma_score = fast > slow ? (sma_sep > 1.0 ? 100.0 : 50.0) : 0.0

// RSI: best entry is near oversold; penalise overbought
float rsi_score = rsi <= rsi_os ? 100.0 :
                  rsi <  40     ? 100.0 :
                  rsi <  55     ? 50.0  : 0.0

// MACD: histogram positive and rising = strongest confirmation
float macd_score = hist > 0 and hist > hist[1] ? 100.0 :
                   hist > 0                     ? 50.0  : 0.0

// Composite (SMA 40% · RSI 30% · MACD 30%)
float composite = math.round(sma_score * 0.40 + rsi_score * 0.30 + macd_score * 0.30, 1)

// ── Entry / exit conditions ────────────────────────────────────────────────
buy_cond  = ta.crossover(fast, slow) and rsi < 55 and composite >= 65.0
sell_cond = ta.crossunder(fast, slow) or rsi > rsi_ob

// ── Strategy calls (for backtesting in Strategy Tester) ───────────────────
if buy_cond
    strategy.entry("Long", strategy.long)

if sell_cond
    strategy.close("Long")

// ── Strategy performance stats (shared by scorecard table and alert payload) ──
// These reflect cumulative backtest performance on this ticker up to the current
// bar — so each ticker's alert carries its own per-ticker track record.
total   = strategy.wintrades + strategy.losstrades
win_pct = total > 0 ? strategy.wintrades / total * 100 : 0.0
pf      = strategy.grossloss != 0 ? strategy.grossprofit / math.abs(strategy.grossloss) : 0.0
avg_win = strategy.wintrades  > 0 ? strategy.grossprofit / strategy.wintrades           : 0.0
avg_los = strategy.losstrades > 0 ? math.abs(strategy.grossloss) / strategy.losstrades  : 0.0
rr      = avg_los != 0 ? avg_win / avg_los : 0.0
dd_pct  = strategy.initial_capital > 0 ? strategy.max_drawdown / strategy.initial_capital * 100 : 0.0
net_pct = strategy.initial_capital > 0 ? strategy.netprofit    / strategy.initial_capital * 100 : 0.0

// ── Alerts (for live signal delivery) ─────────────────────────────────────
// alert() with string concatenation embeds the computed score and stats into
// the JSON. alert.freq_once_per_bar_close prevents intra-bar noise.

_stats() =>
    ',"stats":{'                                           +
    '"net_pct":'  + str.tostring(net_pct,  "#.##")        +
    ',"dd_pct":'  + str.tostring(dd_pct,   "#.##")        +
    ',"win_pct":' + str.tostring(win_pct,  "#.#")         +
    ',"pf":'      + str.tostring(pf,       "#.##")        +
    ',"rr":'      + str.tostring(rr,       "#.##")        +
    ',"trades":'  + str.tostring(total,    "#")           +
    '}'

if buy_cond
    alert(
        '{"strategy_id":"golden_cross_rsi_v1"'            +
        ',"ticker":"'    + syminfo.ticker                  + '"' +
        ',"exchange":"'  + syminfo.prefix                  + '"' +
        ',"action":"BUY"'                                        +
        ',"close":'      + str.tostring(close,     "#.##")       +
        ',"volume":'     + str.tostring(volume,    "#")          +
        ',"score":'      + str.tostring(composite, "#.#")        +
        ',"timeframe":"' + timeframe                       + '"' +
        ',"timestamp":"' + str.format_time(timenow, "yyyy-MM-dd'T'HH:mm:ss'Z'", "UTC") + '"' +
        _stats() + '}',
        alert.freq_once_per_bar_close
    )

if sell_cond
    alert(
        '{"strategy_id":"golden_cross_rsi_v1"'            +
        ',"ticker":"'    + syminfo.ticker                  + '"' +
        ',"exchange":"'  + syminfo.prefix                  + '"' +
        ',"action":"SELL"'                                       +
        ',"close":'      + str.tostring(close,     "#.##")       +
        ',"volume":'     + str.tostring(volume,    "#")          +
        ',"score":'      + str.tostring(composite, "#.#")        +
        ',"timeframe":"' + timeframe                       + '"' +
        ',"timestamp":"' + str.format_time(timenow, "yyyy-MM-dd'T'HH:mm:ss'Z'", "UTC") + '"' +
        _stats() + '}',
        alert.freq_once_per_bar_close
    )
```

**Scoring table — component → score:**

| Component | Condition | Score |
|---|---|---|
| SMA | fast > slow and separation > 1% | 100 |
| SMA | fast > slow but separation ≤ 1% (weak crossover) | 50 |
| SMA | fast ≤ slow (downtrend) | 0 |
| RSI | ≤ 30 (oversold) or < 40 (near oversold — best entry) | 100 |
| RSI | 40–55 (neutral momentum, acceptable) | 50 |
| RSI | ≥ 55 (approaching overbought) | 0 |
| MACD | histogram positive and rising (momentum accelerating) | 100 |
| MACD | histogram positive but falling (momentum waning) | 50 |
| MACD | histogram negative (bearish) | 0 |

Weights: SMA 40% · RSI 30% · MACD 30%. Maximum composite = 100. Minimum to fire a BUY = 65.

**Design rules for Pine Scripts in this integration:**

1. `strategy_id` in the alert message must exactly match a config entry in our app's
   `config/strategies/active.yaml`. If it doesn't match, the webhook discards the alert.

2. Use `alert()` inside `if buy_cond / if sell_cond` blocks with `alert.freq_once_per_bar_close`.
   Do **not** use `alertcondition()` — it only supports static `{{placeholder}}` values and
   cannot embed computed variables like `composite`.

3. The alert message must be a valid JSON string with no line breaks.
   `str.tostring(value, "#.##")` formats numbers without scientific notation.

4. Exit signals (`action: "SELL"`) are sent even if no matching open position exists in our app.
   Our RiskGuard will reject them cleanly — no harm done.

5. The `strategy.entry/close` calls and the `alert()` calls share the same `buy_cond/sell_cond`
   logic. Never diverge them — the backtest must accurately represent what live alerts will fire.

### 4.2 Webhook Payload Schema

This is the standardised JSON our app expects from every TradingView strategy:

```json
{
  "strategy_id": "golden_cross_rsi_v1",
  "ticker":      "RELIANCE",
  "exchange":    "NSE",
  "action":      "BUY",
  "close":       1328.50,
  "volume":      3456789,
  "score":       78.5,
  "timeframe":   "1D",
  "timestamp":   "2026-06-01T10:30:00Z",
  "stats": {
    "net_pct":  34.20,
    "dd_pct":   12.10,
    "win_pct":  58.30,
    "pf":        1.87,
    "rr":        1.92,
    "trades":   47
  }
}
```

`stats` reflects cumulative backtest performance **on this specific ticker** up to the
current bar. Because alerts are set up per ticker, AAPL's signal carries AAPL's track
record; RELIANCE carries RELIANCE's. The app uses this to monitor per-ticker strategy
health over time and can auto-suspend a ticker/strategy pair that is degrading.

| Field | Required | Notes |
|---|---|---|
| `strategy_id` | No | Metadata only — stored in the audit log for tracing. Not used for routing or config lookup. |
| `ticker` | Yes | Exchange-native symbol (RELIANCE not RELIANCE.NS) |
| `exchange` | Yes | Used to resolve `market_id`: NSE/BSE → india, NASDAQ/NYSE/ARCA → us |
| `action` | Yes | `BUY` or `SELL` |
| `close` | Yes | Bar close price at time of alert (numeric) |
| `volume` | No | Bar volume; stored in signal context |
| `score` | Yes | Composite score 0–100 computed by Pine Script. Drives `composite_score`, `confidence` label, and position sizing. Falls back to config `default_score` if absent. |
| `timeframe` | No | Chart timeframe; stored in signal context |
| `timestamp` | Yes | ISO 8601. Alerts older than 5 minutes are discarded as stale. |
| `stats.net_pct` | No | Cumulative net profit % on this ticker since backtest start |
| `stats.dd_pct` | No | Max drawdown % on this ticker |
| `stats.win_pct` | No | Win rate % on this ticker |
| `stats.pf` | No | Profit factor (gross profit / gross loss) |
| `stats.rr` | No | Average win / average loss ratio |
| `stats.trades` | No | Total completed trades on this ticker |

**Exchange → market_id mapping:**

```python
EXCHANGE_MAP = {
    "NSE": "india", "BSE": "india",
    "NASDAQ": "us",  "NYSE": "us", "ARCA": "us", "CBOE": "us",
}
```

### 4.3 Alert Configuration in TradingView

For each strategy and each ticker you want to trade:

1. Open the chart for the ticker (e.g. NSE:RELIANCE, NASDAQ:AAPL)
2. Add the Pine Script strategy to the chart
3. Click the clock icon → **Create Alert**
4. Condition: select the strategy → `strategy.order` (or your named alertcondition)
5. Trigger: **Once Per Bar Close** — critical, prevents intra-bar noise
6. Expiry: set to maximum (1 year or no expiry if available on Premium)
7. Webhook URL: `https://<ngrok-or-ec2-domain>/api/webhooks/tradingview`
8. Message: paste the JSON template from §4.2 (TradingView fills in `{{placeholders}}`)

**One alert = one ticker on one strategy.**
For 10 US tickers + 10 India tickers on one strategy, you set up 20 alerts.
TradingView Premium allows up to ~400 active alerts.

### 4.4 Backtesting in Strategy Tester

The acceptance thresholds below are a **universal template** — the same criteria apply
to every strategy and every ticker. You never redefine them per-strategy; you check
whether a given strategy passes them.

| Metric | Minimum threshold | Why |
|---|---|---|
| Net profit (%) | > 0% on at least 3 years | Confirms positive expectancy |
| Max drawdown % | < 25% | Limits catastrophic loss risk |
| Win rate % | > 40% | Combined with risk/reward, net positive |
| Avg win / avg loss | > 1.5 | Wins must be larger than losses |
| Total trades | > 30 | Enough trades to be statistically meaningful |
| Profit factor | > 1.3 | Gross profit / gross loss |

Run on:
- **US equities**: 5 years of daily data (2019–2024 minimum)
- **India equities**: 3 years minimum (NSE data on TradingView goes back to ~2010)

Walk-forward test: verify the strategy performs on the last 12 months
(data it was not optimised on) — this is the most important test.

**Promotion scorecard (embed in every strategy script):**

Rather than reading the Strategy Tester numbers manually, embed this block in every
Pine Script strategy. It renders a PASS/FAIL table directly on the chart — open the
chart, check for an all-green table, done. No per-strategy changes are needed; the
thresholds are hardcoded in the snippet.

The variables (`net_pct`, `dd_pct`, `win_pct`, `pf`, `rr`, `total`) are the same ones
embedded in the webhook alert payload (§4.1), so the scorecard and the live signal stats
are always consistent.

```pine
// ── Promotion criteria scorecard ─────────────────────────────────────────
// Include this block verbatim in every strategy. No per-strategy changes needed.
var table sc = table.new(position.top_right, 3, 8, bgcolor=color.new(color.black, 75), border_width=1)

_row(row, lbl, val, pass) =>
    clr = pass ? color.green : color.red
    table.cell(sc, 0, row, lbl,                      text_color=color.white, text_size=size.small)
    table.cell(sc, 1, row, str.tostring(val, "#.##"), text_color=clr,        text_size=size.small)
    table.cell(sc, 2, row, pass ? "✓" : "✗",         text_color=clr,        text_size=size.small)

if barstate.islast
    table.cell(sc, 0, 0, "Metric",    text_color=color.gray, text_size=size.small)
    table.cell(sc, 1, 0, "Value",     text_color=color.gray, text_size=size.small)
    table.cell(sc, 2, 0, "Pass?",     text_color=color.gray, text_size=size.small)
    _row(1, "Net profit %",   net_pct,  net_pct  > 0)
    _row(2, "Max drawdown %", dd_pct,   dd_pct   < 25)
    _row(3, "Win rate %",     win_pct,  win_pct  > 40)
    _row(4, "Avg win/loss",   rr,       rr       > 1.5)
    _row(5, "Profit factor",  pf,       pf       > 1.3)
    _row(6, "Total trades",   total,    total    > 30)
```

TradingView has no API to export backtest results, so the final promotion sign-off
is still a human look at the chart. The scorecard eliminates the manual computation
step — you are just checking for an all-green table before clicking "promote".

---

## 5. App Side

### 5.1 New Components

```
api/routers/tradingview.py                    ← webhook endpoint (new)
agents/strategy_engine/strategies/
    tradingview/
        __init__.py                           (new)
        adapter.py                            ← payload → TradingSignal (new)
config/strategies/
    tradingview.yaml                          ← single source config for all TV signals (new)
```

`active.yaml` gets **one entry** covering all TradingView strategies:

```yaml
  - id: tradingview
    type: webhook
    enabled: true
    markets: [us, india]
    config_file: strategies/tradingview.yaml
```

The app has no knowledge of individual Pine Script strategies. `strategy_id` in the
payload is metadata stored in the audit log — adding, removing, or renaming a Pine Script
strategy in TradingView requires zero changes to the app or its config.

No changes to `EventBus`, `RiskGuardAgent`, `TraderAgent`, `MonitorAgent`, or any broker adapter.

### 5.2 Webhook Endpoint

```
POST /api/webhooks/tradingview
```

Processing steps:

1. **Authenticate** — check secret query param against `TRADINGVIEW_WEBHOOK_SECRET` env var.
   Return HTTP 403 on mismatch. Log every auth failure with source IP.

2. **Parse** — deserialise JSON body. Return HTTP 400 if malformed or missing required fields
   (`ticker`, `exchange`, `action`, `close`, `timestamp`).

3. **Stale check** — parse `timestamp`; discard (HTTP 200 + log) if alert is more than
   5 minutes old. Return 200 not 4xx — TradingView retries on non-2xx responses, which
   would cause duplicate orders.

4. **Market resolution** — map `exchange` to `market_id` using the exchange map.
   Discard if exchange is not recognised.

5. **Build TradingSignal** — use signal adapter (§5.3).

6. **Publish** — `await bus.publish(TradingSignalEvent(signal=signal))`.

7. **Log to alert log** — append to in-memory circular buffer (last 100 alerts) for
   the diagnostic endpoint (§5.6).

8. **Return HTTP 200** — always return 200 on successful receipt, even if the alert was
   stale or discarded. Returning 4xx would cause TradingView to retry.

### 5.3 Signal Adapter

```python
# agents/strategy_engine/strategies/tradingview/adapter.py

_DEFAULTS = {"default_score": 85.0, "min_score": 60.0}

def build_signal(payload: dict, source_cfg: dict) -> TradingSignal:
    action = payload["action"].upper()  # "BUY" or "SELL"

    # Score from Pine Script payload; fall back to source config default.
    score = float(payload.get("score") or source_cfg.get("signal", _DEFAULTS)
                  .get("default_score", _DEFAULTS["default_score"]))

    # Derive confidence label from score
    if score >= 80:
        confidence = "high"
    elif score >= 60:
        confidence = "medium"
    else:
        confidence = "low"

    # Execution params: payload overrides take precedence over source config defaults.
    # Pine Script can send e.g. "stoploss_pct": 0.08 for strategies with wider stops.
    exec_cfg  = source_cfg.get("execution", {})
    stoploss  = float(payload.get("stoploss_pct",  exec_cfg.get("stoploss_pct",  0.05)))
    target    = float(payload.get("target_pct",    exec_cfg.get("target_pct",    0.12)))
    slippage  = float(payload.get("slippage_pct",  exec_cfg.get("limit_price_slippage", 0.003)))

    stats      = payload.get("stats") or {}
    strategy_id = payload.get("strategy_id", "tradingview")  # metadata only

    return TradingSignal(
        ticker             = payload["ticker"].upper(),
        market_id          = _resolve_market(payload["exchange"]),
        strategy_type      = "technical",
        strategy_id        = strategy_id,
        composite_score    = score,
        recommended_action = action,
        confidence         = confidence,
        rationale          = (
            f"TradingView {strategy_id} — "
            f"{action} @ {payload['close']} "
            f"(score={score:.0f}, tf={payload.get('timeframe','?')}, "
            f"pf={stats.get('pf', '?')}, trades={stats.get('trades', '?')})"
        ),
        context = {
            "source":       "tradingview",
            "close":        payload["close"],
            "volume":       payload.get("volume"),
            "timeframe":    payload.get("timeframe"),
            "exchange":     payload["exchange"],
            "score":        score,
            "stoploss_pct": stoploss,
            "target_pct":   target,
            "slippage_pct": slippage,
            "tv_timestamp": payload["timestamp"],
            "stats":        stats,
        },
    )
```

The adapter takes `source_cfg` (the single `tradingview.yaml`) — not a per-strategy
config. `strategy_id` from the payload is captured as metadata for the audit log;
it has no routing or config-lookup role.

Execution params cascade: **payload value → `tradingview.yaml` default → hardcoded fallback**.
This means a Pine Script strategy that needs wider stops can send `"stoploss_pct": 0.08`
in the payload and the app honours it without any config change.

**Future: auto-suspension.** `RiskGuardAgent` can check `stats.dd_pct` against the §4.4
threshold and reject signals from a degrading ticker/strategy pair. Phase 3+ addition —
paper trading monitors degradation manually via the signal log.

### 5.4 TradingView Source Config

One YAML file covers **all** TradingView signals regardless of which Pine Script strategy
fired them. Adding, removing, or renaming a strategy in TradingView requires no change here.

```yaml
# config/strategies/tradingview.yaml
version: "1.0"

signal:
  default_score: 85    # used only when payload omits 'score' (e.g. legacy alertcondition())
  min_score:     60    # discard signals below this before they reach the pipeline

execution:
  stoploss_pct:          0.05   # 5% stoploss — overridable per-signal via payload
  target_pct:            0.12   # 12% profit target — overridable per-signal via payload
  limit_price_slippage:  0.003  # 0.3% above ask for BUY limit orders
```

**Override at signal time (not at config time):**
If a specific Pine Script strategy warrants different execution params, send them in the
payload — the adapter gives payload values priority over these defaults:

```pine
// In the Pine Script alert message:
',"stoploss_pct":0.08,"target_pct":0.18'   // wider stops for a volatile strategy
```

This keeps strategy-specific knowledge in TradingView where it belongs.

### 5.5 Score-Based Position Sizing

The `composite_score` from Pine Script determines both **whether** an order is placed and
**how large** that order is. This is implemented in `RiskGuardAgent` as a multiplier on
`max_position_value` from the risk config.

**Sizing tiers:**

| Score range | Confidence | Position size | Execution path |
|---|---|---|---|
| ≥ 85 | high | 100% of `max_position_value` | Auto-execute |
| 70–84 | high | 60% of `max_position_value` | Auto-execute |
| 60–69 | medium | 30% of `max_position_value` | Telegram approval |
| < 60 | low | — | Discarded (`min_score` gate) |

**Why tiers, not a continuous formula:**
A continuous formula (e.g. `size = base * score / 100`) is harder to reason about and
harder to audit. Tiers are explicit, reviewable in the audit log, and easy to adjust
in config without code changes.

**Config** (`config/risk/default.yaml`) — proposed additions:

```yaml
position_sizing:
  score_tiers:
    - min_score: 85
      size_pct:  1.00   # full position
    - min_score: 70
      size_pct:  0.60
    - min_score: 60
      size_pct:  0.30
```

**Implementation note:** `RiskGuardAgent` already caps position size against
`max_position_value`. The score multiplier is applied before that cap, so the cap
still applies as an absolute ceiling regardless of score.

Exit signals (`SELL`) are not subject to position sizing — they always close the
full open position.

### 5.6 Alert Log Endpoint

A diagnostic endpoint for debugging signal delivery:

```
GET /api/webhooks/tradingview/log?limit=20
```

Returns the last N received alerts with processing outcome:

```json
{
  "alerts": [
    {
      "received_at": "2026-06-01T10:31:05Z",
      "strategy_id": "golden_cross_rsi_v1",
      "ticker": "RELIANCE",
      "action": "BUY",
      "outcome": "signal_published",
      "signal_id": "abc123"
    },
    {
      "received_at": "2026-06-01T10:29:55Z",
      "strategy_id": "golden_cross_rsi_v1",
      "ticker": "TCS",
      "action": "BUY",
      "outcome": "discarded_stale",
      "age_seconds": 312
    }
  ]
}
```

Useful for confirming: (a) TradingView is reaching our server, (b) alerts are arriving
within the 5-minute freshness window, (c) the payload is well-formed, (d) the score
field is present and sensible.

### 5.7 Embedded Chart Widget

TradingView's free chart widget can be embedded directly in the React dashboard,
giving a live candlestick chart for any watchlist ticker without leaving the app.
Pine Script execution and alert management still require the full TradingView site
(opened in a new tab via a direct link), but chart viewing and signal review stay
in-app.

**What the embedded widget provides:**
- Live OHLCV candlestick chart (daily, 4H, 1H, etc.)
- Built-in indicators (SMA, RSI, MACD, Volume) — same as TradingView.com
- NSE, BSE, NASDAQ, NYSE tickers all supported
- Dark theme, interactive zoom/pan, drawing tools

**Custom signal marks:**
The widget API supports `createShape()` — we overlay our own BUY/SELL signals as
coloured arrows directly on the candlestick chart. A reviewer can instantly see
whether the signal fired at a sensible price and market condition.

**React component pattern:**

```jsx
// frontend/src/components/TvChart.jsx
import { useEffect, useRef } from "react";

export function TvChart({ ticker, market }) {
  const containerRef = useRef();

  useEffect(() => {
    const script = document.createElement("script");
    script.src = "https://s3.tradingview.com/tv.js";
    script.async = true;
    script.onload = () => {
      new window.TradingView.widget({
        container_id: containerRef.current.id,
        symbol: market === "india" ? `NSE:${ticker}` : `NASDAQ:${ticker}`,
        interval: "D",
        theme: "dark",
        style: "1",          // candlesticks
        locale: "en",
        toolbar_bg: "#1a1a2e",
        enable_publishing: false,
        hide_side_toolbar: false,
        autosize: true,
      });
    };
    document.head.appendChild(script);
  }, [ticker, market]);

  return <div id="tv-chart-container" ref={containerRef} style={{ height: 500 }} />;
}
```

**Dashboard placement:**
The chart sits in a "Chart" tab on the ticker detail view (alongside Orders, Signals,
and Positions tabs for that ticker). A dropdown lets the user switch between watchlist
tickers without navigating away.

A "Open in TradingView" button next to the chart links directly to
`https://www.tradingview.com/chart/?symbol=NSE:RELIANCE` for alert management and
Strategy Tester access.

---

## 6. Security

### Shared webhook secret

TradingView lets you add a custom header or embed a token in the message body.
We use a header approach:

- Secret stored as `TRADINGVIEW_WEBHOOK_SECRET` in `.env` (dev) / AWS Secrets Manager (Phase 3)
- TradingView webhook URL includes the secret as a query param:
  `https://<domain>/api/webhooks/tradingview?secret=<token>`
  (TradingView does not support custom request headers, so query param is the standard approach)
- Our endpoint reads `request.query_params["secret"]` and compares with constant-time equality
  to prevent timing attacks

### Additional hardening (Phase 3 / EC2)

- TradingView publishes its webhook source IP ranges. Optionally allowlist these at the
  nginx / security group level for an extra layer.
- Rate limit the endpoint: max 60 requests/minute. Legitimate alert volume is far lower.
- All auth failures logged with IP, timestamp, and payload snippet for incident review.

---

## 7. HTTPS Setup — ngrok (Phase 2) and EC2 (Phase 3)

TradingView requires an HTTPS URL for webhooks. Plain `http://localhost` is rejected.

### Phase 2 — Local development with ngrok

```bash
# Install ngrok (one-time)
brew install ngrok

# Expose local port 8000 with a stable subdomain (requires ngrok account)
ngrok http 8000 --domain=<your-static-subdomain>.ngrok-free.app
```

The HTTPS URL (`https://<subdomain>.ngrok-free.app`) is pasted into the TradingView
alert webhook field. ngrok forwards HTTPS → HTTP to your local server.

**Ngrok free tier** gives one static domain, which is all we need. The URL stays
the same across restarts so you don't have to update TradingView alerts each session.

Workflow for a dev session:
```bash
# Terminal 1: start the app
uv run uvicorn api.main:app --host 0.0.0.0 --port 8000

# Terminal 2: start ngrok tunnel
ngrok http 8000 --domain=<your-subdomain>.ngrok-free.app

# Then set your TradingView alert webhook URL to:
# https://<your-subdomain>.ngrok-free.app/api/webhooks/tradingview?secret=<token>
```

### Phase 3 — EC2 deployment

- EC2 `t3.small` with Elastic IP (already in the Phase 3 plan)
- nginx reverse proxy: HTTPS (port 443, ACM cert via Let's Encrypt) → HTTP (port 8000)
- TradingView webhook URL becomes: `https://<your-domain>/api/webhooks/tradingview?secret=<token>`
- No change to Pine Script or app code — only the URL in TradingView alert config changes

---

## 8. Backtesting → Paper → Live Promotion Workflow

```
┌───────────────────────────────────────────────────────────┐
│ STAGE 1: BACKTEST (TradingView only)                      │
│                                                           │
│  Write Pine Script strategy                               │
│  Run Strategy Tester: 3–5 years history                   │
│  Must pass all thresholds in §4.4                         │
│  Walk-forward test: last 12 months held out               │
│                                                           │
│  ✅ Pass → proceed   ❌ Fail → revise strategy             │
└───────────────────────────────────────────────────────────┘
                         │
                         ▼
┌───────────────────────────────────────────────────────────┐
│ STAGE 2: PAPER TRADING (both systems active)              │
│                                                           │
│  Lower min_score in tradingview.yaml (e.g. 72) so every  │
│    signal routes to Telegram approval — review each one   │
│  Confirm tradingview entry enabled in active.yaml         │
│  Configure TradingView alerts with ngrok/EC2 URL          │
│  Run minimum 4 weeks on paper                             │
│                                                           │
│  Review:                                                  │
│  - Signal timing: does live match backtest entry points?  │
│  - Fill quality: slippage within expected range?          │
│  - False signals: any unexpected BUY/SELL in flat market? │
│  - Alert delivery: are all alerts arriving < 5 min?       │
│                                                           │
│  ✅ Pass → proceed   ❌ Issues → debug / revise            │
└───────────────────────────────────────────────────────────┘
                         │
                         ▼
┌───────────────────────────────────────────────────────────┐
│ STAGE 3: LIVE TRADING (small size)                        │
│                                                           │
│  Set buy_score: 85 (auto-execute)                         │
│  Switch broker from paper → live in market config         │
│  Start at 25% of target position size (risk config)       │
│  Monitor every trade manually for 4 weeks                 │
│  Gradually increase position size as confidence builds    │
└───────────────────────────────────────────────────────────┘
```

**Config changes at each promotion stage** (no code changes required):

| Stage | `active.yaml` | `tradingview.yaml` `default_score` | Broker config |
|---|---|---|---|
| Backtest | n/a (TradingView only) | n/a | n/a |
| Paper | enabled: true | 72 → Telegram approval on every signal | alpaca_paper / upstox_sandbox |
| Live small | enabled: true | 85 → auto-execute | alpaca_live / upstox_live |
| Live full | enabled: true | 85 → auto-execute | alpaca_live / upstox_live |

---

## 9. Relationship to Existing TechnicalStrategy

Our built-in `TechnicalStrategy` (`technical_v1`, currently disabled) computes the same
class of indicators — SMA crossover, RSI, MACD — but does so in Python via pandas-ta.

Once TradingView integration is live, there are three options:

**Option A — TradingView replaces TechnicalStrategy (recommended initially)**
Keep `technical_v1` disabled. Use TradingView as the sole source of technical signals.
Simpler, less noise, no risk of conflicting signals.

**Option B — Run both, require agreement (Phase 4 consideration)**
Enable `technical_v1`. Create a `HybridTechnicalStrategy` that only emits a signal
when both TradingView AND our internal strategy agree on direction. More conservative,
fewer false positives, but adds latency since our candle poll is daily.

**Option C — Use TechnicalStrategy as a validator**
Keep `technical_v1` running silently (not emitting orders). Log when it disagrees
with TradingView signals. Use divergences as a debugging tool to identify when
TradingView strategy parameters need review.

Recommendation: start with Option A, revisit Option B once we have 3+ months of
live TradingView signal data to compare.

---

## 10. Implementation Tasks

Tasks are ordered. Each is a discrete unit of work (half a day or less).

### Phase 2 — Local with ngrok

| # | Task | Files affected |
|---|---|---|
| 1 | Add `TRADINGVIEW_WEBHOOK_SECRET` to `.env` | `.env` |
| 2 | Create `tradingview.yaml` source config (signal thresholds + execution defaults) | `config/strategies/tradingview.yaml` |
| 3 | Add single `tradingview` webhook entry to `active.yaml` | `config/strategies/active.yaml` |
| 4 | Create signal adapter (`source_cfg` only; `strategy_id` as metadata; payload execution overrides) | `agents/strategy_engine/strategies/tradingview/__init__.py`, `adapter.py` |
| 5 | Create webhook endpoint (auth, parse, stale check, market resolution, publish) | `api/routers/tradingview.py` |
| 6 | Wire router into `api/main.py` | `api/main.py` |
| 7 | Add `alert_log` list to `AppState` | `api/state.py` |
| 8 | Add score-based position sizing tiers to `RiskGuardAgent`; add `position_sizing.score_tiers` to `config/risk/default.yaml` | `agents/risk_guard/agent.py`, `config/risk/default.yaml` |
| 9 | Add `GET /api/webhooks/tradingview/log` diagnostic endpoint | `api/routers/tradingview.py` |
| 10 | Add `GET /api/webhooks/tradingview/setup` helper — returns pre-filled webhook URL and alert message JSON per watchlist ticker | `api/routers/tradingview.py` |
| 11 | Write unit tests: adapter, stale check, auth, exchange mapping, score→sizing tiers, payload execution overrides | `tests/strategies/test_tradingview.py` |
| 12 | Set up ngrok with static subdomain; use setup endpoint to configure TradingView alerts | manual / README |
| 13 | Build `TvChart` React component; add "Chart" tab to ticker detail view with "Open in TradingView" link | `frontend/src/components/TvChart.jsx` |
| 14 | Set `default_score: 72` in `tradingview.yaml`; run paper test end-to-end | `config/strategies/tradingview.yaml` |

### Phase 3 — EC2 deployment

| # | Task | Notes |
|---|---|---|
| 15 | Update nginx config with HTTPS for webhook URL | Server config |
| 16 | Move `TRADINGVIEW_WEBHOOK_SECRET` to AWS Secrets Manager | Follows existing secrets pattern |
| 17 | Update TradingView alert webhook URLs to EC2 domain | TradingView UI |
| 18 | Add EC2 IP allowlisting for TradingView source IPs (optional) | nginx / security group |

---

## 11. Open Questions

These need decisions before or during implementation:

| Question | Options | Recommendation |
|---|---|---|
| Which tickers to run first | Full watchlist vs subset | Start with 3 US tickers (AAPL, MSFT, NVDA) to validate end-to-end before scaling |
| `default_score` during paper phase | 72 (Telegram approval) vs 85 (auto-execute) | 72 — want manual review of every paper signal for the first 4 weeks |
| Exit signal handling | TradingView sends SELL alert; app closes position | Accept SELL alerts only if we have an open position for that ticker; RiskGuard handles this |
| Multi-timeframe | Daily only vs daily + 4H | Daily only to start; 4H produces far more alerts and more noise |
| Alert log retention | In-memory (last 100) vs DB table | In-memory is fine for Phase 2; add DB table in Phase 3 for incident replay |

---

*Document version: 1.2 — June 2026*
*Status: Under review — pending approval before implementation begins*
