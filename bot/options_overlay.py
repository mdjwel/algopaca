"""Mechanical options overlay shared by SMA, dip, pair, LS, and AI.

Every strategy cycle maps its equity view onto an Alpaca options trade:

* ``vertical`` (default) — defined-risk debit spread (bull call / bear put)
* ``long_option`` — long ATM call or put
* ``hedge`` — protective put/call, plus a covered call when shares >= 100

Failures never abort the equity engine — they attach ``options`` on the payload.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from bot.options_chain import (
    dte,
    expiration_window,
    is_occ_symbol,
    normalize_options_style,
    occ_root,
    option_label,
    parse_occ,
    pick_expiration,
    pick_long_option,
    pick_protective,
    pick_vertical,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PAPER_STATE_PATH = ROOT / ".options_overlay.paper.json"
LIVE_STATE_PATH = ROOT / ".options_overlay.live.json"


def state_path_for(*, paper: bool = True) -> Path:
    return PAPER_STATE_PATH if paper else LIVE_STATE_PATH


def _read_state(*, paper: bool = True) -> dict[str, Any]:
    path = state_path_for(paper=paper)
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
    except OSError as exc:  # pragma: no cover - disk must not halt trading
        logger.warning("could not persist options overlay state: %s", exc)


def load_overlay_state(symbol: str, *, paper: bool = True) -> dict[str, Any]:
    row = _read_state(paper=paper).get(str(symbol or "").upper()) or {}
    return row if isinstance(row, dict) else {}


def save_overlay_state(
    symbol: str, state: dict[str, Any] | None, *, paper: bool = True
) -> None:
    data = _read_state(paper=paper)
    key = str(symbol or "").upper()
    if not key:
        return
    if state is None:
        data.pop(key, None)
    else:
        data[key] = state
    _write_state(data, paper=paper)


def desired_overlay_side(payload: dict[str, Any]) -> str:
    """``long``, ``short``, or ``flat`` from a strategy cycle payload."""
    intent = str(payload.get("intent") or "").strip().lower()
    if intent in {"open_long"}:
        return "long"
    if intent in {"open_short"}:
        return "short"
    if intent in {"close_long", "cover"}:
        return "flat"

    ls_side = str(payload.get("ls_side") or "").strip().lower()
    if ls_side in {"long", "short"}:
        try:
            qty = float(payload.get("position") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty != 0:
            return "long" if qty > 0 else "short"
        if intent or payload.get("order_id"):
            return ls_side

    try:
        qty = float(payload.get("position") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    if qty > 0:
        return "long"
    if qty < 0:
        return "short"
    return "flat"


_RTH_SESSIONS = frozenset({"regular", "open", "rth"})
_BLOCKED_SESSIONS = frozenset(
    {"closed", "premarket", "afterhours", "overnight", "extended", "night"}
)


def _options_session_blocked(
    payload: dict[str, Any], service: Any, session_info: dict[str, Any] | None = None
) -> bool:
    """Alpaca options are RTH-only — skip extended hours and overnight."""
    is_open = payload.get("is_open")
    session = str(payload.get("session") or "").strip().lower()
    if is_open is True or session in _RTH_SESSIONS:
        return False
    if is_open is False or session in _BLOCKED_SESSIONS:
        return True
    info = session_info
    if info is None:
        try:
            info = service.market_session() or {}
        except Exception:
            return True
    if bool(info.get("is_open")):
        return False
    return str(info.get("session") or "").strip().lower() not in _RTH_SESSIONS


def _spot(payload: dict[str, Any], service: Any, symbol: str) -> float:
    payload_sym = str(payload.get("symbol") or "").strip().upper()
    try:
        price = float(payload.get("price") or payload.get("bar_close") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price > 0 and payload_sym == str(symbol or "").strip().upper():
        return price
    try:
        mark = service.get_mark_price(symbol)
        return float((mark or {}).get("price") or 0)
    except Exception:
        return 0.0


def _existing_options(
    service: Any, underlying: str, snapshot: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    root = occ_root(underlying)
    if not root:
        return []
    if snapshot is None:
        return service.option_positions_for_underlying(underlying)
    out: list[dict[str, Any]] = []
    for pos in snapshot:
        parsed = parse_occ(pos.get("symbol"))
        if not pos.get("is_option") and not parsed:
            continue
        pos_root = occ_root(pos.get("option_root") or (parsed or {}).get("root"))
        if pos_root == root:
            out.append(pos)
    return out


def _order_id(order: Any) -> str:
    if order is None:
        return ""
    if isinstance(order, dict):
        return str(order.get("order_id") or order.get("id") or "")
    oid = getattr(order, "id", None) or getattr(order, "order_id", None)
    if oid:
        return str(oid)
    getter = getattr(order, "get", None)
    if callable(getter):
        return str(getter("order_id") or getter("id") or "")
    return ""


def _equity(service: Any) -> float:
    try:
        return float((service.account_summary() or {}).get("equity") or 0)
    except Exception:
        return 0.0


def _exp_key(value: Any) -> str:
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()[:10]
    return str(value or "")[:10]


def _contracts_on_expiry(
    chain: list[dict[str, Any]], expiration: Any, option_type: str
) -> list[dict[str, Any]]:
    wanted = str(option_type or "").lower()
    want_exp = _exp_key(expiration)
    out: list[dict[str, Any]] = []
    for row in chain:
        kind = str(row.get("type") or "").lower()
        if wanted and kind != wanted:
            continue
        if _exp_key(row.get("expiration")) != want_exp:
            continue
        out.append(row)
    return out


def _leg_payload(contract: dict[str, Any], *, role: str, side: str) -> dict[str, Any]:
    symbol = str(contract.get("symbol") or "")
    return {
        "symbol": symbol,
        "label": option_label(symbol),
        "role": role,
        "side": side,
        "type": contract.get("type"),
        "strike": contract.get("strike"),
        "expiration": str(contract.get("expiration") or ""),
        "dte": dte(contract.get("expiration")),
    }


def _append_action(payload: dict[str, Any], action: dict[str, Any]) -> None:
    actions = payload.get("actions")
    if not isinstance(actions, list):
        payload["actions"] = [action]
        return
    actions.append(action)


def apply_options_overlay(
    config: Any,
    service: Any,
    payload: dict[str, Any],
    *,
    positions: list[dict[str, Any]] | None = None,
    session_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open, hold, or flatten the options overlay for ``payload['symbol']``."""
    if payload.get("error") and not payload.get("intent") and not payload.get("order_id"):
        return payload
    if not bool(getattr(config, "options_enabled", True)):
        payload["options"] = {"skipped": "disabled"}
        return payload
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol or is_occ_symbol(symbol):
        return payload

    desired = desired_overlay_side(payload)
    style = normalize_options_style(getattr(config, "options_style", "vertical"))
    paper = bool(getattr(config, "paper", True))
    result: dict[str, Any] = {
        "style": style,
        "desired": desired,
        "symbol": symbol,
    }
    payload["options"] = result

    if _options_session_blocked(payload, service, session_info):
        result["skipped"] = "market closed"
        return payload

    try:
        return _apply(
            config,
            service,
            payload,
            result,
            desired,
            style,
            paper,
            positions=positions,
        )
    except Exception as exc:
        logger.warning("options overlay failed for %s: %s", symbol, exc)
        result["error"] = str(exc)
        _join_reason(payload, f"options error: {exc}")
        return payload


