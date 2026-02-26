"""ConvictionAlert ORM model — persists high-conviction multi-source alerts."""

from datetime import datetime

from sqlalchemy import Float, Index, Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from alpha_bot.storage.models import Base


class ConvictionAlert(Base):
    __tablename__ = "conviction_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ca: Mapped[str] = mapped_column(String(64), nullable=False)
    chain: Mapped[str] = mapped_column(String(16), default="base")
    ticker: Mapped[str] = mapped_column(String(32), default="")
    conviction_score: Mapped[float] = mapped_column(Float, nullable=False)
    distinct_sources: Mapped[int] = mapped_column(Integer, default=0)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    price_at_alert: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    roi_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    roi_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    alerted_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_conviction_ca", "ca"),
        Index("ix_conviction_alerted_at", "alerted_at"),
    )
