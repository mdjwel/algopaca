"""Disk persistence for buy-the-dip-after-stop plans.

A dip hunt is a standing instruction: "when this long's stop fills, buy back
cheaper." Holding that only in memory meant a restart silently dropped it —
the stop still filled, the cheaper buy never came.

Plans are keyed to a broker account, so the file is scoped by trading mode the
same way ``bot.reinvest_store`` scopes buy-backs. A paper hunt must never be
resumed against live credentials.

``placing`` is the one state a restart cannot finish: the buy may already be
on the wire, so it is reloaded as ``interrupted`` rather than retried.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from bot.dip_hunt import ACTIVE_STATUSES

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PAPER_PLANS_PATH = ROOT / ".dip_hunt_plans.paper.json"
LIVE_PLANS_PATH = ROOT / ".dip_hunt_plans.live.json"

MAX_PLANS = 40

_PERSISTED_FIELDS = (
    "id",
    "symbol",
    "buy_order_id",
    "stop_order_id",
    "take_profit_order_id",
    "qty",
    "stop_loss_pct",
    "take_profit_r",
    "wait_minutes",
    "dip_pct",
    "cycle",
    "created_at",
    "created_at_iso",
    "status",
    "message",
    "stop_fill_price",
    "target_price",
    "hunt_started_at",
    "lowest_price",
    "dip_buy_order_id",
    "dip_buy_qty",
    "settled_at_iso",
)


def plans_path_for(*, paper: bool = True) -> Path:
    return PAPER_PLANS_PATH if paper else LIVE_PLANS_PATH


def _sanitize(plan: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    plan_id = str(plan.get("id") or "").strip()
    symbol = str(plan.get("symbol") or "").upper().strip()
    if not plan_id or not symbol:
        return None
    out = {key: plan.get(key) for key in _PERSISTED_FIELDS if key in plan}
    out["id"] = plan_id
    out["symbol"] = symbol
    try:
        wait = float(out.get("wait_minutes") or 0)
        dip = float(out.get("dip_pct") or 0)
        if wait <= 0 or dip <= 0:
            return None
        out["wait_minutes"] = wait
        out["dip_pct"] = dip
        out["qty"] = float(out.get("qty") or 0)
        out["stop_loss_pct"] = float(out.get("stop_loss_pct") or 0)
        out["take_profit_r"] = float(out.get("take_profit_r") or 0)
        out["cycle"] = int(out.get("cycle") or 1)
        out["created_at"] = float(out.get("created_at") or 0)
    except (TypeError, ValueError):
        return None
    if out["qty"] <= 0 or out["stop_loss_pct"] <= 0:
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
    try:
        path.write_text(
            json.dumps(rows[:MAX_PLANS], indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - disk issues must not halt trading
        logger.warning("could not persist dip-hunt plans: %s", exc)


def load_plans(*, paper: bool = True) -> dict[str, dict[str, Any]]:
    """Read the ledger back, downgrading states that a restart invalidated."""
    path = plans_path_for(paper=paper)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("dip-hunt plan file unreadable — starting empty")
        return {}
    if not isinstance(raw, list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for row in raw[:MAX_PLANS]:
        plan = _sanitize(row)
        if plan is None:
            continue
        status = str(plan.get("status") or "").lower()
        if status == "placing":
            plan["status"] = "interrupted"
            plan["message"] = (
                "The desk restarted while this dip buy was being sent — "
                "check Positions to see whether it landed."
            )
        elif status not in ACTIVE_STATUSES | {
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
    """Highest ``dh-N`` counter in a ledger, so ids stay unique after a restart."""
    highest = 0
    for plan_id in plans or {}:
        _, _, tail = str(plan_id).partition("-")
        if tail.isdigit():
            highest = max(highest, int(tail))
    return highest
