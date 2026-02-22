"""Detect risk signals: FUD, scam warnings, bot/shill patterns."""

import re
from dataclasses import dataclass, field

from alpha_bot.ingestion.models import RawTweet

FUD_KEYWORDS = [
    "rug", "rugged", "rugpull", "rug pull",
    "scam", "ponzi", "fraud",
    "hack", "hacked", "exploit", "exploited", "drained",
    "dump", "dumping", "sell off",
    "sec lawsuit", "investigation", "subpoena",
    "insolvent", "bankrupt",
    "depeg", "depegged",
    "team left", "dev abandoned", "dead project",
    "honeypot", "fake",
]

SHILL_PATTERNS = [
    r"(?i)guaranteed\s+\d+x",
    r"(?i)easy\s+\d+x",
    r"(?i)free\s+(airdrop|money|tokens)",
    r"(?i)send\s+\d+.*get\s+\d+.*back",
    r"(?i)dm\s+me\s+(for|to)",
    r"(?i)join\s+(my|our)\s+(group|telegram|discord)",
    r"(?i)last\s+chance",
    r"(?i)act\s+now",
]
_shill_compiled = [re.compile(p) for p in SHILL_PATTERNS]


@dataclass
class RiskReport:
    fud_tweets: list[RawTweet] = field(default_factory=list)
    shill_tweets: list[RawTweet] = field(default_factory=list)
    low_quality_ratio: float = 0.0  # % of mentions from accounts < 100 followers
    fud_ratio: float = 0.0
    risk_level: str = "low"  # low / medium / high
    warnings: list[str] = field(default_factory=list)


def analyze_risk(tweets: list[RawTweet]) -> RiskReport:
    if not tweets:
        return RiskReport()

    report = RiskReport()
    low_quality_count = 0

    for tweet in tweets:
        text = tweet.text.lower()

        # FUD detection
        if any(kw in text for kw in FUD_KEYWORDS):
            report.fud_tweets.append(tweet)

        # Shill/bot detection
        if any(p.search(tweet.text) for p in _shill_compiled):
            report.shill_tweets.append(tweet)

        # Low-quality account
        if tweet.author.followers_count < 100:
            low_quality_count += 1

    total = len(tweets)
    report.fud_ratio = len(report.fud_tweets) / total
    report.low_quality_ratio = low_quality_count / total

    # Determine risk level
    if report.fud_ratio > 0.3:
        report.risk_level = "high"
        report.warnings.append(
            f"High FUD: {report.fud_ratio:.0%} of tweets contain negative signals"
        )
    elif report.fud_ratio > 0.1:
        report.risk_level = "medium"
        report.warnings.append(
            f"Moderate FUD detected ({report.fud_ratio:.0%} of tweets)"
        )

    if report.low_quality_ratio > 0.6:
        report.risk_level = "high"
        report.warnings.append(
            f"Shill alert: {report.low_quality_ratio:.0%} of mentions from accounts with <100 followers"
        )
    elif report.low_quality_ratio > 0.3:
        if report.risk_level != "high":
            report.risk_level = "medium"
        report.warnings.append(
            f"Low-quality skew: {report.low_quality_ratio:.0%} from small accounts"
        )

    if len(report.shill_tweets) > 3:
        if report.risk_level != "high":
            report.risk_level = "medium"
        report.warnings.append(
            f"Shill patterns detected in {len(report.shill_tweets)} tweets"
        )

    return report
