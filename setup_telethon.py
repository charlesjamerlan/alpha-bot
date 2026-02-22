"""One-time Telethon authentication setup.

Run this once to authenticate your Telegram account for group scraping.
After authentication, the session file is saved and reused automatically.

Usage:
    python setup_telethon.py
"""

import asyncio

from telethon import TelegramClient

from alpha_bot.config import settings


async def main() -> None:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH not set.")
        print()
        print("1. Go to https://my.telegram.org/apps")
        print("2. Create an app to get your API ID and API Hash")
        print("3. Add them to your .env file:")
        print("   TELEGRAM_API_ID=12345678")
        print("   TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890")
        return

    client = TelegramClient(
        "alpha_bot_telethon",
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.start()
    me = await client.get_me()
    print(f"Authenticated as: {me.first_name} (@{me.username})")
    print("Session saved to alpha_bot_telethon.session")
    print("You can now use the P/L analyzer in the web dashboard.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
