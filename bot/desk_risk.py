"""Shared risk-engine helpers for AI, SMA/dip, pair, and manual tickets.

Config knobs stay named ``ai_*`` for persistence compatibility; any desk mode
that shows the Risk Engine UI can call these.
"""

from __future__ import annotations

import logging
from typing import Any

from alpaca.trading.enums import OrderSide

from bot.ai_risk import (
    desired_stop,
    load_trade_state,
    r_multiple,
    save_trade_state,
    should_scale_out,
)
from bot.analysis import compute_technicals

logger = logging.getLogger(__name__)


def atr_from_bars(bars: Any) -> float | None:
    """ATR14 from a bar frame, or None when TA is incomplete."""
    technicals = compute_technicals(bars)
    if not technicals.get("ok"):
        return None
    atr = technicals.get("atr_14")
    try:
        value = float(atr) if atr is not None else 0.0
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def stop_distance_for(config: Any, price: float, atr: float | None) -> float:
    """Dollar stop distance from the desk risk engine."""
    fn = getattr(config, "ai_stop_distance", None)
    if callable(fn):
        return float(fn(price, atr) or 0)
    return 0.0


def risk_qty_for(
    config: Any, price: float, stop_distance: float, equity: float
) -> float | None:
    """Shares so a stop-out costs ``ai_risk_pct`` of equity, or None when off."""
    fn = getattr(config, "ai_qty_for_risk", None)
    if callable(fn):
        return fn(price, stop_distance, equity)
    return None


def stop_pct_from_distance(price: float, stop_distance: float) -> float | None:
    """Convert a dollar stop distance into the percent ``ensure_stop_loss`` expects."""
    price = float(price or 0)
    dist = float(stop_distance or 0)
    if price <= 0 or dist <= 0:
        return None
    return dist / price * 100.0


def arm_protective_stop(
    service: Any,
    symbol: str,
    payload: dict[str, Any],
    stop_distance: float | None = None,
) -> dict[str, Any] | None:
    """Arm a resting stop; prefer ATR-derived distance when provided."""
    pct: float | None = None
    price = float(payload.get("price") or 0)
    if stop_distance and price > 0:
        pct = stop_pct_from_distance(price, float(stop_distance))
    try:
        armed = service.ensure_stop_loss(symbol, pct)
    except Exception as exc:
        logger.warning("could not arm stop loss for %s: %s", symbol, exc)
        payload["reason"] = str(payload.get("reason") or "") + f" | stop arm failed: {exc}"
        return None
    if not armed:
        return None
    logger.info(
        "Stop loss armed: %s qty=%s @ %.2f (%.2f%%)",
        symbol,
        armed["qty"],
        armed["stop_price"],
        armed["pct"],
    )
    payload["stop_loss"] = armed
    sign = "+" if armed.get("side") == "buy" else "-"
    payload["reason"] = str(payload.get("reason") or "") + (
        f" | stop @{armed['stop_price']:.2f} ({sign}{armed['pct']:.2f}%)"
    )
    return armed


def manage_open_position(
    config: Any,
    service: Any,
    symbol: str,
    *,
    side: str,
    entry: float | None,
    price: float,
    qty: float,
    stop_distance: float | None,
    current_stop: float | None,
    take_profit_r: float | None = None,
) -> dict[str, Any]:
    """Scale out at take-profit R and ratchet the stop. Long or short.

    ``take_profit_r`` overrides the desk-wide ``ai_take_profit_r`` for engines with
    their own profit target.
    """
    if side not in {"long", "short"}:
        return {}
    actions: list[str] = []
    out: dict[str, Any] = {}
    paper = bool(getattr(config, "paper", True))
    state = load_trade_state(symbol, entry, paper=paper)
    open_r = r_multiple(
        side=side, entry=entry, price=price, stop_distance=stop_distance
    )
    if open_r is not None:
        state["peak_r"] = max(float(state.get("peak_r") or 0.0), float(open_r))

    if should_scale_out(
        config,
        r=open_r,
        already_scaled=state.get("scaled_out"),
        target_r=take_profit_r,
    ):
        trimmed = _scale_out(service, symbol, qty=qty, side=side)
        if trimmed:
            state["scaled_out"] = True
            out["scale_out"] = trimmed
            actions.append(f"scaled out {trimmed['qty']:g} @ +{float(open_r):.1f}R")
            save_trade_state(symbol, state, paper=paper)
            out["actions"] = actions
            logger.info("%s | managed: %s", symbol, "; ".join(actions))
            return out

    target = desired_stop(
        config,
        side=side,
        entry=entry,
        price=price,
        stop_distance=stop_distance,
        current_stop=current_stop,
    )
    if target:
        step = max(0.01, float(stop_distance or 0) * 0.1)
        moved = (
            current_stop is None
            or abs(float(current_stop) - target["stop_price"]) >= step
        )
        if moved:
            try:
                armed = service.replace_stop_loss(symbol, target["stop_price"])
            except Exception as exc:
                logger.warning("could not move stop for %s: %s", symbol, exc)
                armed = None
            if armed:
                out["stop_moved"] = {**armed, "stage": target["stage"]}
                actions.append(f"stop {target['stage']} @{target['stop_price']:.2f}")

    save_trade_state(symbol, state, paper=paper)
    if actions:
        out["actions"] = actions
        logger.info("%s | managed: %s", symbol, "; ".join(actions))
    return out


def _scale_out(
    service: Any, symbol: str, *, qty: float, side: str
) -> dict[str, Any] | None:
    qty = abs(float(qty or 0))
    if qty <= 0:
        return None
    trim = float(int(qty / 2)) if qty >= 2 else 0.0
    if trim <= 0:
        return None
    session_info = service.market_session()
    if not session_info.get("is_open"):
        return None
    service.cancel_open_stop_orders(symbol)
    order = service.submit_order(
        symbol,
        trim,
        OrderSide.BUY if side == "short" else OrderSide.SELL,
        protect=False,
    )
    return {"id": str(order.id), "qty": trim}
