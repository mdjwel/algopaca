"""Pre-trade guards and portfolio heat for hand-typed tickets.

Auto Trade runs every entry through :func:`bot.ai_risk.entry_gates`. A manual
ticket ran through nothing, so a desk that had already hit its daily loss limit
could still be traded by hand all afternoon, and a user sizing each ticket at a
tidy 0.5% could hold twelve of them without the page ever saying 6%.

Two differences from the AI path, both deliberate:

* A manual breach is **advisory**. The person is looking at the screen and can
  see something the rules cannot, so the ticket reports every breach and asks
  for an explicit override instead of refusing.
* Every breach is collected, not just the first. The AI only needs to know
  whether it may act; a human needs the whole picture before overriding it.

Pure functions over plain dicts — no Alpaca connection, so the arithmetic is
testable on its own.
"""

from __future__ import annotations

from typing import Any

from bot.ai_risk import spread_bps

__all__ = [
    "portfolio_heat",
    "manual_entry_breaches",
    "position_open_risk",
]

# Heat is only meaningful against a live stop; these are the resting order
# types that actually take a position off the book.
_STOP_TYPES = ("stop", "stop_limit", "trailing_stop")


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # drop NaN


def _protective_stop_price(orders: list[dict[str, Any]] | None, side: str) -> float | None:
    """Best resting stop for a position, or None when it is unprotected.

    "Best" means the one that fires first: the highest stop under a long, the
    lowest stop over a short. A position with two stops is over-protected, not
    unprotected, so the nearer one is the honest number.
    """
    prices: list[float] = []
    for order in orders or []:
        if not order.get("is_stop") and str(order.get("type") or "") not in _STOP_TYPES:
            continue
        price = _f(order.get("stop_price"))
        if price is not None and price > 0:
            prices.append(price)
    if not prices:
        return None
    return max(prices) if side == "long" else min(prices)


