"""Disk persistence for next-ticket plans that fire after a close fills.

A follow-on is a promise to open the other way (or a different name) once the
close actually fills: "when this long is gone, short at $X" / "when this short
is gone, buy at market" / "when this closes, buy MSFT at $Y". Alpaca has no
close-then-open order class, so the desk holds the plan and watches the close.

Plans are keyed to a broker account, so the file is scoped by trading mode the
same way ``bot.reinvest_store`` scopes buy-backs. A paper plan must never be
resumed against live credentials.

``placing`` is the one state a restart cannot finish: the next ticket may
already be on the wire, so it is reloaded as ``interrupted`` rather than retried.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PAPER_PLANS_PATH = ROOT / ".followon_plans.paper.json"
LIVE_PLANS_PATH = ROOT / ".followon_plans.live.json"

MAX_PLANS = 40

_PERSISTED_FIELDS = (
    "id",
    "symbol",
    "close_side",
    "close_order_id",
    "close_qty",
    "close_limit_price",
    "kind",
    "target_symbol",
    "next_side",
    "qty_mode",
    "qty",
    "order_type",
    "limit_price",
    "expire_minutes",
    "created_at",
    "created_at_iso",
    "wait_started_at",
    "expires_at",
    "status",
    "message",
    "next_order_id",
    "next_qty",
    "close_filled_qty",
    "error_count",
    "position_error_count",
    "flat_check_count",
    "settled_at_iso",
)


def plans_path_for(*, paper: bool = True) -> Path:
    return PAPER_PLANS_PATH if paper else LIVE_PLANS_PATH


def _sanitize(plan: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    plan_id = str(plan.get("id") or "").strip()
    symbol = str(plan.get("symbol") or "").upper().strip()
    close_order_id = str(plan.get("close_order_id") or "").strip()
    kind = str(plan.get("kind") or "").strip().lower()
    if not plan_id or not symbol or not close_order_id:
        return None
    if kind not in {"reverse", "rotate"}:
        return None
    out = {key: plan.get(key) for key in _PERSISTED_FIELDS if key in plan}
    out["id"] = plan_id
    out["symbol"] = symbol
    out["close_order_id"] = close_order_id
    out["kind"] = kind
    out["target_symbol"] = str(out.get("target_symbol") or symbol).upper().strip()
    out["next_side"] = str(out.get("next_side") or "buy").strip().lower()
    out["close_side"] = str(out.get("close_side") or "sell").strip().lower()
    order_type = str(out.get("order_type") or "limit").strip().lower()
    if order_type not in {"market", "limit"}:
        order_type = "limit"
    out["order_type"] = order_type
    try:
        if order_type == "limit":
            if float(out.get("limit_price") or 0) <= 0:
                return None
        else:
            out["limit_price"] = None
        expires_raw = out.get("expires_at")
        if expires_raw in (None, "") or float(expires_raw) <= 0:
            out["expires_at"] = None
        else:
            out["expires_at"] = float(expires_raw)
        wait_raw = out.get("wait_started_at")
        if wait_raw in (None, ""):
            out["wait_started_at"] = None
        else:
            out["wait_started_at"] = float(wait_raw)
    except (TypeError, ValueError):
        return None
    out.setdefault("error_count", 0)
    out.setdefault("position_error_count", 0)
    out.setdefault("flat_check_count", 0)
    return out


def save_plans(
    plans: dict[str, dict[str, Any]],
    *,
    paper: bool = True,
    path: Path | None = None,
) -> None:
    """Write the ledger. Never raises — a full disk must not kill a ticket."""
    if path is None:
        path = plans_path_for(paper=paper)
    rows = []
    for plan in (plans or {}).values():
        clean = _sanitize(plan)
        if clean is not None:
            rows.append(clean)
    rows.sort(key=lambda p: float(p.get("created_at") or 0.0), reverse=True)
    # A cap is fine for settled history, never for promises that can still
    # place an order. Persist every active plan even if an unusually busy desk
    # has more than MAX_PLANS of them at once.
    active = [p for p in rows if p.get("status") in {"waiting", "placing"}]
    settled = [p for p in rows if p.get("status") not in {"waiting", "placing"}]
    keep_settled = max(0, MAX_PLANS - len(active))
    rows = active + settled[:keep_settled]
    rows.sort(key=lambda p: float(p.get("created_at") or 0.0), reverse=True)
    try:
        path.write_text(
            json.dumps(rows, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - disk issues must not halt trading
        logger.warning("could not persist follow-on plans: %s", exc)


def load_plans(
    *, paper: bool = True, path: Path | None = None
) -> dict[str, dict[str, Any]]:
    """Read the ledger back, downgrading states that a restart invalidated."""
    if path is None:
        path = plans_path_for(paper=paper)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("follow-on plan file unreadable — starting empty")
        return {}
    if not isinstance(raw, list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for row in raw:
        plan = _sanitize(row)
        if plan is None:
            continue
        status = str(plan.get("status") or "").lower()
        if status == "placing":
            plan["status"] = "interrupted"
            plan["message"] = (
                "The desk restarted while this next ticket was being sent — "
                "check Positions to see whether it landed."
            )
        elif status not in {
            "waiting",
            "placed",
            "expired",
            "failed",
            "cancelled",
            "interrupted",
        }:
            plan["status"] = "cancelled"
            plan["message"] = "Dropped on restart — the plan state was unreadable."
        out[str(plan["id"])] = plan
    return out


def max_sequence(plans: dict[str, dict[str, Any]]) -> int:
    """Highest ``fo-N`` counter in a ledger, so ids stay unique after a restart."""
    highest = 0
    for plan_id in plans or {}:
        _, _, tail = str(plan_id).partition("-")
        if tail.isdigit():
            highest = max(highest, int(tail))
    return highest
