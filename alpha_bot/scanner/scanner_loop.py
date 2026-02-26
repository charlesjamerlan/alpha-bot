"""Main scanner loop — discover tokens, match, score, persist, alert."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from sqlalchemy import select

from alpha_bot.config import settings
from alpha_bot.scanner.alerts import fire_scanner_alert
from alpha_bot.scanner.candidate_scorer import (
    compute_composite,
    compute_market_score,
    compute_profile_match,
)
from alpha_bot.scanner.depth_scorer import compute_depth
from alpha_bot.scanner.dex_discovery import discover_tokens
from alpha_bot.scanner.models import ScannerCandidate, TrendingTheme
from alpha_bot.scanner.token_matcher import match_token_to_themes
from alpha_bot.storage.database import async_session

logger = logging.getLogger(__name__)

# Cache winning profile for 1 hour
_profile_cache: dict | None = None
_profile_cache_ts: float = 0
_PROFILE_TTL = 3600  # seconds


async def _get_cached_profile() -> dict | None:
    """Load winning profile with 1-hour cache."""
    global _profile_cache, _profile_cache_ts

    import time
    now = time.time()
    if _profile_cache is not None and (now - _profile_cache_ts) < _PROFILE_TTL:
        return _profile_cache

    try:
        from alpha_bot.tg_intel.pattern_extract import extract_winning_profile
        _profile_cache = await extract_winning_profile()
        _profile_cache_ts = now
    except Exception as exc:
        logger.warning("Failed to load winning profile: %s", exc)
        _profile_cache = None

    return _profile_cache


async def _get_active_themes() -> list[TrendingTheme]:
    """Load trending themes from DB."""
    async with async_session() as session:
        result = await session.execute(
            select(TrendingTheme).order_by(TrendingTheme.velocity.desc()).limit(100)
        )
        return list(result.scalars().all())


async def _already_seen(ca: str) -> bool:
    """Check if this CA is already in scanner_candidates."""
    async with async_session() as session:
        result = await session.execute(
            select(ScannerCandidate.id).where(ScannerCandidate.ca == ca).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def _save_candidate(candidate: ScannerCandidate) -> None:
    """Persist a scanner candidate to DB."""
    async with async_session() as session:
        session.add(candidate)
        await session.commit()


async def _mark_alerted(ca: str) -> None:
    """Mark a candidate as alerted."""
    async with async_session() as session:
        result = await session.execute(
            select(ScannerCandidate).where(ScannerCandidate.ca == ca)
        )
        row = result.scalar_one_or_none()
        if row:
            row.alerted = True
            await session.commit()


async def scanner_loop() -> None:
    """Main loop: discover -> match -> score -> persist -> alert."""
    logger.info(
        "Scanner loop started (interval=%ds, chain=%s)",
        settings.scanner_poll_interval_seconds,
        settings.scanner_chain_filter,
    )

    while True:
        try:
            tokens = await discover_tokens()
            themes = await _get_active_themes()
            profile = await _get_cached_profile()

            new_count = 0
            alert_count = 0

            for token in tokens:
                ca = token["ca"]
                if await _already_seen(ca):
                    continue

                ticker = token.get("ticker", "")
                name = token.get("name", "")

                # Match against themes
                matched_names, nar_score = await match_token_to_themes(
                    name, ticker, themes,
                )

                # Depth score
                depth = compute_depth(
                    name, ticker, matched_names, themes,
                    platform=token.get("platform", "unknown"),
                )

                # Profile match
                token["_matched_themes"] = matched_names
                prof_score = compute_profile_match(token, profile)

                # Market quality
                mkt_score = compute_market_score(token)

                # Platform percentile (Phase 2)
                platform_score = 0.0
                token_platform = token.get("platform", "unknown")
                if token_platform in ("clanker", "virtuals", "flaunch"):
                    # Look up platform bonus from enrichment data
                    platform_bonus = 0.0
                    try:
                        from alpha_bot.platform_intel.models import PlatformToken as PT
                        async with async_session() as _sess:
                            _r = await _sess.execute(
                                select(PT.platform_bonus_score)
                                .where(PT.ca == ca.lower())
                                .limit(1)
                            )
                            _bonus = _r.scalar_one_or_none()
                            if _bonus is not None:
                                platform_bonus = _bonus
                    except Exception:
                        pass

                    try:
                        from alpha_bot.platform_intel.percentile_rank import (
                            compute_platform_percentile,
                        )
                        pct = await compute_platform_percentile(
                            ca, token_platform, token.get("mcap"),
                            None, token.get("volume_24h"),
                            token.get("pair_age_hours"),
                            platform_bonus=platform_bonus,
                        )
                        platform_score = pct.get("overall_percentile", 0.0)
                    except Exception as exc:
                        logger.debug("Platform percentile failed for %s: %s", ca[:12], exc)

                    # Ingest into platform_tokens if not already there
                    try:
                        from alpha_bot.platform_intel.platform_ingest import (
                            maybe_ingest_platform_token,
                        )
                        await maybe_ingest_platform_token(ca, token_platform, token)
                    except Exception as exc:
                        logger.debug("Platform ingest failed for %s: %s", ca[:12], exc)

                # Composite
                composite, tier = compute_composite(
                    nar_score, depth, prof_score, mkt_score,
                    token.get("discovery_source", ""),
                    platform_score=platform_score,
                )

                now = datetime.utcnow()
                candidate = ScannerCandidate(
                    ca=ca,
                    chain=token.get("chain", "base"),
                    ticker=ticker,
                    name=name,
                    platform=token.get("platform", "unknown"),
                    narrative_score=nar_score,
                    narrative_depth=depth,
                    profile_match_score=prof_score,
                    market_score=mkt_score,
                    platform_percentile=platform_score,
                    composite_score=composite,
                    matched_themes=json.dumps(matched_names),
                    price_usd=token.get("price_usd"),
                    mcap=token.get("mcap"),
                    liquidity_usd=token.get("liquidity_usd"),
                    volume_24h=token.get("volume_24h"),
                    pair_age_hours=token.get("pair_age_hours"),
                    discovery_source=token.get("discovery_source", ""),
                    alerted=False,
                    tier=tier,
                    discovered_at=now,
                    last_updated=now,
                )

                await _save_candidate(candidate)
                new_count += 1

                # Conviction signal registration
                try:
                    from alpha_bot.conviction.engine import register_signal, compute_scanner_weight
                    await register_signal(
                        ca=ca,
                        source="scanner",
                        weight=compute_scanner_weight(tier, composite),
                        metadata={
                            "tier": tier,
                            "composite_score": composite,
                            "ticker": ticker,
                            "chain": token.get("chain", "base"),
                        },
                    )
                except Exception:
                    pass

                # Fire alert for Tier 1
                if tier == 1:
                    await fire_scanner_alert(candidate)
                    await _mark_alerted(ca)
                    alert_count += 1

            if new_count > 0:
                logger.info(
                    "Scanner: %d new candidates, %d alerts fired",
                    new_count, alert_count,
                )

        except Exception:
            logger.exception("Scanner loop error")

        await asyncio.sleep(settings.scanner_poll_interval_seconds)
