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

# Stopwords to strip when extracting theme keywords from Reddit titles
_REDDIT_STOPWORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "for", "of",
    "and", "or", "but", "not", "be", "are", "was", "were", "been", "has",
    "have", "had", "do", "does", "did", "will", "would", "could", "should",
    "can", "may", "might", "shall", "this", "that", "these", "those",
    "i", "you", "he", "she", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their", "what", "which", "who",
    "when", "where", "why", "how", "all", "each", "every", "both", "few",
    "more", "most", "other", "some", "such", "no", "nor", "too", "very",
    "just", "about", "up", "out", "if", "so", "than", "then", "here",
    "there", "with", "from", "into", "over", "after", "before", "between",
    "under", "again", "once", "any", "only", "own", "same", "now",
    "also", "get", "got", "really", "even", "much", "still", "already",
    "going", "need", "think", "know", "want", "like", "make", "take",
    "come", "give", "look", "find", "back", "way", "long", "new", "old",
    "big", "good", "bad", "best", "first", "last", "next", "right",
})


def _extract_keywords(title: str, max_words: int = 4) -> str | None:
    """Extract meaningful keywords from a Reddit post title.

    Strips stopwords, keeps up to max_words significant terms.
    Returns None if nothing meaningful remains.
    """
    # Remove special chars, keep alphanumeric + spaces + $ (for tickers)
    cleaned = re.sub(r"[^\w\s$]", " ", title.lower())
    words = cleaned.split()

    # Keep words that are non-stopwords and at least 3 chars
    keywords = [w for w in words if w not in _REDDIT_STOPWORDS and len(w) >= 3]

    if not keywords:
        return None

    # Take the first max_words significant terms
    phrase = " ".join(keywords[:max_words])
    return phrase if len(phrase) >= 3 else None

_SUBREDDITS = [
    # Crypto
    "cryptocurrency", "solana", "CryptoMoonShots", "memecoins",
    # Culture / memes / trending (tokens often mirror cultural events)
    "wallstreetbets", "politics", "technology", "memes",
    "OutOfTheLoop",  # "what's going on with X?" = early trend signal
]

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
            theme = _extract_keywords(title)
            if not theme:
                continue
            engagement = ups + num_comments
            results.append({
                "source": "reddit",
                "theme": theme,
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
                theme = _extract_keywords(title.strip())
                if not theme:
                    continue
                results.append({
                    "source": "reddit",
                    "theme": theme,
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
            theme = _extract_keywords(title)
            if not theme:
                continue
            engagement = ups + num_comments
            results.append({
                "source": "reddit",
                "theme": theme,
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
