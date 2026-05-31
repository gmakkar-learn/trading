from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infrastructure.event_bus.events import DataEvent, TradingSignal
    from agents.strategy_engine.strategy_context import StrategyContext
    from agents.strategy_engine.data_feeds.feed_types import DataFeedType


class BaseStrategy(ABC):
    """All strategies implement this contract.
    To add a strategy: extend this class + create YAML config + add to active.yaml.
    Nothing else in the system changes."""

    def __init__(self, config: dict) -> None:
        self._config = config

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """Unique ID matching the key in active.yaml."""
        ...

    @property
    @abstractmethod
    def subscriptions(self) -> list[DataFeedType]:
        """Declares which data feeds this strategy needs."""
        ...

    @abstractmethod
    async def evaluate(
        self,
        event: DataEvent,
        context: StrategyContext,
    ) -> TradingSignal | None:
        """Core logic. Returns TradingSignal if an opportunity is found, None otherwise.
        None is the normal case — most events do not result in trades."""
        ...

    @property
    def config(self) -> dict:
        return self._config
