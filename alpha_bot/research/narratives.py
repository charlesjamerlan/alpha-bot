"""Map tweets to crypto narrative clusters and detect trending themes."""

import re
from collections import Counter

from alpha_bot.ingestion.models import RawTweet

# Narrative keyword sets — order matters (first match wins for a tweet)
NARRATIVES: dict[str, list[str]] = {
    "AI / AI Agents": [
        "ai agent", "artificial intelligence", "machine learning", "gpt",
        "llm", "ai crypto", "depin ai", "autonomous agent", "ai token",
        "neural", "inference",
    ],
    "DePIN": [
        "depin", "decentralized physical", "iot crypto", "real world infra",
        "helium", "hivemapper", "render network",
    ],
    "RWA (Real World Assets)": [
        "rwa", "real world asset", "tokenized", "treasury", "t-bill",
        "ondo", "centrifuge", "maple", "tokenization",
    ],
    "L2 / Scaling": [
        "layer 2", "l2", "rollup", "zk rollup", "optimistic rollup",
        "base chain", "arbitrum", "optimism", "zksync", "starknet",
        "scaling",
    ],
    "Memecoins": [
        "memecoin", "meme coin", "degen", "pepe", "bonk", "wif",
        "shib", "floki", "pump.fun", "fair launch", "100x",
        "moonshot", "ape",
    ],
    "DeFi": [
        "defi", "yield", "tvl", "liquidity", "amm", "lending",
        "borrowing", "staking", "restaking", "eigenlayer", "pendle",
        "aave", "uniswap", "dex",
    ],
    "Gaming / Metaverse": [
        "gamefi", "play to earn", "p2e", "metaverse", "nft game",
        "virtual world", "web3 gaming",
    ],
    "Bitcoin Ecosystem": [
        "ordinals", "brc-20", "brc20", "runes", "bitcoin l2",
        "stacks", "bitcoin defi", "inscription",
    ],
    "Regulation / Macro": [
        "sec", "etf", "regulation", "congress", "fed", "fomc",
        "rate cut", "cpi", "inflation", "macro", "institutional",
    ],
    "Politics / Culture War": [
        "trump", "biden", "vance", "musk", "elon", "vivek",
        "election", "maga", "liberal", "conservative", "senate",
        "tariff", "executive order", "political",
    ],
    "Pop Culture / Viral": [
        "tiktok", "viral", "trend", "celebrity", "movie",
        "netflix", "anime", "pokemon", "looksmaxxing", "rizz",
        "skibidi", "brainrot", "fanum", "griddy", "hawk tuah",
    ],
    "Animals / Mascots": [
        "dog", "cat", "frog", "pepe", "doge", "shiba",
        "penguin", "monkey", "bear", "bull", "panda",
    ],
}


def classify_narratives(tweets: list[RawTweet]) -> dict[str, list[RawTweet]]:
    """Assign each tweet to narratives it matches. A tweet can appear in multiple."""
    buckets: dict[str, list[RawTweet]] = {name: [] for name in NARRATIVES}

    for tweet in tweets:
        text = tweet.text.lower()
        for narrative, keywords in NARRATIVES.items():
            if any(kw in text for kw in keywords):
                buckets[narrative].append(tweet)

    # Drop empties
    return {k: v for k, v in buckets.items() if v}


def top_narratives(tweets: list[RawTweet], top_n: int = 5) -> list[dict]:
    """Return the top N narratives by tweet count with summary stats."""
    classified = classify_narratives(tweets)
    ranked = sorted(classified.items(), key=lambda x: len(x[1]), reverse=True)[:top_n]

    results = []
    for name, matching in ranked:
        total_engagement = sum(
            t.metrics.like_count + t.metrics.retweet_count for t in matching
        )
        results.append({
            "narrative": name,
            "tweet_count": len(matching),
            "total_engagement": total_engagement,
            "sample_tweets": [t.text[:120] for t in matching[:3]],
        })
    return results


def extract_co_mentioned_tickers(
    tweets: list[RawTweet], exclude_ticker: str
) -> list[tuple[str, int]]:
    """Find other tickers mentioned alongside the target ticker."""
    ticker_re = re.compile(r"\$([A-Z]{2,6})\b")
    exclude = exclude_ticker.upper().strip("$")
    counter: Counter[str] = Counter()

    for tweet in tweets:
        tickers = set(ticker_re.findall(tweet.text))
        tickers.discard(exclude)
        counter.update(tickers)

    return counter.most_common(15)
