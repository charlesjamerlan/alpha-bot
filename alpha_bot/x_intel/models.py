"""XSignal ORM model — stores X/Twitter signals ingested from external scraper."""

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from alpha_bot.storage.models import Base


class XSignal(Base):
    __tablename__ = "x_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tweet_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    author_username: Mapped[str] = mapped_column(String(64), nullable=False)
    author_followers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    cashtags: Mapped[str] = mapped_column(Text, default="[]")  # JSON array
    contract_addresses: Mapped[str] = mapped_column(Text, default="[]")  # JSON array
    tweet_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)  # kol_mention | cashtag | narrative | exit_signal
    tweeted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("ix_xsignal_tweet_id", "tweet_id", unique=True),
        Index("ix_xsignal_tweeted_at", "tweeted_at"),
        Index("ix_xsignal_signal_type", "signal_type"),
    )
