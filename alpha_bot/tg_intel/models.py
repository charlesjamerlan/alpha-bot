"""ORM models for TG intelligence: call outcomes and channel scores."""

from datetime import datetime

from sqlalchemy import Boolean, Float, Index, Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from alpha_bot.storage.models import Base


class CallOutcome(Base):
    """One row per CA mention in a monitored TG channel."""

    __tablename__ = "call_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Source
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(256), default="")
    message_id: Mapped[int] = mapped_column(Integer, default=0)
    message_text: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(256), default="")

    # Token
    ca: Mapped[str] = mapped_column(String(64), nullable=False)
    chain: Mapped[str] = mapped_column(String(16), default="solana")
    ticker: Mapped[str] = mapped_column(String(32), default="")
    mention_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Prices (filled incrementally)
    price_at_mention: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_6h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_peak: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Market data at mention
    mcap_at_mention: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Classification
    platform: Mapped[str] = mapped_column(String(32), default="unknown")
    narrative_tags: Mapped[str] = mapped_column(Text, default="[]")  # JSON array

    # Computed ROIs (percentage, e.g. 100.0 = doubled)
    roi_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    roi_6h: Mapped[float | None] = mapped_column(Float, nullable=True)
    roi_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    roi_peak: Mapped[float | None] = mapped_column(Float, nullable=True)
    hit_2x: Mapped[bool] = mapped_column(Boolean, default=False)
    hit_5x: Mapped[bool] = mapped_column(Boolean, default=False)

    # Engagement (nullable — filled by Phase 0.3)
    reaction_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reaction_velocity: Mapped[float | None] = mapped_column(Float, nullable=True)
    forward_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Status
    price_check_status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending / partial / complete
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_call_outcomes_channel", "channel_id"),
        Index("ix_call_outcomes_ca", "ca"),
        Index("ix_call_outcomes_ts", "mention_timestamp"),
        Index("ix_call_outcomes_channel_ca", "channel_id", "ca"),
    )


class ChannelScore(Base):
    """Aggregated quality score per TG channel — recomputed from call_outcomes."""

    __tablename__ = "channel_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    channel_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    channel_name: Mapped[str] = mapped_column(String(256), default="")

    # Call counts
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    resolved_calls: Mapped[int] = mapped_column(Integer, default=0)

    # Hit rates (0.0 - 1.0)
    hit_rate_2x: Mapped[float] = mapped_column(Float, default=0.0)
    hit_rate_5x: Mapped[float] = mapped_column(Float, default=0.0)

    # ROI stats
    avg_roi_24h: Mapped[float] = mapped_column(Float, default=0.0)
    avg_roi_peak: Mapped[float] = mapped_column(Float, default=0.0)
    median_roi_24h: Mapped[float] = mapped_column(Float, default=0.0)

    # Best performance areas
    median_time_to_peak: Mapped[str] = mapped_column(String(32), default="")
    best_platform: Mapped[str] = mapped_column(String(32), default="unknown")
    best_mcap_range: Mapped[str] = mapped_column(String(64), default="")

    # Mode 2 prep (stub for Phase 0.4)
    first_mover_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Composite quality (0-100)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)

    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (Index("ix_channel_scores_quality", "quality_score"),)
