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
