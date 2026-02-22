from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alpha_bot.storage.models import (
    ForensicsRow,
    PnLAnalysisRow,
    Position,
    ResearchReportRow,
    Signal,
    Trade,
    Tweet,
)


async def save_tweet(session: AsyncSession, tweet: Tweet) -> Tweet:
    session.add(tweet)
    await session.commit()
    await session.refresh(tweet)
    return tweet


async def save_signal(session: AsyncSession, signal: Signal) -> Signal:
    session.add(signal)
    await session.commit()
    await session.refresh(signal)
    return signal


async def tweet_exists(session: AsyncSession, tweet_id: str) -> bool:
    stmt = select(Tweet.id).where(Tweet.tweet_id == tweet_id).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def get_recent_signals(
    session: AsyncSession, limit: int = 50
) -> list[Signal]:
    stmt = (
        select(Signal)
        .order_by(Signal.signaled_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_signals_since(
    session: AsyncSession, since: datetime
) -> list[Signal]:
    stmt = (
        select(Signal)
        .where(Signal.signaled_at >= since)
        .order_by(Signal.signaled_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def save_research_report(
    session: AsyncSession, row: ResearchReportRow
) -> ResearchReportRow:
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_recent_reports(
    session: AsyncSession, limit: int = 20
) -> list[ResearchReportRow]:
    stmt = (
        select(ResearchReportRow)
        .order_by(ResearchReportRow.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_reports_for_ticker(
    session: AsyncSession, ticker: str, limit: int = 10
) -> list[ResearchReportRow]:
    stmt = (
        select(ResearchReportRow)
        .where(ResearchReportRow.ticker == ticker.upper())
        .order_by(ResearchReportRow.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def save_pnl_analysis(
    session: AsyncSession, row: PnLAnalysisRow
) -> PnLAnalysisRow:
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_recent_pnl_analyses(
    session: AsyncSession, limit: int = 10
) -> list[PnLAnalysisRow]:
    stmt = (
        select(PnLAnalysisRow)
        .order_by(PnLAnalysisRow.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_pnl_analysis(
    session: AsyncSession, analysis_id: int
) -> PnLAnalysisRow | None:
    stmt = select(PnLAnalysisRow).where(PnLAnalysisRow.id == analysis_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def save_forensics(
    session: AsyncSession, row: ForensicsRow
) -> ForensicsRow:
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_recent_forensics(
    session: AsyncSession, limit: int = 20
) -> list[ForensicsRow]:
    stmt = (
        select(ForensicsRow)
        .order_by(ForensicsRow.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# --- Position & Trade repository ---


async def save_position(session: AsyncSession, position: Position) -> Position:
    session.add(position)
    await session.commit()
    await session.refresh(position)
    return position


async def save_trade(session: AsyncSession, trade: Trade) -> Trade:
    session.add(trade)
    await session.commit()
    await session.refresh(trade)
    return trade


async def get_open_positions(session: AsyncSession) -> list[Position]:
    stmt = (
        select(Position)
        .where(Position.status == "open")
        .order_by(Position.opened_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_position_by_mint(
    session: AsyncSession, token_mint: str
) -> Position | None:
    stmt = (
        select(Position)
        .where(Position.token_mint == token_mint, Position.status == "open")
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def position_exists_for_mint(session: AsyncSession, token_mint: str) -> bool:
    stmt = (
        select(Position.id)
        .where(Position.token_mint == token_mint, Position.status == "open")
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def update_position(session: AsyncSession, position: Position) -> Position:
    session.add(position)
    await session.commit()
    await session.refresh(position)
    return position


async def get_recent_trades(
    session: AsyncSession, limit: int = 50
) -> list[Trade]:
    stmt = (
        select(Trade)
        .order_by(Trade.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_closed_positions(
    session: AsyncSession, limit: int = 50
) -> list[Position]:
    stmt = (
        select(Position)
        .where(Position.status == "closed")
        .order_by(Position.closed_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
