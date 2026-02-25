"""Clanker token scraper and platform lifecycle checker.

clanker_scraper_loop() — Periodically fetches new tokens from Clanker API.
platform_check_loop()  — Fills holder/mcap snapshots at 1h/6h/24h/7d checkpoints.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from alpha_bot.config import settings
from alpha_bot.platform_intel.basescan import get_holder_count, _RATE_LIMIT_SLEEP
from alpha_bot.platform_intel.models import PlatformToken
from alpha_bot.research.dexscreener import extract_pair_details, get_token_by_address
from alpha_bot.storage.database import async_session

logger = logging.getLogger(__name__)

# Rate limit between Clanker API pages
_CLANKER_PAGE_SLEEP = 0.5


async def _enrich_with_dexscreener(
    ca: str, client: httpx.AsyncClient
) -> dict | None:
    """Fetch current market data from DexScreener for a CA.

    Returns {mcap, liquidity_usd, volume_24h, price_usd} or None.
    """
    pair = await get_token_by_address(ca, client)
    if not pair:
        return None
    d = extract_pair_details(pair)
    return {
        "mcap": d.get("market_cap"),
        "liquidity_usd": d.get("liquidity_usd"),
        "volume_24h": d.get("volume_24h"),
        "price_usd": d.get("price_usd"),
    }


async def _fetch_clanker_page(
    page: int, client: httpx.AsyncClient
) -> list[dict]:
    """Fetch a single page of tokens from Clanker API."""
    try:
        resp = await client.get(
            f"{settings.clanker_api_base}/tokens",
            params={"sort": "desc", "page": page, "pageSize": 50},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # API returns {"data": [...]} or just [...]
        if isinstance(data, dict):
            return data.get("data", [])
        if isinstance(data, list):
            return data
        return []
    except httpx.HTTPError as exc:
        logger.warning("Clanker API page %d failed: %s", page, exc)
        return []


async def _ca_exists(ca: str) -> bool:
    """Check if a CA already exists in platform_tokens."""
    async with async_session() as session:
        result = await session.execute(
            select(PlatformToken.id).where(PlatformToken.ca == ca).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def clanker_scraper_loop() -> None:
    """Periodically scrape Clanker API for new Base tokens.

    First run: backfill last N days. Subsequent runs: stop at first known CA.
    """
    logger.info(
        "Clanker scraper started (interval=%ds, backfill=%dd)",
        settings.clanker_scraper_interval_seconds,
        settings.clanker_backfill_days,
    )

    first_run = True

    while True:
        try:
            cutoff = datetime.utcnow() - timedelta(days=settings.clanker_backfill_days)
            new_count = 0
            stop = False

            async with httpx.AsyncClient(timeout=30) as client:
                page = 1
                while not stop:
                    tokens = await _fetch_clanker_page(page, client)
                    if not tokens:
                        break

                    for raw in tokens:
                        # Filter to Base chain
                        chain_id = raw.get("chain_id")
                        if chain_id and str(chain_id) != "8453":
                            continue

                        ca = raw.get("contract_address") or raw.get("ca") or ""
                        if not ca:
                            continue
                        ca = ca.strip().lower()

                        # Parse deploy timestamp
                        deployed_at_str = raw.get("deployed_at") or raw.get("created_at") or ""
                        deploy_ts: datetime | None = None
                        if deployed_at_str:
                            try:
                                deploy_ts = datetime.fromisoformat(
                                    deployed_at_str.replace("Z", "+00:00")
                                ).replace(tzinfo=None)
                            except (ValueError, TypeError):
                                pass

                        # On first run, stop if token is older than backfill window
                        if first_run and deploy_ts and deploy_ts < cutoff:
                            stop = True
                            break

                        # On subsequent runs, stop at first known CA
                        if not first_run and await _ca_exists(ca):
                            stop = True
                            break

                        if await _ca_exists(ca):
                            continue

                        # Enrich with DexScreener
                        market = await _enrich_with_dexscreener(ca, client)
                        mcap = market.get("mcap") if market else None
                        liq = market.get("liquidity_usd") if market else None

                        now = datetime.utcnow()
                        pt = PlatformToken(
                            ca=ca,
                            chain="base",
                            platform="clanker",
                            name=raw.get("name", "")[:256],
                            symbol=raw.get("symbol", "")[:32],
                            deploy_timestamp=deploy_ts,
                            current_mcap=mcap,
                            liquidity_usd=liq,
                            peak_mcap=mcap,
                            peak_timestamp=now if mcap else None,
                            check_status="pending",
                            last_updated=now,
                            created_at=now,
                        )

                        # Check milestone flags
                        if mcap:
                            pt.reached_100k = mcap >= 100_000
                            pt.reached_500k = mcap >= 500_000
                            pt.reached_1m = mcap >= 1_000_000

                        async with async_session() as session:
                            session.add(pt)
                            await session.commit()

                        new_count += 1

                    page += 1
                    await asyncio.sleep(_CLANKER_PAGE_SLEEP)

            mode = "backfill" if first_run else "incremental"
            if new_count > 0:
                logger.info("Clanker scraper: %d new tokens ingested (%s)", new_count, mode)
            else:
                logger.debug("Clanker scraper: no new tokens (%s)", mode)

            first_run = False

        except Exception:
            logger.exception("Clanker scraper error")

        await asyncio.sleep(settings.clanker_scraper_interval_seconds)


async def platform_check_loop() -> None:
    """Periodically fill holder/mcap snapshots for platform tokens.

    Checks age-based milestones: 1h, 6h, 24h, 7d.
    Processes max 100 tokens per cycle.
    """
    logger.info(
        "Platform check loop started (interval=%ds)",
        settings.platform_check_interval_seconds,
    )

    while True:
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(PlatformToken)
                    .where(PlatformToken.check_status != "complete")
                    .order_by(PlatformToken.deploy_timestamp.asc())
                    .limit(100)
                )
                tokens = list(result.scalars().all())

            if not tokens:
                await asyncio.sleep(settings.platform_check_interval_seconds)
                continue

            checked = 0
            async with httpx.AsyncClient(timeout=30) as client:
                for pt in tokens:
                    try:
                        await _check_token(pt, client)
                        checked += 1
                    except Exception:
                        logger.exception("Check failed for %s", pt.ca[:12])
                    await asyncio.sleep(_RATE_LIMIT_SLEEP)

            if checked > 0:
                logger.info("Platform check: updated %d/%d tokens", checked, len(tokens))

        except Exception:
            logger.exception("Platform check loop error")

        await asyncio.sleep(settings.platform_check_interval_seconds)


async def _check_token(pt: PlatformToken, client: httpx.AsyncClient) -> None:
    """Fill snapshot fields based on token age, then persist."""
    if not pt.deploy_timestamp:
        # Can't compute age — mark complete to avoid re-processing
        async with async_session() as session:
            result = await session.execute(
                select(PlatformToken).where(PlatformToken.id == pt.id)
            )
            row = result.scalar_one_or_none()
            if row:
                row.check_status = "complete"
                await session.commit()
        return

    now = datetime.utcnow()
    age = now - pt.deploy_timestamp
    age_hours = age.total_seconds() / 3600
    updated = False

    # Fetch current holder count (BaseScan)
    holders: int | None = None
    if settings.basescan_api_key:
        holders = await get_holder_count(pt.ca, client)
        await asyncio.sleep(_RATE_LIMIT_SLEEP)

    # Fetch current market data (DexScreener)
    market = await _enrich_with_dexscreener(pt.ca, client)
    current_mcap = market.get("mcap") if market else None

    async with async_session() as session:
        result = await session.execute(
            select(PlatformToken).where(PlatformToken.id == pt.id)
        )
        row = result.scalar_one_or_none()
        if not row:
            return

        # Fill age-based snapshots
        if age_hours >= 1 and row.holders_1h is None:
            row.holders_1h = holders
            row.mcap_1h = current_mcap
            updated = True

        if age_hours >= 6 and row.holders_6h is None:
            row.holders_6h = holders
            updated = True

        if age_hours >= 24 and row.holders_24h is None:
            row.holders_24h = holders
            row.mcap_24h = current_mcap
            updated = True

        if age_hours >= 168 and row.holders_7d is None:  # 7 days
            row.holders_7d = holders
            row.survived_7d = True
            row.check_status = "complete"
            updated = True

        # Always update current mcap
        if current_mcap is not None:
            row.current_mcap = current_mcap
            updated = True

            # Update peak if new high
            if row.peak_mcap is None or current_mcap > row.peak_mcap:
                row.peak_mcap = current_mcap
                row.peak_timestamp = now
                # Update vol/mcap at peak
                if market and market.get("volume_24h") and current_mcap > 0:
                    row.volume_24h_at_peak = market["volume_24h"]
                    row.vol_mcap_ratio_at_peak = market["volume_24h"] / current_mcap

            # Milestone flags
            row.reached_100k = row.reached_100k or current_mcap >= 100_000
            row.reached_500k = row.reached_500k or current_mcap >= 500_000
            row.reached_1m = row.reached_1m or current_mcap >= 1_000_000

        if market and market.get("liquidity_usd") is not None:
            row.liquidity_usd = market["liquidity_usd"]

        # Update check_status based on what we've filled
        if row.check_status != "complete":
            if any([row.holders_1h, row.mcap_1h, row.holders_6h, row.holders_24h]):
                row.check_status = "partial"

        if updated:
            row.last_updated = now
            await session.commit()
