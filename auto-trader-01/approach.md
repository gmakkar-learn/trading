# Auto Trader 01 — Approach Document

**Project:** Event-Driven Algorithmic Trading System — Indian and US Equity Markets
**Universe:** NSE / BSE (India) · NYSE / NASDAQ (US)
**Strategy:** Trigger-based trading on corporate events, technical indicators, or both combined
**Date:** May 2026
**Revision:** 3 — multi-market, free-first, pluggable strategy architecture, config-driven watchlist, local-dev-first secrets

> This document captures the strategic thinking, feasibility assessment, regulatory context,
> and key decisions made before implementation began. For the full technical design, module
> structure, and implementation plan, see `design.md`.

---

## 1. Core Thesis

React to known catalysts — corporate results, technical crossovers, or a combination of both
— faster than the broader market can fully price them in. The edge is not raw speed (institutions
are faster on large-caps) but the quality of signal interpretation: distinguishing a genuine
earnings beat from a headline-inflated number, and acting with discipline on a well-scored signal.

This works best when:
- The stock has meaningful liquidity but is not so large that institutions front-run every move
- The trigger signal is genuinely informative — not just "revenue up YoY"
- Entry timing is controlled (pre-open auction vs. market open vs. N minutes after)
- Exit strategy is defined before entry — target price, stop-loss, or time-based exit

Without a defined exit strategy, the system has an entry engine, not a trading system.

---

## 2. Markets in Scope

### India — NSE / BSE
- Primary trigger: BSE/NSE quarterly result filings (FinancialResults, BoardMeeting)
- Secondary triggers: technical indicator crossovers (e.g. 50DMA / 200DMA), hybrid signals
- Broker path: Upstox Sandbox (paper) → Upstox Live → Zerodha (primary live)
- Regulatory framework: SEBI February 2025 circular on retail algo trading

### United States — NYSE / NASDAQ
- Primary trigger: SEC EDGAR 8-K filings (earnings releases)
- Secondary triggers: same technical strategies as India
- Broker path: Alpaca Paper → Alpaca Live
- Regulatory framework: No equivalent algo registration requirement for retail

Both markets run on the same codebase. Market-specific behaviour (timezone, session times,
circuit breakers, tax rules, broker) is encapsulated in a `MarketContext` configuration object.

---

## 3. Strategy Types

The system is designed to be **open for extension, closed for modification**. Each strategy
is a self-contained plugin. Adding a new strategy type requires writing one Python class and
one YAML config file — nothing else in the system changes.

| Strategy | Trigger | Uses AI | Status |
|---|---|---|---|
| Fundamental | BSE/NSE/EDGAR filing → PDF extraction → Claude Sonnet scoring | Yes | Phase 1 |
| Technical | OHLCV candles → indicator calculation → crossover/threshold detection | No | Phase 2 |
| Hybrid | Fundamental signal + technical confirmation — both must agree | Partial | Phase 3 |
| Future | Sentiment, macro, volume anomaly, options flow — add as new plugins | TBD | Phase 4+ |

All strategies produce the same `TradingSignal` object. The downstream pipeline — Risk Guard,
Trader, Monitor — is completely decoupled from strategy internals and never changes regardless
of which strategy fires.

---

## 4. Feasibility Assessment

### Is this feasible?

Yes. The Indian and US market infrastructure supports event-driven retail algo trading. The
broker API ecosystem (Upstox, Zerodha, Alpaca) provides programmatic order placement with
paper trading environments for validation before going live.

### Where the real edge lies

The automation itself is not the moat — institutions automate faster. The genuine edge is
in the **quality of trigger interpretation**. A naive rule ("PAT up YoY → BUY") will
misread results that have exceptional items, margin compression, or a guidance cut buried
below the headline. An AI-powered interpretation — distinguishing a real beat from a
one-time distortion — is harder to commoditise and closer to what a skilled analyst does.

### Limitations to design around

- **You won't be first on large-caps.** Institutional algos react to Nifty 50 results within
  seconds. The system's realistic edge is in mid-cap and small-cap stocks where price
  discovery is slower.
- **Pre-open auction already reflects news.** Results dropped after 15:30 are partially
  priced in during the 09:00–09:15 pre-open. The system fires limit orders into pre-open,
  not waiting for the 09:15 continuous session.
- **Circuit breakers block entry.** Stocks hitting upper circuit after a strong result
  cannot be bought. The system detects this and logs a missed trade rather than chasing.
- **Break-even is ~0.30% per round trip** (India) after all charges. Every trade must
  move more than this in your favour just to cover costs.

---

## 5. Regulatory Context

