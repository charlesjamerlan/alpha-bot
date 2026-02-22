"""Send buy/sell commands to Sigma trading bot via Telethon userbot.

Sigma flow:
  Buy:  Send CA -> bot shows token info + buy buttons -> click "Buy X SOL"
  Sell: Send CA -> bot shows sell view (if holding) -> click "50%", "100%", etc.

Sell buttons: 10%, 50%, 100%, X%
Buy buttons:  Buy 0.5 SOL, Buy 1 SOL, Buy 2 SOL, Buy 5 SOL, Buy X SOL
"""

import asyncio
import logging

from telethon import TelegramClient

from alpha_bot.config import settings

logger = logging.getLogger(__name__)

# Preset buy amounts available in Sigma's UI
_BUY_PRESETS = [0.5, 1, 2, 5]

# Preset sell percentages available in Sigma's UI
_SELL_PRESETS = [10, 50, 100]


async def _wait_for_sigma_reply(
    client: TelegramClient,
    entity,
    after_id: int,
    timeout: float = 10.0,
):
    """Wait for Sigma's reply message with inline buttons after we send a CA."""
    elapsed = 0.0
    poll = 0.5
    while elapsed < timeout:
        await asyncio.sleep(poll)
        elapsed += poll
        msgs = await client.get_messages(entity, limit=3)
        for msg in msgs:
            if msg.id > after_id and msg.buttons:
                return msg
    return None


def _find_button(msg, text_contains: str) -> tuple[int, int] | None:
    """Find a button by partial text match. Returns (row, col) or None."""
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


async def send_buy_to_sigma(
    client: TelegramClient,
    token_mint: str,
) -> bool:
    """Send CA to Sigma and click the appropriate buy button.

    Matches trade_amount_sol to a preset button (0.5, 1, 2, 5 SOL).
    Falls back to "Buy X SOL" for custom amounts.
    """
    try:
        entity = await client.get_entity(settings.sigma_bot_username)
        sent = await client.send_message(entity, token_mint)
        logger.info("Sent CA to Sigma: %s", token_mint[:12])

        reply = await _wait_for_sigma_reply(client, entity, sent.id)
        if not reply:
            logger.warning("No button reply from Sigma within timeout")
            return False

        logger.debug("Sigma buttons: %s", _log_buttons(reply))

        # If Sigma shows sell view (we already hold this token), switch to buy
        switch_pos = _find_button(reply, "Switch to Buy")
        if switch_pos:
            await reply.click(switch_pos[0], switch_pos[1])
            logger.info("Switched to Buy Menu")
            await asyncio.sleep(1.5)
            # Re-fetch the updated message
            reply = await _wait_for_sigma_reply(client, entity, sent.id)
            if not reply:
                logger.warning("No reply after switching to Buy Menu")
                return False

        amount = settings.trade_amount_sol

        # Try to match a preset button
        if amount in _BUY_PRESETS:
            amount_str = str(int(amount)) if amount == int(amount) else str(amount)
            btn_text = f"Buy {amount_str} SOL"
            pos = _find_button(reply, btn_text)
        else:
            pos = None

        if pos:
            await reply.click(pos[0], pos[1])
            logger.info("Clicked '%s' on Sigma", btn_text)
        else:
            # Use "Buy X SOL" for custom amount
            pos = _find_button(reply, "Buy X SOL")
            if not pos:
                logger.error(
                    "Could not find buy button. Buttons: %s", _log_buttons(reply)
                )
                return False
            await reply.click(pos[0], pos[1])
            logger.info("Clicked 'Buy X SOL', sending amount: %s", amount)
            await asyncio.sleep(1)
            await client.send_message(entity, str(amount))

        return True

    except Exception as exc:
        logger.error("Failed to buy via Sigma: %s", exc)
        return False


async def send_sell_to_sigma(
    client: TelegramClient,
    token_mint: str,
    sell_pct: int = 100,
) -> bool:
    """Send CA to Sigma and click the appropriate sell button.

    Sigma sell buttons: 10%, 50%, 100%, X%
    sell_pct is relative to CURRENT bag (not original).
    """
    try:
        entity = await client.get_entity(settings.sigma_bot_username)
        sent = await client.send_message(entity, token_mint)
        logger.info("Sent sell CA to Sigma: %s (%d%%)", token_mint[:12], sell_pct)

        reply = await _wait_for_sigma_reply(client, entity, sent.id)
        if not reply:
            logger.warning("No button reply from Sigma within timeout")
            return False

        logger.debug("Sigma buttons: %s", _log_buttons(reply))

        # If Sigma shows buy view, switch to sell
        switch_pos = _find_button(reply, "Switch to Sell")
        if switch_pos:
            # Hmm, Sigma shows sell by default if you hold the token.
            # But just in case:
            pass

        # Match a preset sell button
        if sell_pct in _SELL_PRESETS:
            btn_text = f"{sell_pct}%"
            pos = _find_button(reply, btn_text)
        else:
            pos = None

        if pos:
            await reply.click(pos[0], pos[1])
            logger.info("Clicked '%s' sell on Sigma", btn_text)
            return True
        else:
            # Use "X%" for custom percentage
            pos = _find_button(reply, "X%")
            if not pos:
                logger.error(
                    "Could not find sell button. Buttons: %s",
                    _log_buttons(reply),
                )
                return False
            await reply.click(pos[0], pos[1])
            logger.info("Clicked 'X%%', sending sell pct: %d", sell_pct)
            await asyncio.sleep(1)
            await client.send_message(entity, str(sell_pct))
            return True

    except Exception as exc:
        logger.error("Failed to sell via Sigma: %s", exc)
        return False
