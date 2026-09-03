"""Stock ticker search engine for AlgoPaca.

Provides fast, scored search across US equities, ETFs, and major market tickers
by symbol and company name, augmented with live broker assets when available,
and personalized with user portfolio holdings and watchlist priorities.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Cache TTL for Alpaca broker assets list (6 hours)
_BROKER_ASSETS_TTL = 21600.0

# In-memory broker assets cache: user_id -> (timestamp, assets_list)
_BROKER_ASSETS_CACHE: dict[int, tuple[float, list[dict[str, Any]]]] = {}
_BROKER_CACHE_LOCK = threading.Lock()

# Curated catalog of top US equities, ETFs, and indices with company names
# Provides instant 0ms search even when offline, unauthenticated, or in demo mode.
CURATED_TICKERS: list[dict[str, str]] = [
    # Mega-cap Tech & Growth
    {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "MSFT", "name": "Microsoft Corporation", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "NVDA", "name": "NVIDIA Corporation", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "AMZN", "name": "Amazon.com Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "GOOGL", "name": "Alphabet Inc. Class A", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "GOOG", "name": "Alphabet Inc. Class C", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "META", "name": "Meta Platforms Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "TSLA", "name": "Tesla Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "AVGO", "name": "Broadcom Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "ORCL", "name": "Oracle Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "ADBE", "name": "Adobe Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "CRM", "name": "Salesforce Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "AMD", "name": "Advanced Micro Devices Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "INTC", "name": "Intel Corporation", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "QCOM", "name": "Qualcomm Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "CSCO", "name": "Cisco Systems Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "IBM", "name": "International Business Machines Corp.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "TXN", "name": "Texas Instruments Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "NOW", "name": "ServiceNow Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "INTU", "name": "Intuit Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "AMAT", "name": "Applied Materials Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "MU", "name": "Micron Technology Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "LRCX", "name": "Lam Research Corporation", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "PANW", "name": "Palo Alto Networks Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "SNPS", "name": "Synopsys Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "CDNS", "name": "Cadence Design Systems Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "KLAC", "name": "KLA Corporation", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "CRWD", "name": "CrowdStrike Holdings Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "PLTR", "name": "Palantir Technologies Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "ANET", "name": "Arista Networks Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "MRVL", "name": "Marvell Technology Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "FTNT", "name": "Fortinet Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "WDAY", "name": "Workday Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "SNOW", "name": "Snowflake Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "TEAM", "name": "Atlassian Corporation", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "DDOG", "name": "Datadog Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "ZS", "name": "Zscaler Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "MDB", "name": "MongoDB Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "NET", "name": "Cloudflare Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "ARM", "name": "Arm Holdings plc", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "SMCI", "name": "Super Micro Computer Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "DELL", "name": "Dell Technologies Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "HPE", "name": "Hewlett Packard Enterprise Co.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "HPQ", "name": "HP Inc.", "exchange": "NYSE", "asset_class": "us_equity"},

    # Top Major Indices & ETFs
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "QQQ", "name": "Invesco QQQ Trust (Nasdaq 100)", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "IWM", "name": "iShares Russell 2000 ETF", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "DIA", "name": "SPDR Dow Jones Industrial Average ETF", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "VOO", "name": "Vanguard S&P 500 ETF", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "VTI", "name": "Vanguard Total Stock Market ETF", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "IVV", "name": "iShares Core S&P 500 ETF", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "SOXX", "name": "iShares Semiconductor ETF", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "SMH", "name": "VanEck Semiconductor ETF", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "XLF", "name": "Financial Select Sector SPDR Fund", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "XLK", "name": "Technology Select Sector SPDR Fund", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "XLE", "name": "Energy Select Sector SPDR Fund", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "XLV", "name": "Health Care Select Sector SPDR Fund", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "XLI", "name": "Industrial Select Sector SPDR Fund", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "XLP", "name": "Consumer Staples Select Sector SPDR Fund", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "XLY", "name": "Consumer Discretionary Select Sector SPDR", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "XLU", "name": "Utilities Select Sector SPDR Fund", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "XLB", "name": "Materials Select Sector SPDR Fund", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "VNQ", "name": "Vanguard Real Estate ETF", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "ARKK", "name": "ARK Innovation ETF", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "TLT", "name": "iShares 20+ Year Treasury Bond ETF", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "TQQQ", "name": "ProShares UltraPro QQQ (3x Bull)", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "SQQQ", "name": "ProShares UltraPro Short QQQ (-3x Bear)", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "SOXL", "name": "Direxion Daily Semiconductor Bull 3X", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "SOXS", "name": "Direxion Daily Semiconductor Bear 3X", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "UVXY", "name": "ProShares Ultra VIX Short-Term Futures", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "GLD", "name": "SPDR Gold Shares ETF", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "SLV", "name": "iShares Silver Trust", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "USO", "name": "United States Oil Fund", "exchange": "ARCA", "asset_class": "us_equity"},
    {"symbol": "IBIT", "name": "iShares Bitcoin Trust ETF", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "FBTC", "name": "Fidelity Wise Origin Bitcoin Fund", "exchange": "CBOE", "asset_class": "us_equity"},

    # Consumer & Media
    {"symbol": "NFLX", "name": "Netflix Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "DIS", "name": "The Walt Disney Company", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "WMT", "name": "Walmart Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "COST", "name": "Costco Wholesale Corporation", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "TGT", "name": "Target Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "HD", "name": "The Home Depot Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "LOW", "name": "Lowe's Companies Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "NKE", "name": "NIKE Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "MCD", "name": "McDonald's Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "SBUX", "name": "Starbucks Corporation", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "KO", "name": "The Coca-Cola Company", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "PEP", "name": "PepsiCo Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "PG", "name": "The Procter & Gamble Company", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "PM", "name": "Philip Morris International Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "MO", "name": "Altria Group Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "CMG", "name": "Chipotle Mexican Grill Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "BKNG", "name": "Booking Holdings Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "ABNB", "name": "Airbnb Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "UBER", "name": "Uber Technologies Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "LYFT", "name": "Lyft Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "DASH", "name": "DoorDash Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "SPOT", "name": "Spotify Technology S.A.", "exchange": "NYSE", "asset_class": "us_equity"},

    # Financials & Payments
    {"symbol": "JPM", "name": "JPMorgan Chase & Co.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "BAC", "name": "Bank of America Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "WFC", "name": "Wells Fargo & Company", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "C", "name": "Citigroup Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "GS", "name": "The Goldman Sachs Group Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "MS", "name": "Morgan Stanley", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "BLK", "name": "BlackRock Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "SCHW", "name": "The Charles Schwab Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "AXP", "name": "American Express Company", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "V", "name": "Visa Inc. Class A", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "MA", "name": "Mastercard Incorporated Class A", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "PYPL", "name": "PayPal Holdings Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "SQ", "name": "Block Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "COIN", "name": "Coinbase Global Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "HOOD", "name": "Robinhood Markets Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "MSTR", "name": "MicroStrategy Incorporated", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "BRK.B", "name": "Berkshire Hathaway Inc. Class B", "exchange": "NYSE", "asset_class": "us_equity"},

    # Healthcare & Pharma
    {"symbol": "LLY", "name": "Eli Lilly and Company", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "UNH", "name": "UnitedHealth Group Incorporated", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "JNJ", "name": "Johnson & Johnson", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "ABBV", "name": "AbbVie Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "MRK", "name": "Merck & Co. Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "PFE", "name": "Pfizer Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "TMO", "name": "Thermo Fisher Scientific Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "ABT", "name": "Abbott Laboratories", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "DHR", "name": "Danaher Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "BMY", "name": "Bristol-Myers Squibb Company", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "AMGN", "name": "Amgen Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "GILD", "name": "Gilead Sciences Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "ISRG", "name": "Intuitive Surgical Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "VRTX", "name": "Vertex Pharmaceuticals Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "REGN", "name": "Regeneron Pharmaceuticals Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "MDLZ", "name": "Mondelez International Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},

    # Energy, Industrials & Materials
    {"symbol": "XOM", "name": "Exxon Mobil Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "CVX", "name": "Chevron Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "COP", "name": "ConocoPhillips", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "SLB", "name": "Schlumberger Limited", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "EOG", "name": "EOG Resources Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "CAT", "name": "Caterpillar Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "DE", "name": "Deere & Company", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "GE", "name": "General Electric Company", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "HON", "name": "Honeywell International Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "UNP", "name": "Union Pacific Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "RTX", "name": "RTX Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "LMT", "name": "Lockheed Martin Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "BA", "name": "The Boeing Company", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "UPS", "name": "United Parcel Service Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "FDX", "name": "FedEx Corporation", "exchange": "NYSE", "asset_class": "us_equity"},

    # High-Interest / Trending Stocks
    {"symbol": "GME", "name": "GameStop Corp. Class A", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "AMC", "name": "AMC Entertainment Holdings Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "SOFI", "name": "SoFi Technologies Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "RIVN", "name": "Rivian Automotive Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "LCID", "name": "Lucid Group Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "NIO", "name": "NIO Inc. ADR", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "BABA", "name": "Alibaba Group Holding Limited", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "JD", "name": "JD.com Inc. ADR", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "PDD", "name": "PDD Holdings Inc. ADR", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "BIDU", "name": "Baidu Inc. ADR", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "SHOP", "name": "Shopify Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "SE", "name": "Sea Limited ADR", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "RBLX", "name": "Roblox Corporation", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "U", "name": "Unity Software Inc.", "exchange": "NYSE", "asset_class": "us_equity"},
    {"symbol": "AFRM", "name": "Affirm Holdings Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "MARA", "name": "MARA Holdings Inc. (Marathon Digital)", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "RIOT", "name": "Riot Platforms Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "CLSK", "name": "CleanSpark Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "APP", "name": "AppLovin Corporation", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "ASTS", "name": "AST SpaceMobile Inc.", "exchange": "NASDAQ", "asset_class": "us_equity"},
    {"symbol": "RDDT", "name": "Reddit Inc. Class A", "exchange": "NYSE", "asset_class": "us_equity"},
]

# Quick lookup map by uppercase symbol
_CURATED_MAP: dict[str, dict[str, str]] = {
    item["symbol"].upper(): item for item in CURATED_TICKERS
}


def _get_broker_assets(service: Any, user_id: int) -> list[dict[str, Any]]:
    """Fetch all active tradable assets from Alpaca trading client with caching."""
    now = time.monotonic()
    with _BROKER_CACHE_LOCK:
        cached = _BROKER_ASSETS_CACHE.get(user_id)
        if cached and cached[0] > now:
            return cached[1]

    if not service:
        return []

    try:
        trading = getattr(service, "trading", None)
        if not trading:
            return []

        # Attempt to import alpaca GetAssetsRequest if installed
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        req = GetAssetsRequest(
            asset_class=AssetClass.US_EQUITY,
            status=AssetStatus.ACTIVE,
        )
        raw_assets = trading.get_all_assets(req)

        assets: list[dict[str, Any]] = []
        for a in raw_assets:
            sym = str(getattr(a, "symbol", "") or "").upper().strip()
            if not sym or not getattr(a, "tradable", False):
                continue
            assets.append(
                {
                    "symbol": sym,
                    "name": str(getattr(a, "name", "") or ""),
                    "exchange": str(getattr(a, "exchange", "US") or "US"),
                    "asset_class": str(getattr(a, "asset_class", "us_equity") or "us_equity"),
                    "tradable": bool(getattr(a, "tradable", True)),
                    "shortable": bool(getattr(a, "shortable", False)),
                    "fractionable": bool(getattr(a, "fractionable", False)),
                }
            )

        with _BROKER_CACHE_LOCK:
            _BROKER_ASSETS_CACHE[user_id] = (now + _BROKER_ASSETS_TTL, assets)
        return assets
    except Exception as exc:
        logger.debug("Could not fetch Alpaca broker assets for user %s: %s", user_id, exc)
        with _BROKER_CACHE_LOCK:
            _BROKER_ASSETS_CACHE[user_id] = (now + 300.0, [])
        return []


def search_tickers(
    query: str,
    user_id: int | None = None,
    service: Any = None,
    positions: list[dict[str, Any]] | None = None,
    watchlist: list[str] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search stock tickers and companies with relevance scoring.

    Matches against symbol (exact, prefix, substring) and company name words.
    Boosts score for user portfolio holdings and watchlist symbols.
    """
    raw_q = str(query or "").strip()
    q = raw_q.upper()
    q_lower = raw_q.lower()

    pos_map: dict[str, dict[str, Any]] = {}
    if positions:
        for p in positions:
            sym = str(p.get("symbol") or "").upper().strip()
            if sym:
                pos_map[sym] = p

    watch_set: set[str] = set()
    if watchlist:
        for w in watchlist:
            sym = str(w or "").upper().strip()
            if sym:
                watch_set.add(sym)

    # Gather candidate pool
    candidates_dict: dict[str, dict[str, Any]] = {}

    # 1. Curated list first
    for item in CURATED_TICKERS:
        sym = item["symbol"].upper()
        candidates_dict[sym] = {
            "symbol": sym,
            "name": item["name"],
            "exchange": item.get("exchange", "US"),
            "asset_class": item.get("asset_class", "us_equity"),
            "tradable": True,
        }

    # 2. Add broker assets if available
    if service and user_id is not None:
        broker_assets = _get_broker_assets(service, user_id)
        for a in broker_assets:
            sym = a["symbol"].upper()
            if sym not in candidates_dict:
                candidates_dict[sym] = a

    # 3. Add any existing positions / watchlist that might not be in curated list
    for sym, p in pos_map.items():
        if sym not in candidates_dict:
            candidates_dict[sym] = {
                "symbol": sym,
                "name": str(p.get("name") or sym),
                "exchange": "US",
                "asset_class": "us_equity",
                "tradable": True,
            }

    for sym in watch_set:
        if sym not in candidates_dict:
            candidates_dict[sym] = {
                "symbol": sym,
                "name": sym,
                "exchange": "US",
                "asset_class": "us_equity",
                "tradable": True,
            }

    # If query is empty, return popular / holdings / watchlist items
    if not q:
        scored_defaults: list[tuple[float, dict[str, Any]]] = []
        for sym, item in candidates_dict.items():
            score = 0.0
            if sym in pos_map:
                score += 500.0  # Owned positions on top
            elif sym in watch_set:
                score += 300.0  # Watchlist next
            elif sym in _CURATED_MAP:
                score += 100.0 - min(len(scored_defaults), 50)
            else:
                continue

            scored_defaults.append((score, item))

        scored_defaults.sort(key=lambda x: x[0], reverse=True)
        return _format_results(scored_defaults[:limit], pos_map, watch_set)

    # If user typed an exact symbol that isn't in candidates, synthesize candidate as fallback
    is_synthesized: set[str] = set()
    if re.match(r"^[A-Z0-9.\-]{1,10}$", q) and q not in candidates_dict:
        is_synthesized.add(q)
        candidates_dict[q] = {
            "symbol": q,
            "name": f"{q} Equity",
            "exchange": "US",
            "asset_class": "us_equity",
            "tradable": True,
        }

    # Score candidates
    scored: list[tuple[float, dict[str, Any]]] = []
    q_words = [w for w in re.split(r"\W+", q_lower) if w]

    for sym, item in candidates_dict.items():
        name = str(item.get("name") or "")
        name_lower = name.lower()
        score = 0.0

        # Symbol matching
        if sym in is_synthesized:
            score = 350.0
        elif sym == q:
            score += 1000.0
        elif sym.startswith(q):
            # Short prefix matches are higher value: e.g. "AAP" -> "AAPL"
            score += 600.0 - (len(sym) - len(q)) * 25.0
        elif q in sym:
            score += 300.0 - (len(sym) - len(q)) * 10.0

        if sym not in is_synthesized:
            # Name matching
            if q_lower and q_lower == name_lower:
                score += 850.0
            elif name_lower.startswith(q_lower):
                score += 650.0
            elif q_words:
                # Word-boundary matching (e.g. "Apple" matches "Apple Inc.")
                matched_words = 0
                for qw in q_words:
                    pattern = rf"\b{re.escape(qw)}"
                    if re.search(pattern, name_lower):
                        matched_words += 1
                if matched_words == len(q_words):
                    score += 500.0 + (matched_words * 25.0)
                elif matched_words > 0:
                    score += 250.0 * (matched_words / len(q_words))

            # Substring fallback
            if score == 0.0 and q_lower in name_lower:
                score += 150.0

        if score > 0.0:
            # User portfolio holding boost
            if sym in pos_map:
                score += 250.0
            # User watchlist boost
            if sym in watch_set:
                score += 100.0

            scored.append((score, item))

    # Sort descending by score, then alphabetically by symbol
    scored.sort(key=lambda x: (-x[0], x[1]["symbol"]))
    return _format_results(scored[:limit], pos_map, watch_set)


