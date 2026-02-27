"""Conviction engine — detects when 2+ independent signal sources flag the same CA.

Push-based: each signal source calls register_signal() after its own processing.
The engine checks in-memory whether other sources already flagged that CA within
the conviction window, and fires a high-conviction alert when the score threshold
is met.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine

from alpha_bot.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SignalEvent:
    source: str       # "tg_mention", "scanner", "clanker_realtime", "wallet_buy"
    weight: float     # 0-35, computed by weight functions
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: dict = field(default_factory=dict)


# ca -> list of SignalEvents within the window
_signal_buffer: dict[str, list[SignalEvent]] = {}

# ca -> {alerted_at, conviction_score, ...}  (cooldown tracking)
_alerted_cas: dict[str, dict[str, Any]] = {}

# Notification callback
_notify_fn: Callable[[str, str], Coroutine] | None = None


def set_notify_fn(fn: Callable[[str, str], Coroutine]) -> None:
    global _notify_fn
    _notify_fn = fn


async def _notify(text: str) -> None:
    if _notify_fn:
        try:
            await _notify_fn(text, "HTML")
        except Exception as exc:
            logger.warning("Conviction notify failed: %s", exc)


# ---------------------------------------------------------------------------
# Weight functions — compute how strong each signal type is
# ---------------------------------------------------------------------------


def compute_tg_weight(channel_quality: float) -> float:
    """TG mention weight: 10-35 based on channel quality (0-100)."""
    return 10.0 + (channel_quality / 100.0) * 25.0


def compute_scanner_weight(tier: int, composite: float) -> float:
    """Scanner weight: 10-30 based on tier and composite score."""
    if tier == 1:
        return 30.0
    if tier == 3:
        return 10.0
    # Tier 2: scale between 15-25 based on composite
    return 15.0 + min((composite / 100.0) * 10.0, 10.0)


def compute_wallet_weight(quality_score: float) -> float:
    """Wallet buy weight: 5-35 based on wallet quality (0-100)."""
    return 5.0 + (quality_score / 100.0) * 30.0


def compute_clanker_weight(tier: int, composite: float) -> float:
    """Clanker realtime weight: 5-20 based on tier and composite."""
    if tier == 1:
        return 20.0
    if tier == 3:
        return 5.0
    # Tier 2: scale 8-15
    return 8.0 + min((composite / 100.0) * 7.0, 7.0)


def compute_x_weight(signal_type: str, followers: int | None) -> float:
    """X/Twitter signal weight: 5-35 based on signal type and KOL reach."""
    base = {
        "kol_mention": 15.0,
        "exit_signal": 20.0,
        "narrative": 10.0,
        "cashtag": 8.0,
    }.get(signal_type, 8.0)

    # Scale up to +10 based on follower count (cap at 500K)
    f = followers or 0
    follower_bonus = min(f / 500_000 * 10.0, 10.0)

    return min(base + follower_bonus, 35.0)


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------


def _prune_buffers() -> None:
    """Remove entries older than conviction window from both buffers."""
    cutoff = datetime.utcnow() - timedelta(minutes=settings.conviction_window_minutes)

    for ca in list(_signal_buffer):
        _signal_buffer[ca] = [e for e in _signal_buffer[ca] if e.timestamp >= cutoff]
        if not _signal_buffer[ca]:
            del _signal_buffer[ca]

    cooldown_cutoff = datetime.utcnow() - timedelta(minutes=settings.conviction_cooldown_minutes)
    for ca in list(_alerted_cas):
        if _alerted_cas[ca]["alerted_at"] < cooldown_cutoff:
            del _alerted_cas[ca]


def _compute_conviction_score(events: list[SignalEvent]) -> tuple[float, dict[str, SignalEvent]]:
    """Compute conviction score from a list of signal events.

    Returns (score, best_per_source) where best_per_source maps source name
    to the highest-weight event for that source.
    """
    # Group by source, take best weight per source
    best_per_source: dict[str, SignalEvent] = {}
    for event in events:
        existing = best_per_source.get(event.source)
        if existing is None or event.weight > existing.weight:
            best_per_source[event.source] = event

    n_sources = len(best_per_source)
    if n_sources < 2:
        return 0.0, best_per_source

    # Sum best weights + diversity bonus (15 per additional source beyond first)
    weight_sum = sum(e.weight for e in best_per_source.values())
    diversity_bonus = (n_sources - 1) * 15.0
    score = min(weight_sum + diversity_bonus, 100.0)

    return score, best_per_source


async def register_signal(
    ca: str,
    source: str,
    weight: float,
    metadata: dict | None = None,
) -> None:
    """Register a signal from a source. Checks for conviction and fires alert."""
    if not settings.conviction_enabled:
        return

    ca = ca.strip().lower()
    if not ca:
        return

    _prune_buffers()

    event = SignalEvent(
        source=source,
        weight=weight,
        timestamp=datetime.utcnow(),
        metadata=metadata or {},
    )
    _signal_buffer.setdefault(ca, []).append(event)

    logger.debug(
        "Conviction signal: %s from %s (weight=%.1f)", ca[:12], source, weight,
    )

    # Check conviction score
    events = _signal_buffer[ca]
    score, best_per_source = _compute_conviction_score(events)

    if score < settings.conviction_min_score:
        return

    # Already alerted within cooldown?
    if ca in _alerted_cas:
        return

    n_sources = len(best_per_source)

    # Resolve ticker and chain from metadata
    ticker = ""
    chain = "base"
    for evt in best_per_source.values():
        if not ticker and evt.metadata.get("ticker"):
            ticker = evt.metadata["ticker"]
        if evt.metadata.get("chain"):
            chain = evt.metadata["chain"]

    # Fetch current price for outcome tracking
    price_at_alert = None
    try:
        import httpx
        from alpha_bot.research.dexscreener import extract_pair_details, get_token_by_address

        async with httpx.AsyncClient(timeout=10) as client:
            pair = await get_token_by_address(ca, client)
            if pair:
                details = extract_pair_details(pair)
                price_at_alert = details.get("price_usd")
                if not ticker:
                    ticker = details.get("symbol", "")
    except Exception:
        pass

    # Build source lines for alert
    source_lines = []
    for src, evt in sorted(best_per_source.items(), key=lambda x: x[1].timestamp):
        ago = int((datetime.utcnow() - evt.timestamp).total_seconds() / 60)
        ago_str = f"{ago}m ago" if ago > 0 else "just now"

        if src == "tg_mention":
            ch_name = evt.metadata.get("channel_name", "?")
            q = evt.metadata.get("channel_quality", 0)
            source_lines.append(f"  TG: {ch_name} (Q: {q:.0f}) -- {ago_str}")
        elif src == "scanner":
            tier = evt.metadata.get("tier", "?")
            comp = evt.metadata.get("composite_score", 0)
            source_lines.append(f"  Scanner: Tier {tier} (score: {comp:.0f}) -- {ago_str}")
        elif src == "wallet_buy":
            addr = evt.metadata.get("wallet_address", "?")
            q = evt.metadata.get("wallet_quality", 0)
            addr_short = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 12 else addr
            source_lines.append(f"  Wallet: {addr_short} (Q: {q:.0f}) -- {ago_str}")
        elif src == "clanker_realtime":
            tier = evt.metadata.get("tier", "?")
            comp = evt.metadata.get("composite_score", 0)
            source_lines.append(f"  Clanker: Tier {tier} (score: {comp:.0f}) -- {ago_str}")
        elif src == "x_kol":
            author = evt.metadata.get("author", "?")
            followers = evt.metadata.get("followers", 0)
            sig_type = evt.metadata.get("signal_type", "mention")
            f_str = f"{followers // 1000}K" if followers and followers >= 1000 else str(followers or "?")
            source_lines.append(f"  X: @{author} ({f_str} followers, {sig_type}) -- {ago_str}")
        else:
            source_lines.append(f"  {src}: weight {evt.weight:.0f} -- {ago_str}")

    sources_text = "\n".join(source_lines)

    alert_text = (
        f"============================\n"
        f"CONVICTION: ${ticker or '?'}\n"
        f"============================\n\n"
        f"Score: {score:.0f}/100 | {n_sources} independent sources\n"
        f"Chain: {chain}\n\n"
        f"Signal Sources:\n"
        f"{sources_text}\n\n"
        f"<code>{ca}</code>"
    )

    # Mark as alerted (cooldown)
    _alerted_cas[ca] = {
        "alerted_at": datetime.utcnow(),
        "conviction_score": score,
        "ticker": ticker,
        "chain": chain,
        "distinct_sources": n_sources,
        "sources": {src: {"weight": evt.weight, **evt.metadata} for src, evt in best_per_source.items()},
    }

    logger.info(
        "CONVICTION ALERT: %s (%s) — score=%.0f, sources=%d",
        ticker or ca[:12], chain, score, n_sources,
    )
    await _notify(alert_text)

    # Persist to DB
    try:
        from alpha_bot.conviction.models import ConvictionAlert
        from alpha_bot.storage.database import async_session

        sources_data = []
        for src, evt in best_per_source.items():
            sources_data.append({
                "source": src,
                "weight": evt.weight,
                "timestamp": evt.timestamp.isoformat(),
                **evt.metadata,
            })

        alert_row = ConvictionAlert(
            ca=ca,
            chain=chain,
            ticker=ticker,
            conviction_score=score,
            distinct_sources=n_sources,
            sources_json=json.dumps(sources_data),
            price_at_alert=price_at_alert,
        )
        async with async_session() as session:
            session.add(alert_row)
            await session.commit()
            await session.refresh(alert_row)

        # Schedule delayed price checks for outcome tracking
        if price_at_alert and price_at_alert > 0:
            asyncio.create_task(_delayed_price_check(alert_row.id, ca, price_at_alert, 3600, "price_1h", "roi_1h"))
            asyncio.create_task(_delayed_price_check(alert_row.id, ca, price_at_alert, 86400, "price_24h", "roi_24h"))

    except Exception:
        logger.exception("Failed to persist conviction alert")


async def _delayed_price_check(
    alert_id: int, ca: str, price_at_alert: float,
    delay_seconds: int, price_field: str, roi_field: str,
) -> None:
    """Wait then fetch price and update the conviction alert."""
    await asyncio.sleep(delay_seconds)
    try:
        import httpx
        from alpha_bot.research.dexscreener import extract_pair_details, get_token_by_address

        async with httpx.AsyncClient(timeout=15) as client:
            pair = await get_token_by_address(ca, client)
            if not pair:
                return
            details = extract_pair_details(pair)
            price = details.get("price_usd")
            if price is None:
                return

        roi = ((price - price_at_alert) / price_at_alert) * 100.0

        from alpha_bot.conviction.models import ConvictionAlert
        from alpha_bot.storage.database import async_session
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(
                select(ConvictionAlert).where(ConvictionAlert.id == alert_id)
            )
            row = result.scalar_one_or_none()
            if not row:
                return
            setattr(row, price_field, price)
            setattr(row, roi_field, roi)
            await session.commit()

        logger.debug(
            "Conviction %s for #%d: price=%.10f, roi=%+.1f%%",
            price_field, alert_id, price, roi,
        )
    except Exception:
        logger.exception("Conviction delayed price check failed for #%d", alert_id)


# ---------------------------------------------------------------------------
# Query for /conviction bot command
# ---------------------------------------------------------------------------


def get_recent_convictions() -> list[dict[str, Any]]:
    """Return recent conviction alerts for display."""
    _prune_buffers()
    results = []
    for ca, info in _alerted_cas.items():
        results.append({
            "ca": ca,
            "ticker": info.get("ticker", ""),
            "chain": info.get("chain", ""),
            "conviction_score": info.get("conviction_score", 0),
            "distinct_sources": info.get("distinct_sources", 0),
            "sources": info.get("sources", {}),
            "alerted_at": info.get("alerted_at"),
        })
    results.sort(key=lambda r: r.get("alerted_at") or datetime.min, reverse=True)
    return results
