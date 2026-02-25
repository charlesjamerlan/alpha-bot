import logging
from datetime import datetime

import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import httpx

from alpha_bot.config import settings
from alpha_bot.delivery.base import DeliveryChannel
from alpha_bot.ingestion.models import RawTweet
from alpha_bot.research.dexscreener import extract_pair_details, get_token_by_address
from alpha_bot.research.pipeline import run_research
from alpha_bot.research.pnl_analyzer import PnLReport, analyze_pnl
from alpha_bot.research.telegram_group import (
    extract_contract_addresses,
    has_telethon_session,
    is_telethon_configured,
    scrape_group_history,
)
from alpha_bot.scoring.models import ScoreResult
from alpha_bot.storage.database import async_session
from alpha_bot.storage.repository import get_open_positions
from alpha_bot.tg_intel.models import ChannelScore

logger = logging.getLogger(__name__)


class TelegramDelivery(DeliveryChannel):
    """Handles both push notifications and /research command."""

    def __init__(self) -> None:
        self._bot = telegram.Bot(token=settings.telegram_bot_token)
        self._chat_id = settings.telegram_chat_id
        self._app: Application | None = None

    async def send_signal(self, tweet: RawTweet, score: ScoreResult) -> None:
        tickers = ", ".join(f"${t}" for t in score.tickers) if score.tickers else "—"

        text = (
            f"🚨 <b>Alpha Signal</b> (score: {score.overall:.2f})\n\n"
            f"<b>@{tweet.author.username}</b> ({tweet.author.followers_count:,} followers)\n"
            f"{tweet.text}\n\n"
            f"Tickers: {tickers}\n"
            f"Sentiment: {score.sentiment_label}\n"
            f"📊 KW={score.keyword:.2f} | SENT={score.sentiment:.2f} | "
            f"ENG={score.engagement:.2f} | CRED={score.credibility:.2f}"
        )

        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
            )
            logger.info("Telegram signal sent for tweet %s", tweet.tweet_id)
        except telegram.error.TelegramError as exc:
            logger.error("Telegram delivery failed: %s", exc)

    async def send_text(self, text: str, parse_mode: str = "HTML") -> None:
        try:
            # Telegram has a 4096 char limit — split if needed
            for i in range(0, len(text), 4000):
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=text[i : i + 4000],
                    parse_mode=parse_mode,
                )
        except telegram.error.TelegramError as exc:
            logger.error("Telegram send failed: %s", exc)

    def build_application(self) -> Application:
        """Build the telegram Application with command handlers."""
        self._app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .build()
        )
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("research", self._cmd_research))
        self._app.add_handler(CommandHandler("token", self._cmd_token))
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("buy", self._cmd_buy))
        self._app.add_handler(CommandHandler("sell", self._cmd_sell))
        self._app.add_handler(CommandHandler("trading", self._cmd_trading))
        self._app.add_handler(CommandHandler("channels", self._cmd_channels))
        self._app.add_handler(CommandHandler("convergence", self._cmd_convergence))
        self._app.add_handler(CommandHandler("profile", self._cmd_profile))
        self._app.add_handler(CommandHandler("trends", self._cmd_trends))
        self._app.add_handler(CommandHandler("scan", self._cmd_scan))
        self._app.add_handler(CommandHandler("platform", self._cmd_platform))
        self._app.add_handler(CommandHandler("backtest", self._cmd_backtest))
        self._app.add_handler(CommandHandler("weights", self._cmd_weights))
        self._app.add_handler(CommandHandler("wallets", self._cmd_wallets))
        self._app.add_handler(CommandHandler("clusters", self._cmd_clusters))
        self._app.add_handler(CommandHandler("watchlist", self._cmd_watchlist))
        self._app.add_handler(CommandHandler("active", self._cmd_active))
        self._app.add_handler(CommandHandler("exit_check", self._cmd_exit_check))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        return self._app

    @staticmethod
    async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🤖 <b>Alpha Bot</b>\n\n"
            "<b>Research:</b>\n"
            "/research &lt;ticker&gt; — Full research report\n"
            "/token &lt;CA&gt; — DexScreener token lookup\n"
            "/pnl &lt;group&gt; [days] — TG group P/L analysis\n"
            "/channels — TG channel quality rankings\n"
            "/convergence — Recent cross-channel convergences\n"
            "/profile — Winning call profile (Mode 2)\n\n"
            "<b>Scanner:</b>\n"
            "/trends — Current trending themes\n"
            "/scan &lt;CA&gt; — Full scanner score breakdown\n"
            "/platform &lt;CA&gt; — Platform cohort percentile\n"
            "/watchlist — Tier 2 tokens being monitored\n\n"
            "<b>Scoring:</b>\n"
            "/backtest [days] — Run backtest simulation\n"
            "/weights — Show current scoring weights\n\n"
            "<b>Wallets:</b>\n"
            "/wallets — Top private wallets\n"
            "/clusters — Wallet cluster summary\n\n"
            "<b>Trading:</b>\n"
            "/positions — List open positions\n"
            "/active — Tokens you've entered (alias for /positions)\n"
            "/exit_check &lt;CA&gt; — Check exit conditions for a position\n"
            "/buy &lt;CA&gt; — Manual buy via Maestro\n"
            "/sell &lt;CA&gt; [pct] — Manual sell via Maestro\n"
            "/trading on|off — Toggle auto-trading\n\n"
            "<b>System:</b>\n"
            "/status — System health dashboard\n"
            "/help — Show this message",
            parse_mode="HTML",
        )

    @staticmethod
    async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "<b>Usage:</b>\n\n"
            "<b>Research:</b>\n"
            "<code>/research SOL</code> — Research $SOL\n"
            "<code>/token CA_ADDRESS</code> — DexScreener lookup\n"
            "<code>/pnl cryptogroup 30</code> — Analyze last 30 days\n"
            "<code>/channels</code> — Show channel quality rankings\n"
            "<code>/convergence</code> — Recent cross-channel signals\n"
            "<code>/profile</code> — Winning call profile (Mode 2)\n\n"
            "<b>Scanner:</b>\n"
            "<code>/trends</code> — Current trending themes\n"
            "<code>/scan CA_ADDRESS</code> — Full scanner score for a token\n"
            "<code>/platform CA_ADDRESS</code> — Platform cohort percentile\n"
            "<code>/watchlist</code> — Tier 2 watchlist tokens\n\n"
            "<b>Scoring:</b>\n"
            "<code>/backtest 14</code> — Backtest last 14 days\n"
            "<code>/weights</code> — Show current scoring weights\n\n"
            "<b>Wallets:</b>\n"
            "<code>/wallets</code> — Top private wallets by quality\n"
            "<code>/clusters</code> — Wallet cluster summary\n\n"
            "<b>Trading:</b>\n"
            "<code>/positions</code> — Show open positions with P/L\n"
            "<code>/active</code> — Alias for /positions\n"
            "<code>/exit_check CA_ADDRESS</code> — Check exit conditions\n"
            "<code>/buy CA_ADDRESS</code> — Send buy to Maestro bot\n"
            "<code>/sell CA_ADDRESS 50</code> — Sell 50% via Maestro\n"
            "<code>/trading on</code> — Enable auto-trading\n"
            "<code>/trading off</code> — Disable auto-trading\n\n"
            "<b>System:</b>\n"
            "<code>/status</code> — System health dashboard",
            parse_mode="HTML",
        )

    @staticmethod
    async def _cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/research TICKER</code>\nExample: <code>/research SOL</code>",
                parse_mode="HTML",
            )
            return

        ticker = context.args[0].upper().strip("$")
        await update.message.reply_text(
            f"🔍 Researching <b>${ticker}</b>… this may take a minute.",
            parse_mode="HTML",
        )

        try:
            report = await run_research(ticker)
            await update.message.reply_text(
                report.format_telegram(), parse_mode="HTML"
            )
        except Exception as exc:
            logger.exception("Research command failed for %s", ticker)
            await update.message.reply_text(
                f"❌ Research failed: {exc}", parse_mode="HTML"
            )

    @staticmethod
    async def _cmd_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/token CONTRACT_ADDRESS</code>\n"
                "Example: <code>/token DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263</code>",
                parse_mode="HTML",
            )
            return

        ca = context.args[0].strip()

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                pair = await get_token_by_address(ca, client)
        except Exception as exc:
            logger.exception("Token lookup failed for %s", ca[:12])
            await update.message.reply_text(
                f"❌ Lookup failed: {exc}", parse_mode="HTML"
            )
            return

        if not pair:
            await update.message.reply_text(
                f"❌ No token found for <code>{ca[:16]}…</code>\n"
                "Check the contract address and try again.",
                parse_mode="HTML",
            )
            return

        d = extract_pair_details(pair)
        chain = pair.get("chainId", "?")

        price_str = f"${d['price_usd']:.10g}" if d["price_usd"] else "N/A"
        mcap_str = _fmt_mcap(d["market_cap"])
        liq_str = _fmt_mcap(d["liquidity_usd"])
        vol_str = _fmt_mcap(d["volume_24h"])

        changes = []
        for label, key in [("5m", "price_change_5m"), ("1h", "price_change_1h"),
                           ("6h", "price_change_6h"), ("24h", "price_change_24h")]:
            val = d.get(key)
            if val is not None:
                emoji = "🟢" if val >= 0 else "🔴"
                changes.append(f"{emoji} {label}: {val:+.1f}%")

        changes_str = " | ".join(changes) if changes else "N/A"

        text = (
            f"🔎 <b>{d['symbol']}</b> ({d['name']})\n"
            f"Chain: {chain} | DEX: {d['dex']}\n\n"
            f"💰 Price: <b>{price_str}</b>\n"
            f"📊 MCap: {mcap_str} | Liq: {liq_str}\n"
            f"📈 Vol 24h: {vol_str}\n\n"
            f"{changes_str}\n\n"
            f"<code>{ca}</code>"
        )

        await update.message.reply_text(text, parse_mode="HTML")

    @staticmethod
    async def _cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/pnl GROUP [DAYS]</code>\n"
                "Example: <code>/pnl cryptoalpha</code>\n"
                "Example: <code>/pnl cryptoalpha 30</code>",
                parse_mode="HTML",
            )
            return

        if not is_telethon_configured():
            await update.message.reply_text(
                "❌ Telethon not configured. Set TELEGRAM_API_ID and "
                "TELEGRAM_API_HASH in .env, then run <code>python setup_telethon.py</code>.",
                parse_mode="HTML",
            )
            return

        if not has_telethon_session():
            await update.message.reply_text(
                "❌ No Telethon session found. Run <code>python setup_telethon.py</code> first.",
                parse_mode="HTML",
            )
            return

        group = context.args[0]
        days = 90
        if len(context.args) > 1:
            try:
                days = int(context.args[1])
            except ValueError:
                await update.message.reply_text(
                    "❌ Days must be a number. Example: <code>/pnl cryptoalpha 30</code>",
                    parse_mode="HTML",
                )
                return

        await update.message.reply_text(
            f"📊 Analyzing <b>{group}</b> (last {days} days)…\n"
            "This may take several minutes depending on group size and number of tickers.",
            parse_mode="HTML",
        )

        try:
            calls = await scrape_group_history(group, days_back=days)
            if not calls:
                await update.message.reply_text(
                    f"No ticker calls found in <b>{group}</b> over the last {days} days.",
                    parse_mode="HTML",
                )
                return

            await update.message.reply_text(
                f"Found <b>{len(calls)}</b> ticker mentions. "
                "Fetching price data from CoinGecko…",
                parse_mode="HTML",
            )

            report = await analyze_pnl(calls, group_name=group, days_back=days)
            text = _format_pnl_telegram(report)

            # Send in chunks (Telegram 4096 char limit)
            for i in range(0, len(text), 4000):
                await update.message.reply_text(
                    text[i : i + 4000], parse_mode="HTML"
                )
        except Exception as exc:
            logger.exception("P/L command failed for %s", group)
            await update.message.reply_text(
                f"❌ P/L analysis failed: {exc}", parse_mode="HTML"
            )


    @staticmethod
    async def _cmd_positions(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            async with async_session() as session:
                positions = await get_open_positions(session)

            if not positions:
                await update.message.reply_text(
                    "No open positions.", parse_mode="HTML"
                )
                return

            lines = [f"<b>Open Positions ({len(positions)})</b>\n"]
            for p in positions:
                pnl = p.unrealized_pnl_pct
                emoji = "🟢" if pnl >= 0 else "🔴"
                tp_flags = []
                if p.tp1_hit:
                    tp_flags.append("TP1")
                if p.tp2_hit:
                    tp_flags.append("TP2")
                if p.tp3_hit:
                    tp_flags.append("TP3")
                tp_str = f" [{', '.join(tp_flags)}]" if tp_flags else ""

                lines.append(
                    f"{emoji} <b>${p.token_symbol or p.token_mint[:8]}</b> "
                    f"{pnl:+.1f}%{tp_str}\n"
                    f"   Entry: ${p.entry_price_usd:.8f} | Now: ${p.current_price_usd:.8f}\n"
                    f"   <code>{p.token_mint}</code>"
                )

            text = "\n".join(lines)
            for i in range(0, len(text), 4000):
                await update.message.reply_text(
                    text[i : i + 4000], parse_mode="HTML"
                )
        except Exception as exc:
            logger.exception("Positions command failed")
            await update.message.reply_text(
                f"❌ Failed: {exc}", parse_mode="HTML"
            )

    @staticmethod
    async def _cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/buy CONTRACT_ADDRESS</code>",
                parse_mode="HTML",
            )
            return

        ca = context.args[0].strip()
        addresses = extract_contract_addresses(ca)
        if not addresses:
            await update.message.reply_text(
                "❌ Invalid contract address.", parse_mode="HTML"
            )
            return

        from alpha_bot.trading.models import TradeSignal
        from alpha_bot.trading.position_manager import handle_signal

        # Get the telethon client from app context (set in main.py)
        telethon_client = context.application.bot_data.get("telethon_client")
        if not telethon_client:
            await update.message.reply_text(
                "❌ Trading not initialized (Telethon client not available).",
                parse_mode="HTML",
            )
            return

        ca = addresses[0]
        # Detect chain from CA format: 0x prefix = EVM (base/eth), else Solana
        chain = "base" if ca.startswith("0x") else "solana"

        signal = TradeSignal(
            token_mint=ca,
            chain=chain,
            source_group="manual",
            author="manual",
        )

        await update.message.reply_text(
            f"Sending buy to Maestro for <code>{ca}</code> ({chain})...",
            parse_mode="HTML",
        )
        await handle_signal(signal, telethon_client)

    @staticmethod
    async def _cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/sell CONTRACT_ADDRESS [PERCENT]</code>\n"
                "Example: <code>/sell ABC...xyz 50</code>",
                parse_mode="HTML",
            )
            return

        ca = context.args[0].strip()
        sell_pct = 100
        if len(context.args) > 1:
            try:
                sell_pct = int(context.args[1])
            except ValueError:
                await update.message.reply_text(
                    "❌ Percent must be a number.", parse_mode="HTML"
                )
                return

        telethon_client = context.application.bot_data.get("telethon_client")
        if not telethon_client:
            await update.message.reply_text(
                "❌ Trading not initialized.", parse_mode="HTML"
            )
            return

        from alpha_bot.trading.maestro_sender import send_sell_to_maestro

        success = await send_sell_to_maestro(telethon_client, ca, sell_pct)
        if success:
            await update.message.reply_text(
                f"Sell sent to Maestro for <code>{ca}</code> ({sell_pct}%)",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "Failed to send sell to Maestro.", parse_mode="HTML"
            )

    @staticmethod
    async def _cmd_trading(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            status = "ON" if settings.trading_enabled else "OFF"
            await update.message.reply_text(
                f"Auto-trading is currently <b>{status}</b>\n"
                f"Usage: <code>/trading on|off</code>",
                parse_mode="HTML",
            )
            return

        arg = context.args[0].lower()
        if arg in ("on", "true", "1", "enable"):
            settings.trading_enabled = True
            await update.message.reply_text(
                "Auto-trading <b>ENABLED</b>", parse_mode="HTML"
            )
        elif arg in ("off", "false", "0", "disable"):
            settings.trading_enabled = False
            await update.message.reply_text(
                "Auto-trading <b>DISABLED</b>", parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "Usage: <code>/trading on|off</code>", parse_mode="HTML"
            )


    @staticmethod
    async def _cmd_channels(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            from sqlalchemy import select as sa_select

            async with async_session() as session:
                result = await session.execute(
                    sa_select(ChannelScore).order_by(ChannelScore.quality_score.desc())
                )
                scores = list(result.scalars().all())

            if not scores:
                await update.message.reply_text(
                    "No channel scores yet.\n"
                    "Run <code>python backfill_channel_scores.py GROUP</code> to generate.",
                    parse_mode="HTML",
                )
                return

            lines = [f"<b>Channel Rankings ({len(scores)})</b>\n"]
            for i, s in enumerate(scores, 1):
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                lines.append(
                    f"{medal} <b>{s.channel_name or s.channel_id}</b> — "
                    f"<b>{s.quality_score:.0f}/100</b>\n"
                    f"   Calls: {s.total_calls} ({s.resolved_calls} resolved)\n"
                    f"   2x: {s.hit_rate_2x:.0%} | 5x: {s.hit_rate_5x:.0%} | "
                    f"Avg ROI: {s.avg_roi_peak:+.0f}%\n"
                    f"   Best: {s.best_platform} @ {s.best_mcap_range}"
                )

            text = "\n".join(lines)
            for i in range(0, len(text), 4000):
                await update.message.reply_text(
                    text[i : i + 4000], parse_mode="HTML"
                )
        except Exception as exc:
            logger.exception("Channels command failed")
            await update.message.reply_text(
                f"❌ Failed: {exc}", parse_mode="HTML"
            )


    @staticmethod
    async def _cmd_convergence(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        from alpha_bot.tg_intel.convergence import get_recent_convergences

        signals = get_recent_convergences()
        if not signals:
            await update.message.reply_text(
                "No convergence signals in the current window.",
                parse_mode="HTML",
            )
            return

        lines = [f"<b>🔀 Recent Convergences ({len(signals)})</b>\n"]
        for s in signals:
            ca = s["ca"]
            ca_short = f"{ca[:6]}...{ca[-4:]}" if len(ca) > 12 else ca
            ticker = s.get("ticker") or "?"
            ago = ""
            if s.get("alerted_at"):
                delta = datetime.utcnow() - s["alerted_at"]
                ago_min = max(int(delta.total_seconds() / 60), 0)
                ago = f" — {ago_min}m ago"
            lines.append(
                f"<b>${ticker}</b> ({s.get('chain', '?')}) "
                f"conf={s.get('confidence', 0):.2f} "
                f"ch={s.get('channels', 0)}{ago}\n"
                f"  <code>{ca_short}</code>"
            )

        text = "\n".join(lines)
        for i in range(0, len(text), 4000):
            await update.message.reply_text(
                text[i : i + 4000], parse_mode="HTML"
            )


    @staticmethod
    async def _cmd_profile(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            from alpha_bot.tg_intel.pattern_extract import (
                extract_winning_profile,
                format_profile_text,
            )

            profile = await extract_winning_profile()
            text = format_profile_text(profile)
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as exc:
            logger.exception("Profile command failed")
            await update.message.reply_text(
                f"Failed: {exc}", parse_mode="HTML"
            )


    @staticmethod
    async def _cmd_trends(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            from sqlalchemy import select as sa_select
            from alpha_bot.scanner.models import TrendingTheme

            async with async_session() as session:
                result = await session.execute(
                    sa_select(TrendingTheme)
                    .order_by(TrendingTheme.velocity.desc())
                    .limit(20)
                )
                themes = list(result.scalars().all())

            if not themes:
                await update.message.reply_text(
                    "No trending themes yet.\n"
                    "Enable the scanner with <code>SCANNER_ENABLED=true</code>.",
                    parse_mode="HTML",
                )
                return

            lines = [f"<b>Trending Themes ({len(themes)})</b>\n"]
            by_source: dict[str, list] = {}
            for t in themes:
                by_source.setdefault(t.source, []).append(t)

            for source, items in by_source.items():
                lines.append(f"\n<b>{source.upper()}</b>")
                for t in items[:5]:
                    vel = f"+{t.velocity:.0f}%" if t.velocity > 0 else f"{t.velocity:.0f}%"
                    vol = f" (vol: {t.current_volume})" if t.current_volume else ""
                    lines.append(f"  {t.theme[:60]} — {vel}{vol}")

            text = "\n".join(lines)
            for i in range(0, len(text), 4000):
                await update.message.reply_text(
                    text[i : i + 4000], parse_mode="HTML"
                )
        except Exception as exc:
            logger.exception("Trends command failed")
            await update.message.reply_text(
                f"Failed: {exc}", parse_mode="HTML"
            )

    @staticmethod
    async def _cmd_scan(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/scan CONTRACT_ADDRESS</code>",
                parse_mode="HTML",
            )
            return

        ca = context.args[0].strip()
        await update.message.reply_text(
            f"Scanning <code>{ca[:16]}...</code>",
            parse_mode="HTML",
        )

        try:
            # Fetch token data from DexScreener
            async with httpx.AsyncClient(timeout=15) as client:
                pair = await get_token_by_address(ca, client)

            if not pair:
                await update.message.reply_text(
                    f"No token found for <code>{ca[:16]}...</code>",
                    parse_mode="HTML",
                )
                return

            d = extract_pair_details(pair)
            from alpha_bot.tg_intel.platform_detect import detect_platform

            token = {
                "ca": ca,
                "chain": pair.get("chainId", "base"),
                "ticker": d["symbol"],
                "name": d["name"],
                "price_usd": d["price_usd"],
                "mcap": d["market_cap"],
                "liquidity_usd": d["liquidity_usd"],
                "volume_24h": d["volume_24h"],
                "pair_age_hours": None,
                "platform": detect_platform(ca, pair_data=pair),
                "discovery_source": "manual",
            }

            # Load themes
            from sqlalchemy import select as sa_select
            from alpha_bot.scanner.models import TrendingTheme

            async with async_session() as session:
                result = await session.execute(
                    sa_select(TrendingTheme)
                    .order_by(TrendingTheme.velocity.desc())
                    .limit(100)
                )
                themes = list(result.scalars().all())

            # Run matching pipeline
            from alpha_bot.scanner.token_matcher import match_token_to_themes
            from alpha_bot.scanner.depth_scorer import compute_depth
            from alpha_bot.scanner.candidate_scorer import (
                compute_profile_match,
                compute_market_score,
                compute_composite,
            )

            matched_names, nar_score = await match_token_to_themes(
                d["name"], d["symbol"], themes,
            )
            depth = compute_depth(
                d["name"], d["symbol"], matched_names, themes,
                platform=token["platform"],
            )
            token["_matched_themes"] = matched_names
            prof_score = compute_profile_match(token, None)

            # Try loading winning profile
            try:
                from alpha_bot.tg_intel.pattern_extract import extract_winning_profile
                profile = await extract_winning_profile()
                if profile:
                    prof_score = compute_profile_match(token, profile)
            except Exception:
                pass

            mkt_score = compute_market_score(token)

            # Platform percentile
            plat_score = 0.0
            plat_str = "N/A"
            if token["platform"] in ("clanker", "virtuals", "flaunch"):
                try:
                    from alpha_bot.platform_intel.percentile_rank import (
                        compute_platform_percentile,
                    )
                    pct = await compute_platform_percentile(
                        ca, token["platform"], d.get("market_cap"),
                        None, d.get("volume_24h"), token.get("pair_age_hours"),
                    )
                    plat_score = pct.get("overall_percentile", 0.0)
                    plat_str = (
                        f"{plat_score:.0f}/100 "
                        f"({pct['age_bucket']}, {pct['cohort_size']} tokens)"
                    )
                except Exception:
                    pass

            composite, tier = compute_composite(
                nar_score, depth, prof_score, mkt_score, "manual",
                platform_score=plat_score,
            )

            tier_emoji = {1: "\U0001f534", 2: "\U0001f7e1", 3: "\U0001f7e2"}.get(tier, "\u26ab")
            themes_str = ", ".join(f'"{t}"' for t in matched_names[:3]) if matched_names else "none"

            text = (
                f"{tier_emoji} <b>SCAN: ${d['symbol']}</b> ({d['name']})\n\n"
                f"Score: <b>{composite:.0f}/100</b> (Tier {tier})\n"
                f"Chain: {token['chain']} | Platform: {token['platform']}\n\n"
                f"<b>Breakdown:</b>\n"
                f"  Narrative: {nar_score:.0f}/100 — {themes_str}\n"
                f"  Depth: {depth}/100 ({depth // 25} layers)\n"
                f"  Profile match: {prof_score:.0f}/100\n"
                f"  Platform: {plat_str}\n"
                f"  Market quality: {mkt_score:.0f}/100\n\n"
                f"MCap: {_fmt_mcap(d['market_cap'])} | Liq: {_fmt_mcap(d['liquidity_usd'])}\n"
                f"Vol 24h: {_fmt_mcap(d['volume_24h'])}\n\n"
                f"<code>{ca}</code>"
            )

            await update.message.reply_text(text, parse_mode="HTML")

        except Exception as exc:
            logger.exception("Scan command failed for %s", ca[:12])
            await update.message.reply_text(
                f"Scan failed: {exc}", parse_mode="HTML"
            )


    @staticmethod
    async def _cmd_platform(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/platform CONTRACT_ADDRESS</code>",
                parse_mode="HTML",
            )
            return

        ca = context.args[0].strip().lower()
        await update.message.reply_text(
            f"Looking up platform data for <code>{ca[:16]}...</code>",
            parse_mode="HTML",
        )

        try:
            from sqlalchemy import select as sa_select
            from alpha_bot.platform_intel.models import PlatformToken
            from alpha_bot.platform_intel.percentile_rank import compute_platform_percentile
            from alpha_bot.tg_intel.platform_detect import detect_platform

            # Check if already in platform_tokens
            async with async_session() as session:
                result = await session.execute(
                    sa_select(PlatformToken).where(PlatformToken.ca == ca)
                )
                pt = result.scalar_one_or_none()

            # If not found, fetch from DexScreener and detect platform
            if not pt:
                async with httpx.AsyncClient(timeout=15) as client:
                    pair = await get_token_by_address(ca, client)
                if not pair:
                    await update.message.reply_text(
                        f"No token found for <code>{ca[:16]}...</code>",
                        parse_mode="HTML",
                    )
                    return

                d = extract_pair_details(pair)
                platform = detect_platform(ca, pair_data=pair)

                if platform not in ("clanker", "virtuals", "flaunch"):
                    await update.message.reply_text(
                        f"<b>${d['symbol']}</b> — platform: {platform}\n"
                        "Platform percentile only available for Clanker, Virtuals, Flaunch.",
                        parse_mode="HTML",
                    )
                    return

                # Compute age from pair_created_at
                age_hours = None
                pca = d.get("pair_created_at")
                if pca:
                    try:
                        from datetime import datetime
                        created = datetime.utcfromtimestamp(pca / 1000)
                        age_hours = (datetime.utcnow() - created).total_seconds() / 3600
                    except (ValueError, TypeError, OSError):
                        pass

                pct = await compute_platform_percentile(
                    ca, platform, d.get("market_cap"), None,
                    d.get("volume_24h"), age_hours,
                )

                age_str = _fmt_age(age_hours) if age_hours else "?"
                text = (
                    f"📊 <b>PLATFORM: ${d['symbol']}</b> ({platform.title()})\n\n"
                    f"Age: {age_str}\n"
                    f"MCap: {_fmt_mcap(d['market_cap'])} | Liq: {_fmt_mcap(d['liquidity_usd'])}\n\n"
                    f"📈 <b>Percentile ({pct['age_bucket']} cohort, {pct['cohort_size']} tokens):</b>\n"
                    f"  Holders: {pct['holder_percentile']:.0f}th | "
                    f"MCap: {pct['mcap_percentile']:.0f}th | "
                    f"Volume: {pct['volume_percentile']:.0f}th\n"
                    f"  Overall: <b>{pct['overall_percentile']:.0f}th</b> percentile\n\n"
                    f"<code>{ca}</code>"
                )
                await update.message.reply_text(text, parse_mode="HTML")
                return

            # We have the token in DB — show full lifecycle data
            age_hours = None
            if pt.deploy_timestamp:
                from datetime import datetime
                age_hours = (datetime.utcnow() - pt.deploy_timestamp).total_seconds() / 3600

            pct = await compute_platform_percentile(
                ca, pt.platform, pt.current_mcap, pt.holders_7d or pt.holders_24h or pt.holders_1h,
                pt.volume_24h_at_peak, age_hours,
            )

            # Build holder trajectory
            h_parts = []
            if pt.holders_1h is not None:
                h_parts.append(f"1h: {pt.holders_1h:,}")
            if pt.holders_6h is not None:
                h_parts.append(f"6h: {pt.holders_6h:,}")
            if pt.holders_24h is not None:
                h_parts.append(f"24h: {pt.holders_24h:,}")
            if pt.holders_7d is not None:
                h_parts.append(f"7d: {pt.holders_7d:,}")
            holders_str = " → ".join(h_parts) if h_parts else "N/A"

            deploy_str = pt.deploy_timestamp.strftime("%Y-%m-%d %H:%M UTC") if pt.deploy_timestamp else "?"
            age_str = _fmt_age(age_hours) if age_hours else "?"

            survived = "✅" if pt.survived_7d else "❌"
            r100k = "✅" if pt.reached_100k else "❌"
            r500k = "✅" if pt.reached_500k else "❌"
            r1m = "✅" if pt.reached_1m else "❌"

            text = (
                f"📊 <b>PLATFORM: ${pt.symbol}</b> ({pt.platform.title()})\n\n"
                f"Deploy: {deploy_str} ({age_str} ago)\n"
                f"Holders: {holders_str}\n"
                f"Peak MCap: {_fmt_mcap(pt.peak_mcap)} | Current: {_fmt_mcap(pt.current_mcap)}\n"
                f"Survived 7d: {survived} | $100K: {r100k} | $500K: {r500k} | $1M: {r1m}\n\n"
                f"📈 <b>Percentile ({pct['age_bucket']} cohort, {pct['cohort_size']} tokens):</b>\n"
                f"  Holders: {pct['holder_percentile']:.0f}th | "
                f"MCap: {pct['mcap_percentile']:.0f}th | "
                f"Volume: {pct['volume_percentile']:.0f}th\n"
                f"  Overall: <b>{pct['overall_percentile']:.0f}th</b> percentile\n\n"
                f"<code>{ca}</code>"
            )
            await update.message.reply_text(text, parse_mode="HTML")

        except Exception as exc:
            logger.exception("Platform command failed for %s", ca[:12])
            await update.message.reply_text(
                f"Failed: {exc}", parse_mode="HTML"
            )


    @staticmethod
    async def _cmd_backtest(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        days = settings.backtest_default_lookback_days
        if context.args:
            try:
                days = int(context.args[0])
            except ValueError:
                await update.message.reply_text(
                    "Usage: <code>/backtest [days]</code>\n"
                    "Example: <code>/backtest 14</code>",
                    parse_mode="HTML",
                )
                return

        await update.message.reply_text(
            f"Running backtest ({days}d lookback)...",
            parse_mode="HTML",
        )

        try:
            from alpha_bot.scoring_engine.backtest import (
                format_backtest_report,
                run_backtest,
            )

            run = await run_backtest(lookback_days=days)
            text = format_backtest_report(run)
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as exc:
            logger.exception("Backtest command failed")
            await update.message.reply_text(
                f"Backtest failed: {exc}", parse_mode="HTML"
            )

    @staticmethod
    async def _cmd_weights(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            from alpha_bot.scoring_engine.recalibrate import format_weights_text

            text = await format_weights_text()
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as exc:
            logger.exception("Weights command failed")
            await update.message.reply_text(
                f"Failed: {exc}", parse_mode="HTML"
            )

    @staticmethod
    async def _cmd_wallets(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            from sqlalchemy import select as sa_select
            from alpha_bot.wallets.models import PrivateWallet

            async with async_session() as session:
                result = await session.execute(
                    sa_select(PrivateWallet)
                    .where(PrivateWallet.status != "retired")
                    .order_by(PrivateWallet.quality_score.desc())
                    .limit(20)
                )
                wallets = list(result.scalars().all())

            if not wallets:
                await update.message.reply_text(
                    "No private wallets discovered yet.\n"
                    "Enable wallet curation with <code>WALLET_CURATION_ENABLED=true</code>.",
                    parse_mode="HTML",
                )
                return

            lines = [f"<b>Private Wallets ({len(wallets)})</b>\n"]
            for i, w in enumerate(wallets, 1):
                addr_short = f"{w.address[:6]}...{w.address[-4:]}"
                status_icon = {"active": "G", "decaying": "Y"}.get(w.status, "?")
                lines.append(
                    f"{i}. <code>{addr_short}</code> "
                    f"Q:{w.quality_score:.0f} | "
                    f"Wins: {w.total_wins}/{w.total_tracked} | "
                    f"Copiers: {w.estimated_copiers} | "
                    f"{w.status}"
                )

            text = "\n".join(lines)
            for i in range(0, len(text), 4000):
                await update.message.reply_text(
                    text[i : i + 4000], parse_mode="HTML"
                )
        except Exception as exc:
            logger.exception("Wallets command failed")
            await update.message.reply_text(
                f"Failed: {exc}", parse_mode="HTML"
            )

    @staticmethod
    async def _cmd_clusters(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            from sqlalchemy import select as sa_select
            from alpha_bot.wallets.models import WalletCluster

            async with async_session() as session:
                result = await session.execute(
                    sa_select(WalletCluster)
                    .order_by(WalletCluster.avg_quality_score.desc())
                    .limit(20)
                )
                clusters = list(result.scalars().all())

            if not clusters:
                await update.message.reply_text(
                    "No wallet clusters built yet.",
                    parse_mode="HTML",
                )
                return

            lines = [f"<b>Wallet Clusters ({len(clusters)})</b>\n"]
            for c in clusters:
                lines.append(
                    f"<b>{c.cluster_label}</b> — "
                    f"{c.wallet_count} wallets | "
                    f"Avg Q: {c.avg_quality_score:.0f} | "
                    f"Independence: {c.independence_score:.0f}/100"
                )

            text = "\n".join(lines)
            for i in range(0, len(text), 4000):
                await update.message.reply_text(
                    text[i : i + 4000], parse_mode="HTML"
                )
        except Exception as exc:
            logger.exception("Clusters command failed")
            await update.message.reply_text(
                f"Failed: {exc}", parse_mode="HTML"
            )


    @staticmethod
    async def _cmd_watchlist(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            from sqlalchemy import select as sa_select
            from alpha_bot.scanner.models import ScannerCandidate
            import json as _json

            async with async_session() as session:
                result = await session.execute(
                    sa_select(ScannerCandidate)
                    .where(ScannerCandidate.tier == 2)
                    .order_by(ScannerCandidate.composite_score.desc())
                    .limit(20)
                )
                candidates = list(result.scalars().all())

            if not candidates:
                await update.message.reply_text(
                    "No Tier 2 tokens on the watchlist right now.",
                    parse_mode="HTML",
                )
                return

            lines = [f"<b>Watchlist — Tier 2 ({len(candidates)})</b>\n"]
            for c in candidates:
                themes = []
                try:
                    themes = _json.loads(c.matched_themes or "[]")
                except (ValueError, TypeError):
                    pass
                themes_str = ", ".join(themes[:2]) if themes else "—"
                mcap_str = _fmt_mcap(c.mcap)
                lines.append(
                    f"<b>${c.ticker}</b> — {c.composite_score:.0f}/100 "
                    f"({c.platform})\n"
                    f"  MCap: {mcap_str} | Themes: {themes_str}\n"
                    f"  <code>{c.ca}</code>"
                )

            text = "\n".join(lines)
            for i in range(0, len(text), 4000):
                await update.message.reply_text(
                    text[i : i + 4000], parse_mode="HTML"
                )
        except Exception as exc:
            logger.exception("Watchlist command failed")
            await update.message.reply_text(
                f"Failed: {exc}", parse_mode="HTML"
            )

    @staticmethod
    async def _cmd_active(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        # /active is an alias for /positions (open positions = tokens you've entered)
        await TelegramDelivery._cmd_positions(update, context)

    @staticmethod
    async def _cmd_exit_check(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/exit_check CONTRACT_ADDRESS</code>",
                parse_mode="HTML",
            )
            return

        ca = context.args[0].strip()

        try:
            from alpha_bot.storage.repository import get_position_by_mint

            async with async_session() as session:
                position = await get_position_by_mint(session, ca)

            if not position:
                await update.message.reply_text(
                    f"No open position for <code>{ca[:16]}...</code>",
                    parse_mode="HTML",
                )
                return

            # Fetch current price from DexScreener
            current_price = position.current_price_usd
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    pair = await get_token_by_address(ca, client)
                if pair:
                    d = extract_pair_details(pair)
                    if d.get("price_usd"):
                        current_price = d["price_usd"]
            except Exception:
                pass

            entry = position.entry_price_usd
            pnl_pct = ((current_price - entry) / entry * 100) if entry > 0 else 0.0

            # TP/SL distances
            tp1_target = entry * (1 + settings.take_profit_1_pct / 100)
            tp2_target = entry * (1 + settings.take_profit_2_pct / 100)
            tp3_target = entry * (1 + settings.take_profit_3_pct / 100)
            sl_target = entry * (1 + settings.stop_loss_pct / 100)

            def _status_line(label: str, hit: bool, target: float) -> str:
                if hit:
                    return f"  {label}: HIT"
                dist = ((target - current_price) / current_price * 100) if current_price > 0 else 0
                return f"  {label}: {dist:+.1f}% to target (${target:.10g})"

            emoji = "\U0001f7e2" if pnl_pct >= 0 else "\U0001f534"
            text = (
                f"{emoji} <b>Exit Check: ${position.token_symbol or ca[:8]}</b>\n\n"
                f"Entry: ${entry:.10g}\n"
                f"Current: ${current_price:.10g}\n"
                f"P/L: <b>{pnl_pct:+.1f}%</b>\n\n"
                f"<b>Targets:</b>\n"
                f"{_status_line('TP1 (3x)', position.tp1_hit, tp1_target)}\n"
                f"{_status_line('TP2 (5x)', position.tp2_hit, tp2_target)}\n"
                f"{_status_line('TP3 (10x)', position.tp3_hit, tp3_target)}\n"
                f"{_status_line('SL', position.stop_loss_hit, sl_target)}\n\n"
                f"<code>{ca}</code>"
            )

            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as exc:
            logger.exception("Exit check failed for %s", ca[:12])
            await update.message.reply_text(
                f"Failed: {exc}", parse_mode="HTML"
            )

    @staticmethod
    async def _cmd_status(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            from sqlalchemy import select as sa_select, func

            lines = ["<b>System Status</b>\n"]

            # Feature flags
            flags = {
                "Scanner": settings.scanner_enabled,
                "Platform Intel": settings.clanker_scraper_enabled,
                "Recalibration": settings.recalibrate_enabled,
                "Wallets": settings.wallet_curation_enabled,
                "Trading": settings.trading_enabled,
            }
            flag_str = " | ".join(
                f"{'ON' if v else 'OFF'} {k}" for k, v in flags.items()
            )
            lines.append(f"{flag_str}\n")

            # DB counts
            from alpha_bot.tg_intel.models import CallOutcome, ChannelScore
            from alpha_bot.scanner.models import TrendingTheme, ScannerCandidate
            from alpha_bot.platform_intel.models import PlatformToken
            from alpha_bot.storage.models import Position

            counts: dict[str, int] = {}
            async with async_session() as session:
                for label, model in [
                    ("call_outcomes", CallOutcome),
                    ("channel_scores", ChannelScore),
                    ("trending_themes", TrendingTheme),
                    ("scanner_candidates", ScannerCandidate),
                    ("platform_tokens", PlatformToken),
                ]:
                    r = await session.execute(sa_select(func.count()).select_from(model))
                    counts[label] = r.scalar() or 0

                # Open positions
                r = await session.execute(
                    sa_select(func.count()).select_from(Position).where(Position.status == "open")
                )
                counts["open_positions"] = r.scalar() or 0

                # Private wallets (try/except in case table doesn't exist yet)
                try:
                    from alpha_bot.wallets.models import PrivateWallet
                    r = await session.execute(sa_select(func.count()).select_from(PrivateWallet))
                    counts["private_wallets"] = r.scalar() or 0
                except Exception:
                    counts["private_wallets"] = 0

            lines.append("<b>DB Counts:</b>")
            for label, cnt in counts.items():
                lines.append(f"  {label}: {cnt:,}")

            # Last updated timestamps
            lines.append("\n<b>Last Updated:</b>")
            async with async_session() as session:
                for label, model, col in [
                    ("Channel scores", ChannelScore, ChannelScore.last_updated),
                    ("Trending themes", TrendingTheme, TrendingTheme.last_updated),
                    ("Scanner candidates", ScannerCandidate, ScannerCandidate.last_updated),
                    ("Platform tokens", PlatformToken, PlatformToken.last_updated),
                ]:
                    r = await session.execute(
                        sa_select(func.max(col))
                    )
                    ts = r.scalar()
                    ts_str = ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "never"
                    lines.append(f"  {label}: {ts_str}")

            text = "\n".join(lines)
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as exc:
            logger.exception("Status command failed")
            await update.message.reply_text(
                f"Failed: {exc}", parse_mode="HTML"
            )


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "?"
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 24:
        return f"{hours:.0f}h"
    days = hours / 24
    if days < 7:
        return f"{days:.1f}d"
    return f"{days:.0f}d"


def _format_pnl_telegram(report: PnLReport) -> str:
    """Format a PnLReport as a Telegram HTML message."""
    lines = [
        f"📊 <b>P/L Report: {report.group_name}</b>",
        f"Period: last {report.days_analyzed} days\n",
        f"Total ticker calls: <b>{report.total_calls}</b>",
        f"Unique tickers: <b>{report.unique_tickers}</b>",
        f"Resolved (price data): <b>{report.resolved_tickers}</b>",
    ]

    if report.best_call:
        lines.append(
            f"\n🏆 Best: <b>${report.best_call.ticker}</b> "
            f"({report.best_call.pnl_pct:+.1f}%)"
        )
    if report.worst_call:
        lines.append(
            f"💀 Worst: <b>${report.worst_call.ticker}</b> "
            f"({report.worst_call.pnl_pct:+.1f}%)"
        )

    # Tokens with P/L data
    with_pnl = [s for s in report.ticker_summaries if s.avg_pnl_pct is not None]
    if with_pnl:
        lines.append("\n<b>— P/L (CoinGecko) —</b>")
        for s in with_pnl[:10]:
            emoji = "🟢" if s.avg_pnl_pct > 0 else "🔴"
            lines.append(
                f"{emoji} <b>${s.ticker}</b> {s.avg_pnl_pct:+.1f}% "
                f"({s.call_count}x, {s.win_rate:.0f}% win)"
            )

    # Memecoin calls (DexScreener)
    dex_only = [s for s in report.ticker_summaries if s.avg_pnl_pct is None]
    if dex_only:
        lines.append("\n<b>— Memecoin calls —</b>")
        for s in dex_only[:15]:
            status = {"alive": "🟢", "dead": "💀", "low_liq": "⚠️"}.get(s.status, "❓")
            mcap = _fmt_mcap(s.market_cap)
            liq = _fmt_mcap(s.liquidity_usd)
            lines.append(
                f"{status} <b>${s.ticker}</b> — mcap: {mcap}, liq: {liq} ({s.call_count}x)"
            )

    # Status summary
    alive = sum(1 for s in report.ticker_summaries if s.status == "alive")
    dead = sum(1 for s in report.ticker_summaries if s.status == "dead")
    low_liq = sum(1 for s in report.ticker_summaries if s.status == "low_liq")
    if alive or dead or low_liq:
        lines.append(f"\n📈 {alive} alive | ⚠️ {low_liq} low liq | 💀 {dead} dead")

    return "\n".join(lines)


def _fmt_mcap(n: float | None) -> str:
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.0f}"
