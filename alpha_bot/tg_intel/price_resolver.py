"""Look up historical prices for CA mentions using GeckoTerminal + DexScreener."""

import logging
from datetime import datetime, timedelta

import httpx

from alpha_bot.research.dexscreener import (
    extract_pair_details,
    get_token_by_address,
    gt_get_token_price_history,
)

logger = logging.getLogger(__name__)


def _find_closest_price(
    prices: list[tuple[int, float]], target_ts: int, max_delta_s: int = 7200
) -> float | None:
    """Find the price closest to *target_ts* within *max_delta_s* seconds."""
    if not prices:
        return None
    best = None
    best_dist = float("inf")
    for ts, price in prices:
        dist = abs(ts - target_ts)
        if dist < best_dist:
            best_dist = dist
            best = price
    if best_dist > max_delta_s:
        return None
    return best


def _find_peak_in_window(
    prices: list[tuple[int, float]], start_ts: int, end_ts: int
) -> tuple[float | None, int | None]:
    """Find peak price and its timestamp within [start, end]."""
    peak_price = None
    peak_ts = None
    for ts, price in prices:
        if start_ts <= ts <= end_ts:
            if peak_price is None or price > peak_price:
                peak_price = price
                peak_ts = ts
    return peak_price, peak_ts


def _compute_roi(entry: float | None, exit_price: float | None) -> float | None:
    """ROI as percentage. 100.0 = doubled."""
    if not entry or not exit_price or entry <= 0:
        return None
    return ((exit_price - entry) / entry) * 100.0


async def resolve_call_prices(
    ca: str,
    chain: str,
    mention_ts: datetime,
    client: httpx.AsyncClient,
) -> dict:
    """Resolve historical prices at mention, +1h, +6h, +24h, and peak.

    Returns dict with keys: price_at_mention, price_1h, price_6h, price_24h,
    price_peak, peak_timestamp, mcap_at_mention, roi_1h, roi_6h, roi_24h,
    roi_peak, hit_2x, hit_5x.
    """
    result = {
        "price_at_mention": None,
        "price_1h": None,
        "price_6h": None,
        "price_24h": None,
        "price_peak": None,
        "peak_timestamp": None,
        "mcap_at_mention": None,
        "roi_1h": None,
        "roi_6h": None,
        "roi_24h": None,
        "roi_peak": None,
        "hit_2x": False,
        "hit_5x": False,
    }

    # Calculate how many days of history we need
    now = datetime.utcnow()
    days_since = (now - mention_ts).days + 2  # buffer
    days_since = max(days_since, 3)  # minimum 3 days for hourly resolution

    prices = await gt_get_token_price_history(ca, client, days=days_since, chain=chain)
    if not prices:
        logger.debug("No price history for %s on %s", ca[:12], chain)
        return result

    mention_unix = int(mention_ts.timestamp())
    ts_1h = mention_unix + 3600
    ts_6h = mention_unix + 21600
    ts_24h = mention_unix + 86400

    # Allow wider tolerance for hourly candle data
    max_delta = 3600  # 1 hour tolerance

    price_at_mention = _find_closest_price(prices, mention_unix, max_delta)
    price_1h = _find_closest_price(prices, ts_1h, max_delta)
    price_6h = _find_closest_price(prices, ts_6h, max_delta)
    price_24h = _find_closest_price(prices, ts_24h, max_delta)

    # Peak in 24h window after mention
    peak_price, peak_ts = _find_peak_in_window(prices, mention_unix, ts_24h)

    result["price_at_mention"] = price_at_mention
    result["price_1h"] = price_1h
    result["price_6h"] = price_6h
    result["price_24h"] = price_24h
    result["price_peak"] = peak_price
    if peak_ts:
        result["peak_timestamp"] = datetime.utcfromtimestamp(peak_ts)

    # Compute ROIs
    result["roi_1h"] = _compute_roi(price_at_mention, price_1h)
    result["roi_6h"] = _compute_roi(price_at_mention, price_6h)
    result["roi_24h"] = _compute_roi(price_at_mention, price_24h)
    result["roi_peak"] = _compute_roi(price_at_mention, peak_price)

    # Hit flags
    if result["roi_peak"] is not None:
        result["hit_2x"] = result["roi_peak"] >= 100.0  # 2x = 100% gain
        result["hit_5x"] = result["roi_peak"] >= 400.0  # 5x = 400% gain

    # Estimate mcap at mention from price ratio + current mcap
    if price_at_mention:
        pair = await get_token_by_address(ca, client)
        if pair:
            details = extract_pair_details(pair)
            current_price = details.get("price_usd")
            current_mcap = details.get("market_cap")
            if current_price and current_mcap and current_price > 0:
                ratio = price_at_mention / current_price
                result["mcap_at_mention"] = current_mcap * ratio

    return result


async def resolve_single_price(
    ca: str, client: httpx.AsyncClient
) -> float | None:
    """Fetch current price for a CA via DexScreener."""
    pair = await get_token_by_address(ca, client)
    if not pair:
        return None
    details = extract_pair_details(pair)
    return details.get("price_usd")
