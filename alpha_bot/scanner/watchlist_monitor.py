"""Watchlist degradation monitor — alerts when Tier 1/2 tokens degrade.

Runs periodically and checks current mcap/liquidity vs. discovery values.
Fires TG alerts when significant degradation is detected.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Awaitable

import httpx
from sqlalchemy import select

from alpha_bot.config import settings
from alpha_bot.research.dexscreener import extract_pair_details, get_token_by_address
from alpha_bot.scanner.models import ScannerCandidate
from alpha_bot.storage.database import async_session

logger = logging.getLogger(__name__)

# Notification callback (set from main.py)
_notify_fn: Callable[[str, str], Awaitable[None]] | None = None

# Track which tokens we've already alerted for (avoid spam)
_alerted_cas: set[str] = set()


def set_notify_fn(fn: Callable[[str, str], Awaitable[None]]) -> None:
    """Set the TG notification callback."""
    global _notify_fn
    _notify_fn = fn


def _fmt_mcap(n: float | None) -> str:
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.0f}"


async def watchlist_monitor_loop() -> None:
    """Main loop: check Tier 1/2 tokens for degradation every N minutes."""
    interval = settings.watchlist_monitor_interval_seconds
    threshold = settings.watchlist_degradation_pct

    logger.info(
        "Watchlist monitor started (interval=%ds, degradation_threshold=%.0f%%)",
        interval, threshold,
    )

    while True:
        try:
            await _check_watchlist(threshold)
        except Exception:
            logger.exception("Watchlist monitor error")

        await asyncio.sleep(interval)


async def _check_watchlist(threshold_pct: float) -> None:
    """Fetch Tier 1/2 candidates from last 7 days and check for degradation."""
    cutoff = datetime.utcnow() - timedelta(days=7)

    async with async_session() as session:
        result = await session.execute(
            select(ScannerCandidate)
            .where(
                ScannerCandidate.tier.in_([1, 2]),
                ScannerCandidate.discovered_at >= cutoff,
            )
            .order_by(ScannerCandidate.composite_score.desc())
        )
        candidates = list(result.scalars().all())

    if not candidates:
        return

    checked = 0
    degraded = 0

    async with httpx.AsyncClient(timeout=15) as client:
        for c in candidates:
            if c.ca in _alerted_cas:
                continue

            if not c.mcap or c.mcap <= 0:
                continue

            try:
                pair = await get_token_by_address(c.ca, client)
                if not pair:
                    continue

                d = extract_pair_details(pair)
                current_mcap = d.get("market_cap")
                current_liq = d.get("liquidity_usd")

                if not current_mcap:
                    continue

                checked += 1
                pct_change = ((current_mcap - c.mcap) / c.mcap) * 100

                # Check degradation threshold
                if pct_change <= threshold_pct:
                    degraded += 1
                    _alerted_cas.add(c.ca)

                    liq_str = _fmt_mcap(current_liq)
                    discovery_liq_str = _fmt_mcap(c.liquidity_usd)

                    text = (
                        f"⚠️ <b>DEGRADATION: ${c.ticker}</b>\n\n"
                        f"MCap: {_fmt_mcap(c.mcap)} → {_fmt_mcap(current_mcap)} "
                        f"({pct_change:+.0f}%)\n"
                        f"Liquidity: {liq_str} (was {discovery_liq_str})\n"
                        f"Score at discovery: {c.composite_score:.0f}/100\n\n"
                        f"Consider reviewing position.\n"
                        f"<code>{c.ca}</code>"
                    )

                    if _notify_fn:
                        await _notify_fn(text, "HTML")

                    logger.info(
                        "Degradation alert: %s (%s) mcap %s -> %s (%.0f%%)",
                        c.ticker, c.ca[:12],
                        _fmt_mcap(c.mcap), _fmt_mcap(current_mcap), pct_change,
                    )

                # Also check liquidity floor
                elif (
                    current_liq is not None
                    and current_liq < settings.scanner_min_liquidity
                    and c.ca not in _alerted_cas
                ):
                    _alerted_cas.add(c.ca)

                    text = (
                        f"⚠️ <b>LOW LIQUIDITY: ${c.ticker}</b>\n\n"
                        f"Liquidity: {_fmt_mcap(current_liq)} "
                        f"(below ${settings.scanner_min_liquidity:,.0f} threshold)\n"
                        f"MCap: {_fmt_mcap(current_mcap)}\n"
                        f"Score at discovery: {c.composite_score:.0f}/100\n\n"
                        f"<code>{c.ca}</code>"
                    )

                    if _notify_fn:
                        await _notify_fn(text, "HTML")

                # Rate limit DexScreener calls
                await asyncio.sleep(0.3)

            except Exception:
                logger.debug("Watchlist check failed for %s", c.ca[:12], exc_info=True)

    if checked > 0:
        logger.debug(
            "Watchlist monitor: checked %d tokens, %d degradation alerts",
            checked, degraded,
        )
