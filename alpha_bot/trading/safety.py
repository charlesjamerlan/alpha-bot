"""Pre-trade safety checks — run before every buy."""

import logging
import time

import httpx

from alpha_bot.config import settings
from alpha_bot.storage.repository import position_exists_for_mint
from alpha_bot.trading.models import TradeSignal

logger = logging.getLogger(__name__)

# In-memory cooldown tracker: {token_mint: last_buy_timestamp}
_cooldowns: dict[str, float] = {}


async def check_liquidity(token_mint: str) -> str | None:
    """Check DexScreener for minimum liquidity. Returns error string or None."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
            )
            resp.raise_for_status()
            data = resp.json()

        pairs = data.get("pairs") or []
        if not pairs:
            return f"No pairs found on DexScreener for {token_mint[:8]}..."

        # Use the highest-liquidity pair
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
        liq = (best.get("liquidity") or {}).get("usd", 0)

        if liq < settings.min_liquidity_usd:
            return (
                f"Liquidity too low: ${liq:,.0f} "
                f"(min: ${settings.min_liquidity_usd:,.0f})"
            )
        return None
    except Exception as exc:
        return f"Liquidity check failed: {exc}"


def check_cooldown(token_mint: str) -> str | None:
    """Prevent re-buying the same token within cooldown period."""
    last = _cooldowns.get(token_mint)
    if last is None:
        return None
    elapsed = time.time() - last
    if elapsed < settings.trade_cooldown_seconds:
        remaining = int(settings.trade_cooldown_seconds - elapsed)
        return f"Cooldown active — wait {remaining}s before re-buying {token_mint[:8]}..."
    return None


def set_cooldown(token_mint: str) -> None:
    """Mark a token as recently bought."""
    _cooldowns[token_mint] = time.time()


def check_max_positions(open_count: int) -> str | None:
    """Cap the number of concurrent open positions."""
    if open_count >= settings.max_open_positions:
        return (
            f"Max positions reached ({open_count}/{settings.max_open_positions})"
        )
    return None


async def check_duplicate_position(session, token_mint: str) -> str | None:
    """Prevent buying the same token twice."""
    if await position_exists_for_mint(session, token_mint):
        return f"Already have an open position for {token_mint[:8]}..."
    return None


async def run_all_checks(
    signal: TradeSignal,
    session,
    open_count: int,
) -> str | None:
    """Run all safety checks. Returns first failure message or None if all pass."""
    checks = [
        check_max_positions(open_count),
        check_cooldown(signal.token_mint),
        await check_duplicate_position(session, signal.token_mint),
        await check_liquidity(signal.token_mint),
    ]
    for result in checks:
        if result is not None:
            return result
    return None
