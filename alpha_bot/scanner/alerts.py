"""Scanner TG alert delivery — same set_notify_fn pattern as convergence.py."""

from __future__ import annotations

import json
import logging
from typing import Callable, Coroutine

from alpha_bot.scanner.models import ScannerCandidate

logger = logging.getLogger(__name__)

_notify_fn: Callable[[str, str], Coroutine] | None = None


def set_notify_fn(fn: Callable[[str, str], Coroutine]) -> None:
    global _notify_fn
    _notify_fn = fn


async def _notify(text: str) -> None:
    if _notify_fn:
        try:
            await _notify_fn(text, "HTML")
        except Exception as exc:
            logger.warning("Scanner alert notify failed: %s", exc)


def _fmt_mcap(n: float | None) -> str:
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.0f}"


async def fire_scanner_alert(candidate: ScannerCandidate) -> None:
    """Format and send a TG alert for a high-scoring scanner candidate."""
    tier_emoji = {1: "\U0001f534", 2: "\U0001f7e1", 3: "\U0001f7e2"}.get(candidate.tier, "\u26ab")
    tier_label = {1: "Tier 1", 2: "Tier 2", 3: "Tier 3"}.get(candidate.tier, "?")

    # Parse matched themes
    try:
        themes = json.loads(candidate.matched_themes) if candidate.matched_themes else []
    except (json.JSONDecodeError, TypeError):
        themes = []
    themes_str = ", ".join(f'"{t}"' for t in themes[:3]) if themes else "none"

    vol_mcap = ""
    if candidate.mcap and candidate.mcap > 0 and candidate.volume_24h:
        ratio = candidate.volume_24h / candidate.mcap
        vol_mcap = f" | Vol/MCap: {ratio:.2f}"

    text = (
        f"{tier_emoji} <b>SCANNER: ${candidate.ticker} ({tier_label})</b>\n\n"
        f"Score: <b>{candidate.composite_score:.0f}/100</b>\n"
        f"Chain: {candidate.chain} | Platform: {candidate.platform}\n"
        f"Narrative: {themes_str} — {candidate.narrative_depth} layers\n"
        f"MCap: {_fmt_mcap(candidate.mcap)} | Liq: {_fmt_mcap(candidate.liquidity_usd)}{vol_mcap}\n\n"
        f"<code>{candidate.ca}</code>"
    )

    logger.info(
        "Scanner alert: $%s score=%.0f tier=%d",
        candidate.ticker, candidate.composite_score, candidate.tier,
    )
    await _notify(text)
