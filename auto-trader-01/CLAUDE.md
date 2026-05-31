# Auto Trader 01 — Project Context for Claude Code

This file gives Claude Code the full context needed to work on this project.
Read `approach.md` and `design.md` before doing anything else in this project.

---

## What This Project Is

An event-driven algorithmic trading system for Indian (NSE/BSE) and US (NYSE/NASDAQ)
equity markets. It detects corporate triggers — quarterly results filings, technical
indicator crossovers, or a combination — and executes trades via broker APIs.

This is a personal trading system being built by a single owner-operator. It is not
a commercial product.

---

## Must-Read Documents (read these first, every session)

| File | Purpose |
|---|---|
| `approach.md` | Strategic rationale, feasibility assessment, regulatory context, key decisions |
| `design.md` | Full technical design: module structure, agent design, interfaces, config schema, build sequence |

Do not proceed with any implementation task without first reading both documents in full.
The design has been through multiple revisions — earlier decisions visible in code or
comments may have been superseded by later ones in `design.md`.

---

## Current Project State

**Phase:** Pre-implementation. Design is finalised (v3). No code has been written yet.

**Next action:** Begin Phase 1 implementation — project scaffold and infrastructure layer.
Refer to Section 22 of `design.md` (Build Sequence, Phase 1) for the ordered task list.

---

## Architecture in One Page

```
Data feeds (AnnouncementFeed · CandleFeed · TickStream)
    │
    ▼
Strategy Engine  ←── pluggable strategies registered in config/strategies/active.yaml
    │               FundamentalStrategy (Claude Sonnet — PDF extraction + scoring)
    │               TechnicalStrategy   (pure math — OHLCV + indicators)
    │               HybridStrategy      (both must agree)
    │               + future strategies: add BaseStrategy subclass + YAML, nothing else changes
    │
    ▼  TradingSignal (same object regardless of which strategy produced it)
    │
Risk Guard  →  Trader  →  BrokerAdapter  →  Monitor
               │
               ├── Upstox Sandbox  (India paper, Phase 1+2)
               ├── Upstox Live     (India live, Phase 3)
               ├── Zerodha         (India primary live, Phase 4)
               ├── Alpaca Paper    (US paper, Phase 1+2)
               └── Alpaca Live     (US live, Phase 3+)
```

The downstream pipeline (Risk Guard → Trader → Monitor) never changes regardless of
which strategy fires. It receives only a `TradingSignal` and never inspects
`strategy_type` for business logic.

---

## Critical Design Decisions (do not reverse without discussion)

1. **Strategy plugin pattern.** New strategies extend `BaseStrategy` and implement
   `strategy_id`, `subscriptions`, and `evaluate()`. They register in
   `config/strategies/active.yaml`. Nothing else in the system changes.

2. **Broker adapter pattern.** Business logic never calls broker SDKs directly.
   It always calls `BrokerAdapter`. Switching brokers = new adapter class + config line.

3. **Free data sources are the default.** Paid sources (StockInsights.ai, Benzinga)
   are optional adapters enabled via config. The system is fully functional without them.

4. **MarketContext is injected, never hardcoded.** No file should contain hardcoded
   "IST", "NSE", "INR", or "India". These come from `MarketContext` loaded from
   `config/markets/india.yaml` or `config/markets/us.yaml`.

5. **All orders use CNC product type (India).** Never MIS. This is a tax decision
   (avoids speculative income classification), not just an execution preference.

6. **Audit log is append-only.** Every decision — including decisions NOT to trade —
   is written to the audit log before action is taken. Never delete audit records.

7. **Config over code.** Strategy weights, thresholds, position limits, polling
   intervals: all in YAML. No business parameter is hardcoded in Python.

8. **No LangGraph, no Redis, no Kafka.** asyncio queues are sufficient for this
   system's throughput (2–5 orders per day). Do not introduce these dependencies.

9. **Watchlist is config-driven.** The watched stock list is defined entirely in
   `config/watchlist/india.yaml` / `config/watchlist/us.yaml`. It supports static
   tickers, index membership (`NIFTY50`, `SP500`), and index range (`BSE500` ranks
   500–600). No code change or restart required to modify the watchlist.

