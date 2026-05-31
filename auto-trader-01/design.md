# Auto Trader 01 — Design and Implementation Plan

**Project:** Event-Driven Algorithmic Trading System — Indian and US Equity Markets
**Status:** Design approved — pending implementation
**Date:** May 2026
**Revision:** 4 — pluggable strategy engine, config-driven watchlist, environment-aware secrets

---

## 1. Three Foundational Constraints

### 1.1 Multi-market support (India + US)

The system works for both NSE/BSE (India) and NYSE/NASDAQ (US) without duplicating the core pipeline. A `MarketContext` abstraction encapsulates everything market-specific: announcement sources, broker adapter, exchange calendar, timezone, currency, and regulatory rules. Every agent receives a `MarketContext` at startup; none hardcode India or US assumptions.

| Component | Market-specific? | Notes |
|---|---|---|
| Data feeds | Yes | Different announcement sources per market |
| Strategy engine | No | Same strategies run on any market |
| Risk Guard | Partially | Session times and circuit rules differ |
| Trader | Yes | Different broker adapter per market |
| Monitor | No | P&L and tax logic is market-agnostic |

### 1.2 Free-first data sources

Every external data dependency has a free tier as the default. Paid services are optional add-ons implementing the same interface — enabling them is a config change, not a code change.

| Data type | Free (default) | Paid (optional add-on) |
|---|---|---|
| India announcements | BSE/NSE direct scrape (`BseIndiaApi`, `nse-bse-api`) | StockInsights.ai tagged feed |
| US announcements | SEC EDGAR full-text search API (free, official) | Benzinga, Refinitiv |
| India market data | Upstox free API (quotes, WebSocket) | — |
| US market data | `yfinance` (Yahoo Finance wrapper) | Alpaca Market Data paid tier |
| Analyst consensus | Disabled by default | Screener.in, Trendlyne (future) |

### 1.3 Staged broker approach

| Stage | India | US | When |
|---|---|---|---|
| Paper | Upstox Sandbox API (free) | Alpaca Paper (free) | Phase 1 + 2 |
| Live — first | Upstox Live API (free API) | Alpaca Live | Phase 3 |
| Live — primary | Zerodha Kite Connect | Alpaca or IBKR | Phase 4 |

Switching tiers is a single line in `config/markets/india.yaml`. Zero code changes.

---

## 2. Design Principles

**1. Configuration over code.** Every strategy parameter — score weights, thresholds, position sizing, indicator periods, polling intervals — lives in versioned YAML. No parameter change requires a code deploy.

**2. Open for extension, closed for modification.** Adding a new strategy type (technical, hybrid, sentiment, macro) requires writing one new Python class and one new YAML file. Nothing else changes — not Risk Guard, not Trader, not Monitor, not the database, not the API.

**3. The broker is a plug-in.** The system never calls broker SDKs directly from business logic. It calls a `BrokerAdapter` interface. Swapping brokers means writing a new adapter — zero changes to agents.

**4. Free sources are the default.** Paid sources are optional adapters registered via config. The system degrades gracefully when paid sources are unavailable.

**5. Events flow in one direction.** Data feeds → Strategy Engine → Risk Guard → Trader → Monitor. No agent calls another directly. All communication through the event bus.

**6. Every decision is a record.** Before any action (place order, send alert, reject signal), it is written to the audit log. Append-only. Regulatory requirement and debugging tool combined.

**7. Fail safe, not fail silent.** On any error — API timeout, unexpected PDF structure, broker rejection — the system logs, alerts, and stops. No blind retries involving money.

**8. The backtester is a first-class citizen.** Strategies take a `DataEvent` as input, not a live API connection. They work identically offline and in production.

**9. Market context is injected, never assumed.** No strategy hardcodes "IST", "NSE", or "INR". These come from `MarketContext`.

---

## 3. What We Are Not Building (by design)

- **No LangGraph or agent framework.** asyncio queues are sufficient. A framework adds a dependency and constrains structure without benefit at this scale.
- **No Redis / Kafka / RabbitMQ.** Single-process on one EC2 instance. asyncio queues handle the load. Add a message broker only when processing hundreds of signals per day.
- **No ML model training.** Signal scoring is a weighted formula with tunable YAML parameters. Transparent, auditable, no retraining infrastructure.
- **No paid data by default.** Every paid integration is an optional adapter behind the same interface as its free counterpart.

---

## 4. Agent Design

Six top-level agents. The Watcher, Analyst, and Scorer from the previous design are now encapsulated inside the Strategy Engine — they still exist, but as internals of `FundamentalStrategy`, not as top-level agents.

| Agent | Uses LLM? | Market-specific? | Notes |
|---|---|---|---|
| **Strategy Engine** | Yes (Fundamental only) | No | Hosts all pluggable strategies |
| **Risk Guard** | No | Partially | Session rules differ; concentration logic identical |
| **Trader** | No | Yes | Different broker adapter per market |
| **Monitor** | No | No | P&L and tax are universal; rates configured via YAML |

---

## 5. The Strategy Engine — Core Architecture

### 5.1 The Problem It Solves

Different trigger types need fundamentally different data and processing:

| Dimension | Fundamental | Technical | Hybrid |
|---|---|---|---|
| Data source | BSE/NSE filing | Continuous OHLCV candles | Both |
| Timing | Event-driven (filing arrives) | Time-driven (candle close) | Both |
| Input | Unstructured PDF | Structured time-series | Both |
| Processing | LLM extraction + scoring | Mathematical calculation | Both combined |
| Uses Claude | Yes | No | Fundamental sub-component only |

