"""Shared application state — holds live references to all agents and infrastructure."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppState:
    """Mutable state shared across API routers and background tasks."""
    brokers: dict[str, Any] = field(default_factory=dict)           # market_id → BrokerAdapter
    engines: dict[str, Any] = field(default_factory=dict)           # market_id → StrategyEngine
    monitor_agents: dict[str, Any] = field(default_factory=dict)    # market_id → MonitorAgent
    feeds: dict[str, Any] = field(default_factory=dict)             # market_id → feed instance
    telegram_sender: Any = None
    signal_history: list[dict] = field(default_factory=list)        # in-memory cache, capped at 200
    audit: Any = None                                                # AuditLogger
    system_config: dict = field(default_factory=dict)
    event_bus: Any = None


_state = AppState()


def get_state() -> AppState:
    return _state
