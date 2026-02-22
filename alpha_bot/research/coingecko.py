import logging

import httpx

from alpha_bot.config import settings

logger = logging.getLogger(__name__)

# Common ticker → CoinGecko ID overrides (CoinGecko search is fuzzy,
# so hard-code the big ones to avoid mismatches)
_TICKER_ID_MAP: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "LINK": "chainlink",
    "UNI": "uniswap",
    "ATOM": "cosmos",
    "LTC": "litecoin",
    "ARB": "arbitrum",
    "OP": "optimism",
    "APT": "aptos",
    "SUI": "sui",
    "NEAR": "near",
    "FTM": "fantom",
    "INJ": "injective-protocol",
    "TIA": "celestia",
    "SEI": "sei-network",
    "PEPE": "pepe",
    "WIF": "dogwifcoin",
    "BONK": "bonk",
    "JUP": "jupiter-exchange-solana",
    "RENDER": "render-token",
    "FET": "fetch-ai",
    "RNDR": "render-token",
    "TAO": "bittensor",
    "WLD": "worldcoin-wld",
    "PENDLE": "pendle",
    "AAVE": "aave",
    "MKR": "maker",
    "EIGEN": "eigenlayer",
}


async def _resolve_coingecko_id(ticker: str, client: httpx.AsyncClient) -> str | None:
    upper = ticker.upper().strip("$")
    if upper in _TICKER_ID_MAP:
        return _TICKER_ID_MAP[upper]

    # Fallback: search CoinGecko
    try:
        resp = await client.get(
            f"{settings.coingecko_base_url}/search",
            params={"query": upper},
        )
        resp.raise_for_status()
        coins = resp.json().get("coins", [])
        for coin in coins:
            if coin.get("symbol", "").upper() == upper:
                return coin["id"]
    except httpx.HTTPError as exc:
        logger.warning("CoinGecko search failed for %s: %s", ticker, exc)

    return None


async def get_price_snapshot(ticker: str) -> dict | None:
    """Fetch price, market cap, volume, 24h change for a ticker."""
    async with httpx.AsyncClient(timeout=15) as client:
        coin_id = await _resolve_coingecko_id(ticker, client)
        if not coin_id:
            logger.warning("Could not resolve CoinGecko ID for %s", ticker)
            return None

        try:
            resp = await client.get(
                f"{settings.coingecko_base_url}/coins/{coin_id}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "community_data": "false",
                    "developer_data": "false",
                    "sparkline": "false",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("CoinGecko fetch failed for %s: %s", coin_id, exc)
            return None

    md = data.get("market_data", {})
    return {
        "coin_id": coin_id,
        "name": data.get("name", ticker),
        "symbol": data.get("symbol", ticker).upper(),
        "price_usd": md.get("current_price", {}).get("usd"),
        "market_cap_usd": md.get("market_cap", {}).get("usd"),
        "volume_24h_usd": md.get("total_volume", {}).get("usd"),
        "price_change_24h_pct": md.get("price_change_percentage_24h"),
        "price_change_7d_pct": md.get("price_change_percentage_7d"),
        "price_change_30d_pct": md.get("price_change_percentage_30d"),
        "ath_usd": md.get("ath", {}).get("usd"),
        "ath_change_pct": md.get("ath_change_percentage", {}).get("usd"),
    }
