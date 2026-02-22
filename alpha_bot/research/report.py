"""Structured research report model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from alpha_bot.research.risk import RiskReport
from alpha_bot.research.smart_money import SmartMoneyReport


@dataclass
class BuzzStats:
    total_tweets: int = 0
    bullish_count: int = 0
    bearish_count: int = 0
    neutral_count: int = 0
    total_engagement: int = 0
    top_tweets: list[dict] = field(default_factory=list)  # {text, author, likes, rts}


@dataclass
class ResearchReport:
    ticker: str = ""
    generated_at: datetime = field(default_factory=datetime.utcnow)

    # Section 1: Snapshot
    snapshot: dict | None = None  # from CoinGecko

    # Section 2: Direct Buzz
    buzz: BuzzStats = field(default_factory=BuzzStats)

    # Section 3: Smart Money Radar
    smart_money: SmartMoneyReport = field(default_factory=SmartMoneyReport)

    # Section 4: Narrative Map
    narratives: list[dict] = field(default_factory=list)
    co_mentioned_tickers: list[tuple[str, int]] = field(default_factory=list)

    # Section 5: Risk Signals
    risk: RiskReport = field(default_factory=RiskReport)

    # Section 6: LLM Summary (optional)
    llm_summary: str = ""

    def format_telegram(self) -> str:
        """Format report as Telegram HTML message."""
        lines: list[str] = []
        lines.append(f"📋 <b>Research Report: ${self.ticker}</b>\n")

        # Snapshot
        if self.snapshot:
            s = self.snapshot
            price = s.get("price_usd")
            chg24 = s.get("price_change_24h_pct")
            mcap = s.get("market_cap_usd")
            vol = s.get("volume_24h_usd")
            lines.append("<b>📊 Snapshot</b>")
            if price is not None:
                lines.append(f"  Price: ${price:,.4f}" if price < 1 else f"  Price: ${price:,.2f}")
            if chg24 is not None:
                emoji = "🟢" if chg24 >= 0 else "🔴"
                lines.append(f"  24h: {emoji} {chg24:+.2f}%")
            if mcap:
                lines.append(f"  MCap: ${mcap:,.0f}")
            if vol:
                lines.append(f"  24h Vol: ${vol:,.0f}")
            lines.append("")

        # Buzz
        b = self.buzz
        lines.append("<b>🐦 Direct Buzz</b>")
        lines.append(f"  Tweets found: {b.total_tweets}")
        lines.append(f"  Bullish/Bearish/Neutral: {b.bullish_count}/{b.bearish_count}/{b.neutral_count}")
        lines.append(f"  Total engagement: {b.total_engagement:,}")
        if b.top_tweets:
            lines.append("  Top tweets:")
            for t in b.top_tweets[:3]:
                lines.append(f"    • @{t['author']}: {t['text'][:80]}…")
        lines.append("")

        # Smart Money
        if self.smart_money.top_accounts:
            lines.append("<b>🧠 Smart Money Radar</b>")
            for acct in self.smart_money.top_accounts[:5]:
                flag = " 🆕" if acct.first_mention else ""
                lines.append(
                    f"  • @{acct.username} ({acct.followers:,} followers){flag}"
                )
            if self.smart_money.adjacent_tickers:
                adj = ", ".join(f"${t}({c})" for t, c in self.smart_money.adjacent_tickers[:8])
                lines.append(f"  Adjacent tickers: {adj}")
            lines.append("")

        # Narratives
        if self.narratives:
            lines.append("<b>📡 Narrative Map</b>")
            for n in self.narratives[:5]:
                lines.append(f"  • {n['narrative']}: {n['tweet_count']} tweets, {n['total_engagement']:,} engagement")
            lines.append("")

        # Co-mentioned tickers
        if self.co_mentioned_tickers:
            mentioned = ", ".join(f"${t}({c})" for t, c in self.co_mentioned_tickers[:10])
            lines.append(f"<b>🔗 Also Mentioned:</b> {mentioned}\n")

        # Risk
        r = self.risk
        risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(r.risk_level, "⚪")
        lines.append(f"<b>⚠️ Risk Level:</b> {risk_emoji} {r.risk_level.upper()}")
        for w in r.warnings:
            lines.append(f"  • {w}")
        lines.append("")

        # LLM Summary
        if self.llm_summary:
            lines.append(f"<b>🤖 AI Summary</b>\n{self.llm_summary}\n")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize for JSON API responses."""
        return {
            "ticker": self.ticker,
            "generated_at": self.generated_at.isoformat(),
            "snapshot": self.snapshot,
            "buzz": {
                "total_tweets": self.buzz.total_tweets,
                "bullish": self.buzz.bullish_count,
                "bearish": self.buzz.bearish_count,
                "neutral": self.buzz.neutral_count,
                "total_engagement": self.buzz.total_engagement,
                "top_tweets": self.buzz.top_tweets[:5],
            },
            "smart_money": {
                "top_accounts": [
                    {
                        "username": a.username,
                        "followers": a.followers,
                        "mentions": a.tweet_count_about_ticker,
                        "first_mention": a.first_mention,
                        "adjacent_tickers": a.adjacent_tickers,
                    }
                    for a in self.smart_money.top_accounts[:10]
                ],
                "adjacent_tickers": [
                    {"ticker": t, "count": c}
                    for t, c in self.smart_money.adjacent_tickers[:15]
                ],
            },
            "narratives": self.narratives,
            "co_mentioned_tickers": [
                {"ticker": t, "count": c} for t, c in self.co_mentioned_tickers
            ],
            "risk": {
                "level": self.risk.risk_level,
                "fud_ratio": self.risk.fud_ratio,
                "low_quality_ratio": self.risk.low_quality_ratio,
                "warnings": self.risk.warnings,
            },
            "llm_summary": self.llm_summary,
        }
