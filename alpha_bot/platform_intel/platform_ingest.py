"""Ingest Virtuals and Flaunch tokens from the scanner pipeline.

Since these platforms lack listing APIs, we piggyback on DexScreener discovery.
Clanker gets bulk-ingested by the dedicated scraper; Virtuals and Flaunch trickle
in here from scanner_loop.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from alpha_bot.platform_intel.models import PlatformToken
from alpha_bot.storage.database import async_session

logger = logging.getLogger(__name__)

_KNOWN_PLATFORMS = {"clanker", "virtuals", "flaunch"}


async def maybe_ingest_platform_token(
    ca: str,
    platform: str,
    token_data: dict,
) -> None:
    """If platform is known and CA not in platform_tokens, insert it.

    Called from scanner_loop.py for every discovered token with a known platform.

    Args:
        ca: Contract address.
        platform: Detected platform (clanker, virtuals, flaunch).
        token_data: Dict with keys from DexScreener / scanner discovery:
            ticker, name, chain, mcap, liquidity_usd, volume_24h, pair_age_hours.
    """
    if platform not in _KNOWN_PLATFORMS:
        return

    ca = ca.strip().lower()

    # Check if already tracked
    async with async_session() as session:
        result = await session.execute(
            select(PlatformToken.id).where(PlatformToken.ca == ca).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return

    # Estimate deploy timestamp from pair age
    deploy_ts: datetime | None = None
    age_hours = token_data.get("pair_age_hours")
    if age_hours and age_hours > 0:
        from datetime import timedelta
        deploy_ts = datetime.utcnow() - timedelta(hours=age_hours)

    mcap = token_data.get("mcap")
    now = datetime.utcnow()

    pt = PlatformToken(
        ca=ca,
        chain=token_data.get("chain", "base"),
        platform=platform,
        name=(token_data.get("name") or "")[:256],
        symbol=(token_data.get("ticker") or "")[:32],
        deploy_timestamp=deploy_ts,
        current_mcap=mcap,
        liquidity_usd=token_data.get("liquidity_usd"),
        peak_mcap=mcap,
        peak_timestamp=now if mcap else None,
        check_status="pending",
        last_updated=now,
        created_at=now,
    )

    if mcap:
        pt.reached_100k = mcap >= 100_000
        pt.reached_500k = mcap >= 500_000
        pt.reached_1m = mcap >= 1_000_000

    async with async_session() as session:
        session.add(pt)
        await session.commit()

    logger.debug("Ingested %s token: %s (%s)", platform, pt.symbol, ca[:12])
