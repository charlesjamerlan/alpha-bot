# TRENCH EDGE v2 — Build Spec

## What This Is

An intelligence system for crypto memecoin trading on Base chain. It grades TG alpha callers, extracts winning patterns, then finds tokens autonomously. TG is the entry signal. X/CT is the exit signal. Everything in between is the edge.

**Core thesis:** Existing tools (Nansen, GMGN, Arkham) tell you WHAT smart money is buying. They don't tell you WHETHER the narrative has cultural escape velocity, HOW the token compares to its platform cohort, or WHEN to exit because CT is picking it up. We build those missing layers.

**Principle:** Build intelligence, not infrastructure. Use free/cheap existing tools for wallet tracking and safety checks. Only build what doesn't exist.

---

## Existing Assets

1. **Telethon App** — Already built. Monitors 5-10 alpha caller TG channels via Telethon, stores CAs + full messages + reactions/forwards metadata in **SQLite**. This is the most unique data source in the stack — no public tool has access to private alpha group signals.

2. **Hetzner VPS** — CX22 (2 vCPU, 4GB RAM). Already running. Use for all backend services.

3. **Free Tool Accounts** — GMGN.ai (wallet tracking), Nansen free tier (holder analysis), Arkham (entity intel), DEXScreener (market data), Cielo Finance (alerts).

---

## Tech Stack

- **Language:** Python 3.11+
- **Database:** SQLite (existing Telethon DB) + PostgreSQL on VPS for new data (platform datasets, scoring)
- **Async:** asyncio throughout
- **APIs:** DEXScreener (free, REST), BaseScan (free tier), Clanker events, Google Trends (pytrends), Farcaster Hub, Claude API (narrative matching)
- **Delivery:** python-telegram-bot for alert bot
- **Analysis:** pandas, scikit-learn (scoring/backtesting), NetworkX (wallet clustering)
- **Deployment:** Docker on Hetzner VPS, systemd services

---

## Project Structure

```
trench-edge/
├── CLAUDE.md                    # This file
├── docker-compose.yml
├── .env                         # API keys, TG bot token, DB paths
├── requirements.txt
│
├── src/
│   ├── __init__.py
│   │
│   ├── tg_intel/                # Phase 0: TG Intelligence Layer
│   │   ├── __init__.py
│   │   ├── channel_scorer.py    # Retroactive channel quality scoring
│   │   ├── convergence.py       # Cross-channel CA convergence detection
│   │   ├── reaction_velocity.py # Engagement velocity analysis
│   │   ├── first_mover.py       # First-caller tracking per channel
│   │   ├── pattern_extract.py   # Extract winning call profiles (bridge to Mode 2)
│   │   └── telethon_reader.py   # Read from existing SQLite DB
│   │
│   ├── narrative/               # Phase 1: Narrative Radar
│   │   ├── __init__.py
│   │   ├── trend_tracker.py     # Google Trends, TikTok, Farcaster, Reddit
│   │   ├── token_matcher.py     # Match token name/desc against trending themes
│   │   ├── depth_scorer.py      # Count narrative layers per token
│   │   └── claude_matcher.py    # Claude API semantic matching
│   │
│   ├── platform_intel/          # Phase 2: Platform Intelligence
│   │   ├── __init__.py
│   │   ├── clanker_scraper.py   # Scrape Clanker token deployments + outcomes
│   │   ├── virtuals_scorer.py   # Score Virtuals agent tokens
│   │   ├── flaunch_tracker.py   # Monitor Flaunch buyback mechanics
│   │   └── percentile_rank.py   # Rank token vs platform cohort
│   │
│   ├── scoring/                 # Phase 3: Composite Scoring
│   │   ├── __init__.py
│   │   ├── composite.py         # Weighted composite score calculator
│   │   ├── backtest.py          # Backtest scoring model against outcomes
│   │   └── recalibrate.py       # Weekly weight recalibration
│   │
│   ├── wallets/                 # Phase 4: Private Wallet Curation
│   │   ├── __init__.py
│   │   ├── reverse_engineer.py  # Find early wallets in winning tokens
│   │   ├── decay_monitor.py     # Detect crowded/copied wallets
│   │   └── cluster_map.py       # NetworkX wallet relationship graph
│   │
│   ├── alert_bot/               # Phase 5: Telegram Alert Bot
│   │   ├── __init__.py
│   │   ├── bot.py               # Main bot: tiered alerts + /scan command
│   │   ├── formatters.py        # Format alert messages
│   │   └── exit_monitor.py      # Watch for exit conditions
│   │
│   ├── x_monitor/               # Phase 6: X/CT Exit Signal Layer
│   │   ├── __init__.py
│   │   ├── alpha_window.py      # Measure TG→CT time gap
│   │   ├── buzz_threshold.py    # Detect cashtag velocity spikes
│   │   ├── meta_narrative.py    # Track CT sector narratives
│   │   └── kol_profiler.py      # Map KOL tweet → price impact
│   │
│   ├── data/                    # Shared data layer
│   │   ├── __init__.py
│   │   ├── db.py                # Database connections (SQLite reader + PostgreSQL writer)
│   │   ├── dexscreener.py       # DEXScreener API client
│   │   ├── basescan.py          # BaseScan API client
│   │   └── models.py            # Shared data models / schemas
│   │
│   └── config.py                # Central config, env vars, constants
│
├── scripts/
│   ├── backfill_channel_scores.py   # One-time: score all historical TG calls
│   ├── seed_clanker_dataset.py      # One-time: scrape historical Clanker tokens
│   └── migrate_sqlite_to_pg.py      # When ready: move TG data to PostgreSQL
│
└── tests/
    ├── test_channel_scorer.py
    ├── test_convergence.py
    ├── test_narrative_matcher.py
    └── test_composite_score.py
```

