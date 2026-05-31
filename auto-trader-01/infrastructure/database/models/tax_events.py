from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.connection import Base


class TaxEvent(Base):
    __tablename__ = "tax_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String, nullable=False)
    market_id: Mapped[str] = mapped_column(String(20), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    exit_date: Mapped[date] = mapped_column(Date, nullable=True)
    holding_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gain_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_class: Mapped[str | None] = mapped_column(String(30), nullable=True)  # "short_term" | "long_term"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
