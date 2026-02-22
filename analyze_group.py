"""CLI tool to analyze a Telegram group's ticker calls and P/L.

Usage:
    python analyze_group.py <group_username_or_id> [--days 90]

Examples:
    python analyze_group.py cryptoalpha
    python analyze_group.py cryptoalpha --days 30
    python analyze_group.py -1001234567890 --days 14
"""

import argparse
import asyncio
import sys

from alpha_bot.research.pnl_analyzer import PnLReport, analyze_pnl
from alpha_bot.research.telegram_group import (
    has_telethon_session,
    is_telethon_configured,
    scrape_group_history,
)
from alpha_bot.utils.logging import setup_logging


def _format_report(report: PnLReport) -> str:
    lines = [
        "",
        f"{'=' * 60}",
        f"  P/L REPORT: {report.group_name}",
        f"  Period: last {report.days_analyzed} days",
        f"{'=' * 60}",
        "",
        f"  Total ticker calls:      {report.total_calls}",
        f"  Unique tickers:          {report.unique_tickers}",
        f"  Resolved (price data):   {report.resolved_tickers}",
        f"  Overall win rate:        {report.overall_win_rate:.1f}%",
        f"  Overall avg P/L:         {report.overall_avg_pnl:+.1f}%",
    ]

    if report.best_call:
        lines.append(
            f"\n  Best call:  ${report.best_call.ticker} "
            f"({report.best_call.pnl_pct:+.1f}%) — {report.best_call.author} "
            f"on {report.best_call.posted_at:%Y-%m-%d}"
        )
    if report.worst_call:
        lines.append(
            f"  Worst call: ${report.worst_call.ticker} "
            f"({report.worst_call.pnl_pct:+.1f}%) — {report.worst_call.author} "
            f"on {report.worst_call.posted_at:%Y-%m-%d}"
        )

    if report.ticker_summaries:
        # Separate into tokens with P/L data vs DexScreener-only
        with_pnl = [s for s in report.ticker_summaries if s.avg_pnl_pct is not None]
        dex_only = [s for s in report.ticker_summaries if s.avg_pnl_pct is None]

        if with_pnl:
            lines.append(f"\n{'─' * 70}")
            lines.append("  TICKERS WITH P/L DATA (CoinGecko)")
            lines.append(f"  {'TICKER':<12} {'AVG P/L':>10} {'CALLS':>7} {'WIN%':>7} {'BEST':>10} {'WORST':>10}")
            lines.append(f"  {'─' * 65}")
            for s in with_pnl:
                avg = f"{s.avg_pnl_pct:+.1f}%"
                best = f"{s.best_pnl_pct:+.1f}%" if s.best_pnl_pct is not None else "N/A"
                worst = f"{s.worst_pnl_pct:+.1f}%" if s.worst_pnl_pct is not None else "N/A"
                lines.append(
                    f"  ${s.ticker:<11} {avg:>10} {s.call_count:>7} {s.win_rate:>6.0f}% {best:>10} {worst:>10}"
                )

        if dex_only:
            lines.append(f"\n{'─' * 70}")
            lines.append("  MEMECOIN CALLS (DexScreener)")
            lines.append(f"  {'TICKER':<12} {'STATUS':<8} {'PRICE':>12} {'MCAP':>12} {'LIQ':>10} {'CALLS':>6}")
            lines.append(f"  {'─' * 65}")
            for s in dex_only:
                status_icon = {"alive": "ALIVE", "dead": "DEAD", "low_liq": "LOW LQ"}.get(s.status, "???")
                price = f"${s.current_price:.8f}" if s.current_price and s.current_price < 0.01 else (
                    f"${s.current_price:.4f}" if s.current_price else "N/A"
                )
                mcap = _fmt_number(s.market_cap) if s.market_cap else "N/A"
                liq = _fmt_number(s.liquidity_usd) if s.liquidity_usd else "N/A"
                lines.append(
                    f"  ${s.ticker:<11} {status_icon:<8} {price:>12} {mcap:>12} {liq:>10} {s.call_count:>6}"
                )

    # Summary counts
    alive = sum(1 for s in report.ticker_summaries if s.status == "alive")
    dead = sum(1 for s in report.ticker_summaries if s.status == "dead")
    low_liq = sum(1 for s in report.ticker_summaries if s.status == "low_liq")
    if alive or dead or low_liq:
        lines.append(f"\n  Token status: {alive} alive, {low_liq} low liquidity, {dead} dead")

    if report.all_calls:
        lines.append(f"\n{'─' * 70}")
        lines.append("  RECENT CALLS (last 20)")
        lines.append(f"  {'─' * 65}")
        for c in report.all_calls[:20]:
            pnl = f"{c.pnl_pct:+.1f}%" if c.pnl_pct is not None else ""
            ca = f" [{c.contract_address[:8]}...]" if c.contract_address else ""
            lines.append(
                f"  {c.posted_at:%Y-%m-%d} | ${c.ticker:<10} | {pnl:>10} | {c.author}{ca}"
            )

    lines.append(f"\n{'=' * 70}")
    return "\n".join(lines)


def _fmt_number(n: float | None) -> str:
    """Format a number with K/M suffixes."""
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.0f}"


async def run(group: str, days: int) -> None:
    setup_logging()

    if not is_telethon_configured():
        print("ERROR: Telethon not configured.")
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in your .env file.")
        print("Then run: python setup_telethon.py")
        sys.exit(1)

    if not has_telethon_session():
        print("ERROR: No Telethon session found.")
        print("Run: python setup_telethon.py")
        sys.exit(1)

    print(f"Scraping {group} (last {days} days)...")
    calls = await scrape_group_history(group, days_back=days)

    if not calls:
        print(f"No ticker calls found in {group} over the last {days} days.")
        sys.exit(0)

    unique = len({c["ticker"] for c in calls})
    print(f"Found {len(calls)} ticker mentions ({unique} unique). Fetching prices...")

    report = await analyze_pnl(calls, group_name=group, days_back=days)
    print(_format_report(report))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze a Telegram group's ticker calls and P/L performance."
    )
    parser.add_argument(
        "group",
        help="Telegram group username (e.g. cryptoalpha) or numeric ID",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of days to look back (default: 90)",
    )
    args = parser.parse_args()

    # Handle numeric group IDs passed as strings
    group = args.group
    try:
        group = int(group)
    except ValueError:
        pass

    asyncio.run(run(group, args.days))


if __name__ == "__main__":
    main()
