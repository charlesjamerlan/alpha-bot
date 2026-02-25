"""Detect token launch platform from CA, DexScreener pair data, and message text."""

import re


_PLATFORM_KEYWORDS = {
    "clanker": "clanker",
    "virtuals": "virtuals",
    "flaunch": "flaunch",
    "pump.fun": "pump.fun",
    "pumpfun": "pump.fun",
}

_URL_PATTERNS = {
    re.compile(r"clanker", re.IGNORECASE): "clanker",
    re.compile(r"virtuals", re.IGNORECASE): "virtuals",
    re.compile(r"flaunch", re.IGNORECASE): "flaunch",
    re.compile(r"pump\.fun|pumpfun", re.IGNORECASE): "pump.fun",
}


def detect_platform(
    ca: str,
    pair_data: dict | None = None,
    message_text: str = "",
) -> str:
    """Detect launch platform for a token.

    Detection order:
    1. CA ends with "pump" -> pump.fun
    2. DexScreener pair data (dexId, info.websites)
    3. Message text keywords
    4. Default: "unknown"
    """
    # 1. Solana pump.fun addresses end with "pump"
    if ca.lower().endswith("pump"):
        return "pump.fun"

    # 2. DexScreener pair data
    if pair_data:
        dex_id = (pair_data.get("dexId") or "").lower()
        for kw, platform in _PLATFORM_KEYWORDS.items():
            if kw in dex_id:
                return platform

        # Check info websites
        info = pair_data.get("info") or {}
        websites = info.get("websites") or []
        for site in websites:
            url = (site.get("url") or "").lower()
            for pattern, platform in _URL_PATTERNS.items():
                if pattern.search(url):
                    return platform

        # Check labels
        labels = pair_data.get("labels") or []
        for label in labels:
            label_lower = label.lower()
            for kw, platform in _PLATFORM_KEYWORDS.items():
                if kw in label_lower:
                    return platform

    # 3. Message text hints
    if message_text:
        lower = message_text.lower()
        for kw, platform in _PLATFORM_KEYWORDS.items():
            if kw in lower:
                return platform

    return "unknown"
