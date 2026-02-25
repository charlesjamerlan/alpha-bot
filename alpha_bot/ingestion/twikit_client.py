"""Twitter data fetcher using twikit (free, no API key)."""

import logging
import os
from datetime import datetime, timezone

from twikit import Client, Capsolver

from alpha_bot.config import settings
from alpha_bot.ingestion.base import BaseIngester
from alpha_bot.ingestion.models import RawTweet, TweetAuthor, TweetMetrics

logger = logging.getLogger(__name__)


class TwikitIngester(BaseIngester):
    def __init__(self) -> None:
        captcha_solver = None
        if settings.capsolver_api_key:
            captcha_solver = Capsolver(api_key=settings.capsolver_api_key)
        self._client = Client("en-US", captcha_solver=captcha_solver)
        self._logged_in = False
        self._since_id: str | None = None

    async def _ensure_login(self) -> None:
        if self._logged_in:
            return

        cookies_path = settings.twikit_cookies_file

        # Try loading saved cookies first
        if os.path.exists(cookies_path):
            try:
                self._client.load_cookies(cookies_path)
                self._logged_in = True
                logger.info("Twikit: loaded session from cookies")
                return
            except Exception:
                logger.debug("Twikit: saved cookies invalid, logging in fresh")

        await self._client.login(
            auth_info_1=settings.twitter_username,
            auth_info_2=settings.twitter_email,
            password=settings.twitter_password,
        )
        self._client.save_cookies(cookies_path)
        self._logged_in = True
        logger.info("Twikit: logged in as @%s", settings.twitter_username)

    async def fetch_new(self) -> list[RawTweet]:
        await self._ensure_login()
        try:
            results = await self._client.search_tweet(
                settings.twitter_search_query, "Latest"
            )
        except Exception as exc:
            logger.error("Twikit search failed: %s", exc)
            self._logged_in = False  # force re-login next time
            return []

        tweets: list[RawTweet] = []
        newest_id: str | None = None

        for tweet in results:
            # Skip tweets we've already seen
            if self._since_id and int(tweet.id) <= int(self._since_id):
                continue

            raw = _parse_tweet(tweet)
            tweets.append(raw)

            if newest_id is None or int(tweet.id) > int(newest_id):
                newest_id = tweet.id

        if newest_id:
            self._since_id = newest_id

        logger.info("Twikit: fetched %d new tweets", len(tweets))
        return tweets

    async def search(self, query: str, count: int = 25) -> list[RawTweet]:
        """On-demand search for the research pipeline."""
        await self._ensure_login()
        try:
            results = await self._client.search_tweet(query, "Latest")
        except Exception as exc:
            logger.error("Twikit search failed: %s", exc)
            self._logged_in = False
            return []

        tweets: list[RawTweet] = []
        for tweet in results:
            tweets.append(_parse_tweet(tweet))
            if len(tweets) >= count:
                break

        logger.info("Twikit: search returned %d tweets for '%s'", len(tweets), query)
        return tweets

    async def get_user_tweets(self, user_id: str, count: int = 20) -> list[RawTweet]:
        """Fetch recent tweets from a specific user."""
        await self._ensure_login()
        try:
            results = await self._client.get_user_tweets(user_id, "Tweets")
        except Exception as exc:
            logger.error("Twikit get_user_tweets failed for %s: %s", user_id, exc)
            return []

        tweets: list[RawTweet] = []
        for tweet in results:
            tweets.append(_parse_tweet(tweet))
            if len(tweets) >= count:
                break

        return tweets


def _parse_tweet(tweet) -> RawTweet:
    """Convert a twikit Tweet object to our RawTweet model."""
    user = tweet.user

    created_at = datetime.now(timezone.utc)
    if tweet.created_at_datetime:
        created_at = tweet.created_at_datetime
    elif tweet.created_at:
        try:
            created_at = datetime.strptime(
                tweet.created_at, "%a %b %d %H:%M:%S %z %Y"
            )
        except (ValueError, TypeError):
            pass

    return RawTweet(
        tweet_id=str(tweet.id),
        text=tweet.text or "",
        created_at=created_at,
        author=TweetAuthor(
            id=str(user.id) if user else "0",
            username=user.screen_name if user else "unknown",
            name=user.name if user else "",
            followers_count=user.followers_count if user else 0,
            verified=getattr(user, "is_blue_verified", False) if user else False,
        ),
        metrics=TweetMetrics(
            like_count=tweet.favorite_count or 0,
            retweet_count=tweet.retweet_count or 0,
            reply_count=tweet.reply_count or 0,
            quote_count=tweet.quote_count or 0,
        ),
    )
