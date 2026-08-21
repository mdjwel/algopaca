"""Historical walk-forward backtest for SMA and buy-the-dip strategies."""

from __future__ import annotations

from typing import Any

import pandas as pd

from bot.strategy import BuyTheDipStrategy, Signal, SmaCrossoverStrategy, StrategyResult

MAX_BACKTEST_SYMBOLS = 10


def parse_backtest_symbols(
    symbols: str | None = None,
    symbol: str | None = None,
    *,
    max_symbols: int = MAX_BACKTEST_SYMBOLS,
) -> list[str]:
    """Parse comma/semicolon/whitespace-separated symbols; fall back to `symbol`."""
    raw = str(symbols or "").strip()
    if not raw:
        raw = str(symbol or "").strip()
    # Accept "AAPL, MSFT", "AAPL;MSFT", or newline/space separated lists.
    normalized = raw.replace(";", ",").replace("\n", ",").replace("\t", ",")
    parts = [p.strip().upper() for p in normalized.split(",")]
    # Also split any leftover whitespace-only tokens like "AAPL MSFT".
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if " " in part:
            out.extend(p for p in part.split() if p)
        else:
            out.append(part)
    out = list(dict.fromkeys(out))
    if not out:
        raise ValueError("At least one symbol is required")
    if len(out) > max_symbols:
        raise ValueError(f"At most {max_symbols} symbols allowed")
    return out


def _bars_needed(strategy: SmaCrossoverStrategy | BuyTheDipStrategy) -> int:
    if isinstance(strategy, BuyTheDipStrategy):
        return max(strategy.rsi_period + 2, strategy.bb_period, 50) + 5
    return strategy.slow + 5


def _ts_str(ts: Any) -> str:
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


