"""Disk persistence for synthetic extended-hours orders (Stop-Limit & Trailing Stop).

Alpaca and US equity exchanges reject Stop and Stop-Limit orders during extended
hours (Pre-market, After-hours, 24h Overnight) because native stop triggers are
not supported outside regular trading hours.

The Synthetic Order Engine maintains these orders in local disk storage and
monitors quotes. When the stop trigger or trailing threshold is reached during
extended hours, it executes an eligible Limit order with ``extended_hours=True``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MAX_PLANS = 100

_ET = ZoneInfo("America/New_York")

_PERSISTED_FIELDS = (
    "id",
    "client_order_id",
    "user_id",
    "symbol",
    "side",
    "qty",
    "order_type",
    "time_in_force",
    "stop_price",
    "limit_price",
    "trail_percent",
    "trail_price",
    "high_water_mark",
    "low_water_mark",
    "extended_hours",
    "status",
    "message",
    "alpaca_order_id",
    "created_at",
    "created_at_iso",
    "triggered_at",
    "settled_at_iso",
    "error_count",
)

ACTIVE_STATUSES = frozenset({"waiting", "triggered"})
VALID_STATUSES = frozenset(
    {"waiting", "triggered", "filled", "canceled", "cancelled", "expired", "rejected", "failed"}
)


def is_day_order_expired(created_at: float, now_dt: datetime | None = None) -> bool:
    """Check if a synthetic DAY order has expired past the 8:00 PM ET close.
    
    In US equity markets, extended-hours trading closes at 8:00 PM (20:00) ET on
    weekdays. Orders placed after 8:00 PM ET or over the weekend belong to the
    next trading day and expire at 8:00 PM ET on that trading day.
    """
    if not created_at or created_at <= 0:
        return False
    created_et = datetime.fromtimestamp(created_at, tz=_ET)
    now_et = now_dt if now_dt is not None else datetime.now(_ET)

    # Cutoff milestone is 20:00 (8:00 PM) ET on the session date
    milestone = created_et.replace(hour=20, minute=0, second=0, microsecond=0)
    if created_et >= milestone:
        milestone += timedelta(days=1)
    # If milestone lands on Saturday (5) or Sunday (6), roll to Monday:
    while milestone.weekday() in (5, 6):
        milestone += timedelta(days=1)

    return now_et >= milestone


def plans_path_for(workspace_dir: Path | None = None, *, paper: bool = True) -> Path:
    base = Path(workspace_dir) if workspace_dir else ROOT
    filename = ".synthetic_orders.paper.json" if paper else ".synthetic_orders.live.json"
    return base / filename


def _sanitize(order: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(order, dict):
        return None
    order_id = str(order.get("id") or "").strip()
    symbol = str(order.get("symbol") or "").upper().strip()
    side = str(order.get("side") or "").strip().lower()
    otype = str(order.get("order_type") or "").strip().lower()

    if not order_id or not symbol:
        return None
    if side not in {"buy", "sell"}:
        return None
    if otype not in {"stop_limit", "trailing_stop"}:
        return None

    out = {key: order.get(key) for key in _PERSISTED_FIELDS if key in order}
    out["id"] = order_id
    out["symbol"] = symbol
    out["side"] = side
    out["order_type"] = otype

    try:
        qty = float(out.get("qty") or 0)
        if qty <= 0:
            return None
        out["qty"] = qty
        out["created_at"] = float(out.get("created_at") or 0)

        if otype == "stop_limit":
            stop_price = float(out.get("stop_price") or 0)
            limit_price = float(out.get("limit_price") or 0)
            if stop_price <= 0 or limit_price <= 0:
                return None
            out["stop_price"] = stop_price
            out["limit_price"] = limit_price
        elif otype == "trailing_stop":
            trail_pct = out.get("trail_percent")
            trail_px = out.get("trail_price")
            if trail_pct is not None and float(trail_pct) > 0:
                out["trail_percent"] = float(trail_pct)
            elif trail_px is not None and float(trail_px) > 0:
                out["trail_price"] = float(trail_px)
            else:
                return None
            if out.get("high_water_mark") is not None:
                out["high_water_mark"] = float(out["high_water_mark"])
            if out.get("low_water_mark") is not None:
                out["low_water_mark"] = float(out["low_water_mark"])
            if out.get("stop_price") is not None:
                out["stop_price"] = float(out["stop_price"])
            if out.get("limit_price") is not None:
                out["limit_price"] = float(out["limit_price"])

        tif = str(out.get("time_in_force") or "day").strip().lower()
        out["time_in_force"] = tif if tif in {"day", "gtc"} else "day"
        out["extended_hours"] = True
    except (TypeError, ValueError):
        return None

    raw_status = str(out.get("status") or "waiting").strip().lower()
    out["status"] = raw_status if raw_status in VALID_STATUSES else "waiting"
    out.setdefault("error_count", 0)
    return out


def save_orders(
    orders: dict[str, dict[str, Any]],
    workspace_dir: Path | None = None,
    *,
    paper: bool = True,
) -> None:
    """Write synthetic orders to disk ledger safely."""
    path = plans_path_for(workspace_dir, paper=paper)
    rows: list[dict[str, Any]] = []
    for order in (orders or {}).values():
        clean = _sanitize(order)
        if clean is not None:
            rows.append(clean)
    rows.sort(key=lambda o: float(o.get("created_at") or 0.0), reverse=True)
    rows = rows[:MAX_PLANS]

    tmp = path.with_suffix(f"{path.suffix}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.warning("could not persist synthetic orders to %s: %s", path, exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def load_orders(
    workspace_dir: Path | None = None,
    *,
    paper: bool = True,
) -> dict[str, dict[str, Any]]:
    """Read the synthetic orders ledger from disk."""
    path = plans_path_for(workspace_dir, paper=paper)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("could not read synthetic orders file %s: %s", path, exc)
        return {}

    if not isinstance(raw, list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        clean = _sanitize(item)
        if clean is not None:
            out[clean["id"]] = clean
    return out
