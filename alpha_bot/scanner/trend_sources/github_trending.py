"""GitHub — trending repos with crypto/AI keywords via HTML scrape."""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

_CRYPTO_KEYWORDS = re.compile(
    r"(agent|token|blockchain|defi|crypto|web3|solana|ethereum|base chain|"
    r"smart contract|dapp|nft|memecoin|ai\b)",
    re.IGNORECASE,
)

# Regex to extract repo name and description from the trending page HTML
_REPO_RE = re.compile(
    r'<h2 class="h3 lh-condensed">\s*<a href="/([^"]+)"',
)
_DESC_RE = re.compile(
    r'<p class="col-9 color-fg-muted my-1 pr-4">\s*(.+?)\s*</p>',
    re.DOTALL,
)


async def fetch_github_trending() -> list[dict]:
    """Fetch trending GitHub repos and filter for crypto-relevant ones."""
    results: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                "https://github.com/trending",
                params={"since": "daily"},
                headers={"User-Agent": "alpha-bot/1.0"},
            )
            resp.raise_for_status()
            html = resp.text
    except (httpx.HTTPError, Exception) as exc:
        logger.warning("GitHub trending fetch failed: %s", exc)
        return []

    repos = _REPO_RE.findall(html)
    descriptions = _DESC_RE.findall(html)

    for i, repo_path in enumerate(repos):
        desc = descriptions[i].strip() if i < len(descriptions) else ""
        combined = f"{repo_path} {desc}"

        if not _CRYPTO_KEYWORDS.search(combined):
            continue

        theme = repo_path.split("/")[-1].lower().replace("-", " ")
        results.append({
            "source": "github",
            "theme": theme[:256],
            "volume": 0,
            "velocity": 0.0,
            "category": "trending_repo",
        })

    logger.info("GitHub: %d crypto-relevant trending repos", len(results))
    return results
