"""Base chain API client — RPC (eth_getLogs) + Etherscan V2 fallback.

Primary: Base public RPC for Transfer events (free, no key needed).
Fallback: Etherscan V2 for holder counts and contract metadata.

The Etherscan V2 free tier no longer supports tokentx on Base,
so all transfer queries use eth_getLogs on the public RPC.
"""

import asyncio
import logging
from datetime import datetime

import httpx

from alpha_bot.config import settings

logger = logging.getLogger(__name__)

ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
BASE_CHAIN_ID = "8453"
BASE_RPC_URL = "https://mainnet.base.org"

# ERC-20 Transfer(address,address,uint256) event topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Self-imposed rate limit (seconds between calls)
_RATE_LIMIT_SLEEP = 0.25


async def _etherscan_get(
    params: dict,
    client: httpx.AsyncClient,
    max_retries: int = 3,
) -> dict | None:
    """Make an Etherscan V2 API GET with retry on 429."""
    params = {**params, "chainid": BASE_CHAIN_ID, "apikey": settings.basescan_api_key}

    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(ETHERSCAN_V2_BASE, params=params)
            if resp.status_code == 429:
                if attempt < max_retries:
                    wait = 2 * (2 ** attempt)
                    logger.debug("Etherscan 429 — retrying in %ds", wait)
                    await asyncio.sleep(wait)
                    continue
                logger.warning("Etherscan 429 — exhausted retries")
                return None
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "1" or data.get("message") == "OK":
                return data
            # Some endpoints return status=0 for "no data" — not an error
            if data.get("message") == "No data found":
                return None
            logger.debug("Etherscan non-OK response: %s", data.get("message"))
            return data
        except httpx.HTTPError as exc:
            if attempt < max_retries:
                await asyncio.sleep(2 * (2 ** attempt))
                continue
            logger.warning("Etherscan request failed: %s", exc)
            return None

    return None


