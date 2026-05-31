"""Simple asyncio-based pub/sub event bus. No external dependencies."""
import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable, Type

from .events import DataEvent

logger = logging.getLogger(__name__)

EventHandler = Callable[[DataEvent], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[Type[DataEvent], list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: Type[DataEvent], handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: DataEvent) -> None:
        handlers = self._handlers.get(type(event), [])
        if not handlers:
            return
        results = await asyncio.gather(*[h(event) for h in handlers], return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Event handler error for %s: %s", type(event).__name__, result, exc_info=result)
