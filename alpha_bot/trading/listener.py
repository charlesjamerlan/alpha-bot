"""Real-time TG group listener — detects signals and triggers buys."""

import logging
from dataclasses import dataclass

from telethon import TelegramClient, events

from alpha_bot.config import settings
from alpha_bot.research.telegram_group import (
    SESSION_FILE,
    detect_chain,
    extract_contract_addresses,
    extract_tickers,
)
from alpha_bot.trading.models import TradeSignal
from alpha_bot.trading.position_manager import handle_signal

logger = logging.getLogger(__name__)


@dataclass
class MonitorTarget:
    """A group (and optional topic) to monitor."""
    group: str | int          # username or numeric ID
    topic_id: int | None = None  # forum topic ID, None = all topics


def _parse_monitor_groups() -> list[MonitorTarget]:
    """Parse comma-separated group list from config.

    Formats supported:
      - groupname                    (all messages)
      - groupname/topic_id           (specific forum topic only)
      - 2469811342/1                 (numeric group ID + topic)
      - blessedmemecalls,sailboat/1  (mixed)
    """
    raw = settings.telegram_monitor_groups
    if not raw:
        return []
    targets = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue

        topic_id = None
        if "/" in entry:
            group_part, topic_part = entry.rsplit("/", 1)
            try:
                topic_id = int(topic_part)
            except ValueError:
                group_part = entry  # not a valid topic, treat whole thing as group name
            entry = group_part

        # Numeric group IDs
        try:
            group = int(entry)
        except ValueError:
            group = entry

        targets.append(MonitorTarget(group=group, topic_id=topic_id))
    return targets


async def start_listener(telethon_client: TelegramClient) -> None:
    """Start the real-time TG listener using Telethon events.

    Monitors configured groups for messages containing Solana contract addresses,
    parses them into TradeSignals, and calls handle_signal() for each.
    """
    targets = _parse_monitor_groups()
    if not targets:
        logger.warning(
            "No monitor groups configured (TELEGRAM_MONITOR_GROUPS is empty)"
        )
        return

    logger.info("Starting TG listener for groups: %s", targets)

    # Resolve group entities and build topic filter map
    entities = []
    # Map: entity_id -> set of allowed topic IDs (None = all topics)
    topic_filter: dict[int, set[int] | None] = {}

    for target in targets:
        try:
            entity = await telethon_client.get_entity(target.group)
            title = getattr(entity, "title", target.group)
            entities.append(entity)

            eid = entity.id
            if target.topic_id is not None:
                # Add specific topic filter
                if eid not in topic_filter:
                    topic_filter[eid] = set()
                if topic_filter[eid] is not None:
                    topic_filter[eid].add(target.topic_id)
                logger.info("Monitoring group: %s (topic %d)", title, target.topic_id)
            else:
                # All topics — overrides any specific topic filters
                topic_filter[eid] = None
                logger.info("Monitoring group: %s (all topics)", title)
        except Exception as exc:
            logger.error("Failed to resolve group %s: %s", target.group, exc)

    if not entities:
        logger.error("No valid groups to monitor — listener not started")
        return

    @telethon_client.on(events.NewMessage(chats=entities))
    async def on_new_message(event):
        """Handle each new message in monitored groups."""
        text = event.message.text
        if not text:
            return

        # Topic filtering for forum groups
        chat_id = event.message.chat_id
        allowed_topics = topic_filter.get(chat_id)
        if allowed_topics is not None:
            # We have specific topic filters for this group
            msg_topic_id = None
            reply_to = event.message.reply_to
            if reply_to and hasattr(reply_to, "forum_topic") and reply_to.forum_topic:
                msg_topic_id = reply_to.reply_to_msg_id
            # General topic (topic_id=1) messages may not have reply_to
            if msg_topic_id is None:
                msg_topic_id = 1  # default = General topic

            if msg_topic_id not in allowed_topics:
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
        chain = detect_chain(ca, text)

        signal = TradeSignal(
            token_mint=ca,
            ticker=ticker,
            chain=chain,
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

        # Record call outcome for TG intelligence scoring (fire-and-forget)
        try:
            from alpha_bot.tg_intel.recorder import record_call
            await record_call(
                ca=ca,
                chain=chain,
                ticker=ticker,
                channel_id=str(event.message.chat_id),
                channel_name=group_name,
                message_id=event.message.id,
                message_text=text[:500],
                author=sender_name,
                mention_timestamp=event.message.date.replace(tzinfo=None),
            )
        except Exception as exc:
            logger.warning("Failed to record call outcome: %s", exc)

        if settings.trading_enabled:
            await handle_signal(signal, telethon_client)

    # Keep listening until disconnected
    logger.info("TG listener active — waiting for messages")
    await telethon_client.run_until_disconnected()
