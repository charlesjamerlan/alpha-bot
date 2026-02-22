"""Optional LLM-powered summarization of research findings."""

import logging

from alpha_bot.config import settings

logger = logging.getLogger(__name__)


async def summarize_research(report_dict: dict) -> str:
    """Use Claude to produce a natural-language research brief.

    Returns empty string if Anthropic API key is not configured.
    """
    if not settings.anthropic_api_key:
        return ""

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — skipping LLM summary")
        return ""

    ticker = report_dict.get("ticker", "?")
    snapshot = report_dict.get("snapshot") or {}
    buzz = report_dict.get("buzz", {})
    smart_money = report_dict.get("smart_money", {})
    narratives = report_dict.get("narratives", [])
    risk = report_dict.get("risk", {})

    prompt = f"""You are a crypto research analyst. Based on the following data about ${ticker}, write a concise 3-5 paragraph research brief. Be direct, opinionated, and highlight what matters most for someone deciding whether to enter a position.

PRICE DATA:
{_fmt(snapshot)}

TWITTER BUZZ:
- {buzz.get('total_tweets', 0)} tweets found
- Sentiment split: {buzz.get('bullish', 0)} bullish / {buzz.get('bearish', 0)} bearish / {buzz.get('neutral', 0)} neutral
- Total engagement: {buzz.get('total_engagement', 0):,}

SMART MONEY:
Top accounts discussing it:
{_fmt_accounts(smart_money.get('top_accounts', []))}
Adjacent tickers these accounts also discuss: {_fmt_adjacent(smart_money.get('adjacent_tickers', []))}

NARRATIVES: {', '.join(n.get('narrative', '') for n in narratives[:5]) or 'None detected'}

RISK: Level={risk.get('level', 'unknown')}, Warnings: {'; '.join(risk.get('warnings', [])) or 'None'}

Write the brief now. No headers, just flowing paragraphs. End with a one-line conviction rating (Low/Medium/High) and key risk."""

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        message = await client.messages.create(
            model=settings.llm_model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as exc:
        logger.error("LLM summarization failed: %s", exc)
        return ""


def _fmt(d: dict) -> str:
    if not d:
        return "No data available"
    lines = []
    for k, v in d.items():
        if v is not None:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines) or "No data"


def _fmt_accounts(accounts: list[dict]) -> str:
    if not accounts:
        return "  None identified"
    lines = []
    for a in accounts[:8]:
        first = " (FIRST TIME)" if a.get("first_mention") else ""
        lines.append(f"  @{a['username']} ({a.get('followers', 0):,} followers){first}")
    return "\n".join(lines)


def _fmt_adjacent(tickers: list[dict]) -> str:
    if not tickers:
        return "None"
    return ", ".join(f"${t['ticker']}({t['count']})" for t in tickers[:10])
