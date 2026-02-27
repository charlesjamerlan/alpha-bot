"""Solana RPC client for token transfer analysis.

Uses the public Solana RPC to fetch early token transfer events.
Free tier: ~10 req/sec on public endpoint — must be conservative.
"""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

# Conservative rate limiting for free public endpoint
_RATE_LIMIT_SLEEP = 0.5
_BATCH_DELAY = 1.5


async def _rpc_call(
    method: str, params: list, client: httpx.AsyncClient, max_retries: int = 3,
) -> dict | None:
    """Make a JSON-RPC call to the Solana RPC with retry."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(SOLANA_RPC_URL, json=payload)
            if resp.status_code in (429, 503):
                if attempt < max_retries:
                    wait = 3 * (2 ** attempt)  # 3s, 6s, 12s
                    logger.debug("Solana RPC %d — retry in %ds", resp.status_code, wait)
                    await asyncio.sleep(wait)
                    continue
                logger.warning("Solana RPC %d — exhausted retries for %s", resp.status_code, method)
                return None
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                logger.debug("Solana RPC error: %s", data["error"])
                return None
            return data
        except httpx.HTTPError as exc:
            if attempt < max_retries:
                await asyncio.sleep(3 * (2 ** attempt))
                continue
            logger.warning("Solana RPC request failed: %s", exc)
            return None
    return None


async def get_token_transfers_solana(
    mint: str,
    client: httpx.AsyncClient,
    limit: int = 200,
) -> list[dict] | None:
    """Get early token transfer events for a Solana SPL token.

    Uses getSignaturesForAddress to find early transactions involving the
    token mint, then parses each for token transfer instructions.

    Returns list of {from, to, value, timestamp, hash, blockNumber} or None.
    """
    await asyncio.sleep(_RATE_LIMIT_SLEEP)

    sigs_data = await _rpc_call(
        "getSignaturesForAddress",
        [mint, {"limit": min(limit, 1000)}],
        client,
    )

    if not sigs_data or not sigs_data.get("result"):
        return None

    signatures = sigs_data["result"]
    if not signatures:
        return None

    # Sort by slot ascending (earliest first)
    signatures.sort(key=lambda s: s.get("slot", 0))

    # Take the earliest N
    signatures = signatures[:limit]

    # Step 2: Parse each transaction sequentially to respect rate limits
    transfers = []
    parsed = 0

    for sig in signatures:
        if sig.get("err"):
            continue

        await asyncio.sleep(_RATE_LIMIT_SLEEP)
        result = await _parse_transaction(sig["signature"], client)
        if result:
            transfers.extend(result)
        parsed += 1

        # Progress log every 20 txs
        if parsed % 20 == 0:
            logger.info("Solana xray: parsed %d/%d txs, %d transfers so far",
                        parsed, len(signatures), len(transfers))

    return transfers if transfers else None


async def _parse_transaction(
    signature: str, client: httpx.AsyncClient
) -> list[dict]:
    """Parse a Solana transaction for SPL token transfers."""
    data = await _rpc_call(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        client,
    )

    if not data or not data.get("result"):
        return []

    tx = data["result"]
    block_time = tx.get("blockTime", 0)
    slot = tx.get("slot", 0)

    meta = tx.get("meta")
    if not meta:
        return []

    transfers = []

    # Check inner instructions and main instructions for token transfers
    all_instructions = []

    # Main instructions
    message = tx.get("transaction", {}).get("message", {})
    for ix in message.get("instructions", []):
        all_instructions.append(ix)

    # Inner instructions (from CPI calls — DEX swaps, etc.)
    for inner in meta.get("innerInstructions", []):
        for ix in inner.get("instructions", []):
            all_instructions.append(ix)

    for ix in all_instructions:
        parsed = ix.get("parsed")
        if not parsed:
            continue

        ix_type = parsed.get("type", "")
        info = parsed.get("info", {})

        # SPL token transfers
        if ix_type in ("transfer", "transferChecked"):
            source = info.get("source", "") or info.get("authority", "")
            dest = info.get("destination", "")
            amount = info.get("amount", "0")

            if ix_type == "transferChecked":
                token_amount = info.get("tokenAmount", {})
                amount = token_amount.get("amount", "0")

            if source and dest:
                transfers.append({
                    "from": source,
                    "to": dest,
                    "value": str(amount),
                    "timestamp": str(block_time),
                    "hash": signature,
                    "blockNumber": str(slot),
                    "contractAddress": "",
                    "tokenSymbol": "",
                })

    return transfers


async def get_address_transfers_solana(
    address: str,
    client: httpx.AsyncClient,
    limit: int = 50,
) -> list[dict] | None:
    """Get recent token transfers for a Solana wallet address."""
    await asyncio.sleep(_RATE_LIMIT_SLEEP)

    sigs_data = await _rpc_call(
        "getSignaturesForAddress",
        [address, {"limit": min(limit, 100)}],
        client,
    )

    if not sigs_data or not sigs_data.get("result"):
        return None

    signatures = sigs_data["result"]
    if not signatures:
        return None

    transfers = []

    for sig in signatures:
        if sig.get("err"):
            continue

        await asyncio.sleep(_RATE_LIMIT_SLEEP)
        result = await _parse_transaction(sig["signature"], client)
        if result:
            transfers.extend(result)

    return transfers if transfers else None