A rigid pipeline (Watcher → Analyst → Scorer) cannot accommodate this. The Strategy Engine replaces that rigid pipeline with a plug-in architecture.

### 5.2 The Principle

```
Data feeds ──→ StrategyEngine ──→ TradingSignal → Risk Guard → Trader → Monitor
                     │
              [Strategy Registry]
                     │
          ┌──────────┼────────────┐
          │          │            │
    Fundamental  Technical    Hybrid
     Strategy    Strategy    Strategy
    (existing)   (new)       (future)
```

The downstream pipeline — Risk Guard, Trader, Monitor — never changes regardless of what strategy produced the signal, because they all consume the same `TradingSignal` object. The downstream never knows which strategy produced it.

### 5.3 The BaseStrategy Interface

Every strategy implements this contract. It is intentionally minimal:

```python
# agents/strategy_engine/base_strategy.py

class BaseStrategy(ABC):

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """Unique identifier. Must match the key in active.yaml."""
        ...

    @property
    @abstractmethod
    def subscriptions(self) -> list[DataFeedType]:
        """Which data feeds this strategy needs.
        Engine subscribes only to feeds that active strategies declare."""
        ...

    @abstractmethod
    async def evaluate(
        self,
        event: DataEvent,
        context: StrategyContext
    ) -> TradingSignal | None:
        """Core logic. Return TradingSignal if a trade opportunity is found.
        Return None if no action warranted — this is the normal case."""
        ...

    @property
    def config(self) -> dict:
        """Strategy-specific config from YAML.
        Base class handles loading — subclasses read self.config."""
        return self._config
```

Three design decisions in this interface:

- `subscriptions` declares data dependencies upfront. A fundamental-only deployment never starts the OHLCV candle feed.
- `evaluate()` returns `TradingSignal | None`. `None` is normal — most events do not result in trades.
- The output type is always `TradingSignal`. The downstream is fully decoupled from strategy internals.

### 5.4 FundamentalStrategy (existing logic, encapsulated)

```python
# agents/strategy_engine/strategies/fundamental/strategy.py

class FundamentalStrategy(BaseStrategy):

    strategy_id = "fundamental_v1"
    subscriptions = [DataFeedType.ANNOUNCEMENTS]

    async def evaluate(self, event: DataEvent,
                       context: StrategyContext) -> TradingSignal | None:

        if not isinstance(event, AnnouncementEvent):
            return None
        if event.filing_type not in ["FinancialResults", "BoardMeeting"]:
            return None

        # PDF extraction (pdf_extractor.py)
        result_doc = await self.pdf_extractor.extract(event.filing_url)

        # Claude Sonnet scoring (claude_client.py)
        scored = await self.claude_client.analyse(result_doc)

        # Composite scoring (composite_scorer.py)
        score = self.composite_scorer.score(scored, self.config)

        if score.composite_score < self.config["thresholds"]["neutral_low"]:
            return None

        return TradingSignal(
            ticker=event.ticker,
            strategy_type="fundamental",
            strategy_id=self.strategy_id,
            composite_score=score.composite_score,
            recommended_action=score.action,
            confidence=score.confidence,
            rationale=scored.raw_claude_response,
            context=score.context
        )
```

The existing PDF extractor, Claude client, and composite scorer are unchanged — they are encapsulated as internals of this class, not deleted.

### 5.5 TechnicalStrategy (new)

```python
# agents/strategy_engine/strategies/technical/strategy.py

class TechnicalStrategy(BaseStrategy):

    strategy_id = "technical_v1"
    subscriptions = [DataFeedType.OHLCV_CANDLES]

    async def evaluate(self, event: DataEvent,
                       context: StrategyContext) -> TradingSignal | None:

        if not isinstance(event, CandleEvent):
            return None

        # Fetch required historical candles
        candles = await context.data_feed.get_candles(
            ticker=event.ticker,
            timeframe=event.timeframe,
            count=max(i["period"] for i in self.config["indicators"]) + 1
        )

        # Calculate indicators (pure math — no LLM)
        values = self.indicator_engine.calculate(
            candles, self.config["indicators"]
        )

        # Evaluate entry conditions defined in YAML
        signal = self.condition_evaluator.evaluate(
            values, self.config["entry_conditions"]
        )

        if not signal.triggered:
            return None

        return TradingSignal(
            ticker=event.ticker,
            strategy_type="technical",
            strategy_id=self.strategy_id,
            composite_score=signal.strength,
            recommended_action=signal.action,
            confidence=signal.confidence,
            rationale=signal.description,   # e.g. "50DMA crossed above 200DMA; RSI=58"
            context=signal.context
        )
```

### 5.6 HybridStrategy (new)

```python
# agents/strategy_engine/strategies/hybrid/strategy.py

class HybridStrategy(BaseStrategy):
    """Requires both fundamental and technical signals to agree."""

    strategy_id = "hybrid_v1"
    subscriptions = [DataFeedType.ANNOUNCEMENTS, DataFeedType.OHLCV_CANDLES]

    async def evaluate(self, event: DataEvent,
                       context: StrategyContext) -> TradingSignal | None:

        # Look up cached signals from constituent strategies
        fundamental_signal = context.signal_cache.get(
            event.ticker, "fundamental_v1"
        )
        technical_signal = context.signal_cache.get(
            event.ticker, "technical_v1"
        )

        # Both must be present and agree on direction
        if not fundamental_signal or not technical_signal:
            return None
        if fundamental_signal.recommended_action != technical_signal.recommended_action:
            return None

        # Combine scores using config-defined weights
        w = self.config["weights"]
        combined_score = (
            fundamental_signal.composite_score * w["fundamental"] +
            technical_signal.composite_score   * w["technical"]
        )

        return TradingSignal(
            ticker=event.ticker,
            strategy_type="hybrid",
            strategy_id=self.strategy_id,
            composite_score=combined_score,
            recommended_action=fundamental_signal.recommended_action,
            confidence="high",
            rationale=(
                f"Fundamental: {fundamental_signal.rationale} | "
                f"Technical: {technical_signal.rationale}"
            ),
            context={**fundamental_signal.context, **technical_signal.context}
        )
```