def apply_options_overlays(
    config: Any, service: Any, rows: list[dict[str, Any]]
) -> None:
    """Apply the overlay to every symbol using one clock + position snapshot."""
    if not rows:
        return
    session_info: dict[str, Any] | None = None
    try:
        session_info = service.market_session() or {}
    except Exception:
        session_info = None
    snapshot: list[dict[str, Any]] | None
    try:
        snapshot = service.get_all_positions()
    except Exception as exc:
        logger.warning("options overlay could not list positions: %s", exc)
        # None (not []) — an empty list would read as "confirmed no positions"
        # and let _existing_options wrongly re-open on top of real ones.
        snapshot = None
    for row in rows:
        apply_options_overlay(
            config, service, row, positions=snapshot, session_info=session_info
        )


def apply_pair_options_overlay(
    config: Any, service: Any, primary: dict[str, Any]
) -> dict[str, Any]:
    """Pair holds one ETF long at a time — overlay each leg independently."""
    long_s = str(primary.get("long_symbol") or "").upper()
    short_s = str(primary.get("short_symbol") or "").upper()
    session_info: dict[str, Any] | None = None
    try:
        session_info = service.market_session() or {}
    except Exception:
        session_info = None
    try:
        snapshot = service.get_all_positions()
    except Exception:
        # None (not []) — an empty list would read as "confirmed no positions"
        # and let _existing_options wrongly re-open on top of real ones.
        snapshot = None
    overlays: list[dict[str, Any]] = []
    notes: list[str] = []
    for sym in (long_s, short_s):
        if not sym:
            continue
        try:
            qty = float(service.get_position_qty(sym) or 0)
        except Exception:
            qty = 0.0
        row = {
            "symbol": sym,
            "price": 0,
            "session": primary.get("session"),
            "is_open": primary.get("is_open"),
            "position": qty,
            "intent": "open_long" if qty > 0 else "close_long",
            "signal": "buy" if qty > 0 else "sell",
            "engine": "pair",
            "actions": [],
        }
        apply_options_overlay(
            config,
            service,
            row,
            positions=snapshot,
            session_info=session_info,
        )
        overlay = row.get("options")
        if overlay:
            overlays.append(overlay)
            note = _overlay_note(overlay)
            if note:
                notes.append(f"{sym} {note}")
        for action in row.get("actions") or []:
            _append_action(primary, action)
    primary["options"] = overlays
    if notes:
        _join_reason(primary, "options: " + "; ".join(notes))
    return primary


