from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Twitter provider: "api" (official, paid) or "twikit" (scraper, free)
    twitter_provider: Literal["api", "twikit"] = "twikit"

    # Official X API (only needed if twitter_provider=api)
    twitter_bearer_token: str = ""

    # Twikit credentials (only needed if twitter_provider=twikit)
    twitter_username: str = ""
    twitter_email: str = ""
    twitter_password: str = ""
    twikit_cookies_file: str = "twikit_cookies.json"
    capsolver_api_key: str = ""  # capsolver.com API key (for Cloudflare bypass)

    twitter_search_query: str = (
        "(crypto OR bitcoin OR ethereum OR $BTC OR $ETH OR $SOL OR stocks OR macro) -is:retweet"
    )

    # Telegram Bot (push notifications)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Telegram Monitoring (Telethon — scrape groups for ticker calls)
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_monitor_group: str = ""  # group username or numeric ID

    # Scoring
    alpha_threshold: float = 0.6

    # Ingestion
    poll_interval_seconds: int = 60
    background_ingestion: bool = False  # disable by default to save API credits

    # Database
    database_url: str = "sqlite+aiosqlite:///alpha_bot.db"

    # Web
    web_host: str = "0.0.0.0"
    web_port: int = 8888

    # Research
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    smart_money_expand_count: int = 5  # top N accounts to expand
    smart_money_recent_tweets: int = 20  # recent tweets per account
    research_max_tweets: int = 25  # tweets per research search

    # LLM summarization (optional — leave blank to disable)
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-4-5-20250929"

    # --- Auto-Trading (Maestro bot integration) ---
    trading_enabled: bool = False  # Master kill switch
    maestro_bot_username: str = "MaestroSniperBot"  # Maestro bot's TG username
    trade_amount_sol: float = 0.1  # SOL per trade
    trade_amount_base_eth: float = 0.0001  # ETH per trade on BASE/ETH chains
    slippage_bps: int = 500  # 5%
    max_open_positions: int = 10
    min_liquidity_usd: float = 5000.0
    stop_loss_pct: float = -50.0  # -50% -> sell 100%
    take_profit_1_pct: float = 200.0  # 3x (200% gain)
    take_profit_1_sell_pct: float = 50.0  # sell 50% of bag
    take_profit_2_pct: float = 400.0  # 5x
    take_profit_2_sell_pct: float = 25.0
    take_profit_3_pct: float = 900.0  # 10x
    take_profit_3_sell_pct: float = 25.0
    price_poll_interval: int = 10  # seconds between price checks
    trade_cooldown_seconds: int = 30  # prevent re-buy within N seconds
    telegram_monitor_groups: str = ""  # comma-separated group usernames/IDs

    # Convergence detection (Phase 0.2)
    convergence_window_hours: int = 2
    convergence_min_channels: int = 2


settings = Settings()
