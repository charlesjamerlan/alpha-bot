"""Cluster wallets by co-buying behavior using NetworkX."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select as sa_select

from alpha_bot.storage.database import async_session
from alpha_bot.wallets.models import PrivateWallet, WalletCluster, WalletTransaction

logger = logging.getLogger(__name__)

# Two wallets that bought the same token within this window are linked
_CO_BUY_WINDOW_MINUTES = 30


async def rebuild_clusters() -> int:
    """Build wallet clusters from co-buying patterns.

    Returns the number of clusters found.
    """
    try:
        import networkx as nx
    except ImportError:
        logger.warning("networkx not installed, cannot build clusters")
        return 0

    # Load active wallets and their transactions
    async with async_session() as session:
        w_result = await session.execute(
            sa_select(PrivateWallet).where(
                PrivateWallet.status.in_(["active", "decaying"])
            )
        )
        wallets = list(w_result.scalars().all())

        tx_result = await session.execute(
            sa_select(WalletTransaction).where(
                WalletTransaction.wallet_address.in_([w.address for w in wallets])
            )
        )
        transactions = list(tx_result.scalars().all())

    if len(wallets) < 2:
        logger.debug("Not enough wallets for clustering (%d)", len(wallets))
        return 0

    # Group transactions by CA
    tx_by_ca: dict[str, list[WalletTransaction]] = defaultdict(list)
    for tx in transactions:
        tx_by_ca[tx.ca].append(tx)

    # Build graph: edge between wallets that co-bought same token within window
    G = nx.Graph()
    wallet_addresses = {w.address for w in wallets}
    for addr in wallet_addresses:
        G.add_node(addr)

    edge_weights: dict[tuple[str, str], int] = defaultdict(int)
    window = timedelta(minutes=_CO_BUY_WINDOW_MINUTES)

    for ca, txs in tx_by_ca.items():
        # Sort by timestamp
        txs.sort(key=lambda t: t.timestamp)

        # For each pair, check if within window
        for i in range(len(txs)):
            for j in range(i + 1, len(txs)):
                a = txs[i].wallet_address.lower()
                b = txs[j].wallet_address.lower()
                if a == b:
                    continue
                if a not in wallet_addresses or b not in wallet_addresses:
                    continue
                if txs[j].timestamp - txs[i].timestamp <= window:
                    key = tuple(sorted([a, b]))
                    edge_weights[key] += 1

    for (a, b), weight in edge_weights.items():
        G.add_edge(a, b, weight=weight)

    # Find connected components
    components = list(nx.connected_components(G))

    # Filter out singletons
    clusters = [c for c in components if len(c) >= 2]

    if not clusters:
        logger.debug("No clusters found (all wallets independent)")
        return 0

    # Build quality scores lookup
    quality_map = {w.address.lower(): w.quality_score for w in wallets}

    # Persist clusters
    async with async_session() as session:
        # Delete old clusters
        old_result = await session.execute(sa_select(WalletCluster))
        for old in old_result.scalars().all():
            await session.delete(old)

        # Reset cluster_id on all wallets
        all_w_result = await session.execute(sa_select(PrivateWallet))
        for w in all_w_result.scalars().all():
            w.cluster_id = None

        await session.flush()

        for idx, component in enumerate(clusters):
            addrs = list(component)
            avg_q = sum(quality_map.get(a, 0) for a in addrs) / len(addrs)

            # Independence score: fewer edges = more independent
            subgraph = G.subgraph(component)
            max_possible_edges = len(addrs) * (len(addrs) - 1) / 2
            actual_edges = subgraph.number_of_edges()
            avg_weight = 0.0
            if actual_edges > 0:
                avg_weight = sum(
                    d.get("weight", 1) for _, _, d in subgraph.edges(data=True)
                ) / actual_edges
            independence = 100.0 - min(avg_weight * 10, 100.0)

            cluster = WalletCluster(
                cluster_label=f"Cluster-{idx + 1}",
                wallet_count=len(addrs),
                wallets_json=json.dumps(addrs),
                avg_quality_score=round(avg_q, 1),
                independence_score=round(max(independence, 0.0), 1),
                last_updated=datetime.utcnow(),
            )
            session.add(cluster)
            await session.flush()

            # Assign cluster_id to wallets
            for addr in addrs:
                w_result = await session.execute(
                    sa_select(PrivateWallet).where(
                        PrivateWallet.address == addr
                    )
                )
                w = w_result.scalar_one_or_none()
                if w:
                    w.cluster_id = cluster.id

        await session.commit()

    logger.info("Built %d wallet clusters from %d wallets", len(clusters), len(wallets))
    return len(clusters)
