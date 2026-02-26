"""Token-to-narrative matcher — keyword + optional Claude semantic matching."""

from __future__ import annotations

import json
import logging
import time
from difflib import SequenceMatcher

from alpha_bot.config import settings
from alpha_bot.scanner.models import TrendingTheme

logger = logging.getLogger(__name__)

# Minimum fuzzy match ratio to consider a match
_FUZZY_THRESHOLD = 0.55

# Simple LRU cache for Claude context calls (avoid re-calling for same token)
_claude_cache: dict[str, tuple[list[str], float]] = {}
_CLAUDE_CACHE_MAX = 200
_CLAUDE_CACHE_TTL = 3600  # 1 hour

# Stopwords: generic crypto/meme words that match everything and carry no signal
_STOPWORDS = frozenset({
    "meme", "coin", "crypto", "token", "buy", "sell", "best", "will",
    "they", "ever", "what", "this", "that", "with", "from", "have",
    "your", "about", "just", "like", "more", "been", "into", "when",
    "does", "should", "would", "could", "really", "very", "much",
    "some", "than", "look", "well", "most", "know", "think",
    "price", "market", "value", "stock", "trade", "money",
    "pump", "dump", "moon", "launch", "fair", "next", "good",
    "gets", "says", "deal", "face", "look", "take", "make",
    "news", "now", "new", "top", "how", "why", "all", "get",
})

# Skip themes that are too long (likely full Reddit post titles, not real themes)
_MAX_THEME_WORDS = 5


def _keyword_match(
    token_name: str, ticker: str, themes: list[TrendingTheme],
) -> list[tuple[TrendingTheme, float]]:
    """Fast keyword + fuzzy matching. Returns (theme, match_score) pairs."""
    matches: list[tuple[TrendingTheme, float]] = []
    name_lower = token_name.lower()
    ticker_lower = ticker.lower()

    for theme in themes:
        t = theme.theme.lower()
        t_words = t.split()

        # Skip long Reddit post titles — they're not real themes
        if len(t_words) > _MAX_THEME_WORDS:
            continue

        # Exact substring match (theme is fully contained in name or vice versa)
        if t in name_lower or t in ticker_lower or name_lower in t or ticker_lower in t:
            matches.append((theme, 1.0))
            continue

        # Word-level match: require non-stopword, 5+ chars to reduce noise
        significant_words = [
            w for w in t_words
            if len(w) >= 5 and w not in _STOPWORDS
        ]
        if significant_words and any(
            w in name_lower or w in ticker_lower for w in significant_words
        ):
            matches.append((theme, 0.8))
            continue

        # Fuzzy match (only for short themes — long ones create false positives)
        if len(t_words) <= 3:
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
    """Use Claude API for semantic matching against trending themes."""
    if not settings.anthropic_api_key:
        return []

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — skipping Claude matching")
        return []

    # Send more themes (top 30) for better coverage
    top_themes = sorted(themes, key=lambda t: t.velocity, reverse=True)[:30]
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


async def _claude_cultural_context(
    token_name: str, ticker: str,
) -> list[str]:
    """Use Claude to identify what cultural reference a token represents.

    Unlike _claude_match which checks against existing themes, this generates
    cultural tags from scratch — catches political figures, pop culture refs,
    memes, etc. that may not be in the trending_themes DB yet.
    """
    if not settings.anthropic_api_key:
        return []

    # Check cache
    cache_key = f"{ticker}:{token_name}".lower()
    cached = _claude_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < _CLAUDE_CACHE_TTL:
        return cached[0]

    try:
        import anthropic
    except ImportError:
        return []

    prompt = (
        f"Crypto token: ${ticker} (full name: \"{token_name}\")\n\n"
        "What real-world person, event, meme, trend, or cultural reference does "
        "this token name/ticker refer to? Think about: politicians, celebrities, "
        "viral memes, movies, TV shows, internet culture, animals, sports, tech.\n\n"
        "Return a JSON object with:\n"
        "- \"tags\": array of 1-3 short cultural category tags (e.g. \"politics\", \"elon musk\", \"viral meme\")\n"
        "- \"reference\": one-line description of what it references\n\n"
        "If the name is generic/meaningless with no clear cultural reference, return {\"tags\": [], \"reference\": \"none\"}.\n"
        "Return ONLY valid JSON, nothing else."
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        if "{" in text:
            text = text[text.index("{"):text.rindex("}") + 1]
            data = json.loads(text)
            tags = data.get("tags", [])
            if tags:
                logger.info(
                    "Claude cultural context for %s (%s): %s — %s",
                    ticker, token_name, tags, data.get("reference", ""),
                )
            # Cache result
            if len(_claude_cache) >= _CLAUDE_CACHE_MAX:
                # Evict oldest entries
                oldest = sorted(_claude_cache, key=lambda k: _claude_cache[k][1])
                for k in oldest[:50]:
                    del _claude_cache[k]
            _claude_cache[cache_key] = (tags, time.time())
            return tags
    except Exception as exc:
        logger.warning("Claude cultural context failed: %s", exc)

    return []


async def match_token_to_themes(
    token_name: str,
    ticker: str,
    themes: list[TrendingTheme],
) -> tuple[list[str], float]:
    """Match a token against trending themes.

    Returns (matched_theme_names, narrative_score 0-100).
    """
    # Tier 1: keyword/fuzzy matching against existing themes
    keyword_matches = _keyword_match(token_name, ticker, themes) if themes else []
    matched_names = list({m[0].theme for m in keyword_matches})

    # Tier 2: Claude semantic matching against themes (if keyword matches are thin)
    if settings.scanner_use_claude_matching and themes and len(matched_names) < 2:
        claude_matches = await _claude_match(token_name, ticker, themes)
        for cm in claude_matches:
            if cm.lower() not in [m.lower() for m in matched_names]:
                matched_names.append(cm)

    # Tier 3: Claude cultural context (if still no matches — identifies what the
    # token references even without existing trending themes)
    if settings.scanner_use_claude_matching and len(matched_names) == 0:
        cultural_tags = await _claude_cultural_context(token_name, ticker)
        for tag in cultural_tags:
            if tag.lower() not in [m.lower() for m in matched_names]:
                matched_names.append(tag)

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

    # Cultural context match gets a smaller boost (no velocity data)
    # but still meaningful since Claude confirmed a real-world reference
    if not keyword_matches and matched_names:
        velocity_boost = 10.0  # modest boost for cultural match without trending data

    score = min(base_score + velocity_boost, 100.0)

    return matched_names, score
