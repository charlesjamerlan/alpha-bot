import asyncio
import logging

import uvicorn

from alpha_bot.config import settings
from alpha_bot.delivery.telegram_bot import TelegramDelivery
from alpha_bot.delivery.web.app import create_app
from alpha_bot.ingestion.factory import create_ingester
from alpha_bot.ingestion.models import RawTweet
from alpha_bot.research.telegram_group import is_telethon_configured, has_telethon_session
from alpha_bot.scoring.models import ScoreResult
from alpha_bot.scoring.scorer import CompositeScorer
from alpha_bot.storage.database import async_session, init_db
from alpha_bot.storage.models import Signal, Tweet
from alpha_bot.storage.repository import save_signal, save_tweet, tweet_exists
from alpha_bot.utils.logging import setup_logging

logger = logging.getLogger(__name__)


def _tweet_to_orm(raw: RawTweet, score: float) -> Tweet:
    return Tweet(
        tweet_id=raw.tweet_id,
        author_id=raw.author.id,
        author_username=raw.author.username,
        author_name=raw.author.name,
        text=raw.text,
        created_at=raw.created_at,
        follower_count=raw.author.followers_count,
        like_count=raw.metrics.like_count,
        retweet_count=raw.metrics.retweet_count,
        reply_count=raw.metrics.reply_count,
        score=score,
    )


def _signal_from(raw: RawTweet, result: ScoreResult) -> Signal:
    return Signal(
        tweet_id=raw.tweet_id,
        author_username=raw.author.username,
        text=raw.text,
        score=result.overall,
        tickers=",".join(result.tickers),
        sentiment=result.sentiment_label,
        created_at=raw.created_at,
    )


async def _ingest_loop(
    ingester,
    scorer: CompositeScorer,
    delivery: TelegramDelivery | None,
) -> None:
    while True:
        try:
            tweets = await ingester.fetch_new()
            async with async_session() as session:
                for raw in tweets:
                    if await tweet_exists(session, raw.tweet_id):
                        continue

                    result = scorer.score(raw)
                    orm_tweet = _tweet_to_orm(raw, result.overall)
                    await save_tweet(session, orm_tweet)

                    if result.overall >= settings.alpha_threshold:
                        signal = _signal_from(raw, result)
                        await save_signal(session, signal)
                        logger.info(
                            "Signal: @%s | score=%.2f | %s",
                            raw.author.username,
                            result.overall,
                            raw.text[:80],
                        )
                        if delivery:
                            await delivery.send_signal(raw, result)

        except Exception:
            logger.exception("Error in ingest loop")

        await asyncio.sleep(settings.poll_interval_seconds)


async def main() -> None:
    setup_logging()
    logger.info("Starting Alpha Bot")

    await init_db()
    logger.info("Database initialized")

    ingester = create_ingester()
    scorer = CompositeScorer()

    # Telegram: push delivery + command handling (/research)
    delivery: TelegramDelivery | None = None
    tg_app = None
    if settings.telegram_bot_token and settings.telegram_chat_id:
        delivery = TelegramDelivery()
        tg_app = delivery.build_application()
        logger.info("Telegram delivery enabled (with /research command)")
    else:
        logger.warning("Telegram not configured — signals will only be stored in DB")

    # Web dashboard
    app = create_app()
    config = uvicorn.Config(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    # --- Auto-Trading (Maestro integration) ---
    telethon_client = None
    if settings.trading_enabled and is_telethon_configured() and has_telethon_session():
        from telethon import TelegramClient
        from alpha_bot.research.telegram_group import SESSION_FILE
        from alpha_bot.trading.listener import start_listener
        from alpha_bot.trading.position_manager import set_notify_fn
        from alpha_bot.trading.price_monitor import price_monitor_loop

        telethon_client = TelegramClient(
            SESSION_FILE,
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await telethon_client.start()
        logger.info("Telethon client started for auto-trading")

        # Wire notification callback to TG bot
        if delivery:
            set_notify_fn(delivery.send_text)
    elif settings.trading_enabled:
        logger.warning(
            "Trading enabled but Telethon not configured/no session — "
            "run setup_telethon.py first"
        )

    # Share telethon client with TG bot commands (/buy, /sell)
    if tg_app is not None and telethon_client is not None:
        tg_app.bot_data["telethon_client"] = telethon_client

    # Build the list of concurrent tasks
    tasks = [server.serve()]
    if settings.background_ingestion:
        tasks.append(_ingest_loop(ingester, scorer, delivery))
        logger.info("Background ingestion loop enabled (polling every %ds)", settings.poll_interval_seconds)
    else:
        logger.info("Background ingestion disabled — using on-demand /research only (saves API credits)")
    if tg_app is not None:
        # Initialize and start the telegram polling in the background
        async def run_telegram():
            async with tg_app:
                await tg_app.updater.start_polling()
                await tg_app.start()
                logger.info("Telegram bot polling started")
                # Keep running until cancelled
                try:
                    while True:
                        await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await tg_app.updater.stop()
                    await tg_app.stop()

        tasks.append(run_telegram())

    # Trading tasks (only if enabled and telethon is ready)
    if settings.trading_enabled and telethon_client is not None:
        tasks.append(start_listener(telethon_client))
        tasks.append(price_monitor_loop(telethon_client))
        logger.info(
            "Auto-trading ENABLED — monitoring groups: %s",
            settings.telegram_monitor_groups or "(none)",
        )

    await asyncio.gather(*tasks)
