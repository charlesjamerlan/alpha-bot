"""Scrape a Telegram group for ticker calls using Telethon."""

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient

from alpha_bot.config import settings

logger = logging.getLogger(__name__)

# --- Patterns ---

# $TICKER (e.g. $TOLY, $HOODRAT)
TICKER_RE = re.compile(r"\$([A-Za-z]{2,10})\b")

# Quickscope bot: [TOLY](https://app.quickscope.gg/...) or bold **TOLY**
QUICKSCOPE_RE = re.compile(
    r"\[([A-Za-z]{2,10})\]\(https?://app\.quickscope\.gg/"
)

# Solana contract addresses — base58, typically 32-44 chars
# Pump.fun addresses end with "pump"
SOL_CA_RE = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")

# DexScreener links: dexscreener.com/solana/<address>
DEXSCREENER_RE = re.compile(
    r"dexscreener\.com/solana/([1-9A-HJ-NP-Za-km-z]{32,44})"
)

SESSION_FILE = "alpha_bot_telethon"

# Words that look like tickers but aren't
_NOISE_WORDS = {
    # Common English
    "THE", "AND", "FOR", "NOT", "BUT", "ARE", "WAS", "HAS", "HAD",
    "YOU", "ALL", "CAN", "ONE", "OUR", "OUT", "DAY", "GET", "HOW",
    "ITS", "LET", "MAY", "NEW", "NOW", "OLD", "SEE", "WAY", "WHO",
    "DID", "GOT", "SAY", "SHE", "TOO", "USE", "HIS", "HER", "HIM",
    "THIS", "THAT", "WITH", "FROM", "JUST", "BEEN", "HAVE", "WILL",
    "WHAT", "WHEN", "YOUR", "THAN", "THEM", "THEN", "SOME", "ONLY",
    "VERY", "MUCH", "MORE", "ALSO", "HERE", "LIKE", "NEXT", "BACK",
    "GOOD", "BEST", "EASY",
    # Trading jargon
    "BUY", "SELL", "LONG", "SHORT", "PUMP", "DUMP", "HODL", "MOON",
    "BULL", "BEAR", "DIP", "ATH", "ATL", "CALL", "PUT", "FOMO",
    "YUGE", "CHAD", "BAGS", "REKT", "JEET",
    # Crypto jargon
    "NFT", "DAO", "DEX", "CEX", "TVL", "APR", "APY", "ROI", "PNL",
    "GAS", "WEB", "SOL", "BSC", "ETH",
    # Stablecoins
    "USD", "USDT", "USDC", "BUSD", "DAI", "TUSD", "FRAX",
    # Common abbreviations
    "IMO", "NFA", "DYOR", "TBH", "FWIW", "LMAO", "LMFAO", "WTF",
    "LOL", "IDK", "BTW", "FYI",
    # Market cap references (not tickers)
    "MC", "CAP",
}

# Dollar-amount patterns to exclude: $50K, $1M, $700K, etc.
DOLLAR_AMOUNT_RE = re.compile(r"^\d+[KMBkmb]?$")


def _get_client() -> TelegramClient:
    return TelegramClient(
        SESSION_FILE,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )


def is_telethon_configured() -> bool:
    return bool(settings.telegram_api_id and settings.telegram_api_hash)


def has_telethon_session() -> bool:
    return os.path.exists(f"{SESSION_FILE}.session")


def _is_valid_ticker(t: str) -> bool:
    """Check if a string looks like a real ticker (not noise)."""
    t = t.upper()
    if t in _NOISE_WORDS:
        return False
    if len(t) < 2:
        return False
    if DOLLAR_AMOUNT_RE.match(t):
        return False
    return True


def extract_tickers(text: str) -> list[str]:
    """Extract ticker symbols from message text.

    Handles:
    - $TICKER format
    - Quickscope bot [TICKER](url) format
    """
    tickers = set()

    # 1. $TICKER
    for t in TICKER_RE.findall(text):
        if _is_valid_ticker(t):
            tickers.add(t.upper())

    # 2. Quickscope bot format: [TICKER](https://app.quickscope.gg/...)
    for t in QUICKSCOPE_RE.findall(text):
        if _is_valid_ticker(t):
            tickers.add(t.upper())

    return list(tickers)


