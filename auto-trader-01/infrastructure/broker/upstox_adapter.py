"""Upstox broker adapter — paper (sandbox) and live trading for India markets.

Sandbox: set sandbox=True. No real capital at risk. Auth token still required.
Live: set sandbox=False. Requires funded Upstox account + SEBI-compliant static IP.

All India orders use product_type=CNC (delivery). Never MIS.
Auth token is fetched via scripts/upstox_auth.py and stored in UPSTOX_ACCESS_TOKEN env var.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

import httpx

from infrastructure.broker.base import (
    BrokerAdapter, Position, Holding, Quote, OrderStatus, OrderUpdate,
)
from infrastructure.event_bus.events import Order, OrderResult

logger = logging.getLogger(__name__)

_SANDBOX_BASE = "https://sandbox.upstox.com/v2"
_LIVE_BASE = "https://api.upstox.com/v2"

# Upstox exchange segment codes
_EXCHANGE_MAP = {"NSE": "NSE_EQ", "BSE": "BSE_EQ"}


class UpstoxAdapter(BrokerAdapter):
    """Upstox Markets broker adapter.

    Supports both sandbox (paper) and live trading.
    All India equity orders use CNC product type — delivery, never MIS.

    Auth: Upstox uses OAuth2. Run scripts/upstox_auth.py to get an access token.
    Store the token as UPSTOX_ACCESS_TOKEN in .env or AWS Secrets Manager.
    """

    def __init__(self, access_token: str, sandbox: bool = True):
        self._sandbox = sandbox
        self._base = _SANDBOX_BASE if sandbox else _LIVE_BASE
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        env = "sandbox" if sandbox else "live"
        logger.info("UpstoxAdapter initialised (%s)", env)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=30.0)

    async def place_order(self, order: Order) -> OrderResult:
        # All India orders: product = CNC (delivery), never MIS
        payload = {
            "quantity": order.quantity,
            "product": "CNC",
            "validity": "DAY",
            "price": order.limit_price if order.order_type == "LIMIT" else 0,
            "tag": f"auto-trader-{order.order_id[:8]}",
            "instrument_token": self._instrument_token(order.ticker),
            "order_type": "LIMIT" if order.order_type == "LIMIT" else "MARKET",
            "transaction_type": order.side,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        try:
            async with self._client() as client:
                resp = await client.post(f"{self._base}/order/place", json=payload)
                resp.raise_for_status()
                data = resp.json()
            broker_id = data.get("data", {}).get("order_id", "")
            logger.info(
                "Upstox order placed: %s %s x%d @ %.2f [broker_id=%s]",
                order.side, order.ticker, order.quantity, order.limit_price, broker_id,
            )
            return OrderResult(
                order_id=order.order_id,
                broker_order_id=broker_id,
                status="ACCEPTED",
                fill_price=0.0,
            )
        except httpx.HTTPStatusError as exc:
            msg = exc.response.text
            logger.error("Upstox place_order HTTP error: %s", msg)
            return OrderResult(
                order_id=order.order_id,
                broker_order_id="",
                status="REJECTED",
                fill_price=0.0,
                message=msg,
            )
        except Exception as exc:
            logger.error("Upstox place_order failed: %s", exc)
            return OrderResult(
                order_id=order.order_id,
                broker_order_id="",
                status="REJECTED",
                fill_price=0.0,
                message=str(exc),
            )

    async def cancel_order(self, order_id: str) -> bool:
        try:
            async with self._client() as client:
                resp = await client.delete(f"{self._base}/order/cancel", params={"order_id": order_id})
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Upstox cancel_order failed: %s", exc)
            return False

    async def modify_order(self, order_id: str, updates: OrderUpdate) -> OrderResult:
        payload = {"order_id": order_id}
        if updates.limit_price is not None:
            payload["price"] = updates.limit_price
        if updates.quantity is not None:
            payload["quantity"] = updates.quantity
        try:
            async with self._client() as client:
                resp = await client.put(f"{self._base}/order/modify", json=payload)
                resp.raise_for_status()
            return OrderResult(order_id=order_id, status="ACCEPTED")
        except Exception as exc:
            logger.error("Upstox modify_order failed: %s", exc)
            return OrderResult(order_id=order_id, status="REJECTED", message=str(exc))

    async def get_positions(self) -> list[Position]:
        try:
            async with self._client() as client:
                resp = await client.get(f"{self._base}/portfolio/short-term-positions")
                resp.raise_for_status()
                data = resp.json().get("data", [])
            return [
                Position(
                    ticker=p["tradingsymbol"],
                    market_id="india",
                    quantity=int(p.get("quantity", 0)),
                    average_price=float(p.get("average_price", 0)),
                    current_price=float(p.get("last_price", 0)),
                    unrealised_pnl=float(p.get("pnl", 0)),
                    currency="INR",
                )
                for p in data
                if p.get("quantity", 0) != 0
            ]
        except Exception as exc:
            logger.error("Upstox get_positions failed: %s", exc)
            return []

    async def get_holdings(self) -> list[Holding]:
        try:
            async with self._client() as client:
                resp = await client.get(f"{self._base}/portfolio/long-term-holdings")
                resp.raise_for_status()
                data = resp.json().get("data", [])
            return [
                Holding(
                    ticker=h["tradingsymbol"],
                    market_id="india",
                    quantity=int(h.get("quantity", 0)),
                    average_price=float(h.get("average_price", 0)),
                    current_price=float(h.get("last_price", 0)),
                    currency="INR",
                )
                for h in data
            ]
        except Exception as exc:
            logger.error("Upstox get_holdings failed: %s", exc)
            return []

    async def get_quote(self, ticker: str) -> Quote:
        token = self._instrument_token(ticker)
        try:
            async with self._client() as client:
                resp = await client.get(
                    f"{self._base}/market-quote/quotes",
                    params={"instrument_key": token},
                )
                resp.raise_for_status()
                data = resp.json().get("data", {}).get(token, {})
            return Quote(
                ticker=ticker,
                last_price=float(data.get("last_price", 0)),
                bid=float(data.get("depth", {}).get("buy", [{}])[0].get("price", 0)),
                ask=float(data.get("depth", {}).get("sell", [{}])[0].get("price", 0)),
                volume=float(data.get("volume", 0)),
                timestamp=datetime.utcnow(),
            )
        except Exception as exc:
            logger.error("Upstox get_quote failed for %s: %s", ticker, exc)
            raise

    async def subscribe_ticks(self, tickers: list[str], callback: Callable) -> None:
        logger.info("Upstox WebSocket tick subscription not implemented in Phase 2 (polling-based)")

    async def get_order_status(self, order_id: str) -> OrderStatus:
        try:
            async with self._client() as client:
                resp = await client.get(
                    f"{self._base}/order/details",
                    params={"order_id": order_id},
                )
                resp.raise_for_status()
                o = resp.json().get("data", {})
            status_map = {
                "complete": "FILLED",
                "cancelled": "CANCELLED",
                "rejected": "REJECTED",
                "open": "OPEN",
                "pending": "OPEN",
            }
            return OrderStatus(
                order_id=order_id,
                broker_order_id=o.get("order_id", ""),
                status=status_map.get(o.get("status", "").lower(), "OPEN"),
                fill_price=float(o.get("average_price", 0)),
                filled_qty=int(o.get("filled_quantity", 0)),
            )
        except Exception as exc:
            logger.error("Upstox get_order_status failed: %s", exc)
            raise

    async def get_orders(self, status: str = "open") -> list:
        logger.info("Upstox get_orders not yet implemented")
        return []

    async def health_check(self) -> bool:
        try:
            async with self._client() as client:
                resp = await client.get(f"{self._base}/user/profile")
            return resp.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _instrument_token(ticker: str) -> str:
        """Convert NSE ticker symbol to Upstox instrument key format."""
        return f"NSE_EQ|{ticker}"
