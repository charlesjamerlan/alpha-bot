"""ORM models for backtesting and scoring weight management."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from alpha_bot.storage.models import Base


class BacktestRun(Base):
    """Stores each backtest simulation result."""

    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    tier1_count: Mapped[int] = mapped_column(Integer, default=0)
    tier2_count: Mapped[int] = mapped_column(Integer, default=0)
    tier3_count: Mapped[int] = mapped_column(Integer, default=0)
    tier1_hit_rate_2x: Mapped[float] = mapped_column(Float, default=0.0)
    tier2_hit_rate_2x: Mapped[float] = mapped_column(Float, default=0.0)
    tier1_avg_roi: Mapped[float] = mapped_column(Float, default=0.0)
    tier2_avg_roi: Mapped[float] = mapped_column(Float, default=0.0)
    optimal_tier1_threshold: Mapped[float] = mapped_column(Float, default=80.0)
    optimal_tier2_threshold: Mapped[float] = mapped_column(Float, default=60.0)
    weights_json: Mapped[str] = mapped_column(Text, default="{}")
    results_json: Mapped[str] = mapped_column(Text, default="[]")

    __table_args__ = (Index("ix_backtest_runs_ts", "run_timestamp"),)


class ScoringWeights(Base):
    """Versioned weight snapshots for composite scoring."""

    __tablename__ = "scoring_weights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    w_narrative: Mapped[float] = mapped_column(Float, default=0.25)
    w_profile: Mapped[float] = mapped_column(Float, default=0.20)
    w_platform: Mapped[float] = mapped_column(Float, default=0.15)
    w_market: Mapped[float] = mapped_column(Float, default=0.15)
    w_depth: Mapped[float] = mapped_column(Float, default=0.15)
    w_source: Mapped[float] = mapped_column(Float, default=0.10)
    source: Mapped[str] = mapped_column(String(32), default="initial")
    backtest_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (Index("ix_scoring_weights_active", "active"),)
