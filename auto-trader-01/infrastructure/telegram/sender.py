"""Telegram alert and approval sender.

Sends:
- Signal alerts (new BUY signal detected)
- Approval requests (inline keyboard: Approve / Reject)
- Fill confirmations
- General system alerts

Requires:
    TELEGRAM_BOT_TOKEN  — bot token from @BotFather
    TELEGRAM_CHAT_ID    — your personal chat ID (get via /start with the bot)

Approval flow:
    1. TraderAgent calls send_approval_request(req) with an ApprovalRequest
    2. This sends a Telegram message with [Approve] and [Reject] inline buttons
    3. User taps a button → Telegram sends a callback_query to the bot
    4. handle_callback_query() fires ApprovalEvent on the event bus
    5. TraderAgent's pending Future resolves → order is placed or skipped
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from infrastructure.event_bus.bus import EventBus
from infrastructure.event_bus.events import ApprovalEvent, ApprovalRequest, TradeRecord

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"


class TelegramSender:
    """Sends Telegram alerts and handles approval button callbacks."""

    def __init__(self, bot_token: str, chat_id: str, event_bus: EventBus):
        self._token = bot_token
        self._chat_id = chat_id
        self._bus = event_bus
        self._base = f"https://api.telegram.org/bot{bot_token}"

    async def send_alert(self, message: str) -> None:
        """Send a plain-text alert message."""
        await self._post("sendMessage", {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "Markdown",
        })

    async def send_approval_request(self, req: ApprovalRequest) -> None:
        """Send an approval request with Approve/Reject inline buttons."""
        currency = "₹" if req.market_id == "india" else "$"
        expires_str = req.expires_at.strftime("%H:%M UTC")
        text = (
            f"*Trade Approval Required*\n\n"
            f"Ticker: `{req.ticker}` ({req.market_id.upper()})\n"
            f"Action: *{req.side}*\n"
            f"Quantity: {req.quantity}\n"
            f"Limit: {currency}{req.limit_price:.2f}\n"
            f"Score: {req.composite_score:.1f}/100\n\n"
            f"Rationale:\n_{req.rationale[:300]}_\n\n"
            f"_Expires at {expires_str} — no action = auto-reject_"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{req.request_id}"},
                {"text": "❌ Reject",  "callback_data": f"reject:{req.request_id}"},
            ]]
        }
        await self._post("sendMessage", {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
        })
        logger.info("Approval request sent for %s [%s]", req.ticker, req.request_id)

    async def send_fill_alert(self, trade: TradeRecord) -> None:
        """Send a fill confirmation after an order is executed."""
        currency = "₹" if trade.market_id == "india" else "$"
        text = (
            f"*Order Filled* ✅\n\n"
            f"Ticker: `{trade.ticker}` ({trade.market_id.upper()})\n"
            f"Side: {trade.side}\n"
            f"Qty: {trade.quantity}\n"
            f"Fill price: {currency}{trade.fill_price:.2f}\n"
            f"Broker ref: `{trade.broker_order_id}`"
        )
        await self.send_alert(text)

    async def handle_callback_query(self, callback_data: str, callback_query_id: str) -> None:
        """Called by the webhook/polling handler when a button is tapped."""
        await self._post("answerCallbackQuery", {"callback_query_id": callback_query_id})

        parts = callback_data.split(":", 1)
        if len(parts) != 2:
            logger.warning("Unrecognised callback_data: %s", callback_data)
            return

        action, request_id = parts
        approved = action == "approve"
        await self._bus.publish(ApprovalEvent(
            request_id=request_id,
            approved=approved,
            approver="telegram",
        ))
        logger.info("Approval %s for request_id=%s", "GRANTED" if approved else "DENIED", request_id)

    async def _post(self, method: str, payload: dict) -> dict:
        url = f"{self._base}/{method}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("Telegram %s failed (%d): %s", method, exc.response.status_code, exc.response.text)
            return {}
        except Exception as exc:
            logger.error("Telegram %s error: %s", method, exc)
            return {}
