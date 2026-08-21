"""Economic calendar via Forex Factory weekly JSON mirror."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_UA = "AlgoPaca/1.0 (+local educational bot)"
_ET = ZoneInfo("America/New_York")

# Keep high-impact / USD-focused events that move equities.
_IMPACT_RANK = {"High": 3, "Medium": 2, "Low": 1, "Holiday": 0}


def fetch_economic_calendar(
    hours_ahead: int = 72,
    hours_behind: int = 12,
    currencies: tuple[str, ...] = ("USD",),
    min_impact: str = "Medium",
) -> list[dict[str, Any]]:
    """Return nearby economic events relevant to US equities."""
    try:
        req = urllib.request.Request(_URL, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            events = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.warning("economic calendar fetch failed: %s", exc)
        return []

    if not isinstance(events, list):
        return []

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_behind)
    end = now + timedelta(hours=hours_ahead)
    min_rank = _IMPACT_RANK.get(min_impact, 2)
    currency_set = {c.upper() for c in currencies}

    out: list[dict[str, Any]] = []
    for ev in events:
        country = str(ev.get("country") or "").upper()
        if currency_set and country not in currency_set:
            continue
        impact = str(ev.get("impact") or "Low")
        if _IMPACT_RANK.get(impact, 0) < min_rank:
            continue
        when = _parse_when(ev.get("date"))
        if when is None or when < start or when > end:
            continue
        out.append(
            {
                "title": ev.get("title") or "",
                "country": country,
                "impact": impact,
                "when_utc": when.isoformat(),
                "when_et": when.astimezone(_ET).strftime("%a %b %d %H:%M ET"),
                "forecast": ev.get("forecast") or "",
                "previous": ev.get("previous") or "",
                "actual": ev.get("actual") or "",
            }
        )

    out.sort(key=lambda x: x["when_utc"])
    return out[:25]


def _parse_when(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        # Forex Factory mirror uses ISO-like timestamps, often with offset.
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None