---

## 6. Strategy Configuration

### Adding a new strategy: four steps, zero code changes to the harness

1. Create `agents/strategy_engine/strategies/new_strategy/strategy.py` extending `BaseStrategy`
2. Create `config/strategies/new_strategy.yaml` with its parameters
3. Add one entry to `config/strategies/active.yaml`
4. Done — Risk Guard, Trader, Monitor, API, frontend, database: all unchanged

### config/strategies/active.yaml

```yaml
active_strategies:

  - id: fundamental_v1
    enabled: true
    markets: [india, us]
    config_file: strategies/fundamental.yaml

  - id: technical_v1
    enabled: false          # flip to true to activate; no code change needed
    markets: [india, us]
    config_file: strategies/technical.yaml

  - id: hybrid_v1
    enabled: false
    markets: [india]
    config_file: strategies/hybrid.yaml
```

### config/strategies/fundamental.yaml

```yaml
version: "1.0"

scoring:
  weights:
    pat_beat:            0.30
    revenue_beat:        0.20
    margin_direction:    0.20
    guidance_change:     0.15
    dividend_signal:     0.10
    exceptional_penalty: -0.15

  consensus_component:
    enabled: false          # requires paid data; disabled by default
    weight: 0.15            # redistributed when disabled

  thresholds:
    strong_buy:   75
    moderate_buy: 60
    neutral_low:  40        # below this → no action (or SELL on existing holdings)

execution:
  auto_threshold:        85
  limit_price_slippage:  0.005
  stoploss_pct:          0.05
  target_pct:            0.12
  product_type:          CNC
  preferred_exchange:    primary
```

### config/strategies/technical.yaml

```yaml
version: "1.0"

indicators:
  - id: sma_50
    type: sma
    period: 50
    source: close

  - id: sma_200
    type: sma
    period: 200
    source: close

  - id: rsi_14
    type: rsi
    period: 14

entry_conditions:
  - type: crossover
    indicator_a: sma_50
    indicator_b: sma_200
    direction: above        # golden cross
    required: true

  - type: threshold
    indicator: rsi_14
    operator: between
    min: 45
    max: 70                 # not overbought — optional confirmation
    required: false

timeframe: daily
lookback_candles: 210       # sufficient for 200-period SMA

execution:
  auto_threshold:        80
  limit_price_slippage:  0.003
  stoploss_pct:          0.06
  target_pct:            0.10
```

### config/strategies/hybrid.yaml

```yaml
version: "1.0"

weights:
  fundamental: 0.60
  technical:   0.40

signal_cache_ttl_hours: 48  # how long a fundamental signal stays valid for hybrid matching

execution:
  auto_threshold:        88
  limit_price_slippage:  0.005
  stoploss_pct:          0.05
  target_pct:            0.15
```

---

## 7. Full Project Structure

