"""Weekly weight recalibration loop and active weights accessor."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

from sqlalchemy import select as sa_select, func

from alpha_bot.config import settings
from alpha_bot.scoring_engine.models import BacktestRun, ScoringWeights
from alpha_bot.storage.database import async_session

logger = logging.getLogger(__name__)

_DEFAULT_WEIGHTS = {
    "narrative": 0.25,
    "profile": 0.20,
    "platform": 0.15,
    "market": 0.15,
    "depth": 0.15,
    "source": 0.10,
}

# Simple in-memory cache for active weights
_weights_cache: dict | None = None
_weights_cache_ts: float = 0.0
_CACHE_TTL = 300  # 5 minutes

_notify_fn: Callable[[str, str], Awaitable[None]] | None = None


def set_notify_fn(fn: Callable[[str, str], Awaitable[None]]) -> None:
    global _notify_fn
    _notify_fn = fn


async def get_active_weights() -> dict:
    """Load current active weights from DB (5-min cache). Falls back to defaults."""
    global _weights_cache, _weights_cache_ts

    now = time.time()
    if _weights_cache is not None and (now - _weights_cache_ts) < _CACHE_TTL:
        return dict(_weights_cache)

    try:
        async with async_session() as session:
            result = await session.execute(
                sa_select(ScoringWeights)
                .where(ScoringWeights.active == True)  # noqa: E712
                .order_by(ScoringWeights.version.desc())
                .limit(1)
            )
            sw = result.scalar_one_or_none()

        if sw:
            weights = {
                "narrative": sw.w_narrative,
                "profile": sw.w_profile,
                "platform": sw.w_platform,
                "market": sw.w_market,
                "depth": sw.w_depth,
                "source": sw.w_source,
            }
            _weights_cache = weights
            _weights_cache_ts = now
            return dict(weights)
    except Exception:
        logger.debug("Failed to load weights from DB, using defaults")

    _weights_cache = dict(_DEFAULT_WEIGHTS)
    _weights_cache_ts = now
    return dict(_DEFAULT_WEIGHTS)


async def recalibrate_loop() -> None:
    """Run weekly recalibration: backtest, compute signal lift, adjust weights."""
    logger.info(
        "Recalibration loop started (interval=%ds)",
        settings.recalibrate_interval_seconds,
    )

    while True:
        try:
            await asyncio.sleep(settings.recalibrate_interval_seconds)
            await _run_recalibration()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Recalibration loop error")


async def _run_recalibration() -> None:
    """Single recalibration cycle."""
    from alpha_bot.scoring_engine.backtest import run_backtest
    from alpha_bot.tg_intel.models import CallOutcome
    from alpha_bot.platform_intel.models import PlatformToken
    from datetime import datetime, timedelta

    logger.info("Starting weight recalibration...")

    # Step 1: Run backtest over last 14 days
    run = await run_backtest(lookback_days=14)

    if run.token_count < 10:
        logger.info("Not enough data for recalibration (%d tokens)", run.token_count)
        return

    # Step 2: Split into winners/losers and compute signal lift
    try:
        results = json.loads(run.results_json)
    except (json.JSONDecodeError, TypeError):
        return

    winners = [t for t in results if t.get("hit_2x")]
    losers = [t for t in results if not t.get("hit_2x")]

    if not winners or not losers:
        logger.info("Cannot recalibrate: need both winners and losers")
        return

    # Load current weights
    old_weights = await get_active_weights()

    # Step 3: For each signal dimension, compute lift
    # We use composite_score as a proxy since individual scores aren't stored
    # Instead, adjust based on which source types (tg vs platform) had more winners
    tg_winners = sum(1 for w in winners if w.get("source") == "tg")
    tg_total = sum(1 for t in results if t.get("source") == "tg")
    plat_winners = sum(1 for w in winners if w.get("source") == "platform")
    plat_total = sum(1 for t in results if t.get("source") == "platform")

    overall_win_rate = len(winners) / len(results) if results else 0

    # Compute lift per dimension
    lifts = {}
    tg_wr = (tg_winners / tg_total) if tg_total > 0 else overall_win_rate
    plat_wr = (plat_winners / plat_total) if plat_total > 0 else overall_win_rate

    # Winner avg score vs loser avg score
    w_avg = sum(t["composite_score"] for t in winners) / len(winners)
    l_avg = sum(t["composite_score"] for t in losers) / len(losers)
    overall_avg = (w_avg + l_avg) / 2 if (w_avg + l_avg) > 0 else 1.0

    # Heuristic lifts per signal
    lifts["narrative"] = (w_avg - l_avg) / overall_avg * 0.3 if overall_avg > 0 else 0
    lifts["profile"] = lifts["narrative"] * 0.8  # slightly correlated
    lifts["platform"] = ((plat_wr - overall_win_rate) / max(overall_win_rate, 0.01)) * 0.3
    lifts["market"] = lifts["narrative"] * 0.6
    lifts["depth"] = lifts["narrative"] * 0.5
    lifts["source"] = ((tg_wr - overall_win_rate) / max(overall_win_rate, 0.01)) * 0.3

    # Step 4: Adjust weights
    new_weights = {}
    for key in _DEFAULT_WEIGHTS:
        lift = lifts.get(key, 0.0)
        new_w = old_weights.get(key, _DEFAULT_WEIGHTS[key]) * (1.0 + lift)
        new_weights[key] = new_w

    # Normalize to sum=1.0
    total = sum(new_weights.values())
    if total > 0:
        new_weights = {k: v / total for k, v in new_weights.items()}

    # Clamp to [0.05, 0.35]
    for key in new_weights:
        new_weights[key] = max(0.05, min(0.35, new_weights[key]))

    # Re-normalize after clamping
    total = sum(new_weights.values())
    if total > 0:
        new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}

    # Step 5: Save new weights
    async with async_session() as session:
        # Get next version
        max_ver_result = await session.execute(
            sa_select(func.max(ScoringWeights.version))
        )
        max_ver = max_ver_result.scalar() or 0

        # Deactivate previous
        prev_result = await session.execute(
            sa_select(ScoringWeights).where(ScoringWeights.active == True)  # noqa: E712
        )
        for prev in prev_result.scalars().all():
            prev.active = False

        # Insert new
        sw = ScoringWeights(
            version=max_ver + 1,
            w_narrative=new_weights["narrative"],
            w_profile=new_weights["profile"],
            w_platform=new_weights["platform"],
            w_market=new_weights["market"],
            w_depth=new_weights["depth"],
            w_source=new_weights["source"],
            source="recalibration",
            backtest_run_id=run.id,
            active=True,
        )
        session.add(sw)
        await session.commit()

    # Clear cache
    global _weights_cache, _weights_cache_ts
    _weights_cache = None
    _weights_cache_ts = 0.0

    logger.info("Recalibration complete: v%d weights saved", max_ver + 1)

    # Step 6: Notify
    if _notify_fn:
        changes = []
        for key in _DEFAULT_WEIGHTS:
            old_v = old_weights.get(key, 0)
            new_v = new_weights.get(key, 0)
            arrow = "+" if new_v > old_v else "-" if new_v < old_v else "="
            changes.append(f"  {key}: {old_v:.0%} {arrow} {new_v:.0%}")

        text = (
            "<b>Scoring Weights Recalibrated</b>\n\n"
            f"Backtest: {run.token_count} tokens, "
            f"T1 hit rate: {run.tier1_hit_rate_2x:.0%}\n\n"
            + "\n".join(changes)
        )
        try:
            await _notify_fn(text, "HTML")
        except Exception:
            logger.debug("Failed to send recalibration notification")


async def format_weights_text() -> str:
    """Format current weights for display."""
    weights = await get_active_weights()

    async with async_session() as session:
        result = await session.execute(
            sa_select(ScoringWeights)
            .where(ScoringWeights.active == True)  # noqa: E712
            .order_by(ScoringWeights.version.desc())
            .limit(1)
        )
        sw = result.scalar_one_or_none()

    lines = ["<b>Current Scoring Weights</b>\n"]

    if sw:
        lines.append(f"Version: {sw.version} ({sw.source})")
        lines.append(f"Set: {sw.created_at.strftime('%Y-%m-%d %H:%M UTC')}\n")
    else:
        lines.append("Source: defaults (no DB weights)\n")

    for key, val in weights.items():
        bar_len = int(val * 40)
        bar = "=" * bar_len
        lines.append(f"  {key:12s} {val:.0%} {bar}")

    return "\n".join(lines)
