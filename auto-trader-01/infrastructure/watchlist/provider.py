"""Config-driven watchlist provider.
Resolves static, index_membership, and index_range sources into a flat ticker list."""
import logging
from pathlib import Path
from typing import Any

import yaml

from .index_fetcher import IndexFetcher

logger = logging.getLogger(__name__)


class WatchlistProvider:
    def __init__(self, config_dir: str | Path = "config") -> None:
        self._config_dir = Path(config_dir)
        self._cache: dict[str, list[str]] = {}
        self._fetcher = IndexFetcher()

    async def get_tickers(self, market_id: str) -> list[str]:
        if market_id not in self._cache:
            await self.refresh(market_id)
        return self._cache[market_id]

    async def refresh(self, market_id: str) -> None:
        path = self._config_dir / "watchlist" / f"{market_id}.yaml"
        if not path.exists():
            logger.warning("No watchlist config for market '%s' at %s", market_id, path)
            self._cache[market_id] = []
            return
        with open(path) as f:
            config = yaml.safe_load(f)

        tickers: set[str] = set()
        for source in config.get("sources", []):
            resolved = await self._resolve_source(source)
            tickers.update(resolved)

        exclude = {t.upper() for t in config.get("exclude", [])}
        self._cache[market_id] = sorted(tickers - exclude)
        logger.info("Watchlist refreshed for %s: %d tickers", market_id, len(self._cache[market_id]))

    def _static_source_index(self, config: dict) -> int | None:
        """Return index of the first 'static' source in config, or None."""
        for i, src in enumerate(config.get("sources", [])):
            if src.get("type") == "static":
                return i
        return None

    async def add_ticker(self, market_id: str, ticker: str) -> bool:
        """Add ticker to the static source in the YAML and update the cache. Returns True if added."""
        ticker = ticker.upper()
        path = self._config_dir / "watchlist" / f"{market_id}.yaml"
        if not path.exists():
            return False
        with open(path) as f:
            config = yaml.safe_load(f) or {}

        idx = self._static_source_index(config)
        if idx is None:
            config.setdefault("sources", []).append({"type": "static", "tickers": []})
            idx = len(config["sources"]) - 1

        existing = [t.upper() for t in config["sources"][idx].get("tickers", [])]
        if ticker in existing:
            return False
        config["sources"][idx].setdefault("tickers", []).append(ticker)

        with open(path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        self._cache.setdefault(market_id, [])
        if ticker not in self._cache[market_id]:
            self._cache[market_id] = sorted(self._cache[market_id] + [ticker])
        logger.info("Added %s to %s watchlist", ticker, market_id)
        return True

    async def remove_ticker(self, market_id: str, ticker: str) -> bool:
        """Remove ticker from the static source in the YAML and update the cache. Returns True if removed."""
        ticker = ticker.upper()
        path = self._config_dir / "watchlist" / f"{market_id}.yaml"
        if not path.exists():
            return False
        with open(path) as f:
            config = yaml.safe_load(f) or {}

        idx = self._static_source_index(config)
        if idx is None:
            return False

        tickers = config["sources"][idx].get("tickers", [])
        new_tickers = [t for t in tickers if t.upper() != ticker]
        if len(new_tickers) == len(tickers):
            return False
        config["sources"][idx]["tickers"] = new_tickers

        with open(path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        if market_id in self._cache:
            self._cache[market_id] = [t for t in self._cache[market_id] if t != ticker]
        logger.info("Removed %s from %s watchlist", ticker, market_id)
        return True

    async def _resolve_source(self, source: dict[str, Any]) -> list[str]:
        source_type = source.get("type")
        if source_type == "static":
            return [t.upper() for t in source.get("tickers", [])]
        elif source_type == "index_membership":
            return await self._fetcher.get_index_members(source["index"])
        elif source_type == "index_range":
            members = await self._fetcher.get_index_ranked(source["index"])
            lo = source.get("rank_from", 1) - 1  # 0-based
            hi = source.get("rank_to", len(members))
            return members[lo:hi]
        else:
            logger.warning("Unknown watchlist source type: %s", source_type)
            return []
