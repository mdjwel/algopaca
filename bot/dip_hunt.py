"""Buy-the-dip after a manual stop-out.

The point of the loop is a lower entry, not a faster one. After the protective
stop fills at ``P``, the desk watches for a further ``dip_pct`` drop:

* If that drop arrives *before* ``wait_minutes`` is up, it buys immediately at
  ``P × (1 − dip%)`` — waiting out the clock would only risk a bounce.
* If the wait elapses without the drop, it parks a limit at the same target so
  the fill happens whenever price finally gets there.

The new entry carries the same stop (and the same hunt), so a second stop-out
repeats the cycle until the user cancels it or a take-profit ends the trade.
"""

from __future__ import annotations

from bot.client import normalize_stock_order_price

WAIT_MINUTES_DEFAULT = 10.0
WAIT_MINUTES_MAX = 1440.0
DIP_PCT_DEFAULT = 5.0
DIP_PCT_MAX = 50.0

# States the watcher still owns. Anything else is history.
ACTIVE_STATUSES = frozenset(
    {
        "watching_entry",
        "watching_stop",
        "hunting",
        "awaiting_fill",
        "placing",
    }
)
# A user cancel is meaningful here — the desk has not yet spent the cash, or
# the parked limit can still be pulled.
CANCELLABLE_STATUSES = frozenset(
    {
        "watching_entry",
        "watching_stop",
        "hunting",
        "awaiting_fill",
    }
)


def target_buy_price(stop_fill_price: float, dip_pct: float) -> float:
    """Limit the re-entry will pay: ``dip_pct`` below the stop-out fill."""
    fill = float(stop_fill_price)
    pct = float(dip_pct)
    if fill <= 0 or pct <= 0:
        raise ValueError("Dip hunt needs a stop-out price and a dip % greater than 0")
    raw = fill * (1.0 - pct / 100.0)
    if raw <= 0:
        raise ValueError("Dip % is too large for this stop-out price")
    return normalize_stock_order_price(raw, field="dip hunt limit_price")


def hunt_action(
    *,
    mark: float | None,
    target: float,
    started_at: float,
    wait_minutes: float,
    now: float,
) -> str:
    """What the watcher should do on this poll.

    ``buy_now`` — price is already at or below the target; do not wait.
    ``park_limit`` — the wait elapsed without the dip; rest a limit at target.
    ``wait`` — still inside the window and the dip has not printed.
    The hunt does not expire on its own: after the wait, its GTC limit stays
    live until it fills or the user cancels it.
    """
    if mark is not None and float(mark) > 0 and float(mark) <= float(target):
        return "buy_now"
    if now >= float(started_at) + float(wait_minutes) * 60.0:
        return "park_limit"
    return "wait"
