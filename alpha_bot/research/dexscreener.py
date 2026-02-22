"""DexScreener + GeckoTerminal API clients for Solana token price lookups."""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"


@dataclass
class DexPricePoint:
    price_usd: float
    market_cap: float | None
    volume_24h: float | None
    pair_address: str
    dex: str


async def get_token_by_address(
    address: str, client: httpx.AsyncClient
) -> dict | None:
    """Look up a token on DexScreener by contract address (any chain).

    Returns the best (highest liquidity) pair info, or None.
    """
    try:
        resp = await client.get(
            f"{DEXSCREENER_BASE}/tokens/{address}",
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("DexScreener lookup failed for %s: %s", address[:12], exc)
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    # Pick the pair with highest liquidity
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    return best


async def get_token_by_ticker(
    ticker: str, client: httpx.AsyncClient, chains: list[str] | None = None,
) -> dict | None:
    """Search DexScreener for a ticker symbol.

    Args:
        ticker: Token ticker symbol.
        client: httpx async client.
        chains: List of chain IDs to filter by (e.g. ["solana", "base"]).
                Defaults to ["solana", "base", "ethereum", "bsc"].

    Returns the best matching pair, or None.
    """
    if chains is None:
        chains = ["solana", "base", "ethereum", "bsc"]

    try:
        resp = await client.get(
            f"{DEXSCREENER_BASE}/search",
            params={"q": ticker},
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("DexScreener search failed for %s: %s", ticker, exc)
        return None

    pairs = data.get("pairs") or []
    # Filter to supported chains matching the ticker
    matching = [
        p for p in pairs
        if p.get("chainId") in chains
        and p.get("baseToken", {}).get("symbol", "").upper() == ticker.upper()
    ]

    if not matching:
        return None

    # Pick highest liquidity
    return max(matching, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def extract_price_from_pair(pair: dict) -> float | None:
    """Extract current USD price from a DexScreener pair response."""
    price = pair.get("priceUsd")
    if price:
        try:
            return float(price)
        except (ValueError, TypeError):
            pass
    return None


def extract_token_name(pair: dict) -> str:
    """Get the token name/symbol from a pair."""
    base = pair.get("baseToken") or {}
    return base.get("symbol", "???").upper()


def extract_pair_details(pair: dict) -> dict:
    """Extract useful details from a DexScreener pair response."""
    base = pair.get("baseToken") or {}
    liquidity = pair.get("liquidity") or {}
    price_change = pair.get("priceChange") or {}

    price_usd = None
    try:
        price_usd = float(pair.get("priceUsd", 0))
    except (ValueError, TypeError):
        pass

    return {
        "symbol": base.get("symbol", "???").upper(),
        "name": base.get("name", ""),
        "address": base.get("address", ""),
        "price_usd": price_usd,
        "market_cap": pair.get("marketCap") or pair.get("fdv"),
        "liquidity_usd": liquidity.get("usd"),
        "volume_24h": pair.get("volume", {}).get("h24"),
        "price_change_5m": price_change.get("m5"),
        "price_change_1h": price_change.get("h1"),
        "price_change_6h": price_change.get("h6"),
        "price_change_24h": price_change.get("h24"),
        "pair_address": pair.get("pairAddress", ""),
        "dex": pair.get("dexId", ""),
        "pair_created_at": pair.get("pairCreatedAt"),
    }


# --- GeckoTerminal (historical OHLCV) ---


# Map DexScreener chainId to GeckoTerminal network slug
_CHAIN_TO_GT_NETWORK = {
    "solana": "solana",
    "base": "base",
    "ethereum": "eth",
    "bsc": "bsc",
}


async def gt_find_pool(
    token_address: str, client: httpx.AsyncClient, chain: str = "solana",
) -> str | None:
    """Find the best pool address for a token via GeckoTerminal."""
    network = _CHAIN_TO_GT_NETWORK.get(chain, chain)
    try:
        resp = await client.get(
            f"{GECKOTERMINAL_BASE}/networks/{network}/tokens/{token_address}/pools",
            params={"page": 1},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("GeckoTerminal pool lookup failed for %s: %s", token_address[:12], exc)
        return None

    pools = data.get("data") or []
    if not pools:
        return None

    # First pool is typically highest volume
    pool_id = pools[0].get("id", "")
    # id format: "<network>_<pool_address>"
    if "_" in pool_id:
        return pool_id.split("_", 1)[1]
    # fallback: try attributes.address
    return pools[0].get("attributes", {}).get("address")


async def gt_get_ohlcv(
    pool_address: str,
    client: httpx.AsyncClient,
    timeframe: str = "day",
    limit: int = 90,
    chain: str = "solana",
) -> list[tuple[int, float]]:
    """Fetch historical OHLCV from GeckoTerminal.

    Returns list of (timestamp_unix, close_price_usd) sorted oldest first.
    """
    network = _CHAIN_TO_GT_NETWORK.get(chain, chain)
    try:
        resp = await client.get(
            f"{GECKOTERMINAL_BASE}/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}",
            params={"limit": limit, "currency": "usd"},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("GeckoTerminal OHLCV failed for %s: %s", pool_address[:12], exc)
        return []

    ohlcv_list = (
        data.get("data", {}).get("attributes", {}).get("ohlcv_list") or []
    )
    if not ohlcv_list:
        return []

    # Each entry: [timestamp, open, high, low, close, volume]
    # Return (timestamp_seconds, close_price) sorted oldest first
    prices = []
    for candle in ohlcv_list:
        if len(candle) >= 5:
            ts = int(candle[0])
            close = float(candle[4])
            prices.append((ts, close))

    prices.sort(key=lambda p: p[0])
    return prices


async def gt_get_token_price_history(
    token_address: str,
    client: httpx.AsyncClient,
    days: int = 90,
    chain: str = "solana",
) -> list[tuple[int, float]]:
    """Full pipeline: find pool -> get OHLCV for a token address.

    Resolution strategy:
    - <= 2 days: minute candles (best for fresh pump.fun tokens)
    - <= 41 days: hourly candles
    - > 41 days: daily candles

    Returns list of (timestamp_seconds, close_price_usd).
    """
    pool = await gt_find_pool(token_address, client, chain=chain)
    if not pool:
        return []

    # For very new tokens (< 2 days), try minute candles first
    if days <= 2:
        prices = await gt_get_ohlcv(
            pool, client, timeframe="minute", limit=1000, chain=chain
        )
        if prices:
            return prices

    # Try hourly (up to 1000 candles = ~41 days)
    hour_limit = min(days * 24, 1000)
    prices = await gt_get_ohlcv(pool, client, timeframe="hour", limit=hour_limit, chain=chain)
    if prices:
        return prices

    # Fallback to daily
    return await gt_get_ohlcv(pool, client, timeframe="day", limit=days, chain=chain)
