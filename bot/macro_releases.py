"""Real-time economic release fetcher and calendar enrichment.

Provides millisecond/second-level detection of FOMC Federal Funds Rate decisions
from Federal Reserve official press releases, parses statement target rates,
and enriches the economic calendar so traders and AI models operate on live
released data rather than stale 'upcoming' hold states.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_FED_PRESS_MONETARY_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"

# In-memory cache for live FOMC release to avoid redundant HTTP requests
_FOMC_CACHE: dict[str, Any] | None = None
_FOMC_CACHE_EXPIRES = 0.0
_FOMC_LOCK = threading.Lock()
_FOMC_TTL_SECONDS = 30.0  # Fast 30s TTL during market hours


def parse_rate_fraction(text: str) -> float | None:
    """Parse fractional percentage strings like '3-3/4', '5 1/4', '4 percent' into float."""
    if not text:
        return None
    cleaned = text.strip().replace("percent", "").replace("%", "").strip()
    if "-" in cleaned:
        cleaned = cleaned.replace("-", " ")
    if " " in cleaned and "/" in cleaned:
        parts = cleaned.split()
        try:
            whole = float(parts[0])
            frac_str = parts[1].strip()
            if "/" in frac_str:
                num, denom = frac_str.split("/", 1)
                return whole + float(num) / float(denom)
            return whole + float(frac_str)
        except Exception:
            pass
    elif "/" in cleaned:
        try:
            num, denom = cleaned.split("/", 1)
            return float(num) / float(denom)
        except Exception:
            pass
    try:
        return float(cleaned)
    except Exception:
        return None


def extract_fomc_statement_details(html_or_text: str) -> dict[str, Any] | None:
    """Parse FOMC statement text to extract target rate range, action, and change."""
    clean = re.sub(r"<[^<]+?>", " ", html_or_text)
    clean = " ".join(clean.split())

    # Regex pattern matching FOMC rate decisions:
    # "decided to raise the target range for the federal funds rate by 1/4 percentage point to 3-3/4 to 4 percent"
    # "decided to lower the target range for the federal funds rate by 1/4 percentage point to 4-1/2 to 4-3/4 percent"
    # "decided to maintain the target range for the federal funds rate at 5-1/4 to 5-1/2 percent"
    raise_match = re.search(
        r"(?:decided to\s+)?(raise|lower|reduce|maintain|increase)\s+"
        r"(?:the target range for\s+)?the federal funds rate\s+"
        r"(?:by\s+([0-9/\- ]+)\s+percentage point\s+)?"
        r"(?:to|at)\s+([0-9\-/ ]+)\s+to\s+([0-9\-/ ]+)\s+percent",
        clean,
        re.IGNORECASE,
    )

    if raise_match:
        verb, by_pct, low_str, high_str = raise_match.groups()
        verb = (verb or "").lower()
        if verb in ("raise", "increase", "hike"):
            action = "hike"
            change_bps = 50 if "1/2" in (by_pct or "") else 25
        elif verb in ("lower", "reduce", "cut"):
            action = "cut"
            change_bps = -50 if "1/2" in (by_pct or "") else -25
        else:
            action = "maintain"
            change_bps = 0

        low_val = parse_rate_fraction(low_str)
        high_val = parse_rate_fraction(high_str)
        rate_str = f"{high_val:.2f}%" if high_val is not None else f"{high_str}%"

        return {
            "action": action,
            "change_bps": change_bps,
            "rate": rate_str,
            "range": f"{low_str.strip()} to {high_str.strip()}%",
            "target_rate": high_val,
            "target_rate_lower": low_val,
        }

    # Fallback for single rate target format: "maintain the target range for the federal funds rate at X percent"
    maintain_match = re.search(
        r"(?:decided to\s+)?maintain\s+(?:the target range for\s+)?the federal funds rate\s+at\s+([0-9\-/ ]+)\s+percent",
        clean,
        re.IGNORECASE,
    )
    if maintain_match:
        single_rate = maintain_match.group(1).strip()
        val = parse_rate_fraction(single_rate)
        return {
            "action": "maintain",
            "change_bps": 0,
            "rate": f"{val:.2f}%" if val is not None else single_rate,
            "range": f"{single_rate}%",
            "target_rate": val,
            "target_rate_lower": val,
        }

    return None


def fetch_fomc_live_release(force_refresh: bool = False) -> dict[str, Any] | None:
    """Fetch live FOMC decision directly from Federal Reserve RSS and press statement."""
    global _FOMC_CACHE, _FOMC_CACHE_EXPIRES

    now_ts = time.time()
    with _FOMC_LOCK:
        if not force_refresh and _FOMC_CACHE is not None and now_ts < _FOMC_CACHE_EXPIRES:
            return dict(_FOMC_CACHE)

    try:
        req = urllib.request.Request(_FED_PRESS_MONETARY_URL, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_text = resp.read()

        root = ET.fromstring(xml_text)
        statement_item = None
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if "statement" in title.lower() or "fomc" in title.lower():
                statement_item = item
                break

        if statement_item is None:
            return None

        title = statement_item.findtext("title") or "FOMC Statement"
        link = statement_item.findtext("link") or ""
        pub_date_str = statement_item.findtext("pubDate") or ""

        # Fetch statement page content
        details: dict[str, Any] | None = None
        if link:
            req_stmt = urllib.request.Request(link, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req_stmt, timeout=10) as stmt_resp:
                html = stmt_resp.read().decode("utf-8", errors="replace")
            details = extract_fomc_statement_details(html)

        if not details:
            return None

        action = details["action"]
        rate = details["rate"]
        change_bps = details["change_bps"]

        bias = "hawkish" if action == "hike" else ("dovish" if action == "cut" else "neutral")
        metals_impact = (
            "Bearish headwind for GLD/SLV (higher bond yields & USD strength); favorable for inverse ETFs (GLL/GDXD) or short positions."
            if action == "hike"
            else (
                "Highly bullish catalyst for GLD/SLV (falling bond yields & USD weakness); favors long positions."
                if action == "cut"
                else "Neutral / as-expected; follow prevailing trend and breakout levels."
            )
        )

        result: dict[str, Any] = {
            "title": "Federal Funds Rate",
            "statement_title": title,
            "statement_link": link,
            "pub_date": pub_date_str,
            "actual": rate,
            "rate": rate,
            "range": details["range"],
            "action": action,
            "change_bps": change_bps,
            "bias": bias,
            "metals_impact": metals_impact,
            "status": "released",
            "released": True,
            "asof": datetime.now(timezone.utc).isoformat(),
        }

        with _FOMC_LOCK:
            _FOMC_CACHE = dict(result)
            _FOMC_CACHE_EXPIRES = now_ts + _FOMC_TTL_SECONDS

        return result

    except Exception as exc:
        logger.debug("Failed fetching FOMC live release: %s", exc)
        return None


def fetch_live_macro_headlines(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Fetch live news headlines from Google News RSS for real-time catalyst monitoring."""
    url = (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    )
    items: list[dict[str, Any]] = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            xml_text = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(xml_text)
        for item in root.findall("./channel/item")[:limit]:
            title = (item.findtext("title") or "").strip()
            if " - " in title:
                title, pub = title.rsplit(" - ", 1)
            else:
                pub = ""
            items.append(
                {
                    "title": title.strip(),
                    "publisher": pub.strip(),
                    "link": (item.findtext("link") or "").strip(),
                    "published": (item.findtext("pubDate") or "").strip(),
                }
            )
    except Exception as exc:
        logger.debug("Failed fetching live macro headlines for %s: %s", query, exc)
    return items


