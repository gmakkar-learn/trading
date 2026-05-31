# Auto Trader 01

An event-driven algorithmic trading system for US (NYSE/NASDAQ) and Indian (NSE/BSE)
equity markets. Detects corporate triggers — quarterly earnings filings, technical
crossovers — and executes trades via broker APIs.

> **Current phase:** Phase 1 complete — fundamental strategy validated via backtester.
> Phase 2 (paper trading, Risk Guard, Trader, dashboard) is next.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Quick Start](#3-quick-start)
4. [Running the Backtester](#4-running-the-backtester)
5. [Running Tests](#5-running-tests)
6. [Project Structure](#6-project-structure)
7. [Configuration](#7-configuration)
8. [Design Documents](#8-design-documents)

---

## 1. Overview

- **Strategy:** Extracts earnings data from SEC 8-K press releases via Claude Sonnet,
  scores revenue growth, EPS beats, margin direction, and guidance, then generates
  BUY / HOLD / SELL signals.
- **Markets:** US (SEC EDGAR + yfinance + Alpaca). India (BSE/NSE + Upstox) — Phase 2.
- **Broker:** Alpaca paper trading (Phase 1–2), Alpaca live (Phase 3), Zerodha (Phase 4).
- **Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL, Alembic, `uv`.

---

## 2. Architecture

```
SEC EDGAR 8-K feed
        │
        ▼
  StrategyEngine  ←── config/strategies/active.yaml
        │
        │  FundamentalStrategy
        │    └── pdf_extractor → ClaudeClient → composite_scorer
        │
        ▼  TradingSignal
        │
  [Phase 2+]  Risk Guard → Trader → BrokerAdapter → Monitor
                                         │
                               Alpaca (paper / live)
                               Upstox / Zerodha (India)
```

New strategies extend `BaseStrategy` and register in `config/strategies/active.yaml`.
The downstream pipeline never changes regardless of strategy type.

---

## 3. Quick Start

**Prerequisites:** Docker, Python 3.12+, `uv`

```bash
# 1. Clone and install
git clone <repo>
cd auto-trader-01
uv sync

# 2. Configure secrets
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY and SEC_EDGAR_USER_AGENT

# 3. Start database
docker compose up -d

# 4. Run migrations
uv run alembic upgrade head
```

---

## 4. Running the Backtester

```bash
# Last 3 quarters, all 10 watchlist tickers, no price fetch (faster)
uv run python -m backtester --strategy fundamental_v1 --market us --quarters last3 --no-prices

# With price accuracy stats
uv run python -m backtester --strategy fundamental_v1 --market us --quarters last3

# Custom date range, export to CSV
uv run python -m backtester --strategy fundamental_v1 --market us \
    --quarters 2024-07-01:2025-04-30 --csv results.csv

# Verbose logging
uv run python -m backtester --strategy fundamental_v1 --market us --quarters last3 --verbose
```

Results are cached: documents in `.cache/docs/`, Claude analyses in `.cache/analyses/`.
Repeat runs skip all API calls and complete in ~30s once SEC filing discovery is also
cached (see `optimization.md`).

---

## 5. Running Tests

```bash
uv run pytest                          # all tests
uv run pytest -x -q                    # fail-fast, quiet
uv run pytest --cov=agents --cov=infrastructure --cov=backtester  # with coverage
```

---

## 6. Project Structure

```
auto-trader-01/
├── agents/
│   └── strategy_engine/
│       ├── base_strategy.py           # Abstract base — all strategies implement this
│       ├── engine.py                  # Routes events to strategies
│       ├── strategy_registry.py       # Loads active strategies from YAML
│       ├── data_feeds/
│       │   └── announcement_feed.py   # SEC EDGAR 8-K poller
│       └── strategies/
│           └── fundamental/           # FundamentalStrategy (Phase 1)
│               ├── strategy.py
│               ├── claude_client.py
│               ├── pdf_extractor.py
│               ├── composite_scorer.py
│               └── prompts/
│                   └── result_extraction.txt
├── backtester/
│   ├── cli.py                         # Entry point: python -m backtester
│   ├── historical_loader.py           # Fetches historical 8-K filings
│   ├── simulation_runner.py           # Evaluates filings concurrently
│   └── report.py                      # Rich console output + CSV export
├── infrastructure/
│   ├── cache/                         # Disk caches for documents + analyses
│   ├── config_registry/               # Hot-reload YAML config loader
│   ├── database/                      # SQLAlchemy models + connection pool
│   ├── event_bus/                     # asyncio-based pub/sub + typed events
│   ├── market_context/                # MarketContext loader from YAML
│   ├── secrets/                       # SecretsProvider (env / AWS backends)
│   └── watchlist/                     # Config-driven ticker list provider
├── migrations/                        # Alembic schema migrations
├── tests/
│   ├── strategies/test_fundamental.py
│   └── infrastructure/test_watchlist.py
├── config/
│   ├── markets/us.yaml                # US market parameters
│   ├── strategies/
│   │   ├── active.yaml                # Enabled strategies per market
│   │   └── fundamental.yaml           # Scoring weights and thresholds
│   ├── watchlist/us.yaml              # 10 US tickers (AAPL, MSFT, …)
│   ├── risk/default.yaml              # Position size limits
│   └── system/default.yaml           # Secrets backend, active markets
├── docker-compose.yaml                # PostgreSQL 15 for local dev
├── pyproject.toml                     # Dependencies (uv)
├── alembic.ini
├── design.md                          # Full technical design (read first)
├── approach.md                        # Strategic rationale and decisions
└── optimization.md                    # Performance and cost optimizations
```

---

## 7. Configuration

All business parameters are in `config/` — no code changes needed for tuning.

| File | Controls |
|---|---|
| `config/watchlist/us.yaml` | Tickers to watch (static list, index membership, or range) |
| `config/strategies/active.yaml` | Which strategies are enabled per market |
| `config/strategies/fundamental.yaml` | Scoring weights, BUY/HOLD/SELL thresholds |
| `config/markets/us.yaml` | Market hours, currency, data sources, tax rules |
| `config/risk/default.yaml` | Max position size, max order size |
| `config/system/default.yaml` | Secrets backend (`env` / `aws`), active markets |

Switching from local `.env` secrets to AWS Secrets Manager in Phase 3 is a single line
in `config/system/default.yaml`. Zero code changes.

---

## 8. Design Documents

| Document | Purpose |
|---|---|
| `design.md` | Full technical design: module structure, interfaces, config schema, build sequence |
| `approach.md` | Strategic rationale, feasibility, regulatory context, key decisions |
| `optimization.md` | Implemented and planned performance/cost optimizations |
| `CLAUDE.md` | Context and instructions for Claude Code (AI assistant) |
