"""Aggregate call_outcomes into channel_scores."""

import logging
import statistics
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from alpha_bot.tg_intel.models import CallOutcome, ChannelScore

logger = logging.getLogger(__name__)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return statistics.median(values)


def _mcap_range_label(mcap: float | None) -> str:
    if mcap is None:
        return "unknown"
    if mcap < 50_000:
        return "<50K"
    if mcap < 100_000:
        return "50K-100K"
    if mcap < 500_000:
        return "100K-500K"
    if mcap < 1_000_000:
        return "500K-1M"
    return ">1M"


def _best_mcap_range(outcomes: list[CallOutcome]) -> str:
    """Find mcap range with highest hit rate (min 3 samples)."""
    buckets: dict[str, list[bool]] = defaultdict(list)
    for o in outcomes:
        label = _mcap_range_label(o.mcap_at_mention)
        buckets[label].append(o.hit_2x)

    best_label = "unknown"
    best_rate = -1.0
    for label, hits in buckets.items():
        if len(hits) < 3:
            continue
        rate = sum(hits) / len(hits)
        if rate > best_rate:
            best_rate = rate
            best_label = label
    return best_label


def _best_platform(outcomes: list[CallOutcome]) -> str:
    """Most frequent platform among 2x+ winners."""
    winners = [o for o in outcomes if o.hit_2x]
    if not winners:
        return "unknown"
    counts: dict[str, int] = defaultdict(int)
    for o in winners:
        counts[o.platform] += 1
    return max(counts, key=counts.get)


def _median_time_to_peak(outcomes: list[CallOutcome]) -> str:
    """Median hours from mention to peak price."""
    deltas = []
    for o in outcomes:
        if o.peak_timestamp and o.mention_timestamp:
            dt = (o.peak_timestamp - o.mention_timestamp).total_seconds() / 3600
            if 0 < dt <= 168:  # within 7 days
                deltas.append(dt)
    if not deltas:
        return ""
    med = statistics.median(deltas)
    if med < 1:
        return f"{med * 60:.0f}m"
    return f"{med:.1f}h"


def _compute_first_mover_scores(
    by_channel: dict[str, list[CallOutcome]],
) -> dict[str, float]:
    """For each channel, compute how often it's the first to call a shared CA.

    Returns {channel_id: first_mover_score} where score is 0.0-1.0.
    """
    # Build ca -> list of (channel_id, timestamp)
    ca_mentions: dict[str, list[tuple[str, datetime]]] = defaultdict(list)
    for channel_id, outcomes in by_channel.items():
        for o in outcomes:
            ca_mentions[o.ca].append((channel_id, o.mention_timestamp))

    # Only consider CAs mentioned by 2+ channels
    channel_first: dict[str, int] = defaultdict(int)
    channel_shared: dict[str, int] = defaultdict(int)

    for ca, mentions in ca_mentions.items():
        distinct_channels = {m[0] for m in mentions}
        if len(distinct_channels) < 2:
            continue

        # Find which channel mentioned it first
        sorted_mentions = sorted(mentions, key=lambda m: m[1])
        first_channel = sorted_mentions[0][0]

        for cid in distinct_channels:
            channel_shared[cid] = channel_shared.get(cid, 0) + 1

        channel_first[first_channel] = channel_first.get(first_channel, 0) + 1

    scores: dict[str, float] = {}
    for channel_id in by_channel:
        shared = channel_shared.get(channel_id, 0)
        if shared == 0:
            scores[channel_id] = 0.0
        else:
            scores[channel_id] = channel_first.get(channel_id, 0) / shared

    return scores


