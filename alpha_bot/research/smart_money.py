"""Smart Money Radar — identify key accounts and expand to their adjacent interests."""

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from alpha_bot.config import settings
from alpha_bot.ingestion.factory import get_user_tweets
from alpha_bot.ingestion.models import RawTweet, TweetAuthor

logger = logging.getLogger(__name__)

TICKER_RE = re.compile(r"\$([A-Z]{2,6})\b")


@dataclass
class SmartMoneyAccount:
    username: str
    name: str
    followers: int
    tweet_count_about_ticker: int
    first_mention: bool
    adjacent_tickers: list[str] = field(default_factory=list)
    adjacent_topics: list[str] = field(default_factory=list)


@dataclass
class SmartMoneyReport:
    top_accounts: list[SmartMoneyAccount] = field(default_factory=list)
    adjacent_tickers: list[tuple[str, int]] = field(default_factory=list)
    adjacent_tweets: list[RawTweet] = field(default_factory=list)


def identify_top_accounts(
    tweets: list[RawTweet], top_n: int | None = None
) -> list[dict]:
    if top_n is None:
        top_n = settings.smart_money_expand_count

    account_data: dict[str, dict] = {}

    for tweet in tweets:
        uid = tweet.author.username.lower()
        if uid not in account_data:
            account_data[uid] = {
                "author": tweet.author,
                "tweets": [],
                "total_engagement": 0,
            }
        account_data[uid]["tweets"].append(tweet)
        account_data[uid]["total_engagement"] += (
            tweet.metrics.like_count
            + tweet.metrics.retweet_count * 2
            + tweet.metrics.reply_count
        )

    ranked = sorted(
        account_data.values(),
        key=lambda d: d["author"].followers_count * math.log1p(d["total_engagement"]),
        reverse=True,
    )

    return ranked[:top_n]


async def expand_accounts(
    top_accounts: list[dict],
    target_ticker: str,
) -> SmartMoneyReport:
    """For each top account, pull their recent tweets and find adjacent interests."""
    report = SmartMoneyReport()
    target_upper = target_ticker.upper().strip("$")
    adjacent_counter: Counter[str] = Counter()

    for acct_data in top_accounts:
        author: TweetAuthor = acct_data["author"]

        try:
            user_tweets = await get_user_tweets(
                author.id, settings.smart_money_recent_tweets
            )
        except Exception as exc:
            logger.warning("Could not fetch tweets for @%s: %s", author.username, exc)
            continue

        if not user_tweets:
            continue

        mentions_target = len(acct_data["tweets"])
        other_tickers: list[str] = []

        for tw in user_tweets:
            found = set(TICKER_RE.findall(tw.text))
            found.discard(target_upper)
            other_tickers.extend(found)
            adjacent_counter.update(found)

            # Collect as adjacent context
            if target_upper not in set(TICKER_RE.findall(tw.text)):
                report.adjacent_tweets.append(tw)

        sm = SmartMoneyAccount(
            username=author.username,
            name=author.name,
            followers=author.followers_count,
            tweet_count_about_ticker=mentions_target,
            first_mention=mentions_target <= 1,
            adjacent_tickers=list(set(other_tickers))[:10],
        )
        report.top_accounts.append(sm)

    report.adjacent_tickers = adjacent_counter.most_common(15)
    return report
