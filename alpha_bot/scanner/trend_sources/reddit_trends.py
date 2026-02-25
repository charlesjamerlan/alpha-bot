"""Reddit — rising posts from crypto subreddits.

Strategy:
1. Try Reddit OAuth API (if credentials configured) — most reliable
2. Fallback to RSS feeds (/.rss) — works from most IPs
3. Fallback to JSON API (old.reddit.com) — blocked on datacenter IPs
"""

from __future__ import annotations

import logging
import os
import re
import time

import httpx

logger = logging.getLogger(__name__)

_SUBREDDITS = ["cryptocurrency", "solana", "CryptoMoonShots", "memecoins"]

_USER_AGENT = "linux:alpha-bot-scanner:v1.0 (by /u/crypto_trend_scanner)"

# OAuth token cache
_oauth_token: str | None = None
_oauth_expires: float = 0


async def _get_oauth_token(client: httpx.AsyncClient) -> str | None:
    """Get Reddit OAuth bearer token using client credentials (script app)."""
    global _oauth_token, _oauth_expires

    if _oauth_token and time.time() < _oauth_expires:
        return _oauth_token

    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None

    try:
        resp = await client.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            headers={"User-Agent": _USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
        _oauth_token = data["access_token"]
        _oauth_expires = time.time() + data.get("expires_in", 3600) - 60
        logger.info("Reddit OAuth token acquired")
        return _oauth_token
    except Exception as exc:
        logger.debug("Reddit OAuth failed: %s", exc)
        return None


async def _fetch_via_oauth(client: httpx.AsyncClient, token: str) -> list[dict]:
    """Fetch via Reddit OAuth API (oauth.reddit.com)."""
    results: list[dict] = []
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": _USER_AGENT,
    }

    for sub in _SUBREDDITS:
        try:
            resp = await client.get(
                f"https://oauth.reddit.com/r/{sub}/hot",
                params={"limit": 25, "raw_json": 1},
                headers=headers,
            )
            if resp.status_code == 401:
                # Token expired, clear cache
                global _oauth_token
                _oauth_token = None
                return results
            if resp.status_code in (403, 429):
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug("Reddit OAuth fetch r/%s failed: %s", sub, exc)
            continue

        posts = data.get("data", {}).get("children", [])
        for post in posts:
            d = post.get("data", {})
            title = (d.get("title") or "").strip()
            ups = d.get("ups", 0) or 0
            num_comments = d.get("num_comments", 0) or 0
            if not title:
                continue
            engagement = ups + num_comments
            results.append({
                "source": "reddit",
                "theme": title.lower()[:256],
                "volume": engagement,
                "velocity": float(engagement),
                "category": f"r/{sub}",
            })

    return results


async def _fetch_via_rss(client: httpx.AsyncClient) -> list[dict]:
    """Fallback: fetch via Reddit RSS feeds (works from most IPs)."""
    results: list[dict] = []

    for sub in _SUBREDDITS:
        try:
            resp = await client.get(
                f"https://www.reddit.com/r/{sub}/hot.rss",
                headers={"User-Agent": _USER_AGENT},
            )
            if resp.status_code in (403, 429):
                logger.debug("Reddit RSS %d for r/%s", resp.status_code, sub)
                continue
            resp.raise_for_status()

            # Parse RSS XML — extract <title> tags
            titles = re.findall(
                r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                resp.text,
            )
            # Skip first title (feed title) and last if it's the subreddit name
            for title in titles[1:26]:
                theme = title.strip()
                if not theme or len(theme) < 5:
                    continue
                # RSS doesn't have engagement scores, use 0
                results.append({
                    "source": "reddit",
                    "theme": theme.lower()[:256],
                    "volume": 0,
                    "velocity": 0.0,
                    "category": f"r/{sub}",
                })
        except Exception as exc:
            logger.debug("Reddit RSS fetch r/%s failed: %s", sub, exc)
            continue

    return results


async def _fetch_via_json(client: httpx.AsyncClient) -> list[dict]:
    """Last resort: old.reddit.com JSON API (often blocked from datacenter IPs)."""
    results: list[dict] = []

    for sub in _SUBREDDITS:
        try:
            resp = await client.get(
                f"https://old.reddit.com/r/{sub}/hot.json",
                params={"limit": 25, "raw_json": 1},
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/json",
                },
            )
            if resp.status_code in (403, 429):
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue

        posts = data.get("data", {}).get("children", [])
        for post in posts:
            d = post.get("data", {})
            title = (d.get("title") or "").strip()
            ups = d.get("ups", 0) or 0
            num_comments = d.get("num_comments", 0) or 0
            if not title:
                continue
            engagement = ups + num_comments
            results.append({
                "source": "reddit",
                "theme": title.lower()[:256],
                "volume": engagement,
                "velocity": float(engagement),
                "category": f"r/{sub}",
            })

    return results


async def fetch_reddit_trends() -> list[dict]:
    """Fetch trending themes from Reddit crypto subreddits.

    Tries OAuth → RSS → JSON API in order of reliability.
    """
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        # Strategy 1: OAuth (if credentials are set)
        token = await _get_oauth_token(client)
        if token:
            results = await _fetch_via_oauth(client, token)
            if results:
                logger.info("Reddit (OAuth): %d themes from %d subreddits",
                            len(results), len(_SUBREDDITS))
                return results

        # Strategy 2: RSS feeds (no auth needed, less IP-blocked)
        results = await _fetch_via_rss(client)
        if results:
            logger.info("Reddit (RSS): %d themes from %d subreddits",
                        len(results), len(_SUBREDDITS))
            return results

        # Strategy 3: JSON API (last resort)
        results = await _fetch_via_json(client)
        logger.info("Reddit (JSON): %d themes from %d subreddits",
                     len(results), len(_SUBREDDITS))
        return results