```
auto-trader-01/
│
├── config/
│   ├── markets/
│   │   ├── india.yaml                   # India MarketContext
│   │   └── us.yaml                      # US MarketContext
│   ├── strategies/
│   │   ├── active.yaml                  # Which strategies are enabled
│   │   ├── fundamental.yaml             # Fundamental strategy params
│   │   ├── technical.yaml               # Technical indicator params
│   │   └── hybrid.yaml                  # Hybrid combination weights
│   ├── risk/
│   │   └── default.yaml
│   ├── watchlist/
│   │   ├── india.yaml                   # India watchlist (static, index membership, index range)
│   │   └── us.yaml                      # US watchlist
│   ├── brokers/
│   │   ├── upstox_sandbox.yaml
│   │   ├── upstox_live.yaml
│   │   ├── zerodha.yaml
│   │   ├── alpaca_paper.yaml
│   │   └── alpaca_live.yaml
│   ├── data_sources/
│   │   ├── india_free.yaml
│   │   ├── india_paid.yaml              # StockInsights.ai add-on
│   │   ├── us_free.yaml
│   │   └── us_paid.yaml
│   └── system/
│       └── default.yaml
│
├── agents/
│   │
│   ├── strategy_engine/                 # Replaces watcher+analyst+scorer at top level
│   │   ├── __init__.py
│   │   ├── engine.py                    # StrategyEngine — loads registry, routes events
│   │   ├── base_strategy.py             # BaseStrategy abstract class
│   │   ├── strategy_registry.py         # Loads active strategies from active.yaml
│   │   ├── strategy_context.py          # StrategyContext dataclass injected into evaluate()
│   │   ├── signal_cache.py              # Short-lived cache for hybrid strategy correlation
│   │   │
│   │   ├── data_feeds/
│   │   │   ├── __init__.py
│   │   │   ├── feed_manager.py          # Starts/stops feeds per strategy subscriptions
│   │   │   ├── feed_types.py            # DataFeedType enum: ANNOUNCEMENTS, OHLCV_CANDLES, TICKS
│   │   │   ├── announcement_feed.py     # BSE/NSE/EDGAR polling (was agents/watcher)
│   │   │   └── candle_feed.py           # OHLCV candle stream: Upstox / yfinance
│   │   │
│   │   └── strategies/
│   │       ├── fundamental/
│   │       │   ├── __init__.py
│   │       │   ├── strategy.py          # FundamentalStrategy
│   │       │   ├── pdf_extractor.py     # pdfplumber / pymupdf (was agents/analyst)
│   │       │   ├── claude_client.py     # Claude Sonnet wrapper (was agents/analyst)
│   │       │   ├── composite_scorer.py  # Weighted scoring (was agents/scorer)
│   │       │   ├── context_enricher.py  # 52wk, sector, liquidity (was agents/scorer)
│   │       │   ├── result_document.py   # ResultDocument dataclass
│   │       │   └── prompts/
│   │       │       ├── result_extraction.txt
│   │       │       └── ambiguity_check.txt
│   │       │
│   │       ├── technical/
│   │       │   ├── __init__.py
│   │       │   ├── strategy.py          # TechnicalStrategy
│   │       │   ├── indicator_engine.py  # SMA, EMA, RSI, MACD, Bollinger — pure math
│   │       │   └── condition_evaluator.py  # Crossover / threshold / pattern detection
│   │       │
│   │       └── hybrid/
│   │           ├── __init__.py
│   │           └── strategy.py          # HybridStrategy
│   │
│   ├── risk_guard/                      # UNCHANGED from v2
│   │   ├── __init__.py
│   │   ├── agent.py
│   │   ├── checks/
│   │   │   ├── base.py
│   │   │   ├── concentration.py
│   │   │   ├── liquidity.py
│   │   │   ├── circuit_breaker.py
│   │   │   ├── session.py
│   │   │   └── cash_available.py
│   │   └── order_constructor.py
│   │
│   ├── trader/                          # UNCHANGED from v2
│   │   ├── __init__.py
│   │   ├── agent.py
│   │   ├── approval_gate.py
│   │   ├── order_manager.py
│   │   └── fill_handler.py
│   │
│   └── monitor/                         # UNCHANGED from v2
│       ├── __init__.py
│       ├── agent.py
│       ├── pnl_tracker.py
│       ├── tax_ledger.py
│       ├── stoploss_watcher.py
│       └── report_generator.py
│
├── infrastructure/
│   ├── event_bus/
│   │   ├── bus.py
│   │   └── events.py                    # All event dataclasses (typed contracts)
│   ├── market_context/
│   │   ├── context.py
│   │   └── loader.py
│   ├── broker/
│   │   ├── base.py                      # BrokerAdapter abstract base class
│   │   ├── upstox/
│   │   │   ├── adapter.py
│   │   │   ├── auth.py
│   │   │   └── websocket.py
│   │   ├── zerodha/
│   │   │   ├── adapter.py
│   │   │   ├── auth.py
│   │   │   └── websocket.py
│   │   └── alpaca/
│   │       ├── adapter.py
│   │       └── stream.py
│   ├── market_data/
│   │   ├── base.py                      # MarketDataProvider abstract base class
│   │   ├── yfinance_provider.py
│   │   ├── upstox_provider.py
│   │   └── alpaca_provider.py
│   ├── database/
│   │   ├── connection.py
│   │   ├── models/
│   │   │   ├── signals.py               # now includes strategy_type column
│   │   │   ├── orders.py
│   │   │   ├── positions.py
│   │   │   ├── audit_log.py
│   │   │   ├── tax_events.py
│   │   │   └── watchlist.py
│   │   └── migrations/
│   ├── config_registry/
│   │   ├── loader.py
│   │   ├── hot_reload.py
│   │   └── schemas/
│   │       ├── market_schema.py
│   │       ├── strategy_schema.py       # validates all strategy configs
│   │       ├── risk_schema.py
│   │       └── system_schema.py
│   ├── alerts/
│   │   ├── alert_service.py
│   │   ├── telegram_sender.py
│   │   └── email_sender.py
│   ├── secrets/
│   │   ├── provider.py                  # SecretsProvider abstract base
│   │   ├── env_provider.py              # .env backend (local dev)
│   │   └── aws_provider.py             # AWS Secrets Manager backend (Phase 3)
│   ├── watchlist/
│   │   ├── provider.py                  # Resolves config sources to flat ticker list
│   │   └── index_fetcher.py             # Fetches BSE/NSE/S&P index constituents
│   ├── audit/
│   │   └── audit_logger.py
│   └── scheduler/
│       └── market_scheduler.py
│
├── api/                                 # UNCHANGED from v2
│   ├── main.py
│   ├── routers/
│   │   ├── signals.py                   # signals now include strategy_type filter
│   │   ├── positions.py
│   │   ├── orders.py
│   │   ├── watchlist.py
│   │   ├── config.py
│   │   └── health.py
│   └── dependencies.py
│
├── backtester/                          # strategies plug in here unchanged
│   ├── cli.py                           # --strategy fundamental_v1 --market india
│   ├── historical_loader.py
│   ├── simulation_runner.py
│   └── report.py
│
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── SignalFeed/              # now shows strategy_type badge per signal
│   │   │   ├── ApprovalQueue/
│   │   │   ├── Portfolio/
│   │   │   ├── TaxLedger/
│   │   │   └── Config/
│   │   │       └── StrategyManager/     # enable/disable strategies, edit YAML params
│   │   ├── hooks/
│   │   ├── api/
│   │   └── store/
│   └── package.json
│
├── tests/
│   ├── strategies/                      # unit tests per strategy
│   │   ├── test_fundamental.py
│   │   ├── test_technical.py
│   │   └── test_hybrid.py
│   ├── infrastructure/
│   ├── integration/
│   └── fixtures/
│       ├── india/
│       │   ├── sample_results.pdf
│       │   └── announcements.json
│       ├── us/
│       │   └── 8k_filings.json
│       └── candles/
│           └── ohlcv_sample.csv         # for technical strategy tests
│
├── scripts/
│   ├── seed_watchlist.py
│   ├── backfill_calendar.py
│   └── validate_config.py
│
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yaml
│
├── .env.example
├── pyproject.toml
├── alembic.ini
├── approach.md
└── design.md
```

