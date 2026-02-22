"""Send buy/sell commands to Maestro trading bot via Telethon userbot.

Maestro flow (multi-chain — SOL, BASE, ETH, BSC):
  Buy:  Send CA -> bot auto-detects chain, shows token info + buy buttons -> click buy amount
  Sell: Send CA -> bot shows sell view (if holding) -> click sell percentage

Maestro uses customizable keyboard presets — button text may differ per user.
We use flexible partial-text matching and log all buttons on first interaction.
"""

import asyncio
import logging

from telethon import TelegramClient

from alpha_bot.config import settings

logger = logging.getLogger(__name__)


async def _wait_for_reply(
    client: TelegramClient,
    entity,
    after_id: int,
    timeout: float = 15.0,
):
    """Wait for Maestro's reply message with inline buttons after we send a CA."""
    elapsed = 0.0
    poll = 0.5
    while elapsed < timeout:
        await asyncio.sleep(poll)
        elapsed += poll
        msgs = await client.get_messages(entity, limit=5)
        for msg in msgs:
            if msg.id > after_id and msg.buttons:
                return msg
    return None


def _find_button(msg, text_contains: str) -> tuple[int, int] | None:
    """Find a button by partial text match (case-insensitive). Returns (row, col) or None."""
    if not msg.buttons:
        return None
    for row_idx, row in enumerate(msg.buttons):
        for col_idx, btn in enumerate(row):
            if text_contains.lower() in btn.text.lower():
                return (row_idx, col_idx)
    return None


def _log_buttons(msg) -> str:
    """Dump all button labels for debugging."""
    if not msg.buttons:
        return "no buttons"
    return str([[btn.text for btn in row] for row in msg.buttons])


async def send_buy_to_maestro(
    client: TelegramClient,
    token_mint: str,
) -> bool:
    """Send CA to Maestro and click the appropriate buy button.

    Maestro auto-detects chain from the CA format.
    Matches trade_amount_sol (for SOL) or trade_amount_base_eth (for EVM) to a preset button.
    Falls back to custom amount input if no preset matches.
    """
    try:
        entity = await client.get_entity(settings.maestro_bot_username)
        sent = await client.send_message(entity, token_mint)
        logger.info("Sent CA to Maestro: %s", token_mint[:12])

        reply = await _wait_for_reply(client, entity, sent.id)
        if not reply:
            logger.warning("No button reply from Maestro within timeout")
            return False

        logger.debug("Maestro buttons: %s", _log_buttons(reply))

        # Determine trade amount based on CA format (0x = EVM chain, else Solana)
        is_evm = token_mint.startswith("0x")
        if is_evm:
            amount = settings.trade_amount_base_eth
            amount_str = str(amount)
            native = "ETH"
        else:
            amount = settings.trade_amount_sol
            amount_str = (
                str(int(amount)) if amount == int(amount) else str(amount)
            )
            native = "SOL"

        # Try to find a preset button matching the amount
        # Maestro buttons may say "Buy 0.1 SOL", "0.1 SOL", "0.1", etc.
        pos = _find_button(reply, f"{amount_str} {native}")
        if not pos:
            pos = _find_button(reply, amount_str)

        if pos:
            await reply.click(pos[0], pos[1])
            logger.info("Clicked buy button '%s %s' on Maestro", amount_str, native)
        else:
            # Try custom buy button — Maestro often labels it "Buy X" or "Custom"
            custom_pos = (
                _find_button(reply, "Buy X")
                or _find_button(reply, "Custom")
                or _find_button(reply, "custom")
            )
            if not custom_pos:
                logger.error(
                    "Could not find buy button on Maestro. Buttons: %s",
                    _log_buttons(reply),
                )
                return False
            await reply.click(custom_pos[0], custom_pos[1])
            logger.info("Clicked custom buy, sending amount: %s %s", amount_str, native)
            await asyncio.sleep(1)
            await client.send_message(entity, str(amount))

        return True

    except Exception as exc:
        logger.error("Failed to buy via Maestro: %s", exc)
        return False


async def send_sell_to_maestro(
    client: TelegramClient,
    token_mint: str,
    sell_pct: int = 100,
) -> bool:
    """Send CA to Maestro and click the appropriate sell button.

    Maestro sell buttons typically: 25%, 50%, 100%, Custom
    sell_pct is relative to CURRENT bag (not original).
    """
    try:
        entity = await client.get_entity(settings.maestro_bot_username)
        sent = await client.send_message(entity, token_mint)
        logger.info("Sent sell CA to Maestro: %s (%d%%)", token_mint[:12], sell_pct)

        reply = await _wait_for_reply(client, entity, sent.id)
        if not reply:
            logger.warning("No button reply from Maestro within timeout")
            return False

        logger.debug("Maestro sell buttons: %s", _log_buttons(reply))

        # Try to find a sell/close button first if selling 100%
        if sell_pct == 100:
            pos = (
                _find_button(reply, "Sell 100")
                or _find_button(reply, "100%")
                or _find_button(reply, "close")
            )
        else:
            pos = (
                _find_button(reply, f"Sell {sell_pct}")
                or _find_button(reply, f"{sell_pct}%")
            )

        if pos:
            await reply.click(pos[0], pos[1])
            logger.info("Clicked '%d%%' sell on Maestro", sell_pct)
            return True
        else:
            # Use custom percentage button
            custom_pos = (
                _find_button(reply, "Sell X")
                or _find_button(reply, "X%")
                or _find_button(reply, "Custom")
            )
            if not custom_pos:
                logger.error(
                    "Could not find sell button on Maestro. Buttons: %s",
                    _log_buttons(reply),
                )
                return False
            await reply.click(custom_pos[0], custom_pos[1])
            logger.info("Clicked custom sell, sending pct: %d", sell_pct)
            await asyncio.sleep(1)
            await client.send_message(entity, str(sell_pct))
            return True

    except Exception as exc:
        logger.error("Failed to sell via Maestro: %s", exc)
        return False
