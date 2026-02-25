# Trench Edge v2 — Build Progress

> This file tracks every update to the Trench Edge build. Updated after each session.

---

## Architecture Decision: Build Inside Alpha-Bot

Trench Edge v2 is being built on top of the existing `alpha-bot` codebase rather than as a greenfield project. Rationale:
- ~70% of required infrastructure already exists (TG scraping, DexScreener, scoring framework, bot commands, async DB, multi-chain trading)
- Same tech stack (Python 3.11+, asyncio, SQLAlchemy async, python-telegram-bot, Telethon)
- Avoids duplicating 3,500+ LOC of battle-tested code
- New modules are added under `alpha_bot/` alongside existing ones

Database: Starting with SQLite (already working). PostgreSQL migration deferred until data volume requires it.

---

## Reusable Existing Components

| Trench Edge Need | Existing File | Notes |
|---|---|---|
| TG group scraping + CA extraction | `research/telegram_group.py` | Extracts CAs (Solana/EVM), tickers, chain detection, topic filtering |
| Real-time TG group monitor | `trading/listener.py` | Monitors multiple groups, parses CAs, builds TradeSignals |
| DexScreener API (price/mcap/liq/vol) | `research/dexscreener.py` | `get_token_by_address()`, `extract_pair_details()`, multi-chain |
| GeckoTerminal historical OHLCV | `research/dexscreener.py` | `gt_get_token_price_history()` — minute/hour/day resolution |
| CoinGecko price data | `research/coingecko.py` | For established tokens (BTC, ETH, SOL) |
| P/L analysis | `research/pnl_analyzer.py` | Entry/current price, win rate, per-ticker aggregation |
| Narrative classification | `research/narratives.py` | 9 themes: AI, DePIN, RWA, L2, Memecoins, DeFi, Gaming, BTC Eco, Regulation |
| Claude API integration | `research/summarizer.py` + `config.py` | Sonnet for semantic analysis |
| Composite scoring framework | `scoring/scorer.py` | Weighted multi-strategy pattern, extensible |
| Telegram bot | `delivery/telegram_bot.py` | Commands: /research, /token, /pnl, /positions, /buy, /sell, /trading |
| Async SQLAlchemy DB | `storage/database.py` + `models.py` | Tweets, Signals, Positions, Trades, Reports tables |
| Multi-chain trading (Maestro) | `trading/` | SOL, BASE, ETH, BSC via @MaestroSniperBot |

---

## Build Order & Status

| # | Phase | Component | Status | Date | Notes |
|---|---|---|---|---|---|
| 1 | 0.1 | Channel Quality Scorer | NOT STARTED | — | Needs: `call_outcomes` table, DexScreener historical lookups |
| 2 | 0.2 | Cross-Channel Convergence | NOT STARTED | — | Depends on 0.1 |
| 3 | 0.3 | Reaction Velocity | NOT STARTED | — | Needs Telethon reaction metadata |
| 4 | 0.4 | Pattern Extraction | NOT STARTED | — | Depends on 0.1 data |
| 5 | 5 (partial) | /scan bot command | NOT STARTED | — | Quick win once scoring exists |
| 6 | 1 | Narrative Radar | NOT STARTED | — | Google Trends + Farcaster + Reddit |
| 7 | 2.1 | Clanker Dataset | NOT STARTED | — | Needs BaseScan API |
| 8 | 3.1 | Composite Score (7 signals) | NOT STARTED | — | Depends on 0-2 |
| 9 | 3.2 | Backtesting Engine | NOT STARTED | — | Depends on 3.1 + historical data |
| 10 | 4 | Wallet Curation | NOT STARTED | — | BaseScan + NetworkX |
| 11 | 5 (full) | Complete Alert Bot | NOT STARTED | — | All previous phases |
| 12 | 3.3 | Weight Recalibration | NOT STARTED | — | Needs 2+ weeks of scoring data |
| 13 | 6 | X/CT Exit Signals | BLOCKED | — | Twikit blocked by Cloudflare, needs proxy or alt X data source |
| 14 | 2.2-2.3 | Virtuals + Flaunch | NOT STARTED | — | After Clanker pattern established |

---

## New Tables Needed

### `call_outcomes` (Phase 0.1)
Tracks every CA mention across monitored TG channels with price outcomes.
```sql
- channel_id, ca, chain, mention_timestamp
- price_at_mention, price_1h, price_6h, price_24h, price_peak, peak_timestamp
- mcap_at_mention, platform, narrative_tags (JSON)
- roi_if_sold_peak, holder_count_at_mention
- message_text, reaction_count, reaction_velocity, forward_count
```

### `channel_scores` (Phase 0.1)
Aggregated quality grades per TG channel.
```sql
- channel_id, channel_name, total_calls
- hit_rate_2x, hit_rate_5x, avg_roi
- median_time_to_peak, best_platform, best_mcap_range
- first_mover_score, last_updated
```

### `trending_themes` (Phase 1)
Cultural trend velocities from external sources.
```sql
- source (google/farcaster/reddit/github), theme, velocity
- current_volume, first_seen, last_updated
```

### `platform_tokens` (Phase 2)
Clanker/Virtuals/Flaunch token lifecycle data.
```sql
- ca, chain, platform, deploy_timestamp
- holders_1h/6h/24h/7d, peak_mcap, current_mcap
- survived_7d, volume_24h_at_peak, narrative_tags
```

---

## Changelog

### 2026-02-25 — Session 1: Audit & Planning
- Audited full alpha-bot codebase (57 Python files, ~3,500 LOC)
- Mapped existing components to Trench Edge phases
- Decision: build inside alpha-bot repo, SQLite first
- Created this progress tracking file
- **Also completed (pre-Trench Edge):**
  - Added Capsolver integration to twikit (config + client)
  - Added `/token <CA>` command (DexScreener lookup)
  - Deployed to VPS
  - Note: Twikit still blocked by Cloudflare WAF (Capsolver only solves FunCAPTCHA, not WAF blocks — needs residential proxy)