### India — SEBI February 2025 Circular

Algo trading for retail investors is permitted but regulated under SEBI Circular
`SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013`, enforced from April 1, 2026.

| Requirement | Detail |
|---|---|
| Static IP | All order API calls must originate from a registered static IP. Deployed on AWS EC2 with Elastic IP. |
| Algo ID tagging | Every order must carry an exchange-assigned Algo ID. Handled by Upstox/Zerodha at the API level. |
| Algo registration | Required only if placing > 10 orders per second. This system places 2–5 orders per day — below threshold. |
| Family-only use | A self-built algo may be used by the builder and immediate family only. |
| OAuth + 2FA | Mandatory. Implemented via `pyotp` TOTP with secret stored in AWS Secrets Manager. |

### US — No equivalent retail algo restriction

No SEBI-equivalent registration requirement for retail algo trading in the US. Standard
brokerage terms and SEC rules apply.

---

## 6. Tax Implications

### India

| Scenario | Tax treatment |
|---|---|
| Intraday (buy and sell same day) | Speculative business income — taxed at slab rate |
| Delivery, held < 1 year | STCG at 20% flat |
| Delivery, held > 1 year | LTCG at 12.5% above ₹1.25L exemption |

**Design decision:** All India orders use `product_type = CNC` (delivery) to avoid speculative
income classification. The tax ledger warns before any exit that would result in STCG when
waiting a few more days would qualify for LTCG treatment.

### India Transaction Costs (round trip, approximate)

| Charge | Rate |
|---|---|
| Brokerage (Zerodha / Upstox) | ₹20 per order or 0.03%, whichever is lower |
| STT (buy + sell, delivery) | 0.1% on both sides |
| Exchange transaction charges | ~0.00335% (NSE) |
| GST on brokerage + charges | 18% |
| SEBI turnover fee | ₹10 per crore |
| Stamp duty | 0.015% on buy side |
| **Total round-trip** | **~0.25–0.30%** |

### US
Short-term gains (< 1 year) taxed as ordinary income. Long-term gains (> 1 year) at
0/15/20% depending on income bracket. Wash-sale rule applies (30-day window). Exact
rates are user-specific and configured in `config/markets/us.yaml`.

---

## 7. Broker and Platform Strategy

### Why not third-party platforms (AmiBroker, Streak, Tradetron)?

These platforms are the right tool for technical-indicator-driven, no-code strategies.
They are the wrong tool for this system because:

- None support BSE/NSE announcement parsing and AI-driven signal extraction
- Their paper trading engines simulate fills at last-traded price — too optimistic for
  event-driven trades into potentially illiquid or circuit-hitting stocks
- They add a middleware dependency for capabilities we build ourselves anyway

### Chosen broker path

**India paper trading: Upstox Sandbox API**
Upstox launched an official sandbox in January 2025 that closely emulates the live API
with identical payloads, auth flows, and error codes. No trading account required.
Testing against the sandbox means zero code changes when graduating to live trading.

**India live trading: Upstox Live → Zerodha**
Upstox APIs are completely free. The system starts on Upstox live for Phase 3.
Zerodha (₹500/month data tier) becomes the primary live broker in Phase 4 for its
superior reliability and ecosystem maturity. Both implement the same `BrokerAdapter`
interface — switching is a single config line.

**US paper and live trading: Alpaca**
Alpaca paper trading is free, requires only an email signup, and is available globally.
The same `AlpacaAdapter` handles both paper and live — `paper=True` flag switches modes.

**Local development:** During Phase 1 and 2, the application runs on a laptop. Upstox Sandbox has no static IP requirement — SEBI's static IP rule applies to live trading only. API secrets are stored in a `.env` file locally via `python-dotenv`; no AWS setup is needed. Switching to AWS Secrets Manager in Phase 3 is a single config line change, zero code changes.

---

## 8. Data Source Strategy

Every data dependency has a free tier as the default. Paid services are optional add-ons
plugged into the same interface — enabling them is a config change, not a code change.

| Data type | Free (default) | Paid (optional) |
|---|---|---|
| India announcements | `BseIndiaApi` + `nse-bse-api` (direct scrape) | StockInsights.ai tagged feed |
| US announcements | SEC EDGAR full-text search API (official, free) | Benzinga |
| India market data | Upstox free quotes API + WebSocket | — |
| US market data | `yfinance` (Yahoo Finance wrapper) | Alpaca Market Data paid tier |
| Analyst consensus | Disabled by default | Screener.in, Trendlyne (future) |
| Technical indicators | `pandas-ta` (pure Python, free) | — |

