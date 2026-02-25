#!/usr/bin/env python3
"""Backfill call_outcomes and channel_scores from TG group history.

Usage:
    python backfill_channel_scores.py <group> [--days 90] [--topic TOPIC_ID]

Examples:
    python backfill_channel_scores.py blessedmemecalls --days 30
    python backfill_channel_scores.py -1002469811342 --days 60 --topic 1
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select, and_

from alpha_bot.storage.database import async_session, init_db
from alpha_bot.research.telegram_group import scrape_group_history
from alpha_bot.research.dexscreener import get_token_by_address, extract_pair_details
from alpha_bot.tg_intel.models import CallOutcome, ChannelScore  # noqa: F401 — register models
from alpha_bot.tg_intel.price_resolver import resolve_call_prices
from alpha_bot.tg_intel.platform_detect import detect_platform
from alpha_bot.tg_intel.scorer import compute_channel_scores, save_channel_scores
from alpha_bot.utils.logging import setup_logging

logger = logging.getLogger(__name__)

# GeckoTerminal free tier: ~30 req/min. We make 2-3 requests per token,
# so 5s between tokens keeps us under the limit.
RATE_LIMIT_DELAY = 5.0


async def _find_existing(
    session, channel_id: str, ca: str, ts: datetime
) -> CallOutcome | None:
    """Find existing call outcome within 4h dedup window, or None."""
    cutoff = ts - timedelta(hours=4)
    stmt = select(CallOutcome).where(
        and_(
            CallOutcome.channel_id == channel_id,
            CallOutcome.ca == ca,
            CallOutcome.mention_timestamp >= cutoff,
        )
    ).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def backfill(group: str | int, days: int = 90, topic_id: int | None = None) -> None:
    setup_logging()
    await init_db()

    logger.info("Scraping %s (last %d days)...", group, days)
    calls = await scrape_group_history(group, days_back=days, topic_id=topic_id)

    if not calls:
        logger.warning("No calls found in %s", group)
        return

    # Filter to CA-bearing calls only
    ca_calls = [c for c in calls if c.get("contract_address")]
    logger.info("Found %d total calls, %d with contract addresses", len(calls), len(ca_calls))

    if not ca_calls:
        logger.warning("No contract addresses found — nothing to backfill")
        return

    # Deduplicate: keep first mention of each CA per channel (within 4h)
    seen: dict[str, datetime] = {}
    unique_calls = []
    for c in ca_calls:
        ca = c["contract_address"]
        ts = c["posted_at"]
        key = f"{group}:{ca}"
        if key in seen:
            prev_ts = seen[key]
            if abs((ts - prev_ts).total_seconds()) < 4 * 3600:
                continue
        seen[key] = ts
        unique_calls.append(c)

    logger.info("After dedup: %d unique CA calls to process", len(unique_calls))

    processed = 0
    retried = 0
    skipped = 0
    errors = 0

    async with httpx.AsyncClient(timeout=30) as client:
        for i, call in enumerate(unique_calls, 1):
            ca = call["contract_address"]
            chain = call.get("chain", "solana")
            ticker = call.get("ticker", "")
            mention_ts = call["posted_at"]
            channel_id = str(group)

            # Check if already in DB
            async with async_session() as session:
                existing = await _find_existing(session, channel_id, ca, mention_ts)

            if existing and existing.price_check_status == "complete":
                skipped += 1
                continue

            try:
                # Resolve historical prices
                prices = await resolve_call_prices(ca, chain, mention_ts, client)

                # Get pair data for platform detection + ticker resolution
                pair_data = await get_token_by_address(ca, client)
                platform = detect_platform(ca, pair_data, call.get("message_text", ""))

                # Resolve ticker if stub
                if (not ticker or "..." in ticker) and pair_data:
                    details = extract_pair_details(pair_data)
                    ticker = details.get("symbol", ticker)

                # Determine status
                status = "complete" if prices["price_at_mention"] else "pending"
                if prices["price_at_mention"] and not prices["price_24h"]:
                    status = "partial"

                if existing:
                    # Update existing row that had missing prices
                    async with async_session() as session:
                        result = await session.execute(
                            select(CallOutcome).where(CallOutcome.id == existing.id)
                        )
                        row = result.scalar_one()
                        row.price_at_mention = prices["price_at_mention"]
                        row.price_1h = prices["price_1h"]
                        row.price_6h = prices["price_6h"]
                        row.price_24h = prices["price_24h"]
                        row.price_peak = prices["price_peak"]
                        row.peak_timestamp = prices["peak_timestamp"]
                        row.mcap_at_mention = prices["mcap_at_mention"]
                        row.platform = platform
                        row.roi_1h = prices["roi_1h"]
                        row.roi_6h = prices["roi_6h"]
                        row.roi_24h = prices["roi_24h"]
                        row.roi_peak = prices["roi_peak"]
                        row.hit_2x = prices["hit_2x"]
                        row.hit_5x = prices["hit_5x"]
                        row.price_check_status = status
                        if ticker and (not row.ticker or "..." in row.ticker):
                            row.ticker = ticker
                        await session.commit()
                    retried += 1
                else:
                    # Insert new row
                    outcome = CallOutcome(
                        channel_id=channel_id,
                        channel_name=str(group),
                        message_id=call.get("message_id", 0),
                        message_text=call.get("message_text", "")[:500],
                        author=call.get("author", ""),
                        ca=ca,
                        chain=chain,
                        ticker=ticker,
                        mention_timestamp=mention_ts,
                        price_at_mention=prices["price_at_mention"],
                        price_1h=prices["price_1h"],
                        price_6h=prices["price_6h"],
                        price_24h=prices["price_24h"],
                        price_peak=prices["price_peak"],
                        peak_timestamp=prices["peak_timestamp"],
                        mcap_at_mention=prices["mcap_at_mention"],
                        platform=platform,
                        roi_1h=prices["roi_1h"],
                        roi_6h=prices["roi_6h"],
                        roi_24h=prices["roi_24h"],
                        roi_peak=prices["roi_peak"],
                        hit_2x=prices["hit_2x"],
                        hit_5x=prices["hit_5x"],
                        price_check_status=status,
                    )
                    async with async_session() as session:
                        session.add(outcome)
                        await session.commit()
                    processed += 1

                roi_str = f"ROI peak: {prices['roi_peak']:+.1f}%" if prices["roi_peak"] is not None else "no price data"
                hit_str = ""
                if prices["hit_2x"]:
                    hit_str = " [2x]"
                if prices["hit_5x"]:
                    hit_str = " [5x]"
                action = "RETRY" if existing else "NEW"

                logger.info(
                    "[%d/%d] %s %s (%s) — %s%s | platform: %s",
                    i, len(unique_calls), action, ticker or ca[:12], chain,
                    roi_str, hit_str, platform,
                )

            except Exception:
                logger.exception("Failed to process %s", ca[:12])
                errors += 1

            # Rate limit
            await asyncio.sleep(RATE_LIMIT_DELAY)

    # Compute and save channel scores
    logger.info("Computing channel scores...")
    async with async_session() as session:
        scores = await compute_channel_scores(session)
        await save_channel_scores(session, scores)

    # Print summary
    print("\n" + "=" * 60)
    print(f"BACKFILL COMPLETE: {group}")
    print(f"=" * 60)
    print(f"  New: {processed}")
    print(f"  Retried (filled missing prices): {retried}")
    print(f"  Skipped (already complete): {skipped}")
    print(f"  Errors: {errors}")
    print()

    if scores:
        for s in scores:
            print(f"  Channel: {s.channel_name}")
            print(f"    Quality Score: {s.quality_score}/100")
            print(f"    Total Calls: {s.total_calls} ({s.resolved_calls} resolved)")
            print(f"    Hit Rate 2x: {s.hit_rate_2x:.1%}")
            print(f"    Hit Rate 5x: {s.hit_rate_5x:.1%}")
            print(f"    Avg ROI (peak): {s.avg_roi_peak:+.1f}%")
            print(f"    Best Platform: {s.best_platform}")
            print(f"    Best MCap Range: {s.best_mcap_range}")
            print()


def main():
    parser = argparse.ArgumentParser(description="Backfill channel scores from TG history")
    parser.add_argument("group", help="TG group username or numeric ID")
    parser.add_argument("--days", type=int, default=90, help="Days to look back (default: 90)")
    parser.add_argument("--topic", type=int, default=None, help="Forum topic ID to filter")
    args = parser.parse_args()

    # Parse group: try numeric first
    try:
        group = int(args.group)
    except ValueError:
        group = args.group

    asyncio.run(backfill(group, days=args.days, topic_id=args.topic))


if __name__ == "__main__":
    main()
