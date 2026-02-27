"""Smart wallet buy monitor — detects new buys by tracked wallets in real-time.

Polls BaseScan for recent ERC-20 transfers for each active private wallet.
Fires TG alerts when quality wallets buy new tokens, with cluster convergence
detection for multi-wallet signals.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable, Coroutine

import httpx
from sqlalchemy import select

from alpha_bot.config import settings
from alpha_bot.platform_intel.basescan import get_address_token_transfers, _RATE_LIMIT_SLEEP
from alpha_bot.research.dexscreener import extract_pair_details, get_token_by_address
from alpha_bot.storage.database import async_session
from alpha_bot.wallets.models import PrivateWallet, WalletCluster, WalletTransaction

logger = logging.getLogger(__name__)

# Notification callback (set from main.py)
_notify_fn: Callable[[str, str], Coroutine] | None = None

# Track last checked block per wallet to avoid re-scanning old txns
_last_block: dict[str, int] = {}

# Recent buys for cluster convergence detection: {ca: [(wallet_addr, cluster_id, timestamp)]}
_recent_buys: dict[str, list[tuple[str, int | None, datetime]]] = defaultdict(list)
_CONVERGENCE_WINDOW = timedelta(minutes=5)


def set_notify_fn(fn: Callable[[str, str], Coroutine]) -> None:
    global _notify_fn
    _notify_fn = fn


async def _notify(text: str) -> None:
    if _notify_fn:
        try:
            await _notify_fn(text, "HTML")
        except Exception as exc:
            logger.warning("Wallet buy monitor notify failed: %s", exc)


def _fmt_mcap(n: float | None) -> str:
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.0f}"


async def _get_active_wallets() -> list[PrivateWallet]:
    """Get all active/decaying wallets above min quality threshold."""
    async with async_session() as session:
        result = await session.execute(
            select(PrivateWallet)
            .where(
                PrivateWallet.status.in_(["active", "decaying"]),
                PrivateWallet.quality_score >= settings.wallet_buy_min_quality,
            )
            .order_by(PrivateWallet.quality_score.desc())
        )
        return list(result.scalars().all())


async def _tx_exists(tx_hash: str) -> bool:
    """Check if transaction hash already recorded."""
    async with async_session() as session:
        result = await session.execute(
            select(WalletTransaction.id)
            .where(WalletTransaction.tx_hash == tx_hash)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


async def _enrich_token(ca: str, client: httpx.AsyncClient) -> dict | None:
    """Get market data for a token from DexScreener."""
    pair = await get_token_by_address(ca, client)
    if not pair:
        return None
    d = extract_pair_details(pair)
    return {
        "symbol": d.get("symbol", "?"),
        "name": d.get("name", "?"),
        "mcap": d.get("market_cap"),
        "liquidity_usd": d.get("liquidity_usd"),
        "price_usd": d.get("price_usd"),
        "pair_age_hours": None,
    }


def _prune_recent_buys() -> None:
    """Remove entries older than convergence window."""
    cutoff = datetime.utcnow() - _CONVERGENCE_WINDOW
    for ca in list(_recent_buys.keys()):
        _recent_buys[ca] = [
            (addr, cid, ts)
            for addr, cid, ts in _recent_buys[ca]
            if ts >= cutoff
        ]
        if not _recent_buys[ca]:
            del _recent_buys[ca]


def _check_cluster_convergence(
    ca: str,
) -> list[tuple[str, int | None, datetime]] | None:
    """Check if 2+ wallets in the same cluster bought the same CA recently.

    Returns the list of matching buy entries if convergence detected, else None.
    """
    buys = _recent_buys.get(ca, [])
    if len(buys) < 2:
        return None

    # Group by cluster_id (skip None = no cluster)
    by_cluster: dict[int, list] = defaultdict(list)
    for addr, cid, ts in buys:
        if cid is not None:
            by_cluster[cid].append((addr, cid, ts))

    for cluster_id, entries in by_cluster.items():
        if len(entries) >= 2:
            return entries

    return None


async def wallet_buy_monitor_loop() -> None:
    """Poll BaseScan for wallet buys every N seconds."""
    interval = settings.wallet_buy_monitor_interval_seconds
    logger.info("Wallet buy monitor started (interval=%ds)", interval)

    while True:
        try:
            wallets = await _get_active_wallets()
            if not wallets:
                logger.debug("Wallet buy monitor: no active wallets, sleeping")
                await asyncio.sleep(interval)
                continue

            new_buys = 0

            async with httpx.AsyncClient(timeout=30) as client:
                for wallet in wallets:
                    addr = wallet.address.lower()
                    start_block = _last_block.get(addr, 0)

                    transfers = await get_address_token_transfers(
                        address=addr,
                        client=client,
                        start_block=start_block,
                        page=1,
                        offset=50,
                        sort="desc",
                    )

                    if not transfers:
                        await asyncio.sleep(_RATE_LIMIT_SLEEP)
                        continue

                    # Update last seen block
                    max_block = 0
                    for tx in transfers:
                        try:
                            bn = int(tx.get("blockNumber", 0))
                            if bn > max_block:
                                max_block = bn
                        except (ValueError, TypeError):
                            pass
                    if max_block > 0:
                        _last_block[addr] = max_block

                    for tx in transfers:
                        tx_hash = tx.get("hash", "")
                        if not tx_hash:
                            continue

                        # Filter: wallet is the recipient (= buying / receiving tokens)
                        to_addr = tx.get("to", "").lower()
                        if to_addr != addr:
                            continue

                        # Skip if already recorded
                        if await _tx_exists(tx_hash):
                            continue

                        token_ca = tx.get("contractAddress", "").lower()
                        if not token_ca:
                            continue

                        # Parse timestamp
                        ts_str = tx.get("timestamp", "")
                        try:
                            ts = datetime.utcfromtimestamp(int(ts_str))
                        except (ValueError, TypeError):
                            ts = datetime.utcnow()

                        token_symbol = tx.get("tokenSymbol", "")

                        # Save transaction
                        try:
                            bn = int(tx.get("blockNumber", 0))
                        except (ValueError, TypeError):
                            bn = 0

                        wtx = WalletTransaction(
                            wallet_address=addr,
                            ca=token_ca,
                            chain="base",
                            tx_hash=tx_hash,
                            block_number=bn,
                            timestamp=ts,
                            token_symbol=token_symbol,
                        )
                        async with async_session() as session:
                            session.add(wtx)
                            await session.commit()

                        new_buys += 1

                        # Conviction signal registration
                        try:
                            from alpha_bot.conviction.engine import register_signal, compute_wallet_weight
                            await register_signal(
                                ca=token_ca,
                                source="wallet_buy",
                                weight=compute_wallet_weight(wallet.quality_score),
                                metadata={
                                    "wallet_address": addr,
                                    "wallet_quality": wallet.quality_score,
                                    "ticker": token_symbol,
                                    "chain": "base",
                                },
                            )
                        except Exception:
                            pass

                        # Track for cluster convergence
                        _recent_buys[token_ca].append(
                            (addr, wallet.cluster_id, datetime.utcnow())
                        )

                        # Fire alert if wallet quality meets threshold
                        if wallet.quality_score >= settings.wallet_buy_alert_min_quality:
                            # Enrich token data
                            token_info = await _enrich_token(token_ca, client)
                            symbol = (
                                token_info["symbol"]
                                if token_info
                                else token_symbol or "?"
                            )
                            mcap = token_info["mcap"] if token_info else None
                            liq = (
                                token_info["liquidity_usd"] if token_info else None
                            )

                            # Entity resolution
                            entity_line = ""
                            try:
                                from alpha_bot.wallets.entity_resolver import get_entity_by_address
                                entity = await get_entity_by_address(addr)
                                if entity and entity.entity_name:
                                    org_str = f", {entity.organization}" if entity.organization else ""
                                    entity_line = (
                                        f"Entity: <b>{entity.entity_name}</b> "
                                        f"({entity.entity_type.upper()}{org_str})\n"
                                    )
                                elif settings.entity_resolution_enabled and wallet.quality_score >= 70:
                                    # Background resolve for high-quality unknown wallets
                                    from alpha_bot.wallets.entity_resolver import resolve_entity
                                    entity = await resolve_entity(addr)
                                    if entity and entity.entity_name:
                                        org_str = f", {entity.organization}" if entity.organization else ""
                                        entity_line = (
                                            f"Entity: <b>{entity.entity_name}</b> "
                                            f"({entity.entity_type.upper()}{org_str})\n"
                                        )
                            except Exception:
                                pass

                            addr_short = f"{addr[:6]}...{addr[-4:]}"
                            alert_text = (
                                f"\U0001f45b <b>WALLET BUY: ${symbol}</b>\n\n"
                                f"Wallet: <code>{addr_short}</code> (Q: {wallet.quality_score:.0f}/100)\n"
                                f"{entity_line}"
                                f"Status: {wallet.status.title()} | "
                                f"Copiers: {wallet.estimated_copiers}\n\n"
                                f"Token: ${symbol} | Chain: Base\n"
                                f"MCap: {_fmt_mcap(mcap)} | Liq: {_fmt_mcap(liq)}\n\n"
                                f"<code>{token_ca}</code>"
                            )
                            await _notify(alert_text)

                    await asyncio.sleep(_RATE_LIMIT_SLEEP)

            # --- Cluster convergence check ---
            _prune_recent_buys()
            for ca, buys in list(_recent_buys.items()):
                convergence = _check_cluster_convergence(ca)
                if convergence:
                    # Build alert
                    # Enrich token
                    try:
                        async with httpx.AsyncClient(timeout=15) as client:
                            token_info = await _enrich_token(ca, client)
                    except Exception:
                        token_info = None

                    symbol = token_info["symbol"] if token_info else "?"
                    mcap = token_info["mcap"] if token_info else None
                    liq = token_info["liquidity_usd"] if token_info else None
                    cluster_id = convergence[0][1]

                    wallet_lines = []
                    for w_addr, _, w_ts in convergence:
                        short = f"{w_addr[:6]}...{w_addr[-4:]}"
                        ago = int((datetime.utcnow() - w_ts).total_seconds() / 60)
                        # Look up quality
                        q = "?"
                        try:
                            async with async_session() as session:
                                r = await session.execute(
                                    select(PrivateWallet.quality_score)
                                    .where(PrivateWallet.address == w_addr)
                                )
                                q_val = r.scalar_one_or_none()
                                if q_val is not None:
                                    q = f"{q_val:.0f}"
                        except Exception:
                            pass
                        wallet_lines.append(
                            f"  <code>{short}</code> (Q: {q}) — {ago} min ago"
                        )

                    wallets_text = "\n".join(wallet_lines)
                    convergence_text = (
                        f"\U0001f534 <b>CLUSTER CONVERGENCE: ${symbol}</b>\n\n"
                        f"{len(convergence)} wallets in Cluster-{cluster_id} bought within 5 min:\n"
                        f"{wallets_text}\n\n"
                        f"Token: ${symbol} | Chain: Base\n"
                        f"MCap: {_fmt_mcap(mcap)} | Liq: {_fmt_mcap(liq)}\n\n"
                        f"<code>{ca}</code>"
                    )
                    await _notify(convergence_text)

                    # Clear to avoid re-alerting
                    _recent_buys[ca] = []

            if new_buys > 0:
                logger.info(
                    "Wallet buy monitor: %d new buys detected across %d wallets",
                    new_buys,
                    len(wallets),
                )

        except Exception:
            logger.exception("Wallet buy monitor error")

        await asyncio.sleep(interval)