---

## 8. WatchlistProvider — Config-Driven Watchlist

Watchlists are fully config-driven. No code change or application restart is required to add, remove, or change the stocks the system monitors. Index membership and rank ranges are resolved at runtime by fetching live constituent data from BSE/NSE.

### 8.1 Watchlist Config Schema

```yaml
# config/watchlist/india.yaml
version: "1.0"
refresh_interval_hours: 24      # re-fetch index constituents from BSE/NSE this often

sources:
  - type: static
    tickers: [RELIANCE, INFY, TCS]   # always include these tickers

  - type: index_membership
    index: NIFTY50                   # all current constituents of Nifty 50

  - type: index_range
    index: BSE500                    # BSE index name
    rank_from: 500                   # inclusive — first rank in the slice
    rank_to:   600                   # inclusive — the 100 stocks ranked 500–600

exclude:
  - SOMETICKER                       # remove specific tickers from the resolved list
```

```yaml
# config/watchlist/us.yaml
version: "1.0"
refresh_interval_hours: 24

sources:
  - type: index_membership
    index: SP500
```

### 8.2 WatchlistProvider Interface

```python
# infrastructure/watchlist/provider.py

class WatchlistProvider:

    async def get_tickers(self, market_id: str) -> list[str]:
        """Return a flat, deduplicated, exclude-filtered list of tickers."""
        ...

    async def refresh(self, market_id: str) -> None:
        """Re-fetch index constituents and recompute the ticker list."""
        ...
```

`IndexFetcher` handles each source type:

| Source type | Implementation |
|---|---|
| `static` | Returns the inline ticker list as-is |
| `index_membership` | Fetches current constituents from BSE India API / NSE / S&P 500 |
| `index_range` | Fetches the ranked index and slices `rank_from` to `rank_to` inclusive |

All sources are merged, deduplicated, and `exclude` entries removed. `WatchlistProvider` is injected into `FeedManager` and `StrategyEngine`.

### 8.3 Refresh Behaviour

`WatchlistProvider.refresh()` is called on application startup, on schedule (`refresh_interval_hours`), and whenever the watchlist YAML is modified (config hot-reload).

---

## 9. SecretsProvider — Environment-Aware Secrets

```yaml
# config/system/default.yaml (relevant excerpt)
secrets_backend: env    # env (local dev) | aws (Phase 3+ EC2 deployment)
```

| Backend | When used | Mechanism |
|---|---|---|
| `env` | Local development | Reads from `.env` via `python-dotenv`; no cloud credentials needed |
| `aws` | Phase 3+ EC2 | Reads from AWS Secrets Manager by secret name |

```python
# infrastructure/secrets/provider.py

class SecretsProvider(ABC):
    @abstractmethod
    async def get(self, key: str) -> str: ...

# infrastructure/secrets/env_provider.py
class EnvSecretsProvider(SecretsProvider):
    async def get(self, key: str) -> str:
        return os.environ[key]            # loaded from .env via python-dotenv at startup

# infrastructure/secrets/aws_provider.py
class AwsSecretsProvider(SecretsProvider):
    async def get(self, key: str) -> str: ...   # boto3 Secrets Manager call
```

All agents and adapters receive a `SecretsProvider` instance and call `secrets_provider.get("KEY_NAME")` — never `os.environ` directly.

Switching from `.env` to AWS Secrets Manager in Phase 3 is a single line change in `config/system/default.yaml`. Zero code changes elsewhere.

---

## 10. MarketContext — The Multi-Market Abstraction

### What MarketContext contains

```python
@dataclass
class MarketContext:
    market_id: str                    # "india" | "us"
    currency: str                     # "INR" | "USD"
    timezone: str                     # "Asia/Kolkata" | "America/New_York"
    exchanges: list[str]              # ["NSE", "BSE"] | ["NYSE", "NASDAQ"]
    preopen_start: time | None        # India: 09:00 | US: None
    preopen_end: time | None
    market_open: time
    market_close: time
    announcement_sources: list[str]
    paid_announcement_source: str | None
    market_data_provider: str
    broker_config_key: str
    tax_rules: TaxRules
    circuit_breaker_enabled: bool
    algo_registration_required: bool
```

### config/markets/india.yaml

```yaml
market_id: india
currency: INR
timezone: "Asia/Kolkata"
exchanges: [NSE, BSE]

sessions:
  preopen_start: "09:00"
  preopen_end:   "09:15"
  market_open:   "09:15"
  market_close:  "15:30"

announcement_sources:
  free: [bse_poller, nse_poller]
  paid_addon: stockinsights           # enabled via data_sources/india_paid.yaml

market_data:
  free: upstox_provider
  paid_addon: null

broker: upstox_sandbox               # upstox_live | zerodha for production

tax_rules:
  intraday: speculative_income
  short_term_days: 365
  short_term_rate: 0.20
  long_term_rate: 0.125
  long_term_exemption_inr: 125000

regulatory:
  circuit_breaker_enabled: true
  static_ip_required: true
  algo_registration_threshold_ops: 10
```

### config/markets/us.yaml

