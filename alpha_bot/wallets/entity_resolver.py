"""Wallet entity resolution — map addresses to known identities.

Resolution pipeline (ordered by speed/reliability):
  1. DB cache check
  2. Seed list check (seed_entities.json)
  3. ENS reverse lookup (via The Graph subgraph)
  4. Deployer trace (BaseScan contract creation)
  5. Funding chain trace (first ETH transfers to wallet)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import select as sa_select

from alpha_bot.config import settings
from alpha_bot.storage.database import async_session
from alpha_bot.wallets.models import WalletEntity

logger = logging.getLogger(__name__)

_SEED_LIST: list[dict] | None = None
_SEED_BY_ADDR: dict[str, dict] = {}

# Known exchange deposit addresses (funding sources)
_KNOWN_FUNDING_SOURCES: dict[str, tuple[str, str]] = {
    # Coinbase
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": ("institution", "Coinbase"),
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": ("institution", "Coinbase"),
    "0x503828976d22510aad0201ac7ec88293211d23da": ("institution", "Coinbase"),
    "0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511": ("institution", "Coinbase"),
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740": ("institution", "Coinbase"),
    "0x3cd751e6b0078be393132286c442345e5dc49699": ("institution", "Coinbase"),
    "0x7830c87c02e56aff27fa8ab1241711331fa86f43": ("institution", "Coinbase"),
    "0xedc7001e99a37c3d23b5f7974f837387e09f9c93": ("institution", "Coinbase"),
    # Binance
    "0x28c6c06298d514db089934071355e5743bf21d60": ("institution", "Binance"),
    "0xf977814e90da44bfa03b6295a0616a897441acec": ("institution", "Binance"),
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be": ("institution", "Binance"),
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": ("institution", "Binance"),
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": ("institution", "Binance"),
    # OKX
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": ("institution", "OKX"),
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f": ("institution", "OKX"),
    # Bybit
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": ("institution", "Bybit"),
    "0x1db92e2eebc8e0c075a02bea49a2935bcd2dfcf4": ("institution", "Bybit"),
}


def _load_seed_list() -> None:
    """Load seed_entities.json once."""
    global _SEED_LIST, _SEED_BY_ADDR
    if _SEED_LIST is not None:
        return

    seed_path = Path(__file__).parent / "seed_entities.json"
    try:
        with open(seed_path) as f:
            _SEED_LIST = json.load(f)
        _SEED_BY_ADDR = {e["address"].lower(): e for e in _SEED_LIST}
        logger.info("Loaded %d seed entities", len(_SEED_LIST))
    except FileNotFoundError:
        _SEED_LIST = []
        _SEED_BY_ADDR = {}
        logger.warning("seed_entities.json not found")
    except (json.JSONDecodeError, KeyError) as exc:
        _SEED_LIST = []
        _SEED_BY_ADDR = {}
        logger.warning("Failed to parse seed_entities.json: %s", exc)


async def resolve_entity(address: str) -> WalletEntity | None:
    """Resolve a wallet address to an entity. Returns cached or newly created entity."""
    if not settings.entity_resolution_enabled:
        return None

    address = address.lower().strip()
    if not address.startswith("0x") or len(address) != 42:
        return None

    # 1. DB cache check
    async with async_session() as session:
        result = await session.execute(
            sa_select(WalletEntity).where(WalletEntity.address == address)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

    # 2. Seed list check
    _load_seed_list()
    seed = _SEED_BY_ADDR.get(address)
    if seed:
        entity = WalletEntity(
            address=address,
            entity_type=seed.get("entity_type", "unknown"),
            entity_name=seed.get("entity_name", ""),
            organization=seed.get("organization"),
            resolution_source="seed",
            confidence=1.0,
        )
        return await _save_entity(entity)

    # 3. ENS reverse lookup
    ens_entity = await _resolve_ens(address)
    if ens_entity:
        return await _save_entity(ens_entity)

    # 4. Deployer trace
    if settings.basescan_api_key:
        deployer_entity = await _resolve_deployer_trace(address)
        if deployer_entity:
            return await _save_entity(deployer_entity)

        # 5. Funding chain trace
        funding_entity = await _resolve_funding_trace(address)
        if funding_entity:
            return await _save_entity(funding_entity)

    return None


async def _save_entity(entity: WalletEntity) -> WalletEntity:
    """Persist entity to DB, handling duplicates."""
    async with async_session() as session:
        # Check again for race conditions
        result = await session.execute(
            sa_select(WalletEntity).where(WalletEntity.address == entity.address)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

        session.add(entity)
        await session.commit()
        await session.refresh(entity)
        logger.info(
            "Resolved entity: %s -> %s (%s, conf=%.1f)",
            entity.address[:10], entity.entity_name, entity.resolution_source,
            entity.confidence,
        )
        return entity


async def _resolve_ens(address: str) -> WalletEntity | None:
    """ENS reverse lookup via The Graph subgraph."""
    query = """
    {
      domains(where: {resolvedAddress: "%s"}, first: 1) {
        name
      }
    }
    """ % address

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                settings.entity_ens_subgraph_url,
                json={"query": query},
            )
            resp.raise_for_status()
            data = resp.json()

        domains = data.get("data", {}).get("domains", [])
        if not domains:
            return None

        ens_name = domains[0].get("name", "")
        if not ens_name:
            return None

        return WalletEntity(
            address=address,
            entity_type="unknown",
            entity_name=ens_name,
            resolution_source="ens",
            confidence=0.9,
            ens_name=ens_name,
        )
    except Exception as exc:
        logger.debug("ENS lookup failed for %s: %s", address[:10], exc)
        return None


async def _resolve_deployer_trace(address: str) -> WalletEntity | None:
    """Check if this wallet received tokens from 0x0 (mint/deploy events)."""
    from alpha_bot.platform_intel.basescan import _get_logs_transfers, _RATE_LIMIT_SLEEP
    import asyncio

    try:
        await asyncio.sleep(_RATE_LIMIT_SLEEP)

        async with httpx.AsyncClient(timeout=30) as client:
            # Get token transfers TO this wallet (includes mints from 0x0)
            transfers = await _get_logs_transfers(
                client=client,
                wallet_address=address,
                max_results=30,
            )

        if not transfers:
            return None

        # Count how many transfers are mints (from 0x0 address)
        deploy_count = sum(
            1 for tx in transfers
            if tx.get("from", "").lower() == "0x0000000000000000000000000000000000000000"
        )

        if deploy_count >= 5:
            return WalletEntity(
                address=address,
                entity_type="deployer",
                entity_name=f"Deployer ({deploy_count}+ tokens)",
                resolution_source="deployer_trace",
                confidence=0.6,
            )

        return None
    except Exception as exc:
        logger.debug("Deployer trace failed for %s: %s", address[:10], exc)
        return None


async def _resolve_funding_trace(address: str) -> WalletEntity | None:
    """Check early ETH transfers to this wallet to identify funding source.

    Uses RPC eth_getLogs to find early ERC-20 transfers TO this wallet,
    then checks if the sender is a known exchange/institution.
    """
    from alpha_bot.platform_intel.basescan import _get_logs_transfers, _RATE_LIMIT_SLEEP
    import asyncio

    try:
        await asyncio.sleep(_RATE_LIMIT_SLEEP)

        async with httpx.AsyncClient(timeout=30) as client:
            transfers = await _get_logs_transfers(
                client=client,
                wallet_address=address,
                max_results=10,
            )

        if not transfers:
            return None

        for tx in transfers:
            from_addr = tx.get("from", "").lower()
            if from_addr in _KNOWN_FUNDING_SOURCES:
                entity_type, org = _KNOWN_FUNDING_SOURCES[from_addr]
                return WalletEntity(
                    address=address,
                    entity_type=entity_type,
                    entity_name=f"Funded by {org}",
                    organization=org,
                    resolution_source="funding_trace",
                    confidence=0.6,
                    notes=f"Early tokens from {from_addr[:10]}... ({org})",
                )

        return None
    except Exception as exc:
        logger.debug("Funding trace failed for %s: %s", address[:10], exc)
        return None


async def analyze_token_holders(
    ca: str, chain: str = "base", max_resolutions: int = 10,
) -> list[dict]:
    """Analyze top token holders and resolve entities.

    Returns list of {address, entity_name, entity_type, confidence, pct_supply}.
    Rate-limited to max_resolutions resolution calls per token.
    """
    if not settings.entity_resolution_enabled or not settings.basescan_api_key:
        return []

    from alpha_bot.platform_intel.basescan import get_token_transfers

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            transfers = await get_token_transfers(ca, client, offset=50)

        if not transfers:
            return []

        # Count tokens per buyer address
        buyer_totals: dict[str, int] = {}
        for tx in transfers:
            buyer = tx["to"].lower()
            if buyer == "0x0000000000000000000000000000000000000000":
                continue
            try:
                val = int(tx.get("value", "0"))
            except (ValueError, TypeError):
                val = 0
            buyer_totals[buyer] = buyer_totals.get(buyer, 0) + val

        total_supply = sum(buyer_totals.values()) or 1

        # Sort by amount descending, take top entries
        sorted_buyers = sorted(buyer_totals.items(), key=lambda x: x[1], reverse=True)

        results = []
        resolved_count = 0

        for addr, amount in sorted_buyers[:20]:
            pct = (amount / total_supply) * 100

            entity = None
            if resolved_count < max_resolutions:
                # Check DB first (free), only count API calls
                async with async_session() as session:
                    r = await session.execute(
                        sa_select(WalletEntity).where(WalletEntity.address == addr)
                    )
                    entity = r.scalar_one_or_none()

                if not entity:
                    entity = await resolve_entity(addr)
                    resolved_count += 1

            results.append({
                "address": addr,
                "entity_name": entity.entity_name if entity else "",
                "entity_type": entity.entity_type if entity else "unknown",
                "confidence": entity.confidence if entity else 0.0,
                "pct_supply": round(pct, 2),
            })

        # Filter to only entries with resolved entities or significant holdings
        notable = [
            r for r in results
            if r["entity_name"] or r["pct_supply"] >= 5.0
        ]

        return notable
    except Exception as exc:
        logger.warning("analyze_token_holders failed for %s: %s", ca[:12], exc)
        return []


async def get_entity_by_address(address: str) -> WalletEntity | None:
    """Quick DB lookup for a resolved entity."""
    address = address.lower().strip()
    async with async_session() as session:
        result = await session.execute(
            sa_select(WalletEntity).where(WalletEntity.address == address)
        )
        return result.scalar_one_or_none()


async def tag_wallet_entity(
    address: str,
    entity_type: str,
    entity_name: str,
    organization: str | None = None,
    notes: str | None = None,
) -> WalletEntity:
    """Manually tag a wallet with entity info. Creates or updates."""
    address = address.lower().strip()
    now = datetime.utcnow()

    async with async_session() as session:
        result = await session.execute(
            sa_select(WalletEntity).where(WalletEntity.address == address)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.entity_type = entity_type
            existing.entity_name = entity_name
            existing.organization = organization
            existing.resolution_source = "manual"
            existing.confidence = 1.0
            existing.notes = notes
            existing.last_updated = now
            await session.commit()
            await session.refresh(existing)
            return existing

        entity = WalletEntity(
            address=address,
            entity_type=entity_type,
            entity_name=entity_name,
            organization=organization,
            resolution_source="manual",
            confidence=1.0,
            notes=notes,
            created_at=now,
            last_updated=now,
        )
        session.add(entity)
        await session.commit()
        await session.refresh(entity)
        return entity
