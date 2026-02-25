"""Real-time call recorder with delayed price checks."""

import asyncio
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from alpha_bot.research.dexscreener import (
    extract_pair_details,
    get_token_by_address,
)
from alpha_bot.storage.database import async_session
from alpha_bot.tg_intel.models import CallOutcome
from alpha_bot.tg_intel.platform_detect import detect_platform

logger = logging.getLogger(__name__)

# Dedup window: ignore same channel+CA within this many hours
_DEDUP_HOURS = 4

# Telethon client ref for delayed reaction re-checks
_telethon_client = None


def set_telethon_client(client) -> None:
    """Store Telethon client ref for delayed message re-fetches."""
    global _telethon_client
    _telethon_client = client


async def _is_duplicate(
    session: AsyncSession, channel_id: str, ca: str, mention_ts: datetime
) -> bool:
    """Check if the same CA was already recorded from this channel recently."""
    cutoff = mention_ts - timedelta(hours=_DEDUP_HOURS)
    stmt = select(CallOutcome).where(
        and_(
            CallOutcome.channel_id == channel_id,
            CallOutcome.ca == ca,
            CallOutcome.mention_timestamp >= cutoff,
        )
    ).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _fetch_current_price(ca: str) -> tuple[float | None, dict | None]:
    """Fetch current price and pair data from DexScreener."""
    async with httpx.AsyncClient(timeout=15) as client:
        pair = await get_token_by_address(ca, client)
        if not pair:
            return None, None
        details = extract_pair_details(pair)
        return details.get("price_usd"), pair


async def _delayed_price_check(
    outcome_id: int, ca: str, price_at_mention: float, delay_seconds: int,
    price_field: str, roi_field: str,
) -> None:
    """Wait *delay_seconds*, then fetch price and update the call outcome."""
    await asyncio.sleep(delay_seconds)

    try:
        price, _ = await _fetch_current_price(ca)
        if price is None:
            logger.debug("Delayed price check: no price for %s at +%ds", ca[:12], delay_seconds)
            return

        roi = None
        if price_at_mention and price_at_mention > 0:
            roi = ((price - price_at_mention) / price_at_mention) * 100.0

        async with async_session() as session:
            result = await session.execute(
                select(CallOutcome).where(CallOutcome.id == outcome_id)
            )
            outcome = result.scalar_one_or_none()
            if not outcome:
                return

            setattr(outcome, price_field, price)
            setattr(outcome, roi_field, roi)

            # Update peak if this price is higher
            if outcome.price_peak is None or (price and price > outcome.price_peak):
                outcome.price_peak = price
                outcome.peak_timestamp = datetime.utcnow()

            # Update hit flags
            if outcome.price_at_mention and outcome.price_at_mention > 0 and outcome.price_peak:
                peak_roi = ((outcome.price_peak - outcome.price_at_mention) / outcome.price_at_mention) * 100.0
                outcome.roi_peak = peak_roi
                outcome.hit_2x = peak_roi >= 100.0
                outcome.hit_5x = peak_roi >= 400.0

            # Mark complete after 24h check
            if price_field == "price_24h":
                outcome.price_check_status = "complete"
            elif outcome.price_check_status == "pending":
                outcome.price_check_status = "partial"

            await session.commit()
            logger.debug(
                "Updated %s for outcome #%d: price=%.10f roi=%s",
                price_field, outcome_id, price,
                f"{roi:+.1f}%" if roi is not None else "N/A",
            )
    except Exception:
        logger.exception("Delayed price check failed for outcome #%d (%s)", outcome_id, price_field)


