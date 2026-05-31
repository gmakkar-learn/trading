from datetime import datetime

from sqlalchemy import DateTime, Float, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.connection import Base


class Signal(Base):
    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    strategy_type: Mapped[str] = mapped_column(String(50), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False)
    composite_score: Mapped[float] = mapped_column(Float, nullable=False)
    recommended_action: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
