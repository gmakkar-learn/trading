"""Disk cache for fetched SEC documents, keyed by URL."""
from __future__ import annotations
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class DocumentCache:
    def __init__(self, cache_dir: Path | str = ".cache/docs") -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode()).hexdigest()
        return self._dir / f"{h}.txt"

    def get(self, url: str) -> str | None:
        p = self._path(url)
        if p.exists():
            logger.debug("Doc cache hit: %s", url[-80:])
            return p.read_text(encoding="utf-8", errors="replace")
        return None

    def put(self, url: str, content: str) -> None:
        self._path(url).write_text(content, encoding="utf-8", errors="replace")
        logger.debug("Doc cached: %s", url[-80:])
