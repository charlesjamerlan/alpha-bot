"""Forensic analysis of a pumped token — what social signals preceded the move."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from alpha_bot.config import settings
from alpha_bot.ingestion.models import RawTweet
from alpha_bot.research.coingecko import _resolve_coingecko_id

logger = logging.getLogger(__name__)
_vader = SentimentIntensityAnalyzer()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TweetSignal:
    """A tweet mapped onto the pump timeline."""

    author: str
    followers: int
    text: str
    posted_at: datetime
    likes: int
    retweets: int
    sentiment: float  # VADER compound score
    phase: str  # "pre_pump", "during_pump", "post_pump"

    def to_dict(self) -> dict:
        return {
            "author": self.author,
            "followers": self.followers,
            "text": self.text,
            "posted_at": self.posted_at.isoformat(),
            "likes": self.likes,
            "retweets": self.retweets,
            "sentiment": self.sentiment,
            "phase": self.phase,
        }


@dataclass
class PumpForensicsReport:
    ticker: str
    coin_id: str
    name: str

    # Price action
    current_price: float = 0
    change_24h: float | None = None
    change_7d: float | None = None
    pump_start_price: float = 0
    pump_peak_price: float = 0
    pump_magnitude_pct: float = 0
    pump_start_time: datetime | None = None
    pump_peak_time: datetime | None = None
    price_points: list[dict] = field(default_factory=list)  # for chart

    # Social signal counts
    total_tweets: int = 0
    pre_pump_tweets: int = 0
    during_pump_tweets: int = 0
    post_pump_tweets: int = 0

    # Pre-pump analysis (the alpha)
    earliest_mention: datetime | None = None
    hours_early: float = 0  # how many hours before pump was it first mentioned
    early_mentioners: list[TweetSignal] = field(default_factory=list)
    pre_pump_sentiment_avg: float = 0
    pre_pump_engagement: int = 0

    # All signals for display
    all_signals: list[TweetSignal] = field(default_factory=list)

    # Verdict
    signal_score: float = 0  # 0-1: how predictable was this pump from social data
    verdict: str = ""

    twitter_available: bool = True

    analyzed_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "coin_id": self.coin_id,
            "name": self.name,
            "current_price": self.current_price,
            "change_24h": self.change_24h,
            "change_7d": self.change_7d,
            "pump_start_price": self.pump_start_price,
            "pump_peak_price": self.pump_peak_price,
            "pump_magnitude_pct": self.pump_magnitude_pct,
            "pump_start_time": self.pump_start_time.isoformat()
            if self.pump_start_time
            else None,
            "pump_peak_time": self.pump_peak_time.isoformat()
            if self.pump_peak_time
            else None,
            "price_points": self.price_points,
            "total_tweets": self.total_tweets,
            "pre_pump_tweets": self.pre_pump_tweets,
            "during_pump_tweets": self.during_pump_tweets,
            "post_pump_tweets": self.post_pump_tweets,
            "earliest_mention": self.earliest_mention.isoformat()
            if self.earliest_mention
            else None,
            "hours_early": self.hours_early,
            "early_mentioners": [s.to_dict() for s in self.early_mentioners],
            "pre_pump_sentiment_avg": self.pre_pump_sentiment_avg,
            "pre_pump_engagement": self.pre_pump_engagement,
            "all_signals": [s.to_dict() for s in self.all_signals],
            "signal_score": self.signal_score,
            "verdict": self.verdict,
            "twitter_available": self.twitter_available,
            "analyzed_at": self.analyzed_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Price analysis helpers
# ---------------------------------------------------------------------------


async def _get_hourly_prices(
    coin_id: str, days: int = 7
) -> list[tuple[int, float]]:
    """Fetch hourly price data for the last N days."""
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(
                f"{settings.coingecko_base_url}/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": days},
            )
            resp.raise_for_status()
            return [
                (int(p[0]), float(p[1])) for p in resp.json().get("prices", [])
            ]
        except httpx.HTTPError as exc:
            logger.warning("CoinGecko chart failed for %s: %s", coin_id, exc)
            return []


def _detect_pump(prices: list[tuple[int, float]]) -> dict:
    """
    Find the biggest upward move in the price data.

    Returns dict with pump_start, pump_peak, magnitude, and their timestamps.
    """
    if len(prices) < 3:
        return {}

    # Find the global max (peak of the pump)
    peak_idx = max(range(len(prices)), key=lambda i: prices[i][1])
    peak_ts, peak_price = prices[peak_idx]

    # Find the lowest point before the peak (pump start)
    if peak_idx == 0:
        start_idx = 0
    else:
        start_idx = min(range(peak_idx + 1), key=lambda i: prices[i][1])

    start_ts, start_price = prices[start_idx]
    magnitude = (
        ((peak_price - start_price) / start_price) * 100
        if start_price > 0
        else 0
    )

    return {
        "start_ts": start_ts,
        "start_price": start_price,
        "peak_ts": peak_ts,
        "peak_price": peak_price,
        "magnitude_pct": magnitude,
        "start_time": datetime.utcfromtimestamp(start_ts / 1000),
        "peak_time": datetime.utcfromtimestamp(peak_ts / 1000),
    }


# ---------------------------------------------------------------------------
# Social analysis
# ---------------------------------------------------------------------------


def _classify_tweet(
    tweet: RawTweet, pump_start: datetime, pump_peak: datetime
) -> TweetSignal:
    """Map a tweet onto the pump timeline and analyze sentiment."""
    posted = tweet.created_at.replace(tzinfo=None)
    compound = _vader.polarity_scores(tweet.text)["compound"]

    if posted < pump_start:
        phase = "pre_pump"
    elif posted <= pump_peak:
        phase = "during_pump"
    else:
        phase = "post_pump"

    return TweetSignal(
        author=tweet.author.username,
        followers=tweet.author.followers_count,
        text=tweet.text,
        posted_at=posted,
        likes=tweet.metrics.like_count,
        retweets=tweet.metrics.retweet_count,
        sentiment=compound,
        phase=phase,
    )


def _compute_signal_score(report: PumpForensicsReport) -> float:
    """
    Score 0-1 indicating how predictable the pump was from social signals.

    High score = strong social signals appeared before the pump.
    """
    score = 0.0

    # Were there pre-pump tweets at all?
    if report.pre_pump_tweets > 0:
        score += 0.2

    # More pre-pump tweets = stronger signal
    if report.pre_pump_tweets >= 3:
        score += 0.15
    if report.pre_pump_tweets >= 10:
        score += 0.1

    # How early were signals? (more hours early = better)
    if report.hours_early >= 2:
        score += 0.1
    if report.hours_early >= 12:
        score += 0.15
    if report.hours_early >= 24:
        score += 0.1

    # Pre-pump sentiment was bullish
    if report.pre_pump_sentiment_avg > 0.2:
        score += 0.1

    # High-follower accounts mentioned it early
    if report.early_mentioners:
        max_followers = max(s.followers for s in report.early_mentioners)
        if max_followers >= 10000:
            score += 0.1

    return min(score, 1.0)


def _generate_verdict(report: PumpForensicsReport) -> str:
    """Generate a human-readable verdict."""
    if not report.twitter_available:
        return "Twitter data unavailable — price analysis only."

    if report.total_tweets == 0:
        return "No Twitter chatter found. This pump had no visible social signals."

    if report.pre_pump_tweets == 0:
        return (
            "All social activity came AFTER the pump started. "
            "No early warning signals were visible on Twitter."
        )

    if report.signal_score >= 0.7:
        return (
            f"Strong early signals: {report.pre_pump_tweets} tweets appeared "
            f"{report.hours_early:.0f}h before the pump with "
            f"{'bullish' if report.pre_pump_sentiment_avg > 0 else 'mixed'} sentiment. "
            "This pump was potentially foreseeable from social data."
        )

    if report.signal_score >= 0.4:
        return (
            f"Some early signals: {report.pre_pump_tweets} tweets appeared before the pump, "
            f"but with only {report.hours_early:.0f}h of lead time. "
            "Catching this early would have required fast monitoring."
        )

    return (
        f"Weak signals: only {report.pre_pump_tweets} pre-pump tweets. "
        "Social data alone would not have reliably predicted this move."
    )


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


async def analyze_pump(
    ticker: str,
    coin_id: str | None = None,
) -> PumpForensicsReport:
    """
    Full forensic analysis of a recently pumped token.

    1. Get 7-day hourly price chart
    2. Detect pump timing (inflection point)
    3. Search Twitter for mentions
    4. Classify tweets as pre/during/post pump
    5. Analyze early signals
    6. Score predictability
    """
    ticker = ticker.upper().strip("$")

    # Resolve CoinGecko ID if not provided
    if not coin_id:
        async with httpx.AsyncClient(timeout=15) as client:
            coin_id = await _resolve_coingecko_id(ticker, client)
    if not coin_id:
        raise ValueError(f"Could not resolve CoinGecko ID for {ticker}")

    report = PumpForensicsReport(ticker=ticker, coin_id=coin_id, name=ticker)

    # --- Step 1: Get price chart ---
    prices = await _get_hourly_prices(coin_id, days=7)
    if not prices:
        raise ValueError(f"No price data available for {ticker}")

    report.current_price = prices[-1][1]
    report.price_points = [
        {"ts": p[0], "price": p[1]} for p in prices
    ]

    # --- Step 2: Detect pump timing ---
    pump = _detect_pump(prices)
    if not pump:
        return report

    report.pump_start_price = pump["start_price"]
    report.pump_peak_price = pump["peak_price"]
    report.pump_magnitude_pct = pump["magnitude_pct"]
    report.pump_start_time = pump["start_time"]
    report.pump_peak_time = pump["peak_time"]

    # --- Step 3: Search Twitter ---
    tweets: list[RawTweet] = []
    try:
        from alpha_bot.ingestion.factory import search_tweets

        tweets = await search_tweets(f"${ticker}", count=settings.research_max_tweets)
        logger.info("Found %d tweets for $%s", len(tweets), ticker)
    except Exception as exc:
        logger.warning("Twitter search failed for %s: %s", ticker, exc)
        report.twitter_available = False

    if not tweets:
        report.signal_score = 0
        report.verdict = _generate_verdict(report)
        return report

    # --- Step 4: Classify tweets onto the pump timeline ---
    signals = [
        _classify_tweet(t, report.pump_start_time, report.pump_peak_time)
        for t in tweets
    ]
    signals.sort(key=lambda s: s.posted_at)

    report.all_signals = signals
    report.total_tweets = len(signals)
    report.pre_pump_tweets = sum(1 for s in signals if s.phase == "pre_pump")
    report.during_pump_tweets = sum(
        1 for s in signals if s.phase == "during_pump"
    )
    report.post_pump_tweets = sum(1 for s in signals if s.phase == "post_pump")

    # --- Step 5: Analyze pre-pump signals ---
    pre_pump = [s for s in signals if s.phase == "pre_pump"]
    if pre_pump:
        report.earliest_mention = pre_pump[0].posted_at
        td = report.pump_start_time - report.earliest_mention
        report.hours_early = td.total_seconds() / 3600

        report.early_mentioners = sorted(
            pre_pump, key=lambda s: s.followers, reverse=True
        )[:10]
        report.pre_pump_sentiment_avg = sum(s.sentiment for s in pre_pump) / len(
            pre_pump
        )
        report.pre_pump_engagement = sum(
            s.likes + s.retweets for s in pre_pump
        )

    # --- Step 6: Score and verdict ---
    report.signal_score = _compute_signal_score(report)
    report.verdict = _generate_verdict(report)

    return report