```yaml
market_id: us
currency: USD
timezone: "America/New_York"
exchanges: [NYSE, NASDAQ]

sessions:
  preopen_start: null
  preopen_end:   null
  market_open:   "09:30"
  market_close:  "16:00"

announcement_sources:
  free: [sec_edgar]
  paid_addon: benzinga

market_data:
  free: yfinance
  paid_addon: alpaca_data

broker: alpaca_paper                 # alpaca_live for production

tax_rules:
  short_term_days: 365
  short_term_rate: null              # user-specific; configure per user
  long_term_rate: null
  wash_sale_rule: true

regulatory:
  circuit_breaker_enabled: false
  static_ip_required: false
  algo_registration_threshold_ops: null
```

---

## 11. Broker Adapter Pattern

### The Interface

```python
# infrastructure/broker/base.py

class BrokerAdapter(ABC):

    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    async def modify_order(self, order_id: str,
                           updates: OrderUpdate) -> OrderResult: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_holdings(self) -> list[Holding]: ...

    @abstractmethod
    async def get_quote(self, ticker: str) -> Quote: ...

    @abstractmethod
    async def subscribe_ticks(self, tickers: list[str],
                               callback: Callable) -> None: ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderStatus: ...
```

### Broker Progression

```
India:
  Phase 1+2  →  UpstoxAdapter(sandbox=True)    # free, no account required
  Phase 3    →  UpstoxAdapter(sandbox=False)    # same account, one flag change
  Phase 4    →  ZerodhaAdapter()                # new adapter, same interface

US:
  Phase 1+2  →  AlpacaAdapter(paper=True)       # free, email signup only
  Phase 3+   →  AlpacaAdapter(paper=False)      # live trading
```

---

## 12. The TradingSignal Contract

This is the object that makes the system's downstream fully decoupled from strategy internals. Every strategy — regardless of type — produces exactly this:

```python
@dataclass
class TradingSignal:
    signal_id: str                    # UUID
    ticker: str
    market_id: str                    # "india" | "us"
    strategy_type: str                # "fundamental" | "technical" | "hybrid"
    strategy_id: str                  # "fundamental_v1" etc.
    strategy_version: str
    composite_score: float            # 0–100
    recommended_action: str           # "BUY" | "SELL" | "HOLD"
    confidence: str                   # "high" | "medium" | "low"
    rationale: str                    # Human-readable explanation
    context: dict                     # Strategy-specific enrichment data
    created_at: datetime
```

Risk Guard, Trader, and Monitor receive a `TradingSignal`. They never inspect `strategy_type` for business logic — they treat all signals identically. The only place `strategy_type` appears is in logging, UI display, and analytics.

---

## 13. Full Event Flow — End to End

```
1.  Scheduler triggers FeedManager per market session

2.  FeedManager
    → starts only the feeds declared by active strategies
    → AnnouncementFeed: polls BSE/NSE/EDGAR continuously
    → CandleFeed: streams OHLCV on candle close events

3.  StrategyEngine (subscribes to all feed events)
    → routes each DataEvent to every active strategy that declared that feed type
    → runs strategy.evaluate(event, context) concurrently
    → collects TradingSignal | None from each
    → writes non-None signals to DB and signal_cache
    → publishes TradingSignalEvent to event bus

4.  RiskGuardAgent (subscribes to TradingSignalEvent)
    → runs checks sequentially, stops on first failure:
        - session validity (market-aware)
        - circuit breaker (India only)
        - concentration (position + sector limits)
        - liquidity (order size vs ADV)
        - cash availability
    → if all pass: constructs Order, publishes OrderProposalEvent
    → if any fail: publishes SignalRejectedEvent (logged + alerted)

5.  TraderAgent (subscribes to OrderProposalEvent)
    → checks composite_score vs AUTO_THRESHOLD (from strategy config)
    → if AUTO:   calls BrokerAdapter.place_order()
    → if MANUAL: writes to approval_queue, sends Telegram alert
                 waits for ApprovalEvent (Telegram bot or React UI)
    → on approval: calls BrokerAdapter.place_order()
    → publishes OrderPlacedEvent

6.  MonitorAgent (subscribes to OrderPlacedEvent + tick stream)
    → records TradeRecord in DB
    → places stoploss order via BrokerAdapter
    → starts position watch loop (SL / target)
    → updates P&L tracker, tax ledger
    → sends fill confirmation alert
```

---

## 14. Risk Configuration

### config/risk/default.yaml

```yaml
position_limits:
  max_single_stock_pct:     0.08
  max_sector_pct:           0.25
  max_concurrent_positions: 10
  max_order_size_inr:       500000    # India: ₹5L hard cap
  max_order_size_usd:       5000      # US: $5K hard cap

liquidity:
  max_order_as_pct_adv:     0.02     # ≤ 2% of 30-day avg daily volume

sessions:
  allow_preopen:    true             # India only
  allow_continuous: true
  allow_closing:    false
```

### config/system/default.yaml

```yaml
secrets_backend: env                 # env (local dev) | aws (Phase 3+ EC2)

polling:
  normal_interval_seconds:   60
  elevated_interval_seconds: 10      # within 60 min of expected result

active_markets: [india]              # add "us" to run both simultaneously

broker_mode: paper                   # paper | live

alerts:
  telegram_enabled:       true
  email_enabled:          true
  approval_channel:       telegram
  approval_timeout_min:   30         # auto-expire (not auto-approve) on timeout
```

---

## 15. Key Data Contracts Between Components

