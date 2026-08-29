"""Disk persistence for auto-trade orders pending manual approval.

When 'require_approval' is enabled, automated strategy cycles (SMA, Dip,
Pair, LS, AI) do not dispatch orders straight to Alpaca. Instead, actionable
signals are staged as approval records so the trader can confirm or reject
them from the desk or via email notification links.

Approvals live in the caller's workspace directory, split by trading mode
(paper vs live) the same way reinvest/follow-on plans are — a ticket staged
against one account must never surface on another's desk.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PAPER_APPROVALS_NAME = ".approvals.paper.json"
LIVE_APPROVALS_NAME = ".approvals.live.json"

MAX_APPROVALS = 100

_VALID_SIDES = {"buy", "sell", "short", "cover"}
_VALID_STATUS = {"pending", "approved", "rejected", "expired"}

# Everything the approve path needs to rebuild the original order after a
# restart. `protect` and `stop_price` in particular decide whether Alpaca
# attaches an OTO stop leg — dropping them turns an approved COVER into a
# buy that arms a sell-stop on a flat position.
_PERSISTED_FIELDS = (
    "id",
    "symbol",
    "side",
    "action",
    "qty",
    "price",
    "estimated_value",
    "stop_price",
    "stop_distance",
    "take_profit",
    "protect",
    "cancel_stops",
    "order_type",
    "engine",
    "strategy_mode",
    "environment",
    "intent",
    "reason",
    "thesis",
    "confidence",
    "status",
    "order_id",
    "created_at",
    "created_ts",
    "approved_at",
    "rejected_at",
    "resolved_at_iso",
    "resolution_note",
)


def approvals_path_for(workspace_dir: Path, *, paper: bool = True) -> Path:
    """Approvals file for one workspace and trading mode."""
    name = PAPER_APPROVALS_NAME if paper else LIVE_APPROVALS_NAME
    return Path(workspace_dir) / name


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sanitize_approval(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    appr_id = str(item.get("id") or "").strip()
    symbol = str(item.get("symbol") or "").upper().strip()
    side = str(item.get("side") or item.get("action") or "buy").lower().strip()
    if not appr_id or not symbol:
        return None
    if side not in _VALID_SIDES:
        side = "buy"

    out: dict[str, Any] = {k: item.get(k) for k in _PERSISTED_FIELDS if k in item}
    out["id"] = appr_id
    out["symbol"] = symbol
    out["side"] = side
    out["action"] = side.upper()

    qty = _coerce_float(out.get("qty"), 1.0) or 1.0
    out["qty"] = qty if qty > 0 else 1.0

    price = _coerce_float(out.get("price"), 0.0) or 0.0
    out["price"] = price if price >= 0 else 0.0

    est = _coerce_float(out.get("estimated_value"))
    out["estimated_value"] = (
        est if est is not None else round(out["qty"] * out["price"], 2) or None
    )

    out["stop_price"] = _coerce_float(out.get("stop_price"))
    out["stop_distance"] = _coerce_float(out.get("stop_distance"))
    out["take_profit"] = _coerce_float(out.get("take_profit"))
    out["confidence"] = _coerce_float(out.get("confidence"))

    # Tri-state: None means "let submit_order apply its legacy buy-only rule".
    protect = out.get("protect")
    out["protect"] = None if protect is None else bool(protect)
    out["cancel_stops"] = bool(out.get("cancel_stops"))

    status = str(out.get("status") or "pending").lower().strip()
    out["status"] = status if status in _VALID_STATUS else "pending"

    out["engine"] = str(out.get("engine") or "auto").lower().strip()
    out["strategy_mode"] = str(out.get("strategy_mode") or "").lower().strip()
    out["environment"] = str(out.get("environment") or "").lower().strip()
    out["intent"] = str(out.get("intent") or "").strip()
    out["reason"] = str(out.get("reason") or "").strip()
    out["thesis"] = str(out.get("thesis") or "").strip()
    out["order_id"] = str(out.get("order_id") or "").strip() or None

    # `created_ts` sorts; `created_at` is the ISO string the UI parses. Keep
    # them in step so a reload does not restamp every ticket with "just now".
    created_ts = _coerce_float(out.get("created_ts"))
    created_at = out.get("created_at")
    if created_ts is None and isinstance(created_at, (int, float)):
        created_ts = float(created_at)
        created_at = None
    if created_ts is None and isinstance(created_at, str) and created_at.strip():
        try:
            created_ts = datetime.fromisoformat(created_at).timestamp()
        except ValueError:
            created_ts = None
    if created_ts is None:
        created_ts = time.time()
    out["created_ts"] = created_ts
    if not isinstance(created_at, str) or not created_at.strip():
        created_at = datetime.fromtimestamp(created_ts, timezone.utc).isoformat()
    out["created_at"] = created_at

    return out


def load_approvals(path: Path) -> dict[str, dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read approvals file %s: %s", target, exc)
        return {}
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        sanitized = _sanitize_approval(item)
        if sanitized:
            out[sanitized["id"]] = sanitized
    return out


def save_approvals(approvals: dict[str, dict[str, Any]], path: Path) -> Path:
    target = Path(path)
    clean: list[dict[str, Any]] = []
    for item in approvals.values():
        sanitized = _sanitize_approval(item)
        if sanitized:
            clean.append(sanitized)
    clean.sort(key=lambda p: p.get("created_ts") or 0.0, reverse=True)
    clean = clean[:MAX_APPROVALS]

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save approvals file %s: %s", target, exc)
    return target
