"""Abstract broker adapter — all broker implementations must satisfy this interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from infrastructure.event_bus.events import Order, OrderResult


@dataclass
class Position:
    ticker: str
    market_id: str
    quantity: int
    average_price: float
    current_price: float
    unrealised_pnl: float
    currency: str


@dataclass
class Holding:
    """Long-term delivery holding (distinct from intraday position)."""
    ticker: str
    market_id: str
    quantity: int
    average_price: float
    current_price: float
    currency: str


@dataclass
class Quote:
    ticker: str
    last_price: float
    bid: float
    ask: float
    volume: float
    timestamp: datetime


@dataclass
class OrderUpdate:
    limit_price: float | None = None
    quantity: int | None = None


@dataclass
class OrderStatus:
    order_id: str
    broker_order_id: str
    status: str         # "OPEN" | "FILLED" | "CANCELLED" | "REJECTED"
    fill_price: float
    filled_qty: int
    message: str = ""


@dataclass
class BrokerOrder:
    """Normalised order record returned by get_orders()."""
    broker_order_id: str
    ticker: str
    side: str           # "BUY" | "SELL"
    quantity: int
    order_type: str     # "LIMIT" | "MARKET"
    status: str         # "OPEN" | "FILLED" | "CANCELLED" | "REJECTED"
    limit_price: float
    fill_price: float
    filled_qty: int
    created_at: datetime


class BrokerAdapter(ABC):
    """Abstract base for all broker integrations.

    Business logic never calls broker SDKs directly — it always calls
    a BrokerAdapter. Swapping brokers means writing a new adapter subclass.
    """

    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    async def modify_order(self, order_id: str, updates: OrderUpdate) -> OrderResult: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_holdings(self) -> list[Holding]: ...

    @abstractmethod
    async def get_quote(self, ticker: str) -> Quote: ...

    @abstractmethod
    async def subscribe_ticks(self, tickers: list[str], callback: Callable) -> None: ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderStatus: ...

    @abstractmethod
    async def get_orders(self, status: str = "open") -> list[BrokerOrder]: ...

    @abstractmethod
    async def health_check(self) -> bool: ...
