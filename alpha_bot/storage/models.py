from datetime import datetime

from sqlalchemy import Boolean, Float, Index, Integer, String, Text, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tweet(Base):
    __tablename__ = "tweets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tweet_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    author_id: Mapped[str] = mapped_column(String(64), nullable=False)
    author_username: Mapped[str] = mapped_column(String(128), nullable=False)
    author_name: Mapped[str] = mapped_column(String(256), default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    follower_count: Mapped[int] = mapped_column(Integer, default=0)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    retweet_count: Mapped[int] = mapped_column(Integer, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (Index("ix_tweets_score", "score"),)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tweet_id: Mapped[str] = mapped_column(String(64), nullable=False)
    author_username: Mapped[str] = mapped_column(String(128), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    tickers: Mapped[str] = mapped_column(Text, default="")  # comma-separated
    sentiment: Mapped[str] = mapped_column(String(16), default="neutral")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    signaled_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (Index("ix_signals_signaled_at", "signaled_at"),)


class ResearchReportRow(Base):
    __tablename__ = "research_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    buzz_json: Mapped[str] = mapped_column(Text, default="{}")
    smart_money_json: Mapped[str] = mapped_column(Text, default="{}")
    narratives_json: Mapped[str] = mapped_column(Text, default="[]")
    co_mentioned_json: Mapped[str] = mapped_column(Text, default="[]")
    risk_json: Mapped[str] = mapped_column(Text, default="{}")
    llm_summary: Mapped[str] = mapped_column(Text, default="")
    report_json: Mapped[str] = mapped_column(Text, default="{}")  # full to_dict() blob
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_research_ticker", "ticker"),
        Index("ix_research_created", "created_at"),
    )


class PnLAnalysisRow(Base):
    __tablename__ = "pnl_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_name: Mapped[str] = mapped_column(String(256), nullable=False)
    days_analyzed: Mapped[int] = mapped_column(Integer, nullable=False)
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    unique_tickers: Mapped[int] = mapped_column(Integer, default=0)
    resolved_tickers: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (Index("ix_pnl_created", "created_at"),)


class ForensicsRow(Base):
    __tablename__ = "forensics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    coin_id: Mapped[str] = mapped_column(String(128), default="")
    pump_magnitude: Mapped[float] = mapped_column(Float, default=0.0)
    signal_score: Mapped[float] = mapped_column(Float, default=0.0)
    pre_pump_tweets: Mapped[int] = mapped_column(Integer, default=0)
    verdict: Mapped[str] = mapped_column(Text, default="")
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (Index("ix_forensics_created", "created_at"),)


class TrackedSource(Base):
    __tablename__ = "tracked_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # "account" or "keyword"
    value: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_mint: Mapped[str] = mapped_column(String(64), nullable=False)
    token_symbol: Mapped[str] = mapped_column(String(32), default="")
    chain: Mapped[str] = mapped_column(String(16), default="solana")
    entry_price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    current_price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    entry_amount_sol: Mapped[float] = mapped_column(Float, default=0.0)
    token_balance: Mapped[float] = mapped_column(Float, default=0.0)
    initial_token_balance: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl_sol: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / closed
    tp1_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    tp2_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    tp3_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_loss_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    source_group: Mapped[str] = mapped_column(String(256), default="")
    source_message_id: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_positions_status", "status"),
        Index("ix_positions_token_mint", "token_mint"),
    )


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # buy / sell
    token_mint: Mapped[str] = mapped_column(String(64), nullable=False)
    token_symbol: Mapped[str] = mapped_column(String(32), default="")
    chain: Mapped[str] = mapped_column(String(16), default="solana")
    amount_in: Mapped[float] = mapped_column(Float, default=0.0)
    amount_out: Mapped[float] = mapped_column(Float, default=0.0)
    price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tx_hash: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending / confirmed / failed
    trigger: Mapped[str] = mapped_column(
        String(16), default=""
    )  # auto_buy / tp1 / tp2 / tp3 / stop_loss / manual
    error_msg: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_trades_position_id", "position_id"),
        Index("ix_trades_created", "created_at"),
    )
