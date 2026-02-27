"""Reverse-engineer early buyers in winning tokens to discover smart wallets."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime
from typing import Callable, Awaitable

import httpx
from sqlalchemy import select as sa_select

from alpha_bot.config import settings
from alpha_bot.platform_intel.basescan import get_token_transfers
from alpha_bot.storage.database import async_session
from alpha_bot.wallets.models import PrivateWallet, WalletTransaction

logger = logging.getLogger(__name__)

# Known addresses to exclude (null address, routers, deployers)
_EXCLUDED_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

_notify_fn: Callable[[str, str], Awaitable[None]] | None = None


def set_notify_fn(fn: Callable[[str, str], Awaitable[None]]) -> None:
    global _notify_fn
    _notify_fn = fn


async def reverse_engineer_loop() -> None:
    """Periodically scan winning tokens for early buyers."""
    logger.info(
        "Reverse-engineer loop started (interval=%ds)",
        settings.wallet_scan_interval_seconds,
    )

    while True:
        try:
            await _scan_winners()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Reverse-engineer loop error")

        await asyncio.sleep(settings.wallet_scan_interval_seconds)


async def _scan_winners() -> None:
    """Find winning tokens and extract early buyer wallets."""
    from alpha_bot.platform_intel.models import PlatformToken
    from alpha_bot.tg_intel.models import CallOutcome

    if not settings.basescan_api_key:
        logger.debug("BaseScan API key not set, skipping wallet scan")
        return

    winner_cas: list[str] = []

    async with async_session() as session:
        # Source 1: Platform tokens that reached $1M mcap
        pt_result = await session.execute(
            sa_select(PlatformToken.ca).where(
                PlatformToken.reached_1m == True,  # noqa: E712
                PlatformToken.chain == "base",
            )
        )
        winner_cas.extend(row[0] for row in pt_result.all())

        # Source 2: TG call outcomes with 10x+ ROI (900%+)
        co_result = await session.execute(
            sa_select(CallOutcome.ca).where(
                CallOutcome.roi_peak >= 900.0,
                CallOutcome.chain == "base",
            )
        )
        winner_cas.extend(row[0] for row in co_result.all())

    # Deduplicate
    winner_cas = list(set(winner_cas))
    if not winner_cas:
        logger.debug("No winning tokens found for wallet scan")
        return

    logger.info("Scanning %d winning tokens for early buyers", len(winner_cas))

    # Track how many winning tokens each wallet appears in
    wallet_counter: Counter[str] = Counter()
    wallet_txs: dict[str, list[dict]] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        for ca in winner_cas:
            transfers = await get_token_transfers(ca, client, offset=50)
            if not transfers:
                continue

            # Extract unique buyer addresses (the "to" address in early transfers)
            for tx in transfers:
                buyer = tx["to"].lower()
                if buyer in _EXCLUDED_ADDRESSES:
                    continue
                wallet_counter[buyer] += 1
                wallet_txs.setdefault(buyer, []).append({
                    "ca": ca,
                    "tx_hash": tx["hash"],
                    "block_number": int(tx["blockNumber"]) if tx["blockNumber"] else 0,
                    "timestamp": tx["timestamp"],
                    "token_symbol": tx["tokenSymbol"],
                })

            # Rate limit
            await asyncio.sleep(0.3)

    # Promote wallets appearing in 3+ winners
    min_appearances = settings.wallet_min_winner_appearances
    new_wallets = 0

    async with async_session() as session:
        for address, count in wallet_counter.items():
            if count < min_appearances:
                continue

            # Check if already tracked
            existing = await session.execute(
                sa_select(PrivateWallet).where(PrivateWallet.address == address)
            )
            wallet = existing.scalar_one_or_none()

            if wallet:
                # Update existing
                wallet.total_wins = count
                wallet.total_tracked = max(wallet.total_tracked, count)
                wallet.quality_score = min(count / len(winner_cas) * 100, 100.0)
                wallet.last_updated = datetime.utcnow()
            else:
                # New discovery
                wallet = PrivateWallet(
                    address=address,
                    source="reverse_engineer",
                    quality_score=min(count / max(len(winner_cas), 1) * 100, 100.0),
                    total_wins=count,
                    total_tracked=count,
                    status="active",
                )
                session.add(wallet)
                new_wallets += 1

            # Record transactions
            txs = wallet_txs.get(address, [])
            for tx_data in txs:
                tx_exists = await session.execute(
                    sa_select(WalletTransaction).where(
                        WalletTransaction.tx_hash == tx_data["tx_hash"]
                    )
                )
                if tx_exists.scalar_one_or_none():
                    continue

                ts = datetime.utcnow()
                try:
                    ts = datetime.utcfromtimestamp(int(tx_data["timestamp"]))
                except (ValueError, TypeError, OSError):
                    pass

                wt = WalletTransaction(
                    wallet_address=address,
                    ca=tx_data["ca"],
                    chain="base",
                    tx_hash=tx_data["tx_hash"],
                    block_number=tx_data["block_number"],
                    timestamp=ts,
                    token_symbol=tx_data["token_symbol"],
                    is_winner=True,
                )
                session.add(wt)

        await session.commit()

    if new_wallets > 0:
        logger.info("Discovered %d new private wallets", new_wallets)

        # Resolve entities for newly discovered wallets
        try:
            from alpha_bot.wallets.entity_resolver import resolve_entity
            from alpha_bot.config import settings as _settings
            if _settings.entity_resolution_enabled:
                resolved = 0
                for address, count in wallet_counter.items():
                    if count < min_appearances:
                        continue
                    entity = await resolve_entity(address)
                    if entity and entity.entity_name:
                        resolved += 1
                if resolved:
                    logger.info("Resolved %d entities from new wallets", resolved)
        except Exception:
            logger.debug("Entity resolution skipped during wallet discovery")

        if _notify_fn:
            try:
                await _notify_fn(
                    f"<b>Wallet Discovery</b>\n\n"
                    f"Found <b>{new_wallets}</b> new wallet(s) appearing in "
                    f"{min_appearances}+ winning tokens.\n"
                    f"Total winners scanned: {len(winner_cas)}",
                    "HTML",
                )
            except Exception:
                logger.debug("Failed to send wallet discovery notification")
