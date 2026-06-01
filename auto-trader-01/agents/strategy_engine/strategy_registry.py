"""Loads active strategies from active.yaml and instantiates them."""
import importlib
import logging
from pathlib import Path

import yaml

from .base_strategy import BaseStrategy
from infrastructure.config_registry.loader import ConfigRegistry

logger = logging.getLogger(__name__)

_STRATEGY_MODULES: dict[str, str] = {
    "fundamental_v1": "agents.strategy_engine.strategies.fundamental.strategy",
    "technical_v1":   "agents.strategy_engine.strategies.technical.strategy",
    "hybrid_v1":      "agents.strategy_engine.strategies.hybrid.strategy",
}
_STRATEGY_CLASSES: dict[str, str] = {
    "fundamental_v1": "FundamentalStrategy",
    "technical_v1":   "TechnicalStrategy",
    "hybrid_v1":      "HybridStrategy",
}


def load_active_strategies(
    market_id: str,
    config_registry: ConfigRegistry,
    config_dir: Path = Path("config"),
) -> list[BaseStrategy]:
    active_path = config_dir / "strategies" / "active.yaml"
    with open(active_path) as f:
        active_cfg = yaml.safe_load(f)

    strategies: list[BaseStrategy] = []
    for entry in active_cfg.get("active_strategies", []):
        if not entry.get("enabled", False):
            continue
        if market_id not in entry.get("markets", []):
            continue

        if entry.get("type") == "webhook":
            continue  # webhook strategies are handled by their own router, not the strategy engine

        strategy_id = entry["id"]
        config_key = entry["config_file"].removesuffix(".yaml")
        strategy_config = config_registry.get(config_key)

        module_path = _STRATEGY_MODULES.get(strategy_id)
        class_name = _STRATEGY_CLASSES.get(strategy_id)
        if not module_path:
            logger.warning("No module registered for strategy_id=%s; skipping", strategy_id)
            continue

        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            strategies.append(cls(config=strategy_config))
            logger.info("Loaded strategy: %s", strategy_id)
        except Exception as exc:
            logger.error("Failed to load strategy %s: %s", strategy_id, exc, exc_info=True)

    return strategies
