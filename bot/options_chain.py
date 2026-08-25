"""OCC parsing and contract selection for the options overlay.

Pure functions over plain dicts so the overlay can be tested without Alpaca.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

# Compact OCC: ROOT + YYMMDD + C/P + strike*1000 (8 digits).
OCC_RE = re.compile(
    r"^(?P<root>[A-Z]{1,6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<cp>[CP])(?P<strike>\d{8})$"
)

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

OPTIONS_STYLES = ("vertical", "long_option", "hedge")


def normalize_options_style(raw: str | None) -> str:
    text = str(raw or "vertical").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "debit": "vertical",
        "spread": "vertical",
        "vertical_spread": "vertical",
        "call": "long_option",
        "put": "long_option",
        "long": "long_option",
        "naked": "long_option",
        "protective": "hedge",
        "covered": "hedge",
        "married": "hedge",
    }
    text = aliases.get(text, text)
    return text if text in OPTIONS_STYLES else "vertical"


def occ_root(symbol: str | None) -> str:
    """Underlying root as it appears in OCC symbols (``BRK.B`` → ``BRKB``)."""
    return str(symbol or "").strip().upper().replace(".", "")


def is_occ_symbol(symbol: str | None) -> bool:
    return bool(OCC_RE.match(str(symbol or "").strip().upper()))


def parse_occ(symbol: str | None) -> dict[str, Any] | None:
    """Break an OCC option symbol into root, expiry, type, and strike."""
    match = OCC_RE.match(str(symbol or "").strip().upper())
    if not match:
        return None
    yy = int(match.group("yy"))
    mm = int(match.group("mm"))
    dd = int(match.group("dd"))
    year = 2000 + yy if yy < 80 else 1900 + yy
    try:
        expiration = date(year, mm, dd)
    except ValueError:
        return None
    strike = int(match.group("strike")) / 1000.0
    cp = match.group("cp")
    return {
        "symbol": match.group(0),
        "root": match.group("root"),
        "expiration": expiration,
        "cp": cp,
        "type": "call" if cp == "C" else "put",
        "strike": strike,
    }


def option_label(symbol: str | None) -> str:
    """Human label: ``AAPL 17Jan25 150C``."""
    parsed = parse_occ(symbol)
    if not parsed:
        return str(symbol or "").upper()
    exp: date = parsed["expiration"]
    strike = parsed["strike"]
    strike_txt = f"{strike:.0f}" if strike == int(strike) else f"{strike:.2f}".rstrip("0")
    mon = _MONTHS[exp.month - 1]
    return (
        f"{parsed['root']} {exp.day:02d}{mon}{exp.strftime('%y')} "
        f"{strike_txt}{parsed['cp']}"
    )


def as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def dte(expiration: Any, today: date | None = None) -> int | None:
    exp = as_date(expiration)
    if exp is None:
        return None
    return (exp - (today or date.today())).days


def pick_expiration(
    expirations: Iterable[Any],
    *,
    min_dte: int = 21,
    max_dte: int = 45,
    today: date | None = None,
) -> date | None:
    """Nearest expiry inside the DTE window; otherwise the closest future date."""
    today = today or date.today()
    dates: list[date] = []
    seen: set[date] = set()
    for raw in expirations:
        exp = as_date(raw)
        if exp is None or exp in seen or exp < today:
            continue
        seen.add(exp)
        dates.append(exp)
    if not dates:
        return None
    dates.sort()
    lo = max(0, int(min_dte))
    hi = max(lo, int(max_dte))
    in_window = [d for d in dates if lo <= (d - today).days <= hi]
    if in_window:
        return in_window[0]
    # Prefer the first expiry at/after min DTE, else the furthest listed.
    later = [d for d in dates if (d - today).days >= lo]
    return later[0] if later else dates[-1]


def _strike(contract: dict[str, Any]) -> float:
    try:
        return float(contract.get("strike") or contract.get("strike_price") or 0)
    except (TypeError, ValueError):
        return 0.0


def nearest_contract(
    contracts: list[dict[str, Any]],
    target: float,
    *,
    min_strike: float | None = None,
    max_strike: float | None = None,
) -> dict[str, Any] | None:
    """Contract whose strike is closest to ``target``, optional bounds inclusive."""
    eligible: list[dict[str, Any]] = []
    for row in contracts:
        strike = _strike(row)
        if strike <= 0:
            continue
        if min_strike is not None and strike < min_strike - 1e-9:
            continue
        if max_strike is not None and strike > max_strike + 1e-9:
            continue
        eligible.append(row)
    if not eligible:
        return None

    def _key(row: dict[str, Any]) -> tuple[float, float, int]:
        strike = _strike(row)
        oi = int(row.get("open_interest") or 0)
        return (abs(strike - target), strike, -oi)

    return min(eligible, key=_key)


def pick_vertical(
    contracts: list[dict[str, Any]],
    spot: float,
    *,
    option_type: str,
    otm_pct: float = 5.0,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """ATM long + OTM short of the same type (debit vertical).

    Calls: long near spot, short further OTM (higher strike).
    Puts: long near spot, short further OTM (lower strike).
    """
    spot = float(spot or 0)
    if spot <= 0 or not contracts:
        return None
    otm = max(0.5, float(otm_pct or 5.0)) / 100.0
    kind = str(option_type or "call").lower()
    long_leg = nearest_contract(contracts, spot)
    if long_leg is None:
        return None
    long_strike = _strike(long_leg)
    if kind == "put":
        short_target = spot * (1.0 - otm)
        short_leg = nearest_contract(
            contracts, short_target, max_strike=long_strike - 0.01
        )
    else:
        short_target = spot * (1.0 + otm)
        short_leg = nearest_contract(
            contracts, short_target, min_strike=long_strike + 0.01
        )
    if short_leg is None or _strike(short_leg) == long_strike:
        return None
    return long_leg, short_leg


def pick_long_option(
    contracts: list[dict[str, Any]], spot: float
) -> dict[str, Any] | None:
    return nearest_contract(contracts, float(spot or 0))


def pick_protective(
    contracts: list[dict[str, Any]],
    spot: float,
    *,
    option_type: str,
    otm_pct: float = 5.0,
) -> dict[str, Any] | None:
    """OTM hedge: put below spot for longs, call above spot for shorts."""
    spot = float(spot or 0)
    if spot <= 0:
        return None
    otm = max(0.5, float(otm_pct or 5.0)) / 100.0
    kind = str(option_type or "put").lower()
    if kind == "call":
        return nearest_contract(contracts, spot * (1.0 + otm), min_strike=spot)
    return nearest_contract(contracts, spot * (1.0 - otm), max_strike=spot)


def expiration_window(
    *,
    min_dte: int = 21,
    max_dte: int = 45,
    today: date | None = None,
) -> tuple[date, date]:
    today = today or date.today()
    lo = max(1, int(min_dte))
    hi = max(lo, int(max_dte))
    return today + timedelta(days=lo), today + timedelta(days=hi)
