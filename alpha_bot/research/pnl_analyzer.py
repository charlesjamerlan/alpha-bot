"""Analyze P/L performance of ticker calls using CoinGecko + DexScreener."""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from alpha_bot.config import settings
from alpha_bot.research.coingecko import _resolve_coingecko_id
from alpha_bot.research.dexscreener import (
    extract_pair_details,
    extract_price_from_pair,
    extract_token_name,
    get_token_by_address,
    get_token_by_ticker,
    gt_get_token_price_history,
)

logger = logging.getLogger(__name__)


@dataclass
class TickerCallResult:
    ticker: str
    posted_at: datetime
    message_text: str
    author: str
    contract_address: str | None = None
    entry_price: float | None = None
    current_price: float | None = None
    pnl_pct: float | None = None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "posted_at": self.posted_at.isoformat(),
            "message_text": self.message_text,
            "author": self.author,
            "contract_address": self.contract_address,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "pnl_pct": self.pnl_pct,
        }


@dataclass
class TickerSummary:
    ticker: str
    call_count: int = 0
    avg_entry_price: float | None = None
    current_price: float | None = None
    avg_pnl_pct: float | None = None
    best_pnl_pct: float | None = None
    worst_pnl_pct: float | None = None
    win_count: int = 0
    loss_count: int = 0
    first_call: datetime | None = None
    last_call: datetime | None = None
    source: str = ""  # "coingecko" or "dexscreener"
    market_cap: float | None = None
    liquidity_usd: float | None = None
    status: str = ""  # "alive", "dead", "low_liq"

    @property
    def win_rate(self) -> float:
        return self.win_count / self.call_count * 100 if self.call_count > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "call_count": self.call_count,
            "avg_entry_price": self.avg_entry_price,
            "current_price": self.current_price,
            "avg_pnl_pct": self.avg_pnl_pct,
            "best_pnl_pct": self.best_pnl_pct,
            "worst_pnl_pct": self.worst_pnl_pct,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": self.win_rate,
            "first_call": self.first_call.isoformat() if self.first_call else None,
            "last_call": self.last_call.isoformat() if self.last_call else None,
            "source": self.source,
            "market_cap": self.market_cap,
            "liquidity_usd": self.liquidity_usd,
            "status": self.status,
        }


@dataclass
class PnLReport:
    group_name: str
    days_analyzed: int
    total_calls: int = 0
    unique_tickers: int = 0
    resolved_tickers: int = 0
    overall_win_rate: float = 0.0
    overall_avg_pnl: float = 0.0
    best_call: TickerCallResult | None = None
    worst_call: TickerCallResult | None = None
    ticker_summaries: list[TickerSummary] = field(default_factory=list)
    all_calls: list[TickerCallResult] = field(default_factory=list)
    analyzed_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "group_name": self.group_name,
            "days_analyzed": self.days_analyzed,
            "total_calls": self.total_calls,
            "unique_tickers": self.unique_tickers,
            "resolved_tickers": self.resolved_tickers,
            "overall_win_rate": self.overall_win_rate,
            "overall_avg_pnl": self.overall_avg_pnl,
            "best_call": self.best_call.to_dict() if self.best_call else None,
            "worst_call": self.worst_call.to_dict() if self.worst_call else None,
            "ticker_summaries": [s.to_dict() for s in self.ticker_summaries],
            "all_calls": [c.to_dict() for c in self.all_calls],
            "analyzed_at": self.analyzed_at.isoformat(),
        }


async def _get_price_history(
    coin_id: str,
    from_ts: int,
    to_ts: int,
    client: httpx.AsyncClient,
) -> list[tuple[int, float]]:
    """Fetch price history from CoinGecko market_chart/range."""
    try:
        resp = await client.get(
            f"{settings.coingecko_base_url}/coins/{coin_id}/market_chart/range",
            params={"vs_currency": "usd", "from": from_ts, "to": to_ts},
        )
        resp.raise_for_status()
        prices = resp.json().get("prices", [])
        return [(int(p[0]), float(p[1])) for p in prices]
    except httpx.HTTPError as exc:
        logger.warning("CoinGecko history failed for %s: %s", coin_id, exc)
        return []


def _find_closest_price(
    prices: list[tuple[int, float]], target_ms: int
) -> float | None:
    """Find the price data point closest to a target timestamp (ms)."""
    if not prices:
        return None
    closest = min(prices, key=lambda p: abs(p[0] - target_ms))
    return closest[1]