def extract_contract_addresses(text: str) -> list[str]:
    """Extract Solana contract addresses from message text.

    Handles:
    - Raw addresses (base58, 32-44 chars)
    - DexScreener links
    - pump.fun addresses
    """
    addresses = set()

    # DexScreener links
    for addr in DEXSCREENER_RE.findall(text):
        addresses.add(addr)

    # Raw addresses in text
    for addr in SOL_CA_RE.findall(text):
        # Filter out things that are clearly not addresses:
        # - Too short and all letters (likely a word)
        # - URLs/domains
        if len(addr) < 30:
            continue
        # Skip if it looks like a URL component
        if addr in text and any(
            prefix in text.split(addr)[0][-10:]
            for prefix in ["http", "://", ".com", ".gg", ".io"]
            if text.split(addr)[0]
        ):
            # Only skip if it's part of a non-dex URL
            before = text.split(addr)[0]
            if "dexscreener" not in before and "pump" not in addr:
                continue
        addresses.add(addr)

    return list(addresses)


async def scrape_group_history(
    group: str | int,
    days_back: int = 90,
) -> list[dict]:
    """
    Scrape a Telegram group's message history and extract ticker calls.

    Returns list of dicts with keys:
        ticker, contract_address, message_id, message_text, posted_at, author
    """
    client = _get_client()
    await client.start()

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    calls: list[dict] = []
    msg_count = 0

    try:
        entity = await client.get_entity(group)
        title = getattr(entity, "title", group)
        logger.info("Scraping group: %s (looking back %d days)", title, days_back)

        async for msg in client.iter_messages(entity):
            if msg.date.replace(tzinfo=timezone.utc) < cutoff:
                break

            msg_count += 1
            if not msg.text:
                continue

            tickers = extract_tickers(msg.text)
            contract_addresses = extract_contract_addresses(msg.text)

            if not tickers and not contract_addresses:
                continue

            sender_name = ""
            if msg.sender:
                sender_name = (
                    getattr(msg.sender, "username", "")
                    or getattr(msg.sender, "first_name", "")
                    or str(msg.sender_id)
                )

            posted_at = msg.date.replace(tzinfo=None)

            # Prioritize messages with contract addresses — those are real calls.
            # Ticker-only mentions without a CA are often just casual chatter.
            if contract_addresses:
                ca = contract_addresses[0]
                if tickers:
                    # CA + ticker name (best case: "bought $TEDDY ... AdJSR8...pump")
                    for ticker in tickers:
                        calls.append(
                            {
                                "ticker": ticker,
                                "contract_address": ca,
                                "message_id": msg.id,
                                "message_text": msg.text[:500],
                                "posted_at": posted_at,
                                "author": sender_name,
                            }
                        )
                else:
                    # CA only, no ticker name — resolve name later via DexScreener
                    for addr in contract_addresses:
                        calls.append(
                            {
                                "ticker": addr[:8] + "...",
                                "contract_address": addr,
                                "message_id": msg.id,
                                "message_text": msg.text[:500],
                                "posted_at": posted_at,
                                "author": sender_name,
                            }
                        )
            elif tickers:
                # Ticker only, no CA — skip noise, only keep if it
                # looks like a real call (contains buy-intent keywords)
                lower = msg.text.lower()
                buy_signals = (
                    "bought", "aped", "ape", "grabbed", "buy",
                    "slap", "bid", "entry", "loaded", "accumul",
                    "bag", "shill",
                )
                if any(kw in lower for kw in buy_signals):
                    for ticker in tickers:
                        calls.append(
                            {
                                "ticker": ticker,
                                "contract_address": None,
                                "message_id": msg.id,
                                "message_text": msg.text[:500],
                                "posted_at": posted_at,
                                "author": sender_name,
                            }
                        )

        logger.info(
            "Scraped %d messages, found %d ticker calls", msg_count, len(calls)
        )
    finally:
        await client.disconnect()

    return calls
