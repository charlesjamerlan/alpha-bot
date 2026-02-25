"""ORM models for the scanner: trending themes and discovered candidates."""

from datetime import datetime

from sqlalchemy import Float, Index, Integer, String, Text, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from alpha_bot.storage.models import Base


class TrendingTheme(Base):
    """A trending theme from an external source (Google, Reddit, etc.)."""

    __tablename__ = "trending_themes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    theme: Mapped[str] = mapped_column(String(256), nullable=False)
    velocity: Mapped[float] = mapped_column(Float, default=0.0)
    current_volume: Mapped[int] = mapped_column(Integer, default=0)
    previous_volume: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(64), default="")
    first_seen: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("source", "theme", name="uq_source_theme"),
        Index("ix_trending_velocity", "velocity"),
    )


class ScannerCandidate(Base):
    """A token discovered and scored by the scanner."""

    __tablename__ = "scanner_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ca: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    chain: Mapped[str] = mapped_column(String(16), default="base")
    ticker: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(256), default="")
    platform: Mapped[str] = mapped_column(String(32), default="unknown")

    # Scores (0-100)
    narrative_score: Mapped[float] = mapped_column(Float, default=0.0)
    narrative_depth: Mapped[int] = mapped_column(Integer, default=0)
    profile_match_score: Mapped[float] = mapped_column(Float, default=0.0)
    market_score: Mapped[float] = mapped_column(Float, default=0.0)
    platform_percentile: Mapped[float] = mapped_column(Float, default=0.0)
    composite_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Matched themes (JSON list of theme names)
    matched_themes: Mapped[str] = mapped_column(Text, default="[]")

    # Market data at discovery
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    mcap: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    holder_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pair_age_hours: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Discovery metadata
    discovery_source: Mapped[str] = mapped_column(String(32), default="")
    alerted: Mapped[bool] = mapped_column(default=False)
    tier: Mapped[int] = mapped_column(Integer, default=3)

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_scanner_score", "composite_score"),
        Index("ix_scanner_tier", "tier"),
        Index("ix_scanner_discovered", "discovered_at"),
    )
