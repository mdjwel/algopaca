"""Per-symbol earnings calendar and EPS surprise via Nasdaq public APIs."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_MONEY_RE = re.compile(r"[^0-9.\-]")

_CAL_TTL_SEC = 30 * 60
_SURPRISE_TTL_SEC = 60 * 60
_CAL_LOOKAHEAD_DAYS = 14
_BLACKOUT_HOURS_BEFORE = 12
_WAIT_FOR_PRINT_HOURS = 18

_CAL_CACHE: tuple[float, dict[str, dict[str, Any]]] | None = None
_SURPRISE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def fetch_earnings(symbol: str) -> dict[str, Any]:
    """Return next report, recent surprises, and an execution plan for `symbol`."""
    symbol = symbol.upper().strip()
    upcoming = _upcoming_for(symbol)
    history = _surprise_history(symbol)
    return build_earnings_snapshot(symbol, upcoming=upcoming, history=history)


def build_earnings_snapshot(
    symbol: str,
    *,
    upcoming: dict[str, Any] | None,
    history: list[dict[str, Any]] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure planner used by fetch_earnings and tests."""
    now_et = (now or datetime.now(timezone.utc)).astimezone(_ET)
    history = [row for row in (history or []) if isinstance(row, dict)]
    # `last` drives the blackout / react gating, so never trust the upstream row
    # order — a stale row first would block new longs off a months-old miss.
    history.sort(key=_sort_key_desc, reverse=True)
    last = history[0] if history else None
    next_report = dict(upcoming) if upcoming else None

    if next_report and last:
        next_day = _parse_iso_date(next_report.get("date"))
        last_day = _parse_iso_date(last.get("date"))
        if next_day and last_day and last_day >= next_day:
            next_report = None

    beats = sum(1 for row in history[:4] if row.get("result") == "beat")
    misses = sum(1 for row in history[:4] if row.get("result") == "miss")
    last_result = str((last or {}).get("result") or "")
    last_is_fresh = _fresh_print(last, now_et)

    blackout = False
    stance = "clear"
    equity_bias = "neutral"
    plan = "No nearby earnings print — trade on TA and news."

    next_when = _parse_when_utc((next_report or {}).get("when_utc")) if next_report else None
    if next_when is not None:
        hours = (next_when - now_et.astimezone(timezone.utc)).total_seconds() / 3600
        next_report["hours_until"] = round(hours, 2)
        if hours > 0:
            stance = "wait"
            if hours <= _BLACKOUT_HOURS_BEFORE:
                blackout = True
                plan = (
                    f"Earnings in {hours:.1f}h ({next_report.get('session')}). "
                    "Do not open a new long or short into the print; flattening is allowed."
                )
            else:
                plan = (
                    f"Next earnings {next_report.get('when_et')} "
                    f"(EPS forecast {next_report.get('eps_forecast')}, "
                    f"last year {next_report.get('last_year_eps')}). "
                    "Do not buy only because the forecast is above last year; "
                    "do not short only because it is below."
                )
        elif hours > -_WAIT_FOR_PRINT_HOURS and not last_is_fresh:
            stance = "wait"
            blackout = True
            plan = (
                "Scheduled print has passed but EPS actual is not in yet — "
                "do not open a new long or short until the surprise posts."
            )

    if last_is_fresh and not blackout:
        stance = "react"
        if last_result == "beat":
            equity_bias = "bullish"
            plan = (
                f"Just reported a beat (EPS {last.get('eps')} vs consensus "
                f"{last.get('consensus')}, surprise {last.get('surprise_pct')}%). "
                "Lean long if technicals are not strongly against you; cover shorts; "
                "do not fade a clean beat with a new short."
            )
        elif last_result == "miss":
            equity_bias = "bearish"
            plan = (
                f"Just reported a miss (EPS {last.get('eps')} vs consensus "
                f"{last.get('consensus')}, surprise {last.get('surprise_pct')}%). "
                "Do not open a new long; exit an existing long. A short is allowed "
                "if the tape confirms the miss."
            )
        else:
            plan = (
                f"Just reported in-line (EPS {last.get('eps')} vs consensus "
                f"{last.get('consensus')}). Prefer hold unless TA/news are decisive."
            )

    ok = bool(next_report or history)
    return {
        "ok": ok,
        "symbol": symbol.upper().strip(),
        "next": next_report,
        "last": last,
        "history": history[:4],
        "beats_last_4": beats,
        "misses_last_4": misses,
        "last_result": last_result or None,
        "stance": stance,
        "equity_bias": equity_bias,
        "blackout": blackout,
        "plan": plan,
        "source": "nasdaq" if ok else None,
    }


def reset_earnings_cache() -> None:
    global _CAL_CACHE
    _CAL_CACHE = None
    _SURPRISE_CACHE.clear()


def _upcoming_for(symbol: str) -> dict[str, Any] | None:
    calendar = _load_upcoming_calendar()
    return calendar.get(symbol.upper())


def _load_upcoming_calendar() -> dict[str, dict[str, Any]]:
    global _CAL_CACHE
    now = time.time()
    if _CAL_CACHE and _CAL_CACHE[0] > now:
        return _CAL_CACHE[1]

    by_symbol: dict[str, dict[str, Any]] = {}
    today = datetime.now(_ET).date()
    days = [
        today + timedelta(days=offset)
        for offset in range(_CAL_LOOKAHEAD_DAYS + 1)
        if (today + timedelta(days=offset)).weekday() < 5
    ]
    rows_by_day: dict[date, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_calendar_day, day): day for day in days}
        for fut in as_completed(futures):
            day = futures[fut]
            try:
                rows_by_day[day] = fut.result()
            except Exception as exc:
                logger.warning("earnings calendar fetch failed for %s: %s", day, exc)
                rows_by_day[day] = []
    for day in sorted(rows_by_day):
        for row in rows_by_day[day]:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper().strip()
            if not sym or sym in by_symbol:
                continue
            normalized = _normalize_upcoming(row, day)
            if normalized:
                by_symbol[sym] = normalized

    _CAL_CACHE = (now + _CAL_TTL_SEC, by_symbol)
    return by_symbol


