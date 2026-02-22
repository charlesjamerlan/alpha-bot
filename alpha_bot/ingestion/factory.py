"""Factory for creating the right Twitter client based on config."""

import logging

from alpha_bot.config import settings
from alpha_bot.ingestion.models import RawTweet

logger = logging.getLogger(__name__)


def create_ingester():
    """Create the configured ingester (twikit or official API)."""
    if settings.twitter_provider == "twikit":
        from alpha_bot.ingestion.twikit_client import TwikitIngester
        logger.info("Using twikit (free scraper) for Twitter data")
        return TwikitIngester()
    else:
        from alpha_bot.ingestion.twitter import TwitterIngester
        logger.info("Using official X API (paid) for Twitter data")
        return TwitterIngester()


async def search_tweets(query: str, count: int = 25) -> list[RawTweet]:
    """Search tweets using the configured provider."""
    if settings.twitter_provider == "twikit":
        from alpha_bot.ingestion.twikit_client import TwikitIngester
        client = TwikitIngester()
        return await client.search(query, count)
    else:
        from alpha_bot.ingestion.twitter import TwitterIngester
        return _tweepy_search(query, count)


async def get_user_tweets(user_id: str, count: int = 20) -> list[RawTweet]:
    """Get a user's recent tweets using the configured provider."""
    if settings.twitter_provider == "twikit":
        from alpha_bot.ingestion.twikit_client import TwikitIngester
        client = TwikitIngester()
        return await client.get_user_tweets(user_id, count)
    else:
        return _tweepy_user_tweets(user_id, count)


def _tweepy_search(query: str, count: int) -> list[RawTweet]:
    """Sync tweepy search (called from async context via pipeline)."""
    import tweepy
    from alpha_bot.ingestion.twitter import _parse_tweepy_response

    client = tweepy.Client(
        bearer_token=settings.twitter_bearer_token, wait_on_rate_limit=True
    )
    response = client.search_recent_tweets(
        query=query,
        max_results=min(count, 100),
        tweet_fields=["created_at", "public_metrics", "author_id"],
        user_fields=["username", "name", "public_metrics", "verified"],
        expansions=["author_id"],
    )
    return _parse_tweepy_response(response)


def _tweepy_user_tweets(user_id: str, count: int) -> list[RawTweet]:
    """Sync tweepy user tweets lookup."""
    import tweepy
    from alpha_bot.ingestion.twitter import _parse_tweepy_response

    client = tweepy.Client(
        bearer_token=settings.twitter_bearer_token, wait_on_rate_limit=True
    )
    response = client.get_users_tweets(
        id=user_id,
        max_results=min(count, 100),
        tweet_fields=["created_at", "public_metrics"],
    )
    return _parse_tweepy_response(response)
