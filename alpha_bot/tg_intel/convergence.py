"""Cross-channel convergence detection.

When 2+ independent alpha callers mention the same CA within a configurable
window, that's a high-confidence signal.  This module keeps an in-memory
tracker and fires TG alerts weighted by channel quality scores.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine

from alpha_bot.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# ca -> list of {channel_id, channel_name, ticker, chain, timestamp}
_recent_calls: dict[str, list[dict[str, Any]]] = {}

# CAs already alerted (prevent repeat alerts within window)
# ca -> {alerted_at, details}
_alerted_cas: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Notification callback (same pattern as position_manager.py)
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
            logger.warning("Convergence notify failed: %s", exc)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _prune_old_entries() -> None:
    """Remove entries older than the convergence window."""
    cutoff = datetime.utcnow() - timedelta(hours=settings.convergence_window_hours)

    # Prune recent calls
    for ca in list(_recent_calls):
        _recent_calls[ca] = [
            c for c in _recent_calls[ca] if c["timestamp"] >= cutoff
        ]
        if not _recent_calls[ca]:
            del _recent_calls[ca]

    # Prune alerted CAs
    for ca in list(_alerted_cas):
        if _alerted_cas[ca]["alerted_at"] < cutoff:
            del _alerted_cas[ca]


async def _load_channel_quality(channel_ids: list[str]) -> dict[str, float]:
    """Load quality_score for channels from DB. Returns {channel_id: score}."""
    from sqlalchemy import select
    from alpha_bot.storage.database import async_session
    from alpha_bot.tg_intel.models import ChannelScore

    scores: dict[str, float] = {}
    try:
        async with async_session() as session:
            result = await session.execute(
                select(ChannelScore.channel_id, ChannelScore.quality_score).where(
                    ChannelScore.channel_id.in_(channel_ids)
                )
            )
            for row in result:
                scores[row.channel_id] = row.quality_score
    except Exception as exc:
        logger.warning("Failed to load channel scores: %s", exc)

    return scores


async def check_convergence(
    ca: str,
    channel_id: str,
    channel_name: str = "",
    ticker: str = "",
    chain: str = "",
    mention_timestamp: datetime | None = None,
) -> None:
    """Record a CA call and fire alert if convergence threshold is met."""
    if mention_timestamp is None:
        mention_timestamp = datetime.utcnow()

    _prune_old_entries()

    # Add this call
    entry = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "ticker": ticker,
        "chain": chain,
        "timestamp": mention_timestamp,
    }
    _recent_calls.setdefault(ca, []).append(entry)

    # Count distinct channels for this CA
    distinct_channels = {c["channel_id"] for c in _recent_calls[ca]}
    if len(distinct_channels) < settings.convergence_min_channels:
        return

    # Already alerted for this CA within the window?
    if ca in _alerted_cas:
        return

    # Load channel quality scores
    channel_ids = list(distinct_channels)
    quality_scores = await _load_channel_quality(channel_ids)

    # Default score for channels without a quality_score yet
    DEFAULT_SCORE = 50.0

    calls = _recent_calls[ca]
    channel_lines: list[str] = []
    total_quality = 0.0
    for c in calls:
        cid = c["channel_id"]
        if cid not in distinct_channels:
            continue
        # Only include first mention per channel
        distinct_channels.discard(cid)
        q = quality_scores.get(cid, DEFAULT_SCORE)
        total_quality += q
        ago = datetime.utcnow() - c["timestamp"]
        ago_min = max(int(ago.total_seconds() / 60), 0)
        channel_lines.append(
            f"📡 {c['channel_name'] or cid} ({q:.0f}/100) — {ago_min} min ago"
        )

    count = len(channel_lines)
    confidence = total_quality / (count * 100) if count else 0.0

    # Determine display ticker
    display_ticker = ticker
    if not display_ticker:
        for c in calls:
            if c.get("ticker"):
                display_ticker = c["ticker"]
                break

    # Build alert
    ca_short = f"{ca[:6]}...{ca[-4:]}" if len(ca) > 12 else ca
    alert_chain = chain or calls[0].get("chain", "?")

    text = (
        f"🔀 <b>CONVERGENCE: ${display_ticker or '?'}</b>\n\n"
        f"CA: <code>{ca_short}</code>\n"
        f"Chain: {alert_chain} | Confidence: {confidence:.2f}\n\n"
        + "\n".join(channel_lines)
        + f"\n\n<code>{ca}</code>"
    )

    # Mark as alerted
    _alerted_cas[ca] = {
        "alerted_at": datetime.utcnow(),
        "ticker": display_ticker,
        "chain": alert_chain,
        "confidence": confidence,
        "channels": count,
    }

    logger.info(
        "Convergence detected: %s (%s) — %d channels, confidence=%.2f",
        display_ticker or ca[:12], alert_chain, count, confidence,
    )
    await _notify(text)


# ---------------------------------------------------------------------------
# Query for /convergence bot command
# ---------------------------------------------------------------------------


def get_recent_convergences() -> list[dict[str, Any]]:
    """Return recent convergence signals for display."""
    _prune_old_entries()
    results = []
    for ca, info in _alerted_cas.items():
        results.append({
            "ca": ca,
            "ticker": info.get("ticker", ""),
            "chain": info.get("chain", ""),
            "confidence": info.get("confidence", 0.0),
            "channels": info.get("channels", 0),
            "alerted_at": info.get("alerted_at"),
        })
    # Most recent first
    results.sort(key=lambda r: r.get("alerted_at") or datetime.min, reverse=True)
    return results
