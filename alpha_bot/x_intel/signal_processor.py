"""X/Twitter signal processor — polls new X signals and feeds them into the conviction engine.

Runs as a background loop. For each unprocessed XSignal with a contract address,
registers it with the conviction engine so it can contribute to multi-source
conviction alerts (alongside TG, Scanner, Wallet, Clanker signals).
"""

import asyncio
import json
import logging
from typing import Callable, Coroutine

from sqlalchemy import select as sa_select

from alpha_bot.config import settings
from alpha_bot.storage.database import async_session
from alpha_bot.x_intel.models import XSignal

logger = logging.getLogger(__name__)

_notify_fn: Callable[[str, str], Coroutine] | None = None

_POLL_INTERVAL = 30  # seconds


def set_notify_fn(fn: Callable[[str, str], Coroutine]) -> None:
    global _notify_fn
    _notify_fn = fn


async def _notify(text: str) -> None:
    if _notify_fn:
        try:
            await _notify_fn(text, "HTML")
        except Exception as exc:
            logger.warning("X signal notify failed: %s", exc)


async def x_signal_processor_loop() -> None:
    """Background loop: process new X signals into conviction engine."""
    logger.info("X signal processor started (poll every %ds)", _POLL_INTERVAL)

    while True:
        try:
            await _process_new_signals()
        except Exception:
            logger.exception("X signal processor error")

        await asyncio.sleep(_POLL_INTERVAL)


async def _process_new_signals() -> None:
    """Find unprocessed X signals and register them with conviction engine."""
    from alpha_bot.conviction.engine import compute_x_weight, register_signal

    async with async_session() as session:
        result = await session.execute(
            sa_select(XSignal)
            .where(XSignal.processed == False)  # noqa: E712
            .order_by(XSignal.tweeted_at.asc())
            .limit(50)
        )
        signals = list(result.scalars().all())

        if not signals:
            return

        registered = 0
        skipped = 0

        for sig in signals:
            # Extract contract addresses
            cas = []
            try:
                cas = json.loads(sig.contract_addresses) if sig.contract_addresses else []
            except (json.JSONDecodeError, TypeError):
                pass

            # Extract cashtags for ticker info
            cashtags = []
            try:
                cashtags = json.loads(sig.cashtags) if sig.cashtags else []
            except (json.JSONDecodeError, TypeError):
                pass

            ticker = cashtags[0].lstrip("$") if cashtags else ""

            # Mark as processed regardless — we don't want to re-process
            sig.processed = True

            # Need at least one CA to register with conviction engine
            if not cas:
                skipped += 1
                continue

            weight = compute_x_weight(sig.signal_type, sig.author_followers)

            # Register each CA mentioned in the tweet
            for ca in cas:
                ca = ca.strip()
                if not ca:
                    continue

                await register_signal(
                    ca=ca,
                    source="x_kol",
                    weight=weight,
                    metadata={
                        "author": sig.author_username,
                        "followers": sig.author_followers or 0,
                        "signal_type": sig.signal_type,
                        "tweet_url": sig.tweet_url or "",
                        "ticker": ticker,
                        "chain": _detect_chain(ca),
                    },
                )
                registered += 1

        await session.commit()

        if registered or skipped:
            logger.info(
                "X signal processor: %d registered, %d skipped (no CA) from %d signals",
                registered, skipped, len(signals),
            )


def _detect_chain(ca: str) -> str:
    """Simple chain detection from address format."""
    if ca.startswith("0x") and len(ca) == 42:
        return "base"
    return "solana"
