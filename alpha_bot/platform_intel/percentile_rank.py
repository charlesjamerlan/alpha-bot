"""Rank a token against its platform cohort by age bucket.

Computes holder, mcap, and volume percentiles vs. all tracked tokens
on the same platform at a similar age.
"""

from __future__ import annotations

import bisect
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, and_

from alpha_bot.platform_intel.models import PlatformToken
from alpha_bot.storage.database import async_session

logger = logging.getLogger(__name__)

# Age buckets: (label, min_hours, max_hours)
_AGE_BUCKETS = [
    ("0-6h", 0, 6),
    ("6-24h", 6, 24),
    ("1d-7d", 24, 168),
    ("7d-30d", 168, 720),
    ("30d+", 720, 999_999),
]

# Percentile weights for overall score
_W_HOLDERS = 0.40
_W_MCAP = 0.35
_W_VOLUME = 0.25

# Minimum cohort size for meaningful percentile
_MIN_COHORT = 5


def _get_age_bucket(age_hours: float) -> tuple[str, float, float] | None:
    """Return the matching age bucket for a given age in hours."""
    for label, lo, hi in _AGE_BUCKETS:
        if lo <= age_hours < hi:
            return label, lo, hi
    return None


def _percentile_of(value: float, sorted_values: list[float]) -> float:
    """Compute the percentile rank of value in a sorted list (0-100)."""
    if not sorted_values:
        return 0.0
    pos = bisect.bisect_left(sorted_values, value)
    return round((pos / len(sorted_values)) * 100, 1)


async def compute_platform_percentile(
    ca: str,
    platform: str,
    current_mcap: float | None,
    current_holders: int | None,
    current_volume: float | None,
    pair_age_hours: float | None,
    platform_bonus: float = 0.0,
) -> dict:
    """Compare token against platform cohort in the same age bucket.

    Returns:
        {
            "holder_percentile": 89,
            "mcap_percentile": 75,
            "volume_percentile": 82,
            "overall_percentile": 82,
            "cohort_size": 1247,
            "age_bucket": "7d-30d",
        }
    Returns all zeros with cohort_size=0 if insufficient data.
    """
    empty = {
        "holder_percentile": 0.0,
        "mcap_percentile": 0.0,
        "volume_percentile": 0.0,
        "overall_percentile": 0.0,
        "cohort_size": 0,
        "age_bucket": "unknown",
    }

    if pair_age_hours is None or pair_age_hours < 0:
        return empty

    bucket = _get_age_bucket(pair_age_hours)
    if not bucket:
        return empty

    label, lo_hours, hi_hours = bucket
    now = datetime.utcnow()

    # Tokens in the same platform + age bucket
    deploy_lo = now - timedelta(hours=hi_hours)
    deploy_hi = now - timedelta(hours=lo_hours)

    async with async_session() as session:
        result = await session.execute(
            select(
                PlatformToken.current_mcap,
                PlatformToken.holders_7d,
                PlatformToken.holders_24h,
                PlatformToken.holders_1h,
                PlatformToken.volume_24h_at_peak,
            ).where(
                and_(
                    PlatformToken.platform == platform,
                    PlatformToken.deploy_timestamp.isnot(None),
                    PlatformToken.deploy_timestamp >= deploy_lo,
                    PlatformToken.deploy_timestamp <= deploy_hi,
                )
            )
        )
        rows = result.all()

    if len(rows) < _MIN_COHORT:
        return {**empty, "age_bucket": label, "cohort_size": len(rows)}

    # Collect cohort values
    mcaps: list[float] = []
    holders_list: list[float] = []
    volumes: list[float] = []

    for row in rows:
        if row.current_mcap is not None and row.current_mcap > 0:
            mcaps.append(row.current_mcap)
        # Use best available holder snapshot
        h = row.holders_7d or row.holders_24h or row.holders_1h
        if h is not None and h > 0:
            holders_list.append(float(h))
        if row.volume_24h_at_peak is not None and row.volume_24h_at_peak > 0:
            volumes.append(row.volume_24h_at_peak)

    mcaps.sort()
    holders_list.sort()
    volumes.sort()

    # Compute percentiles
    mcap_pct = _percentile_of(current_mcap, mcaps) if current_mcap else 0.0
    holder_pct = _percentile_of(float(current_holders), holders_list) if current_holders else 0.0
    volume_pct = _percentile_of(current_volume, volumes) if current_volume else 0.0

    overall = _W_HOLDERS * holder_pct + _W_MCAP * mcap_pct + _W_VOLUME * volume_pct

    # Apply platform-specific bonus/penalty (clamped to 0-100)
    if platform_bonus != 0.0:
        overall = max(0.0, min(100.0, overall + platform_bonus))

    overall = round(overall, 1)

    return {
        "holder_percentile": holder_pct,
        "mcap_percentile": mcap_pct,
        "volume_percentile": volume_pct,
        "overall_percentile": overall,
        "cohort_size": len(rows),
        "age_bucket": label,
    }
