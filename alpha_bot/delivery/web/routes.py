import json
import logging
import os

from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from alpha_bot.config import settings
from alpha_bot.delivery.web.dependencies import get_db
from alpha_bot.storage.repository import get_recent_signals

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="alpha_bot/delivery/web/templates")


# ---------------------------------------------------------------------------
# Jinja2 custom filter: "time ago" for datetime objects
# ---------------------------------------------------------------------------

def _timeago_filter(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    delta = datetime.utcnow() - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


templates.env.filters["timeago"] = _timeago_filter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_mcap(n: float | None) -> str:
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.0f}"


router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════
# Command Center — single-page dashboard
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/", response_class=HTMLResponse)
async def command_center(request: Request, db: AsyncSession = Depends(get_db)):
    now = datetime.utcnow()

    # ── 1. Conviction Alerts (last 24h) ──
    conviction_alerts: list = []
    try:
        from alpha_bot.conviction.models import ConvictionAlert

        result = await db.execute(
            sa_select(ConvictionAlert)
            .where(ConvictionAlert.alerted_at >= now - timedelta(hours=24))
            .order_by(ConvictionAlert.alerted_at.desc())
            .limit(10)
        )
        conviction_alerts = list(result.scalars().all())
        for a in conviction_alerts:
            try:
                a._sources_list = json.loads(a.sources_json) if a.sources_json else []
            except (json.JSONDecodeError, TypeError):
                a._sources_list = []
    except Exception as exc:
        logger.warning("Failed to load conviction alerts: %s", exc)

    # ── 2. Live Feed: TG calls ──
    tg_calls: list = []
    try:
        from alpha_bot.tg_intel.models import CallOutcome

        result = await db.execute(
            sa_select(CallOutcome)
            .order_by(CallOutcome.mention_timestamp.desc())
            .limit(20)
        )
        tg_calls = list(result.scalars().all())
    except Exception as exc:
        logger.warning("Failed to load TG calls: %s", exc)

    # ── 3. Live Feed: Scanner Tier 1-2 ──
    scanner_hits: list = []
    try:
        from alpha_bot.scanner.models import ScannerCandidate

        result = await db.execute(
            sa_select(ScannerCandidate)
            .where(ScannerCandidate.tier <= 2)
            .order_by(ScannerCandidate.discovered_at.desc())
            .limit(20)
        )
        scanner_hits = list(result.scalars().all())
    except Exception as exc:
        logger.warning("Failed to load scanner hits: %s", exc)

    # ── 4. Live Feed: Wallet buys ──
    wallet_buys: list = []
    try:
        from alpha_bot.wallets.models import WalletTransaction, PrivateWallet

        result = await db.execute(
            sa_select(WalletTransaction, PrivateWallet)
            .join(PrivateWallet, WalletTransaction.wallet_address == PrivateWallet.address)
            .order_by(WalletTransaction.timestamp.desc())
            .limit(10)
        )
        wallet_buys = list(result.all())  # list of Row(WalletTransaction, PrivateWallet)
    except Exception as exc:
        logger.warning("Failed to load wallet buys: %s", exc)

    # ── 5. Live Feed: Convergences (DB-backed) ──
    convergences: list = []
    try:
        from alpha_bot.tg_intel.convergence import get_recent_convergences_db
        convergences = await get_recent_convergences_db(db)
    except Exception as exc:
        logger.warning("Failed to load convergences: %s", exc)

    # ── 6. Live Feed: X/Twitter signals ──
    x_signals: list = []
    try:
        from alpha_bot.x_intel.models import XSignal

        result = await db.execute(
            sa_select(XSignal)
            .order_by(XSignal.tweeted_at.desc())
            .limit(20)
        )
        x_signals = list(result.scalars().all())
    except Exception as exc:
        logger.warning("Failed to load X signals: %s", exc)

    # ── Merge into unified feed ──
    feed: list[dict] = []

    for c in tg_calls:
        # Signal description: channel name + quality context
        sig = c.channel_name[:20] if c.channel_name else ""
        if c.roi_peak is not None and c.roi_peak > 0:
            sig += f" (peak {c.roi_peak:.0f}%)"

        # Price change: use roi_1h if available, else roi_24h
        change = c.roi_1h if c.roi_1h is not None else c.roi_24h

        # Recommendation: if from a channel call with good ROI history
        rec = "BUY" if c.mcap_at_mention and c.mcap_at_mention < 1_000_000 else None

        feed.append({
            "source_type": "TG",
            "ticker": c.ticker or "",
            "ca": c.ca,
            "chain": c.chain or "base",
            "platform": c.platform if c.platform != "unknown" else "",
            "score": None,
            "mcap_str": _fmt_mcap(c.mcap_at_mention),
            "change": change,
            "signal_str": sig,
            "rec": rec,
            "timestamp": c.mention_timestamp,
        })

    for s in scanner_hits:
        # Scanner has full composite score + tier
        tier_label = f"Tier {s.tier}"
        themes = ""
        try:
            themes_list = json.loads(s.matched_themes) if s.matched_themes else []
            if themes_list:
                themes = " | " + ", ".join(themes_list[:2])
        except (json.JSONDecodeError, TypeError):
            pass

        rec = "BUY" if s.tier == 1 and s.composite_score >= 75 else None

        feed.append({
            "source_type": "SCAN",
            "ticker": s.ticker or "",
            "ca": s.ca,
            "chain": s.chain or "base",
            "platform": s.platform if s.platform != "unknown" else "",
            "score": s.composite_score,
            "mcap_str": _fmt_mcap(s.mcap),
            "change": None,
            "signal_str": f"{tier_label}{themes}",
            "rec": rec,
            "timestamp": s.discovered_at,
        })

    for row in wallet_buys:
        tx = row[0]  # WalletTransaction
        wallet = row[1]  # PrivateWallet
        label = wallet.label[:16] if wallet.label else tx.wallet_address[:8] + "..."
        change = tx.peak_roi if tx.peak_roi else None

        feed.append({
            "source_type": "WALLET",
            "ticker": tx.token_symbol or "",
            "ca": tx.ca,
            "chain": tx.chain or "base",
            "platform": "",
            "score": wallet.quality_score if wallet else None,
            "mcap_str": "",
            "change": change,
            "signal_str": f"{label} (Q:{wallet.quality_score:.0f})" if wallet else label,
            "rec": "BUY" if wallet and wallet.quality_score >= 80 else None,
            "timestamp": tx.timestamp,
        })

    for cv in convergences:
        conf = cv.get("confidence", 0)
        channels = cv.get("channels", 0)

        feed.append({
            "source_type": "CONV",
            "ticker": cv.get("ticker", ""),
            "ca": cv.get("ca", ""),
            "chain": cv.get("chain", "base"),
            "platform": "",
            "score": conf * 100 if conf else None,
            "mcap_str": "",
            "change": None,
            "signal_str": f"{channels} channels, conf {conf:.2f}",
            "rec": "BUY" if conf >= 0.7 and channels >= 3 else None,
            "timestamp": cv.get("alerted_at"),
        })

    for xs in x_signals:
        # First cashtag stripped of $
        cashtags_list = []
        try:
            cashtags_list = json.loads(xs.cashtags) if xs.cashtags else []
        except (json.JSONDecodeError, TypeError):
            pass
        ticker = cashtags_list[0].lstrip("$") if cashtags_list else ""

        # First contract address
        cas_list = []
        try:
            cas_list = json.loads(xs.contract_addresses) if xs.contract_addresses else []
        except (json.JSONDecodeError, TypeError):
            pass
        ca = cas_list[0] if cas_list else ""

        feed.append({
            "source_type": "X",
            "ticker": ticker,
            "ca": ca,
            "chain": "base",
            "platform": "",
            "score": None,
            "mcap_str": "",
            "change": None,
            "signal_str": f"@{xs.author_username} \u2022 {xs.signal_type}",
            "rec": None,
            "timestamp": xs.tweeted_at,
        })

    # Sort by time, most recent first, take 30
    feed.sort(key=lambda x: x.get("timestamp") or datetime.min, reverse=True)
    feed = feed[:30]

    return templates.TemplateResponse(
        "command_center.html",
        {
            "request": request,
            "conviction_alerts": conviction_alerts,
            "feed": feed,
            "scan_result": None,
            "scan_error": None,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# Quick Scan — POST /scan
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/scan", response_class=HTMLResponse)
async def quick_scan(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    ca = str(form.get("ca", "")).strip()

    if not ca:
        return RedirectResponse("/", status_code=303)

    scan_result = None
    scan_error = None

    try:
        from alpha_bot.research.dexscreener import extract_pair_details, get_token_by_address
        from alpha_bot.tg_intel.platform_detect import detect_platform

        async with httpx.AsyncClient(timeout=15) as client:
            pair = await get_token_by_address(ca, client)

        if not pair:
            scan_error = f"No token found for {ca[:16]}..."
        else:
            d = extract_pair_details(pair)
            token = {
                "ca": ca,
                "chain": pair.get("chainId", "base"),
                "ticker": d["symbol"],
                "name": d["name"],
                "price_usd": d["price_usd"],
                "mcap": d["market_cap"],
                "liquidity_usd": d["liquidity_usd"],
                "volume_24h": d["volume_24h"],
                "pair_age_hours": None,
                "platform": detect_platform(ca, pair_data=pair),
                "discovery_source": "manual",
            }

            # Load themes
            from alpha_bot.scanner.models import TrendingTheme

            result = await db.execute(
                sa_select(TrendingTheme)
                .order_by(TrendingTheme.velocity.desc())
                .limit(100)
            )
            themes = list(result.scalars().all())

            # Scoring pipeline
            from alpha_bot.scanner.token_matcher import match_token_to_themes
            from alpha_bot.scanner.depth_scorer import compute_depth
            from alpha_bot.scanner.candidate_scorer import (
                compute_profile_match,
                compute_market_score,
                compute_composite,
            )

            matched_names, nar_score = await match_token_to_themes(
                d["name"], d["symbol"], themes,
            )
            depth = compute_depth(
                d["name"], d["symbol"], matched_names, themes,
                platform=token["platform"],
            )
            token["_matched_themes"] = matched_names
            prof_score = compute_profile_match(token, None)

            try:
                from alpha_bot.tg_intel.pattern_extract import extract_winning_profile
                profile = await extract_winning_profile()
                if profile:
                    prof_score = compute_profile_match(token, profile)
            except Exception:
                pass

            mkt_score = compute_market_score(token)

            plat_score = 0.0
            plat_str = "N/A"
            if token["platform"] in ("clanker", "virtuals", "flaunch"):
                try:
                    from alpha_bot.platform_intel.percentile_rank import (
                        compute_platform_percentile,
                    )
                    pct = await compute_platform_percentile(
                        ca, token["platform"], d.get("market_cap"),
                        None, d.get("volume_24h"), token.get("pair_age_hours"),
                    )
                    plat_score = pct.get("overall_percentile", 0.0)
                    plat_str = (
                        f"{plat_score:.0f}/100 "
                        f"({pct['age_bucket']}, {pct['cohort_size']} tokens)"
                    )
                except Exception:
                    pass

            composite, tier = compute_composite(
                nar_score, depth, prof_score, mkt_score, "manual",
                platform_score=plat_score,
            )

            themes_str = ", ".join(f'"{t}"' for t in matched_names[:3]) if matched_names else "none"

            # Token age
            age_str = "?"
            if d.get("pair_created_at"):
                try:
                    created_ms = int(d["pair_created_at"])
                    age_delta = datetime.utcnow() - datetime.utcfromtimestamp(created_ms / 1000)
                    age_hours = age_delta.total_seconds() / 3600
                    if age_hours < 1:
                        age_str = f"{int(age_hours * 60)}m"
                    elif age_hours < 24:
                        age_str = f"{age_hours:.0f}h"
                    else:
                        age_str = f"{age_hours / 24:.1f}d"
                except (ValueError, TypeError, OSError):
                    pass

            # Price formatting
            price_str = "N/A"
            if d.get("price_usd") is not None:
                p = d["price_usd"]
                if p >= 1:
                    price_str = f"${p:,.2f}"
                elif p >= 0.001:
                    price_str = f"${p:.4f}"
                elif p >= 0.0000001:
                    price_str = f"${p:.8f}"
                else:
                    price_str = f"${p:.12f}"

            # Liq/MCap ratio
            liq_mcap_ratio = None
            if d.get("liquidity_usd") and d.get("market_cap") and d["market_cap"] > 0:
                liq_mcap_ratio = d["liquidity_usd"] / d["market_cap"]

            # Buy/sell counts
            buys_24h = d.get("txns_24h_buys")
            sells_24h = d.get("txns_24h_sells")
            buys_1h = d.get("txns_1h_buys")
            sells_1h = d.get("txns_1h_sells")

            scan_result = {
                "ca": ca,
                "chain": token["chain"],
                "symbol": d["symbol"],
                "name": d["name"],
                "platform": token["platform"],
                "composite": composite,
                "tier": tier,
                "narrative_score": nar_score,
                "depth": depth,
                "profile_score": prof_score,
                "market_score": mkt_score,
                "platform_str": plat_str,
                "themes_str": themes_str,
                # Market data
                "price_str": price_str,
                "mcap_str": _fmt_mcap(d["market_cap"]),
                "liq_str": _fmt_mcap(d["liquidity_usd"]),
                "vol_str": _fmt_mcap(d["volume_24h"]),
                "liq_mcap_pct": f"{liq_mcap_ratio * 100:.1f}%" if liq_mcap_ratio else "N/A",
                # Price changes
                "change_5m": d.get("price_change_5m"),
                "change_1h": d.get("price_change_1h"),
                "change_6h": d.get("price_change_6h"),
                "change_24h": d.get("price_change_24h"),
                # Transaction counts
                "buys_24h": buys_24h,
                "sells_24h": sells_24h,
                "buys_1h": buys_1h,
                "sells_1h": sells_1h,
                "buy_sell_ratio": f"{buys_24h / sells_24h:.1f}" if buys_24h and sells_24h and sells_24h > 0 else "N/A",
                # Age
                "age_str": age_str,
                # DEX info
                "dex": d.get("dex", ""),
            }

    except Exception as exc:
        logger.exception("Quick scan failed for %s", ca[:12])
        scan_error = str(exc)

    # Re-fetch dashboard data
    now = datetime.utcnow()

    # Conviction alerts
    conviction_alerts: list = []
    try:
        from alpha_bot.conviction.models import ConvictionAlert
        result = await db.execute(
            sa_select(ConvictionAlert)
            .where(ConvictionAlert.alerted_at >= now - timedelta(hours=24))
            .order_by(ConvictionAlert.alerted_at.desc())
            .limit(10)
        )
        conviction_alerts = list(result.scalars().all())
        for a in conviction_alerts:
            try:
                a._sources_list = json.loads(a.sources_json) if a.sources_json else []
            except (json.JSONDecodeError, TypeError):
                a._sources_list = []
    except Exception:
        pass

    # Build lightweight feed for scan response
    feed: list[dict] = []
    try:
        from alpha_bot.tg_intel.models import CallOutcome
        result = await db.execute(
            sa_select(CallOutcome).order_by(CallOutcome.mention_timestamp.desc()).limit(10)
        )
        for c in result.scalars().all():
            feed.append({
                "source_type": "TG",
                "ticker": c.ticker or "",
                "ca": c.ca,
                "chain": c.chain or "base",
                "platform": c.platform if c.platform != "unknown" else "",
                "score": None,
                "mcap_str": _fmt_mcap(c.mcap_at_mention),
                "change": c.roi_1h if c.roi_1h is not None else c.roi_24h,
                "signal_str": c.channel_name[:20] if c.channel_name else "",
                "rec": None,
                "timestamp": c.mention_timestamp,
            })
    except Exception:
        pass

    try:
        from alpha_bot.scanner.models import ScannerCandidate
        result = await db.execute(
            sa_select(ScannerCandidate)
            .where(ScannerCandidate.tier <= 2)
            .order_by(ScannerCandidate.discovered_at.desc())
            .limit(10)
        )
        for s in result.scalars().all():
            feed.append({
                "source_type": "SCAN",
                "ticker": s.ticker or "",
                "ca": s.ca,
                "chain": s.chain or "base",
                "platform": s.platform if s.platform != "unknown" else "",
                "score": s.composite_score,
                "mcap_str": _fmt_mcap(s.mcap),
                "change": None,
                "signal_str": f"Tier {s.tier}",
                "rec": "BUY" if s.tier == 1 and s.composite_score >= 75 else None,
                "timestamp": s.discovered_at,
            })
    except Exception:
        pass

    feed.sort(key=lambda x: x.get("timestamp") or datetime.min, reverse=True)
    feed = feed[:30]

    return templates.TemplateResponse(
        "command_center.html",
        {
            "request": request,
            "conviction_alerts": conviction_alerts,
            "feed": feed,
            "scan_result": scan_result,
            "scan_error": scan_error,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# API endpoints (kept for external/future use)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/signals")
async def api_signals(db: AsyncSession = Depends(get_db), limit: int = 50):
    signals = await get_recent_signals(db, limit=limit)
    return [
        {
            "tweet_id": s.tweet_id,
            "author": s.author_username,
            "text": s.text,
            "score": s.score,
            "tickers": s.tickers,
            "sentiment": s.sentiment,
            "created_at": s.created_at.isoformat(),
            "signaled_at": s.signaled_at.isoformat(),
        }
        for s in signals
    ]


@router.get("/api/research")
async def api_research(ticker: str = Query(..., min_length=1, max_length=10)):
    from alpha_bot.research.pipeline import run_research
    report = await run_research(ticker)
    return report.to_dict()


@router.get("/api/reports")
async def api_reports(db: AsyncSession = Depends(get_db), limit: int = 20):
    from alpha_bot.storage.repository import get_recent_reports
    rows = await get_recent_reports(db, limit=limit)
    return [
        {
            "id": r.id,
            "ticker": r.ticker,
            "created_at": r.created_at.isoformat(),
            "report": json.loads(r.report_json),
        }
        for r in rows
    ]


@router.get("/api/forensics/gainers")
async def api_gainers(
    timeframe: str = "24h", min_gain: float = 20, limit: int = 30
):
    from alpha_bot.research.pump_scanner import scan_top_gainers
    gainers = await scan_top_gainers(
        timeframe=timeframe, min_gain_pct=min_gain, limit=limit
    )
    return [g.to_dict() for g in gainers]


@router.get("/api/forensics/analyze")
async def api_forensics_analyze(
    ticker: str = Query(..., min_length=1, max_length=10),
    coin_id: str | None = None,
):
    from alpha_bot.research.pump_forensics import analyze_pump
    report = await analyze_pump(ticker, coin_id=coin_id)
    return report.to_dict()


@router.get("/api/pnl")
async def api_pnl_analyses(db: AsyncSession = Depends(get_db), limit: int = 10):
    from alpha_bot.storage.repository import get_recent_pnl_analyses
    rows = await get_recent_pnl_analyses(db, limit=limit)
    return [
        {
            "id": r.id,
            "group_name": r.group_name,
            "days_analyzed": r.days_analyzed,
            "total_calls": r.total_calls,
            "win_rate": r.win_rate,
            "avg_pnl": r.avg_pnl,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/api/scanner/themes")
async def api_scanner_themes(db: AsyncSession = Depends(get_db)):
    from alpha_bot.scanner.models import TrendingTheme

    result = await db.execute(
        sa_select(TrendingTheme).order_by(TrendingTheme.velocity.desc()).limit(50)
    )
    themes = list(result.scalars().all())
    return [
        {
            "source": t.source,
            "theme": t.theme,
            "velocity": t.velocity,
            "volume": t.current_volume,
            "category": t.category,
            "first_seen": t.first_seen.isoformat() if t.first_seen else None,
            "last_updated": t.last_updated.isoformat() if t.last_updated else None,
        }
        for t in themes
    ]


@router.get("/api/scanner/candidates")
async def api_scanner_candidates(
    db: AsyncSession = Depends(get_db),
    tier: int | None = None,
    limit: int = 50,
):
    from alpha_bot.scanner.models import ScannerCandidate

    query = sa_select(ScannerCandidate).order_by(ScannerCandidate.composite_score.desc()).limit(limit)
    if tier is not None:
        query = query.where(ScannerCandidate.tier == tier)
    result = await db.execute(query)
    candidates = list(result.scalars().all())
    return [
        {
            "ca": c.ca,
            "chain": c.chain,
            "ticker": c.ticker,
            "name": c.name,
            "platform": c.platform,
            "composite_score": c.composite_score,
            "tier": c.tier,
            "narrative_score": c.narrative_score,
            "narrative_depth": c.narrative_depth,
            "profile_match_score": c.profile_match_score,
            "market_score": c.market_score,
            "platform_percentile": c.platform_percentile,
            "matched_themes": c.matched_themes,
            "mcap": c.mcap,
            "liquidity_usd": c.liquidity_usd,
            "volume_24h": c.volume_24h,
            "discovery_source": c.discovery_source,
            "discovered_at": c.discovered_at.isoformat() if c.discovered_at else None,
        }
        for c in candidates
    ]


@router.get("/api/platforms/tokens")
async def api_platform_tokens(
    db: AsyncSession = Depends(get_db),
    platform: str | None = None,
    limit: int = 100,
):
    from alpha_bot.platform_intel.models import PlatformToken

    query = (
        sa_select(PlatformToken)
        .order_by(PlatformToken.created_at.desc())
        .limit(limit)
    )
    if platform:
        query = query.where(PlatformToken.platform == platform)
    result = await db.execute(query)
    tokens = list(result.scalars().all())
    return [
        {
            "ca": t.ca,
            "chain": t.chain,
            "platform": t.platform,
            "name": t.name,
            "symbol": t.symbol,
            "deploy_timestamp": t.deploy_timestamp.isoformat() if t.deploy_timestamp else None,
            "holders_1h": t.holders_1h,
            "holders_6h": t.holders_6h,
            "holders_24h": t.holders_24h,
            "holders_7d": t.holders_7d,
            "peak_mcap": t.peak_mcap,
            "current_mcap": t.current_mcap,
            "survived_7d": t.survived_7d,
            "reached_100k": t.reached_100k,
            "reached_500k": t.reached_500k,
            "reached_1m": t.reached_1m,
            "check_status": t.check_status,
            "last_updated": t.last_updated.isoformat() if t.last_updated else None,
        }
        for t in tokens
    ]


@router.get("/api/platforms/stats")
async def api_platform_stats(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func
    from alpha_bot.platform_intel.models import PlatformToken

    stats = []
    for plat in ("clanker", "virtuals", "flaunch"):
        count_result = await db.execute(
            sa_select(func.count()).select_from(PlatformToken).where(PlatformToken.platform == plat)
        )
        total = count_result.scalar() or 0

        survived_result = await db.execute(
            sa_select(func.count()).select_from(PlatformToken).where(
                PlatformToken.platform == plat,
                PlatformToken.survived_7d == True,  # noqa: E712
            )
        )
        survived = survived_result.scalar() or 0

        eligible_result = await db.execute(
            sa_select(func.count()).select_from(PlatformToken).where(
                PlatformToken.platform == plat,
                PlatformToken.check_status == "complete",
            )
        )
        eligible = eligible_result.scalar() or 0

        avg_peak_result = await db.execute(
            sa_select(func.avg(PlatformToken.peak_mcap)).where(
                PlatformToken.platform == plat,
                PlatformToken.peak_mcap.isnot(None),
            )
        )
        avg_peak = avg_peak_result.scalar() or 0

        stats.append({
            "platform": plat,
            "total": total,
            "survived": survived,
            "survival_pct": (survived / eligible * 100) if eligible > 0 else 0,
            "avg_peak_mcap": avg_peak,
        })
    return stats


@router.get("/api/backtest/runs")
async def api_backtest_runs(db: AsyncSession = Depends(get_db), limit: int = 20):
    from alpha_bot.scoring_engine.models import BacktestRun

    result = await db.execute(
        sa_select(BacktestRun).order_by(BacktestRun.run_timestamp.desc()).limit(limit)
    )
    runs = list(result.scalars().all())
    return [
        {
            "id": r.id,
            "run_timestamp": r.run_timestamp.isoformat(),
            "lookback_days": r.lookback_days,
            "token_count": r.token_count,
            "tier1_count": r.tier1_count,
            "tier2_count": r.tier2_count,
            "tier1_hit_rate_2x": r.tier1_hit_rate_2x,
            "tier2_hit_rate_2x": r.tier2_hit_rate_2x,
            "tier1_avg_roi": r.tier1_avg_roi,
            "tier2_avg_roi": r.tier2_avg_roi,
            "optimal_tier1_threshold": r.optimal_tier1_threshold,
            "optimal_tier2_threshold": r.optimal_tier2_threshold,
        }
        for r in runs
    ]


@router.get("/api/weights")
async def api_weights(db: AsyncSession = Depends(get_db)):
    from alpha_bot.scoring_engine.models import ScoringWeights

    result = await db.execute(
        sa_select(ScoringWeights).order_by(ScoringWeights.version.desc()).limit(10)
    )
    weights = list(result.scalars().all())
    return [
        {
            "version": w.version,
            "active": w.active,
            "source": w.source,
            "w_narrative": w.w_narrative,
            "w_profile": w.w_profile,
            "w_platform": w.w_platform,
            "w_market": w.w_market,
            "w_depth": w.w_depth,
            "w_source": w.w_source,
            "created_at": w.created_at.isoformat(),
        }
        for w in weights
    ]


@router.get("/api/wallets")
async def api_wallets(db: AsyncSession = Depends(get_db), limit: int = 50):
    from alpha_bot.wallets.models import PrivateWallet

    result = await db.execute(
        sa_select(PrivateWallet)
        .order_by(PrivateWallet.quality_score.desc())
        .limit(limit)
    )
    wallets = list(result.scalars().all())
    return [
        {
            "address": w.address,
            "label": w.label,
            "source": w.source,
            "quality_score": w.quality_score,
            "estimated_copiers": w.estimated_copiers,
            "decay_score": w.decay_score,
            "cluster_id": w.cluster_id,
            "total_wins": w.total_wins,
            "total_tracked": w.total_tracked,
            "avg_entry_roi": w.avg_entry_roi,
            "status": w.status,
            "first_seen": w.first_seen.isoformat() if w.first_seen else None,
            "last_updated": w.last_updated.isoformat() if w.last_updated else None,
        }
        for w in wallets
    ]


@router.get("/api/clusters")
async def api_clusters(db: AsyncSession = Depends(get_db)):
    from alpha_bot.wallets.models import WalletCluster

    result = await db.execute(
        sa_select(WalletCluster)
        .order_by(WalletCluster.avg_quality_score.desc())
        .limit(20)
    )
    clusters = list(result.scalars().all())
    return [
        {
            "id": c.id,
            "cluster_label": c.cluster_label,
            "wallet_count": c.wallet_count,
            "avg_quality_score": c.avg_quality_score,
            "independence_score": c.independence_score,
            "last_updated": c.last_updated.isoformat() if c.last_updated else None,
        }
        for c in clusters
    ]


# ═══════════════════════════════════════════════════════════════════════════
# X/Twitter Signal Ingestion
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/api/x-signals")
async def ingest_x_signals(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    # Auth check
    if not settings.x_ingest_api_key or x_api_key != settings.x_ingest_api_key:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()

    # Accept single object or array
    items = body if isinstance(body, list) else [body]

    from alpha_bot.x_intel.models import XSignal

    ingested = 0
    skipped = 0

    for item in items:
        tweet_id = item.get("tweet_id")
        if not tweet_id:
            skipped += 1
            continue

        # Dedup check
        existing = await db.execute(
            sa_select(XSignal).where(XSignal.tweet_id == tweet_id).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            skipped += 1
            continue

        # Parse tweeted_at
        tweeted_at_raw = item.get("tweeted_at")
        if not tweeted_at_raw:
            skipped += 1
            continue
        try:
            tweeted_at = datetime.fromisoformat(tweeted_at_raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, AttributeError):
            skipped += 1
            continue

        signal = XSignal(
            tweet_id=tweet_id,
            author_username=item.get("author_username", ""),
            author_followers=item.get("author_followers"),
            text=item.get("text", ""),
            cashtags=json.dumps(item.get("cashtags", [])),
            contract_addresses=json.dumps(item.get("contract_addresses", [])),
            tweet_url=item.get("tweet_url"),
            signal_type=item.get("signal_type", "kol_mention"),
            tweeted_at=tweeted_at,
        )
        db.add(signal)
        ingested += 1

    await db.commit()

    return JSONResponse({"ingested": ingested, "skipped": skipped}, status_code=201)


# ═══════════════════════════════════════════════════════════════════════════
# Settings (unchanged)
# ═══════════════════════════════════════════════════════════════════════════

_EDITABLE_FIELDS = {
    "twitter_provider",
    "twitter_username",
    "twitter_email",
    "twitter_password",
    "twitter_bearer_token",
    "research_max_tweets",
    "smart_money_expand_count",
    "alpha_threshold",
    "anthropic_api_key",
    "telegram_bot_token",
    "telegram_chat_id",
    "telegram_api_id",
    "telegram_api_hash",
    "telegram_monitor_group",
}


def _update_env_file(updates: dict[str, str]) -> None:
    """Write updated values to .env, preserving comments and order."""
    env_path = ".env"
    lines: list[str] = []
    keys_written: set[str] = set()

    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0]
                    env_key = key.strip()
                    if env_key.upper() in {k.upper() for k in updates}:
                        for uk, uv in updates.items():
                            if uk.upper() == env_key.upper():
                                lines.append(f"{env_key}={uv}\n")
                                keys_written.add(uk.upper())
                                break
                        continue
                lines.append(line if line.endswith("\n") else line + "\n")

    for key, val in updates.items():
        if key.upper() not in keys_written:
            lines.append(f"{key.upper()}={val}\n")

    with open(env_path, "w") as f:
        f.writelines(lines)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(
        "settings.html", {"request": request, "s": settings, "saved": False}
    )


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    form = await request.form()
    env_updates: dict[str, str] = {}

    for field in _EDITABLE_FIELDS:
        val = form.get(field)
        if val is None:
            continue
        val = str(val).strip()

        if field in ("research_max_tweets", "smart_money_expand_count", "telegram_api_id"):
            try:
                setattr(settings, field, int(val))
                env_updates[field] = val
            except ValueError:
                pass
        elif field == "alpha_threshold":
            try:
                setattr(settings, field, float(val))
                env_updates[field] = val
            except ValueError:
                pass
        else:
            setattr(settings, field, val)
            env_updates[field] = val

    if env_updates.get("twitter_provider") == "twikit":
        cookies = settings.twikit_cookies_file
        if os.path.exists(cookies):
            os.remove(cookies)
            logger.info("Cleared twikit cookies for fresh login")

    _update_env_file(env_updates)
    logger.info("Settings updated: %s", list(env_updates.keys()))

    return templates.TemplateResponse(
        "settings.html", {"request": request, "s": settings, "saved": True}
    )
