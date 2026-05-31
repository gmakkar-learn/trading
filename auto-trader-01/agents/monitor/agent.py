"""MonitorAgent — records trades, watches stoploss/targets, updates P&L.

Subscribes to OrderPlacedEvent. For each filled trade:
1. Records TradeRecord in the audit log
2. Places a stoploss order via BrokerAdapter
3. Starts a background watch loop (polls position status every 60s)
4. Sends fill confirmation alert via Telegram
5. On stoploss/target hit: closes position, records tax event, alerts

This agent is market-agnostic: all market-specific behaviour comes from
MarketContext (currency, tax rules) and BrokerAdapter (order format).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from infrastructure.audit.audit_logger import AuditLogger
from infrastructure.broker.base import BrokerAdapter
from infrastructure.event_bus.bus import EventBus
from infrastructure.event_bus.events import Order, OrderPlacedEvent, TradeRecord
from infrastructure.market_context.context import MarketContext

logger = logging.getLogger(__name__)

_WATCH_INTERVAL = 60    # seconds between position polls
_STOPLOSS_PCT = 0.05    # 5% below entry
_TARGET_PCT = 0.10      # 10% above entry


class MonitorAgent:
    """Records trades, monitors positions, manages stoploss and targets."""

    def __init__(
        self,
        event_bus: EventBus,
        broker: BrokerAdapter,
        market_context: MarketContext,
        audit_logger: AuditLogger,
        telegram_sender=None,
    ):
        self._bus = event_bus
        self._broker = broker
        self._ctx = market_context
        self._audit = audit_logger
        self._telegram = telegram_sender

        # active_positions: ticker → TradeRecord
        self._active: dict[str, TradeRecord] = {}
        self._watch_tasks: dict[str, asyncio.Task] = {}

        self._bus.subscribe(OrderPlacedEvent, self._on_order_placed)

    async def _on_order_placed(self, event: OrderPlacedEvent) -> None:
        if event.trade is None:
            return
        trade = event.trade
        if trade.market_id != self._ctx.market_id:
            return

        logger.info("Trade recorded: %s %s x%d @ %.2f", trade.side, trade.ticker, trade.quantity, trade.fill_price)

        await self._audit.log(
            market_id=trade.market_id,
            ticker=trade.ticker,
            decision="TRADE_FILL",
            reason=f"side={trade.side} qty={trade.quantity} fill={trade.fill_price} broker={trade.broker_order_id}",
            signal_id=trade.signal_id,
            order_id=trade.order_id,
        )

        if self._telegram is not None:
            await self._telegram.send_fill_alert(trade)

        if trade.side == "BUY":
            self._active[trade.ticker] = trade
            stoploss_price = round(trade.fill_price * (1 - _STOPLOSS_PCT), 2)
            await self._place_stoploss(trade, stoploss_price)
            task = asyncio.create_task(self._watch_position(trade, stoploss_price))
            self._watch_tasks[trade.ticker] = task

    async def _place_stoploss(self, trade: TradeRecord, stoploss_price: float) -> None:
        """Place a sell stoploss order immediately after a buy fill."""
        market_product = "CNC" if trade.market_id == "india" else "DAY"
        order = Order(
            signal_id=trade.signal_id,
            ticker=trade.ticker,
            market_id=trade.market_id,
            side="SELL",
            quantity=trade.quantity,
            order_type="LIMIT",
            limit_price=stoploss_price,
            product_type=market_product,
        )
        result = await self._broker.place_order(order)
        if result.status in ("ACCEPTED", "FILLED"):
            logger.info("Stoploss placed for %s @ %.2f", trade.ticker, stoploss_price)
        else:
            logger.error("Stoploss order REJECTED for %s: %s", trade.ticker, result.message)
            if self._telegram is not None:
                await self._telegram.send_alert(
                    f"ALERT: Stoploss rejected for {trade.ticker}: {result.message}"
                )

    async def _watch_position(self, trade: TradeRecord, stoploss_price: float) -> None:
        """Poll position until stoploss or target is hit, then record exit."""
        entry = trade.fill_price
        target_price = round(entry * (1 + _TARGET_PCT), 2)
        ticker = trade.ticker

        while True:
            await asyncio.sleep(_WATCH_INTERVAL)

            try:
                quote = await self._broker.get_quote(ticker)
                current = quote.last_price
            except Exception as exc:
                logger.warning("Quote fetch failed for %s: %s", ticker, exc)
                continue

            if current <= stoploss_price:
                logger.info("%s stoploss hit: %.2f <= %.2f", ticker, current, stoploss_price)
                await self._close_position(trade, current, reason="stoploss")
                break

            if current >= target_price:
                logger.info("%s target hit: %.2f >= %.2f", ticker, current, target_price)
                await self._close_position(trade, current, reason="target")
                break

    async def _close_position(self, trade: TradeRecord, exit_price: float, reason: str) -> None:
        market_product = "CNC" if trade.market_id == "india" else "DAY"
        close_order = Order(
            signal_id=trade.signal_id,
            ticker=trade.ticker,
            market_id=trade.market_id,
            side="SELL",
            quantity=trade.quantity,
            order_type="MARKET",
            limit_price=0.0,
            product_type=market_product,
        )
        result = await self._broker.place_order(close_order)

        gain_loss = (exit_price - trade.fill_price) * trade.quantity
        holding_days = (date.today() - trade.filled_at.date()).days
        tax_class = self._classify_tax(holding_days, trade.market_id)

        await self._audit.log(
            market_id=trade.market_id,
            ticker=trade.ticker,
            decision="POSITION_CLOSED",
            reason=f"reason={reason} exit={exit_price:.2f} pnl={gain_loss:.2f} tax={tax_class}",
            signal_id=trade.signal_id,
            order_id=close_order.order_id,
        )

        if self._telegram is not None:
            pnl_str = f"+{gain_loss:.2f}" if gain_loss >= 0 else f"{gain_loss:.2f}"
            symbol = "INR" if trade.market_id == "india" else "USD"
            await self._telegram.send_alert(
                f"Position closed: {trade.ticker} [{reason.upper()}]\n"
                f"Entry: {trade.fill_price:.2f} → Exit: {exit_price:.2f}\n"
                f"P&L: {pnl_str} {symbol} ({tax_class})"
            )

        self._active.pop(trade.ticker, None)
        self._watch_tasks.pop(trade.ticker, None)

    @staticmethod
    def _classify_tax(holding_days: int, market_id: str) -> str:
        threshold = 365
        if holding_days < threshold:
            return "STCG"
        return "LTCG"

    def get_active_positions(self) -> dict[str, TradeRecord]:
        return dict(self._active)
