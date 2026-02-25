"""Caller pattern extraction — bridge to autonomous discovery (Mode 2).

Analyzes winning calls from high-performing channels to produce a statistical
"winning call profile": the fingerprint of what a good call looks like at
the moment it's made.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from datetime import datetime

from sqlalchemy import select, and_

from alpha_bot.storage.database import async_session
from alpha_bot.tg_intel.models import CallOutcome, ChannelScore

logger = logging.getLogger(__name__)


async def extract_winning_profile(
    min_hit_rate: float = 0.3,
    min_sample: int = 5,
) -> dict | None:
    """Extract the statistical winning call profile.

    Filters to 2x+ winning calls from channels with hit_rate_2x >= min_hit_rate.
    Returns a dict with the profile, or None if insufficient data.
    """
    async with async_session() as session:
        # Load qualifying channels
        ch_result = await session.execute(
            select(ChannelScore).where(
                ChannelScore.hit_rate_2x >= min_hit_rate
            )
        )
        qualifying_channels = list(ch_result.scalars().all())

        if not qualifying_channels:
            logger.info("No channels meet min_hit_rate=%.2f", min_hit_rate)
            return None

        channel_ids = [ch.channel_id for ch in qualifying_channels]

        # Load winning calls from those channels
        co_result = await session.execute(
            select(CallOutcome).where(
                and_(
                    CallOutcome.channel_id.in_(channel_ids),
                    CallOutcome.hit_2x == True,  # noqa: E712
                )
            )
        )
        winners = list(co_result.scalars().all())

    if len(winners) < min_sample:
        logger.info(
            "Only %d winning calls (need %d) — profile not generated",
            len(winners), min_sample,
        )
        return None

    # --- Compute profile stats ---

    # Market cap at mention
    mcaps = [w.mcap_at_mention for w in winners if w.mcap_at_mention and w.mcap_at_mention > 0]
    median_mcap = statistics.median(mcaps) if mcaps else None
    mcap_range = _percentile_range(mcaps, 10, 90) if len(mcaps) >= 3 else None

    # Platforms
    platforms = [w.platform for w in winners if w.platform and w.platform != "unknown"]
    top_platforms = [p for p, _ in Counter(platforms).most_common(3)]

    # Narrative tags
    all_tags: list[str] = []
    for w in winners:
        try:
            tags = json.loads(w.narrative_tags) if w.narrative_tags else []
            all_tags.extend(tags)
        except (json.JSONDecodeError, TypeError):
            pass
    top_narratives = [t for t, _ in Counter(all_tags).most_common(5)] if all_tags else []

    # Reaction velocity
    velocities = [w.reaction_velocity for w in winners if w.reaction_velocity is not None]
    avg_velocity = statistics.mean(velocities) if velocities else None

    # ROI stats
    rois_peak = [w.roi_peak for w in winners if w.roi_peak is not None]
    avg_roi = statistics.mean(rois_peak) if rois_peak else None
    median_roi = statistics.median(rois_peak) if rois_peak else None

    # Confidence based on sample size
    n = len(winners)
    if n >= 30:
        confidence = "high"
    elif n >= 15:
        confidence = "medium"
    else:
        confidence = "low"

    profile = {
        "median_mcap_at_call": round(median_mcap) if median_mcap else None,
        "mcap_range": [round(v) for v in mcap_range] if mcap_range else None,
        "top_platforms": top_platforms,
        "top_narratives": top_narratives,
        "avg_reaction_velocity": round(avg_velocity, 2) if avg_velocity else None,
        "avg_roi_peak": round(avg_roi, 1) if avg_roi else None,
        "median_roi_peak": round(median_roi, 1) if median_roi else None,
        "sample_size": n,
        "channel_count": len(channel_ids),
        "confidence": confidence,
        "generated_at": datetime.utcnow().isoformat(),
    }

    logger.info(
        "Winning profile: %d samples from %d channels (confidence=%s)",
        n, len(channel_ids), confidence,
    )
    return profile


def format_profile_text(profile: dict) -> str:
    """Format a winning profile dict as Telegram HTML."""
    if not profile:
        return "No winning profile available yet. Need more resolved call data."

    lines = [
        "<b>Winning Call Profile</b>",
        f"Confidence: <b>{profile.get('confidence', '?').upper()}</b> "
        f"({profile.get('sample_size', 0)} winning calls from "
        f"{profile.get('channel_count', 0)} channels)\n",
    ]

    mcap = profile.get("median_mcap_at_call")
    if mcap:
        lines.append(f"Median MCap at call: <b>${mcap:,.0f}</b>")
    mcap_range = profile.get("mcap_range")
    if mcap_range:
        lines.append(f"MCap range (p10-p90): ${mcap_range[0]:,.0f} — ${mcap_range[1]:,.0f}")

    platforms = profile.get("top_platforms")
    if platforms:
        lines.append(f"Top platforms: <b>{', '.join(platforms)}</b>")

    narratives = profile.get("top_narratives")
    if narratives:
        lines.append(f"Top narratives: {', '.join(narratives)}")

    velocity = profile.get("avg_reaction_velocity")
    if velocity:
        lines.append(f"Avg reaction velocity: {velocity:.1f}x")

    avg_roi = profile.get("avg_roi_peak")
    median_roi = profile.get("median_roi_peak")
    if avg_roi:
        lines.append(f"Avg peak ROI: <b>{avg_roi:+.0f}%</b>")
    if median_roi:
        lines.append(f"Median peak ROI: {median_roi:+.0f}%")

    lines.append(f"\nGenerated: {profile.get('generated_at', '?')}")

    return "\n".join(lines)


def _percentile_range(
    values: list[float], low_pct: int, high_pct: int
) -> tuple[float, float] | None:
    """Return (low_percentile, high_percentile) from a sorted list."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    lo_idx = max(0, int(n * low_pct / 100))
    hi_idx = min(n - 1, int(n * high_pct / 100))
    return (s[lo_idx], s[hi_idx])
