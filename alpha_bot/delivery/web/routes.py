import json
import logging
import os
import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from alpha_bot.config import settings
from alpha_bot.delivery.web.dependencies import get_db
from alpha_bot.research.pipeline import run_research
from alpha_bot.research.pnl_analyzer import analyze_pnl
from alpha_bot.research.pump_forensics import analyze_pump
from alpha_bot.research.pump_scanner import scan_top_gainers
from alpha_bot.research.telegram_group import (
    extract_tickers,
    has_telethon_session,
    is_telethon_configured,
    scrape_group_history,
)
from alpha_bot.storage.models import ForensicsRow, PnLAnalysisRow
from alpha_bot.storage.repository import (
    get_recent_forensics,
    get_recent_pnl_analyses,
    get_recent_reports,
    get_recent_signals,
    save_forensics,
    save_pnl_analysis,
)

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="alpha_bot/delivery/web/templates")

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    signals = await get_recent_signals(db, limit=50)
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "signals": signals}
    )


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


@router.get("/research", response_class=HTMLResponse)
async def research_page(request: Request, db: AsyncSession = Depends(get_db)):
    past_reports = await get_recent_reports(db, limit=20)
    return templates.TemplateResponse(
        "research.html", {"request": request, "report": None, "past_reports": past_reports, "json": json}
    )


@router.get("/api/research")
async def api_research(ticker: str = Query(..., min_length=1, max_length=10)):
    report = await run_research(ticker)
    return report.to_dict()


@router.get("/api/reports")
async def api_reports(db: AsyncSession = Depends(get_db), limit: int = 20):
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


@router.post("/research", response_class=HTMLResponse)
async def research_submit(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    ticker = str(form.get("ticker", "")).strip().upper().strip("$")
    if not ticker:
        past_reports = await get_recent_reports(db, limit=20)
        return templates.TemplateResponse(
            "research.html",
            {"request": request, "report": None, "error": "Enter a ticker", "past_reports": past_reports, "json": json},
        )

    report = await run_research(ticker)
    past_reports = await get_recent_reports(db, limit=20)
    return templates.TemplateResponse(
        "research.html", {"request": request, "report": report, "past_reports": past_reports, "json": json}
    )


# --- Pump Forensics ---


@router.get("/forensics", response_class=HTMLResponse)
async def forensics_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    timeframe: str = "24h",
    min_gain: float = 20,
):
    gainers = []
    error = None
    try:
        gainers = await scan_top_gainers(
            timeframe=timeframe, min_gain_pct=min_gain, limit=30
        )
    except Exception as exc:
        logger.exception("Pump scanner failed")
        error = f"Failed to fetch top gainers: {exc}"

    past = await get_recent_forensics(db, limit=15)
    return templates.TemplateResponse(
        "forensics.html",
        {
            "request": request,
            "gainers": gainers,
            "report": None,
            "timeframe": timeframe,
            "min_gain": min_gain,
            "error": error,
            "past_forensics": past,
            "json": json,
        },
    )


@router.post("/forensics", response_class=HTMLResponse)
async def forensics_analyze(
    request: Request, db: AsyncSession = Depends(get_db)
):
    form = await request.form()
    ticker = str(form.get("ticker", "")).strip().upper().strip("$")
    coin_id = str(form.get("coin_id", "")).strip() or None
    timeframe = str(form.get("timeframe", "24h"))
    min_gain = float(form.get("min_gain", 20))

    if not ticker:
        return RedirectResponse("/forensics", status_code=303)

    report = None
    error = None
    try:
        report = await analyze_pump(ticker, coin_id=coin_id)

        # Persist
        row = ForensicsRow(
            ticker=ticker,
            coin_id=report.coin_id,
            pump_magnitude=report.pump_magnitude_pct,
            signal_score=report.signal_score,
            pre_pump_tweets=report.pre_pump_tweets,
            verdict=report.verdict,
            report_json=json.dumps(report.to_dict()),
        )
        await save_forensics(db, row)
    except Exception as exc:
        logger.exception("Forensics analysis failed for %s", ticker)
        error = f"Analysis failed: {exc}"

    # Re-fetch gainers for the page
    gainers = []
    try:
        gainers = await scan_top_gainers(
            timeframe=timeframe, min_gain_pct=min_gain, limit=30
        )
    except Exception:
        pass

    past = await get_recent_forensics(db, limit=15)
    return templates.TemplateResponse(
        "forensics.html",
        {
            "request": request,
            "gainers": gainers,
            "report": report,
            "ticker": ticker,
            "timeframe": timeframe,
            "min_gain": min_gain,
            "error": error,
            "past_forensics": past,
            "json": json,
        },
    )


