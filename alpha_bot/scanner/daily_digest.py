"""Daily digest — scheduled summary of trends and top candidates."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, and_

from alpha_bot.config import settings
from alpha_bot.scanner.alerts import _notify, _fmt_mcap
from alpha_bot.scanner.models import ScannerCandidate, TrendingTheme
from alpha_bot.storage.database import async_session

logger = logging.getLogger(__name__)


async def _build_digest() -> str:
    """Build the daily digest message."""
    now = datetime.utcnow()
    cutoff_24h = now - timedelta(hours=24)

    async with async_session() as session:
        # Top trending themes by velocity
        themes_result = await session.execute(
            select(TrendingTheme)
            .order_by(TrendingTheme.velocity.desc())
            .limit(5)
        )
        top_themes = list(themes_result.scalars().all())

        # Top candidates from last 24h
        cands_result = await session.execute(
            select(ScannerCandidate)
            .where(ScannerCandidate.discovered_at >= cutoff_24h)
            .order_by(ScannerCandidate.composite_score.desc())
            .limit(10)
        )
        top_candidates = list(cands_result.scalars().all())

        # Counts
        total_result = await session.execute(
            select(ScannerCandidate)
            .where(ScannerCandidate.discovered_at >= cutoff_24h)
        )
        total_24h = len(list(total_result.scalars().all()))

    # Build message
    lines = ["\U0001f4ca <b>DAILY SCANNER DIGEST</b>\n"]

    # Trending themes
    if top_themes:
        lines.append("\U0001f525 <b>TRENDING (Top 5):</b>")
        for i, t in enumerate(top_themes, 1):
            vel = f"+{t.velocity:.0f}%" if t.velocity > 0 else f"{t.velocity:.0f}%"
            lines.append(
                f"{i}. \"{t.theme}\" ({t.source}) — {vel}"
            )
        lines.append("")

    # Top candidates
    if top_candidates:
        lines.append("\U0001f3c6 <b>TOP CANDIDATES (24h):</b>")
        for i, c in enumerate(top_candidates, 1):
            tier_emoji = {1: "\U0001f534", 2: "\U0001f7e1", 3: "\U0001f7e2"}.get(c.tier, "\u26ab")
            try:
                themes_list = json.loads(c.matched_themes) if c.matched_themes else []
            except (json.JSONDecodeError, TypeError):
                themes_list = []
            theme_str = f'"{themes_list[0]}"' if themes_list else "—"
            lines.append(
                f"{i}. {tier_emoji} <b>${c.ticker}</b> — {c.composite_score:.0f}/100 "
                f"— {theme_str} {c.narrative_depth} layers "
                f"— {_fmt_mcap(c.mcap)} mcap"
            )
        lines.append("")

    # Summary stats
    lines.append(
        f"\U0001f4e1 Total discovered (24h): {total_24h}"
    )

    # Profile info
    try:
        from alpha_bot.tg_intel.pattern_extract import extract_winning_profile
        profile = await extract_winning_profile()
        if profile:
            lines.append(
                f"\U0001f4cb Profile: {profile.get('confidence', '?')} confidence "
                f"({profile.get('sample_size', 0)} samples)"
            )
    except Exception:
        pass

    return "\n".join(lines)


async def _seconds_until_hour(hour_utc: int) -> float:
    """Calculate seconds until the next occurrence of the given UTC hour."""
    now = datetime.utcnow()
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_digest_loop() -> None:
    """Wait for configured hour, then send digest. Repeat daily."""
    logger.info("Daily digest scheduled at %02d:00 UTC", settings.digest_hour_utc)

    while True:
        wait_seconds = await _seconds_until_hour(settings.digest_hour_utc)
        logger.debug("Daily digest: sleeping %.0f seconds until %02d:00 UTC", wait_seconds, settings.digest_hour_utc)
        await asyncio.sleep(wait_seconds)

        try:
            digest = await _build_digest()
            await _notify(digest)
            logger.info("Daily digest sent")
        except Exception:
            logger.exception("Daily digest failed")

        # Sleep a bit to avoid double-trigger
        await asyncio.sleep(60)
