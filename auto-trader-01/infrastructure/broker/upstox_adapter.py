"""Upstox broker adapter — paper (sandbox) and live trading for India markets.

Sandbox: set sandbox=True. Uses api-sandbox.upstox.com. No real capital at risk.
Live: set sandbox=False. Uses api.upstox.com. Requires funded account + SEBI static IP.

Product type: sandbox uses 'D' (delivery), live uses 'CNC'. Never MIS.
Instrument keys: NSE_EQ|{ISIN} format, resolved from Upstox instruments master CSV.
Auth token: fetched via scripts/upstox_auth.py, stored as UPSTOX_ACCESS_TOKEN.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
from datetime import datetime
from typing import Callable

import httpx

from infrastructure.broker.base import (
    BrokerAdapter, BrokerOrder, Position, Holding, Quote, OrderStatus, OrderUpdate,
)
from infrastructure.event_bus.events import Order, OrderResult

logger = logging.getLogger(__name__)

_SANDBOX_BASE = "https://api-sandbox.upstox.com/v2"
_LIVE_BASE = "https://api.upstox.com/v2"
_INSTRUMENTS_CSV_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"

_STATUS_MAP = {
    "complete": "FILLED",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
    "open": "OPEN",
    "pending": "OPEN",
    "open pending": "OPEN",
    "trigger pending": "OPEN",
    "modified pending": "OPEN",
    "modify pending": "OPEN",
    "after market order req received": "OPEN",
}


class UpstoxAdapter(BrokerAdapter):
    """Upstox Markets broker adapter.

    Supports both sandbox (paper) and live trading.
    Sandbox: product=D, base=api-sandbox.upstox.com (order flow only; no quotes/portfolio).
    Live: product=CNC, base=api.upstox.com (full API).
    """

    def __init__(self, access_token: str, sandbox: bool = True):
        self._sandbox = sandbox
        self._base = _SANDBOX_BASE if sandbox else _LIVE_BASE
        self._product = "D" if sandbox else "CNC"
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # ticker → NSE_EQ|ISIN instrument key; populated lazily from Upstox CDN
        self._instrument_cache: dict[str, str] = {}
        # ticker → last_price from instruments master (used as sandbox quote fallback)
        self._instrument_prices: dict[str, float] = {}
        env = "sandbox" if sandbox else "live"
        logger.info("UpstoxAdapter initialised (%s, product=%s)", env, self._product)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=30.0)

    async def _ensure_instruments(self) -> None:
        if self._instrument_cache:
            return
        logger.info("Loading Upstox instruments master from CDN...")
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(_INSTRUMENTS_CSV_URL)
            resp.raise_for_status()
        with gzip.open(io.BytesIO(resp.content)) as f:
            reader = csv.DictReader(io.TextIOWrapper(f))
            for row in reader:
                if row.get("exchange") == "NSE_EQ" and row.get("instrument_type") == "EQUITY":
                    symbol = row["tradingsymbol"].upper()
                    self._instrument_cache[symbol] = row["instrument_key"]
                    try:
                        self._instrument_prices[symbol] = float(row.get("last_price", 0) or 0)
                    except ValueError:
                        self._instrument_prices[symbol] = 0.0
        logger.info("Loaded %d NSE_EQ equity instruments", len(self._instrument_cache))

    def _instrument_token(self, ticker: str) -> str:
        key = self._instrument_cache.get(ticker.upper())
        if not key:
            logger.warning("Instrument key not found for %s, using symbol fallback", ticker)
            return f"NSE_EQ|{ticker}"
        return key

    async def place_order(self, order: Order) -> OrderResult:
        await self._ensure_instruments()
        payload = {
            "quantity": order.quantity,
            "product": self._product,
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
                order_id=order.order_id, broker_order_id="",
                status="REJECTED", fill_price=0.0, message=msg,
            )
        except Exception as exc:
            logger.error("Upstox place_order failed: %s", exc)
            return OrderResult(
                order_id=order.order_id, broker_order_id="",
                status="REJECTED", fill_price=0.0, message=str(exc),
            )

    async def cancel_order(self, order_id: str) -> bool:
        try:
            async with self._client() as client:
                resp = await client.delete(
                    f"{self._base}/order/cancel", params={"order_id": order_id}
                )
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
        if self._sandbox:
            return []  # sandbox does not implement portfolio endpoints
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
        if self._sandbox:
            return []  # sandbox does not implement portfolio endpoints
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
        await self._ensure_instruments()
        token = self._instrument_token(ticker)
        if self._sandbox:
            # Sandbox has no market-quote endpoint; return last_price from instruments master.
            last = self._instrument_prices.get(ticker.upper(), 0.0)
            return Quote(
                ticker=ticker, last_price=last, bid=0.0, ask=0.0,
                volume=0.0, timestamp=datetime.utcnow(),
            )
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
            return OrderStatus(
                order_id=order_id,
                broker_order_id=o.get("order_id", ""),
                status=_STATUS_MAP.get(o.get("status", "").lower(), "OPEN"),
                fill_price=float(o.get("average_price", 0)),
                filled_qty=int(o.get("filled_quantity", 0)),
            )
        except Exception as exc:
            logger.error("Upstox get_order_status failed: %s", exc)
            raise

    async def get_orders(self, status: str = "open") -> list[BrokerOrder]:
        try:
            async with self._client() as client:
                resp = await client.get(f"{self._base}/order/retrieve-all")
                resp.raise_for_status()
                data = resp.json().get("data", [])
            result = []
            for o in data:
                order_status = _STATUS_MAP.get(o.get("status", "").lower(), "OPEN")
                if status == "open" and order_status != "OPEN":
                    continue
                if status == "closed" and order_status == "OPEN":
                    continue
                result.append(BrokerOrder(
                    broker_order_id=o.get("order_id", ""),
                    ticker=o.get("tradingsymbol", ""),
                    side="BUY" if o.get("transaction_type") == "BUY" else "SELL",
                    quantity=int(o.get("quantity", 0)),
                    order_type="MARKET" if o.get("order_type") == "MARKET" else "LIMIT",
                    status=order_status,
                    limit_price=float(o.get("price", 0)),
                    fill_price=float(o.get("average_price", 0)),
                    filled_qty=int(o.get("filled_quantity", 0)),
                    created_at=_parse_upstox_datetime(o.get("order_timestamp")),
                ))
            return result
        except Exception as exc:
            logger.error("Upstox get_orders failed: %s", exc)
            return []

    async def health_check(self) -> bool:
        try:
            async with self._client() as client:
                resp = await client.get(f"{self._base}/order/retrieve-all")
            return resp.status_code == 200
        except Exception:
            return False


def _parse_upstox_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            pass
    return None
