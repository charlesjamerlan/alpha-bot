"""ORM model for platform token lifecycle tracking."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from alpha_bot.storage.models import Base


class PlatformToken(Base):
    """A token deployed on a tracked platform (Clanker, Virtuals, Flaunch)."""

    __tablename__ = "platform_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ca: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    chain: Mapped[str] = mapped_column(String(16), default="base")
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(256), default="")
    symbol: Mapped[str] = mapped_column(String(32), default="")
    deploy_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Holder snapshots (filled incrementally by delayed checks)
    holders_1h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    holders_6h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    holders_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    holders_7d: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Market snapshots at checkpoints
    mcap_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    mcap_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_mcap: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_mcap: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Lifecycle flags
    survived_7d: Mapped[bool] = mapped_column(Boolean, default=False)
    reached_100k: Mapped[bool] = mapped_column(Boolean, default=False)
    reached_500k: Mapped[bool] = mapped_column(Boolean, default=False)
    reached_1m: Mapped[bool] = mapped_column(Boolean, default=False)

    # Volume at peak
    volume_24h_at_peak: Mapped[float | None] = mapped_column(Float, nullable=True)
    vol_mcap_ratio_at_peak: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Metadata
    narrative_tags: Mapped[str] = mapped_column(Text, default="[]")
    check_status: Mapped[str] = mapped_column(String(16), default="pending")
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    # --- Platform-specific enrichment (Phase 2.2/2.3) ---
    # Virtuals: correlation with $VIRTUAL token
    virtual_correlation: Mapped[float | None] = mapped_column(Float, nullable=True)
    agent_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    agent_activity_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Flaunch: buyback mechanics
    buyback_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buyback_total_eth: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_buyback_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Bonus/penalty applied on top of percentile (-20 to +20)
    platform_bonus_score: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (
        Index("ix_platform_tokens_platform", "platform"),
        Index("ix_platform_tokens_deploy", "deploy_timestamp"),
        Index("ix_platform_tokens_status", "platform", "check_status"),
    )
