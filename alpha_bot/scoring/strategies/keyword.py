import re

from alpha_bot.ingestion.models import RawTweet
from alpha_bot.scoring.strategies.base import ScoringStrategy

TICKER_PATTERN = re.compile(r"\$([A-Z]{2,6})\b")

ALPHA_PHRASES = [
    "alpha leak",
    "insider",
    "just announced",
    "breaking",
    "accumulating",
    "whale",
    "massive buy",
    "about to pump",
    "undervalued",
    "100x",
    "moonshot",
    "hidden gem",
    "early",
    "before everyone",
    "not financial advice",
    "dyor",
    "ape in",
    "send it",
    "loading up",
    "buying the dip",
]

BULLISH_TERMS = [
    "bullish",
    "long",
    "buy",
    "moon",
    "pump",
    "breakout",
    "rally",
    "rip",
    "send",
    "green",
    "calls",
    "ATH",
    "parabolic",
    "accumulate",
]

BEARISH_TERMS = [
    "bearish",
    "short",
    "sell",
    "dump",
    "crash",
    "rug",
    "scam",
    "puts",
    "red",
    "collapse",
    "liquidation",
]


class KeywordStrategy(ScoringStrategy):
    def score(self, tweet: RawTweet) -> float:
        text = tweet.text.lower()
        points = 0.0

        # Ticker mentions
        tickers = TICKER_PATTERN.findall(tweet.text)
        if tickers:
            points += min(len(tickers) * 0.15, 0.3)

        # Alpha phrases
        for phrase in ALPHA_PHRASES:
            if phrase in text:
                points += 0.1
                break

        # Directional terms
        bullish_hits = sum(1 for t in BULLISH_TERMS if t.lower() in text)
        bearish_hits = sum(1 for t in BEARISH_TERMS if t.lower() in text)
        if bullish_hits + bearish_hits > 0:
            points += min((bullish_hits + bearish_hits) * 0.05, 0.3)

        return min(points, 1.0)

    def extract_tickers(self, tweet: RawTweet) -> list[str]:
        return TICKER_PATTERN.findall(tweet.text)

    def sentiment_direction(self, tweet: RawTweet) -> str:
        text = tweet.text.lower()
        bullish = sum(1 for t in BULLISH_TERMS if t.lower() in text)
        bearish = sum(1 for t in BEARISH_TERMS if t.lower() in text)
        if bullish > bearish:
            return "bullish"
        if bearish > bullish:
            return "bearish"
        return "neutral"
