"""Loads and caches YAML configs. Keys are relative paths without .yaml extension."""
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class ConfigRegistry:
    def __init__(self, config_dir: str | Path = "config") -> None:
        self._config_dir = Path(config_dir)
        self._cache: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        """key = relative path without .yaml, e.g. 'strategies/fundamental'"""
        if key not in self._cache:
            self._cache[key] = self._load(key)
        return self._cache[key]

    def _load(self, key: str) -> Any:
        path = self._config_dir / f"{key}.yaml"
        with open(path) as f:
            return yaml.safe_load(f)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def reload_all(self) -> None:
        for key in list(self._cache):
            try:
                self._cache[key] = self._load(key)
            except Exception as exc:
                logger.error("Failed to reload config %s: %s", key, exc)
