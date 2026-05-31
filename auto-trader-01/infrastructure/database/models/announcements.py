import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.connection import Base


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    filing_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    filing_type: Mapped[str] = mapped_column(String(20), nullable=False)
    filing_url: Mapped[str] = mapped_column(Text, nullable=True)
    document_url: Mapped[str] = mapped_column(Text, nullable=True)
    items: Mapped[str] = mapped_column(String(200), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
