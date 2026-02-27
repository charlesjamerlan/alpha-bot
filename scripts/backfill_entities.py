#!/usr/bin/env python3
"""Backfill wallet entities by reverse-engineering top Base tokens.

Scans the first 100 transfers of each winning token, identifies wallets
appearing in 2+ winners, promotes them to private_wallets, and resolves
their entities via seed list + ENS + traces.

Usage:
    python scripts/backfill_entities.py
"""

import asyncio
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import select as sa_select

from alpha_bot.config import settings
from alpha_bot.platform_intel.basescan import get_token_transfers
from alpha_bot.storage.database import async_session, engine
from alpha_bot.storage.models import Base
from alpha_bot.wallets.models import PrivateWallet, WalletTransaction, WalletEntity

# Delay between tokens (RPC has its own internal rate limiting)
_TOKEN_DELAY = 3.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("backfill_entities")

# ── Top performing Base tokens to reverse-engineer ──
# These are the tokens that showed strength over the last 6-12 months.
# For each, we pull early buyers and find wallets appearing in multiple winners.
WINNING_TOKENS = [
    # Memecoins
    ("BRETT", "0x532f27101965dd16442e59d40670faf5ebb142e4"),
    ("TOSHI", "0xac1bd2486aaf3b5c0fc3fd868558b082a531b2b4"),
    ("DEGEN", "0x4ed4e862860bed51a9570b96d89af5e1b0efefed"),
    ("HIGHER", "0x0578d8a44db98b23bf096a382e016e29a5ce0ffe"),
    ("MFER", "0xe3086852a4b125803c815a158249ae468a3254ca"),
    ("DOGINME", "0x6921b130d297cc43754afba22e5eac0fbf8db75b"),
    ("KEYCAT", "0x9a26f5433671751c3276a065f57e5a02d2817973"),
    ("SKI", "0x768be13e1680b5ebe0024c42c896e3db59ec0149"),
    ("MIGGLES", "0xb1a03eda10342529bbf8eb700a06c60441fef25d"),
    # TIBBIR / Virtuals ecosystem
    ("TIBBIR", "0xa4a2e2ca3fbfe21aed83471d28b6f65a233c6e00"),
    ("VIRTUAL", "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b"),
    ("AIXBT", "0x4f9fd6be4a90f2620860d680c0d4d5fb53d1a825"),
    ("LUNA", "0x55cd6469f597452b5a7536e2cd98fde4c1247ee4"),
    ("GAME", "0x1c4cca7c5db003824208adda61bd749e55f463a3"),
    ("SEKOIA", "0x1185cb5122edad199bdbc0cbd7a0457e448f23c7"),
    # Clanker tokens
    ("CLANKER", "0x1bc0c42215582d5a085795f4badbac3ff36d1bcb"),
    ("LUM", "0x0fd7a301b51d0a83fcaf6718628174d527b373b6"),
    ("ANON", "0x0db510e79909666d6dec7f5e49370838c16d950f"),
    ("BNKR", "0x22af33fe49fd1fa80c7149773dde5890d3c76f3b"),
    # DeFi
    ("AERO", "0x940181a94a35a4569e4529a3cdfb74e38fd98631"),
    ("MORPHO", "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842"),
    ("WELL", "0xdcc822276d4e6bac33bfb1bad287f2b9b9f877a6"),
    # ZORA address 0x111... causes slow binary search — skip
    # Fartcoin (bridged from Solana, may have limited Base transfers)
    ("FARTCOIN", "0x2f6c17fa9f9bc3600346ab4e48c0701e1d5962ae"),
]