async def _delayed_reaction_check(
    outcome_id: int, channel_id: str, message_id: int, delay: int = 1800,
) -> None:
    """Wait *delay* seconds, re-fetch message reactions, update outcome + velocity."""
    await asyncio.sleep(delay)

    if not _telethon_client:
        return

    try:
        # Resolve channel entity
        try:
            entity = int(channel_id)
        except ValueError:
            entity = channel_id

        msgs = await _telethon_client.get_messages(entity, ids=message_id)
        msg = msgs if not isinstance(msgs, list) else (msgs[0] if msgs else None)
        if not msg:
            logger.debug("Delayed reaction check: message %d not found in %s", message_id, channel_id)
            return

        reaction_count = 0
        if msg.reactions:
            for r in msg.reactions.results:
                reaction_count += r.count
        forward_count = getattr(msg, 'forwards', None) or 0

        async with async_session() as session:
            result = await session.execute(
                select(CallOutcome).where(CallOutcome.id == outcome_id)
            )
            outcome = result.scalar_one_or_none()
            if not outcome:
                return

            outcome.reaction_count = reaction_count
            outcome.forward_count = forward_count
            await session.commit()

        # Feed into reaction velocity tracker
        try:
            from alpha_bot.tg_intel.reaction_velocity import track_reaction
            await track_reaction(
                channel_id=channel_id,
                channel_name="",
                ca="",
                ticker="",
                reaction_count=reaction_count,
                outcome_id=outcome_id,
            )
        except Exception as exc:
            logger.warning("Reaction velocity tracking failed: %s", exc)

        logger.debug(
            "Delayed reaction check: outcome #%d updated — reactions=%d, forwards=%d",
            outcome_id, reaction_count, forward_count,
        )
    except Exception:
        logger.exception("Delayed reaction check failed for outcome #%d", outcome_id)


async def record_call(
    ca: str,
    chain: str,
    ticker: str,
    channel_id: str,
    channel_name: str = "",
    message_id: int = 0,
    message_text: str = "",
    author: str = "",
    mention_timestamp: datetime | None = None,
    reaction_count: int = 0,
    forward_count: int = 0,
    views: int = 0,
) -> None:
    """Record a new CA call and schedule delayed price checks.

    Called by the trading listener for each new CA detection.
    """
    if mention_timestamp is None:
        mention_timestamp = datetime.utcnow()

    async with async_session() as session:
        # Dedup check
        if await _is_duplicate(session, channel_id, ca, mention_timestamp):
            logger.debug("Duplicate call skipped: %s in %s", ca[:12], channel_name)
            return

        # Fetch current price + pair data
        price_at_mention, pair_data = await _fetch_current_price(ca)

        # Detect platform
        platform = detect_platform(ca, pair_data, message_text)

        # Estimate mcap at mention
        mcap = None
        if pair_data:
            details = extract_pair_details(pair_data)
            mcap = details.get("market_cap")

        # Resolve ticker from DexScreener if we only have a stub
        if (not ticker or "..." in ticker) and pair_data:
            details = extract_pair_details(pair_data)
            ticker = details.get("symbol", ticker)

        outcome = CallOutcome(
            channel_id=channel_id,
            channel_name=channel_name,
            message_id=message_id,
            message_text=message_text[:500],
            author=author,
            ca=ca,
            chain=chain,
            ticker=ticker,
            mention_timestamp=mention_timestamp,
            price_at_mention=price_at_mention,
            mcap_at_mention=mcap,
            platform=platform,
            reaction_count=reaction_count or None,
            forward_count=forward_count or None,
            views=views or None,
            price_check_status="pending",
        )
        session.add(outcome)
        await session.commit()
        await session.refresh(outcome)

        logger.info(
            "Recorded call: %s (%s) from %s — price=$%.10g, mcap=%s, platform=%s",
            ticker or ca[:12], chain, channel_name,
            price_at_mention or 0,
            f"${mcap:,.0f}" if mcap else "N/A",
            platform,
        )

    # Convergence check (outside session context)
    try:
        from alpha_bot.tg_intel.convergence import check_convergence
        await check_convergence(
            ca=ca, channel_id=channel_id, channel_name=channel_name,
            ticker=ticker, chain=chain, mention_timestamp=mention_timestamp,
        )
    except Exception as exc:
        logger.warning("Convergence check failed: %s", exc)

    # Schedule delayed reaction re-check at +30min (fire-and-forget)
    if _telethon_client and message_id:
        asyncio.create_task(
            _delayed_reaction_check(
                outcome.id, channel_id, message_id, delay=1800,
            )
        )

    # Schedule delayed price checks (fire-and-forget)
    if price_at_mention and price_at_mention > 0:
        for delay, p_field, r_field in [
            (3600, "price_1h", "roi_1h"),
            (21600, "price_6h", "roi_6h"),
            (86400, "price_24h", "roi_24h"),
        ]:
            asyncio.create_task(
                _delayed_price_check(
                    outcome.id, ca, price_at_mention, delay, p_field, r_field
                )
            )
    else:
        logger.debug("No entry price for %s — skipping delayed checks", ca[:12])