def enrich_economic_calendar_realtime(
    calendar_events: list[dict[str, Any]],
    now_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    """Enrich calendar events with real-time actual release values and accurate statuses.

    - Marks events whose scheduled time has arrived (when_dt <= now_utc) as 'released'.
    - Injects real-time FOMC rate decision into 'Federal Funds Rate' and related events.
    - Prevents past or already-released events from being flagged as 'upcoming'.
    """
    if not calendar_events:
        return []

    now = now_utc or datetime.now(timezone.utc)
    enriched: list[dict[str, Any]] = []

    fomc_live: dict[str, Any] | None = None

    for ev in calendar_events:
        item = dict(ev)
        title = str(item.get("title") or "")
        title_lower = title.lower()
        when_utc_str = item.get("when_utc")

        when_dt: datetime | None = None
        minutes_diff: float | None = None
        if when_utc_str:
            try:
                when_dt = datetime.fromisoformat(str(when_utc_str).replace("Z", "+00:00"))
                if when_dt.tzinfo is None:
                    when_dt = when_dt.replace(tzinfo=timezone.utc)
                minutes_diff = (when_dt - now).total_seconds() / 60.0
                item["minutes_away"] = round(minutes_diff, 1)
            except Exception:
                pass

        # Check if this is an FOMC / Federal Funds Rate event
        is_fomc_rate = "federal funds" in title_lower or (
            "fomc" in title_lower and "rate" in title_lower
        )
        is_fomc_statement = "fomc statement" in title_lower or "fomc economic" in title_lower
        is_fomc_press = "fomc press conference" in title_lower

        # Determine release status based on time and actual value
        actual_val = str(item.get("actual") or "").strip()
        already_has_actual = bool(actual_val)

        # If time has arrived or passed
        is_due_or_past = minutes_diff is not None and minutes_diff <= 0.0

        if is_fomc_rate or is_fomc_statement:
            if is_due_or_past or not already_has_actual:
                if fomc_live is None:
                    fomc_live = fetch_fomc_live_release()

                if fomc_live:
                    item["actual"] = fomc_live["actual"]
                    item["status"] = "released"
                    item["released"] = True
                    item["action"] = fomc_live["action"]
                    item["change_bps"] = fomc_live["change_bps"]
                    item["bias"] = fomc_live["bias"]
                    item["outcome"] = (
                        f"{fomc_live['action'].upper()} ({fomc_live['change_bps']:+d} bps) to {fomc_live['actual']}"
                    )
                    item["metals_impact"] = fomc_live["metals_impact"]
                    actual_val = fomc_live["actual"]
                    already_has_actual = True

        if is_fomc_press:
            # If the rate decision was already announced (e.g. 14:00), the press conference
            # is follow-up remarks, NOT an unannounced surprise rate shock.
            if fomc_live is None and (minutes_diff is not None and minutes_diff <= 60):
                fomc_live = fetch_fomc_live_release()
            if fomc_live:
                item["rate_already_released"] = True
                item["rate_decision"] = fomc_live["actual"]
                item["rate_action"] = fomc_live["action"]

        # Default release status based on whether scheduled time has passed or actual is present
        if already_has_actual or is_due_or_past:
            item["status"] = "released"
            item["released"] = True
        else:
            item["status"] = "upcoming"
            item["released"] = False

        enriched.append(item)

    return enriched


def get_active_macro_catalyst(
    calendar_events: list[dict[str, Any]] | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the primary active macro catalyst released recently (within last 4 hours)."""
    now = now_utc or datetime.now(timezone.utc)

    # If calendar_events is explicitly provided as empty list, no events are in scope
    has_fomc_in_calendar = True
    if calendar_events is not None:
        if not calendar_events:
            return None
        has_fomc_in_calendar = any(
            "fomc" in str(ev.get("title") or "").lower()
            or "federal funds" in str(ev.get("title") or "").lower()
            for ev in calendar_events
        )

    # Check live FOMC release if FOMC is in scope
    if has_fomc_in_calendar:
        fomc = fetch_fomc_live_release()
        if fomc:
            return {
                "event": "Federal Funds Rate",
                "actual": fomc["actual"],
                "action": fomc["action"],
                "change_bps": fomc["change_bps"],
                "bias": fomc["bias"],
                "metals_impact": fomc["metals_impact"],
                "status": "released",
                "released": True,
                "headline": f"FOMC {fomc['action'].upper()} to {fomc['actual']} ({fomc['change_bps']:+d} bps)",
                "asof": fomc.get("asof"),
            }

    # Otherwise scan calendar events for recently released high-impact events
    if calendar_events:
        for ev in calendar_events:
            impact = str(ev.get("impact") or "Low")
            if impact != "High":
                continue
            if not ev.get("released"):
                continue
            actual = str(ev.get("actual") or "").strip()
            if not actual:
                continue
            minutes_away = float(ev.get("minutes_away", 0.0))
            # Released within the last 240 minutes (-240m <= minutes_away <= 0m)
            if -240.0 <= minutes_away <= 0.0:
                return {
                    "event": ev.get("title"),
                    "actual": actual,
                    "forecast": ev.get("forecast"),
                    "previous": ev.get("previous"),
                    "status": "released",
                    "released": True,
                    "headline": f"{ev.get('title')}: {actual}",
                }

    return None