| Contract | From → To | Key fields |
|---|---|---|
| `AnnouncementEvent` | AnnouncementFeed → FundamentalStrategy | `market_id`, `filing_id`, `ticker`, `exchange`, `filing_url`, `filing_type`, `published_at` |
| `CandleEvent` | CandleFeed → TechnicalStrategy | `ticker`, `market_id`, `timeframe`, `ohlcv`, `timestamp` |
| `ResultDocument` | pdf_extractor → claude_client | `ticker`, `quarter`, `revenue_actual`, `pat_actual`, `margin_direction`, `guidance_change`, `exceptional_items`, `confidence` |
| `TradingSignal` | StrategyEngine → Risk Guard | see Section 10 — full schema |
| `TradingSignalEvent` | StrategyEngine → Risk Guard | `signal: TradingSignal`, `timestamp` |
| `OrderProposal` | Risk Guard → Trader | `ticker`, `market_id`, `side`, `quantity`, `limit_price`, `stoploss`, `target`, `signal_id` |
| `Order` | Trader → BrokerAdapter | Broker-normalised params |
| `TradeRecord` | Trader → Monitor | `order_id`, `fill_price`, `qty`, `currency`, `signal_id`, `market_id`, `strategy_type` |

All contracts are Pydantic dataclasses defined in `infrastructure/event_bus/events.py`.

---

## 16. Database Schema

Eight tables. `market_id` and `strategy_type` on every signal-related table.

| Table | Key columns |
|---|---|
| `watchlist` | `ticker`, `exchange`, `market_id`, `active`, `config_overrides` |
| `announcements` | `filing_id`, `ticker`, `market_id`, `filing_url`, `filing_type`, `published_at`, `processed` |
| `signals` | `signal_id`, `ticker`, `market_id`, `strategy_type`, `strategy_id`, `composite_score`, `recommended_action`, `signal_json`, `created_at` |
| `orders` | `order_id`, `broker_order_id`, `ticker`, `market_id`, `strategy_type`, `side`, `qty`, `limit_price`, `fill_price`, `status`, `signal_id` |
| `positions` | `ticker`, `market_id`, `qty`, `entry_price`, `entry_date`, `stoploss`, `target`, `strategy_type` |
| `audit_log` | `event_id`, `market_id`, `ticker`, `decision`, `reason`, `signal_json`, `order_id`, `created_at` |
| `tax_events` | `trade_id`, `market_id`, `ticker`, `entry_date`, `exit_date`, `holding_days`, `gain_loss`, `tax_class` |
| `approval_queue` | `proposal_id`, `market_id`, `signal_id`, `strategy_type`, `status`, `proposed_at`, `decided_at` |

---

## 17. API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | System health + broker status per market |
| GET | `/status` | Active agents, feed states, queue depths |
| GET | `/signals` | Signals (`?market=india&strategy=fundamental&action=BUY`) |
| GET | `/signals/{id}` | Full signal with rationale |
| GET | `/orders` | Order history (`?market=us&status=filled`) |
| POST | `/orders/{id}/approve` | Approve pending manual order |
| POST | `/orders/{id}/reject` | Reject pending manual order |
| GET | `/positions` | Open positions |
| GET | `/portfolio` | Consolidated portfolio across markets |
| GET | `/watchlist` | Watchlist (`?market=india`) |
| POST | `/watchlist` | Add ticker |
| DELETE | `/watchlist/{ticker}` | Remove ticker |
| GET | `/config/strategies` | Active strategies and their configs |
| PUT | `/config/strategies/{id}/enable` | Enable a strategy (admin) |
| PUT | `/config/strategies/{id}/disable` | Disable a strategy (admin) |
| GET | `/config/risk` | Risk config |
| PUT | `/config/risk` | Update risk config (admin) |
| GET | `/tax` | Tax ledger (`?market=india&fy=2026`) |
| GET | `/tax/export` | Download CSV for ITR / tax filing |
| GET | `/audit` | Audit log (paginated, filterable) |

---

## 18. Frontend Component Map

```
App
├── Layout
│   ├── Sidebar
│   ├── MarketSelector (India | US | All)
│   └── TopBar (LIVE / PAPER / OFFLINE per market)
│
├── Dashboard
│   ├── PortfolioSummary (value, P&L, MTD, YTD)
│   ├── SignalFeed (strategy_type badge per signal: FUNDAMENTAL | TECHNICAL | HYBRID)
│   └── ApprovalQueue (pending manual approvals)
│
├── Positions
│   ├── PositionTable (ticker, market, strategy, entry, P&L, SL, target, days held)
│   └── PositionDetail (full signal, Claude rationale if fundamental)
│
├── Signals
│   ├── SignalTable (filterable by market, strategy type, action, date)
│   └── SignalDetail (score breakdown, rationale, order outcome)
│
├── TaxLedger
│   ├── RealisedTradesTable
│   ├── TaxSummaryCard
│   └── ExportButton
│
└── Config (admin only)
    ├── StrategyManager
    │   ├── StrategyList (enable/disable toggle per strategy)
    │   └── StrategyEditor (YAML param editor per strategy)
    ├── RiskEditor
    ├── WatchlistManager (per-market)
    └── MarketSettings (broker mode, data sources)
```

---

## 19. Approval Flow

Two channels, same `approval_queue` table. Either can approve or reject.

**Telegram (primary — mobile):**
Signal message includes `strategy_type` label so you know what produced it.
Inline buttons: ✅ Approve | ❌ Reject.
Timeout: 30 minutes → auto-expired (never auto-approved).

**React dashboard (secondary — desktop):**
`ApprovalQueue` polls `/orders?status=pending_approval` every 10 seconds.
Full signal context displayed before approve/reject.

---

## 20. SEBI Compliance (India-Specific)