---

## Phase 0: TG Intelligence Layer (Week 1-2)

**Priority: HIGHEST — builds directly on existing Telethon app.**

Two modes:
- **Mode 1 (now):** Grade alpha callers, learn what patterns win, build scoring dataset
- **Mode 2 (goal):** Find tokens autonomously using extracted patterns. Callers become validation, not primary signal.

### 0.1 Channel Quality Scorer (`channel_scorer.py`)

Read every CA mention from existing SQLite DB. For each:
1. Query DEXScreener API for token price at mention timestamp
2. Query price at +1h, +6h, +24h after mention
3. Calculate: did it 2x? 3x? 5x? What was peak? What was the ROI if you bought at mention and sold at peak?
4. Aggregate per channel: hit rate (% of calls that 2x+), avg ROI, median time-to-peak, consistency

**Output:** `channel_scores` table:
```sql
CREATE TABLE channel_scores (
    channel_id TEXT,
    channel_name TEXT,
    total_calls INTEGER,
    hit_rate_2x REAL,        -- % of calls that reached 2x
    hit_rate_5x REAL,        -- % that reached 5x
    avg_roi REAL,            -- average ROI across all calls
    median_time_to_peak TEXT, -- how long to peak
    best_platform TEXT,       -- which platform their best calls are on (Clanker, Virtuals, etc)
    best_mcap_range TEXT,     -- which mcap range they're best at
    first_mover_score REAL,  -- how often they call it first vs other channels
    last_updated TIMESTAMP
);
```

Also store per-call outcomes:
```sql
CREATE TABLE call_outcomes (
    id INTEGER PRIMARY KEY,
    channel_id TEXT,
    ca TEXT,                  -- contract address
    chain TEXT,               -- 'base', 'solana', etc
    mention_timestamp TIMESTAMP,
    price_at_mention REAL,
    price_1h REAL,
    price_6h REAL,
    price_24h REAL,
    price_peak REAL,
    peak_timestamp TIMESTAMP,
    mcap_at_mention REAL,
    platform TEXT,            -- 'clanker', 'virtuals', 'flaunch', 'pump.fun', 'unknown'
    narrative_tags TEXT,      -- JSON array: ["ai", "meme", "gaming"]
    roi_if_sold_peak REAL,
    holder_count_at_mention INTEGER,
    message_text TEXT,
    reaction_count INTEGER,
    reaction_velocity REAL,   -- reactions per minute in first 30min
    forward_count INTEGER
);
```

### 0.2 Cross-Channel Convergence (`convergence.py`)

Real-time monitor (runs continuously alongside Telethon):
1. When a new CA is detected in any channel, check if the same CA appeared in any other monitored channel within the last 2 hours
2. If 2+ channels mention same CA → CONVERGENCE signal
3. Weight by channel quality scores
4. Fire alert to the alert bot

**Convergence score formula:**
```
confidence = sum(channel_quality_score for each channel that mentioned it) / max_possible_score
```

### 0.3 Reaction Velocity (`reaction_velocity.py`)