def _apply(
    config: Any,
    service: Any,
    payload: dict[str, Any],
    result: dict[str, Any],
    desired: str,
    style: str,
    paper: bool,
    positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    symbol = result["symbol"]
    existing = _existing_options(service, symbol, positions)
    stored = load_overlay_state(symbol, paper=paper)

    if desired == "flat":
        closed = _close_underlying(service, symbol, existing)
        save_overlay_state(symbol, None, paper=paper)
        result["action"] = "close" if closed else "flat"
        result["closed"] = closed
        if closed:
            result["summary"] = f"closed {len(closed)} option leg(s)"
            _annotate_reason(payload, result["summary"])
            for row in closed:
                _append_action(payload, row)
        else:
            result["summary"] = "no option overlay"
        return payload

    stored_mismatch = bool(stored) and (
        stored.get("side") != desired or stored.get("style") != style
    )
    if existing and not stored_mismatch:
        result["action"] = "hold"
        result["summary"] = f"holding {style} {desired} overlay"
        result["legs"] = stored.get("legs") or []
        return payload

    if existing and stored_mismatch:
        closed = _close_underlying(service, symbol, existing)
        result["closed"] = closed
        for row in closed:
            _append_action(payload, row)

    opened = _open_overlay(config, service, payload, symbol, desired, style)
    result.update(opened)
    if opened.get("state"):
        save_overlay_state(symbol, opened["state"], paper=paper)
    note = opened.get("summary") or (
        opened.get("skipped") if opened.get("action") == "skip" else None
    )
    if note:
        _annotate_reason(payload, note)
    for row in opened.get("orders") or []:
        _append_action(payload, row)
    return payload


def _overlay_note(result: dict[str, Any]) -> str | None:
    if result.get("error"):
        return str(result["error"])
    action = str(result.get("action") or "")
    if action == "open" and result.get("summary"):
        return str(result["summary"])
    if action == "close" and result.get("closed"):
        return str(result.get("summary") or "closed overlay")
    skipped = str(result.get("skipped") or "")
    if skipped and skipped not in {"disabled", "market closed"}:
        return skipped
    return None


def _join_reason(payload: dict[str, Any], note: str) -> None:
    note = str(note or "").strip()
    if not note:
        return
    current = str(payload.get("reason") or "").rstrip()
    payload["reason"] = f"{current} | {note}" if current else note


def _annotate_reason(payload: dict[str, Any], note: str) -> None:
    note = str(note or "").strip()
    if not note:
        return
    _join_reason(payload, f"options: {note}")


def _close_underlying(
    service: Any, underlying: str, positions: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    rows = positions if positions is not None else service.option_positions_for_underlying(
        underlying
    )
    closed: list[dict[str, Any]] = []
    for pos in rows:
        occ = str(pos.get("symbol") or "")
        qty = abs(float(pos.get("qty") or pos.get("signed_qty") or 0))
        if not occ or qty <= 0:
            continue
        try:
            order = service.close_position(occ)
            oid = _order_id(order)
        except Exception as exc:
            logger.warning("could not close option %s: %s", occ, exc)
            closed.append(
                {
                    "symbol": occ,
                    "side": "close",
                    "qty": qty,
                    "reason": f"options close failed: {exc}",
                    "order_id": None,
                }
            )
            continue
        closed.append(
            {
                "symbol": occ,
                "side": "close",
                "qty": qty,
                "reason": f"flatten overlay {option_label(occ)}",
                "order_id": oid or None,
            }
        )
    return closed


def _open_overlay(
    config: Any,
    service: Any,
    payload: dict[str, Any],
    symbol: str,
    desired: str,
    style: str,
) -> dict[str, Any]:
    spot = _spot(payload, service, symbol)
    if spot <= 0:
        return {"action": "skip", "skipped": "no spot price"}

    min_dte = int(getattr(config, "options_dte_min", 21) or 21)
    max_dte = int(getattr(config, "options_dte_max", 45) or 45)
    otm_pct = float(getattr(config, "options_otm_pct", 5.0) or 5.0)
    qty = max(1, int(getattr(config, "options_max_contracts", 1) or 1))
    start, end = expiration_window(min_dte=min_dte, max_dte=max_dte)
    chain = service.list_option_contracts(
        symbol, expiration_gte=start, expiration_lte=end
    )
    expiry = pick_expiration(
        [c.get("expiration") for c in chain], min_dte=min_dte, max_dte=max_dte
    )
    if expiry is None:
        return {"action": "skip", "skipped": "no option expirations"}

    max_premium_pct = float(getattr(config, "options_max_premium_pct", 1.0) or 0)
    cap: float | None = None
    if max_premium_pct > 0:
        equity = _equity(service)
        if equity <= 0:
            # Cap is configured but we can't size it — fail safe, don't trade uncapped.
            return {"action": "skip", "skipped": "no account equity"}
        cap = equity * (max_premium_pct / 100.0)

    if style == "hedge":
        return _open_hedge(
            service,
            payload,
            symbol,
            desired,
            spot,
            chain,
            expiry,
            otm_pct,
            qty,
            cap,
        )
    if style == "long_option":
        return _open_long_option(
            service, symbol, desired, spot, chain, expiry, qty, cap
        )
    return _open_vertical(
        service, symbol, desired, spot, chain, expiry, otm_pct, qty, cap
    )


def _premium_ok(debit: float | None, qty: int, cap: float | None) -> str | None:
    if debit is None or debit <= 0:
        return "no option quote"
    if cap is None:
        return None
    cost = debit * 100.0 * qty
    if cost > cap:
        return f"premium ${cost:.0f} exceeds cap ${cap:.0f}"
    return None


def _open_vertical(
    service: Any,
    symbol: str,
    desired: str,
    spot: float,
    chain: list[dict[str, Any]],
    expiry: Any,
    otm_pct: float,
    qty: int,
    cap: float | None,
) -> dict[str, Any]:
    option_type = "call" if desired == "long" else "put"
    legs = _contracts_on_expiry(chain, expiry, option_type)
    pair = pick_vertical(legs, spot, option_type=option_type, otm_pct=otm_pct)
    if pair is None:
        return {"action": "skip", "skipped": f"no {option_type} vertical"}
    long_leg, short_leg = pair
    debit = _spread_debit(service, long_leg["symbol"], short_leg["symbol"])
    blocked = _premium_ok(debit, qty, cap)
    if blocked:
        return {"action": "skip", "skipped": blocked}
    order = service.submit_option_spread(
        long_leg["symbol"], short_leg["symbol"], qty=qty
    )
    oid = _order_id(order)
    name = "bull call" if desired == "long" else "bear put"
    long_info = _leg_payload(long_leg, role="long", side="buy")
    short_info = _leg_payload(short_leg, role="short", side="sell")
    summary = (
        f"{name} {long_info['label']} / {short_info['label']} x{qty}"
    )
    return {
        "action": "open",
        "summary": summary,
        "debit": debit,
        "orders": [
            {
                "symbol": long_leg["symbol"],
                "side": "buy",
                "qty": qty,
                "order_id": oid or None,
                "reason": summary,
            }
        ],
        "state": {
            "side": desired,
            "style": "vertical",
            "legs": [long_info, short_info],
            "qty": qty,
        },
    }


def _open_long_option(
    service: Any,
    symbol: str,
    desired: str,
    spot: float,
    chain: list[dict[str, Any]],
    expiry: Any,
    qty: int,
    cap: float | None,
) -> dict[str, Any]:
    option_type = "call" if desired == "long" else "put"
    legs = _contracts_on_expiry(chain, expiry, option_type)
    contract = pick_long_option(legs, spot)
    if contract is None:
        return {"action": "skip", "skipped": f"no {option_type} contract"}
    mid = service.option_quote_mid(contract["symbol"])
    blocked = _premium_ok(mid, qty, cap)
    if blocked:
        return {"action": "skip", "skipped": blocked}
    order = service.submit_option_order(contract["symbol"], qty, "buy")
    oid = _order_id(order)
    info = _leg_payload(contract, role="long", side="buy")
    summary = f"long {info['label']} x{qty}"
    return {
        "action": "open",
        "summary": summary,
        "orders": [
            {
                "symbol": contract["symbol"],
                "side": "buy",
                "qty": qty,
                "order_id": oid or None,
                "reason": summary,
            }
        ],
        "state": {
            "side": desired,
            "style": "long_option",
            "legs": [info],
            "qty": qty,
        },
    }


def _open_hedge(
    service: Any,
    payload: dict[str, Any],
    symbol: str,
    desired: str,
    spot: float,
    chain: list[dict[str, Any]],
    expiry: Any,
    otm_pct: float,
    qty: int,
    cap: float | None,
) -> dict[str, Any]:
    protect_type = "put" if desired == "long" else "call"
    protect_pool = _contracts_on_expiry(chain, expiry, protect_type)
    protect = pick_protective(
        protect_pool, spot, option_type=protect_type, otm_pct=otm_pct
    )
    if protect is None:
        return {"action": "skip", "skipped": f"no protective {protect_type}"}
    mid = service.option_quote_mid(protect["symbol"])
    blocked = _premium_ok(mid, qty, cap)
    if blocked:
        return {"action": "skip", "skipped": blocked}

    orders: list[dict[str, Any]] = []
    legs = [_leg_payload(protect, role="protect", side="buy")]
    order = service.submit_option_order(protect["symbol"], qty, "buy")
    oid = _order_id(order)
    summary_parts = [f"protective {legs[0]['label']} x{qty}"]
    orders.append(
        {
            "symbol": protect["symbol"],
            "side": "buy",
            "qty": qty,
            "order_id": oid or None,
            "reason": summary_parts[0],
        }
    )

    try:
        shares = abs(float(payload.get("position") or service.get_position_qty(symbol) or 0))
    except Exception:
        shares = 0.0
    if desired == "long" and shares >= 100:
        cover_pool = _contracts_on_expiry(chain, expiry, "call")
        cover = pick_protective(
            cover_pool, spot, option_type="call", otm_pct=otm_pct
        )
        cover_qty = min(qty, int(shares // 100))
        if cover is not None and cover_qty > 0:
            sold = service.submit_option_order(cover["symbol"], cover_qty, "sell")
            sold_id = _order_id(sold)
            cover_info = _leg_payload(cover, role="cover", side="sell")
            legs.append(cover_info)
            summary_parts.append(f"covered {cover_info['label']} x{cover_qty}")
            orders.append(
                {
                    "symbol": cover["symbol"],
                    "side": "sell",
                    "qty": cover_qty,
                    "order_id": sold_id or None,
                    "reason": summary_parts[-1],
                }
            )

    summary = " + ".join(summary_parts)
    return {
        "action": "open",
        "summary": summary,
        "orders": orders,
        "state": {
            "side": desired,
            "style": "hedge",
            "legs": legs,
            "qty": qty,
        },
    }


def _spread_debit(service: Any, long_symbol: str, short_symbol: str) -> float | None:
    long_mid = service.option_quote_mid(long_symbol)
    short_mid = service.option_quote_mid(short_symbol)
    if long_mid is None or short_mid is None:
        return None
    return max(0.01, float(long_mid) - float(short_mid))
