"""Async loop that polls token prices and triggers exits."""

import asyncio
import logging

import httpx

from alpha_bot.config import settings
from alpha_bot.storage.database import async_session
from alpha_bot.storage.repository import get_open_positions
from alpha_bot.trading.position_manager import check_exits

logger = logging.getLogger(__name__)


async def _get_jupiter_prices(
    mints: list[str], client: httpx.AsyncClient
) -> dict[str, float]:
    """Batch fetch from Jupiter Price API v2."""
    try:
        resp = await client.get(
            "https://api.jup.ag/price/v2",
            params={"ids": ",".join(mints)},
        )
        resp.raise_for_status()
        data = resp.json()
        prices = {}
        for mint in mints:
            price_data = data.get("data", {}).get(mint)
            if price_data and price_data.get("price"):
                prices[mint] = float(price_data["price"])
        return prices
    except Exception as exc:
        logger.warning("Jupiter price fetch failed: %s", exc)
        return {}


async def _get_dexscreener_price(
    mint: str, client: httpx.AsyncClient
) -> float | None:
    """Fetch price from DexScreener for a single token (fallback)."""
    try:
        resp = await client.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return None
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
        price = best.get("priceUsd")
        return float(price) if price else None
    except Exception:
        return None


async def get_token_prices(mints: list[str]) -> dict[str, float]:
    """Batch fetch prices — DexScreener primary, Jupiter as fallback."""
    if not mints:
        return {}

    async with httpx.AsyncClient(timeout=15) as client:
        # DexScreener as primary (Jupiter now requires API key)
        prices: dict[str, float] = {}
        for mint in mints:
            price = await _get_dexscreener_price(mint, client)
            if price is not None:
                prices[mint] = price
            await asyncio.sleep(0.3)  # DexScreener rate limit

        # Jupiter fallback for any misses
        missing = [m for m in mints if m not in prices]
        if missing:
            jup_prices = await _get_jupiter_prices(missing, client)
            prices.update(jup_prices)

    return prices


async def price_monitor_loop(telethon_client) -> None:
    """Main monitoring loop — runs every N seconds, checks all open positions."""
    logger.info(
        "Price monitor started (polling every %ds)", settings.price_poll_interval
    )

    while True:
        try:
            async with async_session() as session:
                positions = await get_open_positions(session)

            if positions:
                mints = [p.token_mint for p in positions]
                prices = await get_token_prices(mints)

                for pos in positions:
                    price = prices.get(pos.token_mint)
                    if price is not None:
                        await check_exits(pos, price, telethon_client)

        except Exception:
            logger.exception("Error in price monitor loop")

        await asyncio.sleep(settings.price_poll_interval)