def _fetch_calendar_day(day: date) -> list[dict[str, Any]]:
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}"
    payload = _nasdaq_get(
        url,
        referer="https://www.nasdaq.com/market-activity/earnings",
    )
    rows = ((payload.get("data") or {}).get("rows")) or []
    return rows if isinstance(rows, list) else []


def _surprise_history(symbol: str) -> list[dict[str, Any]]:
    now = time.time()
    cached = _SURPRISE_CACHE.get(symbol)
    if cached and cached[0] > now:
        return list(cached[1])
    try:
        payload = _nasdaq_get(
            f"https://api.nasdaq.com/api/company/{symbol}/earnings-surprise",
            referer=f"https://www.nasdaq.com/market-activity/stocks/{symbol.lower()}",
        )
    except Exception as exc:
        logger.warning("earnings surprise fetch failed for %s: %s", symbol, exc)
        return []
    rows = (
        ((payload.get("data") or {}).get("earningsSurpriseTable") or {}).get("rows")
        or []
    )
    history = [_normalize_surprise(row) for row in rows if isinstance(row, dict)]
    history = [row for row in history if row]
    _SURPRISE_CACHE[symbol] = (now + _SURPRISE_TTL_SEC, history)
    return list(history)


def _normalize_upcoming(row: dict[str, Any], day: date) -> dict[str, Any] | None:
    session = _session_from_time(row.get("time"))
    when = _scheduled_datetime(day, session)
    forecast = _parse_eps(row.get("epsForecast"))
    last_year = _parse_eps(row.get("lastYearEPS"))
    vs_last_year = _compare_eps(forecast, last_year)
    return {
        "date": day.isoformat(),
        "when_utc": when.astimezone(timezone.utc).isoformat(),
        "when_et": when.strftime("%a %b %d %H:%M ET") + f" ({session})",
        "session": session,
        "eps_forecast": forecast,
        "last_year_eps": last_year,
        "forecast_vs_last_year": vs_last_year,
        "fiscal_quarter": row.get("fiscalQuarterEnding") or "",
        "name": row.get("name") or "",
    }


def _normalize_surprise(row: dict[str, Any]) -> dict[str, Any] | None:
    reported = _parse_mdy(row.get("dateReported"))
    if reported is None:
        return None
    eps = _parse_eps(row.get("eps"))
    consensus = _parse_eps(row.get("consensusForecast"))
    surprise_pct = _parse_eps(row.get("percentageSurprise"))
    result = _surprise_result(eps, consensus, surprise_pct)
    return {
        "date": reported.isoformat(),
        "fiscal_quarter": row.get("fiscalQtrEnd") or "",
        "eps": eps,
        "consensus": consensus,
        "surprise_pct": surprise_pct,
        "result": result,
    }


def _surprise_result(
    eps: float | None,
    consensus: float | None,
    surprise_pct: float | None,
) -> str:
    if surprise_pct is not None:
        if surprise_pct > 0.5:
            return "beat"
        if surprise_pct < -0.5:
            return "miss"
        return "inline"
    if eps is None or consensus is None:
        return "unknown"
    delta = eps - consensus
    if abs(consensus) > 1e-9:
        pct = 100.0 * delta / abs(consensus)
        if pct > 0.5:
            return "beat"
        if pct < -0.5:
            return "miss"
        return "inline"
    if delta > 0:
        return "beat"
    if delta < 0:
        return "miss"
    return "inline"


def _sort_key_desc(row: dict[str, Any]) -> date:
    """Report date for ordering; undated rows sort last."""
    return _parse_iso_date(row.get("date")) or date.min


def _fresh_print(last: dict[str, Any] | None, now_et: datetime) -> bool:
    if not last:
        return False
    reported = _parse_iso_date(last.get("date"))
    if reported is None:
        return False
    return 0 <= (now_et.date() - reported).days <= 1


def _session_from_time(value: Any) -> str:
    text = str(value or "").lower()
    if "pre" in text:
        return "pre-market"
    if "after" in text:
        return "after-hours"
    return "unknown"


def _scheduled_datetime(day: date, session: str) -> datetime:
    hour, minute = (8, 0) if session == "pre-market" else (16, 0)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET)


def _parse_eps(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.upper() in {"N/A", "NA", "-", "--", "NONE"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = _MONEY_RE.sub("", text.strip("()"))
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def _compare_eps(forecast: float | None, last_year: float | None) -> str:
    if forecast is None or last_year is None:
        return "unknown"
    if abs(last_year) < 1e-9:
        return "higher" if forecast > 0 else "lower" if forecast < 0 else "inline"
    pct = (forecast - last_year) / abs(last_year)
    if pct > 0.01:
        return "higher"
    if pct < -0.01:
        return "lower"
    return "inline"


def _parse_mdy(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_iso_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return _parse_mdy(text)


def _parse_when_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _nasdaq_get(url: str, *, referer: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "application/json,text/plain,*/*",
            "Origin": "https://www.nasdaq.com",
            "Referer": referer,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Nasdaq request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Nasdaq returned a non-object")
    return payload