async def _rpc_call(
    method: str, params: list, client: httpx.AsyncClient, max_retries: int = 3,
) -> dict | None:
    """Make a JSON-RPC call to the Base public RPC with retry on 503/429."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(BASE_RPC_URL, json=payload)
            if resp.status_code in (429, 503):
                if attempt < max_retries:
                    wait = 2 * (2 ** attempt)  # 2s, 4s, 8s
                    logger.debug("RPC %d — retry in %ds", resp.status_code, wait)
                    await asyncio.sleep(wait)
                    continue
                logger.warning("RPC %d — exhausted retries for %s", resp.status_code, method)
                return None
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                logger.debug("RPC error: %s", data["error"])
                return None
            return data
        except httpx.HTTPError as exc:
            if attempt < max_retries:
                await asyncio.sleep(2 * (2 ** attempt))
                continue
            logger.warning("RPC request failed: %s", exc)
            return None
    return None


async def _get_block_timestamp(block_hex: str, client: httpx.AsyncClient) -> str:
    """Get timestamp for a block number (hex). Returns unix timestamp string."""
    data = await _rpc_call("eth_getBlockByNumber", [block_hex, False], client)
    if data and data.get("result"):
        ts_hex = data["result"].get("timestamp", "0x0")
        return str(int(ts_hex, 16))
    return ""


def _parse_address_from_topic(topic: str) -> str:
    """Extract 20-byte address from 32-byte log topic."""
    if not topic or len(topic) < 66:
        return ""
    return "0x" + topic[-40:]


async def _find_creation_block(
    ca: str, client: httpx.AsyncClient
) -> int | None:
    """Binary search for contract creation block using eth_getCode."""
    ca = ca.lower()

    # Get latest block
    data = await _rpc_call("eth_blockNumber", [], client)
    if not data or not data.get("result"):
        return None
    latest = int(data["result"], 16)

    # Check if contract exists at all
    data = await _rpc_call("eth_getCode", [ca, "latest"], client)
    if not data or data.get("result", "0x") in ("0x", "0x0", None):
        return None

    lo, hi = 0, latest
    while lo < hi:
        mid = (lo + hi) // 2
        data = await _rpc_call("eth_getCode", [ca, hex(mid)], client)
        if data is None:
            # RPC failure during search — bail
            return None
        code = data.get("result", "0x")
        if code and code != "0x" and len(code) > 2:
            hi = mid
        else:
            lo = mid + 1
        # Small delay to avoid rate-limiting during binary search
        await asyncio.sleep(0.05)

    return lo


# Cache creation blocks to avoid repeated binary searches
_creation_block_cache: dict[str, int] = {}


async def _get_logs_transfers(
    client: httpx.AsyncClient,
    contract_address: str | None = None,
    wallet_address: str | None = None,
    from_block: str = "0x0",
    to_block: str = "latest",
    max_results: int = 100,
) -> list[dict]:
    """Fetch ERC-20 Transfer events via eth_getLogs on Base RPC.

    Either contract_address (for token transfers) or wallet_address (for
    wallet activity) must be provided.

    Note: Base public RPC limits eth_getLogs to a 10,000 block range.
    For contract queries, we auto-discover the creation block and scan from there.

    Returns list of {from, to, value, timestamp, hash, blockNumber, contractAddress}.
    """
    await asyncio.sleep(_RATE_LIMIT_SLEEP)

    # For contract address queries, find creation block to set proper range
    if contract_address and from_block == "0x0":
        ca_lower = contract_address.lower()
        if ca_lower not in _creation_block_cache:
            creation = await _find_creation_block(ca_lower, client)
            if creation is None:
                logger.warning("Could not find creation block for %s", ca_lower[:12])
                return []
            _creation_block_cache[ca_lower] = creation
            logger.debug("Contract %s created at block %d", ca_lower[:12], creation)

        from_block = hex(_creation_block_cache[ca_lower])

    # Determine numeric block range
    if to_block == "latest":
        data = await _rpc_call("eth_blockNumber", [], client)
        if not data or not data.get("result"):
            return []
        end_block = int(data["result"], 16)
    else:
        end_block = int(to_block, 16)

    start_block = int(from_block, 16)

    # Collect logs across multiple 10k-block chunks until we have enough
    all_logs: list[dict] = []
    chunk_size = 10_000
    current = start_block

    while current <= end_block and len(all_logs) < max_results:
        chunk_end = min(current + chunk_size - 1, end_block)

        filter_params: dict = {
            "fromBlock": hex(current),
            "toBlock": hex(chunk_end),
            "topics": [TRANSFER_TOPIC],
        }

        if contract_address:
            filter_params["address"] = contract_address.lower()

        if wallet_address:
            padded = "0x" + wallet_address.lower().replace("0x", "").zfill(64)
            filter_params["topics"] = [TRANSFER_TOPIC, None, padded]

        data = await _rpc_call("eth_getLogs", [filter_params], client)
        if data and isinstance(data.get("result"), list):
            all_logs.extend(data["result"])

        current = chunk_end + 1
        if current <= end_block and len(all_logs) < max_results:
            await asyncio.sleep(_RATE_LIMIT_SLEEP)

    # Truncate to max_results
    all_logs = all_logs[:max_results]

    if not all_logs:
        return []

    # Batch-fetch block timestamps for unique blocks
    unique_blocks = list({log.get("blockNumber", "") for log in all_logs if log.get("blockNumber")})
    block_timestamps: dict[str, str] = {}

    for i in range(0, len(unique_blocks), 10):
        batch = unique_blocks[i:i + 10]
        tasks = [_get_block_timestamp(b, client) for b in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for block_hex, ts in zip(batch, results):
            if isinstance(ts, str):
                block_timestamps[block_hex] = ts
            else:
                block_timestamps[block_hex] = ""
        if i + 10 < len(unique_blocks):
            await asyncio.sleep(_RATE_LIMIT_SLEEP)

    transfers = []
    for log in all_logs:
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue

        from_addr = _parse_address_from_topic(topics[1])
        to_addr = _parse_address_from_topic(topics[2])
        block_num_hex = log.get("blockNumber", "0x0")
        block_num = str(int(block_num_hex, 16)) if block_num_hex else "0"

        raw_value = log.get("data", "0x0")
        try:
            value = str(int(raw_value, 16))
        except (ValueError, TypeError):
            value = "0"

        transfers.append({
            "from": from_addr,
            "to": to_addr,
            "value": value,
            "timestamp": block_timestamps.get(block_num_hex, ""),
            "hash": log.get("transactionHash", ""),
            "blockNumber": block_num,
            "contractAddress": log.get("address", ""),
            "tokenSymbol": "",  # Not available from logs
        })

    return transfers


async def get_holder_count(ca: str, client: httpx.AsyncClient) -> int | None:
    """Get the number of token holders for a contract on Base.

    Returns int holder count or None on failure.
    """
    data = await _etherscan_get(
        {
            "module": "token",
            "action": "tokenholdercount",
            "contractaddress": ca,
        },
        client,
    )
    if data is None:
        return None

    result = data.get("result")
    if result is not None:
        try:
            return int(result)
        except (ValueError, TypeError):
            pass
    return None


async def get_token_transfers(
    ca: str,
    client: httpx.AsyncClient,
    page: int = 1,
    offset: int = 50,
    sort: str = "asc",
) -> list[dict] | None:
    """Get token transfer events for a contract on Base via RPC eth_getLogs.

    Returns the first N transfers sorted by block number (ascending by default),
    useful for finding early buyers.

    Each entry: {from, to, value, timestamp, hash, blockNumber, tokenSymbol}
    """
    transfers = await _get_logs_transfers(
        client=client,
        contract_address=ca,
        max_results=offset,
    )

    if not transfers:
        return None

    if sort == "desc":
        transfers.sort(key=lambda t: int(t.get("blockNumber", "0")), reverse=True)

    return transfers


async def get_address_token_transfers(
    address: str,
    client: httpx.AsyncClient,
    start_block: int = 0,
    page: int = 1,
    offset: int = 50,
    sort: str = "desc",
) -> list[dict] | None:
    """Get ERC-20 token transfers for a wallet address on Base via RPC eth_getLogs.

    Queries by wallet address (transfers TO this wallet), useful for monitoring
    what tokens a specific wallet is buying.

    Returns list of {from, to, value, timestamp, hash, blockNumber, tokenSymbol,
    contractAddress} or None on failure.
    """
    from_block = hex(start_block) if start_block > 0 else "0x0"

    transfers = await _get_logs_transfers(
        client=client,
        wallet_address=address,
        from_block=from_block,
        max_results=offset,
    )

    if not transfers:
        return None

    reverse = sort == "desc"
    transfers.sort(key=lambda t: int(t.get("blockNumber", "0")), reverse=reverse)

    return transfers


async def get_contract_creation(
    ca: str, client: httpx.AsyncClient
) -> dict | None:
    """Get contract creation info (block, timestamp, creator).

    Returns {"block": str, "timestamp": str, "creator": str} or None.
    """
    data = await _etherscan_get(
        {
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": ca,
        },
        client,
    )
    if data is None:
        return None

    result = data.get("result")
    if isinstance(result, list) and result:
        entry = result[0]
        return {
            "block": entry.get("blockNumber", ""),
            "timestamp": entry.get("timestamp", ""),
            "creator": entry.get("contractCreator", ""),
        }
    return None
