"""Live equity quotes including pre/post market.

Alpaca free/paper IEX often lags outside RTH. Nasdaq's public quote API
exposes the current sale (pre-market / after-hours / regular). Yahoo chart
is kept as a secondary fallback with caching because it rate-limits.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_YAHOO_BACKOFF_UNTIL = 0.0
_CACHE_TTL_OK = 15.0
_CACHE_TTL_ERR = 30.0

_MONEY_RE = re.compile(r"[^0-9.\-]")


def fetch_live_mark(symbol: str) -> dict[str, Any]:
    """Best-effort live mark with pre/post coverage."""
    sym = symbol.upper().strip()
    now = time.time()
    cached = _CACHE.get(sym)
    if cached and cached[0] > now and cached[1] is not None:
        return dict(cached[1])

    errors: list[str] = []
    for fetcher in (_fetch_nasdaq, _fetch_yahoo):
        try:
            mark = fetcher(sym)
            _CACHE[sym] = (now + _CACHE_TTL_OK, mark)
            return dict(mark)
        except Exception as exc:
            errors.append(f"{fetcher.__name__}: {exc}")
            logger.info("live quote miss %s via %s: %s", sym, fetcher.__name__, exc)

    if cached and cached[1] is not None:
        return dict(cached[1])
    _CACHE[sym] = (now + _CACHE_TTL_ERR, None)
    raise ValueError(f"Live quote failed for {sym} ({'; '.join(errors)})")


def _fetch_nasdaq(sym: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for assetclass in ("stocks", "etf"):
        try:
            return _fetch_nasdaq_asset(sym, assetclass)
        except Exception as exc:
            last_error = exc
    raise last_error or ValueError("Nasdaq quote failed")


def _fetch_nasdaq_asset(sym: str, assetclass: str) -> dict[str, Any]:
    url = f"https://api.nasdaq.com/api/quote/{sym}/info?assetclass={assetclass}"
    payload = _http_get_json(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "application/json,text/plain,*/*",
            "Origin": "https://www.nasdaq.com",
            "Referer": f"https://www.nasdaq.com/market-activity/stocks/{sym.lower()}",
        },
    )
    data = payload.get("data") or {}
    primary = data.get("primaryData") or {}
    secondary = data.get("secondaryData") or {}
    price = _parse_money(primary.get("lastSalePrice"))
    if price is None:
        raise ValueError(f"Nasdaq missing lastSalePrice ({assetclass})")

    asof = _parse_nasdaq_timestamp(primary.get("lastTradeTimestamp"))
    bar_close = _parse_money(secondary.get("lastSalePrice"))
    return {
        "symbol": sym,
        "price": price,
        "asof": asof,
        "source": "nasdaq_live",
        "previous_close": bar_close,
        "regular_price": bar_close,
        "market_status": data.get("marketStatus"),
        "bid": _parse_money(primary.get("bidPrice")),
        "ask": _parse_money(primary.get("askPrice")),
    }


def _fetch_yahoo(sym: str) -> dict[str, Any]:
    global _YAHOO_BACKOFF_UNTIL
    now = time.time()
    if now < _YAHOO_BACKOFF_UNTIL:
        raise ValueError(f"Yahoo backing off {int(_YAHOO_BACKOFF_UNTIL - now)}s")

    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{sym}?interval=1m&range=1d&includePrePost=true"
    )
    try:
        payload = _http_get_json(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "application/json",
            },
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _YAHOO_BACKOFF_UNTIL = now + 90
        raise

    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        raise ValueError("Yahoo empty chart")
    result = results[0]
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    pairs = [(ts, float(c)) for ts, c in zip(timestamps, closes) if c is not None]
    if not pairs:
        raise ValueError("Yahoo empty closes")
    ts, price = pairs[-1]
    from datetime import timezone

    asof = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return {
        "symbol": sym,
        "price": price,
        "asof": asof,
        "source": "yahoo_live",
        "previous_close": _meta_float(meta, "previousClose"),
        "regular_price": _meta_float(meta, "regularMarketPrice"),
        "bid": None,
        "ask": None,
    }


def _http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=2.5) as resp:
        return json.loads(resp.read().decode())


def _parse_money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _MONEY_RE.sub("", str(value))
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_nasdaq_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    # e.g. "Aug 7, 2026 8:41 AM ET", "Closed at Aug 6, 2026 4:00 PM ET",
    # "DATA AS OF Aug 13, 2026 10:42 AM ET"
    text = re.sub(r"^(DATA AS OF|Closed at)\s+", "", text, flags=re.I)
    text = re.sub(r"\s+ET$", "", text)
    for fmt in ("%b %d, %Y %I:%M %p", "%b %d, %Y %H:%M"):
        try:
            naive = datetime.strptime(text, fmt)
            return naive.replace(tzinfo=_ET)
        except ValueError:
            continue
    return None


def _meta_float(meta: dict[str, Any], key: str) -> float | None:
    val = meta.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
