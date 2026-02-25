"""ORM models for private wallet curation, transactions, and clustering."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from alpha_bot.storage.models import Base


class PrivateWallet(Base):
    """A curated wallet discovered by reverse-engineering winning tokens."""

    __tablename__ = "private_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(256), default="")
    source: Mapped[str] = mapped_column(String(32), default="reverse_engineer")
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_copiers: Mapped[int] = mapped_column(Integer, default=0)
    decay_score: Mapped[float] = mapped_column(Float, default=0.0)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_wins: Mapped[int] = mapped_column(Integer, default=0)
    total_tracked: Mapped[int] = mapped_column(Integer, default=0)
    avg_entry_roi: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="active")
    first_seen: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_private_wallets_quality", "quality_score"),
        Index("ix_private_wallets_status", "status"),
    )


class WalletTransaction(Base):
    """A token transfer involving a tracked wallet."""

    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ca: Mapped[str] = mapped_column(String(64), nullable=False)
    chain: Mapped[str] = mapped_column(String(16), default="base")
    tx_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    block_number: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    token_symbol: Mapped[str] = mapped_column(String(32), default="")
    peak_roi: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_wallet_tx_ca", "ca"),
        Index("ix_wallet_tx_wallet_ca", "wallet_address", "ca"),
    )


class WalletCluster(Base):
    """A group of wallets that frequently co-buy the same tokens."""

    __tablename__ = "wallet_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_label: Mapped[str] = mapped_column(String(64), default="")
    wallet_count: Mapped[int] = mapped_column(Integer, default=0)
    wallets_json: Mapped[str] = mapped_column(Text, default="[]")
    avg_quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    independence_score: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
