from __future__ import annotations
from dataclasses import dataclass, field
from datetime import time
from typing import Optional


@dataclass
class TaxRules:
    short_term_days: int = 365
    short_term_rate: Optional[float] = None
    long_term_rate: Optional[float] = None
    wash_sale_rule: bool = False
    intraday: Optional[str] = None
    long_term_exemption_inr: Optional[float] = None


@dataclass
class Sessions:
    market_open: time
    market_close: time
    preopen_start: Optional[time] = None
    preopen_end: Optional[time] = None


@dataclass
class MarketContext:
    market_id: str
    currency: str
    timezone: str
    exchanges: list[str]
    sessions: Sessions
    announcement_sources: dict
    market_data: dict
    broker_config_key: str
    tax_rules: TaxRules
    circuit_breaker_enabled: bool = False
    static_ip_required: bool = False
    algo_registration_threshold_ops: Optional[int] = None
