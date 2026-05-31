"""Fetches index constituents from public sources.
Phase 1: only 'static' source type is used. Index fetching is stubbed for Phase 4."""
import logging

logger = logging.getLogger(__name__)


class IndexFetcher:
    async def get_index_members(self, index: str) -> list[str]:
        """Return all tickers in an index. Phase 1 stub — implement per-index in Phase 4."""
        logger.warning("Index membership fetch not yet implemented for %s; returning []", index)
        return []

    async def get_index_ranked(self, index: str) -> list[str]:
        """Return tickers in rank order. Phase 1 stub."""
        logger.warning("Index rank fetch not yet implemented for %s; returning []", index)
        return []
