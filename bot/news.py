"""Fetch recent news headlines for a symbol (Yahoo Finance public APIs)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def fetch_news(symbol: str, limit: int = 8) -> list[dict[str, Any]]:
    """Return recent news items for `symbol`. Best-effort; empty on failure."""
    symbol = symbol.upper().strip()
    for fetcher in (_from_chart_meta, _from_search, _from_google_rss):
        try:
            items = fetcher(symbol, limit)
            if items:
                return items[:limit]
        except Exception as exc:
            logger.warning(
                "news fetch via %s failed for %s: %s", fetcher.__name__, symbol, exc
            )
    return []


def _from_google_rss(symbol: str, limit: int) -> list[dict[str, Any]]:
    import xml.etree.ElementTree as ET

    url = (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode({"q": f"{symbol} stock", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    )
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        xml_text = resp.read().decode("utf-8", errors="replace")
    root = ET.fromstring(xml_text)
    items: list[dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        # Strip trailing " - Publisher" when present.
        publisher = ""
        if " - " in title:
            title, publisher = title.rsplit(" - ", 1)
        items.append(
            {
                "title": title.strip(),
                "publisher": publisher.strip(),
                "link": (item.findtext("link") or "").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
            }
        )
        if len(items) >= limit:
            break
    return items


def _from_chart_meta(symbol: str, limit: int) -> list[dict[str, Any]]:
    """Use quoteSummary modules when available via chart endpoint extras."""
    # Yahoo chart does not include news; try finance news RSS-like crumb-free endpoint.
    url = (
        "https://query1.finance.yahoo.com/v2/finance/news?"
        + urllib.parse.urlencode({"symbols": symbol, "count": limit})
    )
    payload = _get_json(url)
    stream = (
        ((payload.get("Content") or {}).get("result"))
        or payload.get("items")
        or payload.get("news")
        or []
    )
    return _normalize(stream, limit)


def _from_search(symbol: str, limit: int) -> list[dict[str, Any]]:
    url = (
        "https://query1.finance.yahoo.com/v1/finance/search?"
        + urllib.parse.urlencode({"q": symbol, "newsCount": limit, "quotesCount": 1})
    )
    payload = _get_json(url)
    return _normalize(payload.get("news") or [], limit)


def _normalize(raw_items: list[Any], limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = (raw.get("title") or raw.get("headline") or "").strip()
        if not title:
            continue
        items.append(
            {
                "title": title,
                "publisher": raw.get("publisher")
                or raw.get("provider")
                or raw.get("source")
                or "",
                "link": raw.get("link") or raw.get("url") or "",
                "published": raw.get("providerPublishTime")
                or raw.get("pubDate")
                or raw.get("published_at")
                or "",
            }
        )
        if len(items) >= limit:
            break
    return items


def _get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))