# Addresses to exclude (null, dead, routers, deployers, bridges)
EXCLUDED = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    # Clanker deployers
    "0x250c9fb2b411b48273f69879007803790a6aea47",
    "0x9b84fce5dcd9a38d2d01d5d72373f6b6b067c3e1",
    "0x732560fa1d1a76350b1a500155ba978031b53833",
    "0x375c15db32d28cecdcab5c03ab889bf15cbd2c5e",
    "0x5cc4a43f2681a03d9187f3ad6934c748a86d6119",
    "0xe85a59c628f7d27878aceb4bf3b35733630083a9",
    # Aerodrome router
    "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43",
    # Base bridge
    "0x3154cf16ccdb4c6d922629664174b904d80f2c35",
    "0x49048044d57e1c92a77f79988d21fa8faf74e97e",
}

MIN_APPEARANCES = 2  # Wallet must appear in 2+ winners


async def main():
    if not settings.basescan_api_key:
        logger.error("BASESCAN_API_KEY not set — cannot proceed")
        return

    # Ensure tables exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Starting entity backfill with %d winning tokens", len(WINNING_TOKENS))

    # ── Phase 1: Extract early buyers from each token ──
    wallet_counter: Counter[str] = Counter()
    wallet_tokens: dict[str, list[str]] = {}  # addr -> [tickers]
    wallet_txs: dict[str, list[dict]] = {}

    async with httpx.AsyncClient(timeout=120) as client:
        for i, (ticker, ca) in enumerate(WINNING_TOKENS):
            ca = ca.lower()
            logger.info("[%d/%d] Scanning %s (%s)...", i + 1, len(WINNING_TOKENS), ticker, ca[:12])

            try:
                transfers = await get_token_transfers(ca, client, offset=100)
            except Exception as exc:
                logger.warning("  Error fetching %s: %s", ticker, exc)
                await asyncio.sleep(_TOKEN_DELAY)
                continue

            if not transfers:
                logger.warning("  No transfers for %s", ticker)
                await asyncio.sleep(_TOKEN_DELAY)
                continue

            buyers_this_token: set[str] = set()
            for tx in transfers:
                buyer = tx["to"].lower()
                if buyer in EXCLUDED:
                    continue
                if buyer not in buyers_this_token:
                    buyers_this_token.add(buyer)
                    wallet_counter[buyer] += 1
                    wallet_tokens.setdefault(buyer, []).append(ticker)

                wallet_txs.setdefault(buyer, []).append({
                    "ca": ca,
                    "tx_hash": tx["hash"],
                    "block_number": int(tx["blockNumber"]) if tx["blockNumber"] else 0,
                    "timestamp": tx["timestamp"],
                    "token_symbol": ticker,
                })

            logger.info("  %s: %d unique early buyers", ticker, len(buyers_this_token))
            await asyncio.sleep(_TOKEN_DELAY)

    # ── Phase 2: Find wallets appearing in 2+ winners ──
    smart_wallets = {
        addr: count for addr, count in wallet_counter.items()
        if count >= MIN_APPEARANCES
    }
    logger.info(
        "Found %d wallets appearing in %d+ winners (out of %d total)",
        len(smart_wallets), MIN_APPEARANCES, len(wallet_counter),
    )

    # Sort by frequency
    sorted_wallets = sorted(smart_wallets.items(), key=lambda x: -x[1])

    # ── Phase 3: Save to private_wallets + wallet_transactions ──
    new_wallets = 0
    updated_wallets = 0

    async with async_session() as session:
        for addr, count in sorted_wallets:
            existing = await session.execute(
                sa_select(PrivateWallet).where(PrivateWallet.address == addr)
            )
            wallet = existing.scalar_one_or_none()

            quality = min(count / len(WINNING_TOKENS) * 100, 100.0)
            tokens_str = ", ".join(wallet_tokens.get(addr, [])[:5])

            if wallet:
                wallet.total_wins = max(wallet.total_wins, count)
                wallet.total_tracked = max(wallet.total_tracked, count)
                wallet.quality_score = max(wallet.quality_score, quality)
                wallet.last_updated = datetime.utcnow()
                if not wallet.label and tokens_str:
                    wallet.label = f"Early in {tokens_str}"
                updated_wallets += 1
            else:
                wallet = PrivateWallet(
                    address=addr,
                    label=f"Early in {tokens_str}",
                    source="backfill",
                    quality_score=quality,
                    total_wins=count,
                    total_tracked=count,
                    status="active",
                )
                session.add(wallet)
                new_wallets += 1

            # Record transactions
            txs = wallet_txs.get(addr, [])
            for tx_data in txs[:10]:  # Cap per wallet to avoid huge inserts
                tx_exists = await session.execute(
                    sa_select(WalletTransaction.id).where(
                        WalletTransaction.tx_hash == tx_data["tx_hash"]
                    ).limit(1)
                )
                if tx_exists.scalar_one_or_none():
                    continue

                ts = datetime.utcnow()
                try:
                    ts = datetime.utcfromtimestamp(int(tx_data["timestamp"]))
                except (ValueError, TypeError, OSError):
                    pass

                wt = WalletTransaction(
                    wallet_address=addr,
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

    logger.info(
        "Private wallets: %d new, %d updated",
        new_wallets, updated_wallets,
    )

    # ── Phase 4: Resolve entities for all smart wallets ──
    if not settings.entity_resolution_enabled:
        logger.warning(
            "ENTITY_RESOLUTION_ENABLED=false — skipping entity resolution. "
            "Set to true and re-run to resolve entities."
        )
        # Still do seed list matching even if disabled
        logger.info("Running seed-list-only resolution...")
        await _resolve_seed_only(sorted_wallets)
        return

    from alpha_bot.wallets.entity_resolver import resolve_entity

    resolved = 0
    skipped = 0
    errors = 0

    for i, (addr, count) in enumerate(sorted_wallets):
        try:
            entity = await resolve_entity(addr)
            if entity and entity.entity_name:
                resolved += 1
                tokens_str = ", ".join(wallet_tokens.get(addr, [])[:3])
                logger.info(
                    "  [%d/%d] %s -> %s (%s) [in %d winners: %s]",
                    i + 1, len(sorted_wallets),
                    addr[:10], entity.entity_name,
                    entity.resolution_source, count, tokens_str,
                )
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            if errors <= 5:
                logger.warning("  Resolution error for %s: %s", addr[:10], exc)

        # Rate limit for ENS + BaseScan calls
        if (i + 1) % 10 == 0:
            logger.info("  Progress: %d/%d (resolved: %d)", i + 1, len(sorted_wallets), resolved)
            await asyncio.sleep(1)

    logger.info(
        "Entity resolution complete: %d resolved, %d unknown, %d errors (of %d total)",
        resolved, skipped, errors, len(sorted_wallets),
    )

    # ── Summary ──
    async with async_session() as session:
        pw_count = (await session.execute(
            sa_select(sa_select(PrivateWallet).subquery().c.id)
        )).all()
        we_count = (await session.execute(
            sa_select(sa_select(WalletEntity).subquery().c.id)
        )).all()
        logger.info("Final DB state: %d private wallets, %d entities", len(pw_count), len(we_count))


async def _resolve_seed_only(sorted_wallets: list[tuple[str, int]]) -> None:
    """Match wallets against seed list without API calls."""
    from alpha_bot.wallets.entity_resolver import _load_seed_list, _SEED_BY_ADDR

    _load_seed_list()
    matched = 0

    async with async_session() as session:
        for addr, count in sorted_wallets:
            seed = _SEED_BY_ADDR.get(addr)
            if not seed:
                continue

            existing = await session.execute(
                sa_select(WalletEntity).where(WalletEntity.address == addr)
            )
            if existing.scalar_one_or_none():
                continue

            entity = WalletEntity(
                address=addr,
                entity_type=seed.get("entity_type", "unknown"),
                entity_name=seed.get("entity_name", ""),
                organization=seed.get("organization"),
                resolution_source="seed",
                confidence=1.0,
            )
            session.add(entity)
            matched += 1
            logger.info("  Seed match: %s -> %s", addr[:10], entity.entity_name)

        await session.commit()

    logger.info("Seed-only resolution: %d matches", matched)


if __name__ == "__main__":
    asyncio.run(main())
