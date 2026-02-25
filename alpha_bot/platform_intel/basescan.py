"""Etherscan V2 API client for Base chain (chainid=8453).

Used for holder counts and contract creation info.
Free tier: 5 calls/sec — we self-limit to 4/sec (0.25s sleep).
"""

import asyncio
import logging

import httpx

from alpha_bot.config import settings

logger = logging.getLogger(__name__)

ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
BASE_CHAIN_ID = "8453"

# Self-imposed rate limit (seconds between calls)
_RATE_LIMIT_SLEEP = 0.25


async def _etherscan_get(
    params: dict,
    client: httpx.AsyncClient,
    max_retries: int = 3,
) -> dict | None:
    """Make an Etherscan V2 API GET with retry on 429."""
    params = {**params, "chainid": BASE_CHAIN_ID, "apikey": settings.basescan_api_key}

    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(ETHERSCAN_V2_BASE, params=params)
            if resp.status_code == 429:
                if attempt < max_retries:
                    wait = 2 * (2 ** attempt)
                    logger.debug("Etherscan 429 — retrying in %ds", wait)
                    await asyncio.sleep(wait)
                    continue
                logger.warning("Etherscan 429 — exhausted retries")
                return None
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "1" or data.get("message") == "OK":
                return data
            # Some endpoints return status=0 for "no data" — not an error
            if data.get("message") == "No data found":
                return None
            logger.debug("Etherscan non-OK response: %s", data.get("message"))
            return data
        except httpx.HTTPError as exc:
            if attempt < max_retries:
                await asyncio.sleep(2 * (2 ** attempt))
                continue
            logger.warning("Etherscan request failed: %s", exc)
            return None

    return None


async def get_holder_count(ca: str, client: httpx.AsyncClient) -> int | None:
    """Get the number of token holders for a contract on Base.

    Returns int holder count or None on failure.
    """
    data = await _etherscan_get(
        {
            "module": "token",
            "action": "tokenholdercount",
            "contractaddress": ca,
        },
        client,
    )
    if data is None:
        return None

    result = data.get("result")
    if result is not None:
        try:
            return int(result)
        except (ValueError, TypeError):
            pass
    return None


async def get_contract_creation(
    ca: str, client: httpx.AsyncClient
) -> dict | None:
    """Get contract creation info (block, timestamp, creator).

    Returns {"block": str, "timestamp": str, "creator": str} or None.
    """
    data = await _etherscan_get(
        {
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": ca,
        },
        client,
    )
    if data is None:
        return None

    result = data.get("result")
    if isinstance(result, list) and result:
        entry = result[0]
        return {
            "block": entry.get("blockNumber", ""),
            "timestamp": entry.get("timestamp", ""),
            "creator": entry.get("contractCreator", ""),
        }
    return None
