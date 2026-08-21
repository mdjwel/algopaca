"""Pre-trade gates and open-position management for the AI desk.

Everything here is a pure function over plain dicts so the rules can be tested
without an Alpaca connection. The trader wires them to live orders.

The AI desk used to have exactly one hard risk rule (a fixed-percent stop that
never moved). These rules add the missing half: how big a position may be, how
many may be open at once, when a stop moves up, and when the desk must sit out.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
# Legacy flat file — treated as the paper bucket when the mode-scoped files
# are absent so existing trail/TP scratch state keeps working after upgrade.
STATE_PATH = ROOT / ".ai_risk_state.json"
PAPER_STATE_PATH = ROOT / ".ai_risk_state.paper.json"
LIVE_STATE_PATH = ROOT / ".ai_risk_state.live.json"


def state_path_for(*, paper: bool = True) -> Path:
    return PAPER_STATE_PATH if paper else LIVE_STATE_PATH


# --- tiny JSON store: only what cannot be derived from broker state -----------


def _read_state(*, paper: bool = True) -> dict[str, Any]:
    path = state_path_for(paper=paper)
    legacy = STATE_PATH
    if not path.exists() and paper and legacy.exists():
        path = legacy
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(data: dict[str, Any], *, paper: bool = True) -> None:
    path = state_path_for(paper=paper)
    try:
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - disk issues should not halt trading
        logger.warning("could not persist AI risk state: %s", exc)


def load_trade_state(
    symbol: str, entry: float | None, *, paper: bool = True
) -> dict[str, Any]:
    """Per-symbol scratch state, reset whenever the entry price changes."""
    row = _read_state(paper=paper).get(symbol.upper()) or {}
    stored_entry = row.get("entry")
    if entry is None or stored_entry is None or abs(float(stored_entry) - float(entry)) > 0.01:
        return {"entry": entry, "scaled_out": False, "peak_r": 0.0}
    return {
        "entry": stored_entry,
        "scaled_out": bool(row.get("scaled_out")),
        "peak_r": float(row.get("peak_r") or 0.0),
    }


def save_trade_state(
    symbol: str, state: dict[str, Any], *, paper: bool = True
) -> None:
    data = _read_state(paper=paper)
    data[symbol.upper()] = state
    _write_state(data, paper=paper)


def clear_trade_state(symbol: str, *, paper: bool = True) -> None:
    data = _read_state(paper=paper)
    if data.pop(symbol.upper(), None) is not None:
        _write_state(data, paper=paper)


@dataclass(frozen=True)
class Gate:
    """Outcome of a rule check. `allowed=False` blocks the action."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.allowed


ALLOW = Gate(True)