def position_open_risk(
    position: dict[str, Any], orders: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """What this one position still costs if its stop fills from here.

    Measured from the *current* price, not the entry: money already lost is
    gone and money already made is not at risk in the same way. A stop above a
    long's price (a trailing stop in profit) is locked-in gain, so its risk
    floors at zero rather than going negative and flattering the total.
    """
    symbol = str(position.get("symbol") or "").upper()
    qty = abs(_f(position.get("qty")) or 0.0)
    side = str(position.get("side") or "").lower()
    price = _f(position.get("current_price")) or _f(position.get("avg_entry_price")) or 0.0
    market_value = abs(_f(position.get("market_value")) or (qty * price))
    stop = _protective_stop_price(orders, side)

    if stop is None or qty <= 0 or price <= 0:
        return {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "stop_price": None,
            "protected": False,
            "risk": None,
            "market_value": market_value,
        }

    distance = (price - stop) if side != "short" else (stop - price)
    risk = max(0.0, distance) * qty
    return {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "stop_price": stop,
        "protected": True,
        "risk": round(risk, 2),
        "market_value": market_value,
    }


def portfolio_heat(
    positions: list[dict[str, Any]] | None,
    open_orders: dict[str, list[dict[str, Any]]] | None,
    equity: float | None,
) -> dict[str, Any]:
    """Total open risk across the book — the number a per-ticket % cannot show.

    Sizing every ticket at 0.5% is only 0.5% if you hold one. This sums what
    every resting stop would actually cost and reports it against equity, plus
    the positions carrying no stop at all (whose downside is undefined, so they
    are counted by exposure and listed by name rather than folded into the
    risk total where they would look bounded).
    """
    orders = open_orders or {}
    rows = [
        position_open_risk(pos, orders.get(str(pos.get("symbol") or "").upper()))
        for pos in (positions or [])
        if abs(_f(pos.get("qty")) or 0.0) > 0
    ]
    protected = [r for r in rows if r["protected"]]
    naked = [r for r in rows if not r["protected"]]

    defined_risk = round(sum(float(r["risk"] or 0.0) for r in protected), 2)
    unprotected_value = round(sum(float(r["market_value"] or 0.0) for r in naked), 2)
    equity_f = _f(equity) or 0.0

    return {
        "positions": len(rows),
        "protected": len(protected),
        "unprotected": [r["symbol"] for r in naked if r["symbol"]],
        "symbols": [r["symbol"] for r in rows if r["symbol"]],
        "open_risk": defined_risk,
        "open_risk_pct": (
            round(defined_risk / equity_f * 100, 3) if equity_f > 0 else None
        ),
        "unprotected_value": unprotected_value,
        "unprotected_value_pct": (
            round(unprotected_value / equity_f * 100, 3) if equity_f > 0 else None
        ),
        "rows": rows,
    }


def manual_entry_breaches(
    config: Any,
    *,
    mark: dict[str, Any] | None = None,
    open_positions: int = 0,
    day_pl_pct: float | None = None,
    activity: dict[str, Any] | None = None,
    adding_to_position: bool = False,
    heat: dict[str, Any] | None = None,
    ticket_risk: float | None = None,
    equity: float | None = None,
) -> list[dict[str, Any]]:
    """Every desk limit this ticket would cross, as `{code, message}` rows.

    An empty list means the ticket is inside every limit. Exits are never
    checked — these gates only ever stand between the user and *new* risk.
    """
    breaches: list[dict[str, Any]] = []

    def _num(name: str) -> float:
        try:
            return float(getattr(config, name, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    limit = _num("ai_daily_loss_limit_pct")
    if limit > 0 and day_pl_pct is not None and float(day_pl_pct) <= -limit:
        breaches.append(
            {
                "code": "daily_loss",
                "params": {
                    "actual": f"{float(day_pl_pct):.2f}",
                    "limit": f"{limit:.2f}",
                },
                "message": (
                    f"Daily loss limit hit ({float(day_pl_pct):.2f}% ≤ −{limit:.2f}%) — "
                    "the desk would stop taking new risk today."
                ),
            }
        )

    max_pos = int(_num("ai_max_positions"))
    # Adding to a symbol already held opens no new slot, so the count only
    # matters when this ticket would be a position the book does not have.
    if max_pos > 0 and not adding_to_position and int(open_positions) >= max_pos:
        breaches.append(
            {
                "code": "max_positions",
                "params": {
                    "current": int(open_positions),
                    "limit": max_pos,
                },
                "message": (
                    f"Max concurrent positions reached ({int(open_positions)}/{max_pos})."
                ),
            }
        )

    max_spread = _num("ai_max_spread_bps")
    spread = spread_bps(mark)
    if max_spread > 0 and spread is not None and spread > max_spread:
        breaches.append(
            {
                "code": "spread",
                "params": {
                    "actual": f"{spread:.1f}",
                    "limit": f"{max_spread:.0f}",
                },
                "message": (
                    f"Spread {spread:.1f} bps is above the desk limit of "
                    f"{max_spread:.0f} bps — the cost eats the edge."
                ),
            }
        )

    cooldown = int(_num("ai_cooldown_minutes"))
    stop_age = (activity or {}).get("stop_out_age_min")
    if cooldown > 0 and stop_age is not None:
        try:
            age = float(stop_age)
        except (TypeError, ValueError):
            age = None
        if age is not None and age < cooldown:
            breaches.append(
                {
                    "code": "cooldown",
                    "params": {
                        "age": f"{age:.0f}",
                        "limit": cooldown,
                    },
                    "message": (
                        f"Stopped out {age:.0f}m ago — the desk cooldown is {cooldown}m."
                    ),
                }
            )

    # Portfolio heat has no dedicated knob: the desk's stated appetite is
    # `ai_risk_pct` per trade across at most `ai_max_positions` trades, so that
    # product is the budget the book is measured against.
    per_trade = _num("ai_risk_pct")
    equity_f = _f(equity) or 0.0
    if per_trade > 0 and max_pos > 0 and heat and equity_f > 0:
        budget_pct = per_trade * max_pos
        current = _f(heat.get("open_risk")) or 0.0
        projected_pct = (current + (_f(ticket_risk) or 0.0)) / equity_f * 100
        if projected_pct > budget_pct + 1e-9:
            breaches.append(
                {
                    "code": "portfolio_heat",
                    "params": {
                        "projected": f"{projected_pct:.2f}",
                        "budget": f"{budget_pct:.2f}",
                        "per_trade": f"{per_trade:g}",
                        "positions": max_pos,
                    },
                    "message": (
                        f"Open risk would reach {projected_pct:.2f}% of equity, past the "
                        f"desk budget of {budget_pct:.2f}% "
                        f"({per_trade:g}% × {max_pos} positions)."
                    ),
                }
            )

    return breaches
