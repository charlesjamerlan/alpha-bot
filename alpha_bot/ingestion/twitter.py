"""Twitter data fetcher using the official X API v2 (paid)."""

import logging
from datetime import datetime, timezone

import tweepy

from alpha_bot.config import settings
from alpha_bot.ingestion.base import BaseIngester
from alpha_bot.ingestion.models import RawTweet, TweetAuthor, TweetMetrics

logger = logging.getLogger(__name__)


def _parse_tweepy_response(response: tweepy.Response) -> list[RawTweet]:
    """Convert a tweepy Response into a list of RawTweet models."""
    if not response.data:
        return []

    users_map: dict[str, tweepy.User] = {}
    if response.includes and "users" in response.includes:
        for user in response.includes["users"]:
            users_map[user.id] = user

    tweets: list[RawTweet] = []
    for tweet in response.data:
        user = users_map.get(tweet.author_id)
        metrics = tweet.public_metrics or {}

        author = TweetAuthor(
            id=str(tweet.author_id),
            username=user.username if user else "unknown",
            name=user.name if user else "",
            followers_count=(
                user.public_metrics.get("followers_count", 0)
                if user and user.public_metrics
                else 0
            ),
            verified=getattr(user, "verified", False) if user else False,
        )

        raw = RawTweet(
            tweet_id=str(tweet.id),
            text=tweet.text,
            created_at=tweet.created_at or datetime.now(timezone.utc),
            author=author,
            metrics=TweetMetrics(
                like_count=metrics.get("like_count", 0),
                retweet_count=metrics.get("retweet_count", 0),
                reply_count=metrics.get("reply_count", 0),
                quote_count=metrics.get("quote_count", 0),
            ),
        )
        tweets.append(raw)

    return tweets


class TwitterIngester(BaseIngester):
    def __init__(self) -> None:
        self._client = tweepy.Client(
            bearer_token=settings.twitter_bearer_token, wait_on_rate_limit=True
        )
        self._since_id: str | None = None

    async def fetch_new(self) -> list[RawTweet]:
        try:
            response = self._client.search_recent_tweets(
                query=settings.twitter_search_query,
                max_results=100,
                since_id=self._since_id,
                tweet_fields=["created_at", "public_metrics", "author_id"],
                user_fields=["username", "name", "public_metrics", "verified"],
                expansions=["author_id"],
            )
        except tweepy.TweepyException as exc:
            logger.error("Twitter API error: %s", exc)
            return []

        tweets = _parse_tweepy_response(response)

        if tweets:
            newest_id = max(tweets, key=lambda t: int(t.tweet_id)).tweet_id
            self._since_id = newest_id

        logger.info("Fetched %d new tweets", len(tweets))
        return tweets
