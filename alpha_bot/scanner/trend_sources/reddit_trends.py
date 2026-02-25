"""Reddit — rising posts from crypto subreddits via public JSON API."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_SUBREDDITS = ["cryptocurrency", "solana", "CryptoMoonShots", "memecoins"]
_USER_AGENT = "alpha-bot/1.0 (trend scanner)"


async def fetch_reddit_trends() -> list[dict]:
    """Fetch rising posts from crypto subreddits."""
    results: list[dict] = []

    async with httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    ) as client:
        for sub in _SUBREDDITS:
            try:
                resp = await client.get(
                    f"https://www.reddit.com/r/{sub}/rising.json",
                    params={"limit": 25},
                )
                if resp.status_code == 429:
                    logger.debug("Reddit rate-limited for r/%s, skipping", sub)
                    continue
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, Exception) as exc:
                logger.warning("Reddit fetch failed for r/%s: %s", sub, exc)
                continue

            posts = data.get("data", {}).get("children", [])
            for post in posts:
                d = post.get("data", {})
                title = (d.get("title") or "").strip()
                ups = d.get("ups", 0) or 0
                if not title:
                    continue
                results.append({
                    "source": "reddit",
                    "theme": title.lower()[:256],
                    "volume": ups,
                    "velocity": float(ups),
                    "category": f"r/{sub}",
                })

    logger.info("Reddit: %d themes from %d subreddits", len(results), len(_SUBREDDITS))
    return results
