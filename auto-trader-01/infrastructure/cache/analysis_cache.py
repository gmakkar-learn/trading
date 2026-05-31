"""Disk cache for Claude analysis results, keyed by filing_id + prompt version.
Cache is automatically invalidated when the extraction prompt changes."""
from __future__ import annotations
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AnalysisCache:
    def __init__(self, cache_dir: Path | str = ".cache/analyses", prompt_text: str = "") -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Short hash of prompt — changes here invalidate all cached analyses
        self._pv = hashlib.sha256(prompt_text.encode()).hexdigest()[:8]

    def _path(self, filing_id: str) -> Path:
        safe = filing_id.replace("-", "_").replace("/", "_")
        return self._dir / f"{safe}_{self._pv}.txt"

    def get(self, filing_id: str) -> str | None:
        """Return cached raw Claude response text, or None on miss."""
        p = self._path(filing_id)
        if p.exists():
            logger.debug("Analysis cache hit: %s", filing_id)
            return p.read_text(encoding="utf-8")
        return None

    def put(self, filing_id: str, raw_response: str) -> None:
        self._path(filing_id).write_text(raw_response, encoding="utf-8")
        logger.debug("Analysis cached: %s", filing_id)
