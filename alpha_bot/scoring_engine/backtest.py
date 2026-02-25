"""Backtest engine — retroactively score historical tokens and simulate PnL."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select as sa_select

from alpha_bot.config import settings
from alpha_bot.scoring_engine.models import BacktestRun
from alpha_bot.storage.database import async_session

logger = logging.getLogger(__name__)

# Default weights (fallback)
_DEFAULT_WEIGHTS = {
    "narrative": 0.25,
    "profile": 0.20,
    "platform": 0.15,
    "market": 0.15,
    "depth": 0.15,
    "source": 0.10,
}


async def run_backtest(lookback_days: int = 30) -> BacktestRun:
    """Run a full backtest over historical tokens.

    1. Load tokens from call_outcomes (TG-sourced) and platform_tokens (platform-sourced)
    2. Retroactively compute composite scores
    3. Bucket into tiers, compute hit rates and avg ROI
    4. Sweep thresholds for optimal tier1/tier2 cutoffs
    5. Persist and return BacktestRun
    """
    from alpha_bot.tg_intel.models import CallOutcome
    from alpha_bot.platform_intel.models import PlatformToken

    cutoff = datetime.utcnow() - timedelta(days=lookback_days)

    # Load active weights
    try:
        from alpha_bot.scoring_engine.recalibrate import get_active_weights
        weights = await get_active_weights()
    except Exception:
        weights = dict(_DEFAULT_WEIGHTS)

    scored_tokens: list[dict] = []

    async with async_session() as session:
        # --- Source 1: call_outcomes with price data ---
        co_result = await session.execute(
            sa_select(CallOutcome).where(
                CallOutcome.price_check_status == "complete",
                CallOutcome.mention_timestamp >= cutoff,
            )
        )
        call_outcomes = list(co_result.scalars().all())

        for co in call_outcomes:
            score = _retroactive_score(
                name=co.ticker,
                symbol=co.ticker,
                platform=co.platform,
                mcap=co.mcap_at_mention,
                volume=None,
                liquidity=None,
                narrative_tags=co.narrative_tags,
                source="tg",
                weights=weights,
            )
            roi = co.roi_peak if co.roi_peak is not None else 0.0
            scored_tokens.append({
                "ca": co.ca,
                "source": "tg",
                "composite_score": score,
                "roi_peak": roi,
                "hit_2x": roi >= 100.0,
            })

        # --- Source 2: platform_tokens with lifecycle data ---
        pt_result = await session.execute(
            sa_select(PlatformToken).where(
                PlatformToken.check_status == "complete",
                PlatformToken.created_at >= cutoff,
            )
        )
        platform_tokens = list(pt_result.scalars().all())

        for pt in platform_tokens:
            score = _retroactive_score(
                name=pt.name,
                symbol=pt.symbol,
                platform=pt.platform,
                mcap=pt.mcap_1h,
                volume=pt.volume_24h_at_peak,
                liquidity=pt.liquidity_usd,
                narrative_tags=pt.narrative_tags,
                source="new_pairs",
                weights=weights,
            )
            # Compute ROI from mcap_1h → peak_mcap
            roi = 0.0
            if pt.peak_mcap and pt.mcap_1h and pt.mcap_1h > 0:
                roi = ((pt.peak_mcap / pt.mcap_1h) - 1.0) * 100.0
            scored_tokens.append({
                "ca": pt.ca,
                "source": "platform",
                "composite_score": score,
                "roi_peak": roi,
                "hit_2x": roi >= 100.0,
            })

    if not scored_tokens:
        run = BacktestRun(
            lookback_days=lookback_days,
            token_count=0,
            weights_json=json.dumps(weights),
            results_json="[]",
        )
        async with async_session() as session:
            session.add(run)
            await session.commit()
            await session.refresh(run)
        return run

    # --- Sweep thresholds ---
    best_combo = None
    best_value = -1.0

    for t1_thresh in range(60, 91, 5):
        for t2_thresh in range(40, t1_thresh, 5):
            t1_tokens = [t for t in scored_tokens if t["composite_score"] >= t1_thresh]
            if not t1_tokens:
                continue
            t1_hits = sum(1 for t in t1_tokens if t["hit_2x"])
            t1_hr = t1_hits / len(t1_tokens)
            t1_avg = sum(t["roi_peak"] for t in t1_tokens) / len(t1_tokens) if t1_tokens else 0
            value = t1_hr * max(t1_avg, 0)
            if value > best_value:
                best_value = value
                best_combo = (t1_thresh, t2_thresh)

    opt_t1 = best_combo[0] if best_combo else 80.0
    opt_t2 = best_combo[1] if best_combo else 60.0

    # --- Compute final stats using optimal thresholds ---
    tier1 = [t for t in scored_tokens if t["composite_score"] >= opt_t1]
    tier2 = [t for t in scored_tokens if opt_t2 <= t["composite_score"] < opt_t1]
    tier3 = [t for t in scored_tokens if t["composite_score"] < opt_t2]

    t1_hr = (sum(1 for t in tier1 if t["hit_2x"]) / len(tier1)) if tier1 else 0.0
    t2_hr = (sum(1 for t in tier2 if t["hit_2x"]) / len(tier2)) if tier2 else 0.0
    t1_roi = (sum(t["roi_peak"] for t in tier1) / len(tier1)) if tier1 else 0.0
    t2_roi = (sum(t["roi_peak"] for t in tier2) / len(tier2)) if tier2 else 0.0

    run = BacktestRun(
        lookback_days=lookback_days,
        token_count=len(scored_tokens),
        tier1_count=len(tier1),
        tier2_count=len(tier2),
        tier3_count=len(tier3),
        tier1_hit_rate_2x=round(t1_hr, 4),
        tier2_hit_rate_2x=round(t2_hr, 4),
        tier1_avg_roi=round(t1_roi, 2),
        tier2_avg_roi=round(t2_roi, 2),
        optimal_tier1_threshold=opt_t1,
        optimal_tier2_threshold=opt_t2,
        weights_json=json.dumps(weights),
        results_json=json.dumps(scored_tokens[:200]),  # cap stored detail
    )

    async with async_session() as session:
        session.add(run)
        await session.commit()
        await session.refresh(run)

    logger.info(
        "Backtest complete: %d tokens, T1=%d (%.0f%% 2x, %.0f%% avg ROI), T2=%d (%.0f%% 2x)",
        len(scored_tokens), len(tier1), t1_hr * 100, t1_roi, len(tier2), t2_hr * 100,
    )

    return run


def _retroactive_score(
    name: str,
    symbol: str,
    platform: str,
    mcap: float | None,
    volume: float | None,
    liquidity: float | None,
    narrative_tags: str,
    source: str,
    weights: dict,
) -> float:
    """Compute a simplified retroactive composite score."""
    from alpha_bot.scanner.candidate_scorer import _SOURCE_BONUS

    # Narrative: simple keyword heuristic from tags
    nar_score = 0.0
    try:
        tags = json.loads(narrative_tags) if narrative_tags else []
    except (json.JSONDecodeError, TypeError):
        tags = []
    if tags:
        nar_score = min(len(tags) * 25.0, 100.0)

    # Depth: platform layer + narrative tags
    depth = 0.0
    layers = 0
    if tags:
        layers += 1
    if platform in ("clanker", "virtuals", "flaunch"):
        layers += 1
    if len(tags) >= 2:
        layers += 1
    depth = min(layers * 25.0, 100.0)

    # Profile match: simplified — just check mcap range
    profile_score = 0.0
    if mcap and 50_000 <= mcap <= 1_000_000:
        profile_score = 60.0
    elif mcap and 1_000_000 < mcap <= 5_000_000:
        profile_score = 30.0

    # Market score
    market_score = 0.0
    if mcap and mcap > 0:
        if volume and volume > 0:
            ratio = volume / mcap
            market_score += min(ratio * 80, 40)
        if liquidity:
            if liquidity >= 100_000:
                market_score += 30
            elif liquidity >= 50_000:
                market_score += 25
            elif liquidity >= 20_000:
                market_score += 15

    # Platform score: crude estimate
    platform_score = 50.0 if platform in ("clanker", "virtuals", "flaunch") else 0.0

    # Source
    source_score = float(_SOURCE_BONUS.get(source, 40))

    composite = (
        weights.get("narrative", 0.25) * nar_score
        + weights.get("depth", 0.15) * depth
        + weights.get("profile", 0.20) * profile_score
        + weights.get("platform", 0.15) * platform_score
        + weights.get("market", 0.15) * market_score
        + weights.get("source", 0.10) * source_score
    )

    return round(min(composite, 100.0), 1)


def format_backtest_report(run: BacktestRun) -> str:
    """Format a BacktestRun as a Telegram HTML summary."""
    lines = [
        f"<b>Backtest Report</b> ({run.lookback_days}d lookback)",
        f"Run: {run.run_timestamp.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Tokens scored: <b>{run.token_count}</b>\n",
    ]

    if run.token_count == 0:
        lines.append("No tokens with complete data in this period.")
        return "\n".join(lines)

    lines.append(f"<b>Tier 1</b> (>={run.optimal_tier1_threshold:.0f}): "
                 f"{run.tier1_count} tokens")
    lines.append(f"  2x hit rate: <b>{run.tier1_hit_rate_2x:.0%}</b>")
    lines.append(f"  Avg ROI: <b>{run.tier1_avg_roi:+.0f}%</b>\n")

    lines.append(f"<b>Tier 2</b> (>={run.optimal_tier2_threshold:.0f}): "
                 f"{run.tier2_count} tokens")
    lines.append(f"  2x hit rate: <b>{run.tier2_hit_rate_2x:.0%}</b>")
    lines.append(f"  Avg ROI: <b>{run.tier2_avg_roi:+.0f}%</b>\n")

    lines.append(f"Tier 3: {run.tier3_count} tokens")

    lines.append(f"\nOptimal thresholds: T1={run.optimal_tier1_threshold:.0f}, "
                 f"T2={run.optimal_tier2_threshold:.0f}")

    try:
        w = json.loads(run.weights_json)
        w_str = " | ".join(f"{k}={v:.0%}" for k, v in w.items())
        lines.append(f"Weights: {w_str}")
    except (json.JSONDecodeError, TypeError):
        pass

    return "\n".join(lines)
