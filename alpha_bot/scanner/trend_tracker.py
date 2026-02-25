"""Trend tracker orchestrator — polls all sources and upserts trending_themes."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from alpha_bot.config import settings
from alpha_bot.scanner.models import TrendingTheme
from alpha_bot.scanner.trend_sources.farcaster_trends import fetch_farcaster_trends
from alpha_bot.scanner.trend_sources.github_trending import fetch_github_trending
from alpha_bot.scanner.trend_sources.google_trends import fetch_google_trends
from alpha_bot.scanner.trend_sources.reddit_trends import fetch_reddit_trends
from alpha_bot.storage.database import async_session

logger = logging.getLogger(__name__)


async def _upsert_themes(themes: list[dict]) -> int:
    """Upsert themes into DB. Returns count of upserted rows."""
    if not themes:
        return 0

    count = 0
    async with async_session() as session:
        for t in themes:
            source = t.get("source", "")
            theme = t.get("theme", "")[:256]
            if not source or not theme:
                continue

            result = await session.execute(
                select(TrendingTheme).where(
                    TrendingTheme.source == source,
                    TrendingTheme.theme == theme,
                )
            )
            existing = result.scalar_one_or_none()

            now = datetime.utcnow()
            new_volume = t.get("volume", 0)

            if existing:
                old_vol = existing.current_volume or 0
                existing.previous_volume = old_vol
                existing.current_volume = new_volume
                # Velocity = pct change (avoid div by zero)
                if old_vol > 0:
                    existing.velocity = ((new_volume - old_vol) / old_vol) * 100
                else:
                    existing.velocity = float(new_volume)
                existing.category = t.get("category", existing.category)
                existing.last_updated = now
            else:
                row = TrendingTheme(
                    source=source,
                    theme=theme,
                    velocity=t.get("velocity", 0.0),
                    current_volume=new_volume,
                    previous_volume=0,
                    category=t.get("category", ""),
                    first_seen=now,
                    last_updated=now,
                )
                session.add(row)

            count += 1

        await session.commit()

    return count


async def poll_all_sources() -> list[dict]:
    """Poll all trend sources and return combined list."""
    results = await asyncio.gather(
        fetch_google_trends(),
        fetch_reddit_trends(),
        fetch_farcaster_trends(),
        fetch_github_trending(),
        return_exceptions=True,
    )

    themes: list[dict] = []
    source_names = ["Google", "Reddit", "Farcaster", "GitHub"]
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.warning("%s trend source failed: %s", source_names[i], result)
        elif isinstance(result, list):
            themes.extend(result)

    return themes


async def trend_tracker_loop() -> None:
    """Main loop: poll all sources, upsert to DB, repeat."""
    logger.info("Trend tracker started (interval=%ds)", settings.trend_poll_interval_seconds)
    while True:
        try:
            themes = await poll_all_sources()
            upserted = await _upsert_themes(themes)
            logger.info("Trend tracker: loaded %d themes, upserted %d", len(themes), upserted)
        except Exception:
            logger.exception("Trend tracker loop error")

        await asyncio.sleep(settings.trend_poll_interval_seconds)