def _group_calls_by_ticker(calls: list[dict]) -> dict[str, list[dict]]:
    """Group calls by ticker, merging entries that share a contract address."""
    # First pass: if a call has a contract_address, use that as the key
    # so that "2ACRyur..." and "$ELUN" pointing to the same CA get merged.
    ca_to_ticker: dict[str, str] = {}
    ticker_calls: dict[str, list[dict]] = defaultdict(list)

    for call in calls:
        ca = call.get("contract_address")
        ticker = call["ticker"]

        # If we've seen this CA before, use the established ticker name
        if ca and ca in ca_to_ticker:
            ticker = ca_to_ticker[ca]
        elif ca and not ticker.endswith("..."):
            # First time seeing this CA with a real ticker name
            ca_to_ticker[ca] = ticker
        elif ca:
            # CA-only call (ticker is "2ACRyur..."), check if we have a name
            # We'll revisit these after
            pass

        ticker_calls[ticker].append(call)

    # Second pass: rename CA-only groups if we learned the ticker name
    renamed: dict[str, list[dict]] = {}
    for ticker, tcalls in ticker_calls.items():
        if ticker.endswith("..."):
            ca = tcalls[0].get("contract_address")
            if ca and ca in ca_to_ticker:
                real_name = ca_to_ticker[ca]
                if real_name in renamed:
                    renamed[real_name].extend(tcalls)
                else:
                    renamed[real_name] = tcalls
                continue
        renamed[ticker] = tcalls

    return renamed


async def _resolve_dexscreener(
    ticker: str,
    tcalls: list[dict],
    client: httpx.AsyncClient,
) -> dict | None:
    """Try to resolve a ticker via DexScreener.

    Returns pair data or None.
    """
    # Try by contract address first
    for call in tcalls:
        ca = call.get("contract_address")
        if ca:
            pair = await get_token_by_address(ca, client)
            if pair:
                return pair
            break  # One attempt per CA is enough

    # Fallback: search by ticker name
    if not ticker.endswith("..."):
        pair = await get_token_by_ticker(ticker, client)
        if pair:
            return pair

    return None


