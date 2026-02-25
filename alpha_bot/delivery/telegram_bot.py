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
            "/scan &lt;CA&gt; — Full scanner score breakdown\n\n"
            "<b>Trading:</b>\n"
            "/positions — List open positions\n"
            "/buy &lt;CA&gt; — Manual buy via Maestro\n"
            "/sell &lt;CA&gt; [pct] — Manual sell via Maestro\n"
            "/trading on|off — Toggle auto-trading\n\n"
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
            "<code>/scan CA_ADDRESS</code> — Full scanner score for a token\n\n"
            "<b>Trading:</b>\n"
            "<code>/positions</code> — Show open positions with P/L\n"
            "<code>/buy CA_ADDRESS</code> — Send buy to Maestro bot\n"
            "<code>/sell CA_ADDRESS 50</code> — Sell 50% via Maestro\n"
            "<code>/trading on</code> — Enable auto-trading\n"
            "<code>/trading off</code> — Disable auto-trading",
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
            composite, tier = compute_composite(
                nar_score, depth, prof_score, mkt_score, "manual",
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