@router.get("/api/forensics/gainers")
async def api_gainers(
    timeframe: str = "24h", min_gain: float = 20, limit: int = 30
):
    gainers = await scan_top_gainers(
        timeframe=timeframe, min_gain_pct=min_gain, limit=limit
    )
    return [g.to_dict() for g in gainers]


@router.get("/api/forensics/analyze")
async def api_forensics_analyze(
    ticker: str = Query(..., min_length=1, max_length=10),
    coin_id: str | None = None,
):
    report = await analyze_pump(ticker, coin_id=coin_id)
    return report.to_dict()


# --- P/L Analyzer ---


@router.get("/pnl", response_class=HTMLResponse)
async def pnl_page(request: Request, db: AsyncSession = Depends(get_db)):
    configured = is_telethon_configured()
    session_exists = has_telethon_session()
    past_analyses = await get_recent_pnl_analyses(db, limit=10)
    return templates.TemplateResponse(
        "pnl.html",
        {
            "request": request,
            "configured": configured,
            "session_exists": session_exists,
            "s": settings,
            "report": None,
            "past_analyses": past_analyses,
            "json": json,
        },
    )


@router.post("/pnl", response_class=HTMLResponse)
async def pnl_analyze(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    group = str(form.get("group", "")).strip()
    days = int(form.get("days", 90))

    if not group:
        past_analyses = await get_recent_pnl_analyses(db, limit=10)
        return templates.TemplateResponse(
            "pnl.html",
            {
                "request": request,
                "configured": True,
                "session_exists": True,
                "s": settings,
                "report": None,
                "error": "Enter a group username or ID",
                "group_name": group,
                "days": days,
                "past_analyses": past_analyses,
                "json": json,
            },
        )

    try:
        # Step 1: Scrape the group
        calls = await scrape_group_history(group, days_back=days)

        # Step 2: Analyze P/L
        report = await analyze_pnl(calls, group_name=group, days_back=days)

        # Step 3: Persist to DB
        row = PnLAnalysisRow(
            group_name=group,
            days_analyzed=days,
            total_calls=report.total_calls,
            unique_tickers=report.unique_tickers,
            resolved_tickers=report.resolved_tickers,
            win_rate=report.overall_win_rate,
            avg_pnl=report.overall_avg_pnl,
            report_json=json.dumps(report.to_dict()),
        )
        await save_pnl_analysis(db, row)
        logger.info(
            "P/L analysis complete: %d calls, %d tickers, %.1f%% win rate",
            report.total_calls,
            report.resolved_tickers,
            report.overall_win_rate,
        )
    except Exception as exc:
        logger.exception("P/L analysis failed")
        past_analyses = await get_recent_pnl_analyses(db, limit=10)
        return templates.TemplateResponse(
            "pnl.html",
            {
                "request": request,
                "configured": True,
                "session_exists": True,
                "s": settings,
                "report": None,
                "error": f"Analysis failed: {exc}",
                "group_name": group,
                "days": days,
                "past_analyses": past_analyses,
                "json": json,
            },
        )

    past_analyses = await get_recent_pnl_analyses(db, limit=10)
    return templates.TemplateResponse(
        "pnl.html",
        {
            "request": request,
            "configured": True,
            "session_exists": True,
            "s": settings,
            "report": report,
            "group_name": group,
            "days": days,
            "past_analyses": past_analyses,
            "json": json,
        },
    )


@router.get("/api/pnl")
async def api_pnl_analyses(db: AsyncSession = Depends(get_db), limit: int = 10):
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


# --- Scanner (Phase 1) ---


@router.get("/scanner", response_class=HTMLResponse)
async def scanner_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    tier: str | None = None,
):
    from sqlalchemy import select as sa_select
    from alpha_bot.scanner.models import TrendingTheme, ScannerCandidate

    # Themes
    themes_result = await db.execute(
        sa_select(TrendingTheme).order_by(TrendingTheme.velocity.desc()).limit(30)
    )
    themes = list(themes_result.scalars().all())

    # Candidates
    query = sa_select(ScannerCandidate).order_by(ScannerCandidate.composite_score.desc()).limit(50)
    if tier:
        try:
            query = query.where(ScannerCandidate.tier == int(tier))
        except ValueError:
            pass
    cands_result = await db.execute(query)
    candidates = list(cands_result.scalars().all())

    return templates.TemplateResponse(
        "scanner.html",
        {
            "request": request,
            "themes": themes,
            "candidates": candidates,
            "tier_filter": tier,
        },
    )


