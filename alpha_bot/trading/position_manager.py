"""Core orchestrator — handles signal processing, exit logic, and notifications."""

import logging
from datetime import datetime

from telethon import TelegramClient

from alpha_bot.config import settings
from alpha_bot.storage.database import async_session
from alpha_bot.storage.models import Position, Trade
from alpha_bot.storage.repository import (
    get_open_positions,
    save_position,
    save_trade,
    update_position,
)
from alpha_bot.trading.models import TradeSignal
from alpha_bot.trading.safety import run_all_checks, set_cooldown
from alpha_bot.trading.sigma_sender import send_buy_to_sigma, send_sell_to_sigma

logger = logging.getLogger(__name__)

# Will be set by main.py so position_manager can send notifications
_notify_fn = None


def set_notify_fn(fn):
    """Set the async notification callback (sends TG messages to user)."""
    global _notify_fn
    _notify_fn = fn


async def _notify(text: str) -> None:
    if _notify_fn:
        try:
            await _notify_fn(text)
        except Exception as exc:
            logger.error("Notification failed: %s", exc)


async def handle_signal(
    signal: TradeSignal,
    telethon_client: TelegramClient,
) -> None:
    """Called by the listener when a new signal is detected.

    Flow: safety checks -> send buy to Sigma -> record position + trade in DB -> notify.
    """
    async with async_session() as session:
        open_positions = await get_open_positions(session)
        open_count = len(open_positions)

        # Run safety checks
        failure = await run_all_checks(signal, session, open_count)
        if failure:
            logger.info("Signal rejected (%s): %s", signal.token_mint[:8], failure)
            await _notify(
                f"Signal rejected for <b>{signal.ticker or signal.token_mint[:8]}</b>: {failure}"
            )
            return

        # Send buy to Sigma
        success = await send_buy_to_sigma(telethon_client, signal.token_mint)
        if not success:
            await _notify(
                f"Failed to send buy to Sigma for <b>{signal.ticker or signal.token_mint[:8]}</b>"
            )
            return

        set_cooldown(signal.token_mint)

        # Fetch initial price from DexScreener for tracking
        entry_price = await _fetch_price(signal.token_mint)

        # Create position record
        position = Position(
            token_mint=signal.token_mint,
            token_symbol=signal.ticker,
            chain=signal.chain,
            entry_price_usd=entry_price,
            current_price_usd=entry_price,
            entry_amount_sol=settings.trade_amount_sol,
            source_group=signal.source_group,
            source_message_id=signal.source_message_id,
        )
        position = await save_position(session, position)

        # Record the buy trade
        trade = Trade(
            position_id=position.id,
            side="buy",
            token_mint=signal.token_mint,
            token_symbol=signal.ticker,
            chain=signal.chain,
            amount_in=settings.trade_amount_sol,
            price_usd=entry_price,
            status="confirmed",
            trigger="auto_buy",
        )
        await save_trade(session, trade)

        logger.info(
            "BUY sent to Sigma: %s (%s) @ $%s | source: %s",
            signal.ticker,
            signal.token_mint[:8],
            f"{entry_price:.8f}" if entry_price else "?",
            signal.source_group,
        )

        await _notify(
            f"BUY sent to Sigma\n"
            f"Token: <b>${signal.ticker or signal.token_mint[:8]}</b>\n"
            f"CA: <code>{signal.token_mint}</code>\n"
            f"Amount: {settings.trade_amount_sol} SOL\n"
            f"Price: ${entry_price:.8f}\n"
            f"Source: {signal.source_group}"
        )


