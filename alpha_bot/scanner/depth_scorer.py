"""Narrative depth scorer — count independent narrative layers."""

from __future__ import annotations

from alpha_bot.research.narratives import NARRATIVES
from alpha_bot.scanner.models import TrendingTheme

# Depth multiplier: more independent layers = higher score
_DEPTH_SCORES = {1: 25, 2: 50, 3: 75}
_MAX_DEPTH_SCORE = 100


def compute_depth(
    token_name: str,
    ticker: str,
    matched_themes: list[str],
    all_themes: list[TrendingTheme],
    platform: str = "unknown",
) -> int:
    """Count narrative layers and return depth score (0-100).

    Layers:
    1. Cultural trend — matched a Google Trends / Reddit theme
    2. Crypto meta — matches NARRATIVES keywords (AI, DeFi, gaming, etc.)
    3. Platform/ecosystem — Clanker / Virtuals / Flaunch
    4. Timely event — matched a Reddit/Farcaster theme categorized as event
    """
    layers = 0
    combined = f"{token_name} {ticker} {' '.join(matched_themes)}".lower()

    # Layer 1: Cultural trend — matched any Google Trends or Reddit theme
    cultural_sources = {"google", "reddit"}
    theme_sources = {t.source for t in all_themes if t.theme.lower() in [m.lower() for m in matched_themes]}
    if theme_sources & cultural_sources:
        layers += 1

    # Layer 2: Crypto meta — matches existing NARRATIVES dict
    for _narrative, keywords in NARRATIVES.items():
        if any(kw in combined for kw in keywords):
            layers += 1
            break

    # Layer 3: Platform/ecosystem
    if platform in ("clanker", "virtuals", "flaunch"):
        layers += 1

    # Layer 4: Timely event — Farcaster or Reddit "event" category
    event_themes = {
        t.theme.lower() for t in all_themes
        if t.source in ("farcaster", "reddit") and t.velocity > 50
    }
    if any(m.lower() in event_themes for m in matched_themes):
        layers += 1

    return _DEPTH_SCORES.get(layers, _MAX_DEPTH_SCORE if layers >= 4 else 0)
