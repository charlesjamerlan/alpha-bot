"""Real-time TG group listener — detects signals and triggers buys."""

import logging

from telethon import TelegramClient, events

from alpha_bot.config import settings
from alpha_bot.research.telegram_group import (
    SESSION_FILE,
    extract_contract_addresses,
    extract_tickers,
)
from alpha_bot.trading.models import TradeSignal
from alpha_bot.trading.position_manager import handle_signal

logger = logging.getLogger(__name__)


def _parse_monitor_groups() -> list[str | int]:
    """Parse comma-separated group list from config."""
    raw = settings.telegram_monitor_groups
    if not raw:
        return []
    groups = []
    for g in raw.split(","):
        g = g.strip()
        if not g:
            continue
        # Numeric IDs
        try:
            groups.append(int(g))
        except ValueError:
            groups.append(g)
    return groups


async def start_listener(telethon_client: TelegramClient) -> None:
    """Start the real-time TG listener using Telethon events.

    Monitors configured groups for messages containing Solana contract addresses,
    parses them into TradeSignals, and calls handle_signal() for each.
    """
    groups = _parse_monitor_groups()
    if not groups:
        logger.warning(
            "No monitor groups configured (TELEGRAM_MONITOR_GROUPS is empty)"
        )
        return

    logger.info("Starting TG listener for groups: %s", groups)

    # Resolve group entities
    entities = []
    for g in groups:
        try:
            entity = await telethon_client.get_entity(g)
            title = getattr(entity, "title", g)
            entities.append(entity)
            logger.info("Monitoring group: %s", title)
        except Exception as exc:
            logger.error("Failed to resolve group %s: %s", g, exc)

    if not entities:
        logger.error("No valid groups to monitor — listener not started")
        return

    @telethon_client.on(events.NewMessage(chats=entities))
    async def on_new_message(event):
        """Handle each new message in monitored groups."""
        text = event.message.text
        if not text:
            return

        # Extract contract addresses (primary signal)
        addresses = extract_contract_addresses(text)
        if not addresses:
            return

        # Extract tickers for labeling
        tickers = extract_tickers(text)

        # Get sender info
        sender_name = ""
        if event.message.sender:
            sender = event.message.sender
            sender_name = (
                getattr(sender, "username", "")
                or getattr(sender, "first_name", "")
                or str(event.message.sender_id)
            )

        # Get group name
        group_name = ""
        chat = event.message.chat
        if chat:
            group_name = getattr(chat, "title", "") or getattr(chat, "username", "") or ""

        # Build a signal for the first CA found
        ca = addresses[0]
        ticker = tickers[0] if tickers else ""

        signal = TradeSignal(
            token_mint=ca,
            ticker=ticker,
            chain="solana",
            source_group=group_name,
            source_message_id=event.message.id,
            source_message_text=text[:500],
            author=sender_name,
        )

        logger.info(
            "Signal detected: %s (%s) from %s by %s",
            ticker or ca[:8],
            ca[:12] + "...",
            group_name,
            sender_name,
        )

        await handle_signal(signal, telethon_client)

    # Keep listening until disconnected
    logger.info("TG listener active — waiting for messages")
    await telethon_client.run_until_disconnected()
