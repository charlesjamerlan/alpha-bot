from alpha_bot.ingestion.models import RawTweet
from alpha_bot.scoring.strategies.base import ScoringStrategy

# High-credibility accounts for crypto/macro alpha
TRUSTED_ACCOUNTS: set[str] = {
    "zaborrowka",
    "cburniske",
    "100trillionusd",
    "coin_bureau",
    "raborowska",
    "zlomalpha",
    "pentosh1",
    "cryptomessiah",
    "hsaka",
    "degentrading",
    "zmanian",
    "dikiycryptoelf",
    "trader_xl",
    "inversebrah",
    "cobie",
}

# Follower tier thresholds
TIERS = [
    (1_000_000, 1.0),
    (500_000, 0.85),
    (100_000, 0.7),
    (50_000, 0.55),
    (10_000, 0.4),
    (1_000, 0.25),
]


class CredibilityStrategy(ScoringStrategy):
    def score(self, tweet: RawTweet) -> float:
        username = tweet.author.username.lower()

        # Trusted whitelist gets top score
        if username in TRUSTED_ACCOUNTS:
            return 1.0

        followers = tweet.author.followers_count
        for threshold, tier_score in TIERS:
            if followers >= threshold:
                return tier_score

        return 0.1  # low-follower unknown account
