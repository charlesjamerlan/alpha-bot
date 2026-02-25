"""Virtuals Protocol agent token enrichment (Phase 2.2).

Scores Virtuals tokens on two axes:
1. Correlation with $VIRTUAL — high correlation means the token is a proxy,
   not independently valued (bad).
2. Agent activity — presence of social links signals a maintained project (good).
"""

from __future__ import annotations

import logging
import time

import httpx

from alpha_bot.research.dexscreener import extract_pair_details, get_token_by_address

logger = logging.getLogger(__name__)

# $VIRTUAL contract address on Base
VIRTUAL_CA = "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b"

# Cache $VIRTUAL's 24h price change (refresh every 5 min)
_virtual_change_cache: float | None = None
_virtual_cache_ts: float = 0.0
_VIRTUAL_CACHE_TTL = 300  # 5 minutes


async def _get_virtual_24h_change(client: httpx.AsyncClient) -> float | None:
    """Fetch $VIRTUAL's 24h price change (%), with 5-min cache."""
    global _virtual_change_cache, _virtual_cache_ts

    now = time.time()
    if _virtual_change_cache is not None and (now - _virtual_cache_ts) < _VIRTUAL_CACHE_TTL:
        return _virtual_change_cache

    pair = await get_token_by_address(VIRTUAL_CA, client)
    if not pair:
        return _virtual_change_cache  # stale cache better than nothing

    d = extract_pair_details(pair)
    change = d.get("price_change_24h")
    if change is not None:
        _virtual_change_cache = float(change)
        _virtual_cache_ts = now

    return _virtual_change_cache


async def compute_virtual_correlation(
    token_ca: str, client: httpx.AsyncClient
) -> float:
    """Compute price-direction correlation between a token and $VIRTUAL.

    Compares 24h price change direction + magnitude.
    Returns 0.0 (no correlation) to 1.0 (moves exactly like $VIRTUAL).
    """
    # Get token's 24h change
    pair = await get_token_by_address(token_ca, client)
    if not pair:
        return 0.0

    d = extract_pair_details(pair)
    token_change = d.get("price_change_24h")
    if token_change is None:
        return 0.0

    virtual_change = await _get_virtual_24h_change(client)
    if virtual_change is None:
        return 0.0

    token_change = float(token_change)

    # Both near zero → can't determine correlation
    if abs(virtual_change) < 0.5 and abs(token_change) < 0.5:
        return 0.0

    # Same direction?
    same_direction = (token_change >= 0) == (virtual_change >= 0)
    if not same_direction:
        return 0.0

    # Magnitude similarity: ratio of smaller to larger change
    abs_t = abs(token_change)
    abs_v = abs(virtual_change)
    if max(abs_t, abs_v) == 0:
        return 0.0

    ratio = min(abs_t, abs_v) / max(abs_t, abs_v)

    # Correlation = direction match * magnitude similarity
    return round(ratio, 3)


def check_agent_activity(pair_data: dict) -> tuple[bool, str]:
    """Check if a Virtuals agent token has active social presence.

    Uses DexScreener pair info for social links as a proxy.
    Returns (is_active, comma-separated source string).
    """
    info = pair_data.get("info") or {}
    socials = info.get("socials") or []
    websites = info.get("websites") or []

    sources: list[str] = []

    for s in socials:
        stype = s.get("type", "").lower()
        if stype in ("twitter", "telegram", "discord"):
            sources.append(stype)

    if websites:
        sources.append("website")

    is_active = len(sources) >= 1
    return is_active, ",".join(sources) if sources else "none"


def compute_virtuals_bonus(correlation: float, agent_active: bool) -> float:
    """Compute platform bonus/penalty for a Virtuals token.

    Returns a value in [-20, +20].
    """
    bonus = 0.0

    # Correlation component
    if correlation < 0.3:
        bonus += 10.0  # independent price action = good
    elif correlation > 0.7:
        bonus -= 10.0  # just a $VIRTUAL proxy = bad

    # Agent activity component
    if agent_active:
        bonus += 10.0
    else:
        bonus -= 5.0

    return max(-20.0, min(20.0, bonus))
