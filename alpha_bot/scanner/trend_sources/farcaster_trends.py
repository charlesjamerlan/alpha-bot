"""Farcaster — trending casts via Neynar free API."""

from __future__ import annotations

import logging
import re

import httpx

from alpha_bot.config import settings

logger = logging.getLogger(__name__)

_NEYNAR_BASE = "https://api.neynar.com/v2/farcaster"

# Keywords to filter for crypto-relevant casts
_CRYPTO_KEYWORDS = re.compile(
    r"(crypto|token|defi|nft|memecoin|degen|base chain|solana|clanker|virtuals|"
    r"flaunch|airdrop|mint|launch|pump|agent|ai\b)",
    re.IGNORECASE,
)


async def fetch_farcaster_trends() -> list[dict]:
    """Fetch trending casts from Farcaster via Neynar API."""
    if not settings.neynar_api_key:
        logger.debug("Farcaster skipped — no NEYNAR_API_KEY")
        return []

    results: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_NEYNAR_BASE}/feed/trending",
                headers={"api_key": settings.neynar_api_key},
                params={"limit": 50},
            )
            if resp.status_code == 429:
                logger.debug("Neynar rate-limited, skipping")
                return []
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, Exception) as exc:
        logger.warning("Farcaster fetch failed: %s", exc)
        return []

    casts = data.get("casts", [])
    for cast in casts:
        text = (cast.get("text") or "").strip()
        if not text or not _CRYPTO_KEYWORDS.search(text):
            continue

        reactions = cast.get("reactions", {})
        likes = reactions.get("likes_count", 0) or 0
        recasts = reactions.get("recasts_count", 0) or 0
        volume = likes + recasts

        # Use first 128 chars as theme
        theme = text[:128].lower().strip()
        results.append({
            "source": "farcaster",
            "theme": theme,
            "volume": volume,
            "velocity": float(volume),
            "category": "trending_cast",
        })

    logger.info("Farcaster: %d crypto-relevant trending casts", len(results))
    return results