def spread_bps(mark: dict[str, Any] | None) -> float | None:
    """Round-trip cost proxy in basis points, or None when no quote is available."""
    mark = mark or {}
    try:
        bid = float(mark.get("bid") or 0)
        ask = float(mark.get("ask") or 0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return round((ask - bid) / mid * 10_000, 1)


def r_multiple(
    *, side: str, entry: float | None, price: float | None, stop_distance: float | None
) -> float | None:
    """Open profit measured in units of initial risk. Negative = underwater."""
    try:
        entry_f = float(entry or 0)
        price_f = float(price or 0)
        dist = float(stop_distance or 0)
    except (TypeError, ValueError):
        return None
    if entry_f <= 0 or price_f <= 0 or dist <= 0:
        return None
    move = price_f - entry_f if side == "long" else entry_f - price_f
    return round(move / dist, 2)


def entry_gates(
    config: Any,
    context: dict[str, Any],
    *,
    open_positions: int,
    day_pl_pct: float | None,
) -> Gate:
    """Portfolio- and cost-level checks applied before any new position opens.

    Exits are never gated — these only block *opening* risk.
    """
    limit = float(getattr(config, "ai_daily_loss_limit_pct", 0) or 0)
    if limit > 0 and day_pl_pct is not None and day_pl_pct <= -limit:
        return Gate(
            False,
            f"Daily loss limit hit ({day_pl_pct:.2f}% <= -{limit:.2f}%) — no new risk today.",
        )

    max_pos = int(getattr(config, "ai_max_positions", 0) or 0)
    if max_pos > 0 and open_positions >= max_pos:
        return Gate(
            False,
            f"Max concurrent positions reached ({open_positions}/{max_pos}).",
        )

    max_spread = float(getattr(config, "ai_max_spread_bps", 0) or 0)
    spread = spread_bps(context.get("mark"))
    if max_spread > 0 and spread is not None and spread > max_spread:
        return Gate(
            False,
            f"Spread {spread:.1f}bps above {max_spread:.0f}bps — edge is thinner than the cost.",
        )

    cooldown = int(getattr(config, "ai_cooldown_minutes", 0) or 0)
    activity = context.get("activity") or {}
    stop_age = activity.get("stop_out_age_min")
    if cooldown > 0 and stop_age is not None and float(stop_age) < cooldown:
        return Gate(
            False,
            f"Stopped out {float(stop_age):.0f}m ago — cooling down for {cooldown}m.",
        )
    return ALLOW


def reversal_gate(config: Any, context: dict[str, Any], confidence: float) -> Gate:
    """Guards that only apply when flipping or closing an existing position.

    Stops churn: a fresh position may not be reversed on a marginal signal, and
    a reversal needs more conviction than an open did.
    """
    activity = context.get("activity") or {}
    min_hold = int(getattr(config, "ai_min_hold_minutes", 0) or 0)
    age = activity.get("last_fill_age_min")
    if min_hold > 0 and age is not None and float(age) < min_hold:
        return Gate(
            False,
            f"Position is {float(age):.0f}m old — min hold is {min_hold}m (stop still protects).",
        )

    bump = float(getattr(config, "ai_reversal_conf_bump", 0) or 0)
    needed = float(getattr(config, "ai_min_confidence", 0) or 0) + bump
    if bump > 0 and confidence < needed:
        return Gate(
            False,
            f"Reversal needs confidence >= {needed:.2f} (got {confidence:.2f}).",
        )
    return ALLOW


def desired_stop(
    config: Any,
    *,
    side: str,
    entry: float | None,
    price: float | None,
    stop_distance: float | None,
    current_stop: float | None = None,
) -> dict[str, Any] | None:
    """Where the protective stop should sit right now.

    Below `ai_trail_after_r` the initial stop stands. At or above it the stop
    jumps to breakeven and then trails one stop-distance behind price. A stop
    never moves against the position, so this only ever locks in more.
    """
    try:
        entry_f = float(entry or 0)
        price_f = float(price or 0)
        dist = float(stop_distance or 0)
    except (TypeError, ValueError):
        return None
    if entry_f <= 0 or price_f <= 0 or dist <= 0 or side not in {"long", "short"}:
        return None

    is_long = side == "long"
    initial = entry_f - dist if is_long else entry_f + dist
    target = initial
    stage = "initial"

    trail_after = float(getattr(config, "ai_trail_after_r", 0) or 0)
    r = r_multiple(side=side, entry=entry_f, price=price_f, stop_distance=dist)
    if trail_after > 0 and r is not None and r >= trail_after:
        trailed = price_f - dist if is_long else price_f + dist
        # Breakeven acts as a floor: once the trade has paid for its own risk it
        # is not allowed to become a loser again.
        target = max(entry_f, trailed) if is_long else min(entry_f, trailed)
        stage = "trailing" if (
            (is_long and trailed > entry_f) or (not is_long and trailed < entry_f)
        ) else "breakeven"

    if current_stop is not None:
        try:
            existing = float(current_stop)
        except (TypeError, ValueError):
            existing = 0.0
        if existing > 0:
            target = max(target, existing) if is_long else min(target, existing)

    target = round(target, 2)
    # Alpaca rejects a stop on the wrong side of the market.
    if is_long and target >= price_f:
        target = round(price_f - 0.01, 2)
    if not is_long and target <= price_f:
        target = round(price_f + 0.01, 2)
    if target <= 0:
        return None
    return {"stop_price": target, "stage": stage, "r": r}


def should_scale_out(config: Any, *, r: float | None, already_scaled: bool) -> bool:
    """True when the position has earned its take-profit trim and has not taken it."""
    target = float(getattr(config, "ai_take_profit_r", 0) or 0)
    if target <= 0 or already_scaled or r is None:
        return False
    return float(r) >= target


def confidence_scaled_qty(
    config: Any, qty: float, confidence: float, *, floor: float = 0.35
) -> float:
    """Shrink size toward `floor` as confidence approaches the minimum threshold.

    Self-reported LLM confidence is poorly calibrated, so it is a better size
    dial than an on/off switch.
    """
    min_conf = float(getattr(config, "ai_min_confidence", 0) or 0)
    if qty <= 0:
        return 0.0
    headroom = 1.0 - min_conf
    if headroom <= 0:
        return float(qty)
    scale = (float(confidence) - min_conf) / headroom
    scale = max(floor, min(1.0, scale))
    scaled = float(qty) * scale
    # Shorts and extended-hours orders need whole shares, so scaling a one-share
    # trade down to a fraction would silently cancel it rather than de-risk it.
    if qty >= 1 and scaled < 1:
        return 1.0
    return scaled
