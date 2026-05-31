import uuid
from datetime import datetime

from sqlalchemy import DateTime, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.connection import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    event_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    market_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ticker: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    decision: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
