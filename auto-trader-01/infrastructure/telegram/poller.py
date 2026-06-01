"""Telegram long-poll loop — replaces webhook for local/non-public deployments.

Continuously calls getUpdates (long-poll, 30s timeout) and routes
callback_query events to TelegramSender.handle_callback_query().

Run as a background asyncio task alongside the FastAPI server.
Switch to webhook mode in production (EC2 with Elastic IP + HTTPS).
"""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class TelegramPoller:
    def __init__(self, bot_token: str, sender):
        self._token = bot_token
        self._sender = sender
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._offset = 0
        self._running = False

    async def start(self) -> None:
        self._running = True
        logger.info("Telegram long-poll loop started")
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Telegram poll error: %s — retrying in 5s", exc)
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False

    async def _poll_once(self) -> None:
        params = {
            "offset": self._offset,
            "timeout": 30,
            "allowed_updates": ["callback_query"],
        }
        async with httpx.AsyncClient(timeout=40.0) as client:
            resp = await client.get(f"{self._base}/getUpdates", params=params)

        if resp.status_code != 200:
            logger.warning("getUpdates returned %d", resp.status_code)
            await asyncio.sleep(2)
            return

        data = resp.json()
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            cq = update.get("callback_query")
            if cq:
                await self._sender.handle_callback_query(
                    cq.get("data", ""),
                    cq.get("id", ""),
                )