async def compute_channel_scores(session: AsyncSession) -> list[ChannelScore]:
    """Compute quality scores for all channels from call_outcomes data.

    Returns list of ChannelScore objects (not yet saved).
    """
    result = await session.execute(select(CallOutcome))
    all_outcomes = list(result.scalars().all())

    if not all_outcomes:
        logger.info("No call outcomes to score")
        return []

    # Group by channel
    by_channel: dict[str, list[CallOutcome]] = defaultdict(list)
    for o in all_outcomes:
        by_channel[o.channel_id].append(o)

    first_mover_scores = _compute_first_mover_scores(by_channel)

    scores = []
    for channel_id, outcomes in by_channel.items():
        total = len(outcomes)
        resolved = [o for o in outcomes if o.price_check_status == "complete"]
        resolved_count = len(resolved)

        if resolved_count == 0:
            # Can still create a row with zero scores
            scores.append(ChannelScore(
                channel_id=channel_id,
                channel_name=outcomes[0].channel_name,
                total_calls=total,
                resolved_calls=0,
                quality_score=0.0,
                last_updated=datetime.utcnow(),
            ))
            continue

        # Hit rates
        hit_2x_count = sum(1 for o in resolved if o.hit_2x)
        hit_5x_count = sum(1 for o in resolved if o.hit_5x)
        hit_rate_2x = hit_2x_count / resolved_count
        hit_rate_5x = hit_5x_count / resolved_count

        # ROI stats
        rois_24h = [o.roi_24h for o in resolved if o.roi_24h is not None]
        rois_peak = [o.roi_peak for o in resolved if o.roi_peak is not None]

        avg_roi_24h = statistics.mean(rois_24h) if rois_24h else 0.0
        avg_roi_peak = statistics.mean(rois_peak) if rois_peak else 0.0
        median_roi_24h = _median(rois_24h)

        # Quality score: 0-100
        # 40% hit_rate_2x + 20% hit_rate_5x (scaled) + 20% avg_roi (capped) + 20% consistency
        hr_2x_component = hit_rate_2x * 100.0  # 0-100
        hr_5x_component = min(hit_rate_5x * 200.0, 100.0)  # 5x hit rate doubled, capped
        roi_component = min(avg_roi_peak / 5.0, 100.0) if avg_roi_peak > 0 else 0.0

        # Consistency: lower variance = higher score
        if len(rois_peak) >= 3:
            stdev = statistics.stdev(rois_peak)
            mean_abs = abs(avg_roi_peak) if avg_roi_peak != 0 else 1.0
            cv = stdev / mean_abs if mean_abs > 0 else 0
            consistency = max(0, 100.0 - cv * 20)  # lower CV = higher score
        else:
            consistency = 50.0  # not enough data

        first_mover = first_mover_scores.get(channel_id, 0.0)

        quality = (
            0.35 * hr_2x_component
            + 0.20 * hr_5x_component
            + 0.15 * roi_component
            + 0.20 * consistency
            + 0.10 * first_mover * 100
        )
        quality = min(max(quality, 0.0), 100.0)

        scores.append(ChannelScore(
            channel_id=channel_id,
            channel_name=outcomes[0].channel_name,
            total_calls=total,
            resolved_calls=resolved_count,
            hit_rate_2x=round(hit_rate_2x, 4),
            hit_rate_5x=round(hit_rate_5x, 4),
            avg_roi_24h=round(avg_roi_24h, 2),
            avg_roi_peak=round(avg_roi_peak, 2),
            median_roi_24h=round(median_roi_24h, 2),
            median_time_to_peak=_median_time_to_peak(outcomes),
            best_platform=_best_platform(outcomes),
            best_mcap_range=_best_mcap_range(outcomes),
            first_mover_score=round(first_mover, 4),
            quality_score=round(quality, 1),
            last_updated=datetime.utcnow(),
        ))

    scores.sort(key=lambda s: s.quality_score, reverse=True)
    return scores


async def save_channel_scores(
    session: AsyncSession, scores: list[ChannelScore]
) -> None:
    """Delete existing channel_scores and replace with new ones."""
    await session.execute(delete(ChannelScore))
    for s in scores:
        session.add(s)
    await session.commit()
    logger.info("Saved %d channel scores", len(scores))