For each message containing a CA:
1. Track reaction count at +5min, +15min, +30min
2. Calculate reactions/minute over first 30 min
3. Compare to channel's baseline reaction rate (rolling 30-day average)
4. Output: velocity multiplier (1.0 = normal, 3.0+ = unusually excited)

### 0.4 Caller Pattern Extraction (`pattern_extract.py`)

**This is the bridge to autonomous discovery (Mode 2).**

From `call_outcomes` table, filter to calls with 2x+ ROI from channels with 50%+ hit rate. Analyze:
- What was the median mcap at time of call?
- What platform were the winning tokens on?
- What was the token age at time of call?
- What was the holder count at time of call?
- What was the holder velocity (growth rate) at time of call?
- What was the vol/mcap ratio?
- What narrative tags appeared most often?

Output: a **winning call profile** — the statistical fingerprint of what a good call looks like at the moment it's made. This profile feeds directly into the autonomous scanner (Phase 1 + 2 combined).

```python
# Example winning profile output
{
    "median_mcap_at_call": 250_000,
    "mcap_range": [50_000, 1_000_000],
    "top_platforms": ["clanker", "virtuals"],
    "median_age_hours": 72,
    "age_range_hours": [12, 168],
    "median_holders_at_call": 800,
    "min_holders": 200,
    "median_holder_velocity": 0.15,  # 15% growth per hour
    "min_vol_mcap_ratio": 0.3,
    "top_narratives": ["ai", "agent", "gaming"],
    "sample_size": 47,
    "confidence": "high"
}
```

---

## Phase 1: Narrative Radar (Week 3-4)

### 1.1 Trend Velocity Tracker (`trend_tracker.py`)

Scrape trending data every 30 minutes:
- **Google Trends:** pytrends library, track rising queries in categories adjacent to crypto (technology, memes, culture, politics, fitness)
- **Farcaster:** Hub API, trending casts and channels, especially Base-ecosystem channels
- **Reddit:** r/cryptocurrency, r/solana, r/base rising posts
- **GitHub:** trending repos (crypto-adjacent AI projects often precede token launches)

Store in `trending_themes` table:
```sql
CREATE TABLE trending_themes (
    id SERIAL PRIMARY KEY,
    source TEXT,              -- 'google', 'farcaster', 'reddit', 'github'
    theme TEXT,               -- 'looksmaxxing', 'ai agents', 'pokemon'
    velocity REAL,            -- rate of change (higher = accelerating faster)
    current_volume INTEGER,   -- absolute volume/mentions
    first_seen TIMESTAMP,
    last_updated TIMESTAMP
);
```

### 1.2 Token-Narrative Matcher (`token_matcher.py`)

When a new token appears (from DEXScreener new pairs feed, Clanker deployment events, or TG mention):
1. Extract token name, ticker, description/metadata
2. Compare against `trending_themes` table
3. For ambiguous matches, use Claude API for semantic matching (e.g., "$MAXXING" → "looksmaxxing" trend)
4. Output: Narrative Alignment Score (0-100)

### 1.3 Depth Scorer (`depth_scorer.py`)

Count independent narrative layers:
- Layer 1: Cultural trend (looksmaxxing, pokemon, etc.)
- Layer 2: Crypto meta (AI agents, DeFi, gaming)
- Layer 3: Platform/ecosystem (Virtuals, Clanker, OpenClaw)
- Layer 4: Timely event (election, product launch, celebrity mention)

More layers = higher score. Multiplier: 1x (1 layer) → 3x (4 layers).

---

## Phase 2: Platform Intelligence Engine (Week 5-6)

### 2.1 Clanker Outcome Dataset (`clanker_scraper.py`)

Batch scraper (runs daily):
1. Query Clanker deployment events on Base (via BaseScan contract events or Clanker API)
2. For each token: record deploy time, holder count at 1h/6h/24h/7d, peak mcap, current mcap, volume trajectory
3. Flag: survived 7 days? Reached $100K mcap? $500K? $1M?

```sql
CREATE TABLE platform_tokens (
    ca TEXT PRIMARY KEY,
    chain TEXT DEFAULT 'base',
    platform TEXT,            -- 'clanker', 'virtuals', 'flaunch'
    deploy_timestamp TIMESTAMP,
    holders_1h INTEGER,
    holders_6h INTEGER,
    holders_24h INTEGER,
    holders_7d INTEGER,
    peak_mcap REAL,
    peak_timestamp TIMESTAMP,
    current_mcap REAL,
    survived_7d BOOLEAN,
    volume_24h_at_peak REAL,
    vol_mcap_ratio_at_peak REAL,
    narrative_tags TEXT,       -- JSON
    last_updated TIMESTAMP
);
```

