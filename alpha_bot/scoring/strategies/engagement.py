import math

from alpha_bot.ingestion.models import RawTweet
from alpha_bot.scoring.strategies.base import ScoringStrategy


class EngagementStrategy(ScoringStrategy):
    def score(self, tweet: RawTweet) -> float:
        m = tweet.metrics
        followers = tweet.author.followers_count

        total_engagement = m.like_count + m.retweet_count * 2 + m.reply_count
        if total_engagement == 0:
            return 0.0

        if followers > 0:
            ratio = total_engagement / followers
            # A ratio > 0.05 is considered high engagement
            score = min(ratio / 0.05, 1.0)
        else:
            # No follower data — use raw engagement with log scaling
            score = min(math.log1p(total_engagement) / math.log1p(1000), 1.0)

        return round(score, 4)
