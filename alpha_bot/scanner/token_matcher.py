"""Token-to-narrative matcher — keyword + optional Claude semantic matching."""

from __future__ import annotations

import json
import logging
from difflib import SequenceMatcher

from alpha_bot.config import settings
from alpha_bot.scanner.models import TrendingTheme

logger = logging.getLogger(__name__)

# Minimum fuzzy match ratio to consider a match
_FUZZY_THRESHOLD = 0.55


def _keyword_match(
    token_name: str, ticker: str, themes: list[TrendingTheme],
) -> list[tuple[TrendingTheme, float]]:
    """Fast keyword + fuzzy matching. Returns (theme, match_score) pairs."""
    matches: list[tuple[TrendingTheme, float]] = []
    name_lower = token_name.lower()
    ticker_lower = ticker.lower()

    for theme in themes:
        t = theme.theme.lower()

        # Exact substring match
        if t in name_lower or t in ticker_lower or name_lower in t or ticker_lower in t:
            matches.append((theme, 1.0))
            continue

        # Check individual words in theme against ticker/name
        theme_words = t.split()
        if any(w in name_lower or w in ticker_lower for w in theme_words if len(w) > 3):
            matches.append((theme, 0.8))
            continue

        # Fuzzy match
        ratio = max(
            SequenceMatcher(None, ticker_lower, t).ratio(),
            SequenceMatcher(None, name_lower, t).ratio(),
        )
        if ratio >= _FUZZY_THRESHOLD:
            matches.append((theme, ratio))

    return matches


async def _claude_match(
    token_name: str, ticker: str, themes: list[TrendingTheme],
) -> list[str]:
    """Use Claude API for semantic matching (top 10 themes only)."""
    if not settings.anthropic_api_key:
        return []

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — skipping Claude matching")
        return []

    top_themes = sorted(themes, key=lambda t: t.velocity, reverse=True)[:10]
    theme_list = "\n".join(f"- {t.theme} (source: {t.source})" for t in top_themes)

    prompt = (
        f"Token: {ticker} ({token_name})\n\n"
        f"Trending themes:\n{theme_list}\n\n"
        "Which themes does this token's name or ticker relate to? "
        "Return ONLY a JSON array of matching theme strings, e.g. [\"theme1\", \"theme2\"]. "
        "Return [] if no matches. Be strict — only match if there's a clear semantic connection."
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        # Parse JSON array from response
        if "[" in text:
            text = text[text.index("["):text.rindex("]") + 1]
            return json.loads(text)
    except Exception as exc:
        logger.warning("Claude matching failed: %s", exc)

    return []


async def match_token_to_themes(
    token_name: str,
    ticker: str,
    themes: list[TrendingTheme],
) -> tuple[list[str], float]:
    """Match a token against trending themes.

    Returns (matched_theme_names, narrative_score 0-100).
    """
    if not themes:
        return [], 0.0

    # Tier 1: keyword/fuzzy matching
    keyword_matches = _keyword_match(token_name, ticker, themes)

    matched_names = list({m[0].theme for m in keyword_matches})

    # Tier 2: Claude semantic matching (if enabled and keyword matches are thin)
    if settings.scanner_use_claude_matching and len(matched_names) < 2:
        claude_matches = await _claude_match(token_name, ticker, themes)
        for cm in claude_matches:
            if cm.lower() not in [m.lower() for m in matched_names]:
                matched_names.append(cm)

    if not matched_names:
        return [], 0.0

    # Score: base 40 for any match, +20 per additional match (cap 100)
    # Boost by velocity of matched themes
    base_score = min(40 + 20 * len(matched_names), 80)

    # Velocity boost: top matched theme velocity
    velocity_boost = 0.0
    for m in keyword_matches:
        theme = m[0]
        if theme.velocity > 100:
            velocity_boost = max(velocity_boost, 20.0)
        elif theme.velocity > 50:
            velocity_boost = max(velocity_boost, 10.0)

    score = min(base_score + velocity_boost, 100.0)

    return matched_names, score
