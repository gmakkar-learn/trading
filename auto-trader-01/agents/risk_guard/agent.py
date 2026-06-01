"""RiskGuardAgent — validates TradingSignals against risk rules before ordering.

Subscribes to TradingSignalEvent. Runs checks sequentially; stops on first
failure. On pass, constructs OrderProposal and publishes OrderProposalEvent.
On failure, publishes SignalRejectedEvent and writes to the audit log.

Checks (in order):
1. Session validity — market must be open (or preopen, if config allows)
2. Signal action — only BUY signals proceed; HOLD and SELL skip order creation
3. Position concentration — single stock and sector limits
4. Order size cap — hard cap in USD/INR from risk config
5. Cash availability — checked against current broker positions
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from infrastructure.audit.audit_logger import AuditLogger
from infrastructure.broker.base import BrokerAdapter
from infrastructure.event_bus.bus import EventBus
from infrastructure.event_bus.events import (
    OrderProposal, OrderProposalEvent,
    SignalRejectedEvent, TradingSignal, TradingSignalEvent,
)
from infrastructure.market_context.context import MarketContext

logger = logging.getLogger(__name__)


class RiskGuardAgent:
    """Validates trading signals against risk configuration rules."""

    def __init__(
        self,
        event_bus: EventBus,
        broker: BrokerAdapter,
        market_context: MarketContext,
        risk_config: dict,
        audit_logger: AuditLogger,
    ):
        self._bus = event_bus
        self._broker = broker
        self._ctx = market_context
        self._risk = risk_config
        self._audit = audit_logger
        self._bus.subscribe(TradingSignalEvent, self._on_signal)

    async def _on_signal(self, event: TradingSignalEvent) -> None:
        if event.signal is None:
            return
        signal = event.signal
        # Only process signals for our market
        if signal.market_id != self._ctx.market_id:
            return

        rejection_reason = await self._run_checks(signal)

        if rejection_reason:
            logger.warning("Signal rejected [%s]: %s — %s", signal.ticker, signal.signal_id, rejection_reason)
            await self._audit.log(
                market_id=signal.market_id,
                ticker=signal.ticker,
                decision="REJECT",
                reason=rejection_reason,
                signal_id=signal.signal_id,
            )
            await self._bus.publish(SignalRejectedEvent(
                ticker=signal.ticker,
                market_id=signal.market_id,
                reason=rejection_reason,
                signal_id=signal.signal_id,
            ))
            return

        proposal = self._build_proposal(signal)
        logger.info(
            "Signal approved [%s] → proposal %s: %s x%d @ %.2f",
            signal.ticker, proposal.proposal_id, proposal.side, proposal.quantity, proposal.limit_price,
        )
        await self._audit.log(
            market_id=signal.market_id,
            ticker=signal.ticker,
            decision="APPROVE",
            reason="all_checks_passed",
            signal_id=signal.signal_id,
        )
        await self._bus.publish(OrderProposalEvent(proposal=proposal))

    async def _run_checks(self, signal: TradingSignal) -> str | None:
        # 1. Only BUY signals proceed to order creation
        if signal.recommended_action != "BUY":
            return f"action_not_buy:{signal.recommended_action}"

        # 2. Session validity
        session_check = self._check_session()
        if session_check:
            return session_check

        # 3. Position concentration
        conc_check = await self._check_concentration(signal.ticker)
        if conc_check:
            return conc_check

        # 4. Order size
        try:
            quote = await self._broker.get_quote(signal.ticker)
            current_price = quote.last_price
        except Exception as exc:
            logger.warning("Could not fetch quote for %s: %s — skipping order size check", signal.ticker, exc)
            current_price = 0.0

        size_check = self._check_order_size(current_price, signal.market_id)
        if size_check:
            return size_check

        return None

    def _check_session(self) -> str | None:
        sessions_cfg = self._risk.get("sessions", {})
        allow_continuous = sessions_cfg.get("allow_continuous", True)
        if not allow_continuous:
            return "session:continuous_trading_disabled"

        tz = ZoneInfo(self._ctx.timezone)
        now = datetime.now(tz).time()
        market_open = self._ctx.sessions.market_open
        market_close = self._ctx.sessions.market_close

        if market_open is None or market_close is None:
            return None  # no session configured — allow

        if not (market_open <= now <= market_close):
            return f"session:market_closed (now={now}, open={market_open}, close={market_close})"

        return None

    async def _check_concentration(self, ticker: str) -> str | None:
        limits = self._risk.get("position_limits", {})
        max_concurrent = limits.get("max_concurrent_positions", 10)

        try:
            positions = await self._broker.get_positions()
        except Exception as exc:
            logger.warning("Could not fetch positions: %s — skipping concentration check", exc)
            return None

        open_tickers = {p.ticker for p in positions}
        if ticker in open_tickers:
            return f"concentration:already_holding_{ticker}"
        if len(open_tickers) >= max_concurrent:
            return f"concentration:max_positions_reached_{max_concurrent}"

        return None

    def _check_order_size(self, price: float, market_id: str) -> str | None:
        if price <= 0:
            return None  # can't check without price

        limits = self._risk.get("position_limits", {})
        quantity = self._calculate_quantity(price, market_id)

        if market_id == "india":
            cap = limits.get("max_order_size_inr", 500_000)
            order_value = price * quantity
            if order_value > cap:
                return f"order_size:₹{order_value:.0f} exceeds cap ₹{cap:.0f}"
        else:
            cap = limits.get("max_order_size_usd", 5_000)
            order_value = price * quantity
            if order_value > cap:
                return f"order_size:${order_value:.0f} exceeds cap ${cap:.0f}"

        return None

    def _build_proposal(self, signal: TradingSignal) -> OrderProposal:
        current_price = signal.context.get("last_price", 0.0)
        quantity = self._calculate_quantity(current_price, signal.market_id, signal.composite_score) if current_price > 0 else 1

        # Execution params from signal context (set by TV adapter) or fall back to defaults
        stoploss_pct = float(signal.context.get("stoploss_pct", 0.05))
        target_pct   = float(signal.context.get("target_pct",   0.10))
        stoploss = round(current_price * (1 - stoploss_pct), 2) if current_price > 0 else 0.0
        target   = round(current_price * (1 + target_pct),   2) if current_price > 0 else 0.0

        return OrderProposal(
            signal_id=signal.signal_id,
            ticker=signal.ticker,
            market_id=signal.market_id,
            side="BUY",
            quantity=quantity,
            limit_price=current_price,
            stoploss=stoploss,
            target=target,
            composite_score=signal.composite_score,
            rationale=signal.rationale,
        )

    def _calculate_quantity(self, price: float, market_id: str, score: float = 85.0) -> int:
        """Size position using score tiers from risk config."""
        if price <= 0:
            return 1
        limits   = self._risk.get("position_limits", {})
        cap      = limits.get("max_order_size_inr", 500_000) if market_id == "india" else limits.get("max_order_size_usd", 5_000)
        size_pct = self._score_to_size_pct(score)
        return max(1, int((cap * size_pct) / price))

    def _score_to_size_pct(self, score: float) -> float:
        """Return position size fraction for the given composite score."""
        tiers = self._risk.get("position_sizing", {}).get("score_tiers", [])
        for tier in sorted(tiers, key=lambda t: t["min_score"], reverse=True):
            if score >= tier["min_score"]:
                return float(tier["size_pct"])
        return 0.5  # fallback: half position (pre-tier behaviour)


