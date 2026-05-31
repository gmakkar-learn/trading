"""TraderAgent — executes approved order proposals.

Subscribes to OrderProposalEvent. Decision path:
- composite_score >= auto_threshold → auto-execute immediately
- composite_score < auto_threshold  → send Telegram approval request, wait

On approval (Telegram button click or dashboard confirm):
    → calls BrokerAdapter.place_order()
    → publishes OrderPlacedEvent

On rejection / timeout:
    → publishes SignalRejectedEvent with reason

Audit log entry is written before every order attempt, regardless of outcome.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from infrastructure.audit.audit_logger import AuditLogger
from infrastructure.broker.base import BrokerAdapter
from infrastructure.event_bus.bus import EventBus
from infrastructure.event_bus.events import (
    ApprovalEvent, ApprovalRequest, Order, OrderPlacedEvent,
    OrderProposalEvent, SignalRejectedEvent, TradeRecord,
)
from infrastructure.market_context.context import MarketContext

logger = logging.getLogger(__name__)


class TraderAgent:
    """Places orders after receiving approved proposals from Risk Guard."""

    def __init__(
        self,
        event_bus: EventBus,
        broker: BrokerAdapter,
        market_context: MarketContext,
        audit_logger: AuditLogger,
        system_config: dict,
        telegram_sender=None,          # optional: infrastructure.telegram.TelegramSender
    ):
        self._bus = event_bus
        self._broker = broker
        self._ctx = market_context
        self._audit = audit_logger
        self._telegram = telegram_sender
        self._auto_threshold = system_config.get("trader", {}).get("auto_threshold", 80)
        self._approval_timeout = (
            system_config.get("alerts", {}).get("approval_timeout_min", 30) * 60
        )

        # pending approvals: request_id → asyncio.Future[bool]
        self._pending: dict[str, asyncio.Future[bool]] = {}

        self._bus.subscribe(OrderProposalEvent, self._on_proposal)
        self._bus.subscribe(ApprovalEvent, self._on_approval)

    async def _on_proposal(self, event: OrderProposalEvent) -> None:
        if event.proposal is None:
            return
        p = event.proposal
        if p.market_id != self._ctx.market_id:
            return

        if p.composite_score >= self._auto_threshold:
            logger.info(
                "Auto-executing: score=%.1f >= threshold=%d [%s]",
                p.composite_score, self._auto_threshold, p.ticker,
            )
            await self._execute(p, approver="auto")
        else:
            logger.info(
                "Manual approval required: score=%.1f < threshold=%d [%s]",
                p.composite_score, self._auto_threshold, p.ticker,
            )
            await self._request_approval(p)

    async def _request_approval(self, proposal) -> None:
        timeout_at = datetime.now(timezone.utc) + timedelta(seconds=self._approval_timeout)
        req = ApprovalRequest(
            proposal_id=proposal.proposal_id,
            ticker=proposal.ticker,
            market_id=proposal.market_id,
            side=proposal.side,
            quantity=proposal.quantity,
            limit_price=proposal.limit_price,
            composite_score=proposal.composite_score,
            rationale=proposal.rationale,
            expires_at=timeout_at,
        )

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending[req.request_id] = fut

        if self._telegram is not None:
            await self._telegram.send_approval_request(req)
        else:
            logger.warning("No Telegram sender configured — approval request %s will time out", req.request_id)

        try:
            approved = await asyncio.wait_for(fut, timeout=self._approval_timeout)
        except asyncio.TimeoutError:
            approved = False
            logger.info("Approval timed out for %s [%s]", proposal.ticker, req.request_id)
        finally:
            self._pending.pop(req.request_id, None)

        if approved:
            await self._execute(proposal, approver="telegram")
        else:
            await self._bus.publish(SignalRejectedEvent(
                ticker=proposal.ticker,
                market_id=proposal.market_id,
                reason="approval_rejected_or_timed_out",
                signal_id=proposal.signal_id,
            ))

    async def _on_approval(self, event: ApprovalEvent) -> None:
        fut = self._pending.get(event.request_id)
        if fut is None:
            logger.warning("Received approval for unknown request_id=%s", event.request_id)
            return
        if not fut.done():
            fut.set_result(event.approved)

    async def _execute(self, proposal, approver: str) -> None:
        market_product = "CNC" if proposal.market_id == "india" else "DAY"
        order = Order(
            proposal_id=proposal.proposal_id,
            signal_id=proposal.signal_id,
            ticker=proposal.ticker,
            market_id=proposal.market_id,
            side=proposal.side,
            quantity=proposal.quantity,
            order_type="LIMIT",
            limit_price=proposal.limit_price,
            product_type=market_product,
        )

        await self._audit.log(
            market_id=proposal.market_id,
            ticker=proposal.ticker,
            decision="ORDER_ATTEMPT",
            reason=f"approver={approver}",
            signal_id=proposal.signal_id,
            order_id=order.order_id,
        )

        result = await self._broker.place_order(order)

        await self._audit.log(
            market_id=proposal.market_id,
            ticker=proposal.ticker,
            decision="ORDER_RESULT",
            reason=f"status={result.status} broker_id={result.broker_order_id}",
            signal_id=proposal.signal_id,
            order_id=order.order_id,
        )

        if result.status in ("ACCEPTED", "FILLED"):
            currency = "INR" if proposal.market_id == "india" else "USD"
            trade = TradeRecord(
                order_id=order.order_id,
                broker_order_id=result.broker_order_id,
                signal_id=proposal.signal_id,
                ticker=proposal.ticker,
                market_id=proposal.market_id,
                strategy_type="",     # filled by strategy context if needed
                side=proposal.side,
                quantity=proposal.quantity,
                fill_price=result.fill_price,
                currency=currency,
            )
            await self._bus.publish(OrderPlacedEvent(trade=trade))
            logger.info(
                "Order placed: %s %s x%d [broker_id=%s] via %s",
                proposal.side, proposal.ticker, proposal.quantity,
                result.broker_order_id, approver,
            )
        else:
            logger.error(
                "Order rejected by broker: %s %s — %s",
                proposal.ticker, result.status, result.message,
            )
            await self._bus.publish(SignalRejectedEvent(
                ticker=proposal.ticker,
                market_id=proposal.market_id,
                reason=f"broker_rejected:{result.message}",
                signal_id=proposal.signal_id,
            ))