When consensus data is disabled (default), the Fundamental strategy's scorer automatically
redistributes that weight component across the other scoring factors.

---

## 9. System Architecture — Five Layers

The logical architecture remains a five-layer pipeline. The key evolution from the original
design is that Layer 2 is now a pluggable Strategy Engine rather than a fixed
Analyst + Scorer pipeline.

```
┌──────────────────────────────────────────────────────────────┐
│  Layer 1 — Data feeds                                         │
│  AnnouncementFeed (BSE/NSE/EDGAR) · CandleFeed (OHLCV)      │
│  Price ticks (Upstox WebSocket / Alpaca stream)              │
└──────────────────────┬───────────────────────────────────────┘
                       │ normalised DataEvents
┌──────────────────────▼───────────────────────────────────────┐
│  Layer 2 — Strategy Engine (pluggable)                        │
│  FundamentalStrategy · TechnicalStrategy · HybridStrategy    │
│  + future strategies — add without modifying this layer      │
└──────────────────────┬───────────────────────────────────────┘
                       │ TradingSignal (same object regardless of strategy)
┌──────────────────────▼───────────────────────────────────────┐
│  Layer 3 — Decision and Approval Gate                         │
│  Risk Guard · Auto/manual gate · Order constructor           │
└──────────────────────┬───────────────────────────────────────┘
                       │ approved Order
┌──────────────────────▼───────────────────────────────────────┐
│  Layer 4 — Execution (SEBI-compliant for India)              │
│  BrokerAdapter · Order manager · Fill handler                │
│  Upstox Sandbox → Upstox Live → Zerodha (India)              │
│  Alpaca Paper → Alpaca Live (US)                             │
└──────────────────────┬───────────────────────────────────────┘
                       │ TradeRecord
┌──────────────────────▼───────────────────────────────────────┐
│  Layer 5 — Monitoring, Logging and Reporting                  │
│  Audit log · P&L tracker · Tax ledger · Alerts               │
└──────────────────────────────────────────────────────────────┘
```

---

## 10. Fundamental Strategy — Signal Logic

The fundamental strategy parses quarterly result PDFs using Claude Sonnet and scores them
on a weighted composite. All weights are tunable in `config/strategies/fundamental.yaml`.

### Extraction targets

| Metric | Comparison points |
|---|---|
| Revenue | Actual vs. YoY (consensus comparison disabled by default — requires paid data) |
| PAT (Profit After Tax) | Actual vs. YoY |
| EBITDA margin | Current quarter vs. prior quarter vs. YoY |
| Exceptional items | Flagged — prevents false positives on one-time gains |
| Management guidance | Raised / maintained / cut |
| Dividend | First-time / increased / maintained / cut / nil |

### Default scoring weights

| Component | Weight |
|---|---|
| PAT beat magnitude | 30% |
| Revenue beat magnitude | 20% |
| Margin direction | 20% |
| Guidance change | 15% |
| Dividend signal | 10% |
| Exceptional items penalty | −15% if present |

### Score thresholds (configurable)

| Score | Meaning |
|---|---|
| ≥ 75 | Strong BUY — auto-execution eligible |
| 60–74 | Moderate BUY — manual review |
| 40–59 | Neutral — no action |
| < 40 | SELL signal on existing holdings |

---

## 11. Technical Strategy — Signal Logic

The technical strategy evaluates OHLCV candles against indicator conditions defined entirely
in `config/strategies/technical.yaml`. No code change is needed to add or modify indicators.

Example configuration (50DMA / 200DMA golden cross with RSI confirmation):

```yaml
indicators:
  - {id: sma_50,  type: sma, period: 50,  source: close}
  - {id: sma_200, type: sma, period: 200, source: close}
  - {id: rsi_14,  type: rsi, period: 14}

entry_conditions:
  - {type: crossover, indicator_a: sma_50, indicator_b: sma_200,
     direction: above, required: true}
  - {type: threshold, indicator: rsi_14,
     operator: between, min: 45, max: 70, required: false}
```

Supported indicator types (via `pandas-ta`): SMA, EMA, RSI, MACD, Bollinger Bands,
ATR, ADX, Stochastic, VWAP, and more. All are pure mathematical calculations — no LLM.

---

## 12. Approval Flow

Two channels share the same `approval_queue` database table. Either can approve or reject.

**Telegram (primary — mobile):** Signal fires → message with ticker, strategy type, score,
rationale, and proposed order details. Inline ✅ Approve / ❌ Reject buttons.
Timeout: 30 minutes → auto-expired (never auto-approved).

**React dashboard (secondary — desktop):** `ApprovalQueue` component polls for pending
approvals. Full signal context shown before approve/reject.

