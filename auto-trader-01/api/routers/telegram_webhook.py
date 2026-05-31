"""POST /api/telegram/webhook — receives Telegram callback_query events.

Telegram delivers button-tap events (inline keyboard callbacks) to this endpoint
when a webhook is configured. The TelegramSender routes approval results back to
TraderAgent via the event bus.

Set webhook with:
    curl -X POST https://api.telegram.org/bot<TOKEN>/setWebhook \
        -d url=https://<your-host>/api/telegram/webhook
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from api.state import get_state

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/telegram", tags=["telegram"])


@router.post("/webhook")
async def telegram_webhook(request: Request):
    body = await request.json()

    callback_query = body.get("callback_query")
    if callback_query:
        data = callback_query.get("data", "")
        query_id = callback_query.get("id", "")
        state = get_state()
        if state.telegram_sender is not None:
            await state.telegram_sender.handle_callback_query(data, query_id)
        else:
            logger.warning("Telegram webhook received but no sender configured")

    return {"ok": True}
