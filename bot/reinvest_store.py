"""Disk persistence for armed buy-back plans.

A re-investment plan is a promise to spend money later: "when this sell fills,
put the cash back to work at $X". Holding that promise only in memory meant a
restart silently dropped it — the sell still filled, the buy never came, and
nothing on the page ever said so. Worse, the user had no way to tell an expired
plan from one that was simply forgotten.

Plans are keyed to a broker account, so the file is scoped by trading mode the
same way ``bot.ai_risk`` scopes its scratch state. A paper plan must never be
resumed against live credentials.

The wait clock starts when the sell fills and the buy-back is sent, not when
the sell was placed. ``expires_at`` is therefore None until that fill.

Three states can only be resolved by reading the broker, not the file:

* ``waiting`` — the sell had not filled yet. Safe to resume: the watcher
  re-reads the sell order and carries on where it left off.
* ``awaiting_fill`` — the buy-back is resting at the broker. Safe to resume:
  the watcher re-reads that order and cancels it if the wait expires unfilled.
* ``placing`` — the buy was already on the wire when the process died. The
  desk cannot know whether it landed, so it is reloaded as ``interrupted``
  with a sentence telling the user to check Positions. Silently retrying could
  buy the shares twice.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PAPER_PLANS_PATH = ROOT / ".reinvest_plans.paper.json"
LIVE_PLANS_PATH = ROOT / ".reinvest_plans.live.json"

# Matches the in-memory trim so a restart cannot grow the ledger.
MAX_PLANS = 40

# Everything a plan needs to be resumed. Derived/transient fields (seconds_left)
# are recomputed on read and deliberately not written.
_PERSISTED_FIELDS = (
    "id",
    "symbol",
    "sell_order_id",
    "sell_qty",
    "sell_limit_price",
    "qty_mode",
    "qty",
    "limit_price",
    "expire_minutes",
    "created_at",
    "created_at_iso",
    "wait_started_at",
    "expires_at",
    "status",
    "message",
    "buy_order_id",
    "buy_qty",
    "sell_filled_qty",
    "settled_at_iso",
    "error_count",
)


def plans_path_for(*, paper: bool = True) -> Path:
    return PAPER_PLANS_PATH if paper else LIVE_PLANS_PATH


def _sanitize(plan: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    plan_id = str(plan.get("id") or "").strip()
    symbol = str(plan.get("symbol") or "").upper().strip()
    sell_order_id = str(plan.get("sell_order_id") or "").strip()
    if not plan_id or not symbol or not sell_order_id:
        return None
    out = {key: plan.get(key) for key in _PERSISTED_FIELDS if key in plan}
    out["id"] = plan_id
    out["symbol"] = symbol
    out["sell_order_id"] = sell_order_id
    # A plan with no usable price cannot be resumed into a real order.
    try:
        if float(out.get("limit_price") or 0) <= 0:
            return None
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
    return out


def save_plans(plans: dict[str, dict[str, Any]], *, paper: bool = True) -> None:
    """Write the ledger. Never raises — a full disk must not kill a ticket."""
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
    active = [
        p for p in rows if p.get("status") in {"waiting", "placing", "awaiting_fill"}
    ]
    settled = [
        p for p in rows if p.get("status") not in {"waiting", "placing", "awaiting_fill"}
    ]
    keep_settled = max(0, MAX_PLANS - len(active))
    rows = active + settled[:keep_settled]
    rows.sort(key=lambda p: float(p.get("created_at") or 0.0), reverse=True)
    try:
        path.write_text(
            json.dumps(rows, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - disk issues must not halt trading
        logger.warning("could not persist re-investment plans: %s", exc)


def load_plans(*, paper: bool = True) -> dict[str, dict[str, Any]]:
    """Read the ledger back, downgrading states that a restart invalidated."""
    path = plans_path_for(paper=paper)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("re-investment plan file unreadable — starting empty")
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
                "The desk restarted while this buy-back was being sent — "
                "check Positions to see whether it landed."
            )
        elif status not in {
            "waiting",
            "awaiting_fill",
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
    """Highest ``ri-N`` counter in a ledger, so ids stay unique after a restart."""
    highest = 0
    for plan_id in plans or {}:
        _, _, tail = str(plan_id).partition("-")
        if tail.isdigit():
            highest = max(highest, int(tail))
    return highest