### 2.2 Virtuals Agent Scorer (`virtuals_scorer.py`)

For Virtuals Protocol agent tokens:
- Is the agent actually active? (Telegram interactions, Twitter posts)
- Holder growth trend (up, flat, declining)
- $VIRTUAL correlation (how much does it just follow $VIRTUAL?)
- Revenue generation (any buyback events?)

### 2.3 Platform Percentile Ranker (`percentile_rank.py`)

For any token, given its platform and age:
- What percentile is its holder count vs. all tokens at the same age on that platform?
- What percentile is its mcap?
- What percentile is its volume?

Example: "$LUMEN at 20 days with 3,575 holders → 89th percentile of surviving Clanker tokens at same age"

---

## Phase 3: Composite Scoring & Backtesting (Week 7-8)

### 3.1 Composite Score (`composite.py`)

Weighted formula combining all signals:

| Signal | Weight | Source |
|--------|--------|--------|
| TG Alpha Signal (quality-weighted mentions + convergence) | 25% | Phase 0 |
| Narrative Alignment Score | 20% | Phase 1 |
| Smart Wallet Signal | 15% | GMGN alerts (manual input or API) |
| Platform Percentile Rank | 15% | Phase 2 |
| Holder Velocity (vs platform cohort) | 10% | Phase 2 + DEXScreener |
| Volume/MCap Ratio + Liquidity Depth | 10% | DEXScreener |
| Safety Score | 5% | GMGN / RugCheck |

**Output:** Score 0-100. Mapped to alert tiers:
- 🔴 **TIER 1 (80+):** Act now. Multiple strong signals converging.
- 🟡 **TIER 2 (60-79):** Watchlist. Some signals present, missing confirmation.
- 🟢 **TIER 3 (40-59):** Radar. Interesting pattern, needs more data.
- ⬛ **Below 40:** Skip.

### 3.2 Backtesting Engine (`backtest.py`)

Using `call_outcomes` and `platform_tokens` data:
1. For every historical token, retroactively calculate what its composite score WOULD have been at various time points
2. Simulate: if you entered every token above score X at time Y, what's your PnL?
3. Find optimal threshold and timing
4. Calculate: hit rate per tier, average ROI per tier, median hold time for winners

### 3.3 Weight Recalibration (`recalibrate.py`)

Weekly cron job:
1. Look at last 7 days of outcomes
2. Which signal sources were most predictive this week?
3. Adjust weights (within bounds — no single signal can exceed 35% or drop below 5%)
4. Log changes for review

---

## Phase 4: Private Wallet Curation (Week 9-10, ongoing)

### 4.1 Winner Reverse-Engineering (`reverse_engineer.py`)

When a Base token does 10x+:
1. Pull first 50 wallets that bought (via BaseScan token transfer events)
2. Cross-reference against your existing tracked wallet list
3. Flag any NEW wallet that appears in 3+ winners

### 4.2 Wallet Decay Monitor (`decay_monitor.py`)

