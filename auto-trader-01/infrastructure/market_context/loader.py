from __future__ import annotations
from datetime import time as dt_time
from pathlib import Path
from typing import Optional

import yaml

from .context import MarketContext, Sessions, TaxRules


def _parse_time(t: str | None) -> Optional[dt_time]:
    if not t:
        return None
    h, m = map(int, t.split(":"))
    return dt_time(h, m)


def load_market_context(market_id: str, config_dir: str | Path = "config") -> MarketContext:
    path = Path(config_dir) / "markets" / f"{market_id}.yaml"
    with open(path) as f:
        raw = yaml.safe_load(f)

    sess = raw.get("sessions", {})
    sessions = Sessions(
        market_open=_parse_time(sess.get("market_open")),
        market_close=_parse_time(sess.get("market_close")),
        preopen_start=_parse_time(sess.get("preopen_start")),
        preopen_end=_parse_time(sess.get("preopen_end")),
    )

    tax_raw = raw.get("tax_rules", {})
    valid_fields = TaxRules.__dataclass_fields__
    tax_rules = TaxRules(**{k: v for k, v in tax_raw.items() if k in valid_fields})

    reg = raw.get("regulatory", {})
    return MarketContext(
        market_id=raw["market_id"],
        currency=raw["currency"],
        timezone=raw["timezone"],
        exchanges=raw["exchanges"],
        sessions=sessions,
        announcement_sources=raw.get("announcement_sources", {}),
        market_data=raw.get("market_data", {}),
        broker_config_key=raw.get("broker", ""),
        tax_rules=tax_rules,
        circuit_breaker_enabled=reg.get("circuit_breaker_enabled", False),
        static_ip_required=reg.get("static_ip_required", False),
        algo_registration_threshold_ops=reg.get("algo_registration_threshold_ops"),
    )
