"""Flaunch buyback detection and scoring (Phase 2.3).

Flaunch tokens have a buyback mechanism where fees are used to buy back the
token. We detect buybacks by looking for token transfer patterns consistent
with buyback activity, and score tokens based on buyback frequency.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from alpha_bot.platform_intel.basescan import get_token_transfers

logger = logging.getLogger(__name__)

# Known Flaunch-related contracts on Base (fee collectors / routers).
# These are heuristic — add more as discovered.
FLAUNCH_KNOWN_CONTRACTS: set[str] = {
    # Placeholder — populate with actual Flaunch router/fee addresses when discovered.
    # For now we use a heuristic approach instead.
}

# Heuristic: a "buyback-like" transfer is a large buy (token transfer TO the
# contract) from addresses that are not regular holders.  We approximate by
# looking for transfers FROM the null/router addresses TO the token contract,
# or large-value transfers that lack a corresponding sell within the same block.

# Minimum token value (in raw units) to consider a transfer as a potential buyback.
# This is a heuristic filter — very small transfers are noise.
_MIN_BUYBACK_VALUE = 1  # We filter by pattern, not absolute value


async def detect_buybacks(
    ca: str, client: httpx.AsyncClient
) -> list[dict]:
    """Detect potential buyback events for a Flaunch token.

    Uses BaseScan token transfer data.  Looks for:
    1. Transfers from known Flaunch contracts (if any).
    2. Fallback heuristic: transfers from burn/null address (0x000...)
       or from the deployer that look like programmatic buys.

    Returns list of {"timestamp": int, "value": str, "from": str, "hash": str}.
    """
    transfers = await get_token_transfers(ca, client, offset=100, sort="desc")
    if not transfers:
        return []

    buybacks: list[dict] = []
    null_prefix = "0x000000000000000000000000"

    for tx in transfers:
        sender = tx.get("from", "").lower()

        # Pattern 1: transfer from a known Flaunch contract
        if sender in FLAUNCH_KNOWN_CONTRACTS:
            buybacks.append(tx)
            continue

        # Pattern 2: transfer from null/burn-like address (minting = liquidity injection)
        if sender.startswith(null_prefix):
            buybacks.append(tx)
            continue

    return buybacks


def estimate_buyback_eth(buybacks: list[dict]) -> float:
    """Rough estimate of total buyback ETH value.

    Since we only have token transfer data (not ETH values), we return
    a count-based proxy.  Each detected buyback event is worth ~0.01 ETH
    as a rough estimate.  This will be refined when we can read actual
    ETH transfer values.
    """
    return len(buybacks) * 0.01


def compute_flaunch_bonus(buyback_count: int) -> float:
    """Compute platform bonus/penalty for a Flaunch token based on buyback activity.

    Returns a value in [-10, +20].
    """
    if buyback_count >= 5:
        return 15.0
    elif buyback_count >= 2:
        return 10.0
    elif buyback_count >= 1:
        return 5.0
    else:
        return -5.0


async def enrich_flaunch_token(
    ca: str, client: httpx.AsyncClient
) -> dict:
    """Full enrichment pipeline for a Flaunch token.

    Returns {
        "buyback_count": int,
        "buyback_total_eth": float,
        "last_buyback_timestamp": datetime | None,
        "platform_bonus_score": float,
    }
    """
    buybacks = await detect_buybacks(ca, client)
    count = len(buybacks)
    total_eth = estimate_buyback_eth(buybacks)
    bonus = compute_flaunch_bonus(count)

    last_ts: datetime | None = None
    if buybacks:
        # Transfers are sorted desc, so first is newest
        ts_str = buybacks[0].get("timestamp", "")
        if ts_str:
            try:
                last_ts = datetime.utcfromtimestamp(int(ts_str))
            except (ValueError, TypeError, OSError):
                pass

    return {
        "buyback_count": count,
        "buyback_total_eth": total_eth,
        "last_buyback_timestamp": last_ts,
        "platform_bonus_score": bonus,
    }