def _max_drawdown_pct(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for value in equity:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak * 100.0
            if dd > worst:
                worst = dd
    return round(worst, 2)


def _downsample_curve(equity_curve: list[dict[str, Any]]) -> list[dict[str, Any]]:
    curve_out = equity_curve
    if len(curve_out) > 400:
        step = max(1, len(curve_out) // 300)
        curve_out = curve_out[::step]
        if equity_curve[-1] not in curve_out:
            curve_out = list(curve_out) + [equity_curve[-1]]
    return curve_out


def summary_from_result(result: dict[str, Any]) -> dict[str, Any]:
    """Compact comparison-table row from a backtest (or error) payload."""
    err = result.get("error")
    strat = result.get("total_return_pct")
    hold = result.get("buy_hold_return_pct")
    alpha = None
    try:
        if strat is not None and hold is not None:
            alpha = round(float(strat) - float(hold), 2)
    except (TypeError, ValueError):
        alpha = None
    return {
        "symbol": result.get("symbol"),
        "error": err,
        "total_return_pct": result.get("total_return_pct"),
        "buy_hold_return_pct": result.get("buy_hold_return_pct"),
        "alpha_pct": alpha,
        "max_drawdown_pct": result.get("max_drawdown_pct"),
        "final_equity": result.get("final_equity"),
        "realized_pnl": result.get("realized_pnl"),
        "win_rate": result.get("win_rate"),
        "wins": result.get("wins"),
        "losses": result.get("losses"),
        "round_trips": result.get("round_trips"),
        "trades": result.get("trades"),
    }


def run_backtest(
    bars: pd.DataFrame,
    strategy: SmaCrossoverStrategy | BuyTheDipStrategy,
    *,
    qty: float = 1.0,
    initial_cash: float = 10_000.0,
    stop_loss_pct: float = 0.0,
) -> dict[str, Any]:
    """Simulate long-only trades on historical bars using strategy signals.

    Fills at the bar close when a signal fires. Optional stop uses the bar low
    while a position is open (daily approximation).
    """
    if bars is None or bars.empty or "close" not in bars.columns:
        raise ValueError("Need historical bar data with a close column")
    if qty <= 0:
        raise ValueError("qty must be > 0")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be > 0")

    df = bars.sort_index().copy()
    warmup = _bars_needed(strategy)
    if len(df) < warmup + 2:
        raise ValueError(
            f"Need at least {warmup + 2} bars for this strategy, got {len(df)}"
        )

    cash = float(initial_cash)
    position = 0.0
    entry_price = 0.0
    entry_reason = ""
    stop_price: float | None = None
    next_group_id = 1
    open_group_id: int | None = None

    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    closed_pnls: list[float] = []

    first_price = float(df["close"].iloc[warmup])
    last_price = float(df["close"].iloc[-1])

    def _book(px: float) -> float:
        return cash + position * px

    def _append_fill(
        *,
        side: str,
        time: str,
        price: float,
        fill_qty: float,
        reason: str,
        pnl: float | None,
        pnl_pct: float | None,
        group_id: int | None,
        cash_before: float,
        cash_after: float,
        equity_before: float,
        equity_after: float,
    ) -> None:
        trades.append(
            {
                "side": side,
                "time": time,
                "price": round(price, 4),
                "qty": round(fill_qty, 6),
                "reason": reason,
                "pnl": None if pnl is None else round(pnl, 2),
                "pnl_pct": None if pnl_pct is None else round(pnl_pct, 2),
                "group_id": group_id,
                "cash_before": round(cash_before, 2),
                "cash_after": round(cash_after, 2),
                "equity_before": round(equity_before, 2),
                "equity_after": round(equity_after, 2),
                "slip_cost": 0.0,
                "initial_cash": round(float(initial_cash), 2),
            }
        )

    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1]
        # Cap window size for speed; indicators only need a fixed lookback.
        look = window.tail(max(warmup + 10, 120))
        result: StrategyResult = strategy.evaluate(look)

        row = df.iloc[i]
        price = float(row["close"])
        low = float(row["low"]) if "low" in df.columns else price
        ts = _ts_str(df.index[i])

        # Protective stop (approximate with bar low).
        if (
            position > 0
            and stop_price is not None
            and low <= stop_price
        ):
            fill = min(price, stop_price)
            cash_before = cash
            equity_before = _book(fill)
            proceeds = position * fill
            pnl = proceeds - position * entry_price
            pnl_pct = (fill / entry_price - 1.0) * 100.0 if entry_price else 0.0
            fill_qty = position
            cash += proceeds
            _append_fill(
                side="sell",
                time=ts,
                price=fill,
                fill_qty=fill_qty,
                reason=f"stop loss @ {stop_price:.2f}",
                pnl=pnl,
                pnl_pct=pnl_pct,
                group_id=open_group_id,
                cash_before=cash_before,
                cash_after=cash,
                equity_before=equity_before,
                equity_after=cash,
            )
            closed_pnls.append(pnl)
            position = 0.0
            entry_price = 0.0
            entry_reason = ""
            stop_price = None
            open_group_id = None

        if result.signal is Signal.BUY and position <= 0:
            cost = qty * price
            if cost <= cash + 1e-9:
                cash_before = cash
                equity_before = _book(price)
                cash -= cost
                position = float(qty)
                entry_price = price
                entry_reason = result.reason
                open_group_id = next_group_id
                next_group_id += 1
                if stop_loss_pct and stop_loss_pct > 0:
                    stop_price = price * (1.0 - stop_loss_pct / 100.0)
                else:
                    stop_price = None
                _append_fill(
                    side="buy",
                    time=ts,
                    price=price,
                    fill_qty=qty,
                    reason=result.reason,
                    pnl=None,
                    pnl_pct=None,
                    group_id=open_group_id,
                    cash_before=cash_before,
                    cash_after=cash,
                    equity_before=equity_before,
                    equity_after=_book(price),
                )
        elif result.signal is Signal.SELL and position > 0:
            cash_before = cash
            equity_before = _book(price)
            proceeds = position * price
            pnl = proceeds - position * entry_price
            pnl_pct = (price / entry_price - 1.0) * 100.0 if entry_price else 0.0
            fill_qty = position
            cash += proceeds
            _append_fill(
                side="sell",
                time=ts,
                price=price,
                fill_qty=fill_qty,
                reason=result.reason,
                pnl=pnl,
                pnl_pct=pnl_pct,
                group_id=open_group_id,
                cash_before=cash_before,
                cash_after=cash,
                equity_before=equity_before,
                equity_after=cash,
            )
            closed_pnls.append(pnl)
            position = 0.0
            entry_price = 0.0
            entry_reason = ""
            stop_price = None
            open_group_id = None

        equity = _book(price)
        equity_curve.append(
            {
                "t": ts,
                "equity": round(equity, 2),
                "cash": round(cash, 2),
                "price": round(price, 4),
                "position": round(position, 6),
            }
        )

    # Mark-to-market open position at the end (not a forced close trade).
    final_price = float(df["close"].iloc[-1])
    final_equity = cash + position * final_price
    total_return_pct = (
        (final_equity / initial_cash - 1.0) * 100.0 if initial_cash else 0.0
    )
    buy_hold_pct = (
        (last_price / first_price - 1.0) * 100.0 if first_price else 0.0
    )

    wins = sum(1 for p in closed_pnls if p > 0)
    losses = sum(1 for p in closed_pnls if p < 0)
    closed = len(closed_pnls)
    win_rate = (wins / closed) if closed else 0.0
    realized = sum(closed_pnls)
    equities = [p["equity"] for p in equity_curve]
    unrealized = 0.0
    unrealized_pct = 0.0
    if position > 0 and entry_price > 0:
        unrealized = round(position * (final_price - entry_price), 2)
        unrealized_pct = round((final_price / entry_price - 1.0) * 100.0, 2)

    return {
        "bars": len(df),
        "warmup_bars": warmup,
        "evaluated_bars": max(0, len(df) - warmup),
        "start": _ts_str(df.index[warmup]),
        "end": _ts_str(df.index[-1]),
        "initial_cash": round(float(initial_cash), 2),
        "final_equity": round(final_equity, 2),
        "final_cash": round(cash, 2),
        "open_qty": round(position, 6),
        "open_entry": round(entry_price, 4) if position > 0 else None,
        "open_mark": round(final_price, 4) if position > 0 else None,
        "open_reason": entry_reason or None,
        "open_group_id": open_group_id,
        "unrealized_pnl": unrealized,
        "unrealized_pnl_pct": unrealized_pct,
        "total_return_pct": round(total_return_pct, 2),
        "buy_hold_return_pct": round(buy_hold_pct, 2),
        "max_drawdown_pct": _max_drawdown_pct(equities),
        "trades": len(trades),
        "trade_legs": len(trades),
        "round_trips": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "realized_pnl": round(realized, 2),
        "trade_list": trades,
        "equity_curve": _downsample_curve(equity_curve),
    }


def run_portfolio_backtest(
    bars_by_symbol: dict[str, pd.DataFrame],
    strategy: SmaCrossoverStrategy | BuyTheDipStrategy,
    *,
    qty: float = 1.0,
    initial_cash: float = 10_000.0,
    stop_loss_pct: float = 0.0,
) -> dict[str, Any]:
    """Shared-cash long-only walk-forward across multiple symbols.

    Each symbol may hold at most one `qty` long. Buys spend shared cash; sells
    return proceeds. Buy & hold baseline splits cash equally at the first
    common evaluation bar.
    """
    if not bars_by_symbol:
        raise ValueError("Need bar data for at least one symbol")
    if qty <= 0:
        raise ValueError("qty must be > 0")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be > 0")

    warmup = _bars_needed(strategy)
    frames: dict[str, pd.DataFrame] = {}
    for sym, bars in bars_by_symbol.items():
        key = str(sym or "").upper().strip()
        if not key or bars is None or bars.empty or "close" not in bars.columns:
            continue
        df = bars.sort_index().copy()
        if len(df) < warmup + 2:
            continue
        frames[key] = df
    if not frames:
        raise ValueError("Need enough bar data for at least one symbol")

    symbols = list(frames.keys())
    # Per-symbol positional state.
    cash = float(initial_cash)
    positions = {s: 0.0 for s in symbols}
    entry_prices = {s: 0.0 for s in symbols}
    entry_reasons = {s: "" for s in symbols}
    stop_prices: dict[str, float | None] = {s: None for s in symbols}
    open_group_ids: dict[str, int | None] = {s: None for s in symbols}
    next_group_id = 1
    last_close: dict[str, float] = {}
    closed_pnls: list[float] = []
    closed_by_sym: dict[str, list[float]] = {s: [] for s in symbols}
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []

    def _mtm_book() -> float:
        return cash + sum(
            positions[s] * last_close[s] for s in symbols if s in last_close
        )

    def _append_fill(
        *,
        sym: str,
        side: str,
        time: str,
        price: float,
        fill_qty: float,
        reason: str,
        pnl: float | None,
        pnl_pct: float | None,
        group_id: int | None,
        cash_before: float,
        cash_after: float,
        equity_before: float,
        equity_after: float,
    ) -> None:
        trades.append(
            {
                "symbol": sym,
                "side": side,
                "time": time,
                "price": round(price, 4),
                "qty": round(fill_qty, 6),
                "reason": reason,
                "pnl": None if pnl is None else round(pnl, 2),
                "pnl_pct": None if pnl_pct is None else round(pnl_pct, 2),
                "group_id": group_id,
                "cash_before": round(cash_before, 2),
                "cash_after": round(cash_after, 2),
                "equity_before": round(equity_before, 2),
                "equity_after": round(equity_after, 2),
                "slip_cost": 0.0,
                "initial_cash": round(float(initial_cash), 2),
            }
        )

    # Equal-weight buy & hold: reserve 1/N cash per symbol; convert to shares
    # the first time that symbol becomes tradeable (handles staggered listings).
    hold_shares: dict[str, float] = {s: 0.0 for s in symbols}
    hold_cash_reserved: dict[str, float] = {
        s: float(initial_cash) / len(symbols) for s in symbols
    }
    hold_deployed: dict[str, bool] = {s: False for s in symbols}
    first_eval_ts: Any = None
    last_eval_ts: Any = None
    evaluated = 0

    # Union of all bar timestamps (sorted).
    all_ts = sorted({ts for df in frames.values() for ts in df.index})
    # Locators for fast row lookup.
    loc_maps = {s: {ts: i for i, ts in enumerate(df.index)} for s, df in frames.items()}

    for ts in all_ts:
        # Update last known marks for symbols that have a bar here.
        for sym, df in frames.items():
            i = loc_maps[sym].get(ts)
            if i is None:
                continue
            last_close[sym] = float(df["close"].iloc[i])

        # Trade only on symbols that printed a bar at this timestamp and are
        # past warmup on their own series.
        for sym, df in frames.items():
            i = loc_maps[sym].get(ts)
            if i is None or i < warmup:
                continue

            if first_eval_ts is None:
                first_eval_ts = ts
            last_eval_ts = ts

            row = df.iloc[i]
            price = float(row["close"])
            low = float(row["low"]) if "low" in df.columns else price
            ts_s = _ts_str(ts)
            position = positions[sym]

            if not hold_deployed[sym]:
                reserved = hold_cash_reserved[sym]
                if price > 0 and reserved > 0:
                    hold_shares[sym] = reserved / price
                    hold_cash_reserved[sym] = 0.0
                hold_deployed[sym] = True

            window = df.iloc[: i + 1]
            look = window.tail(max(warmup + 10, 120))
            result: StrategyResult = strategy.evaluate(look)

            if (
                position > 0
                and stop_prices[sym] is not None
                and low <= stop_prices[sym]  # type: ignore[operator]
            ):
                stop_px = float(stop_prices[sym] or price)
                fill = min(price, stop_px)
                cash_before = cash
                equity_before = _mtm_book()
                proceeds = position * fill
                pnl = proceeds - position * entry_prices[sym]
                pnl_pct = (
                    (fill / entry_prices[sym] - 1.0) * 100.0 if entry_prices[sym] else 0.0
                )
                fill_qty = position
                cash += proceeds
                last_close[sym] = fill
                _append_fill(
                    sym=sym,
                    side="sell",
                    time=ts_s,
                    price=fill,
                    fill_qty=fill_qty,
                    reason=f"stop loss @ {stop_px:.2f}",
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    group_id=open_group_ids[sym],
                    cash_before=cash_before,
                    cash_after=cash,
                    equity_before=equity_before,
                    equity_after=_mtm_book(),
                )
                closed_pnls.append(pnl)
                closed_by_sym[sym].append(pnl)
                positions[sym] = 0.0
                entry_prices[sym] = 0.0
                entry_reasons[sym] = ""
                stop_prices[sym] = None
                open_group_ids[sym] = None
                position = 0.0

            if result.signal is Signal.BUY and position <= 0:
                cost = qty * price
                if cost <= cash + 1e-9:
                    cash_before = cash
                    equity_before = _mtm_book()
                    cash -= cost
                    positions[sym] = float(qty)
                    entry_prices[sym] = price
                    entry_reasons[sym] = result.reason
                    open_group_ids[sym] = next_group_id
                    next_group_id += 1
                    if stop_loss_pct and stop_loss_pct > 0:
                        stop_prices[sym] = price * (1.0 - stop_loss_pct / 100.0)
                    else:
                        stop_prices[sym] = None
                    last_close[sym] = price
                    _append_fill(
                        sym=sym,
                        side="buy",
                        time=ts_s,
                        price=price,
                        fill_qty=qty,
                        reason=result.reason,
                        pnl=None,
                        pnl_pct=None,
                        group_id=open_group_ids[sym],
                        cash_before=cash_before,
                        cash_after=cash,
                        equity_before=equity_before,
                        equity_after=_mtm_book(),
                    )
            elif result.signal is Signal.SELL and position > 0:
                cash_before = cash
                equity_before = _mtm_book()
                proceeds = position * price
                pnl = proceeds - position * entry_prices[sym]
                pnl_pct = (
                    (price / entry_prices[sym] - 1.0) * 100.0
                    if entry_prices[sym]
                    else 0.0
                )
                fill_qty = position
                cash += proceeds
                last_close[sym] = price
                _append_fill(
                    sym=sym,
                    side="sell",
                    time=ts_s,
                    price=price,
                    fill_qty=fill_qty,
                    reason=result.reason,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    group_id=open_group_ids[sym],
                    cash_before=cash_before,
                    cash_after=cash,
                    equity_before=equity_before,
                    equity_after=_mtm_book(),
                )
                closed_pnls.append(pnl)
                closed_by_sym[sym].append(pnl)
                positions[sym] = 0.0
                entry_prices[sym] = 0.0
                entry_reasons[sym] = ""
                stop_prices[sym] = None
                open_group_ids[sym] = None

        # Book equity after processing this timestamp's signals.
        if first_eval_ts is None:
            continue
        mtm = cash + sum(
            positions[s] * last_close[s] for s in symbols if s in last_close
        )
        hold_eq = sum(hold_cash_reserved.values()) + sum(
            hold_shares[s] * last_close[s]
            for s in symbols
            if hold_deployed[s] and s in last_close
        )
        evaluated += 1
        equity_curve.append(
            {
                "t": _ts_str(ts),
                "equity": round(mtm, 2),
                "cash": round(cash, 2),
                "hold_equity": round(hold_eq, 2),
                "price": round(
                    sum(last_close.values()) / len(last_close) if last_close else 0.0,
                    4,
                ),
                "position": round(sum(positions.values()), 6),
            }
        )

    if not equity_curve:
        raise ValueError("No overlapping evaluation window for portfolio")

    final_equity = float(equity_curve[-1]["equity"])
    final_hold = float(equity_curve[-1].get("hold_equity") or initial_cash)
    total_return_pct = (
        (final_equity / initial_cash - 1.0) * 100.0 if initial_cash else 0.0
    )
    buy_hold_pct = (
        (final_hold / initial_cash - 1.0) * 100.0 if initial_cash else 0.0
    )
    wins = sum(1 for p in closed_pnls if p > 0)
    losses = sum(1 for p in closed_pnls if p < 0)
    closed = len(closed_pnls)
    win_rate = (wins / closed) if closed else 0.0
    realized = sum(closed_pnls)
    equities = [p["equity"] for p in equity_curve]

    open_legs = []
    unrealized_total = 0.0
    for s in symbols:
        if positions[s] <= 0:
            continue
        mark = last_close.get(s)
        entry = entry_prices[s]
        u_pnl = 0.0
        u_pct = 0.0
        if mark is not None and entry > 0:
            u_pnl = round(positions[s] * (mark - entry), 2)
            u_pct = round((mark / entry - 1.0) * 100.0, 2)
            unrealized_total += u_pnl
        open_legs.append(
            {
                "symbol": s,
                "qty": round(positions[s], 6),
                "entry": round(entry, 4),
                "mark": round(mark, 4) if mark is not None else None,
                "reason": entry_reasons[s] or None,
                "group_id": open_group_ids[s],
                "unrealized_pnl": u_pnl,
                "unrealized_pnl_pct": u_pct,
            }
        )

    # Per-symbol legs derived from portfolio trades (not independent cash).
    results: list[dict[str, Any]] = []
    for sym in symbols:
        sym_trades = [t for t in trades if t.get("symbol") == sym]
        pnls = closed_by_sym[sym]
        sw = sum(1 for p in pnls if p > 0)
        sl = sum(1 for p in pnls if p < 0)
        sc = len(pnls)
        mark = last_close.get(sym)
        u_pnl = 0.0
        u_pct = 0.0
        if positions[sym] > 0 and mark is not None and entry_prices[sym] > 0:
            u_pnl = round(positions[sym] * (mark - entry_prices[sym]), 2)
            u_pct = round((mark / entry_prices[sym] - 1.0) * 100.0, 2)
        results.append(
            {
                "symbol": sym,
                "bars": len(frames[sym]),
                "warmup_bars": warmup,
                "trades": len(sym_trades),
                "trade_legs": len(sym_trades),
                "round_trips": sc,
                "wins": sw,
                "losses": sl,
                "win_rate": round((sw / sc) if sc else 0.0, 4),
                "realized_pnl": round(sum(pnls), 2),
                "open_qty": round(positions[sym], 6),
                "open_entry": (
                    round(entry_prices[sym], 4) if positions[sym] > 0 else None
                ),
                "open_mark": round(mark, 4) if positions[sym] > 0 and mark else None,
                "open_group_id": open_group_ids[sym],
                "unrealized_pnl": u_pnl,
                "unrealized_pnl_pct": u_pct,
                "trade_list": sym_trades,
                # Book-level returns aren't attributable cleanly; leave blank.
                "total_return_pct": None,
                "buy_hold_return_pct": None,
                "max_drawdown_pct": None,
                "final_equity": None,
            }
        )

    total_bars = sum(len(df) for df in frames.values())
    return {
        "symbols": symbols,
        "bars": total_bars,
        "warmup_bars": warmup,
        "evaluated_bars": evaluated,
        "start": _ts_str(first_eval_ts) if first_eval_ts is not None else None,
        "end": _ts_str(last_eval_ts) if last_eval_ts is not None else None,
        "initial_cash": round(float(initial_cash), 2),
        "final_equity": round(final_equity, 2),
        "final_cash": round(cash, 2),
        "open_qty": round(sum(positions.values()), 6),
        "open_entry": None,
        "open_mark": None,
        "open_reason": None,
        "open_legs": open_legs,
        "unrealized_pnl": round(unrealized_total, 2),
        "unrealized_pnl_pct": None,
        "total_return_pct": round(total_return_pct, 2),
        "buy_hold_return_pct": round(buy_hold_pct, 2),
        "max_drawdown_pct": _max_drawdown_pct(equities),
        "trades": len(trades),
        "trade_legs": len(trades),
        "round_trips": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "realized_pnl": round(realized, 2),
        "trade_list": trades,
        "equity_curve": _downsample_curve(equity_curve),
        "results": results,
    }


def build_strategy(
    mode: str,
    *,
    fast_sma: int = 10,
    slow_sma: int = 30,
    dip_rsi_buy: float = 30.0,
    dip_rsi_sell: float = 60.0,
    dip_skip_bearish: bool = True,
    use_lower_band: bool = True,
) -> SmaCrossoverStrategy | BuyTheDipStrategy:
    key = (mode or "sma").strip().lower()
    if key == "dip":
        return BuyTheDipStrategy(
            dip_rsi_buy,
            dip_rsi_sell,
            skip_bearish=dip_skip_bearish,
            use_lower_band=use_lower_band,
        )
    if key == "sma":
        return SmaCrossoverStrategy(fast_sma, slow_sma)
    raise ValueError(
        "Backtest mode must be 'sma' or 'dip' (AI unsupported; pair uses run_pair_backtest)"
    )
