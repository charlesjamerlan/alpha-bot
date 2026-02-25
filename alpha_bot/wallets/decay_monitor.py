"""Monitor wallet decay — estimate copier count and degrade wallet quality."""

from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import datetime
from typing import Callable, Awaitable

import httpx
from sqlalchemy import select as sa_select

from alpha_bot.config import settings
from alpha_bot.platform_intel.basescan import get_token_transfers
from alpha_bot.storage.database import async_session
from alpha_bot.wallets.models import PrivateWallet, WalletTransaction

logger = logging.getLogger(__name__)

_notify_fn: Callable[[str, str], Awaitable[None]] | None = None


def set_notify_fn(fn: Callable[[str, str], Awaitable[None]]) -> None:
    global _notify_fn
    _notify_fn = fn


async def decay_monitor_loop() -> None:
    """Periodically check active wallets for copier growth."""
    logger.info(
        "Decay monitor loop started (interval=%ds)",
        settings.wallet_decay_interval_seconds,
    )

    while True:
        try:
            await _check_decay()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Decay monitor error")

        await asyncio.sleep(settings.wallet_decay_interval_seconds)


async def _check_decay() -> None:
    """For each active wallet, estimate copiers from recent transactions."""
    if not settings.basescan_api_key:
        return

    async with async_session() as session:
        result = await session.execute(
            sa_select(PrivateWallet).where(
                PrivateWallet.status.in_(["active", "decaying"])
            )
        )
        wallets = list(result.scalars().all())

    if not wallets:
        return

    logger.info("Checking decay for %d wallets", len(wallets))
    status_changes: list[tuple[str, str, str]] = []  # (addr_short, old, new)

    async with httpx.AsyncClient(timeout=30) as client:
        for wallet in wallets:
            copier_counts = await _estimate_copiers(wallet.address, client)
            if not copier_counts:
                continue

            median_copiers = int(statistics.median(copier_counts))
            old_status = wallet.status

            async with async_session() as session:
                # Re-fetch for update
                w_result = await session.execute(
                    sa_select(PrivateWallet).where(
                        PrivateWallet.address == wallet.address
                    )
                )
                w = w_result.scalar_one_or_none()
                if not w:
                    continue

                w.estimated_copiers = median_copiers
                w.decay_score = min(median_copiers * 2, 100)
                w.last_updated = datetime.utcnow()

                # Status transitions
                new_status = w.status
                if median_copiers >= settings.wallet_copier_retire_threshold:
                    new_status = "retired"
                elif median_copiers >= settings.wallet_copier_retire_threshold // 2:
                    new_status = "decaying"
                else:
                    new_status = "active"

                if new_status != old_status:
                    w.status = new_status
                    addr_short = f"{wallet.address[:6]}...{wallet.address[-4:]}"
                    status_changes.append((addr_short, old_status, new_status))

                await session.commit()

            await asyncio.sleep(0.5)  # rate limit

    if status_changes and _notify_fn:
        lines = ["<b>Wallet Decay Update</b>\n"]
        for addr, old, new in status_changes:
            lines.append(f"  <code>{addr}</code>: {old} -> {new}")
        try:
            await _notify_fn("\n".join(lines), "HTML")
        except Exception:
            logger.debug("Failed to send decay notification")


async def _estimate_copiers(
    wallet_address: str, client: httpx.AsyncClient
) -> list[int]:
    """Pick 3 recent transactions and count how many other wallets bought within 30s."""
    # Get recent transactions for this wallet
    async with async_session() as session:
        result = await session.execute(
            sa_select(WalletTransaction)
            .where(WalletTransaction.wallet_address == wallet_address)
            .order_by(WalletTransaction.timestamp.desc())
            .limit(3)
        )
        recent_txs = list(result.scalars().all())

    if not recent_txs:
        return []

    copier_counts = []

    for tx in recent_txs:
        # Get all transfers for this token around the same time
        transfers = await get_token_transfers(tx.ca, client, offset=100)
        if not transfers:
            continue

        # Find our wallet's buy block
        wallet_block = tx.block_number
        if not wallet_block:
            continue

        # Count transfers within ~30s (roughly 2-3 blocks on Base at ~2s/block)
        block_window = 15  # ~30 seconds
        copiers = 0
        for t in transfers:
            try:
                t_block = int(t["blockNumber"])
            except (ValueError, TypeError):
                continue
            to_addr = t["to"].lower()
            if to_addr == wallet_address.lower():
                continue
            if wallet_block < t_block <= wallet_block + block_window:
                copiers += 1

        copier_counts.append(copiers)
        await asyncio.sleep(0.3)

    return copier_counts