10. **Secrets backend is abstracted.** All API keys and secrets are accessed via
    `SecretsProvider.get("KEY_NAME")` — never `os.environ` directly. Local dev uses
    `env` backend (`.env` file, no cloud needed). Phase 3+ uses `aws` backend (AWS
    Secrets Manager). Switching is a single line in `config/system/default.yaml`.

---

## Broker Progression

| Phase | India | US |
|---|---|---|
| 1 + 2 (paper) | `UpstoxAdapter(sandbox=True)` | `AlpacaAdapter(paper=True)` |
| 3 (live, small) | `UpstoxAdapter(sandbox=False)` | `AlpacaAdapter(paper=False)` |
| 4 (live, scaled) | `ZerodhaAdapter()` | `AlpacaAdapter(paper=False)` |

Owner has an active Upstox live account. Upstox Sandbox requires no account — developer
portal signup only. Zerodha is the eventual primary live broker for India (Phase 4).

---

## Key Interfaces (memorise these — they are the system's contracts)

### BaseStrategy
```python
class BaseStrategy(ABC):
    strategy_id: str                              # matches key in active.yaml
    subscriptions: list[DataFeedType]             # declares data dependencies
    async def evaluate(event, context) -> TradingSignal | None: ...
```

### BrokerAdapter
```python
class BrokerAdapter(ABC):
    async def place_order(order: Order) -> OrderResult: ...
    async def cancel_order(order_id: str) -> bool: ...
    async def get_positions() -> list[Position]: ...
    async def get_quote(ticker: str) -> Quote: ...
    async def subscribe_ticks(tickers, callback) -> None: ...
```

### TradingSignal (the contract between strategy and downstream)
```python
@dataclass
class TradingSignal:
    signal_id: str
    ticker: str
    market_id: str           # "india" | "us"
    strategy_type: str       # "fundamental" | "technical" | "hybrid"
    strategy_id: str
    composite_score: float   # 0–100
    recommended_action: str  # "BUY" | "SELL" | "HOLD"
    confidence: str          # "high" | "medium" | "low"
    rationale: str
    context: dict
    created_at: datetime
```

---

## Technology Stack

| Component | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI |
| AI extraction | Claude Sonnet `claude-sonnet-4-20250514` |
| India paper broker | Upstox Sandbox API (free) |
| India live broker Ph3 | Upstox Live API (free API) |
| India live broker Ph4 | Zerodha Kite Connect (₹500/month data) |
| US broker | Alpaca (`alpaca-py`) |
| India announcements | `BseIndiaApi`, `nse-bse-api` (free) |
| US announcements | SEC EDGAR API (free, official) |
| India market data | Upstox free quotes API |
| US market data | `yfinance` |
| Technical indicators | `pandas-ta` |
| PDF extraction | `pdfplumber`, `pymupdf` |
| Database | PostgreSQL 15+ with SQLAlchemy ORM |
| Migrations | Alembic |
| Scheduling | APScheduler |
| Secrets | AWS Secrets Manager |
| Deployment | AWS EC2 `t3.small` + Elastic IP |
| Alerts | Telegram Bot API |
| Frontend | React + Zustand + Axios |
| Package manager | uv (`pyproject.toml`, `uv.lock`) |

---

## SEBI Compliance (India)

- All order API calls must originate from a registered static IP (AWS EC2 Elastic IP)
- TOTP 2FA via `pyotp`; TOTP secret stored in AWS Secrets Manager, never in `.env`
- All India orders: `product_type = CNC` (delivery), never `MIS`
- Audit log retained minimum 5 years
- System places 2–5 orders per day — below the 10 OPS threshold requiring formal
  algo registration with the exchange

---

## What Not to Do

- Do not hardcode any market-specific value (timezone, exchange, currency, session time)
- Do not call broker SDK methods directly from agent or strategy code — use BrokerAdapter
- Do not add LangGraph, Redis, Kafka, or RabbitMQ without explicit discussion
- Do not make Risk Guard, Trader, or Monitor inspect `strategy_type` for business logic
- Do not commit secrets, API keys, or TOTP secrets to the repository
- Do not write paid data source calls as the default path — always free source first
- Do not use `product_type = MIS` for any India order

---

## Asking Questions

If a task is ambiguous or requires a design decision not covered in `approach.md` or
`design.md`, stop and ask rather than making assumptions. This is a financial system —
incorrect assumptions about order types, position sizing, or risk rules have real
monetary consequences.

---

*Last updated: May 2026 — design v3, pre-implementation*
