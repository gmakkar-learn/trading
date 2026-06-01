"""FastAPI application — serves the REST API and React dashboard.

Startup sequence:
1. Load config (system, risk, market contexts)
2. Initialise broker adapters (Alpaca paper, Upstox sandbox)
3. Initialise Telegram sender (if enabled)
4. Wire event bus → RiskGuard → Trader → Monitor per market
5. Load strategies and StrategyEngine per market
6. Start APScheduler for polling loops
7. Mount React static build at /

Run with:
    uv run uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routers import health, orders, positions, signals, watchlist, telegram_webhook, debug
from api.state import get_state
from agents.monitor.agent import MonitorAgent
from agents.risk_guard.agent import RiskGuardAgent
from agents.trader.agent import TraderAgent
from agents.strategy_engine.engine import StrategyEngine
from agents.strategy_engine.strategy_context import StrategyContext
from agents.strategy_engine.strategy_registry import load_active_strategies
from infrastructure.audit.audit_logger import AuditLogger
from infrastructure.broker.alpaca_adapter import AlpacaAdapter
from infrastructure.broker.upstox_adapter import UpstoxAdapter
from infrastructure.config_registry.loader import ConfigRegistry
from infrastructure.database.connection import get_session_factory
from infrastructure.event_bus.bus import EventBus
from infrastructure.event_bus.events import TradingSignalEvent
from infrastructure.market_context.loader import load_market_context
from infrastructure.secrets.env_provider import EnvSecretsProvider
from infrastructure.telegram.sender import TelegramSender
from infrastructure.watchlist.provider import WatchlistProvider

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"
_FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "dist"


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = get_state()
    system_cfg = _load_yaml(_CONFIG_DIR / "system" / "default.yaml")
    risk_cfg = _load_yaml(_CONFIG_DIR / "risk" / "default.yaml")
    state.system_config = system_cfg

    secrets = EnvSecretsProvider()
    session_factory = get_session_factory()
    audit = AuditLogger(session_factory)
    state.audit = audit
    bus = EventBus()
    state.event_bus = bus

    active_markets: list[str] = system_cfg.get("active_markets", ["us"])

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_sender = None
    if system_cfg.get("alerts", {}).get("telegram_enabled", False):
        try:
            bot_token = await secrets.get("TELEGRAM_BOT_TOKEN")
            chat_id = await secrets.get("TELEGRAM_CHAT_ID")
            telegram_sender = TelegramSender(bot_token, chat_id, bus)
            state.telegram_sender = telegram_sender
            logger.info("Telegram sender initialised")

            from infrastructure.telegram.poller import TelegramPoller
            poller = TelegramPoller(bot_token, telegram_sender)
            poller_task = asyncio.create_task(poller.start())
            state.system_config["_poller"] = poller       # keep reference for shutdown
            state.system_config["_poller_task"] = poller_task
        except KeyError as exc:
            logger.warning("Telegram enabled but secret missing: %s", exc)

    # ── Per-market pipeline setup ─────────────────────────────────────────────
    registry = ConfigRegistry(_CONFIG_DIR)

    for market_id in active_markets:
        try:
            ctx = load_market_context(market_id, _CONFIG_DIR)
        except FileNotFoundError:
            logger.warning("No market config for '%s' — skipping", market_id)
            continue

        broker = await _create_broker(ctx.broker_config_key, secrets)
        if broker is None:
            logger.warning("Could not initialise broker for %s (%s) — skipping", market_id, ctx.broker_config_key)
            continue

        state.brokers[market_id] = broker

        # Wire downstream pipeline
        RiskGuardAgent(bus, broker, ctx, risk_cfg, audit)
        TraderAgent(bus, broker, ctx, audit, system_cfg, telegram_sender)
        monitor = MonitorAgent(bus, broker, ctx, audit, telegram_sender)
        state.monitor_agents[market_id] = monitor

        # Build StrategyEngine
        watchlist_provider = WatchlistProvider(_CONFIG_DIR)
        await watchlist_provider.refresh(market_id)
        strategy_ctx = StrategyContext(
            market=ctx,
            config_registry=registry,
            watchlist=watchlist_provider,
        )
        strategies = load_active_strategies(market_id, registry, _CONFIG_DIR)
        engine = StrategyEngine(strategies, strategy_ctx, bus, audit)
        state.engines[market_id] = engine

        # Build feed instance (tickers already cached by refresh above)
        tickers = await watchlist_provider.get_tickers(market_id)
        state.feeds[market_id] = _create_feed(market_id, tickers)

        logger.info("Pipeline ready for market: %s (%d strategies, %d tickers)", market_id, len(strategies), len(tickers))

    # ── Signal history listener ───────────────────────────────────────────────
    async def _record_signal(event: TradingSignalEvent) -> None:
        if event.signal:
            import dataclasses
            sig = event.signal
            d = dataclasses.asdict(sig)
            d["created_at"] = sig.created_at.isoformat()
            state.signal_history.append(d)
            if len(state.signal_history) > 200:
                state.signal_history.pop(0)
            # Persist to DB so signals survive restarts
            await audit.log(
                decision="SIGNAL",
                market_id=sig.market_id,
                ticker=sig.ticker,
                signal=sig,
            )

    bus.subscribe(TradingSignalEvent, _record_signal)

    # ── APScheduler ───────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler()
    poll_interval = system_cfg.get("polling", {}).get("normal_interval_seconds", 300)

    for market_id in list(state.engines.keys()):
        scheduler.add_job(
            _poll_market,
            "interval",
            seconds=poll_interval,
            args=[market_id],
            id=f"poll_{market_id}",
        )

    scheduler.start()
    logger.info("Scheduler started — polling every %ds per market", poll_interval)

    yield  # app running

    scheduler.shutdown(wait=False)
    poller = state.system_config.get("_poller")
    poller_task = state.system_config.get("_poller_task")
    if poller:
        poller.stop()
    if poller_task:
        poller_task.cancel()
    logger.info("Shutdown complete")


async def _poll_market(market_id: str) -> None:
    """One polling tick: fetch new announcements → strategy engine → signals."""
    state = get_state()
    engine = state.engines.get(market_id)
    feed = state.feeds.get(market_id)
    if engine is None or feed is None:
        return
    try:
        async for event in feed.stream_events():
            await engine.handle_announcement(event)
    except Exception as exc:
        logger.error("Poll error for %s: %s", market_id, exc, exc_info=True)


def _create_feed(market_id: str, tickers: list[str]):
    if market_id == "us":
        from agents.strategy_engine.data_feeds.announcement_feed import SecEdgarFeed
        return SecEdgarFeed(tickers)
    if market_id == "india":
        from agents.strategy_engine.data_feeds.india_announcement_feed import IndiaAnnouncementFeed
        return IndiaAnnouncementFeed(tickers)
    return None


async def _optional_secret(secrets: EnvSecretsProvider, key: str) -> str | None:
    """Return secret value or None if not set — for optional config like base URLs."""
    try:
        return await secrets.get(key)
    except KeyError:
        return None


async def _create_broker(broker_config_key: str, secrets: EnvSecretsProvider):
    try:
        if broker_config_key == "alpaca_paper":
            api_key = await secrets.get("ALPACA_API_KEY")
            api_secret = await secrets.get("ALPACA_API_SECRET")
            base_url = await _optional_secret(secrets, "ALPACA_BASE_URL")
            return AlpacaAdapter(api_key, api_secret, paper=True, base_url=base_url)

        if broker_config_key == "alpaca_live":
            api_key = await secrets.get("ALPACA_API_KEY")
            api_secret = await secrets.get("ALPACA_API_SECRET")
            base_url = await _optional_secret(secrets, "ALPACA_BASE_URL")
            return AlpacaAdapter(api_key, api_secret, paper=False, base_url=base_url)

        if broker_config_key == "upstox_sandbox":
            token = await secrets.get("UPSTOX_ACCESS_TOKEN")
            return UpstoxAdapter(token, sandbox=True)

        if broker_config_key == "upstox_live":
            token = await secrets.get("UPSTOX_ACCESS_TOKEN")
            return UpstoxAdapter(token, sandbox=False)

    except KeyError as exc:
        logger.warning("Missing secret for broker %s: %s", broker_config_key, exc)
        return None

    logger.warning("Unknown broker_config_key: %s", broker_config_key)
    return None


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="Auto Trader 01", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(signals.router)
app.include_router(positions.router)
app.include_router(orders.router)
app.include_router(watchlist.router)
app.include_router(telegram_webhook.router)
app.include_router(debug.router)

# Serve React dashboard if built
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
