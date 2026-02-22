"""Scan CoinGecko for recent top-gaining tokens."""

import logging
from dataclasses import dataclass

import httpx

from alpha_bot.config import settings

logger = logging.getLogger(__name__)


@dataclass
class PumpCandidate:
    coin_id: str
    symbol: str
    name: str
    current_price: float
    market_cap: float
    volume_24h: float
    change_1h: float | None
    change_24h: float | None
    change_7d: float | None
    image: str = ""

    def to_dict(self) -> dict:
        return {
            "coin_id": self.coin_id,
            "symbol": self.symbol,
            "name": self.name,
            "current_price": self.current_price,
            "market_cap": self.market_cap,
            "volume_24h": self.volume_24h,
            "change_1h": self.change_1h,
            "change_24h": self.change_24h,
            "change_7d": self.change_7d,
        }


async def scan_top_gainers(
    timeframe: str = "24h",
    min_gain_pct: float = 20.0,
    min_market_cap: float = 500_000,
    limit: int = 30,
) -> list[PumpCandidate]:
    """
    Fetch tokens with the biggest recent price gains from CoinGecko.

    Args:
        timeframe: "24h" or "7d" — which price change to sort by
        min_gain_pct: Minimum % gain to include
        min_market_cap: Minimum market cap (USD) to filter noise
        limit: Max results to return
    """
    async with httpx.AsyncClient(timeout=20) as client:
        # Fetch top 250 coins by market cap (CoinGecko can't sort by % change)
        all_coins: list[dict] = []
        for page in (1, 2):
            try:
                resp = await client.get(
                    f"{settings.coingecko_base_url}/coins/markets",
                    params={
                        "vs_currency": "usd",
                        "order": "market_cap_desc",
                        "per_page": 250,
                        "page": page,
                        "sparkline": "false",
                        "price_change_percentage": "1h,24h,7d",
                    },
                )
                resp.raise_for_status()
                all_coins.extend(resp.json())
            except httpx.HTTPError as exc:
                logger.warning("CoinGecko markets page %d failed: %s", page, exc)
                break

    if not all_coins:
        return []

    # Pick the right change field
    change_key = {
        "1h": "price_change_percentage_1h_in_currency",
        "24h": "price_change_percentage_24h_in_currency",
        "7d": "price_change_percentage_7d_in_currency",
    }.get(timeframe, "price_change_percentage_24h_in_currency")

    candidates: list[PumpCandidate] = []
    for c in all_coins:
        change_val = c.get(change_key)
        mcap = c.get("market_cap") or 0
        if change_val is None or mcap < min_market_cap:
            continue
        if change_val < min_gain_pct:
            continue

        candidates.append(
            PumpCandidate(
                coin_id=c["id"],
                symbol=c.get("symbol", "").upper(),
                name=c.get("name", ""),
                current_price=c.get("current_price") or 0,
                market_cap=mcap,
                volume_24h=c.get("total_volume") or 0,
                change_1h=c.get("price_change_percentage_1h_in_currency"),
                change_24h=c.get("price_change_percentage_24h_in_currency"),
                change_7d=c.get("price_change_percentage_7d_in_currency"),
                image=c.get("image", ""),
            )
        )

    # Sort by the selected timeframe change (descending)
    candidates.sort(
        key=lambda p: getattr(p, f"change_{timeframe}") or 0, reverse=True
    )
    return candidates[:limit]


async def get_trending() -> list[PumpCandidate]:
    """Fetch CoinGecko trending coins (often early pump signals)."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{settings.coingecko_base_url}/search/trending"
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("CoinGecko trending failed: %s", exc)
            return []

    results: list[PumpCandidate] = []
    for item in data.get("coins", []):
        c = item.get("item", {})
        results.append(
            PumpCandidate(
                coin_id=c.get("id", ""),
                symbol=c.get("symbol", "").upper(),
                name=c.get("name", ""),
                current_price=c.get("price_btc", 0),  # trending only gives BTC price
                market_cap=c.get("market_cap_rank", 0),
                volume_24h=0,
                change_1h=None,
                change_24h=c.get("data", {}).get("price_change_percentage_24h", {}).get("usd"),
                change_7d=None,
                image=c.get("large", ""),
            )
        )
    return results
