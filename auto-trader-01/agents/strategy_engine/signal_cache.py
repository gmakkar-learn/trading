"""Short-lived in-memory signal cache. Used by HybridStrategy to correlate signals."""
from datetime import datetime, timedelta
from infrastructure.event_bus.events import TradingSignal


class SignalCache:
    def __init__(self, ttl_hours: int = 240) -> None:
        self._cache: dict[tuple[str, str], tuple[TradingSignal, datetime]] = {}
        self._ttl = timedelta(hours=ttl_hours)

    def put(self, signal: TradingSignal) -> None:
        self._cache[(signal.ticker, signal.strategy_id)] = (signal, datetime.utcnow())

    def get(self, ticker: str, strategy_id: str) -> TradingSignal | None:
        entry = self._cache.get((ticker, strategy_id))
        if entry is None:
            return None
        signal, ts = entry
        if datetime.utcnow() - ts > self._ttl:
            del self._cache[(ticker, strategy_id)]
            return None
        return signal

    def evict_expired(self) -> None:
        now = datetime.utcnow()
        expired = [k for k, (_, ts) in self._cache.items() if now - ts > self._ttl]
        for k in expired:
            del self._cache[k]