| Requirement | How it is met |
|---|---|
| Static IP | AWS EC2 Elastic IP registered in Upstox and Zerodha developer consoles |
| Algo ID tagging | Handled by broker SDK; Upstox: `algorithm_type` field; Zerodha: API key tagging |
| OAuth + 2FA | `pyotp` TOTP in `auth.py`; secret in AWS Secrets Manager |
| Audit trail | Append-only `audit_logger.py` — every decision logged before action |
| Family-only use | Single API key, single registered IP — enforced operationally |
| OPS limit | Event-driven system: 2–5 orders/day. Well below 10 OPS threshold. |

---

## 21. Technology Stack

| Component | Technology | Cost |
|---|---|---|
| Backend | Python 3.11+, FastAPI | Free |
| AI analysis | Claude Sonnet (`claude-sonnet-4-6`) | Pay per token |
| India paper broker | Upstox Sandbox API | Free |
| India live broker Ph3 | Upstox Live API | Free API; brokerage fees on trades |
| India live broker Ph4 | Zerodha Kite Connect | ₹500/month data |
| US paper + live broker | Alpaca (`alpaca-py`) | Free paper; commission-free live |
| India announcements | `BseIndiaApi`, `nse-bse-api` | Free |
| US announcements | SEC EDGAR API | Free |
| India market data | Upstox free quotes API | Free |
| US market data | `yfinance` | Free |
| Technical indicators | `pandas-ta` or `ta-lib` | Free |
| India announcements add-on | StockInsights.ai | Paid optional |
| PDF extraction | `pdfplumber`, `pymupdf` | Free |
| Database | PostgreSQL 15+ | Free |
| Task scheduling | APScheduler | Free |
| Deployment | AWS EC2 `t3.small` + Elastic IP | ~$15–20/month |
| Secrets | AWS Secrets Manager | ~$1/month |
| Alerts | Telegram Bot API | Free |
| Frontend | React, Zustand, Axios | Free |

**Baseline cost: ~$20/month infrastructure + ~$10–30/month Claude API at moderate volume**

---

## 22. Build Sequence

### Phase 1 — Signal validation, fundamental strategy (weeks 1–6)

1. Scaffold project: uv, pyproject.toml, Docker Compose (app + postgres)
2. Build `infrastructure/` layer: event bus, config registry (hot-reload), DB models, Alembic, audit logger, `SecretsProvider` (`env` backend), `WatchlistProvider`
3. Implement `MarketContext` loader — India only
4. Build `AnnouncementFeed` (BSE + NSE free pollers)
5. Build `FundamentalStrategy`: pdf_extractor + claude_client + composite_scorer
6. Build `StrategyEngine` with registry — load and run `fundamental_v1` only
7. Run on at least 3 most recent completed quarters — validate signal quality manually
8. Tune weights in `config/strategies/fundamental.yaml`
9. Build `backtester/` CLI: `python -m backtester run --strategy fundamental_v1 --market india --quarters last3`

**Exit criteria:** ≥ 80% agreement between AI signal and manual assessment on historical test cases.

### Phase 2 — Paper trading, both markets (weeks 7–14)

1. Build `RiskGuardAgent` (all checks, market-aware)
2. Build `TraderAgent` with `UpstoxAdapter(sandbox=True)` for India, `AlpacaAdapter(paper=True)` for US
3. Add `CandleFeed` and `TechnicalStrategy` (disabled — build and validate but keep off)
4. Build `MonitorAgent`: P&L, tax ledger, stoploss watcher
5. Build alert service: Telegram bot (alerts + approval buttons)
6. Build FastAPI layer and React dashboard
7. Run both markets on paper — validate end-to-end behaviour

**Exit criteria:** Stable for 4 weeks; positive expected value on paper.

### Phase 3 — Live trading, small size (weeks 15–22)

1. Deploy AWS EC2 + Elastic IP; register with Upstox
2. Switch India: `broker: upstox_live`; US: `broker: alpaca_live`
3. `auto_threshold: 95` — almost all manual approval initially
4. India caps: `max_order_size_inr: 50000`, 3 concurrent positions
5. US caps: `max_order_size_usd: 500`, 2 concurrent positions
6. Monitor every trade manually for 4 weeks
7. Gradually loosen thresholds as confidence builds

**Exit criteria:** 20+ live trades, system behaviour matches paper, no unexpected errors.

### Phase 4 — Scaled operation (ongoing)

1. Switch India to Zerodha (`broker: zerodha`)
2. Enable `technical_v1` — run alongside fundamental, compare signal quality
3. Enable `hybrid_v1` — requires both signals to agree
4. Expand watchlists (India: Nifty 500; US: full S&P 500)
5. Add exit signal logic (not just entry)
6. Enable consensus data add-on when budget permits
7. Add sentiment strategy, volume anomaly strategy — each as a new `BaseStrategy` subclass

---

## 23. Open Decisions

| Decision | Status | Notes |
|---|---|---|
| Watchlist scope at launch | Resolved | Config-driven via `config/watchlist/india.yaml`; supports static, index membership, and index range sources |
| Active markets at launch | Resolved | India only for Phase 1; US added in Phase 2 |
| Approval channel preference | TBD | Telegram primary + React secondary (as designed) |
| Technical indicator library | TBD | `pandas-ta` (pure Python, easy) vs `ta-lib` (faster, C dependency) |
| Candle timeframe for technical | TBD | Daily for trend-following; 1H for faster signals |
| Position sizing model | TBD | Fixed % to start; Kelly / volatility-adjusted in Phase 4 |
| US tax rules | TBD | Bracket-dependent; configure per user in `config/markets/us.yaml` |
| IBKR as US backup broker | TBD | Implement `IBKRAdapter` in Phase 4 if needed |

---

*Document status: Design v4 — pluggable strategy engine, config-driven watchlist, environment-aware secrets, open/closed architecture, multi-market, free-first. Implementation starting.*
