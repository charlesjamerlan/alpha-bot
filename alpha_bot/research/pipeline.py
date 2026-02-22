"""Main research pipeline — orchestrates all research components for a ticker."""

import json
import logging

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from alpha_bot.config import settings
from alpha_bot.ingestion.factory import search_tweets
from alpha_bot.ingestion.models import RawTweet
from alpha_bot.research.coingecko import get_price_snapshot
from alpha_bot.research.narratives import extract_co_mentioned_tickers, top_narratives
from alpha_bot.research.report import BuzzStats, ResearchReport
from alpha_bot.research.risk import analyze_risk
from alpha_bot.research.smart_money import (
    SmartMoneyAccount,
    SmartMoneyReport,
    expand_accounts,
    identify_top_accounts,
)
from alpha_bot.research.summarizer import summarize_research

logger = logging.getLogger(__name__)
_vader = SentimentIntensityAnalyzer()


def _build_search_query(ticker: str) -> str:
    clean = ticker.upper().strip("$")
    return f"(${clean} OR #{clean}) -is:retweet lang:en"


def _compute_buzz(tweets: list[RawTweet]) -> BuzzStats:
    stats = BuzzStats(total_tweets=len(tweets))

    for tweet in tweets:
        compound = _vader.polarity_scores(tweet.text)["compound"]
        if compound >= 0.05:
            stats.bullish_count += 1
        elif compound <= -0.05:
            stats.bearish_count += 1
        else:
            stats.neutral_count += 1

        stats.total_engagement += (
            tweet.metrics.like_count
            + tweet.metrics.retweet_count
            + tweet.metrics.reply_count
        )

    sorted_tweets = sorted(
        tweets,
        key=lambda t: t.metrics.like_count + t.metrics.retweet_count * 2,
        reverse=True,
    )
    stats.top_tweets = [
        {
            "text": t.text[:200],
            "author": t.author.username,
            "likes": t.metrics.like_count,
            "retweets": t.metrics.retweet_count,
            "followers": t.author.followers_count,
        }
        for t in sorted_tweets[:10]
    ]

    return stats


async def run_research(ticker: str) -> ResearchReport:
    """Execute the full research pipeline for a ticker."""
    clean = ticker.upper().strip("$")
    logger.info("Starting research for $%s (provider: %s)", clean, settings.twitter_provider)
    report = ResearchReport(ticker=clean)

    # 1. Price snapshot
    report.snapshot = await get_price_snapshot(clean)
    if report.snapshot:
        logger.info("Got price data for $%s", clean)
    else:
        logger.warning("No price data for $%s", clean)

    # 2. Search Twitter (uses configured provider)
    query = _build_search_query(clean)
    try:
        tweets = await search_tweets(query, settings.research_max_tweets)
        logger.info("Found %d tweets for $%s", len(tweets), clean)
    except Exception as exc:
        logger.error("Twitter search failed for $%s: %s", clean, exc)
        tweets = []

    if not tweets:
        report.llm_summary = (
            f"No tweets found for ${clean}. "
            "The token may be too new or the ticker may be incorrect."
        )
        await _save_report(report)
        return report

    # 3. Buzz stats
    report.buzz = _compute_buzz(tweets)

    # 4. Smart money
    top_accounts = identify_top_accounts(tweets)
    try:
        report.smart_money = await expand_accounts(top_accounts, clean)
    except Exception as exc:
        logger.error("Smart money expansion failed: %s", exc)
        report.smart_money = SmartMoneyReport()
        for acct in top_accounts[: settings.smart_money_expand_count]:
            author = acct["author"]
            report.smart_money.top_accounts.append(
                SmartMoneyAccount(
                    username=author.username,
                    name=author.name,
                    followers=author.followers_count,
                    tweet_count_about_ticker=len(acct["tweets"]),
                    first_mention=len(acct["tweets"]) <= 1,
                )
            )

    # 5. Narratives
    all_tweets = tweets + report.smart_money.adjacent_tweets
    report.narratives = top_narratives(all_tweets)
    report.co_mentioned_tickers = extract_co_mentioned_tickers(tweets, clean)

    # 6. Risk
    report.risk = analyze_risk(tweets)

    # 7. LLM summary
    report.llm_summary = await summarize_research(report.to_dict())

    # 8. Persist to DB
    await _save_report(report)

    logger.info(
        "Research complete for $%s: %d tweets, risk=%s",
        clean,
        len(tweets),
        report.risk.risk_level,
    )
    return report


async def _save_report(report: ResearchReport) -> None:
    from alpha_bot.storage.database import async_session
    from alpha_bot.storage.models import ResearchReportRow
    from alpha_bot.storage.repository import save_research_report

    report_dict = report.to_dict()
    row = ResearchReportRow(
        ticker=report.ticker,
        snapshot_json=json.dumps(report_dict.get("snapshot") or {}),
        buzz_json=json.dumps(report_dict.get("buzz", {})),
        smart_money_json=json.dumps(report_dict.get("smart_money", {})),
        narratives_json=json.dumps(report_dict.get("narratives", [])),
        co_mentioned_json=json.dumps(report_dict.get("co_mentioned_tickers", [])),
        risk_json=json.dumps(report_dict.get("risk", {})),
        llm_summary=report.llm_summary,
        report_json=json.dumps(report_dict),
    )
    try:
        async with async_session() as session:
            await save_research_report(session, row)
            logger.info("Saved research report for $%s to DB", report.ticker)
    except Exception as exc:
        logger.error("Failed to save report for $%s: %s", report.ticker, exc)
