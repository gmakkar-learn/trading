"""All event dataclasses — the typed contracts between system components."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Base ──────────────────────────────────────────────────────────────────────

@dataclass
class DataEvent:
    event_id: str = field(default_factory=_uuid)
    timestamp: datetime = field(default_factory=datetime.utcnow)


# ── Feed events ───────────────────────────────────────────────────────────────

@dataclass
class AnnouncementEvent(DataEvent):
    """Emitted when a new financial filing is detected."""
    market_id: str = ""
    ticker: str = ""
    exchange: str = ""
    filing_id: str = ""
    filing_type: str = ""       # "8-K", "FinancialResults", etc.
    filing_url: str = ""        # URL to filing index page
    document_url: str = ""      # URL to the actual press release document
    items: list[str] = field(default_factory=list)  # e.g. ["2.02", "9.01"]
    published_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CandleEvent(DataEvent):
    """Emitted on OHLCV candle close."""
    ticker: str = ""
    market_id: str = ""
    timeframe: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0


# ── Signal contract ───────────────────────────────────────────────────────────

@dataclass
class TradingSignal:
    """The contract between strategy engine and downstream pipeline.
    Risk Guard, Trader, and Monitor never inspect strategy_type for business logic."""
    signal_id: str = field(default_factory=_uuid)
    ticker: str = ""
    market_id: str = ""
    strategy_type: str = ""     # "fundamental" | "technical" | "hybrid"
    strategy_id: str = ""
    strategy_version: str = "1"
    composite_score: float = 0.0   # 0–100
    recommended_action: str = ""   # "BUY" | "SELL" | "HOLD"
    confidence: str = ""           # "high" | "medium" | "low"
    rationale: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


# ── Strategy engine events ────────────────────────────────────────────────────

@dataclass
class TradingSignalEvent(DataEvent):
    signal: TradingSignal | None = None


@dataclass
class SignalRejectedEvent(DataEvent):
    ticker: str = ""
    market_id: str = ""
    reason: str = ""
    signal_id: str = ""


# ── Risk Guard → Trader ───────────────────────────────────────────────────────

@dataclass
class OrderProposal:
    """Constructed by Risk Guard after all checks pass; consumed by Trader."""
    proposal_id: str = field(default_factory=_uuid)
    signal_id: str = ""
    ticker: str = ""
    market_id: str = ""
    side: str = ""              # "BUY" | "SELL"
    quantity: int = 0
    limit_price: float = 0.0
    stoploss: float = 0.0
    target: float = 0.0
    composite_score: float = 0.0
    rationale: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class OrderProposalEvent(DataEvent):
    proposal: OrderProposal | None = None


# ── Trader → Broker ───────────────────────────────────────────────────────────

@dataclass
class Order:
    """Broker-normalised order passed to BrokerAdapter.place_order()."""
    order_id: str = field(default_factory=_uuid)
    proposal_id: str = ""
    signal_id: str = ""
    ticker: str = ""
    market_id: str = ""
    side: str = ""
    quantity: int = 0
    order_type: str = "LIMIT"   # "LIMIT" | "MARKET"
    limit_price: float = 0.0
    product_type: str = "CNC"   # India: always CNC; US: DAY
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class OrderResult:
    order_id: str = ""
    broker_order_id: str = ""
    status: str = ""            # "ACCEPTED" | "REJECTED" | "FILLED"
    fill_price: float = 0.0
    message: str = ""


# ── Trader → Monitor ──────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    trade_id: str = field(default_factory=_uuid)
    order_id: str = ""
    broker_order_id: str = ""
    signal_id: str = ""
    ticker: str = ""
    market_id: str = ""
    strategy_type: str = ""
    side: str = ""
    quantity: int = 0
    fill_price: float = 0.0
    currency: str = ""
    filled_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class OrderPlacedEvent(DataEvent):
    trade: TradeRecord | None = None


# ── Approval flow ─────────────────────────────────────────────────────────────

@dataclass
class ApprovalRequest:
    request_id: str = field(default_factory=_uuid)
    proposal_id: str = ""
    ticker: str = ""
    market_id: str = ""
    side: str = ""
    quantity: int = 0
    limit_price: float = 0.0
    composite_score: float = 0.0
    rationale: str = ""
    expires_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ApprovalEvent(DataEvent):
    request_id: str = ""
    approved: bool = False
    approver: str = ""          # "telegram" | "dashboard" | "auto"
