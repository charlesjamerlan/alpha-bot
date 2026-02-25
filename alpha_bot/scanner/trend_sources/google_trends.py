"""Google Trends — trending queries via pytrends and direct scraping fallback."""

from __future__ import annotations

import asyncio
import logging
from functools import partial

import httpx

logger = logging.getLogger(__name__)

# Seed terms to pull related rising queries for
_SEED_TERMS = [
    "crypto", "meme coin", "AI agent", "base chain", "solana",
]


def _fetch_trending_searches() -> list[dict]:
    """Blocking call — run in executor. Fetches US real-time trending searches."""
    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl="en-US", tz=360, timeout=(10, 25))

        # Try realtime_trending_searches first (newer pytrends versions)
        try:
            df = pytrends.realtime_trending_searches(pn="US")
            if df is not None and not df.empty:
                results = []
                # Column name varies by pytrends version
                title_col = None
                for col in ["title", "entityNames", 0]:
                    if col in df.columns:
                        title_col = col
                        break
                if title_col is None and len(df.columns) > 0:
                    title_col = df.columns[0]

                for _, row in df.head(30).iterrows():
                    theme = str(row[title_col]).strip() if title_col is not None else ""
                    if theme and theme != "nan":
                        results.append({
                            "source": "google",
                            "theme": theme.lower(),
                            "volume": 0,
                            "velocity": 0.0,
                            "category": "trending",
                        })
                if results:
                    return results
        except (AttributeError, Exception) as exc:
            logger.debug("realtime_trending_searches failed: %s", exc)

        # Fallback to trending_searches
        try:
            df = pytrends.trending_searches(pn="united_states")
            if df is not None and not df.empty:
                results = []
                for _, row in df.head(20).iterrows():
                    theme = str(row.iloc[0]).strip()
                    if theme and theme != "nan":
                        results.append({
                            "source": "google",
                            "theme": theme.lower(),
                            "volume": 0,
                            "velocity": 0.0,
                            "category": "trending",
                        })
                return results
        except Exception as exc:
            logger.debug("trending_searches failed: %s", exc)

        return []
    except Exception as exc:
        logger.warning("Google Trends init failed: %s", exc)
        return []


def _fetch_related_queries() -> list[dict]:
    """Blocking call — pull rising related queries for seed terms."""
    results = []
    try:
        from pytrends.request import TrendReq
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


async def _fetch_google_trends_scrape() -> list[dict]:
    """Fallback: scrape Google Trends daily trends page directly."""
    results = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                "https://trends.google.com/trending/rss",
                params={"geo": "US"},
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                },
            )
            if resp.status_code == 200:
                # Simple XML parsing for <title> tags
                import re
                titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", resp.text)
                for title in titles[1:21]:  # skip first (feed title)
                    theme = title.strip().lower()
                    if theme and len(theme) > 2:
                        results.append({
                            "source": "google",
                            "theme": theme,
                            "volume": 0,
                            "velocity": 0.0,
                            "category": "daily_trends",
                        })
    except Exception as exc:
        logger.debug("Google Trends RSS scrape failed: %s", exc)

    return results


async def fetch_google_trends() -> list[dict]:
    """Fetch trending themes from Google Trends (runs blocking calls in executor)."""
    loop = asyncio.get_running_loop()

    trending, related, scraped = await asyncio.gather(
        loop.run_in_executor(None, _fetch_trending_searches),
        loop.run_in_executor(None, _fetch_related_queries),
        _fetch_google_trends_scrape(),
    )

    all_themes = trending + related + scraped

    # Deduplicate by theme name
    seen = set()
    unique = []
    for t in all_themes:
        if t["theme"] not in seen:
            seen.add(t["theme"])
            unique.append(t)

    logger.info(
        "Google Trends: %d themes (%d trending, %d related, %d scraped)",
        len(unique), len(trending), len(related), len(scraped),
    )
    return unique
