"""Quick script to list calls from a specific caller."""
import asyncio
import logging
import sys

from alpha_bot.research.telegram_group import scrape_group_history

logging.disable(logging.INFO)


async def main():
    caller = sys.argv[1] if len(sys.argv) > 1 else "altcoinist_trenchbot"
    group = int(sys.argv[2]) if len(sys.argv) > 2 else 2469811342
    topic = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    days = int(sys.argv[4]) if len(sys.argv) > 4 else 5

    calls = await scrape_group_history(group, days_back=days, topic_id=topic)
    bot_calls = [c for c in calls if c["author"] == caller]

    # Dedupe by contract address
    seen = set()
    unique_calls = []
    for c in bot_calls:
        ca = c.get("contract_address") or c["ticker"]
        if ca not in seen:
            seen.add(ca)
            unique_calls.append(c)

    print(f"Calls by {caller}: {len(bot_calls)} total ({len(unique_calls)} unique tokens)\n")
    print(f"{'DATE':<18}{'TICKER':<15}{'CHAIN':<8}{'CA'}")
    print("-" * 95)
    for c in unique_calls:
        date = c["posted_at"].strftime("%Y-%m-%d %H:%M")
        ticker = "$" + c["ticker"]
        if len(ticker) > 14:
            ticker = ticker[:14]
        ca = c.get("contract_address", "") or "N/A"
        chain = c.get("chain", "solana")
        print(f"{date:<18}{ticker:<15}{chain:<8}{ca}")


asyncio.run(main())
