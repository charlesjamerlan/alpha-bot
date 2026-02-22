import logging

import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from alpha_bot.config import settings
from alpha_bot.delivery.base import DeliveryChannel
from alpha_bot.ingestion.models import RawTweet
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
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("buy", self._cmd_buy))
        self._app.add_handler(CommandHandler("sell", self._cmd_sell))
        self._app.add_handler(CommandHandler("trading", self._cmd_trading))
        return self._app

    @staticmethod
    async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🤖 <b>Alpha Bot</b>\n\n"
            "<b>Research:</b>\n"
            "/research &lt;ticker&gt; — Full research report\n"
            "/pnl &lt;group&gt; [days] — TG group P/L analysis\n\n"
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
            "<code>/pnl cryptogroup 30</code> — Analyze last 30 days\n\n"
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
