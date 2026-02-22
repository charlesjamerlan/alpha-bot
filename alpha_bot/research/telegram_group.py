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

# EVM contract addresses — 0x + 40 hex chars (BASE, ETH, BSC, etc.)
EVM_CA_RE = re.compile(r"\b(0x[0-9a-fA-F]{40})\b")

# DexScreener links: dexscreener.com/<chain>/<address>
DEXSCREENER_RE = re.compile(
    r"dexscreener\.com/solana/([1-9A-HJ-NP-Za-km-z]{32,44})"
)
DEXSCREENER_EVM_RE = re.compile(
    r"dexscreener\.com/(base|ethereum|bsc)/(0x[0-9a-fA-F]{40})"
)

# Basescan links: basescan.org/token/0x...
BASESCAN_RE = re.compile(r"basescan\.org/token/(0x[0-9a-fA-F]{40})")

# Known EVM chain keywords that appear near addresses in messages
_EVM_CHAIN_HINTS = {"base", "eth", "ethereum", "bsc", "binance"}

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
    """Extract contract addresses from message text (Solana + EVM).

    Handles:
    - Solana: Raw base58 addresses, pump.fun addresses, DexScreener /solana/ links
    - EVM: 0x addresses (BASE/ETH/BSC), DexScreener /base/ links, Basescan links
    """
    addresses = set()

    # DexScreener links (Solana)
    for addr in DEXSCREENER_RE.findall(text):
        addresses.add(addr)

    # DexScreener links (EVM) — returns (chain, address) tuples
    for _chain, addr in DEXSCREENER_EVM_RE.findall(text):
        addresses.add(addr)

    # Basescan links
    for addr in BASESCAN_RE.findall(text):
        addresses.add(addr)

    # EVM addresses (0x...)
    for addr in EVM_CA_RE.findall(text):
        addresses.add(addr)

    # Raw Solana addresses in text
    for addr in SOL_CA_RE.findall(text):
        if len(addr) < 30:
            continue
        # Skip if it looks like a URL component
        if addr in text and any(
            prefix in text.split(addr)[0][-10:]
            for prefix in ["http", "://", ".com", ".gg", ".io"]
            if text.split(addr)[0]
        ):
            before = text.split(addr)[0]
            if "dexscreener" not in before and "pump" not in addr:
                continue
        addresses.add(addr)

    return list(addresses)


def detect_chain(address: str, message_text: str = "") -> str:
    """Detect blockchain from address format and message context.

    Returns: 'solana', 'base', 'ethereum', 'bsc', or 'unknown'.
    """
    # EVM address
    if address.startswith("0x") and len(address) == 42:
        lower = message_text.lower()
        # Check DexScreener links for explicit chain
        dex_match = DEXSCREENER_EVM_RE.search(message_text)
        if dex_match:
            chain = dex_match.group(1).lower()
            if chain == "bsc":
                return "bsc"
            if chain == "ethereum":
                return "ethereum"
            return "base"
        # Check basescan link
        if "basescan" in lower:
            return "base"
        # Check text hints
        if "base" in lower or "base chain" in lower:
            return "base"
        if "bsc" in lower or "binance" in lower or "pancake" in lower:
            return "bsc"
        if "ethereum" in lower or "uniswap" in lower:
            return "ethereum"
        # Default EVM to base (most common for memecoins right now)
        return "base"

    # Solana address (base58)
    return "solana"


async def scrape_group_history(
    group: str | int,
    days_back: int = 90,
    topic_id: int | None = None,
) -> list[dict]:
    """
    Scrape a Telegram group's message history and extract ticker calls.

    Args:
        group: Group username or numeric ID.
        days_back: How many days to look back.
        topic_id: If set, only scrape messages from this forum topic/sub-channel.

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
        topic_label = f" (topic {topic_id})" if topic_id else ""
        logger.info("Scraping group: %s%s (looking back %d days)", title, topic_label, days_back)

        # Build iter_messages kwargs — reply_to filters by forum topic
        iter_kwargs = {}
        if topic_id is not None:
            iter_kwargs["reply_to"] = topic_id

        async for msg in client.iter_messages(entity, **iter_kwargs):
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
                chain = detect_chain(ca, msg.text)
                if tickers:
                    # CA + ticker name (best case: "bought $TEDDY ... AdJSR8...pump")
                    for ticker in tickers:
                        calls.append(
                            {
                                "ticker": ticker,
                                "contract_address": ca,
                                "chain": chain,
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
                                "chain": detect_chain(addr, msg.text),
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
