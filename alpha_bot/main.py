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
import alpha_bot.tg_intel.models  # noqa: F401 — register CallOutcome/ChannelScore with Base
import alpha_bot.scanner.models  # noqa: F401 — register TrendingTheme/ScannerCandidate with Base
import alpha_bot.platform_intel.models  # noqa: F401 — register PlatformToken with Base
import alpha_bot.scoring_engine.models  # noqa: F401 — register BacktestRun/ScoringWeights with Base
import alpha_bot.wallets.models  # noqa: F401 — register PrivateWallet/WalletTransaction/WalletCluster with Base
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

    # --- Telethon client (TG group monitoring + trading) ---
    telethon_client = None
    if is_telethon_configured() and has_telethon_session():
        from telethon import TelegramClient
        from alpha_bot.research.telegram_group import SESSION_FILE
        from alpha_bot.trading.listener import start_listener

        telethon_client = TelegramClient(
            SESSION_FILE,
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await telethon_client.start()
        logger.info("Telethon client started for TG monitoring")

        # Wire trading notifications
        if settings.trading_enabled and delivery:
            from alpha_bot.trading.position_manager import set_notify_fn
            set_notify_fn(delivery.send_text)
    elif settings.trading_enabled:
        logger.warning(
            "Trading enabled but Telethon not configured/no session — "
            "run setup_telethon.py first"
        )

    # Wire convergence notifications (works even without trading enabled)
    if delivery:
        from alpha_bot.tg_intel.convergence import set_notify_fn as set_convergence_notify
        set_convergence_notify(delivery.send_text)

    # Wire reaction velocity notifications + set Telethon client on recorder
    if telethon_client is not None:
        from alpha_bot.tg_intel.recorder import set_telethon_client
        set_telethon_client(telethon_client)

    if delivery:
        from alpha_bot.tg_intel.reaction_velocity import set_notify_fn as set_rv_notify
        set_rv_notify(delivery.send_text)

    # Load reaction baselines from DB (avoid cold-start)
    from alpha_bot.tg_intel.reaction_velocity import load_baselines_from_db
    await load_baselines_from_db()

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

    # TG listener (always runs when Telethon is ready — records calls + convergence)
    if telethon_client is not None:
        tasks.append(start_listener(telethon_client))
        logger.info(
            "TG listener active — monitoring groups: %s",
            settings.telegram_monitor_groups or "(none)",
        )

    # Price monitor (only when trading is enabled)
    if settings.trading_enabled and telethon_client is not None:
        from alpha_bot.trading.price_monitor import price_monitor_loop
        tasks.append(price_monitor_loop(telethon_client))
        logger.info("Auto-trading ENABLED")

    # Scanner (Phase 1: Narrative Radar + Active Scanner)
    if settings.scanner_enabled:
        from alpha_bot.scanner.alerts import set_notify_fn as set_scanner_notify
        from alpha_bot.scanner.daily_digest import daily_digest_loop
        from alpha_bot.scanner.scanner_loop import scanner_loop
        from alpha_bot.scanner.trend_tracker import trend_tracker_loop

        if delivery:
            set_scanner_notify(delivery.send_text)

        tasks.append(trend_tracker_loop())
        tasks.append(scanner_loop())
        tasks.append(daily_digest_loop())

        # Watchlist degradation monitor (runs alongside scanner)
        from alpha_bot.scanner.watchlist_monitor import (
            watchlist_monitor_loop,
            set_notify_fn as set_watchlist_notify,
        )
        if delivery:
            set_watchlist_notify(delivery.send_text)
        tasks.append(watchlist_monitor_loop())

        logger.info("Scanner ENABLED (trend poll=%ds, scan poll=%ds)",
                     settings.trend_poll_interval_seconds,
                     settings.scanner_poll_interval_seconds)
    else:
        logger.info("Scanner disabled — set SCANNER_ENABLED=true to activate")

    # Platform Intel (Phase 2: Clanker scraper + lifecycle checks)
    if settings.clanker_scraper_enabled:
        from alpha_bot.platform_intel.clanker_scraper import (
            clanker_scraper_loop,
            platform_check_loop,
        )

        tasks.append(clanker_scraper_loop())
        tasks.append(platform_check_loop())
        logger.info(
            "Platform intel ENABLED (scrape=%ds, checks=%ds)",
            settings.clanker_scraper_interval_seconds,
            settings.platform_check_interval_seconds,
        )
    else:
        logger.info("Platform intel disabled — set CLANKER_SCRAPER_ENABLED=true")

    # Recalibration (Phase 3: weight auto-adjustment)
    if settings.recalibrate_enabled:
        from alpha_bot.scoring_engine.recalibrate import (
            recalibrate_loop,
            set_notify_fn as set_recal_notify,
        )

        if delivery:
            set_recal_notify(delivery.send_text)

        tasks.append(recalibrate_loop())
        logger.info(
            "Recalibration ENABLED (interval=%ds)",
            settings.recalibrate_interval_seconds,
        )
    else:
        logger.info("Recalibration disabled — set RECALIBRATE_ENABLED=true")

    # Private Wallet Curation (Phase 4)
    if settings.wallet_curation_enabled:
        from alpha_bot.wallets.reverse_engineer import (
            reverse_engineer_loop,
            set_notify_fn as set_wallet_notify,
        )
        from alpha_bot.wallets.decay_monitor import (
            decay_monitor_loop,
            set_notify_fn as set_decay_notify,
        )

        if delivery:
            set_wallet_notify(delivery.send_text)
            set_decay_notify(delivery.send_text)

        tasks.append(reverse_engineer_loop())
        tasks.append(decay_monitor_loop())
        logger.info(
            "Wallet curation ENABLED (scan=%ds, decay=%ds)",
            settings.wallet_scan_interval_seconds,
            settings.wallet_decay_interval_seconds,
        )
    else:
        logger.info("Wallet curation disabled — set WALLET_CURATION_ENABLED=true")

    await asyncio.gather(*tasks)
