"""Reaction velocity analysis for TG call outcomes.

Tracks per-channel baseline engagement and computes a velocity multiplier
for each call. When a message gets 3x+ the channel's baseline reactions,
fires a high-engagement alert.
"""

from __future__ import annotations

import logging
import statistics
from typing import Any, Callable, Coroutine

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# channel_id -> recent reaction counts (capped at 200)
_channel_baselines: dict[str, list[int]] = {}

# Max entries per channel for baseline calculation
_MAX_BASELINE_ENTRIES = 200

# Minimum multiplier to fire a high-engagement alert
_ALERT_THRESHOLD = 3.0

# ---------------------------------------------------------------------------
# Notification callback
# ---------------------------------------------------------------------------

_notify_fn: Callable[[str, str], Coroutine] | None = None


def set_notify_fn(fn: Callable[[str, str], Coroutine]) -> None:
    global _notify_fn
    _notify_fn = fn


async def _notify(text: str) -> None:
    if _notify_fn:
        try:
            await _notify_fn(text, "HTML")
        except Exception as exc:
            logger.warning("Reaction velocity notify failed: %s", exc)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


async def track_reaction(
    channel_id: str,
    channel_name: str,
    ca: str,
    ticker: str,
    reaction_count: int,
    outcome_id: int,
) -> float:
    """Track a reaction count, compute velocity multiplier, fire alert if high.

    Returns the velocity_multiplier (1.0 = baseline, 3.0+ = high engagement).
    """
    if reaction_count <= 0:
        return 1.0

    # Append to baseline
    baseline_list = _channel_baselines.setdefault(channel_id, [])
    baseline_list.append(reaction_count)
    if len(baseline_list) > _MAX_BASELINE_ENTRIES:
        _channel_baselines[channel_id] = baseline_list[-_MAX_BASELINE_ENTRIES:]

    # Compute velocity multiplier
    baseline = statistics.mean(baseline_list) if baseline_list else 0
    if baseline > 0:
        velocity_multiplier = reaction_count / baseline
    else:
        velocity_multiplier = 1.0

    # Update CallOutcome.reaction_velocity in DB
    try:
        from alpha_bot.storage.database import async_session
        from alpha_bot.tg_intel.models import CallOutcome

        async with async_session() as session:
            result = await session.execute(
                select(CallOutcome).where(CallOutcome.id == outcome_id)
            )
            outcome = result.scalar_one_or_none()
            if outcome:
                outcome.reaction_velocity = round(velocity_multiplier, 2)
                await session.commit()
    except Exception as exc:
        logger.warning("Failed to update reaction_velocity for outcome #%d: %s", outcome_id, exc)

    # Fire alert if high engagement
    if velocity_multiplier >= _ALERT_THRESHOLD:
        ca_short = f"{ca[:6]}...{ca[-4:]}" if len(ca) > 12 else ca
        ticker_display = f"${ticker}" if ticker else ca_short

        text = (
            f"<b>HIGH ENGAGEMENT: {ticker_display}</b>\n\n"
            f"Reactions: {reaction_count} ({velocity_multiplier:.1f}x baseline)\n"
            f"Channel: {channel_name or channel_id}\n"
        )
        if ca:
            text += f"CA: <code>{ca}</code>"

        logger.info(
            "High engagement: %s — %d reactions (%.1fx baseline) in %s",
            ticker or ca[:12], reaction_count, velocity_multiplier, channel_name or channel_id,
        )
        await _notify(text)

    return velocity_multiplier


async def load_baselines_from_db() -> None:
    """Seed per-channel baselines from recent CallOutcome rows.

    Called once at startup to avoid cold-start problem.
    """
    from datetime import datetime, timedelta
    from alpha_bot.storage.database import async_session
    from alpha_bot.tg_intel.models import CallOutcome

    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        async with async_session() as session:
            result = await session.execute(
                select(
                    CallOutcome.channel_id,
                    CallOutcome.reaction_count,
                ).where(
                    and_(
                        CallOutcome.mention_timestamp >= cutoff,
                        CallOutcome.reaction_count.isnot(None),
                        CallOutcome.reaction_count > 0,
                    )
                ).order_by(CallOutcome.mention_timestamp.asc())
            )
            rows = result.all()

        loaded = 0
        for channel_id, reaction_count in rows:
            baseline_list = _channel_baselines.setdefault(channel_id, [])
            baseline_list.append(reaction_count)
            if len(baseline_list) > _MAX_BASELINE_ENTRIES:
                _channel_baselines[channel_id] = baseline_list[-_MAX_BASELINE_ENTRIES:]
            loaded += 1

        channel_count = len(_channel_baselines)
        logger.info(
            "Loaded reaction baselines: %d data points across %d channels",
            loaded, channel_count,
        )
    except Exception:
        logger.exception("Failed to load reaction baselines from DB")


def get_channel_baseline(channel_id: str) -> float:
    """Return the current baseline reaction count for a channel."""
    baseline_list = _channel_baselines.get(channel_id, [])
    if not baseline_list:
        return 0.0
    return statistics.mean(baseline_list)