@router.get("/api/scanner/themes")
async def api_scanner_themes(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select as sa_select
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
    from sqlalchemy import select as sa_select
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


# --- Platform Intel (Phase 2) ---


def _fmt_mcap_web(n: float | None) -> str:
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.0f}"


def _fmt_age_web(hours: float | None) -> str:
    if hours is None:
        return "?"
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 24:
        return f"{hours:.0f}h"
    days = hours / 24
    if days < 7:
        return f"{days:.1f}d"
    return f"{days:.0f}d"


@router.get("/platforms", response_class=HTMLResponse)
async def platforms_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: str | None = None,
):
    from sqlalchemy import select as sa_select, func
    from alpha_bot.platform_intel.models import PlatformToken
    from datetime import datetime

    # Query tokens
    query = (
        sa_select(PlatformToken)
        .order_by(PlatformToken.created_at.desc())
        .limit(100)
    )
    if platform:
        query = query.where(PlatformToken.platform == platform)
    result = await db.execute(query)
    tokens = list(result.scalars().all())

    # Annotate tokens with display strings
    now = datetime.utcnow()
    for t in tokens:
        age_hours = None
        if t.deploy_timestamp:
            age_hours = (now - t.deploy_timestamp).total_seconds() / 3600
        t._age_str = _fmt_age_web(age_hours)
        t._holders_str = str(t.holders_7d or t.holders_24h or t.holders_6h or t.holders_1h or "—")
        t._peak_mcap_str = _fmt_mcap_web(t.peak_mcap)
        t._current_mcap_str = _fmt_mcap_web(t.current_mcap)

    # Aggregate stats per platform
    stats = []
    for plat in ("clanker", "virtuals", "flaunch"):
        count_result = await db.execute(
            sa_select(func.count()).select_from(PlatformToken).where(PlatformToken.platform == plat)
        )
        total = count_result.scalar() or 0
        if total == 0:
            continue

        survived_result = await db.execute(
            sa_select(func.count()).select_from(PlatformToken).where(
                PlatformToken.platform == plat,
                PlatformToken.survived_7d == True,  # noqa: E712
            )
        )
        survived = survived_result.scalar() or 0

        # Only count tokens old enough for 7d check
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
            "survival_pct": (survived / eligible * 100) if eligible > 0 else 0,
            "avg_peak_mcap": avg_peak,
        })

    return templates.TemplateResponse(
        "platforms.html",
        {
            "request": request,
            "tokens": tokens,
            "stats": stats,
            "platform_filter": platform,
        },
    )


@router.get("/api/platforms/tokens")
async def api_platform_tokens(
    db: AsyncSession = Depends(get_db),
    platform: str | None = None,
    limit: int = 100,
):
    from sqlalchemy import select as sa_select
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
    from sqlalchemy import select as sa_select, func
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


# --- Settings ---

# Fields we allow editing from the UI
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
                        # Find the matching update key (case-insensitive)
                        for uk, uv in updates.items():
                            if uk.upper() == env_key.upper():
                                lines.append(f"{env_key}={uv}\n")
                                keys_written.add(uk.upper())
                                break
                        continue
                lines.append(line if line.endswith("\n") else line + "\n")

    # Append any new keys not already in the file
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

        # Apply to runtime settings
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

    # If provider changed to twikit, invalidate any cached login
    if env_updates.get("twitter_provider") == "twikit":
        cookies = settings.twikit_cookies_file
        if os.path.exists(cookies):
            os.remove(cookies)
            logger.info("Cleared twikit cookies for fresh login")

    # Persist to .env
    _update_env_file(env_updates)
    logger.info("Settings updated: %s", list(env_updates.keys()))

    return templates.TemplateResponse(
        "settings.html", {"request": request, "s": settings, "saved": True}
    )