---

## 13. Risk Controls

All checks must pass before an order is constructed. Checks run sequentially and stop on
the first failure — the failure reason is logged and alerted.

| Check | Rule |
|---|---|
| Position concentration | No single stock > 8% of portfolio |
| Sector concentration | No single sector > 25% of portfolio |
| Circuit breaker state | Do not order if stock is at upper/lower circuit (India only) |
| Market session | Pre-open (India) or continuous session only; no closing session orders |
| Liquidity | Order size ≤ 2% of 30-day average daily volume |
| Cash availability | Sufficient free cash before order construction |

---

## 14. Known Constraints and Risks

| Risk | Mitigation |
|---|---|
| Pre-open already prices in overnight results | Queue limit orders at 09:00 IST (pre-open), not 09:15 |
| Circuit breakers block entry on strong results | Detect circuit state; log as missed trade, never chase |
| Broker API downtime on volatile days | Retry with exponential backoff; suppress orders during outage |
| Large PDF handling (100+ pages) | Extract financial pages only (3–8 typically) before sending to Claude |
| Results season signal surge | Rate-limit strategy evaluation; process queue sequentially |
| Slippage on limit orders | Log unfilled orders as missed trades; use for signal quality analysis |
| Kite daily token expiry | Automated 08:30 IST re-auth job using `pyotp` + AWS Secrets Manager |

---

## 15. Build Sequence

### Phase 1 — Signal validation, fundamental strategy (weeks 1–6)
No trading. Validate that AI signal extraction is reliable before building execution.

1. Scaffold infrastructure: PostgreSQL, event bus, config registry, audit logger
2. Build `AnnouncementFeed` (BSE + NSE free pollers)
3. Build `FundamentalStrategy`: PDF extractor + Claude Sonnet + composite scorer
4. Run on at least 3 most recent completed quarters; manually validate signal quality
5. Tune weights in `config/strategies/fundamental.yaml`
6. Build backtester CLI: `python -m backtester run --strategy fundamental_v1 --market india`

**Exit criteria:** ≥ 80% agreement between AI signal and manual assessment.

### Phase 2 — Paper trading, both markets (weeks 7–14)
Full pipeline, no real money. Validate end-to-end behaviour.

1. Build Risk Guard, Trader (Upstox Sandbox + Alpaca Paper), Monitor
2. Build `CandleFeed` and `TechnicalStrategy` (built but disabled — validate separately)
3. Build Telegram alert bot + React dashboard
4. Run India and US markets on paper continuously

**Exit criteria:** Stable for 4 weeks; positive expected value on paper trades.

### Phase 3 — Live trading, small size (weeks 15–22)
Real money, conservative position limits, high approval threshold.

1. Deploy AWS EC2 + Elastic IP; register with Upstox developer console
2. Switch India to `upstox_live`; US to `alpaca_live`
3. `auto_threshold: 95` — almost all manual approval initially
4. India caps: ₹50,000 per trade, 3 concurrent positions
5. US caps: $500 per trade, 2 concurrent positions
6. Enable `technical_v1` once fundamental live trading is stable

**Exit criteria:** 20+ live trades, behaviour matches paper, no unexpected errors.

### Phase 4 — Scaled operation (ongoing)
1. Switch India to Zerodha (`broker: zerodha`)
2. Enable `hybrid_v1` — requires both fundamental and technical to agree
3. Expand watchlists (India: Nifty 500; US: S&P 500)
4. Add exit signal logic (not just entry triggers)
5. Enable paid data add-ons when budget permits
6. Add new strategy plugins (sentiment, volume anomaly, macro) as needed

---

## 16. Open Decisions

| Decision | Status | Notes |
|---|---|---|
| Watchlist scope at launch | Resolved | Config-driven via YAML; supports static tickers, index membership (e.g. NIFTY50), and index range (e.g. BSE-500 to BSE-600) |
| Active markets at launch | Resolved | India only for Phase 1; US added in Phase 2 |
| Approval channel preference | TBD | Telegram primary (as designed), or React primary? |
| Technical indicator library | TBD | `pandas-ta` (pure Python) vs `ta-lib` (faster, C dependency) |
| Candle timeframe for technical | TBD | Daily for trend-following; 1H for faster signals |
| Position sizing model | TBD | Fixed % to start; Kelly / volatility-adjusted in Phase 4 |
| US tax configuration | TBD | Bracket-dependent rates; configure per user |
| IBKR as US backup broker | TBD | Implement `IBKRAdapter` in Phase 4 if Alpaca has issues |

---

*Document status: Approach finalised (v3). For full technical design, see `design.md`.*
