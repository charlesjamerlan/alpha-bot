"""Composite candidate scorer — combines narrative, profile match, and market quality."""

from __future__ import annotations

import logging
import time

from alpha_bot.config import settings

logger = logging.getLogger(__name__)

# Default weights (used as fallback if DB has no active weights)
_W_NARRATIVE = 0.25
_W_PROFILE = 0.20
_W_PLATFORM = 0.15
_W_MARKET = 0.15
_W_DEPTH = 0.15
_W_SOURCE = 0.10

# Sync cache for weights (loaded from async get_active_weights periodically)
_cached_weights: dict | None = None
_cached_weights_ts: float = 0.0
_WEIGHTS_CACHE_TTL = 300  # 5 minutes


def _get_weights() -> dict:
    """Get current weights from cache or return defaults.

    The cache is populated by refresh_weights_cache() which should be called
    periodically from async code.
    """
    global _cached_weights, _cached_weights_ts
    if _cached_weights and (time.time() - _cached_weights_ts) < _WEIGHTS_CACHE_TTL:
        return _cached_weights
    return {
        "narrative": _W_NARRATIVE,
        "profile": _W_PROFILE,
        "platform": _W_PLATFORM,
        "market": _W_MARKET,
        "depth": _W_DEPTH,
        "source": _W_SOURCE,
    }


async def refresh_weights_cache() -> None:
    """Async refresh of the weights cache from DB."""
    global _cached_weights, _cached_weights_ts
    try:
        from alpha_bot.scoring_engine.recalibrate import get_active_weights
        _cached_weights = await get_active_weights()
        _cached_weights_ts = time.time()
    except Exception:
        logger.debug("Could not refresh weights from DB, using defaults")

# Discovery source bonus scores (0-100)
_SOURCE_BONUS = {
    "boosted": 80,
    "profile": 60,
    "new_pairs": 40,
    "tg": 70,
    "realtime_deploy": 60,
}


def compute_profile_match(token: dict, profile: dict | None) -> float:
    """Score how well this token matches the winning call profile (0-100)."""
    if not profile:
        return 0.0

    score = 0.0

    # MCap in range (+40)
    mcap_range = profile.get("mcap_range")
    mcap = token.get("mcap")
    if mcap and mcap_range and len(mcap_range) == 2:
        if mcap_range[0] <= mcap <= mcap_range[1]:
            score += 40.0
        elif mcap_range[0] * 0.5 <= mcap <= mcap_range[1] * 1.5:
            score += 20.0  # close but not exact

    # Platform match (+25)
    top_platforms = profile.get("top_platforms", [])
    if token.get("platform") in top_platforms:
        score += 25.0

    # Narrative overlap (+20)
    top_narratives = [n.lower() for n in profile.get("top_narratives", [])]
    matched = [m.lower() for m in token.get("_matched_themes", [])]
    if any(m in top_narratives for m in matched):
        score += 20.0

    # Vol/MCap ratio (+15)
    vol = token.get("volume_24h") or 0
    mcap_val = token.get("mcap") or 0
    if mcap_val > 0:
        vol_mcap = vol / mcap_val
        if vol_mcap >= 0.3:
            score += 15.0
        elif vol_mcap >= 0.15:
            score += 8.0

    return min(score, 100.0)


def compute_market_score(token: dict) -> float:
    """Score market quality: vol/mcap, liquidity depth, age sweet spot (0-100)."""
    score = 0.0

    vol = token.get("volume_24h") or 0
    mcap = token.get("mcap") or 0
    liq = token.get("liquidity_usd") or 0
    age_hours = token.get("pair_age_hours")

    # Vol/MCap ratio (0-40)
    if mcap > 0:
        ratio = vol / mcap
        score += min(ratio * 80, 40)

    # Liquidity depth (0-30)
    if liq >= 100_000:
        score += 30
    elif liq >= 50_000:
        score += 25
    elif liq >= 20_000:
        score += 15
    elif liq >= settings.scanner_min_liquidity:
        score += 8

    # Age sweet spot: 12h-168h is ideal (0-30)
    if age_hours is not None:
        if 12 <= age_hours <= 168:
            score += 30
        elif 6 <= age_hours < 12 or 168 < age_hours <= 336:
            score += 15
        elif age_hours < 6:
            score += 5  # very new, risky

    return min(score, 100.0)


def compute_composite(
    narrative_score: float,
    depth_score: float,
    profile_match_score: float,
    market_score: float,
    discovery_source: str,
    platform_score: float = 0.0,
) -> tuple[float, int]:
    """Compute weighted composite score and tier.

    Args:
        platform_score: Platform percentile (0-100). Defaults to 0 if no cohort data.

    Returns (composite_score, tier) where tier is 1, 2, or 3.
    """
    w = _get_weights()
    source_score = float(_SOURCE_BONUS.get(discovery_source, 40))

    # Build {signal_name: (weight, score)} for active signals
    signals = {
        "narrative": (w["narrative"], narrative_score),
        "depth": (w["depth"], depth_score),
        "profile": (w["profile"], profile_match_score),
        "platform": (w["platform"], platform_score),
        "market": (w["market"], market_score),
        "source": (w["source"], source_score),
    }

    # Redistribute weight from unavailable signals (score == 0 AND data
    # genuinely missing — not just a low-scoring token).  Profile is
    # unavailable when no winning profile has been built yet; platform is
    # unavailable when there's no cohort data for the token.
    unavailable_weight = 0.0
    available_weight = 0.0
    for name, (wt, sc) in signals.items():
        if name in ("profile", "platform") and sc == 0.0:
            unavailable_weight += wt
        else:
            available_weight += wt

    # Compute composite, scaling available signals up if some are unavailable
    if unavailable_weight > 0 and available_weight > 0:
        scale = 1.0 / available_weight  # normalise so available weights sum to 1
        composite = sum(
            wt * scale * sc
            for name, (wt, sc) in signals.items()
            if not (name in ("profile", "platform") and sc == 0.0)
        )
    else:
        composite = sum(wt * sc for wt, sc in signals.values())

    composite = round(min(composite, 100.0), 1)

    if composite >= settings.scanner_tier1_threshold:
        tier = 1
    elif composite >= settings.scanner_tier2_threshold:
        tier = 2
    else:
        tier = 3

    return composite, tier
