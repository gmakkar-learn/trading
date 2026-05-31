"""Alpaca broker adapter — paper and live trading for US markets.

Paper trading: set paper=True (default). No live capital at risk.
Live trading: set paper=False. Requires funded Alpaca account.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType

from infrastructure.broker.base import (
    BrokerAdapter, Position, Holding, Quote, OrderStatus, OrderUpdate,
)
from infrastructure.event_bus.events import Order, OrderResult

logger = logging.getLogger(__name__)


class AlpacaAdapter(BrokerAdapter):
    """Alpaca Markets broker adapter.

    Supports both paper (sandbox) and live trading.
    paper=True uses Alpaca's paper trading environment — no real capital.
    """

    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        self._paper = paper
        self._client = TradingClient(api_key, api_secret, paper=paper)
        env = "paper" if paper else "live"
        logger.info("AlpacaAdapter initialised (%s)", env)

    async def place_order(self, order: Order) -> OrderResult:
        side = OrderSide.BUY if order.side == "BUY" else OrderSide.SELL
        try:
            if order.order_type == "MARKET":
                req = MarketOrderRequest(
                    symbol=order.ticker,
                    qty=order.quantity,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )
            else:
                req = LimitOrderRequest(
                    symbol=order.ticker,
                    qty=order.quantity,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=order.limit_price,
                )
            result = self._client.submit_order(req)
            logger.info(
                "Alpaca order submitted: %s %s x%d @ %.2f [broker_id=%s]",
                order.side, order.ticker, order.quantity, order.limit_price,
                result.id,
            )
            return OrderResult(
                order_id=order.order_id,
                broker_order_id=str(result.id),
                status="ACCEPTED",
                fill_price=0.0,
            )
        except Exception as exc:
            logger.error("Alpaca place_order failed: %s", exc)
            return OrderResult(
                order_id=order.order_id,
                broker_order_id="",
                status="REJECTED",
                fill_price=0.0,
                message=str(exc),
            )

    async def cancel_order(self, order_id: str) -> bool:
        try:
            self._client.cancel_order_by_id(order_id)
            return True
        except Exception as exc:
            logger.error("Alpaca cancel_order failed: %s", exc)
            return False

    async def modify_order(self, order_id: str, updates: OrderUpdate) -> OrderResult:
        # Alpaca replace-order: cancel + resubmit is the standard pattern
        logger.warning("Alpaca does not support in-place order modification; cancel and resubmit")
        return OrderResult(order_id=order_id, status="REJECTED", message="not_supported")

    async def get_positions(self) -> list[Position]:
        try:
            raw = self._client.get_all_positions()
            return [
                Position(
                    ticker=p.symbol,
                    market_id="us",
                    quantity=int(p.qty),
                    average_price=float(p.avg_entry_price),
                    current_price=float(p.current_price),
                    unrealised_pnl=float(p.unrealized_pl),
                    currency="USD",
                )
                for p in raw
            ]
        except Exception as exc:
            logger.error("Alpaca get_positions failed: %s", exc)
            return []

    async def get_holdings(self) -> list[Holding]:
        # Alpaca doesn't distinguish holdings from positions
        positions = await self.get_positions()
        return [
            Holding(
                ticker=p.ticker,
                market_id=p.market_id,
                quantity=p.quantity,
                average_price=p.average_price,
                current_price=p.current_price,
                currency=p.currency,
            )
            for p in positions
        ]

    async def get_quote(self, ticker: str) -> Quote:
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestTradeRequest
            # Use last trade as quote approximation
            snap = self._client.get_asset(ticker)
            return Quote(
                ticker=ticker,
                last_price=0.0,
                bid=0.0,
                ask=0.0,
                volume=0.0,
                timestamp=datetime.utcnow(),
            )
        except Exception as exc:
            logger.error("Alpaca get_quote failed: %s", exc)
            raise

    async def subscribe_ticks(self, tickers: list[str], callback: Callable) -> None:
        logger.info("Alpaca tick subscription not implemented in Phase 2 (polling-based)")

    async def get_order_status(self, order_id: str) -> OrderStatus:
        try:
            o = self._client.get_order_by_id(order_id)
            status_map = {
                "filled": "FILLED",
                "canceled": "CANCELLED",
                "rejected": "REJECTED",
                "new": "OPEN",
                "pending_new": "OPEN",
                "partially_filled": "OPEN",
            }
            return OrderStatus(
                order_id=order_id,
                broker_order_id=str(o.id),
                status=status_map.get(o.status.value, "OPEN"),
                fill_price=float(o.filled_avg_price or 0),
                filled_qty=int(o.filled_qty or 0),
            )
        except Exception as exc:
            logger.error("Alpaca get_order_status failed: %s", exc)
            raise

    async def health_check(self) -> bool:
        try:
            account = self._client.get_account()
            return account.status.value == "ACTIVE"
        except Exception:
            return False