async def analyze_pnl(
    calls: list[dict],
    group_name: str = "Unknown",
    days_back: int = 90,
) -> PnLReport:
    """
    Analyze P/L for a list of ticker calls.

    Uses CoinGecko for established tokens and DexScreener for memecoins/pump.fun tokens.
    """
    report = PnLReport(group_name=group_name, days_analyzed=days_back)

    if not calls:
        return report

    ticker_calls = _group_calls_by_ticker(calls)

    report.total_calls = len(calls)
    report.unique_tickers = len(ticker_calls)

    all_results: list[TickerCallResult] = []
    ticker_summaries: list[TickerSummary] = []

    async with httpx.AsyncClient(timeout=30) as client:
        now_ts = int(datetime.utcnow().timestamp())

        for ticker, tcalls in ticker_calls.items():
            source = ""

            # --- Try CoinGecko first (for established tokens) ---
            # Skip CoinGecko if any call has a contract address —
            # CA-based tokens are memecoins/pump.fun that CoinGecko often
            # resolves to the wrong token with the same name.
            coin_id = None
            prices = []
            current_price = None
            has_ca = any(c.get("contract_address") for c in tcalls)

            if not has_ca and not ticker.endswith("..."):
                coin_id = await _resolve_coingecko_id(ticker, client)

            if coin_id:
                earliest = min(c["posted_at"] for c in tcalls)
                from_ts = int(earliest.replace(tzinfo=timezone.utc).timestamp()) - 86400
                prices = await _get_price_history(coin_id, from_ts, now_ts, client)
                if prices:
                    current_price = prices[-1][1]
                    source = "coingecko"
                await asyncio.sleep(1.5)  # CoinGecko rate limit

            # --- Fallback: DexScreener + GeckoTerminal historical ---
            dex_details = None
            if not prices:
                pair = await _resolve_dexscreener(ticker, tcalls, client)
                if pair:
                    dex_details = extract_pair_details(pair)
                    current_price = extract_price_from_pair(pair)
                    # Update ticker name from DexScreener if we only had a CA
                    real_name = extract_token_name(pair)
                    if ticker.endswith("...") and real_name != "???":
                        ticker = real_name
                    source = "dexscreener"

                    # Try GeckoTerminal for historical OHLCV
                    ca = dex_details.get("address")
                    if not ca:
                        # Grab CA from the calls
                        for c in tcalls:
                            if c.get("contract_address"):
                                ca = c["contract_address"]
                                break
                    if ca:
                        # For very new tokens, request minute-level candles
                        gt_days = days_back
                        earliest = min(c["posted_at"] for c in tcalls)
                        age_hours = (datetime.utcnow() - earliest).total_seconds() / 3600
                        if age_hours < 48:
                            gt_days = 2  # triggers minute candles in gt_get_token_price_history

                        # Detect chain from call data or address format
                        call_chain = "solana"
                        for c in tcalls:
                            if c.get("chain"):
                                call_chain = c["chain"]
                                break
                        if ca.startswith("0x"):
                            call_chain = call_chain if call_chain != "solana" else "base"

                        gt_prices = await gt_get_token_price_history(
                            ca, client, days=gt_days, chain=call_chain,
                        )
                        if gt_prices:
                            # GeckoTerminal returns (timestamp_sec, price)
                            # Convert to (timestamp_ms, price) to match CoinGecko format
                            prices = [(ts * 1000, p) for ts, p in gt_prices]
                            current_price = prices[-1][1] if prices else current_price
                            logger.info(
                                "Got %d candles of history for %s via GeckoTerminal",
                                len(prices), ticker,
                            )
                        # GeckoTerminal free tier: 30 req/min
                        # pool lookup + ohlcv = 2 calls, so ~2.5s between tokens
                        await asyncio.sleep(2.5)

                    logger.info(
                        "Resolved %s via DexScreener (price: $%s, mcap: %s, liq: $%s, history: %s)",
                        ticker,
                        f"{current_price:.8f}" if current_price else "N/A",
                        f"${dex_details['market_cap']:,.0f}" if dex_details.get("market_cap") else "N/A",
                        f"{dex_details['liquidity_usd']:,.0f}" if dex_details.get("liquidity_usd") else "N/A",
                        f"{len(prices)} candles" if prices else "none",
                    )
                else:
                    logger.info("Could not resolve %s — skipping", ticker)
                    await asyncio.sleep(0.3)
                    continue

                await asyncio.sleep(0.3)  # DexScreener rate limit

            # --- Compute P/L for each call ---
            entry_prices: list[float] = []
            pnls: list[float] = []

            for call in tcalls:
                ca = call.get("contract_address")
                entry_price = None

                if prices:
                    # posted_at is naive UTC — force UTC to avoid local tz offset
                    posted_utc = call["posted_at"].replace(tzinfo=timezone.utc)
                    call_ts_ms = int(posted_utc.timestamp()) * 1000
                    entry_price = _find_closest_price(prices, call_ts_ms)

                result = TickerCallResult(
                    ticker=ticker,
                    posted_at=call["posted_at"],
                    message_text=call["message_text"],
                    author=call["author"],
                    contract_address=ca,
                    entry_price=entry_price,
                    current_price=current_price,
                )

                if entry_price and current_price and entry_price > 0:
                    result.pnl_pct = (
                        (current_price - entry_price) / entry_price
                    ) * 100
                    pnls.append(result.pnl_pct)
                    entry_prices.append(entry_price)

                all_results.append(result)

            # Build per-ticker summary
            summary = TickerSummary(
                ticker=ticker,
                call_count=len(tcalls),
                current_price=current_price,
                first_call=min(c["posted_at"] for c in tcalls),
                last_call=max(c["posted_at"] for c in tcalls),
                source=source,
            )

            # DexScreener metadata
            if dex_details:
                summary.market_cap = dex_details.get("market_cap")
                summary.liquidity_usd = dex_details.get("liquidity_usd")
                liq = summary.liquidity_usd or 0
                if not current_price or current_price == 0:
                    summary.status = "dead"
                elif liq < 500:
                    summary.status = "dead"
                elif liq < 5000:
                    summary.status = "low_liq"
                else:
                    summary.status = "alive"
            elif source == "coingecko":
                summary.status = "alive"

            if entry_prices:
                summary.avg_entry_price = sum(entry_prices) / len(entry_prices)
            if pnls:
                summary.avg_pnl_pct = sum(pnls) / len(pnls)
                summary.best_pnl_pct = max(pnls)
                summary.worst_pnl_pct = min(pnls)
                summary.win_count = sum(1 for p in pnls if p > 0)
                summary.loss_count = sum(1 for p in pnls if p <= 0)

            ticker_summaries.append(summary)
            report.resolved_tickers += 1

    # Sort
    report.all_calls = sorted(all_results, key=lambda r: r.posted_at, reverse=True)
    report.ticker_summaries = sorted(
        ticker_summaries, key=lambda s: s.avg_pnl_pct or 0, reverse=True
    )

    # Aggregate stats
    valid_calls = [r for r in all_results if r.pnl_pct is not None]
    if valid_calls:
        wins = sum(1 for r in valid_calls if r.pnl_pct > 0)
        report.overall_win_rate = wins / len(valid_calls) * 100
        report.overall_avg_pnl = sum(r.pnl_pct for r in valid_calls) / len(
            valid_calls
        )
        report.best_call = max(valid_calls, key=lambda r: r.pnl_pct)
        report.worst_call = min(valid_calls, key=lambda r: r.pnl_pct)

    return report
