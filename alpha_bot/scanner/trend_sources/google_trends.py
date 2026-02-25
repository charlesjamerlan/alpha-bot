"""Google Trends — rising queries in tech/culture/memes via pytrends."""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from pytrends.request import TrendReq

logger = logging.getLogger(__name__)

# Seed terms to pull related rising queries for
_SEED_TERMS = [
    "crypto", "meme coin", "AI agent", "base chain", "solana",
]


def _fetch_trending_searches() -> list[dict]:
    """Blocking call — run in executor. Fetches US real-time trending searches."""
    try:
        pytrends = TrendReq(hl="en-US", tz=360, timeout=(10, 25))
        df = pytrends.trending_searches(pn="united_states")
        if df is None or df.empty:
            return []

        results = []
        for _, row in df.iterrows():
            theme = str(row.iloc[0]).strip()
            if theme:
                results.append({
                    "source": "google",
                    "theme": theme.lower(),
                    "volume": 0,
                    "velocity": 0.0,
                    "category": "trending",
                })
        return results
    except Exception as exc:
        logger.warning("Google trending_searches failed: %s", exc)
        return []


def _fetch_related_queries() -> list[dict]:
    """Blocking call — pull rising related queries for seed terms."""
    results = []
    try:
        pytrends = TrendReq(hl="en-US", tz=360, timeout=(10, 25))
        pytrends.build_payload(_SEED_TERMS[:5], timeframe="now 7-d")
        related = pytrends.related_queries()
    except Exception as exc:
        logger.warning("Google related_queries failed: %s", exc)
        return []

    for term, data in related.items():
        rising = data.get("rising")
        if rising is None or rising.empty:
            continue
        for _, row in rising.iterrows():
            query = str(row.get("query", "")).strip()
            value = int(row.get("value", 0)) if row.get("value") is not None else 0
            if query:
                results.append({
                    "source": "google",
                    "theme": query.lower(),
                    "volume": value,
                    "velocity": float(value),
                    "category": "related_rising",
                })

    return results


async def fetch_google_trends() -> list[dict]:
    """Fetch trending themes from Google Trends (runs blocking calls in executor)."""
    loop = asyncio.get_running_loop()

    trending, related = await asyncio.gather(
        loop.run_in_executor(None, _fetch_trending_searches),
        loop.run_in_executor(None, _fetch_related_queries),
    )

    all_themes = trending + related
    logger.info("Google Trends: %d themes (%d trending, %d related)", len(all_themes), len(trending), len(related))
    return all_themes
