"""DexScreener token discovery — boosted, latest profiles, and new pairs on Base."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from alpha_bot.config import settings
from alpha_bot.tg_intel.platform_detect import detect_platform

logger = logging.getLogger(__name__)

_TIMEOUT = 15


def _parse_pair(pair: dict, source: str) -> dict | None:
    """Extract a normalized token dict from a DexScreener pair."""
    chain_id = pair.get("chainId", "")
    if chain_id != settings.scanner_chain_filter:
        return None

    base = pair.get("baseToken") or {}
    ca = base.get("address", "")
    if not ca:
        return None

    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    liq = (pair.get("liquidity") or {}).get("usd") or 0

    if mcap < settings.scanner_min_mcap or mcap > settings.scanner_max_mcap:
        return None
    if liq < settings.scanner_min_liquidity:
        return None

    # Pair age in hours
    created_at = pair.get("pairCreatedAt")
    pair_age_hours = None
    if created_at:
        try:
            created_dt = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
            pair_age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
        except (ValueError, TypeError, OSError):
            pass

    volume_24h = (pair.get("volume") or {}).get("h24") or 0
    price_usd = None
    try:
        price_usd = float(pair.get("priceUsd", 0))
    except (ValueError, TypeError):
        pass

    platform = detect_platform(ca, pair_data=pair)

    return {
        "ca": ca,
        "chain": chain_id,
        "ticker": base.get("symbol", "???").upper(),
        "name": base.get("name", ""),
        "price_usd": price_usd,
        "mcap": mcap,
        "liquidity_usd": liq,
        "volume_24h": volume_24h,
        "pair_age_hours": pair_age_hours,
        "platform": platform,
        "discovery_source": source,
    }


async def _fetch_boosted_tokens(client: httpx.AsyncClient) -> list[dict]:
    """Tokens with active paid boosts (team activity signal)."""
    try:
        resp = await client.get("https://api.dexscreener.com/token-boosts/top/v1")
        if resp.status_code == 429:
            logger.debug("DexScreener boosted rate-limited")
            return []
        resp.raise_for_status()
        items = resp.json()
    except (httpx.HTTPError, Exception) as exc:
        logger.warning("DexScreener boosted fetch failed: %s", exc)
        return []

    results = []
    if isinstance(items, list):
        for item in items:
            chain_id = item.get("chainId", "")
            ca = item.get("tokenAddress", "")
            if chain_id != settings.scanner_chain_filter or not ca:
                continue
            # Boosted tokens don't have full pair data, so create a minimal entry
            # that will be enriched by the scanner loop
            results.append({
                "ca": ca,
                "chain": chain_id,
                "ticker": "",
                "name": item.get("description", ""),
                "price_usd": None,
                "mcap": None,
                "liquidity_usd": None,
                "volume_24h": None,
                "pair_age_hours": None,
                "platform": "unknown",
                "discovery_source": "boosted",
                "_needs_enrichment": True,
            })

    return results


async def _fetch_latest_profiles(client: httpx.AsyncClient) -> list[dict]:
    """Latest token profiles on DexScreener."""
    try:
        resp = await client.get("https://api.dexscreener.com/token-profiles/latest/v1")
        if resp.status_code == 429:
            logger.debug("DexScreener profiles rate-limited")
            return []
        resp.raise_for_status()
        items = resp.json()
    except (httpx.HTTPError, Exception) as exc:
        logger.warning("DexScreener profiles fetch failed: %s", exc)
        return []

    results = []
    if isinstance(items, list):
        for item in items:
            chain_id = item.get("chainId", "")
            ca = item.get("tokenAddress", "")
            if chain_id != settings.scanner_chain_filter or not ca:
                continue
            results.append({
                "ca": ca,
                "chain": chain_id,
                "ticker": "",
                "name": item.get("description", ""),
                "price_usd": None,
                "mcap": None,
                "liquidity_usd": None,
                "volume_24h": None,
                "pair_age_hours": None,
                "platform": "unknown",
                "discovery_source": "profile",
                "_needs_enrichment": True,
            })

    return results


async def _enrich_token(ca: str, client: httpx.AsyncClient, source: str) -> dict | None:
    """Enrich a token with full pair data from DexScreener."""
    try:
        resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}")
        if resp.status_code == 429:
            return None
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, Exception):
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    return _parse_pair(best, source)


async def discover_tokens() -> list[dict]:
    """Discover tokens from multiple DexScreener endpoints.

    Returns a list of token dicts ready for matching/scoring.
    """
    results: list[dict] = []
    seen_cas: set[str] = set()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        boosted = await _fetch_boosted_tokens(client)
        profiles = await _fetch_latest_profiles(client)

        # Enrich boosted/profile tokens that lack pair data
        for raw in boosted + profiles:
            ca = raw["ca"]
            if ca in seen_cas:
                continue
            seen_cas.add(ca)

            if raw.get("_needs_enrichment"):
                enriched = await _enrich_token(ca, client, raw["discovery_source"])
                if enriched:
                    results.append(enriched)
            else:
                results.append(raw)

    logger.info("DexScreener discovery: %d tokens after filtering", len(results))
    return results
