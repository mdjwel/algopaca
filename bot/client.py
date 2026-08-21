"""Alpaca Trading + Market Data clients."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    ClosePositionRequest,
    GetOrderByIdRequest,
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
    TrailingStopOrderRequest,
)

from bot.config import Config
from bot.live_quote import fetch_live_mark

_TIMEFRAME_MAP = {
    "1Min": TimeFrame.Minute,
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame.Hour,
    "1Day": TimeFrame.Day,
}

# Free/paper keys usually cannot query recent SIP; IEX works for recent bars.
_DEFAULT_FEED = DataFeed.IEX
_ET = ZoneInfo("America/New_York")

# Slightly aggressive limits so paper fills are more likely outside RTH.
_BUY_SLIPPAGE = 1.005
_SELL_SLIPPAGE = 0.995
_MARK_CACHE_TTL = 5.0
# Order states that will never change again — the ticket can stop polling.
_TERMINAL_ORDER_STATES = frozenset(
    {"filled", "canceled", "cancelled", "expired", "rejected", "replaced"}
)
# Some non-terminal states are still broker-owned and cannot accept another
# cancel/replace request. Replacement has two additional early-state locks.
_NON_CANCELABLE_STATES = _TERMINAL_ORDER_STATES | {
    "pending_cancel",
    "pending_replace",
    "done_for_day",
    "stopped",
    "calculated",
}
_NON_REPLACEABLE_STATES = _NON_CANCELABLE_STATES | {"accepted", "pending_new"}
# Alpaca rejects PATCH while the ticket is still accepted/pending_new
# (typical overnight). The blotter still offers Edit; replace_order
# cancel-and-resubmits instead of patching.
_REWRITE_INSTEAD_OF_PATCH = frozenset({"accepted", "pending_new"})
_REPLACEABLE_ORDER_TYPES = frozenset(
    {"limit", "stop", "stop_limit", "trailing_stop"}
)
_BLOTTER_STATUS = {
    "open": QueryOrderStatus.OPEN,
    "closed": QueryOrderStatus.CLOSED,
}

# Order types a hand-typed ticket may use.
MANUAL_ORDER_TYPES = frozenset(
    {"market", "limit", "stop", "stop_limit", "trailing_stop"}
)
# Only a simple entry can carry an OTO stop or a bracket target; a conditional
# entry already owns a trigger, and Alpaca will not nest the two.
ATTACHABLE_ENTRY_TYPES = frozenset({"market", "limit"})

_TIME_IN_FORCE = {
    "day": TimeInForce.DAY,
    "gtc": TimeInForce.GTC,
    "ioc": TimeInForce.IOC,
    "fok": TimeInForce.FOK,
    "opg": TimeInForce.OPG,
    "cls": TimeInForce.CLS,
}
MANUAL_TIME_IN_FORCE = frozenset(_TIME_IN_FORCE)


def is_alpaca_order_id(order_id: str) -> bool:
    """True when ``order_id`` is a broker UUID Alpaca will accept on GET."""
    try:
        UUID(str(order_id).strip())
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def normalize_time_in_force(value: Any) -> TimeInForce:
    """`"gtc"` → ``TimeInForce.GTC``. Unknown values are an error, not a default.

    Silently falling back to DAY would turn a ticket the user meant to rest for
    weeks into one that dies at the close.
    """
    key = str(value or "day").strip().lower()
    try:
        return _TIME_IN_FORCE[key]
    except KeyError:
        raise ValueError(
            "time_in_force must be one of: " + ", ".join(sorted(_TIME_IN_FORCE))
        ) from None


def whole_qty_for_attached_stop(qty: float) -> tuple[float, bool]:
    """Size a protected entry: ``(qty, can_attach_stop)``.

    An attached stop makes the entry an OTO order, and Alpaca rejects fractional
    quantities on anything but a simple order ("fractional orders must be simple
    orders", code 42210000). Floor to whole shares; below one share the stop
    cannot ride along at all, so the caller sends a plain fractional order.
    """
    qty = float(qty or 0)
    whole = float(int(qty))  # truncate toward zero; never round size up
    if whole >= 1:
        return whole, True
    return qty, False


def normalize_stock_order_price(value: Any, *, field: str = "price") -> float:
    """Apply Alpaca's equity tick precision: 4 decimals below $1, else 2."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive number") from None
    if price <= 0:
        raise ValueError(f"{field} must be greater than 0")
    return round(price, 4 if price < 1 else 2)


def limit_price_for_stop(
    stop_price: float,
    offset_pct: float | None,
    *,
    short: bool = False,
) -> float | None:
    """Sell/cover limit after a stop triggers.

    ``offset_pct`` 0/None keeps a stop-market exit (fill at whatever the tape
    gives). Above 0 turns the exit into a stop-limit: long sells this % below
    the stop, short covers this % above it. The limit may sit *at* the stop
    (never past it the wrong way).
    """
    try:
        stop = float(stop_price)
        offset = float(offset_pct or 0)
    except (TypeError, ValueError):
        return None
    if stop <= 0 or offset <= 0:
        return None
    raw = stop * (1.0 + offset / 100.0) if short else stop * (1.0 - offset / 100.0)
    if raw <= 0:
        return None
    return normalize_stop_exit_limit(stop, raw, short=short)


def normalize_stop_exit_limit(
    stop_price: float,
    limit_price: float,
    *,
    short: bool = False,
    clamp: bool = True,
) -> float:
    """Protective stop-limit fill: long ≤ stop, short ≥ stop (equality allowed).

    ``clamp`` soft-corrects a cushion that rounded the wrong way. Absolute
    ticket prices should pass ``clamp=False`` so a limit past the stop is an
    error the user can fix, not a silent rewrite.
    """
    stop = normalize_stock_order_price(stop_price, field="stop_price")
    limit = normalize_stock_order_price(limit_price, field="limit_price")
    if short:
        if limit < stop:
            if not clamp:
                raise ValueError(
                    "Cover limit must be at or above the stop price for a short."
                )
            limit = stop
    elif limit > stop:
        if not clamp:
            raise ValueError(
                "Sell limit must be at or below the stop price for a long."
            )
        limit = stop
    return limit


def stop_loss_request_for(
    stop_price: float,
    offset_pct: float | None = 0.0,
    *,
    short: bool = False,
    limit_price: float | None = None,
) -> StopLossRequest:
    """OTO/bracket stop leg — market exit, or stop-limit when a limit is set."""
    if limit_price is not None and float(limit_price) > 0:
        limit = normalize_stop_exit_limit(
            stop_price, limit_price, short=short, clamp=True
        )
    else:
        limit = limit_price_for_stop(stop_price, offset_pct, short=short)
    if limit is not None:
        return StopLossRequest(stop_price=stop_price, limit_price=limit)
    return StopLossRequest(stop_price=stop_price)


def equity_session_date(now: datetime | None = None) -> date:
    """US equity session date in America/New_York (overnight after 20:00 → next day)."""
    et = (now or datetime.now(timezone.utc)).astimezone(_ET)
    if et.time() >= time(20, 0):
        return et.date() + timedelta(days=1)
    return et.date()


def bar_session_date(ts: Any) -> date | None:
    """Trading date a daily bar belongs to.

    Alpaca 1Day bars are labeled at the start of the bar. Midnight UTC timestamps
    fall on the previous calendar date in ET (20:00), so use the UTC date then.
    """
    if ts is None:
        return None
    try:
        stamp = pd.Timestamp(ts)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    et = stamp.tz_convert(_ET)
    if et.hour >= 20:
        return stamp.date()
    return et.date()


class AlpacaService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.trading = TradingClient(
            config.api_key,
            config.secret_key,
            paper=config.paper,
        )
        self.data = StockHistoricalDataClient(config.api_key, config.secret_key)
        self._mark_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def account_summary(self) -> dict:
        account = self.trading.get_account()
        equity = float(account.equity)
        # last_equity is the prior session close — the daily loss limit reads it.
        try:
            last_equity = float(getattr(account, "last_equity", 0) or 0)
        except (TypeError, ValueError):
            last_equity = 0.0
        day_pl_pct = (
            round((equity - last_equity) / last_equity * 100, 3)
            if last_equity > 0
            else None
        )
        return {
            "id": str(getattr(account, "id", "") or ""),
            "account_number": str(getattr(account, "account_number", "") or ""),
            "status": account.status,
            "equity": equity,
            "last_equity": last_equity or None,
            "day_pl_pct": day_pl_pct,
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "paper": self.config.paper,
            "trading_mode": "paper" if self.config.paper else "live",
        }

    def fetch_closed_orders(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        symbols: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Filled orders newest-first, plus how many closed orders Alpaca returned.

        Callers need the pre-filter count: unfilled (canceled / expired) orders are
        dropped below, so the filled-row count alone cannot tell you whether Alpaca
        truncated the window. Comparing the raw count against the requested limit can.
        """
        requested = max(1, min(int(limit or 100), 500))
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=after,
            until=until,
            symbols=symbols or None,
            limit=requested,
            direction="desc",
            nested=True,
        )
        orders = list(self.trading.get_orders(req) or [])
        out: list[dict[str, Any]] = []
        # Paging walks back by submitted_at, and it must step past orders that
        # never filled too — so take the cursor from the raw page, not from the
        # filled rows that survive the loop below.
        oldest_submitted: datetime | None = None
        for order in orders:
            stamp = getattr(order, "submitted_at", None)
            if isinstance(stamp, datetime) and (
                oldest_submitted is None or stamp < oldest_submitted
            ):
                oldest_submitted = stamp
        for order in orders:
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
            if filled_qty <= 0:
                continue
            side = getattr(order, "side", None)
            side_s = side.value if hasattr(side, "value") else str(side or "").lower()
            otype = getattr(order, "type", None)
            type_s = otype.value if hasattr(otype, "value") else str(otype or "")
            status = getattr(order, "status", None)
            status_s = status.value if hasattr(status, "value") else str(status or "")
            filled_at = getattr(order, "filled_at", None) or getattr(
                order, "submitted_at", None
            )
            out.append(
                {
                    "id": str(getattr(order, "id", "") or ""),
                    "symbol": str(getattr(order, "symbol", "") or "").upper(),
                    "side": side_s.lower(),
                    "type": type_s.lower(),
                    "status": status_s.lower(),
                    "qty": filled_qty,
                    "filled_avg_price": float(
                        getattr(order, "filled_avg_price", 0) or 0
                    )
                    or None,
                    "filled_at": (
                        filled_at.isoformat()
                        if hasattr(filled_at, "isoformat")
                        else (str(filled_at) if filled_at else None)
                    ),
                    "submitted_at": (
                        order.submitted_at.isoformat()
                        if getattr(order, "submitted_at", None) is not None
                        and hasattr(order.submitted_at, "isoformat")
                        else None
                    ),
                    "notional": (
                        round(filled_qty * float(getattr(order, "filled_avg_price", 0) or 0), 4)
                        if getattr(order, "filled_avg_price", None)
                        else None
                    ),
                }
            )
        return {
            "orders": out,
            "raw_count": len(orders),
            "requested": requested,
            "oldest_submitted_at": oldest_submitted,
            # Alpaca returned a full page, so older orders almost certainly exist.
            "truncated": len(orders) >= requested,
        }

    def fetch_closed_order_window(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        symbols: list[str] | None = None,
        page_limit: int = 500,
        max_pages: int = 10,
    ) -> dict[str, Any]:
        """Every filled order in a range, paging back through Alpaca.

        `fetch_closed_orders` is one page, and Alpaca caps a page at 500. A range
        like "All" on an active account holds far more than that, so a single
        page silently hides the older half of the account's life — and FIFO then
        pairs sells against buys that were never loaded. This walks the `until`
        cursor backwards until a short page says the range is exhausted.

        `truncated` here means the *ceiling* stopped the walk, not the page size.
        """
        page = max(1, min(int(page_limit or 500), 500))
        pages = max(1, int(max_pages or 1))
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        raw_total = 0
        cursor = until
        truncated = False

        for _ in range(pages):
            chunk = self.fetch_closed_orders(
                after=after, until=cursor, symbols=symbols, limit=page
            )
            raw_total += int(chunk.get("raw_count") or 0)
            for row in chunk["orders"]:
                # `until` may be inclusive, so the boundary order can repeat.
                oid = row.get("id") or ""
                if oid and oid in seen:
                    continue
                if oid:
                    seen.add(oid)
                rows.append(row)
            if not chunk["truncated"]:
                break  # a short page means we reached the start of the range
            oldest = chunk.get("oldest_submitted_at")
            if oldest is None or (cursor is not None and oldest >= cursor):
                # No usable cursor, or it refuses to move — stop rather than spin.
                truncated = True
                break
            cursor = oldest
        else:
            # Ran out of pages with a full one still coming back.
            truncated = True

        return {
            "orders": rows,
            "raw_count": raw_total,
            "requested": page * pages,
            "truncated": truncated,
        }

    def fetch_fill_activities(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        page_size: int = 100,
        max_pages: int | None = None,
    ) -> dict[str, Any]:
        """Every execution fill in a range, newest-first.

        The orders endpoint only exposes an order's aggregate filled quantity and
        average price. Account activities are the broker's actual fill ledger,
        including partial fills, and provide a stable page token for walking the
        complete account history.

        A trade activity carries no order type. The schema is qty / price / side
        / symbol / order_id / order_status plus ``type``, which is the *fill's*
        kind — ``fill`` or ``partial_fill``. Rows therefore expose ``fill_type``
        and no ``type``: a caller that needs limit/stop/market has to read the
        order itself, and must not mistake one field for the other.
        """
        size = max(1, min(int(page_size or 100), 100))
        page_ceiling = max(1, int(max_pages)) if max_pages is not None else None
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_token: str | None = None
        page_count = 0
        raw_total = 0
        truncated = False

        while page_ceiling is None or page_count < page_ceiling:
            params: dict[str, Any] = {
                "activity_types": "FILL",
                "direction": "desc",
                "page_size": size,
            }
            if after is not None:
                params["after"] = after.isoformat()
            if until is not None:
                params["until"] = until.isoformat()
            if page_token:
                params["page_token"] = page_token

            activities = list(self.trading.get("/account/activities", params) or [])
            page_count += 1
            raw_total += len(activities)
            added = 0

            for activity in activities:
                if not isinstance(activity, dict):
                    continue
                activity_id = str(activity.get("id") or "")
                if activity_id and activity_id in seen:
                    continue
                if activity_id:
                    seen.add(activity_id)

                qty = float(activity.get("qty") or 0)
                price = float(activity.get("price") or 0)
                if qty <= 0 or price <= 0:
                    continue
                order_id = str(activity.get("order_id") or "")
                filled_at = activity.get("transaction_time")
                # Activity sides describe intent as well as direction. FIFO only
                # needs inventory direction, so short entries are sells and
                # covers are buys. Leaving `sell_short` untouched caused the
                # walk to ignore that execution whenever it fell inside the
                # selected range, while an older closed-order seed called the
                # same execution `sell` — making P&L change with the range chip.
                raw_side = str(activity.get("side") or "").lower()
                side = {
                    "buy_to_cover": "buy",
                    "sell_short": "sell",
                }.get(raw_side, raw_side)
                rows.append(
                    {
                        # Keep `id` as the order id: desk attribution and CSV
                        # exports correlate against the order the desk placed.
                        "id": order_id or activity_id,
                        "activity_id": activity_id,
                        "symbol": str(activity.get("symbol") or "").upper(),
                        "side": side,
                        "fill_type": str(activity.get("type") or "fill").lower(),
                        "status": str(activity.get("order_status") or "filled").lower(),
                        "qty": qty,
                        "filled_avg_price": price,
                        "filled_at": str(filled_at) if filled_at else None,
                        "submitted_at": None,
                        "notional": round(qty * price, 4),
                    }
                )
                added += 1

            if len(activities) < size:
                break
            # The cursor is the last row's id, and the page may end on something
            # that is not a dict — take the last one that is rather than raise.
            last = next(
                (a for a in reversed(activities) if isinstance(a, dict)), None
            )
            next_token = str((last or {}).get("id") or "")
            if not next_token or next_token == page_token or added == 0:
                # A broken/repeating cursor must be visible to the page instead
                # of spinning forever or silently claiming a complete ledger.
                truncated = True
                break
            page_token = next_token
        else:
            truncated = True

        return {
            "orders": rows,
            "raw_count": raw_total,
            "requested": size * page_count,
            "page_count": page_count,
            "truncated": truncated,
        }

    def list_closed_orders(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        symbols: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Filled / closed orders newest-first for history + P&L views."""
        return self.fetch_closed_orders(
            after=after, until=until, symbols=symbols, limit=limit
        )["orders"]

    def portfolio_history_summary(
        self,
        *,
        period: str | None = "1M",
        timeframe: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        label: str | None = None,
        max_points: int = 400,
    ) -> dict[str, Any]:
        """Account equity / P&L summary for a period or start date (Alpaca)."""
        req_kwargs: dict[str, Any] = {}
        if start is not None:
            # True YTD (or custom window): use start, not period="all".
            req_kwargs["start"] = start
            if end is not None:
                req_kwargs["end"] = end
            req_kwargs["timeframe"] = timeframe or "1D"
            period_label = label or "custom"
        else:
            period = (period or "1M").strip()
            req_kwargs["period"] = period
            req_kwargs["timeframe"] = timeframe or (
                "1H" if period in {"1D", "1H"} else "1D"
            )
            period_label = period
        hist = self.trading.get_portfolio_history(
            GetPortfolioHistoryRequest(**req_kwargs)
        )
        equity = list(getattr(hist, "equity", None) or [])
        profit_loss = list(getattr(hist, "profit_loss", None) or [])
        profit_loss_pct = list(getattr(hist, "profit_loss_pct", None) or [])
        timestamps = list(getattr(hist, "timestamp", None) or [])
        base = float(getattr(hist, "base_value", 0) or 0)
        last_eq = float(equity[-1]) if equity else None
        last_pl = float(profit_loss[-1]) if profit_loss else None
        last_pct = float(profit_loss_pct[-1]) if profit_loss_pct else None
        # Alpaca's profit_loss array is per-bar. The P&L *for the window* is the
        # move from base_value to the closing equity — not the final bar's delta.
        window_pl = (
            round(last_eq - base, 4) if last_eq is not None and base > 0 else None
        )
        window_pct = (
            round(window_pl / base, 6) if window_pl is not None and base > 0 else None
        )
        series = self._equity_series(equity, timestamps, max_points)
        return {
            "period": period_label,
            "timeframe": req_kwargs["timeframe"],
            "base_value": base or None,
            "equity": last_eq,
            "profit_loss": window_pl,
            "profit_loss_pct": window_pct,
            # Latest bar only — useful, but never the window figure.
            "day_profit_loss": last_pl,
            "day_profit_loss_pct": last_pct,
            "points": len(equity),
            "series": series,
            "start_ts": timestamps[0] if timestamps else None,
            "end_ts": timestamps[-1] if timestamps else None,
        }

    @staticmethod
    def _equity_series(
        equity: list[Any], timestamps: list[Any], max_points: int = 400
    ) -> list[dict[str, Any]]:
        """Equity curve points for the History chart, evenly downsampled."""
        pairs = [
            {"t": int(ts), "equity": round(float(eq), 4)}
            for ts, eq in zip(timestamps, equity)
            if ts is not None and eq is not None
        ]
        cap = max(2, int(max_points or 400))
        if len(pairs) <= cap:
            return pairs
        stride = len(pairs) / cap
        picked = [pairs[int(i * stride)] for i in range(cap)]
        # Always keep the true endpoint so the curve ends on the real closing equity.
        if picked[-1] is not pairs[-1]:
            picked[-1] = pairs[-1]
        return picked

    @staticmethod
    def realized_pnl_from_orders(
        orders: list[dict[str, Any]],
        *,
        opening_lots: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """FIFO realized P&L from filled buy/sell rows (oldest first).

        Signed inventory, same walk as ``open_lots_from_orders``: a sell against
        a long closes it, a buy against a short covers it, and leftover qty
        opens the other way. Realized P&L rides on the closing fill — the sell
        of a long or the cover buy of a short.

        ``opening_lots`` is the inventory the account already held when the
        window opens: ``{symbol: [{"qty": >0, "price": float|None,
        "direction": ±1, "estimated": bool}, ...]}``, oldest parcel first.
        Without it the walk starts flat and has to guess: a sell that is really
        closing a position bought before the window looks like a *new short*,
        and the next buy of that symbol then "covers" it and invents a profit on
        what is plainly an opening trade. Seeding the queue removes the guess.

        These are real parcels, not one blended average, because a close eats
        them one at a time: collapsing five lots into their mean prices every
        partial close wrong, and mixing a priced parcel with an unpriced one
        inside a single fill yields a percentage against a cost basis that only
        covers part of the shares.

        A parcel with no price (nothing left in the account to read a basis
        from) still fixes the direction, but it cannot price the close: those
        shares contribute no P&L and are reported through ``estimated_close_qty``
        instead of carrying a number nobody can stand behind.
        """
        chron = sorted(
            [dict(o) for o in orders],
            key=lambda o: str(o.get("filled_at") or o.get("submitted_at") or ""),
        )
        # Each lot is [qty, price, opening_order_id, direction, basis] with
        # direction +1 long / -1 short and qty always positive. `basis` says
        # where the cost came from: "fill" (an execution in this window),
        # "carried" (priced from Alpaca's average entry), or None (carried with
        # no price available — the close is real but its profit is not).
        lots: dict[str, list[list[Any]]] = {}
        for sym, parcels in (opening_lots or {}).items():
            queue: list[list[Any]] = []
            for parcel in parcels or []:
                qty = abs(float(parcel.get("qty") or 0.0))
                if qty <= 1e-9:
                    continue
                price = parcel.get("price")
                priced = price is not None and float(price) > 0
                if not priced:
                    basis = None
                else:
                    basis = "carried" if parcel.get("estimated") else "fill"
                queue.append(
                    [
                        qty,
                        float(price or 0.0),
                        "",  # no opening order to credit: it predates the range
                        -1 if float(parcel.get("direction") or 1) < 0 else 1,
                        basis,
                    ]
                )
            if queue:
                lots[str(sym).upper()] = queue
        realized = 0.0
        realized_by_symbol: dict[str, float] = {}
        # P&L credited to the order that OPENED the position, so the desk can
        # attribute it back to the strategy/preset that made the entry.
        realized_by_entry: dict[str, float] = {}
        matched = 0
        closed_pnls: list[float] = []
        # Qty closed against carried inventory we could not price, per symbol.
        estimated_close_qty: dict[str, float] = {}
        for row in chron:
            symbol = str(row.get("symbol") or "").upper()
            side = str(row.get("side") or "").lower()
            qty = float(row.get("qty") or 0)
            px = float(row.get("filled_avg_price") or 0)
            row.pop("realized_pnl", None)
            row.pop("realized_pnl_pct", None)
            row.pop("unpriced_qty", None)
            row.pop("basis_estimated", None)
            if not symbol or qty <= 0 or px <= 0:
                continue
            if side not in {"buy", "sell"}:
                continue
            direction = 1 if side == "buy" else -1
            queue = lots.setdefault(symbol, [])
            remaining = qty
            close_pnl = 0.0
            cost_basis = 0.0
            unpriced = 0.0
            estimated = False
            while remaining > 1e-9 and queue and queue[0][3] != direction:
                lot_qty, lot_px, lot_order, lot_dir, lot_basis = queue[0]
                take = min(remaining, lot_qty)
                if lot_basis != "fill":
                    estimated = True
                if lot_basis is not None:
                    # Long close: (exit - entry) * qty. Short cover: the inverse.
                    pnl = lot_dir * (px - lot_px) * take
                    close_pnl += pnl
                    cost_basis += lot_px * take
                    realized += pnl
                    realized_by_symbol[symbol] = (
                        realized_by_symbol.get(symbol, 0.0) + pnl
                    )
                    if lot_order:
                        realized_by_entry[lot_order] = (
                            realized_by_entry.get(lot_order, 0.0) + pnl
                        )
                else:
                    # Carried inventory with no cost basis: the close is real,
                    # the profit on it is not knowable from this window.
                    unpriced += take
                lot_qty -= take
                remaining -= take
                if lot_qty <= 1e-9:
                    queue.pop(0)
                else:
                    queue[0][0] = lot_qty
            if cost_basis > 0:
                # Count closing executions, not the number of FIFO lots they
                # consumed. The UI labels this value "matched close(s)".
                matched += 1
                row["realized_pnl"] = round(close_pnl, 4)
                row["realized_pnl_pct"] = round(close_pnl / cost_basis * 100, 4)
                closed_pnls.append(close_pnl)
            if estimated:
                # Priced off Alpaca's blended average entry, not off a fill this
                # window actually saw. The page marks the number as an estimate.
                row["basis_estimated"] = True
            if unpriced > 1e-9:
                row["unpriced_qty"] = round(unpriced, 9)
                estimated_close_qty[symbol] = round(
                    estimated_close_qty.get(symbol, 0.0) + unpriced, 9
                )
            if remaining > 1e-9:
                queue.append(
                    [remaining, px, str(row.get("id") or ""), direction, "fill"]
                )
        open_qty = {
            sym: round(sum(lot[3] * lot[0] for lot in queue), 6)
            for sym, queue in lots.items()
            if queue
        }
        return {
            "realized_pnl": round(realized, 4),
            "by_symbol": {k: round(v, 4) for k, v in sorted(realized_by_symbol.items())},
            "matched_sells": matched,
            "open_lot_qty": open_qty,
            "realized_by_entry_order": {
                k: round(v, 4) for k, v in realized_by_entry.items()
            },
            "estimated_close_qty": estimated_close_qty,
            # Kept for older History clients. Signed FIFO no longer leaves
            # leftover sells unmatched — they open a short lot instead.
            "unmatched_sells": 0,
            "unmatched_sell_qty": {},
            "stats": AlpacaService._trade_stats(closed_pnls),
            "orders": list(reversed(chron)),  # newest-first for UI
        }

    @staticmethod
    def _trade_stats(pnls: list[float]) -> dict[str, Any]:
        """Win rate and win/loss shape across closed (matched) fills."""
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_profit = sum(wins)
        gross_loss = -sum(losses)  # positive magnitude
        closed = len(pnls)
        return {
            "closed_trades": closed,
            "wins": len(wins),
            "losses": len(losses),
            "scratches": closed - len(wins) - len(losses),
            "win_rate": round(len(wins) / closed * 100, 2) if closed else None,
            "avg_win": round(gross_profit / len(wins), 4) if wins else None,
            "avg_loss": round(-gross_loss / len(losses), 4) if losses else None,
            "largest_win": round(max(wins), 4) if wins else None,
            "largest_loss": round(min(losses), 4) if losses else None,
            "gross_profit": round(gross_profit, 4),
            "gross_loss": round(-gross_loss, 4),
            "profit_factor": (
                round(gross_profit / gross_loss, 3) if gross_loss > 1e-9 else None
            ),
        }

    @staticmethod
    def open_lots_from_orders(
        orders: list[dict[str, Any]],
        *,
        symbols: set[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Entry fills no exit has retired yet, per symbol, oldest first.

        ``realized_pnl_from_orders`` walks the same FIFO queue but throws the
        residue away; the Positions page needs exactly that residue — the
        parcels a holding is actually built from, each with the price and date
        it was bought at. Signed, so a short book reconstructs the same way:
        the sell opens the lot and the buy retires it.
        """
        wanted = {str(s).upper() for s in symbols} if symbols is not None else None
        chron = sorted(
            orders,
            key=lambda o: str(o.get("filled_at") or o.get("submitted_at") or ""),
        )
        queues: dict[str, list[dict[str, Any]]] = {}
        for row in chron:
            symbol = str(row.get("symbol") or "").upper()
            side = str(row.get("side") or "").lower()
            qty = float(row.get("qty") or 0)
            px = float(row.get("filled_avg_price") or 0)
            if not symbol or qty <= 0 or px <= 0:
                continue
            if wanted is not None and symbol not in wanted:
                continue
            if side not in {"buy", "sell"}:
                continue
            direction = 1 if side == "buy" else -1
            queue = queues.setdefault(symbol, [])
            remaining = qty
            # An opposite-direction fill retires the oldest open parcels first.
            # Whatever it cannot absorb opens a new parcel the other way, which
            # is how a long that flips short reconstructs correctly.
            while remaining > 1e-9 and queue and queue[0]["direction"] != direction:
                take = min(remaining, queue[0]["qty"])
                queue[0]["qty"] = round(queue[0]["qty"] - take, 9)
                remaining -= take
                if queue[0]["qty"] <= 1e-9:
                    queue.pop(0)
            if remaining > 1e-9:
                queue.append(
                    {
                        "direction": direction,
                        "qty": round(remaining, 9),
                        "price": px,
                        "order_id": str(row.get("id") or ""),
                        "opened_at": row.get("filled_at") or row.get("submitted_at"),
                        "order_type": str(row.get("type") or ""),
                    }
                )
        return {sym: queue for sym, queue in queues.items() if queue}

    @staticmethod
    def _signed_qty(position: Any) -> float:
        try:
            qty = float(position.qty)
        except (TypeError, ValueError):
            return 0.0
        side = str(getattr(position, "side", "") or "").lower()
        if "short" in side:
            return -abs(qty)
        if "long" in side:
            return abs(qty)
        return qty

    def get_position_qty(self, symbol: str) -> float:
        """Signed quantity: >0 long, <0 short, 0 flat."""
        try:
            return self._signed_qty(self.trading.get_open_position(symbol))
        except Exception:
            return 0.0

    def get_position_qty_strict(self, symbol: str) -> float:
        """Signed quantity, only treating Alpaca's not-found response as flat.

        Safety-sensitive order flows must not turn a timeout, authentication
        failure, or broker outage into a false flat position.
        """
        try:
            return self._signed_qty(self.trading.get_open_position(symbol))
        except APIError as exc:
            if exc.status_code == 404:
                return 0.0
            raise

    def get_position_detail(self, symbol: str) -> dict[str, Any]:
        """Full open-position read: size, entry, and live unrealized P&L.

        The AI desk cannot manage an exit without knowing where it got in, so
        this feeds both the model context and the stop manager.
        """
        try:
            position = self.trading.get_open_position(symbol)
        except Exception:
            return {"qty": 0.0, "side": "flat", "avg_entry": None, "unrealized_pct": None}
        # Reuse the position just fetched — asking Alpaca again for the same
        # symbol doubled the round trips on every AI context build.
        qty = self._signed_qty(position)

        def _f(attr: str) -> float | None:
            try:
                value = float(getattr(position, attr, 0) or 0)
            except (TypeError, ValueError):
                return None
            return value or None

        avg_entry = _f("avg_entry_price")
        unrealized_plpc = _f("unrealized_plpc")
        return {
            "qty": qty,
            "side": "long" if qty > 0 else "short" if qty < 0 else "flat",
            "avg_entry": avg_entry,
            "current_price": _f("current_price"),
            "market_value": _f("market_value"),
            "unrealized_pl": _f("unrealized_pl"),
            # Alpaca reports plpc as a signed fraction already adjusted for side.
            "unrealized_pct": (
                round(unrealized_plpc * 100, 3) if unrealized_plpc is not None else None
            ),
        }

    def get_avg_entry_price(self, symbol: str) -> float | None:
        try:
            position = self.trading.get_open_position(symbol)
            avg = float(getattr(position, "avg_entry_price", 0) or 0)
            return avg if avg > 0 else None
        except Exception:
            return None

    @staticmethod
    def _enum_name(value: Any, default: str = "") -> str:
        """`AssetExchange.ARCA` → `ARCA`.

        The SDK hands back enum members whose ``str()`` carries the class
        prefix. Rendered raw, the Positions table showed literal
        "AssetExchange.ARCA" chips, so strip the prefix at the boundary.
        """
        if value is None:
            return default
        text = str(getattr(value, "value", value) or "").strip()
        if not text:
            return default
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        return text or default

    def get_all_positions(self) -> list[dict[str, Any]]:
        """Fetch all open positions from Alpaca with normalized properties."""
        if not getattr(self, "trading", None):
            return []
        raw_positions = self.trading.get_all_positions() or []

        result = []
        for pos in raw_positions:
            def _f(attr: str) -> float | None:
                try:
                    val = getattr(pos, attr, None)
                    if val is None:
                        return None
                    return float(val)
                except (TypeError, ValueError):
                    return None

            raw_qty = _f("qty") or 0.0
            side_str = str(getattr(pos, "side", "") or "").lower()
            if "short" in side_str:
                signed_qty = -abs(raw_qty)
                side = "short"
            elif "long" in side_str:
                signed_qty = abs(raw_qty)
                side = "long"
            else:
                signed_qty = raw_qty
                side = "long" if raw_qty > 0 else "short" if raw_qty < 0 else "flat"

            unrealized_plpc = _f("unrealized_plpc")
            unrealized_intraday_plpc = _f("unrealized_intraday_plpc")
            change_today = _f("change_today")
            qty_avail = _f("qty_available")

            item = {
                "asset_id": str(getattr(pos, "asset_id", "") or ""),
                "symbol": str(getattr(pos, "symbol", "") or "").upper(),
                "exchange": self._enum_name(getattr(pos, "exchange", None)),
                "asset_class": self._enum_name(
                    getattr(pos, "asset_class", None), "us_equity"
                ),
                "qty": abs(raw_qty),
                "signed_qty": signed_qty,
                "side": side,
                "market_value": _f("market_value"),
                "cost_basis": _f("cost_basis"),
                "avg_entry_price": _f("avg_entry_price"),
                "current_price": _f("current_price"),
                "lastday_price": _f("lastday_price"),
                "unrealized_pl": _f("unrealized_pl"),
                "unrealized_pct": (
                    round(unrealized_plpc * 100, 3) if unrealized_plpc is not None else None
                ),
                "unrealized_intraday_pl": _f("unrealized_intraday_pl"),
                "unrealized_intraday_pct": (
                    round(unrealized_intraday_plpc * 100, 3)
                    if unrealized_intraday_plpc is not None
                    else None
                ),
                "change_today": (
                    round(change_today * 100, 3) if change_today is not None else None
                ),
                "qty_available": abs(qty_avail) if qty_avail is not None else abs(raw_qty),
            }
            result.append(item)
        return result

    def close_position(
        self,
        symbol: str,
        *,
        qty: float | None = None,
        percentage: float | None = None,
        cancel_orders: bool | None = None,
    ) -> dict[str, Any]:
        """Liquidate an open position (100% or partial by qty / percentage)."""
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            raise ValueError("Symbol is required to close position")
        if qty is not None and percentage is not None:
            raise ValueError("Provide qty or percentage, not both")

        close_options = None
        if qty is not None and qty > 0:
            close_options = ClosePositionRequest(qty=str(qty))
        elif percentage is not None and 0 < percentage <= 100:
            close_options = ClosePositionRequest(percentage=str(percentage))

        # Full close defaults to cancelling resting orders so they cannot wash
        # against the exit. Partial closes keep protection unless asked.
        should_cancel = (
            cancel_orders if cancel_orders is not None else close_options is None
        )
        if should_cancel:
            try:
                self.cancel_open_orders_for_symbol(symbol)
            except Exception:
                pass

        res = self.trading.close_position(symbol, close_options=close_options)
        return self._normalize_close_response(res, symbol=symbol)

    def close_all_positions(
        self, cancel_orders: bool = True
    ) -> list[dict[str, Any]]:
        """Liquidate all open positions."""
        responses = self.trading.close_all_positions(cancel_orders=cancel_orders) or []
        return [self._normalize_close_response(r) for r in responses]

    def close_batch_positions(
        self,
        symbols: list[str],
        *,
        cancel_orders: bool = True,
    ) -> list[dict[str, Any]]:
        """Liquidate selected open positions."""
        results = []
        for sym in symbols:
            s = str(sym or "").strip().upper()
            if not s:
                continue
            try:
                res = self.close_position(s, cancel_orders=cancel_orders)
                results.append(res)
            except Exception as exc:
                results.append({"symbol": s, "error": str(exc), "status": "failed"})
        return results

    def get_open_orders_summary(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch all open orders and group them by symbol with protection metadata."""
        if not getattr(self, "trading", None):
            return {}
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                limit=200,
            )
            orders = list(self.trading.get_orders(req) or [])
        except Exception:
            return {}
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for o in orders:
            sym = str(getattr(o, "symbol", "") or "").upper()
            if not sym:
                continue
            otype = getattr(o, "type", None)
            side = str(getattr(o, "side", "") or "").lower()
            stop_px = float(getattr(o, "stop_price", 0) or 0) if getattr(o, "stop_price", None) else None
            limit_px = float(getattr(o, "limit_price", 0) or 0) if getattr(o, "limit_price", None) else None
            by_symbol.setdefault(sym, []).append({
                "id": str(getattr(o, "id", "")),
                "type": str(otype).lower(),
                "side": side,
                "qty": float(getattr(o, "qty", 0) or 0) or None,
                "stop_price": stop_px,
                "limit_price": limit_px,
                "is_stop": self._is_protective_stop(o),
            })
        return by_symbol

    @staticmethod
    def _order_num(order: Any, name: str) -> float | None:
        raw = getattr(order, name, None)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _order_iso(value: Any) -> str | None:
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:
                return None
        text = str(value).strip()
        return text or None

    def serialize_blotter_order(self, order: Any) -> dict[str, Any]:
        """One working or closed order as the blotter page expects it."""
        otype = self._enum_name(getattr(order, "type", ""), "").lower()
        status = self._enum_name(getattr(order, "status", ""), "").lower()
        qty = self._order_num(order, "qty")
        filled_qty = self._order_num(order, "filled_qty") or 0.0
        notional = self._order_num(order, "notional")
        is_stop = otype in {"stop", "stop_limit", "trailing_stop"}
        is_cancelable = bool(status) and status not in _NON_CANCELABLE_STATES
        is_replaceable = (
            bool(status)
            and status not in _NON_CANCELABLE_STATES
            and otype in _REPLACEABLE_ORDER_TYPES
            and notional is None
        )
        return {
            "id": str(getattr(order, "id", "") or ""),
            "client_order_id": str(getattr(order, "client_order_id", "") or "") or None,
            "symbol": str(getattr(order, "symbol", "") or "").upper(),
            "side": self._enum_name(getattr(order, "side", ""), "").lower(),
            "type": otype,
            "order_class": self._enum_name(
                getattr(order, "order_class", ""), "simple"
            ).lower(),
            "status": status,
            "qty": qty,
            "filled_qty": filled_qty,
            "filled_avg_price": self._order_num(order, "filled_avg_price"),
            "notional": notional,
            "limit_price": self._order_num(order, "limit_price"),
            "stop_price": self._order_num(order, "stop_price"),
            "trail_percent": self._order_num(order, "trail_percent"),
            "trail_price": self._order_num(order, "trail_price"),
            "time_in_force": self._enum_name(
                getattr(order, "time_in_force", ""), ""
            ).lower(),
            "extended_hours": bool(getattr(order, "extended_hours", False)),
            "submitted_at": self._order_iso(getattr(order, "submitted_at", None)),
            "updated_at": self._order_iso(getattr(order, "updated_at", None)),
            "filled_at": self._order_iso(getattr(order, "filled_at", None)),
            "canceled_at": self._order_iso(getattr(order, "canceled_at", None)),
            "is_stop": is_stop,
            "is_cancelable": is_cancelable,
            "is_replaceable": is_replaceable,
        }

    def list_orders(
        self,
        *,
        status: str = "open",
        symbols: list[str] | None = None,
        side: str = "",
        limit: int = 200,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Flat blotter rows for open or recently closed orders."""
        if not getattr(self, "trading", None):
            return []
        key = str(status or "open").strip().lower()
        query_status = _BLOTTER_STATUS.get(key)
        if query_status is None:
            raise ValueError("status must be open or closed")
        requested = max(1, min(int(limit or 200), 500))
        symbols_clean = [s for s in (symbols or []) if s]
        side_key = str(side or "").strip().lower()
        req_kwargs: dict[str, Any] = {
            "status": query_status,
            "limit": requested,
            "direction": "desc",
            # A blotter needs every working leg as its own actionable row.
            # nested=True rolls bracket/OCO children under their parent, and the
            # serializer intentionally has no nested `legs` collection.
            "nested": False,
        }
        if symbols_clean:
            req_kwargs["symbols"] = symbols_clean
        if side_key in {"buy", "sell"}:
            req_kwargs["side"] = OrderSide.BUY if side_key == "buy" else OrderSide.SELL
        # A closed blotter with no window is "the last N tickets", which says
        # nothing about *when*. The window makes that answerable.
        if after is not None:
            req_kwargs["after"] = after
        if until is not None:
            req_kwargs["until"] = until
        req = GetOrdersRequest(**req_kwargs)
        orders = list(self.trading.get_orders(req) or [])
        rows = [self.serialize_blotter_order(o) for o in orders]
        return [row for row in rows if row.get("id") and row.get("symbol")]

    def _open_orders(self, symbol: str):
        req = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol],
            limit=50,
        )
        return list(self.trading.get_orders(req) or [])

    @staticmethod
    def _is_protective_stop(order) -> bool:
        """Resting stop (sell below a long, or buy above a short) — ignore for entry blocks."""
        try:
            otype = order.type
        except Exception:
            return False
        return otype in {OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP}

    def has_open_orders(self, symbol: str) -> bool:
        """True when non-stop open orders exist (entries / limit exits / etc.)."""
        return any(not self._is_protective_stop(o) for o in self._open_orders(symbol))

    def has_open_stop_sell(self, symbol: str) -> bool:
        return any(self._is_protective_stop(o) for o in self._open_orders(symbol))

    def cancel_open_stop_orders(self, symbol: str) -> int:
        cancelled = 0
        for order in self._open_orders(symbol):
            if not self._is_protective_stop(order):
                continue
            try:
                self.trading.cancel_order_by_id(order.id)
                cancelled += 1
            except Exception:
                continue
        return cancelled

    def cancel_order(self, order_id: str) -> bool:
        """Cancel one open order by id. False when the broker refuses it."""
        order_id = str(order_id or "").strip()
        if not order_id:
            raise ValueError("Order id is required")
        self.trading.cancel_order_by_id(order_id)
        return True

    def cancel_all_open_orders(self) -> dict[str, Any]:
        """Cancel every open order through Alpaca's account-wide bulk endpoint."""
        if not getattr(self, "trading", None):
            return {"cancelled": 0, "failed": 0, "errors": []}
        responses = list(self.trading.cancel_orders() or [])
        cancelled = 0
        failed = 0
        errors: list[dict[str, str]] = []
        for response in responses:
            if isinstance(response, dict):
                oid = str(response.get("id") or "")
                status = int(response.get("status") or 0)
                body = response.get("body")
            else:
                oid = str(getattr(response, "id", "") or "")
                status = int(getattr(response, "status", 0) or 0)
                body = getattr(response, "body", None)
            if 200 <= status < 300:
                cancelled += 1
            else:
                failed += 1
                errors.append({"id": oid, "error": str(body or f"HTTP {status}")})
        return {"cancelled": cancelled, "failed": failed, "errors": errors}

    def replace_order(
        self,
        order_id: str,
        *,
        qty: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        time_in_force: str | None = None,
        trail: float | None = None,
    ) -> dict[str, Any]:
        """Patch a resting limit/stop. Notional and fractional qty cannot change."""
        order_id = str(order_id or "").strip()
        if not order_id:
            raise ValueError("Order id is required")
        if (
            qty is None
            and limit_price is None
            and stop_price is None
            and not time_in_force
            and trail is None
        ):
            raise ValueError(
                "Give a quantity, price, trail, or time-in-force to replace"
            )
        if not getattr(self, "trading", None):
            raise ValueError("Trading client is not connected")

        order = self.trading.get_order_by_id(order_id)
        otype = self._enum_name(getattr(order, "type", ""), "").lower()
        status = self._enum_name(getattr(order, "status", ""), "").lower()
        notional = self._order_num(order, "notional")
        current_qty = self._order_num(order, "qty")

        if status in _NON_REPLACEABLE_STATES and status not in _REWRITE_INSTEAD_OF_PATCH:
            raise ValueError("That order can no longer be replaced")
        if otype not in _REPLACEABLE_ORDER_TYPES:
            raise ValueError(
                "Only limit, stop, stop-limit, and trailing-stop orders can be replaced"
            )
        if notional is not None:
            raise ValueError(
                "Notional orders cannot be replaced — cancel and submit a new ticket"
            )
        if qty is not None:
            if qty <= 0:
                raise ValueError("Replacement quantity must be greater than zero")
            if abs(qty - round(qty)) > 1e-9:
                raise ValueError("Replacement quantity must be a whole number of shares")
            if current_qty is not None and abs(current_qty - int(current_qty)) > 1e-9:
                raise ValueError(
                    "Fractional share quantity cannot be changed on replace — "
                    "cancel and submit a new ticket"
                )

        if status in _REWRITE_INSTEAD_OF_PATCH:
            return self._rewrite_resting_order(
                order,
                qty=qty,
                limit_price=limit_price,
                stop_price=stop_price,
                time_in_force=time_in_force,
                trail=trail,
            )

        tif = None
        if time_in_force:
            tif = normalize_time_in_force(time_in_force)

        kwargs: dict[str, Any] = {}
        if qty is not None:
            kwargs["qty"] = int(round(qty))
        if limit_price is not None:
            kwargs["limit_price"] = limit_price
        if stop_price is not None:
            kwargs["stop_price"] = stop_price
        if tif is not None:
            kwargs["time_in_force"] = tif
        if trail is not None:
            kwargs["trail"] = trail
        req = ReplaceOrderRequest(**kwargs)
        try:
            replaced = self.trading.replace_order_by_id(order_id, req)
        except TypeError:
            replaced = self.trading.replace_order_by_id(order_id, order_data=req)
        except Exception as exc:
            raise ValueError(f"Could not replace order: {exc}") from exc
        return self.serialize_blotter_order(replaced)

    def _rewrite_resting_order(
        self,
        order: Any,
        *,
        qty: float | None,
        limit_price: float | None,
        stop_price: float | None,
        time_in_force: str | None,
        trail: float | None,
    ) -> dict[str, Any]:
        """Cancel an accepted/pending ticket and submit the edited one.

        Alpaca will not PATCH those early states, but the desk can still
        change price or qty by replacing the resting ticket in two steps.
        """
        filled = self._order_num(order, "filled_qty") or 0.0
        if filled > 0:
            raise ValueError(
                "This order already has a fill. Wait until it is working, "
                "or cancel and submit a new ticket."
            )
        order_id = str(getattr(order, "id", "") or "")
        symbol = str(getattr(order, "symbol", "") or "").upper()
        otype = self._enum_name(getattr(order, "type", ""), "").lower()
        side_name = self._enum_name(getattr(order, "side", ""), "").lower()
        side = OrderSide.SELL if side_name == "sell" else OrderSide.BUY
        current_qty = self._order_num(order, "qty")
        new_qty = float(qty) if qty is not None else current_qty
        if new_qty is None or new_qty <= 0:
            raise ValueError("Replacement quantity must be greater than zero")
        intent = self._enum_name(getattr(order, "position_intent", ""), "").lower()
        short_entry = side == OrderSide.SELL and "short" in intent
        new_limit = (
            limit_price
            if limit_price is not None
            else self._order_num(order, "limit_price")
        )
        new_stop = (
            stop_price if stop_price is not None else self._order_num(order, "stop_price")
        )
        new_tif = time_in_force or self._enum_name(
            getattr(order, "time_in_force", ""), "day"
        )
        trail_percent = self._order_num(order, "trail_percent")
        trail_price = self._order_num(order, "trail_price")
        if trail is not None:
            if trail_percent is None and trail_price is not None:
                trail_price = trail
                trail_percent = None
            else:
                trail_percent = trail
                trail_price = None
        try:
            self.cancel_order(order_id)
        except Exception as exc:
            raise ValueError(
                f"Could not cancel the accepted order to edit it: {exc}"
            ) from exc
        try:
            submitted, _ = self.submit_manual_order(
                symbol,
                new_qty,
                side,
                order_type=otype,
                limit_price=new_limit,
                stop_price=new_stop,
                trail_percent=trail_percent,
                trail_price=trail_price,
                time_in_force=new_tif,
                extended_hours=bool(getattr(order, "extended_hours", False)),
                stop_loss_pct=0.0,
                short_entry=short_entry,
            )
        except Exception as exc:
            raise ValueError(
                "The original order was cancelled, but the replacement "
                f"could not be sent: {exc}"
            ) from exc
        return self.serialize_blotter_order(submitted)

    def get_order_snapshot(self, order_id: str) -> dict[str, Any]:
        """What actually happened to an order — status, fill, and price.

        Acceptance is not a fill: a market order can still be rejected, and a
        limit order can rest all day. The ticket needs the terminal state.
        """
        order_id = str(order_id or "").strip()
        if not order_id:
            raise ValueError("Order id is required")
        if not is_alpaca_order_id(order_id):
            raise ValueError("Order id is not a valid Alpaca order id")
        # Bracket/OTO child ids are what stop-following automations need.
        # Alpaca omits them unless the parent is requested as nested.
        order = self.trading.get_order_by_id(
            order_id, GetOrderByIdRequest(nested=True)
        )

        def _f(name: str) -> float | None:
            raw = getattr(order, name, None)
            if raw in (None, ""):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        status = self._enum_name(getattr(order, "status", ""), "").lower()
        legs = self.exit_leg_ids(order)
        replaced_by = str(getattr(order, "replaced_by", "") or "").strip()
        return {
            "id": str(getattr(order, "id", order_id)),
            "symbol": str(getattr(order, "symbol", "") or "").upper(),
            "status": status,
            "is_terminal": status in _TERMINAL_ORDER_STATES,
            "side": self._enum_name(getattr(order, "side", ""), "").lower(),
            "type": self._enum_name(getattr(order, "type", ""), "").lower(),
            "qty": _f("qty"),
            "filled_qty": _f("filled_qty") or 0.0,
            "filled_avg_price": _f("filled_avg_price"),
            "limit_price": _f("limit_price"),
            "stop_price": _f("stop_price"),
            "replaced_by": replaced_by or None,
            "stop_order_id": legs.get("stop_order_id"),
            "take_profit_order_id": legs.get("take_profit_order_id"),
        }

    def exit_leg_ids(self, order: Any) -> dict[str, str | None]:
        """Stop and take-profit child ids on an OTO/bracket parent, if present.

        Alpaca sometimes omits ``legs`` on the submit response and only fills
        them in on a later GET, so callers must be ready for both to be None.
        """
        stop_id: str | None = None
        tp_id: str | None = None
        for leg in getattr(order, "legs", None) or []:
            otype = self._enum_name(getattr(leg, "type", ""), "").lower()
            oid = str(getattr(leg, "id", "") or "").strip()
            if not oid:
                continue
            if otype in {"stop", "stop_limit", "trailing_stop"}:
                stop_id = oid
            elif otype == "limit":
                tp_id = oid
        return {"stop_order_id": stop_id, "take_profit_order_id": tp_id}

    def open_protective_stop_id(self, symbol: str) -> str | None:
        """Id of the resting protective stop on ``symbol``, if one is open."""
        for order in self._open_orders(symbol):
            if self._is_protective_stop(order):
                oid = str(getattr(order, "id", "") or "").strip()
                if oid:
                    return oid
        return None

    def cancel_open_orders_for_symbol(self, symbol: str) -> int:
        """Cancel every open order for `symbol` (stops, limits, and entries)."""
        return len(self.cancel_open_order_ids_for_symbol(symbol))

    def cancel_open_order_ids_for_symbol(self, symbol: str) -> list[str]:
        """Cancel a symbol's open orders and return only ids that succeeded."""
        cancelled: list[str] = []
        for order in self._open_orders(symbol):
            order_id = str(getattr(order, "id", "") or "").strip()
            if not order_id:
                continue
            try:
                self.trading.cancel_order_by_id(order_id)
                cancelled.append(order_id)
            except Exception:
                continue
        return cancelled

    @staticmethod
    def _close_field(obj: Any, *names: str) -> str:
        for name in names:
            if obj is None:
                continue
            if isinstance(obj, dict):
                val = obj.get(name)
            else:
                val = getattr(obj, name, None)
            if val not in (None, ""):
                return str(val)
        return ""

    def _normalize_close_response(self, res: Any, symbol: str = "") -> dict[str, Any]:
        body = getattr(res, "body", None) if res is not None else None
        return {
            "symbol": (
                self._close_field(res, "symbol")
                or self._close_field(body, "symbol")
                or symbol
            ),
            "order_id": (
                self._close_field(res, "order_id", "id")
                or self._close_field(body, "id", "order_id")
            ),
            "status": self._close_field(res, "status") or "submitted",
            "body": body,
        }

    def stop_price_for_entry(
        self, entry_price: float, pct: float | None = None, *, short: bool = False
    ) -> float | None:
        if pct is None:
            pct = float(getattr(self.config, "stop_loss_pct", 0) or 0)
        else:
            pct = float(pct)
        if pct <= 0 or entry_price <= 0:
            return None
        if short:
            stop = normalize_stock_order_price(
                entry_price * (1.0 + pct / 100.0), field="stop_price"
            )
            min_stop = normalize_stock_order_price(
                entry_price + 0.01, field="stop_price"
            )
            if stop <= entry_price:
                stop = min_stop
            return stop
        # Alpaca requires stop at least $0.01 below the base/entry reference.
        if entry_price <= 0.01:
            return None
        max_stop = normalize_stock_order_price(
            entry_price - 0.01, field="stop_price"
        )
        raw = entry_price * (1.0 - pct / 100.0)
        stop: float | None = None
        if raw > 0:
            try:
                stop = normalize_stock_order_price(raw, field="stop_price")
            except ValueError:
                stop = None
        if stop is None or stop <= 0:
            # ATR distance can exceed the price on cheap/volatile names. Park
            # the stop at a tick above zero instead of dropping protection.
            floor = 0.0001 if entry_price < 1 else 0.01
            stop = normalize_stock_order_price(floor, field="stop_price")
        if stop >= entry_price:
            stop = max_stop
        if stop <= 0:
            return None
        return stop

    def ensure_stop_loss(
        self, symbol: str, pct: float | None = None, *, qty: float | None = None
    ) -> dict[str, Any] | None:
        """Place a GTC protective stop when a position is open and none is resting.

        Long: stop sell below entry. Short: stop buy above entry.
        Used after extended-hours entries (OTO not supported) and to re-arm if a
        prior stop was cancelled.

        ``qty`` covers only part of the position. It exists for the partial-exit
        case: shares already committed to a resting exit order are not available
        to a stop, so covering the whole position would have the broker reject
        the stop for insufficient quantity. Capped at the position size.
        """
        if pct is None:
            pct = float(getattr(self.config, "stop_loss_pct", 0) or 0)
        else:
            pct = float(pct)
        if pct <= 0:
            return None
        position_qty = self.get_position_qty(symbol)
        if position_qty == 0:
            return None
        is_short = position_qty < 0
        abs_qty = abs(position_qty)
        if qty is not None:
            requested = abs(float(qty))
            if requested <= 0:
                return None
            abs_qty = min(abs_qty, requested)
        if self.has_open_stop_sell(symbol):
            return None
        avg = self.get_avg_entry_price(symbol)
        if avg is None:
            try:
                avg = float(self.get_mark_price(symbol)["price"])
            except Exception:
                return None
        stop_price = self.stop_price_for_entry(avg, pct=pct, short=is_short)
        if stop_price is None:
            return None
        # Whole shares for stop orders; Alpaca does not short fractionals.
        if is_short:
            stop_qty = float(int(abs_qty))
            if stop_qty < 1:
                return None
        else:
            stop_qty = float(int(abs_qty)) if abs_qty >= 1 else float(abs_qty)
            if stop_qty <= 0:
                return None
        offset = float(getattr(self.config, "stop_limit_offset_pct", 0) or 0)
        limit = limit_price_for_stop(stop_price, offset, short=is_short)
        common = {
            "symbol": symbol,
            "qty": stop_qty,
            "side": OrderSide.BUY if is_short else OrderSide.SELL,
            "time_in_force": TimeInForce.GTC,
            "stop_price": stop_price,
        }
        if limit is not None:
            order = StopLimitOrderRequest(**common, limit_price=limit)
        else:
            order = StopOrderRequest(**common)
        submitted = self.trading.submit_order(order)
        info = {
            "id": str(submitted.id),
            "stop_price": stop_price,
            "qty": stop_qty,
            "pct": pct,
            "side": "buy" if is_short else "sell",
        }
        if limit is not None:
            info["limit_price"] = limit
            info["offset_pct"] = offset
        return info

    def recent_activity(self, symbol: str, *, lookback_minutes: int = 1440) -> dict[str, Any]:
        """Last fill and last stop-out for `symbol`, derived from closed orders.

        Reading this from Alpaca rather than local state keeps the min-hold and
        cooldown guards correct across restarts and across desks.
        """
        now = datetime.now(timezone.utc)
        after = now - timedelta(minutes=max(1, int(lookback_minutes)))
        try:
            orders = self.list_closed_orders(after=after, symbols=[symbol], limit=100)
        except Exception:
            return {"last_fill_age_min": None, "stop_out_age_min": None}

        def _age(row: dict[str, Any]) -> float | None:
            stamp = row.get("filled_at") or row.get("submitted_at")
            if not stamp:
                return None
            try:
                ts = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError:
                return None
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return max(0.0, (now - ts).total_seconds() / 60.0)

        last_age: float | None = None
        last_side: str | None = None
        stop_age: float | None = None
        for row in orders:
            if str(row.get("status") or "") != "filled":
                continue
            age = _age(row)
            if age is None:
                continue
            if last_age is None or age < last_age:
                last_age, last_side = age, str(row.get("side") or "")
            if "stop" in str(row.get("type") or "") and (
                stop_age is None or age < stop_age
            ):
                stop_age = age
        return {
            "last_fill_age_min": round(last_age, 1) if last_age is not None else None,
            "last_fill_side": last_side,
            "stop_out_age_min": round(stop_age, 1) if stop_age is not None else None,
        }

    def current_stop_price(self, symbol: str) -> float | None:
        """Stop price of the resting protective order, if any."""
        prices = []
        for order in self._open_orders(symbol):
            if not self._is_protective_stop(order):
                continue
            try:
                price = float(getattr(order, "stop_price", 0) or 0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                prices.append(price)
        return max(prices) if prices else None

    def replace_stop_loss(self, symbol: str, stop_price: float) -> dict[str, Any] | None:
        """Cancel resting protective stops and re-arm at `stop_price`.

        Used by the trailing manager — ``ensure_stop_loss`` deliberately refuses
        to touch an existing stop, so moving one needs this explicit path.
        """
        qty = self.get_position_qty(symbol)
        if qty == 0:
            return None
        try:
            stop_price = normalize_stock_order_price(stop_price, field="stop_price")
        except ValueError:
            return None
        is_short = qty < 0
        abs_qty = abs(qty)
        stop_qty = float(int(abs_qty)) if (is_short or abs_qty >= 1) else float(abs_qty)
        if stop_qty <= 0:
            return None
        self.cancel_open_stop_orders(symbol)
        offset = float(getattr(self.config, "stop_limit_offset_pct", 0) or 0)
        limit = limit_price_for_stop(stop_price, offset, short=is_short)
        common = {
            "symbol": symbol,
            "qty": stop_qty,
            "side": OrderSide.BUY if is_short else OrderSide.SELL,
            "time_in_force": TimeInForce.GTC,
            "stop_price": stop_price,
        }
        if limit is not None:
            order = StopLimitOrderRequest(**common, limit_price=limit)
        else:
            order = StopOrderRequest(**common)
        submitted = self.trading.submit_order(order)
        info = {
            "id": str(submitted.id),
            "stop_price": stop_price,
            "qty": stop_qty,
            "side": "buy" if is_short else "sell",
        }
        if limit is not None:
            info["limit_price"] = limit
            info["offset_pct"] = offset
        return info

    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        *,
        protect: bool | None = None,
        stop_price: float | None = None,
    ):
        """Place an order that can fill in regular, extended, or overnight sessions.

        Regular hours: market DAY order (OTO + stop-loss when configured).
        Outside RTH: limit DAY + extended_hours=True (Alpaca requirement).

        ``protect``: True attaches an OTO stop (long sell-stop or short buy-stop).
        None keeps the legacy rule — OTO only on buys. False never attaches
        (use when covering a short so a leftover sell-stop does not re-short).
        ``stop_price``: explicit protective level (the AI desk sizes it from ATR);
        omit to derive it from ``stop_loss_pct``.
        """
        if qty <= 0:
            raise ValueError("Order qty must be positive")

        session_info = self.market_session()
        session = session_info["session"]
        stop_pct = float(getattr(self.config, "stop_loss_pct", 0) or 0)
        has_stop_level = stop_pct > 0 or (stop_price or 0) > 0
        if protect is None:
            attach_stop = side == OrderSide.BUY and has_stop_level
            short_stop = False
        else:
            attach_stop = bool(protect) and has_stop_level
            short_stop = side == OrderSide.SELL

        if attach_stop:
            qty, attach_stop = whole_qty_for_attached_stop(qty)

        if session_info["is_open"]:
            kwargs: dict[str, Any] = {
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "time_in_force": TimeInForce.DAY,
            }
            if attach_stop:
                level = (
                    normalize_stock_order_price(stop_price, field="stop_price")
                    if stop_price
                    else None
                )
                if level is None:
                    mark = self.get_mark_price(symbol)["price"]
                    level = self.stop_price_for_entry(float(mark), short=short_stop)
                if level is not None and level > 0:
                    kwargs["order_class"] = OrderClass.OTO
                    kwargs["stop_loss"] = stop_loss_request_for(
                        level,
                        getattr(self.config, "stop_limit_offset_pct", 0),
                        short=short_stop,
                    )
            order = MarketOrderRequest(**kwargs)
            return self.trading.submit_order(order)

        if session == "closed":
            raise ValueError(
                "Market is closed (weekend). Orders resume Sunday 8:00 PM ET overnight."
            )

        # Extended / overnight / pre-market: limit + extended_hours only.
        # Bracket/OTO are not supported with extended_hours — arm stop after fill.
        whole_qty = int(qty)  # truncate toward zero; never round up size
        if whole_qty < 1:
            raise ValueError(
                "Outside regular hours Alpaca needs whole-share limit orders "
                "(qty ≥ 1)."
            )
        if float(qty) != float(whole_qty):
            # Keep behavior explicit so callers know size was reduced.
            qty = float(whole_qty)

        mark = self.get_mark_price(symbol)
        raw = mark["price"]
        if side == OrderSide.BUY:
            limit_price = round(raw * _BUY_SLIPPAGE, 2)
        else:
            limit_price = round(raw * _SELL_SLIPPAGE, 2)

        order = LimitOrderRequest(
            symbol=symbol,
            qty=float(whole_qty),
            side=side,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            extended_hours=True,
        )
        return self.trading.submit_order(order)

    def submit_market_order(self, symbol: str, qty: float, side: OrderSide):
        """Back-compat alias — routes through session-aware submit_order."""
        return self.submit_order(symbol, qty, side)

    def submit_manual_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        *,
        order_type: str = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
        trail_percent: float | None = None,
        trail_price: float | None = None,
        time_in_force: str = "day",
        extended_hours: bool = False,
        stop_loss_pct: float | None = None,
        stop_limit_offset_pct: float | None = None,
        stop_limit_price: float | None = None,
        take_profit_price: float | None = None,
        short_entry: bool = False,
        client_order_id: str | None = None,
    ) -> tuple[Any, dict[str, Any] | None]:
        """Place a user-directed order of any supported type, optionally protected.

        Returns ``(submitted_order, attached_exit_info_or_None)``.
        The attached-exit info is None for unprotected or exit orders.
        Protected conditional and extended-hours entries are rejected because
        Alpaca cannot attach the promised OTO/bracket protection to them.

        A protected long or short entry uses OrderClass.OTO, or BRACKET when a
        take-profit rides along too. Outside RTH, any non-extended order queues
        for regular hours; setting ``extended_hours`` makes a Limit DAY/GTC
        order eligible immediately.
        """
        if qty <= 0:
            raise ValueError("Order qty must be positive")
        symbol = symbol.upper().strip()
        if not symbol:
            raise ValueError("Symbol is required")

        otype = str(order_type or "market").strip().lower()
        if otype not in MANUAL_ORDER_TYPES:
            raise ValueError(
                "order_type must be one of: " + ", ".join(sorted(MANUAL_ORDER_TYPES))
            )
        tif = normalize_time_in_force(time_in_force)
        client_id = str(client_order_id or "").strip()
        if len(client_id) > 128:
            raise ValueError("client_order_id must be at most 128 characters")

        if otype in {"stop", "stop_limit", "trailing_stop"} and tif not in {
            TimeInForce.DAY,
            TimeInForce.GTC,
        }:
            raise ValueError(
                f"{otype.replace('_', ' ').capitalize()} orders only support "
                "DAY or GTC time in force."
            )

        if stop_loss_pct is None:
            stop_pct = float(getattr(self.config, "stop_loss_pct", 0) or 0)
        else:
            stop_pct = float(stop_loss_pct)
        # Flat ``stop_loss_pct`` on the API is capped at 50%. ATR-derived
        # distances are converted to a percent internally and can be wider on
        # cheap/volatile names — rejecting those would send the entry naked.
        if stop_pct < 0:
            raise ValueError("stop_loss_pct must be at least 0")

        if stop_limit_offset_pct is None:
            stop_limit_offset = float(
                getattr(self.config, "stop_limit_offset_pct", 0) or 0
            )
        else:
            stop_limit_offset = float(stop_limit_offset_pct)
        if stop_limit_offset < 0 or stop_limit_offset > 50:
            raise ValueError("stop_limit_offset_pct must be between 0 and 50")

        exit_limit: float | None = None
        if stop_limit_price is not None and float(stop_limit_price) > 0:
            exit_limit = normalize_stock_order_price(
                stop_limit_price, field="stop_limit_price"
            )

        session_info = self.market_session()
        session = session_info["session"]
        if session == "closed":
            raise ValueError(
                "Market is closed (weekend). Orders resume Sunday 8:00 PM ET overnight."
            )

        # An attached exit turns the entry into an OTO/bracket, and Alpaca only
        # accepts those on a simple market or limit entry. A stop or trailing
        # entry cannot also carry the promised protective OTO/bracket.
        attach_stop = (
            (side == OrderSide.BUY or short_entry)
            and stop_pct > 0
            and otype in ATTACHABLE_ENTRY_TYPES
        )
        if (side == OrderSide.BUY or short_entry) and stop_pct > 0 and not attach_stop:
            raise ValueError(
                "Protected entries must use Market or Limit. Alpaca cannot attach "
                "an OTO/bracket stop to this conditional order type."
            )
        oto_stop: dict[str, Any] | None = None
        if attach_stop:
            if tif not in {TimeInForce.DAY, TimeInForce.GTC}:
                raise ValueError(
                    "Protected OTO/bracket entries only support DAY or GTC time in force."
                )
            if extended_hours:
                raise ValueError(
                    "Protective OTO/bracket exits cannot execute in extended hours. "
                    "Turn off extended-hours fills to queue the protected entry for RTH."
                )
            qty, attach_stop = whole_qty_for_attached_stop(qty)
            if not attach_stop:
                raise ValueError(
                    "A protected entry needs at least 1 whole share so its "
                    "OTO/bracket stop can attach."
                )

        fractional_qty = qty != float(int(qty))
        if short_entry and fractional_qty:
            raise ValueError("Alpaca does not support fractional short sales.")
        if fractional_qty and tif is not TimeInForce.DAY:
            raise ValueError("Fractional stock orders must use DAY time in force.")

        # Outside RTH a non-extended ticket of any type is accepted and rests
        # until regular hours. Alpaca only fills Market/Stop/Trailing in RTH;
        # Limit + extended_hours is the path that can work the current session.

        if not extended_hours:
            kwargs: dict[str, Any] = {
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "time_in_force": tif,
            }
            if client_id:
                kwargs["client_order_id"] = client_id
            if attach_stop:
                if otype == "limit" and limit_price is not None and limit_price > 0:
                    entry_ref = float(limit_price)
                else:
                    entry_ref = float(self.get_mark_price(symbol)["price"])
                attached_stop_price = self.stop_price_for_entry(
                    entry_ref, pct=stop_pct, short=short_entry
                )
                if attached_stop_price is not None:
                    if exit_limit is not None:
                        # Absolute sell/cover limit — may sit at the stop, never
                        # past it the wrong way for the side.
                        limit_exit = normalize_stop_exit_limit(
                            attached_stop_price,
                            exit_limit,
                            short=short_entry,
                            clamp=False,
                        )
                    else:
                        limit_exit = limit_price_for_stop(
                            attached_stop_price,
                            stop_limit_offset,
                            short=short_entry,
                        )
                    kwargs["order_class"] = OrderClass.OTO
                    kwargs["stop_loss"] = stop_loss_request_for(
                        attached_stop_price,
                        stop_limit_offset,
                        short=short_entry,
                        limit_price=limit_exit,
                    )
                    oto_stop = {
                        "stop_price": attached_stop_price,
                        "pct": stop_pct,
                        "attached": "oto",
                    }
                    if limit_exit is not None:
                        oto_stop["limit_price"] = limit_exit
                        if exit_limit is None:
                            oto_stop["offset_pct"] = stop_limit_offset
                    # A target only exists as the other leg of a bracket — the
                    # two exits then cancel each other when one fills.
                    target = (
                        normalize_stock_order_price(
                            take_profit_price, field="take_profit_price"
                        )
                        if take_profit_price
                        else 0.0
                    )
                    target_is_valid = target < entry_ref if short_entry else target > entry_ref
                    if target_is_valid:
                        kwargs["order_class"] = OrderClass.BRACKET
                        kwargs["take_profit"] = TakeProfitRequest(limit_price=target)
                        oto_stop["take_profit_price"] = target
                        oto_stop["attached"] = "bracket"

            if otype == "market":
                submitted = self.trading.submit_order(MarketOrderRequest(**kwargs))
                return submitted, oto_stop

            if otype == "limit":
                if limit_price is None or limit_price <= 0:
                    raise ValueError("Limit price is required for limit orders")
                kwargs["limit_price"] = normalize_stock_order_price(
                    limit_price, field="limit_price"
                )
                submitted = self.trading.submit_order(LimitOrderRequest(**kwargs))
                return submitted, oto_stop

            if otype == "trailing_stop":
                trail = self._trailing_kwargs(trail_percent, trail_price)
                kwargs.pop("order_class", None)
                kwargs.pop("stop_loss", None)
                kwargs.pop("take_profit", None)
                kwargs.update(trail)
                submitted = self.trading.submit_order(
                    TrailingStopOrderRequest(**kwargs)
                )
                return submitted, None

            if stop_price is None or float(stop_price) <= 0:
                raise ValueError("Stop price is required for stop and stop-limit orders")
            kwargs["stop_price"] = normalize_stock_order_price(
                stop_price, field="stop_price"
            )

            if otype == "stop":
                submitted = self.trading.submit_order(StopOrderRequest(**kwargs))
                return submitted, oto_stop

            # stop_limit
            if limit_price is None or float(limit_price) <= 0:
                raise ValueError("Stop-limit orders need both a stop and a limit price")
            kwargs["limit_price"] = normalize_stock_order_price(
                limit_price, field="limit_price"
            )
            submitted = self.trading.submit_order(StopLimitOrderRequest(**kwargs))
            return submitted, oto_stop

        # Extended / overnight / pre-market: limit + DAY/GTC only. Fractional
        # DAY limit orders are supported, so never truncate a quantity merely
        # because the order is extended-hours eligible.
        if otype != "limit":
            raise ValueError(
                f"{otype.replace('_', ' ').capitalize()} orders are not available "
                f"during {session}. Choose Limit and set a price (extended hours)."
            )
        if limit_price is None or limit_price <= 0:
            raise ValueError("Limit price is required outside regular hours")
        if tif not in {TimeInForce.DAY, TimeInForce.GTC}:
            raise ValueError(
                "Extended-hours orders must use DAY or GTC time in force."
            )
        kwargs = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "time_in_force": tif,
            "limit_price": normalize_stock_order_price(
                limit_price, field="limit_price"
            ),
            "extended_hours": True,
        }
        if client_id:
            kwargs["client_order_id"] = client_id
        order = LimitOrderRequest(**kwargs)
        submitted = self.trading.submit_order(order)
        return submitted, None

    @staticmethod
    def _trailing_kwargs(
        trail_percent: float | None, trail_price: float | None
    ) -> dict[str, Any]:
        """Exactly one trail dimension — Alpaca rejects an order carrying both."""
        pct = float(trail_percent or 0)
        amount = float(trail_price or 0)
        if pct > 0 and amount > 0:
            raise ValueError(
                "A trailing stop takes either a trail percent or a trail amount, "
                "not both."
            )
        if pct > 0:
            if pct > 50:
                raise ValueError("Trail percent must be between 0 and 50")
            return {"trail_percent": round(pct, 2)}
        if amount > 0:
            return {"trail_price": round(amount, 2)}
        raise ValueError(
            "A trailing stop needs a trail percent or a trail amount greater than 0"
        )

    def arm_trailing_stop(
        self,
        symbol: str,
        *,
        trail_percent: float | None = None,
        trail_price: float | None = None,
    ) -> dict[str, Any] | None:
        """Replace resting protection with a trailing stop over the whole position.

        Unlike ``ensure_stop_loss`` this deliberately clears what is already
        there: a fixed stop left beside a trailing one would double the exit
        size and leave a stray order behind after the first fill.
        """
        qty = self.get_position_qty(symbol)
        if qty == 0:
            return None
        trail = self._trailing_kwargs(trail_percent, trail_price)
        is_short = qty < 0
        stop_qty = float(int(abs(qty)))
        if stop_qty < 1:
            raise ValueError(
                "A trailing stop needs at least one whole share — Alpaca does "
                "not accept fractional stop orders."
            )
        self.cancel_open_stop_orders(symbol)
        order = TrailingStopOrderRequest(
            symbol=symbol.upper().strip(),
            qty=stop_qty,
            side=OrderSide.BUY if is_short else OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            **trail,
        )
        submitted = self.trading.submit_order(order)
        return {
            "id": str(submitted.id),
            "qty": stop_qty,
            "side": "buy" if is_short else "sell",
            "type": "trailing_stop",
            **trail,
        }

    def get_asset_info(self, symbol: str) -> dict[str, Any]:
        """Broker-side facts that decide whether a ticket can exist at all.

        Shortable, fractionable, and tradable are the difference between a
        rejection the user reads afterwards and a field the page can grey out
        beforehand.
        """
        symbol = str(symbol or "").upper().strip()
        if not symbol:
            return {}
        try:
            asset = self.trading.get_asset(symbol)
        except Exception:
            return {}

        def _b(name: str) -> bool:
            return bool(getattr(asset, name, False))

        status = self._enum_name(getattr(asset, "status", ""), "").lower()
        return {
            "symbol": str(getattr(asset, "symbol", symbol) or symbol).upper(),
            "name": str(getattr(asset, "name", "") or ""),
            "exchange": self._enum_name(getattr(asset, "exchange", None)),
            "asset_class": self._enum_name(
                getattr(asset, "asset_class", None), "us_equity"
            ),
            "status": status,
            "active": status == "active",
            "tradable": _b("tradable"),
            "shortable": _b("shortable"),
            "easy_to_borrow": _b("easy_to_borrow"),
            "fractionable": _b("fractionable"),
            "marginable": _b("marginable"),
        }

    def get_bars(
        self, symbol: str, limit: int, timeframe: str | None = None
    ) -> pd.DataFrame:
        """Return the most recent `limit` bars (not the oldest in a window).

        `timeframe` overrides the configured one — used to pull a daily trend
        read alongside intraday bars.
        """
        tf_key = (timeframe or self.config.bar_timeframe or "1Day").strip()
        tf = _TIMEFRAME_MAP.get(tf_key, TimeFrame.Day)
        end = datetime.now(timezone.utc)
        if tf_key == "1Day":
            lookback = timedelta(days=max(limit * 3, 90))
        elif tf_key == "1Hour":
            lookback = timedelta(days=max(limit // 4, 14))
        else:
            # Intraday: need enough calendar time for `limit` bars + gaps.
            minutes = {"1Min": 1, "5Min": 5, "15Min": 15}.get(tf_key, 1)
            lookback = timedelta(minutes=max(limit * minutes * 3, 60 * 24))
        start = end - lookback
        df = self._fetch_bars(symbol, tf, start, end, timeframe_key=tf_key)
        # Stitch against the frame we actually asked for, not the configured one —
        # the daily higher-timeframe read is pulled while bar_timeframe is intraday.
        df = self._apply_live_mark_to_bars(df, symbol, timeframe_key=tf_key)
        if df.empty:
            return df
        return df.tail(limit).copy()

    def get_bars_range(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime | None = None,
        timeframe: str | None = None,
    ) -> pd.DataFrame:
        """Return bars from `start` through `end` (UTC-aware preferred)."""
        tf_key = (timeframe or self.config.bar_timeframe or "1Day").strip()
        tf = _TIMEFRAME_MAP.get(tf_key, TimeFrame.Day)
        end_dt = end or datetime.now(timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        if end_dt <= start:
            raise ValueError("end must be after start")
        return self._fetch_bars(symbol, tf, start, end_dt, timeframe_key=tf_key)

    def _fetch_bars(
        self,
        symbol: str,
        timeframe: TimeFrame,
        start: datetime,
        end: datetime,
        *,
        timeframe_key: str | None = None,
    ) -> pd.DataFrame:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            feed=_DEFAULT_FEED,
        )
        bars = self.data.get_stock_bars(request)
        df = bars.df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol)
        df = df.sort_index()
        key = timeframe_key or self.config.bar_timeframe
        # Historical 1Day bars often freeze close at the open. Drop that
        # incomplete row here; live get_bars() restitches the current mark.
        if key == "1Day" and not df.empty:
            df = self._drop_incomplete_daily_bar(df)
        return df.copy()

    def _drop_incomplete_daily_bar(
        self, df: pd.DataFrame, now: datetime | None = None
    ) -> pd.DataFrame:
        """Remove today's daily bar until the regular session has closed."""
        try:
            clock = self.trading.get_clock()
            now_et = (now or datetime.now(timezone.utc)).astimezone(_ET)
            market_closed_for_day = not clock.is_open and now_et.time() >= time(16, 0)
            if clock.is_open or not market_closed_for_day:
                last_session = bar_session_date(df.index[-1])
                if last_session is not None and last_session == equity_session_date(
                    now_et
                ):
                    return df.iloc[:-1].copy()
        except Exception:
            return df
        return df

    def _apply_live_mark_to_bars(
        self,
        df: pd.DataFrame,
        symbol: str,
        *,
        timeframe_key: str | None = None,
        mark: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        """Replace the in-progress bar close with the live mark (not the 09:30 open)."""
        key = timeframe_key or self.config.bar_timeframe
        try:
            mark = mark if mark is not None else self.get_mark_price(symbol)
        except Exception:
            return df
        try:
            price = float(mark["price"])
        except (TypeError, ValueError, KeyError):
            return df
        if price <= 0:
            return df

        session = str(mark.get("session") or "")
        if session == "closed":
            return df

        session_day = equity_session_date()
        last_session = bar_session_date(df.index[-1]) if not df.empty else None

        if key != "1Day":
            if (
                session in {"overnight", "premarket", "afterhours"}
                and not df.empty
                and last_session is not None
                and last_session != session_day
            ):
                return self._append_intraday_mark_bar(df, price, mark)
            return self._overlay_intraday_mark(df, price, mark)

        open_px = _finite_px(mark.get("daily_open")) or price
        high_px = max(
            _finite_px(mark.get("daily_high")) or price,
            price,
            open_px,
        )
        low_px = min(
            _finite_px(mark.get("daily_low")) or price,
            price,
            open_px,
        )
        volume = _finite_px(mark.get("daily_volume")) or 0.0
        ts = _session_bar_timestamp(mark.get("daily_ts"), session_day)

        row = {
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": price,
            "volume": volume,
        }
        if df.empty:
            return pd.DataFrame([row], index=pd.DatetimeIndex([ts]))

        out = df.copy()
        if last_session == session_day:
            i = -1
            for col, val in row.items():
                if col in out.columns:
                    out.iloc[i, out.columns.get_loc(col)] = val
            return out

        extra = {col: row.get(col, out.iloc[-1][col]) for col in out.columns}
        if getattr(out.index, "tz", None) is not None:
            if ts.tzinfo is None:
                ts = ts.tz_localize(out.index.tz)
            else:
                ts = ts.tz_convert(out.index.tz)
        elif ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return pd.concat([out, pd.DataFrame([extra], index=pd.DatetimeIndex([ts]))])

    @staticmethod
    def _overlay_intraday_mark(
        df: pd.DataFrame, price: float, _mark: dict[str, Any]
    ) -> pd.DataFrame:
        if df.empty:
            return df
        out = df.copy()
        i = -1
        if "close" in out.columns:
            out.iloc[i, out.columns.get_loc("close")] = price
        if "high" in out.columns:
            prev_high = _finite_px(out.iloc[i]["high"])
            out.iloc[i, out.columns.get_loc("high")] = max(prev_high or price, price)
        if "low" in out.columns:
            prev_low = _finite_px(out.iloc[i]["low"])
            out.iloc[i, out.columns.get_loc("low")] = min(prev_low or price, price)
        return out

    def _append_intraday_mark_bar(
        self, df: pd.DataFrame, price: float, mark: dict[str, Any]
    ) -> pd.DataFrame:
        """Start a new in-progress bar when the live mark is in a later session."""
        raw = mark.get("asof") or datetime.now(timezone.utc)
        ts = pd.Timestamp(raw)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        row = {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0.0,
        }
        out = df.copy()
        extra = {col: row.get(col, out.iloc[-1][col]) for col in out.columns}
        # `ts` is always tz-aware by here; match whatever the frame's index uses
        # so the concat does not raise on mixed awareness.
        if getattr(out.index, "tz", None) is not None:
            ts = ts.tz_convert(out.index.tz)
        else:
            ts = ts.tz_localize(None)
        return pd.concat([out, pd.DataFrame([extra], index=pd.DatetimeIndex([ts]))])

    def market_session(self, now: datetime | None = None) -> dict[str, Any]:
        """Classify US equity session in America/New_York."""
        clock = self.trading.get_clock()
        et_now = (now or datetime.now(timezone.utc)).astimezone(_ET)
        weekday = et_now.weekday()  # Mon=0 … Sun=6
        t = et_now.time()

        if clock.is_open:
            session = "regular"
        elif weekday == 5 or (weekday == 6 and t < time(20, 0)):
            # Sat all day, or Sun before overnight opens at 20:00 ET
            session = "closed"
        elif t >= time(16, 0) and t < time(20, 0):
            session = "afterhours"
        elif t >= time(20, 0) or t < time(4, 0):
            session = "overnight"
        elif t >= time(4, 0) and t < time(9, 30):
            session = "premarket"
        else:
            session = "closed"

        return {
            "session": session,
            "is_open": bool(clock.is_open),
            "timestamp_et": et_now.isoformat(),
            "next_open": clock.next_open.isoformat() if clock.next_open else None,
            "next_close": clock.next_close.isoformat() if clock.next_close else None,
        }

    def get_mark_price(self, symbol: str) -> dict[str, Any]:
        """Best available mark: prefer fresh Alpaca (inc. overnight) over stale scrapes."""
        symbol = symbol.upper().strip()
        cached = self._mark_cache.get(symbol)
        now_mono = monotonic()
        if cached and cached[0] > now_mono:
            return dict(cached[1])

        session_info = self.market_session()
        alpaca = self._alpaca_mark(symbol, session=session_info["session"])

        live = None
        if not self._alpaca_mark_is_fresh(alpaca):
            try:
                live = fetch_live_mark(symbol)
            except Exception:
                live = None

        chosen = self._pick_fresher_mark(alpaca, live)
        if chosen is None:
            raise ValueError(f"No quote available for {symbol}")

        payload = self._mark_payload(symbol, session_info, alpaca, live, chosen)
        self._mark_cache[symbol] = (now_mono + _MARK_CACHE_TTL, payload)
        return dict(payload)

    def get_mark_prices(
        self, symbols: list[str], *, scrape_fallback: bool = True
    ) -> dict[str, dict[str, Any]]:
        """Marks for a whole watchlist, batching the Alpaca snapshot call.

        Symbols still inside the mark cache are served from it, so a UI polling
        this every few seconds costs at most one snapshot per feed per TTL.

        ``scrape_fallback=False`` keeps this to that one snapshot call. Out of
        hours every symbol looks stale to Alpaca, so the fallback would scrape
        the web once per symbol — worth it when a mark drives a trading
        decision, far too expensive for a display column that already tells the
        reader how old its price is.
        """
        wanted: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            sym = str(raw or "").upper().strip()
            if sym and sym not in seen:
                seen.add(sym)
                wanted.append(sym)
        if not wanted:
            return {}

        now_mono = monotonic()
        out: dict[str, dict[str, Any]] = {}
        pending: list[str] = []
        for sym in wanted:
            cached = self._mark_cache.get(sym)
            if cached and cached[0] > now_mono:
                out[sym] = dict(cached[1])
            else:
                pending.append(sym)
        if not pending:
            return out

        session_info = self.market_session()
        alpaca_marks = self._alpaca_marks(pending, session=session_info["session"])

        # Only symbols Alpaca could not price freshly pay for a scrape, and those
        # run concurrently so one slow name does not stall the whole watchlist.
        stale = (
            [
                sym
                for sym in pending
                if not self._alpaca_mark_is_fresh(alpaca_marks.get(sym))
            ]
            if scrape_fallback
            else []
        )
        live_marks: dict[str, dict[str, Any]] = {}
        if stale:
            with ThreadPoolExecutor(max_workers=min(8, len(stale))) as pool:
                futures = {pool.submit(fetch_live_mark, sym): sym for sym in stale}
                for future in as_completed(futures):
                    try:
                        live_marks[futures[future]] = future.result()
                    except Exception:
                        continue

        for sym in pending:
            alpaca = alpaca_marks.get(sym)
            live = live_marks.get(sym)
            chosen = self._pick_fresher_mark(alpaca, live)
            if chosen is None:
                continue
            payload = self._mark_payload(sym, session_info, alpaca, live, chosen)
            self._mark_cache[sym] = (now_mono + _MARK_CACHE_TTL, payload)
            out[sym] = dict(payload)
        return out

    @staticmethod
    def _alpaca_mark_is_fresh(alpaca: dict[str, Any] | None) -> bool:
        """True when the Alpaca mark is recent enough to skip the live scrape."""
        if not alpaca or not alpaca.get("asof"):
            return False
        ts = alpaca["asof"]
        ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        age = (
            datetime.now(timezone.utc) - ts_utc.astimezone(timezone.utc)
        ).total_seconds()
        return age <= 120

    def _mark_payload(
        self,
        symbol: str,
        session_info: dict[str, Any],
        alpaca: dict[str, Any] | None,
        live: dict[str, Any] | None,
        chosen: dict[str, Any],
    ) -> dict[str, Any]:
        asof = chosen["asof"]
        age_seconds = None
        if asof is not None:
            ts = asof if asof.tzinfo else asof.replace(tzinfo=timezone.utc)
            age_seconds = max(
                0,
                int(
                    (
                        datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
                    ).total_seconds()
                ),
            )

        bar_close = None
        if alpaca:
            bar_close = alpaca.get("bar_close")
        if bar_close is None and live:
            bar_close = live.get("regular_price") or live.get("previous_close")

        bid = None
        ask = None
        if chosen["source"].startswith("alpaca") and alpaca:
            bid = alpaca.get("bid")
            ask = alpaca.get("ask")
        if bid is None and live:
            bid = live.get("bid")
        if ask is None and live:
            ask = live.get("ask")
        if bid is None and alpaca:
            bid = alpaca.get("bid")
        if ask is None and alpaca:
            ask = alpaca.get("ask")

        return {
            "symbol": symbol,
            "price": float(chosen["price"]),
            "bar_close": float(bar_close) if bar_close is not None else None,
            "source": chosen["source"],
            "asof": asof.isoformat() if asof is not None else None,
            "age_seconds": age_seconds,
            "session": session_info["session"],
            "is_open": session_info["is_open"],
            "bid": bid,
            "ask": ask,
            "daily_open": (alpaca or {}).get("daily_open"),
            "daily_high": (alpaca or {}).get("daily_high"),
            "daily_low": (alpaca or {}).get("daily_low"),
            "daily_volume": (alpaca or {}).get("daily_volume"),
            "daily_ts": (alpaca or {}).get("daily_ts"),
        }

    def _alpaca_mark(
        self, symbol: str, session: str | None = None
    ) -> dict[str, Any] | None:
        return self._alpaca_marks([symbol], session=session).get(symbol)

    def _alpaca_marks(
        self, symbols: list[str], session: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Snapshot marks for many symbols in one request per feed.

        Alpaca's snapshot endpoint takes a symbol list, so a watchlist costs the
        same one or two calls a single symbol does.
        """
        wanted = [s.upper().strip() for s in symbols if s and s.strip()]
        if not wanted:
            return {}

        target_session = session
        if target_session is None:
            try:
                target_session = self.market_session()["session"]
            except Exception:
                target_session = "regular"

        overnight_feed = getattr(DataFeed, "OVERNIGHT", None)
        feeds: list[tuple[DataFeed, str]] = []
        if overnight_feed is not None and target_session != "regular":
            feeds.append((overnight_feed, "alpaca_overnight"))
            feeds.append((_DEFAULT_FEED, "alpaca"))
        elif overnight_feed is not None:
            feeds.append((_DEFAULT_FEED, "alpaca"))
            feeds.append((overnight_feed, "alpaca_overnight"))
        else:
            feeds.append((_DEFAULT_FEED, "alpaca"))

        results: dict[str, list[dict[str, Any]]] = {sym: [] for sym in wanted}
        for feed, prefix in feeds:
            try:
                request = StockSnapshotRequest(
                    symbol_or_symbols=wanted,
                    feed=feed,
                )
                snaps = self.data.get_stock_snapshot(request)
            except Exception:
                continue

            for sym in wanted:
                snap = snaps.get(sym) if hasattr(snaps, "get") else None
                if snap is None:
                    continue
                mark = self._snapshot_mark(snap, prefix)
                if mark is not None:
                    results[sym].append(mark)

        def _res_ts(r: dict[str, Any]) -> datetime:
            asof = r.get("asof")
            if asof is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            return asof if asof.tzinfo else asof.replace(tzinfo=timezone.utc)

        return {
            sym: max(rows, key=_res_ts) for sym, rows in results.items() if rows
        }

    @staticmethod
    def _snapshot_mark(snap: Any, prefix: str) -> dict[str, Any] | None:
        """Freshest usable price out of one snapshot, or None when it is empty."""
        candidates: list[tuple[datetime | None, float, str]] = []
        if snap.latest_trade is not None and snap.latest_trade.price:
            candidates.append(
                (
                    snap.latest_trade.timestamp,
                    float(snap.latest_trade.price),
                    f"{prefix}_trade",
                )
            )
        if snap.minute_bar is not None and snap.minute_bar.close:
            candidates.append(
                (
                    snap.minute_bar.timestamp,
                    float(snap.minute_bar.close),
                    f"{prefix}_minute",
                )
            )
        if snap.daily_bar is not None and snap.daily_bar.close:
            candidates.append(
                (
                    snap.daily_bar.timestamp,
                    float(snap.daily_bar.close),
                    f"{prefix}_daily",
                )
            )
        if not candidates:
            if snap.latest_quote and (snap.latest_quote.bid_price or snap.latest_quote.ask_price):
                bid_px = float(snap.latest_quote.bid_price) if snap.latest_quote.bid_price else None
                ask_px = float(snap.latest_quote.ask_price) if snap.latest_quote.ask_price else None
                mid = (bid_px + ask_px) / 2 if (bid_px and ask_px) else (bid_px or ask_px)
                if mid:
                    candidates.append((snap.latest_quote.timestamp, float(mid), f"{prefix}_quote"))
        if not candidates:
            return None

        def _ts_key(item: tuple[datetime | None, float, str]) -> datetime:
            ts = item[0]
            if ts is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

        asof, price, source = max(candidates, key=_ts_key)

        daily = snap.daily_bar
        bar_close = float(daily.close) if daily is not None and daily.close else None
        return {
            "price": price,
            "source": source,
            "asof": asof,
            "bar_close": bar_close,
            "daily_open": float(daily.open) if daily is not None and daily.open else None,
            "daily_high": float(daily.high) if daily is not None and daily.high else None,
            "daily_low": float(daily.low) if daily is not None and daily.low else None,
            "daily_volume": (
                float(daily.volume) if daily is not None and daily.volume else None
            ),
            "daily_ts": daily.timestamp if daily is not None else None,
            "bid": float(snap.latest_quote.bid_price)
            if snap.latest_quote and snap.latest_quote.bid_price
            else None,
            "ask": float(snap.latest_quote.ask_price)
            if snap.latest_quote and snap.latest_quote.ask_price
            else None,
        }

    @staticmethod
    def _pick_fresher_mark(
        alpaca: dict[str, Any] | None,
        live: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Prefer the quote with the newest timestamp; live (Nasdaq/Yahoo) if Alpaca is stale."""
        if alpaca is None and live is None:
            return None
        if alpaca is None:
            return {
                "price": live["price"],
                "source": live["source"],
                "asof": live["asof"],
            }
        if live is None:
            return {
                "price": alpaca["price"],
                "source": alpaca["source"],
                "asof": alpaca["asof"],
            }

        a_asof = alpaca["asof"]
        l_asof = live["asof"]
        if a_asof is None and l_asof is not None:
            return {
                "price": live["price"],
                "source": live["source"],
                "asof": l_asof,
            }
        if l_asof is None:
            return {
                "price": alpaca["price"],
                "source": alpaca["source"],
                "asof": a_asof,
            }

        a_ts = a_asof if a_asof.tzinfo else a_asof.replace(tzinfo=timezone.utc)
        l_ts = l_asof if l_asof.tzinfo else l_asof.replace(tzinfo=timezone.utc)
        alpaca_age = (
            datetime.now(timezone.utc) - a_ts.astimezone(timezone.utc)
        ).total_seconds()
        if l_ts > a_ts and alpaca_age > 60:
            return {
                "price": live["price"],
                "source": live["source"],
                "asof": l_asof,
            }
        return {
            "price": alpaca["price"],
            "source": alpaca["source"],
            "asof": a_asof,
        }


def _finite_px(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f <= 0:  # NaN or non-positive
        return None
    return f


def _session_bar_timestamp(raw: Any, session_day: date) -> pd.Timestamp:
    """Timestamp for a synthetic daily bar of `session_day`.

    Use Alpaca's daily_ts only when it already belongs to this session; otherwise
    pin to 04:00 ET (premarket open) so overnight marks do not reuse yesterday.
    """
    if raw is not None:
        try:
            ts = pd.Timestamp(raw)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            if bar_session_date(ts) == session_day:
                return ts
        except (TypeError, ValueError):
            pass
    return pd.Timestamp(datetime.combine(session_day, time(4, 0)), tz=_ET)