async def check_exits(
    position: Position,
    current_price: float,
    telethon_client: TelegramClient,
) -> None:
    """Check if a position should be partially or fully exited."""
    if position.entry_price_usd <= 0 or current_price <= 0:
        return

    pnl_pct = ((current_price - position.entry_price_usd) / position.entry_price_usd) * 100
    position.current_price_usd = current_price
    position.unrealized_pnl_pct = pnl_pct

    # Sigma sells % of CURRENT bag, not original.
    # Our config defines % of ORIGINAL to sell at each TP:
    #   TP1: 50% of original -> 50% of current (nothing sold yet)
    #   TP2: 25% of original -> 50% of current (50% already sold at TP1)
    #   TP3: 25% of original -> 100% of remaining (75% already sold)
    # We compute the Sigma % dynamically based on what's been sold.

    # Stop-loss: sell 100% of whatever is left
    if pnl_pct <= settings.stop_loss_pct and not position.stop_loss_hit:
        position.stop_loss_hit = True
        await _execute_sell(position, 100, "stop_loss", telethon_client, close=True)
        return

    # TP3: 10x — sell everything remaining
    if pnl_pct >= settings.take_profit_3_pct and not position.tp3_hit:
        position.tp3_hit = True
        await _execute_sell(position, 100, "tp3", telethon_client, close=True)
        return

    # TP2: 5x — sell 50% of current bag (= 25% of original after TP1 sold 50%)
    if pnl_pct >= settings.take_profit_2_pct and not position.tp2_hit:
        position.tp2_hit = True
        await _execute_sell(position, 50, "tp2", telethon_client)
        return

    # TP1: 3x — sell 50% of current bag
    if pnl_pct >= settings.take_profit_1_pct and not position.tp1_hit:
        position.tp1_hit = True
        await _execute_sell(position, 50, "tp1", telethon_client)
        return

    # Just update the position with latest price
    async with async_session() as session:
        await update_position(session, position)


async def _execute_sell(
    position: Position,
    sell_pct: int,
    trigger: str,
    telethon_client: TelegramClient,
    close: bool = False,
) -> None:
    """Send sell command to Sigma, update position, record trade, notify."""
    success = await send_sell_to_sigma(
        telethon_client, position.token_mint, sell_pct
    )

    if close:
        position.status = "closed"
        position.closed_at = datetime.utcnow()

    async with async_session() as session:
        await update_position(session, position)

        trade = Trade(
            position_id=position.id,
            side="sell",
            token_mint=position.token_mint,
            token_symbol=position.token_symbol,
            chain=position.chain,
            price_usd=position.current_price_usd,
            status="confirmed" if success else "failed",
            trigger=trigger,
        )
        await save_trade(session, trade)

    trigger_labels = {
        "stop_loss": "STOP LOSS",
        "tp1": "TP1 (3x)",
        "tp2": "TP2 (5x)",
        "tp3": "TP3 (10x)",
        "manual": "MANUAL",
    }
    label = trigger_labels.get(trigger, trigger.upper())
    pnl = position.unrealized_pnl_pct

    status = "SELL sent to Sigma" if success else "SELL FAILED"
    await _notify(
        f"{status} — <b>{label}</b>\n"
        f"Token: <b>${position.token_symbol or position.token_mint[:8]}</b>\n"
        f"CA: <code>{position.token_mint}</code>\n"
        f"Sell: {sell_pct}% of bag\n"
        f"P/L: {pnl:+.1f}%\n"
        f"{'Position CLOSED' if close else 'Position still open'}"
    )

    logger.info(
        "SELL %s: %s (%s) %d%% | P/L: %+.1f%% | %s",
        label,
        position.token_symbol,
        position.token_mint[:8],
        sell_pct,
        pnl,
        "CLOSED" if close else "OPEN",
    )


async def _fetch_price(token_mint: str) -> float:
    """Fetch current price — Jupiter first, DexScreener fallback."""
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        # Try Jupiter first
        try:
            resp = await client.get(
                "https://api.jup.ag/price/v2",
                params={"ids": token_mint},
            )
            resp.raise_for_status()
            data = resp.json()
            price_data = data.get("data", {}).get(token_mint)
            if price_data and price_data.get("price"):
                return float(price_data["price"])
        except Exception as exc:
            logger.debug("Jupiter price failed for %s: %s", token_mint[:8], exc)

        # Fallback: DexScreener
        try:
            resp = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
            )
            resp.raise_for_status()
            pairs = resp.json().get("pairs") or []
            if pairs:
                best = max(
                    pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0)
                )
                price = best.get("priceUsd")
                if price:
                    return float(price)
        except Exception as exc:
            logger.warning("DexScreener price failed for %s: %s", token_mint[:8], exc)

    return 0.0