def _format_results(
    scored_items: list[tuple[float, dict[str, Any]]],
    pos_map: dict[str, dict[str, Any]],
    watch_set: set[str],
) -> list[dict[str, Any]]:
    """Format matching candidates with portfolio and watchlist metadata."""
    results: list[dict[str, Any]] = []
    for _, item in scored_items:
        sym = item["symbol"].upper()
        pos = pos_map.get(sym)
        in_portfolio = pos is not None
        holding_qty = None
        unrealized_pl = None
        unrealized_plpc = None
        current_price = None

        if in_portfolio and pos:
            try:
                holding_qty = float(pos.get("qty", 0.0) or 0.0)
                unrealized_pl = float(pos.get("unrealized_pl", 0.0) or 0.0)
                unrealized_plpc = float(pos.get("unrealized_plpc", 0.0) or 0.0)
                current_price = float(pos.get("current_price", 0.0) or 0.0)
            except (ValueError, TypeError):
                pass

        results.append(
            {
                "symbol": sym,
                "name": item.get("name") or sym,
                "exchange": item.get("exchange", "US"),
                "asset_class": item.get("asset_class", "us_equity"),
                "tradable": item.get("tradable", True),
                "in_portfolio": in_portfolio,
                "holding_qty": holding_qty,
                "unrealized_pl": unrealized_pl,
                "unrealized_plpc": unrealized_plpc,
                "current_price": current_price,
                "in_watchlist": sym in watch_set,
            }
        )
    return results
