"""Real-time Clanker deploy watcher — polls every 15s for new tokens.

Detects new Clanker deployments, enriches with DexScreener, runs the scoring
pipeline, and fires TG alerts for Tier 1/2 tokens.  Runs alongside (not
replacing) the 6-hour backfill scraper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import Callable, Coroutine

import httpx
from sqlalchemy import select

from alpha_bot.config import settings
from alpha_bot.platform_intel.clanker_scraper import (
    _enrich_with_dexscreener,
    _fetch_clanker_page,
)
from alpha_bot.platform_intel.models import PlatformToken
from alpha_bot.platform_intel.platform_ingest import maybe_ingest_platform_token
from alpha_bot.scanner.candidate_scorer import (
    compute_composite,
    compute_market_score,
    compute_profile_match,
)
from alpha_bot.scanner.depth_scorer import compute_depth
from alpha_bot.scanner.models import ScannerCandidate, TrendingTheme
from alpha_bot.scanner.token_matcher import match_token_to_themes
from alpha_bot.storage.database import async_session

logger = logging.getLogger(__name__)

# In-memory set of recently seen CAs — avoids hammering DB each poll
_recent_cas: deque[str] = deque(maxlen=500)

# Notification callback (set from main.py)
_notify_fn: Callable[[str, str], Coroutine] | None = None

# Cached themes + profile (refreshed periodically)
_themes_cache: list[TrendingTheme] = []
_themes_ts: float = 0
_profile_cache: dict | None = None
_profile_ts: float = 0
_CACHE_TTL = 300  # 5 minutes


def set_notify_fn(fn: Callable[[str, str], Coroutine]) -> None:
    global _notify_fn
    _notify_fn = fn


async def _notify(text: str) -> None:
    if _notify_fn:
        try:
            await _notify_fn(text, "HTML")
        except Exception as exc:
            logger.warning("Clanker realtime notify failed: %s", exc)


async def _get_themes() -> list[TrendingTheme]:
    global _themes_cache, _themes_ts
    now = time.time()
    if _themes_cache and (now - _themes_ts) < _CACHE_TTL:
        return _themes_cache
    try:
        async with async_session() as session:
            result = await session.execute(
                select(TrendingTheme)
                .order_by(TrendingTheme.velocity.desc())
                .limit(100)
            )
            _themes_cache = list(result.scalars().all())
            _themes_ts = now
    except Exception:
        logger.debug("Failed to refresh themes cache")
    return _themes_cache


async def _get_profile() -> dict | None:
    global _profile_cache, _profile_ts
    now = time.time()
    if _profile_cache is not None and (now - _profile_ts) < _CACHE_TTL:
        return _profile_cache
    try:
        from alpha_bot.tg_intel.pattern_extract import extract_winning_profile

        _profile_cache = await extract_winning_profile()
        _profile_ts = now
    except Exception:
        logger.debug("Failed to refresh winning profile cache")
    return _profile_cache


def _fmt_mcap(n: float | None) -> str:
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.0f}"


async def _ca_in_db(ca: str) -> bool:
    """Check if CA is already in platform_tokens."""
    async with async_session() as session:
        result = await session.execute(
            select(PlatformToken.id).where(PlatformToken.ca == ca).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def _already_scanned(ca: str) -> bool:
    """Check if CA is already in scanner_candidates."""
    async with async_session() as session:
        result = await session.execute(
            select(ScannerCandidate.id).where(ScannerCandidate.ca == ca).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def clanker_realtime_loop() -> None:
    """Poll Clanker API every N seconds for new token deployments."""
    interval = settings.clanker_realtime_interval_seconds
    logger.info("Clanker realtime watcher started (interval=%ds)", interval)

    while True:
        try:
            new_count = 0
            scored_count = 0

            async with httpx.AsyncClient(timeout=30) as client:
                tokens = await _fetch_clanker_page(1, client)
                if not tokens:
                    await asyncio.sleep(interval)
                    continue

                themes = await _get_themes()
                profile = await _get_profile()

                for raw in tokens:
                    # Filter to Base chain
                    chain_id = raw.get("chain_id")
                    if chain_id and str(chain_id) != "8453":
                        continue

                    ca = raw.get("contract_address") or raw.get("ca") or ""
                    if not ca:
                        continue
                    ca = ca.strip().lower()

                    # Fast in-memory check first
                    if ca in _recent_cas:
                        continue
                    _recent_cas.append(ca)

                    # DB check
                    if await _ca_in_db(ca):
                        continue

                    # Parse deploy timestamp
                    deployed_at_str = (
                        raw.get("deployed_at") or raw.get("created_at") or ""
                    )
                    deploy_ts: datetime | None = None
                    if deployed_at_str:
                        try:
                            deploy_ts = datetime.fromisoformat(
                                deployed_at_str.replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                        except (ValueError, TypeError):
                            pass

                    # Enrich with DexScreener
                    market = await _enrich_with_dexscreener(ca, client)
                    mcap = market.get("mcap") if market else None
                    liq = market.get("liquidity_usd") if market else None
                    vol = market.get("volume_24h") if market else None

                    # Basic filters
                    if liq is not None and liq < settings.scanner_min_liquidity:
                        continue
                    if mcap is not None and mcap < settings.scanner_min_mcap:
                        continue

                    # Ingest into platform_tokens
                    now = datetime.utcnow()
                    symbol = raw.get("symbol", "")[:32]
                    name = raw.get("name", "")[:256]

                    pt = PlatformToken(
                        ca=ca,
                        chain="base",
                        platform="clanker",
                        name=name,
                        symbol=symbol,
                        deploy_timestamp=deploy_ts,
                        current_mcap=mcap,
                        liquidity_usd=liq,
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

                    new_count += 1

                    # --- Scoring pipeline ---
                    age_seconds = None
                    if deploy_ts:
                        age_seconds = (now - deploy_ts).total_seconds()

                    age_hours = age_seconds / 3600 if age_seconds else None

                    token_data = {
                        "ca": ca,
                        "chain": "base",
                        "ticker": symbol,
                        "name": name,
                        "price_usd": market.get("price_usd") if market else None,
                        "mcap": mcap,
                        "liquidity_usd": liq,
                        "volume_24h": vol,
                        "pair_age_hours": age_hours,
                        "platform": "clanker",
                        "discovery_source": "realtime_deploy",
                    }

                    # Narrative matching
                    matched_names, nar_score = await match_token_to_themes(
                        name, symbol, themes,
                    )

                    # Depth score
                    depth = compute_depth(
                        name, symbol, matched_names, themes, platform="clanker",
                    )

                    # Profile match
                    token_data["_matched_themes"] = matched_names
                    prof_score = compute_profile_match(token_data, profile)

                    # Market quality
                    mkt_score = compute_market_score(token_data)

                    # Platform percentile
                    plat_score = 0.0
                    try:
                        from alpha_bot.platform_intel.percentile_rank import (
                            compute_platform_percentile,
                        )

                        pct = await compute_platform_percentile(
                            ca, "clanker", mcap, None, vol, age_hours,
                        )
                        plat_score = pct.get("overall_percentile", 0.0)
                    except Exception:
                        pass

                    # Composite score
                    composite, tier = compute_composite(
                        nar_score, depth, prof_score, mkt_score,
                        "realtime_deploy",
                        platform_score=plat_score,
                    )

                    # Save scanner candidate
                    if not await _already_scanned(ca):
                        candidate = ScannerCandidate(
                            ca=ca,
                            chain="base",
                            ticker=symbol,
                            name=name,
                            platform="clanker",
                            narrative_score=nar_score,
                            narrative_depth=depth,
                            profile_match_score=prof_score,
                            market_score=mkt_score,
                            platform_percentile=plat_score,
                            composite_score=composite,
                            matched_themes=json.dumps(matched_names),
                            price_usd=token_data.get("price_usd"),
                            mcap=mcap,
                            liquidity_usd=liq,
                            volume_24h=vol,
                            pair_age_hours=age_hours,
                            discovery_source="realtime_deploy",
                            alerted=False,
                            tier=tier,
                            discovered_at=now,
                            last_updated=now,
                        )
                        async with async_session() as session:
                            session.add(candidate)
                            await session.commit()

                    scored_count += 1

                    # Fire alert for Tier 1 or Tier 2
                    if tier <= 2:
                        age_str = (
                            f"{int(age_seconds)}s"
                            if age_seconds and age_seconds < 120
                            else f"{int(age_seconds / 60)}m"
                            if age_seconds
                            else "?"
                        )
                        themes_str = (
                            ", ".join(f'"{t}"' for t in matched_names[:3])
                            if matched_names
                            else "none"
                        )
                        tier_emoji = {
                            1: "\U0001f534",
                            2: "\U0001f7e1",
                        }.get(tier, "\u26ab")

                        alert_text = (
                            f"\U0001f195 <b>NEW DEPLOY: ${symbol}</b> (Clanker)\n\n"
                            f"{tier_emoji} Score: <b>{composite:.0f}/100</b> (Tier {tier})\n"
                            f"Age: {age_str}\n"
                            f"MCap: {_fmt_mcap(mcap)} | Liq: {_fmt_mcap(liq)}\n\n"
                            f"Narrative: {themes_str} — {depth // 25} layers\n"
                            f"Profile match: {prof_score:.0f}/100\n\n"
                            f"<code>{ca}</code>"
                        )
                        await _notify(alert_text)

                        # Mark as alerted
                        async with async_session() as session:
                            result = await session.execute(
                                select(ScannerCandidate).where(
                                    ScannerCandidate.ca == ca
                                )
                            )
                            row = result.scalar_one_or_none()
                            if row:
                                row.alerted = True
                                await session.commit()

            if new_count > 0:
                logger.info(
                    "Clanker realtime: %d new tokens, %d scored",
                    new_count,
                    scored_count,
                )

        except Exception:
            logger.exception("Clanker realtime loop error")

        await asyncio.sleep(interval)