For each wallet in your private list:
- Estimate how many copiers it has (watch for txns within 5 seconds of the wallet's trades)
- When copier count rises significantly, downweight the wallet
- Alert: "Wallet 0xABC... alpha is decaying — 47 estimated copiers"

### 4.3 Cluster Mapper (`cluster_map.py`)

NetworkX graph:
- Nodes = wallets in your list
- Edges = bought the same token within 30 minutes
- Identify clusters (wallets that always move together)
- Signal from 3 independent clusters > signal from 3 wallets in same cluster

---

## Phase 5: Telegram Alert Bot (Week 11-12)

### 5.1 Bot Commands

```
/scan <CA>          → Instant full report: composite score, narrative, platform percentile, 
                      holder data, safety, known wallet presence
/watchlist           → Current TIER 2 tokens being monitored
/active              → Tokens you've entered (manually flagged)
/exit_check <CA>     → Check exit conditions for a token you hold
/channels            → Show channel quality rankings
/profile             → Show current "winning call profile" from Mode 2
/status              → System health: all scrapers running, DB sizes, last update times
```

### 5.2 Alert Format

```
🔴 TIER 1: ACT NOW

Token: $EXAMPLE
CA: 0x1234...5678
Chain: Base | Platform: Clanker
Score: 84/100

📡 Narrative: "AI agent" (velocity +210%) — 2 layers
💬 TG: Called by 3/7 channels (convergence 0.81)
   First call: Channel A (62% hit rate) 14 min ago
   Reaction velocity: 3.2x baseline
👛 Wallets: 1 smart wallet (GMGN) + 2 from private list
📊 Platform: 78th percentile Clanker token at 3 days old
📈 Holders: 1,247 (+22%/hr) | MCap: $340K | Vol/MCap: 0.61
🔒 Safety: 92/100 (LP locked, no mint, clean distribution)

⏱ Estimated alpha window: ~2-3 hours before CT pickup
```

### 5.3 Exit Alerts

```
⚠️ EXIT SIGNAL: $EXAMPLE

Reason: CT buzz threshold crossed
- Cashtag mentions: 3/hr → 52/hr in last 30 min
- 2 KOLs (50K+ followers) just tweeted
- Smart wallet from private list sold 40% position

Recommendation: Scale out. Alpha window closing.
```

---

## Phase 6: X/CT Exit Signal Layer (Week 13+)

**Only build after core system (Phases 0-5) is working and profitable.**

### 6.1 Alpha Window Clock (`alpha_window.py`)

After every entry signal (TG or autonomous):
1. Begin monitoring for the same CA on X (via SociaVault/TwitterAPI.io, $10-30/mo)
2. Record first KOL tweet timestamp, first cashtag volume spike, first DEXScreener trending appearance
3. Calculate alpha window duration: TG signal → CT awareness
4. Store per channel and per token type for future calibration

### 6.2 CT Buzz Threshold (`buzz_threshold.py`)

For tokens you're holding:
1. Poll cashtag mention volume every 5 minutes
2. When velocity crosses 10x baseline → fire EXIT warning
3. When 2+ KOLs with 20K+ followers tweet → fire EXIT signal

### 6.3 CT Meta-Narrative Monitor (`meta_narrative.py`)

Track sector-level CT narratives:
- Monitor 30-50 CT accounts for keywords: "Clanker meta", "Virtuals rotation", "Base AI season", "agent meta"
- When multiple accounts align on a sector narrative → alert to increase exposure to that sector

### 6.4 KOL Price Impact Profiler (`kol_profiler.py`)

For tracked KOL accounts:
- When they tweet a CA, record the token price at tweet time
- Track price at +15m, +1h, +4h
- Build profile: does this KOL's tweet = buy signal or sell signal?
- Some KOLs pump and their followers hold. Others pump and followers dump within hours.

---

## Composite Score Formula (Detailed)

```python
def calculate_composite_score(token_data: dict) -> float:
    """
    Returns score 0-100.
    Weights are initial defaults — recalibrated weekly by backtest engine.
    """
    weights = {
        'tg_signal': 0.25,
        'narrative': 0.20,
        'wallet_signal': 0.15,
        'platform_rank': 0.15,
        'holder_velocity': 0.10,
        'vol_mcap': 0.10,
        'safety': 0.05,
    }

    scores = {
        'tg_signal': compute_tg_score(
            convergence_count=token_data['tg_channels_mentioned'],
            channel_qualities=token_data['channel_quality_scores'],
            reaction_velocity=token_data['avg_reaction_velocity'],
        ),
        'narrative': token_data['narrative_alignment_score'],  # 0-100
        'wallet_signal': compute_wallet_score(
            gmgn_smart_wallets=token_data['smart_wallet_count'],
            private_list_wallets=token_data['private_wallet_count'],
            cluster_diversity=token_data['cluster_count'],
        ),
        'platform_rank': token_data['platform_percentile'] * 100,  # 0-100
        'holder_velocity': normalize_velocity(
            token_data['holder_growth_rate'],
            token_data['platform_baseline_growth'],
        ),
        'vol_mcap': min(token_data['vol_mcap_ratio'] * 100, 100),  # cap at 100
        'safety': token_data['safety_score'],  # 0-100
    }

    composite = sum(weights[k] * scores[k] for k in weights)
    return round(composite, 1)
```

---

## Database Schema Summary

**SQLite (existing — Telethon app):**
- Raw TG messages with metadata (reactions, forwards, timestamps, channel info)

**PostgreSQL (new — on VPS):**
- `channel_scores` — graded quality per TG channel
- `call_outcomes` — every CA mention with price outcomes
- `trending_themes` — current cultural trend velocities
- `platform_tokens` — historical Clanker/Virtuals/Flaunch token lifecycle data
- `composite_scores` — scored tokens with all signal components
- `private_wallets` — curated wallet list with decay scores
- `wallet_clusters` — NetworkX-derived cluster assignments
- `scoring_weights` — current model weights (updated weekly)
- `kol_profiles` — X KOL tweet-to-price impact data

---

## Environment Variables (.env)

```
# Database
TELETHON_SQLITE_PATH=/path/to/existing/telethon.db
POSTGRES_URL=postgresql://user:pass@localhost:5432/trench_edge

# APIs
DEXSCREENER_BASE_URL=https://api.dexscreener.com/latest
BASESCAN_API_KEY=your_key
CLAUDE_API_KEY=your_key

# Telegram Bot
TG_BOT_TOKEN=your_bot_token
TG_ALERT_CHAT_ID=your_private_chat_id

# X/CT (Phase 6 — add later)
# SOCIAVAULT_API_KEY=
# TWITTERAPI_IO_KEY=

# Config
BASE_CHAIN_ID=8453
CONVERGENCE_WINDOW_HOURS=2
MIN_CHANNEL_HIT_RATE=0.3
COMPOSITE_TIER1_THRESHOLD=80
COMPOSITE_TIER2_THRESHOLD=60
```

---

## Build Order

| Priority | Phase | What | Depends On | Est. Time |
|----------|-------|------|------------|-----------|
| 🔴 1 | Phase 0.1 | Channel Quality Scorer | Existing SQLite DB + DEXScreener API | 3-4 days |
| 🔴 2 | Phase 0.2 | Cross-Channel Convergence | Phase 0.1 | 2 days |
| 🔴 3 | Phase 0.3 | Reaction Velocity Analyzer | Existing SQLite DB | 1 day |
| 🔴 4 | Phase 0.4 | Caller Pattern Extraction | Phase 0.1 | 2 days |
| 🟡 5 | Phase 5 (partial) | Basic /scan bot command | DEXScreener API + BaseScan API | 2 days |
| 🟡 6 | Phase 1 | Narrative Radar | Google Trends + Claude API | 3-4 days |
| 🟡 7 | Phase 2.1 | Clanker Outcome Dataset | BaseScan API | 3-4 days |
| 🟡 8 | Phase 3.1 | Composite Score | Phases 0-2 | 2 days |
| 🟢 9 | Phase 3.2 | Backtesting Engine | Phase 3.1 + historical data | 3 days |
| 🟢 10 | Phase 4 | Private Wallet Curation | BaseScan API + GMGN data | 4-5 days |
| 🟢 11 | Phase 5 (full) | Complete Alert Bot | All previous phases | 3 days |
| 🟢 12 | Phase 3.3 | Weight Recalibration | Phase 3.2 running for 2+ weeks | 1 day |
| ⬜ 13 | Phase 6 | X/CT Exit Signals | Core system profitable | 4-5 days |
| ⬜ 14 | Phase 2.2-2.3 | Virtuals + Flaunch scorers | Phase 2.1 pattern established | 3-4 days |

---

## Monthly Cost

| Item | Cost |
|------|------|
| Hetzner VPS (existing) | ~$6 |
| GMGN.ai (free tier) | $0 |
| Nansen (free tier) | $0 |
| Arkham | $0 |
| DEXScreener | $0 |
| Claude API (narrative matching, ~1000 calls/day) | $10-30 |
| BaseScan API (free tier) | $0 |
| RPC calls (Helius free tier) | $0-15 |
| X data (Phase 6 only, SociaVault) | $0-30 |
| **Total** | **$16-81/month** |

---

## Key Design Decisions

1. **SQLite stays for Telethon.** Don't migrate the existing app. Read from SQLite, write new analysis data to PostgreSQL.

2. **Base chain first.** All platform intelligence targets Clanker, Virtuals, Flaunch on Base. Expand to Solana/Pump.fun later only if needed.

3. **Scores, not trades.** This system generates scored alerts. It does NOT auto-trade. Human in the loop for all buy/sell decisions.

4. **Mode 1 before Mode 2.** Grade TG callers first. Extract patterns. THEN build autonomous scanning. Don't skip the learning phase.

5. **X is exits, not entries.** Never use X for token discovery. TG + autonomous patterns handle entries. X tells you when to sell.

6. **Weekly recalibration.** Scoring weights are not static. The meta shifts constantly. Automate weight adjustment but log all changes for human review.

7. **Free tools for commoditized work.** Wallet tracking = GMGN. Holder analysis = Nansen. Entity intel = Arkham. Market data = DEXScreener. Only build what these tools can't do.
