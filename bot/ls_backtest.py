"""Long/short walk-forward backtest with ATR risk, frictions, and full metrics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from bot.backtest import _downsample_curve, _max_drawdown_pct, _ts_str
from bot.ls_strategy import LSSide, LongShortRegimeStrategy


@dataclass(frozen=True)
class LSRiskParams:
    atr_stop_mult: float = 1.5
    min_stop_pct: float = 2.0
    rr: float = 2.0
    trail_atr_mult: float = 1.0
    risk_pct: float = 1.0
    time_stop_bars: int = 15
    commission_pct: float = 0.05  # 0.05% per leg
    slippage_pct: float = 0.02  # 0.02% adverse
    short_borrow_apr: float = 0.05  # 5% annualized
    max_portfolio_dd_pct: float = 15.0


def _apply_slip(price: float, *, side: str, is_entry: bool, slip_pct: float) -> float:
    """Adverse slippage on fill price."""
    slip = max(0.0, float(slip_pct)) / 100.0
    if side == LSSide.LONG.value:
        return price * (1.0 + slip) if is_entry else price * (1.0 - slip)
    # short: enter lower fill hurts (sell lower); exit higher fill hurts (buy higher)
    return price * (1.0 - slip) if is_entry else price * (1.0 + slip)


def _commission(notional: float, commission_pct: float) -> float:
    return abs(notional) * max(0.0, float(commission_pct)) / 100.0


def _stop_distance(entry: float, atr: float, risk: LSRiskParams) -> float:
    atr_dist = max(0.0, float(atr)) * float(risk.atr_stop_mult)
    pct_dist = abs(float(entry)) * float(risk.min_stop_pct) / 100.0
    return max(atr_dist, pct_dist, abs(float(entry)) * 1e-6)


def _position_qty(equity: float, stop_dist: float, risk: LSRiskParams) -> float:
    if equity <= 0 or stop_dist <= 0:
        return 0.0
    risk_dollars = equity * float(risk.risk_pct) / 100.0
    qty = risk_dollars / stop_dist
    return max(0.0, round(qty, 6))


def _sharpe_sortino(daily_returns: list[float]) -> tuple[float | None, float | None]:
    if len(daily_returns) < 2:
        return None, None
    arr = np.asarray(daily_returns, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 2:
        return None, None
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1))
    sharpe = (mean / std * np.sqrt(252)) if std > 1e-12 else None
    downside = arr[arr < 0]
    if len(downside) < 1:
        sortino = None if mean <= 0 else float("inf")
    else:
        dstd = float(np.std(downside, ddof=1)) if len(downside) > 1 else float(abs(downside[0]))
        sortino = (mean / dstd * np.sqrt(252)) if dstd > 1e-12 else None
    if sharpe is not None and not np.isfinite(sharpe):
        sharpe = None
    if sortino is not None and not np.isfinite(sortino):
        sortino = None
    return (
        round(sharpe, 3) if sharpe is not None else None,
        round(sortino, 3) if sortino is not None else None,
    )


def _profit_factor(pnls: list[float]) -> float | None:
    gains = sum(p for p in pnls if p > 0)
    losses = sum(-p for p in pnls if p < 0)
    if losses <= 1e-12:
        return None if gains <= 0 else None  # undefined / infinite → None
    return round(gains / losses, 3)


def buy_hold_metrics(bars: pd.DataFrame, *, initial_cash: float = 10_000.0) -> dict[str, Any]:
    """Equal single-asset buy & hold over the given bars (no frictions)."""
    if bars is None or bars.empty or "close" not in bars.columns:
        raise ValueError("Need bars with close")
    closes = bars["close"].astype(float).sort_index()
    first = float(closes.iloc[0])
    last = float(closes.iloc[-1])
    if first <= 0:
        raise ValueError("Invalid first close")
    qty = initial_cash / first
    equity = [qty * float(c) for c in closes]
    rets = [0.0]
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        rets.append((equity[i] / prev - 1.0) if prev else 0.0)
    total_pct = (last / first - 1.0) * 100.0
    sharpe, sortino = _sharpe_sortino(rets[1:])
    return {
        "total_return_pct": round(total_pct, 2),
        "max_drawdown_pct": _max_drawdown_pct(equity),
        "sharpe": sharpe,
        "sortino": sortino,
        "final_equity": round(equity[-1], 2),
        "start": _ts_str(closes.index[0]),
        "end": _ts_str(closes.index[-1]),
    }


def run_ls_backtest(
    bars: pd.DataFrame,
    strategy: LongShortRegimeStrategy,
    risk: LSRiskParams | None = None,
    *,
    initial_cash: float = 10_000.0,
    eval_start: pd.Timestamp | None = None,
    allow_entries: bool = True,
) -> dict[str, Any]:
    """Simulate long and short trades with ATR stops, 2R targets, and frictions.

    Signals and protective exits evaluated on each bar. Entries/exits fill at
    the close (with slippage); stops use bar high/low approximation.
    Metrics are computed on the equity curve from `eval_start` (inclusive)
    when provided; bars before that are warmup only (no entries).
    """
    if bars is None or bars.empty or "close" not in bars.columns:
        raise ValueError("Need historical bar data with a close column")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be > 0")

    risk = risk or LSRiskParams()
    df = bars.sort_index().copy()
    warmup = strategy.bars_needed
    if len(df) < warmup + 2:
        raise ValueError(
            f"Need at least {warmup + 2} bars for this strategy, got {len(df)}"
        )

    frame = strategy.indicator_frame(df)
    cash = float(initial_cash)
    position = 0.0  # signed: >0 long, <0 short
    side: str | None = None
    entry_price = 0.0
    entry_commission = 0.0
    stop_price: float | None = None
    target_price: float | None = None
    r_dist = 0.0
    bars_in_trade = 0
    trail_armed = False
    killed = False
    peak_equity = float(initial_cash)

    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    closed_pnls: list[float] = []
    closed_durations: list[int] = []
    next_group_id = 1
    open_group_id: int | None = None
    entry_reason = ""
    total_commission = 0.0
    total_slip_cost = 0.0
    total_borrow = 0.0

    eval_start_ts = None
    if eval_start is not None:
        eval_start_ts = pd.Timestamp(eval_start)
        if eval_start_ts.tzinfo is None and getattr(df.index[0], "tzinfo", None):
            eval_start_ts = eval_start_ts.tz_localize(df.index[0].tzinfo)
        elif eval_start_ts.tzinfo is not None and getattr(df.index[0], "tzinfo", None) is None:
            eval_start_ts = eval_start_ts.tz_localize(None)

    def _mark(px: float) -> float:
        if position == 0:
            return cash
        # long: cash + qty*px; short: cash holds proceeds; MTM = cash - qty_abs*(px-entry) wait
        # Accounting: on short entry we credit cash with proceeds. Equity = cash + position*px
        # with position negative works: cash += entry*qty_abs; position = -qty_abs;
        # equity = cash + (-qty)*px = cash - qty*px = initial + qty*(entry-px) ✓
        return cash + position * px

    def _close_position(
        *,
        fill_raw: float,
        ts: str,
        reason: str,
        atr_now: float,
    ) -> None:
        nonlocal cash, position, side, entry_price, entry_commission
        nonlocal stop_price, target_price
        nonlocal r_dist, bars_in_trade, trail_armed, open_group_id, entry_reason
        nonlocal total_commission, total_slip_cost

        if position == 0 or side is None:
            return
        is_long = position > 0
        qty_abs = abs(position)
        fill = _apply_slip(
            fill_raw,
            side=side,
            is_entry=False,
            slip_pct=risk.slippage_pct,
        )
        notional = qty_abs * fill
        commission = _commission(notional, risk.commission_pct)
        slip_cost = abs(fill - fill_raw) * qty_abs
        cash_before = cash
        equity_before = _mark(fill_raw)

        if is_long:
            cash += qty_abs * fill
            cash -= commission
            pnl = qty_abs * (fill - entry_price) - commission - entry_commission
        else:
            cash -= qty_abs * fill
            cash -= commission
            pnl = qty_abs * (entry_price - fill) - commission - entry_commission

        if is_long and entry_price:
            pnl_pct = (fill / entry_price - 1.0) * 100.0
        elif entry_price:
            pnl_pct = (entry_price - fill) / entry_price * 100.0
        else:
            pnl_pct = 0.0

        total_commission += commission
        total_slip_cost += slip_cost
        closed_pnls.append(pnl)
        closed_durations.append(bars_in_trade)

        trades.append(
            {
                "side": "sell" if is_long else "cover",
                "position_side": side,
                "time": ts,
                "price": round(fill, 4),
                "qty": round(qty_abs, 6),
                "reason": reason,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "group_id": open_group_id,
                "bars_held": bars_in_trade,
                "commission": round(commission, 4),
                "slip_cost": round(slip_cost, 4),
                "cash_before": round(cash_before, 2),
                "cash_after": round(cash, 2),
                "equity_before": round(equity_before, 2),
                "equity_after": round(cash, 2),
            }
        )
        position = 0.0
        side = None
        entry_price = 0.0
        entry_commission = 0.0
        stop_price = None
        target_price = None
        r_dist = 0.0
        bars_in_trade = 0
        trail_armed = False
        open_group_id = None
        entry_reason = ""

    def _open_position(
        *,
        new_side: str,
        fill_raw: float,
        atr_now: float,
        ts: str,
        reason: str,
    ) -> None:
        nonlocal cash, position, side, entry_price, entry_commission
        nonlocal stop_price, target_price
        nonlocal r_dist, bars_in_trade, trail_armed, open_group_id, next_group_id
        nonlocal entry_reason, total_commission, total_slip_cost

        stop_dist = _stop_distance(fill_raw, atr_now, risk)
        qty = _position_qty(cash if position == 0 else _mark(fill_raw), stop_dist, risk)
        if qty <= 0:
            return
        # Cap notional to available cash (long) / conservative short margin = cash
        max_qty = cash / max(fill_raw, 1e-9)
        qty = min(qty, max_qty)
        if qty * fill_raw < 1.0:
            return

        fill = _apply_slip(
            fill_raw, side=new_side, is_entry=True, slip_pct=risk.slippage_pct
        )
        notional = qty * fill
        commission = _commission(notional, risk.commission_pct)
        slip_cost = abs(fill - fill_raw) * qty
        if notional + commission > cash + 1e-9 and new_side == LSSide.LONG.value:
            qty = (cash * 0.99) / (fill * (1.0 + risk.commission_pct / 100.0))
            qty = round(qty, 6)
            if qty <= 0:
                return
            notional = qty * fill
            commission = _commission(notional, risk.commission_pct)

        cash_before = cash
        equity_before = _mark(fill_raw)
        if new_side == LSSide.LONG.value:
            cash -= notional + commission
            position = qty
            stop_price = fill - stop_dist
            target_price = fill + stop_dist * float(risk.rr)
        else:
            cash += notional
            cash -= commission
            position = -qty
            stop_price = fill + stop_dist
            target_price = fill - stop_dist * float(risk.rr)

        side = new_side
        entry_price = fill
        entry_commission = commission
        r_dist = stop_dist
        bars_in_trade = 0
        trail_armed = False
        open_group_id = next_group_id
        next_group_id += 1
        entry_reason = reason
        total_commission += commission
        total_slip_cost += slip_cost

        trades.append(
            {
                "side": "buy" if new_side == LSSide.LONG.value else "short",
                "position_side": new_side,
                "time": ts,
                "price": round(fill, 4),
                "qty": round(qty, 6),
                "reason": reason,
                "pnl": None,
                "pnl_pct": None,
                "group_id": open_group_id,
                "bars_held": 0,
                "commission": round(commission, 4),
                "slip_cost": round(slip_cost, 4),
                "stop": round(stop_price, 4) if stop_price else None,
                "target": round(target_price, 4) if target_price else None,
                "cash_before": round(cash_before, 2),
                "cash_after": round(cash, 2),
                "equity_before": round(equity_before, 2),
                "equity_after": round(_mark(fill), 2),
            }
        )

    metric_equities: list[float] = []
    metric_returns: list[float] = []
    prev_metric_eq: float | None = None

    for i in range(warmup, len(df)):
        row = frame.iloc[i]
        prev = frame.iloc[i - 1]
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        atr_now = float(row["atr"]) if pd.notna(row["atr"]) else 0.0
        ts = _ts_str(df.index[i])
        in_eval = eval_start_ts is None or df.index[i] >= eval_start_ts

        # Daily short borrow fee
        if position < 0 and in_eval:
            notional = abs(position) * price
            borrow = notional * float(risk.short_borrow_apr) / 252.0
            cash -= borrow
            total_borrow += borrow

        # Kill switch on portfolio (single-symbol) drawdown
        equity_now = _mark(price)
        if equity_now > peak_equity:
            peak_equity = equity_now
        dd_pct = (
            (peak_equity - equity_now) / peak_equity * 100.0 if peak_equity > 0 else 0.0
        )
        if (
            in_eval
            and not killed
            and dd_pct >= float(risk.max_portfolio_dd_pct)
            and position != 0
        ):
            _close_position(
                fill_raw=price,
                ts=ts,
                reason=f"kill switch MaxDD {dd_pct:.1f}% >= {risk.max_portfolio_dd_pct:.0f}%",
                atr_now=atr_now,
            )
            killed = True

        # Manage open trade: stop / target / trail / time
        if position != 0 and side is not None and stop_price is not None:
            bars_in_trade += 1
            is_long = position > 0

            # Arm trailing after +1R
            if r_dist > 0:
                if is_long and price >= entry_price + r_dist:
                    trail_armed = True
                if (not is_long) and price <= entry_price - r_dist:
                    trail_armed = True
            if trail_armed and atr_now > 0:
                if is_long:
                    trail = price - atr_now * float(risk.trail_atr_mult)
                    stop_price = max(stop_price, trail)
                else:
                    trail = price + atr_now * float(risk.trail_atr_mult)
                    stop_price = min(stop_price, trail)

            hit_stop = (is_long and low <= stop_price) or (
                (not is_long) and high >= stop_price
            )
            hit_target = (
                target_price is not None
                and (
                    (is_long and high >= target_price)
                    or ((not is_long) and low <= target_price)
                )
            )
            hit_time = bars_in_trade >= int(risk.time_stop_bars)

            if hit_stop:
                fill = min(price, stop_price) if is_long else max(price, stop_price)
                _close_position(
                    fill_raw=fill,
                    ts=ts,
                    reason=f"stop @ {stop_price:.4f}",
                    atr_now=atr_now,
                )
            elif hit_target and target_price is not None:
                fill = max(price, target_price) if is_long else min(price, target_price)
                # conservative: use target
                fill = float(target_price)
                _close_position(
                    fill_raw=fill,
                    ts=ts,
                    reason=f"take profit {float(risk.rr):g}R @ {target_price:.4f}",
                    atr_now=atr_now,
                )
            elif hit_time:
                _close_position(
                    fill_raw=price,
                    ts=ts,
                    reason=f"time stop after {bars_in_trade} bars",
                    atr_now=atr_now,
                )

        # Strategy signal (regime flip / new entry)
        sig = strategy._signal_from_rows(row, prev)

        if position != 0 and side is not None:
            # Opposite directional entry signal → flatten
            if side == LSSide.LONG.value and sig.side is LSSide.SHORT:
                _close_position(
                    fill_raw=price,
                    ts=ts,
                    reason="regime/signal flip to short",
                    atr_now=atr_now,
                )
            elif side == LSSide.SHORT.value and sig.side is LSSide.LONG:
                _close_position(
                    fill_raw=price,
                    ts=ts,
                    reason="regime/signal flip to long",
                    atr_now=atr_now,
                )
            # Soft exit: EMA regime flip against position
            elif side == LSSide.LONG.value and float(row["ema_fast"]) < float(
                row["ema_slow"]
            ):
                _close_position(
                    fill_raw=price,
                    ts=ts,
                    reason="bearish EMA regime flip",
                    atr_now=atr_now,
                )
            elif side == LSSide.SHORT.value and float(row["ema_fast"]) > float(
                row["ema_slow"]
            ):
                _close_position(
                    fill_raw=price,
                    ts=ts,
                    reason="bullish EMA regime flip",
                    atr_now=atr_now,
                )

        can_enter = (
            allow_entries
            and not killed
            and in_eval
            and position == 0
            and sig.side in (LSSide.LONG, LSSide.SHORT)
        )
        if can_enter:
            _open_position(
                new_side=sig.side.value,
                fill_raw=price,
                atr_now=atr_now if atr_now > 0 else sig.atr,
                ts=ts,
                reason=sig.reason,
            )

        equity = _mark(price)
        point = {
            "t": ts,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "price": round(price, 4),
            "position": round(position, 6),
            "side": side,
        }
        equity_curve.append(point)

        if in_eval:
            metric_equities.append(equity)
            if prev_metric_eq is not None and prev_metric_eq != 0:
                metric_returns.append(equity / prev_metric_eq - 1.0)
            prev_metric_eq = equity

    final_price = float(df["close"].iloc[-1])
    final_equity = _mark(final_price)

    # Buy & hold over evaluation window
    if eval_start_ts is not None:
        eval_bars = df.loc[df.index >= eval_start_ts]
    else:
        eval_bars = df.iloc[warmup:]
    bh = buy_hold_metrics(eval_bars, initial_cash=initial_cash) if len(eval_bars) >= 2 else {
        "total_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "sharpe": None,
        "sortino": None,
    }

    if metric_equities:
        start_eq = metric_equities[0]
        total_return_pct = (
            (metric_equities[-1] / start_eq - 1.0) * 100.0 if start_eq else 0.0
        )
        # Rebase: strategy return from initial_cash at first eval bar
        # Using mark-to-market path that started at initial_cash before first eval.
        first_eval_eq = metric_equities[0]
        # Prefer return vs initial_cash when we start flat at eval
        total_return_pct = (final_equity / initial_cash - 1.0) * 100.0
        # But if eval window mid-stream, use first metric equity as base when it
        # differs materially from initial (warmup-only path with no trades → same).
        if abs(first_eval_eq - initial_cash) > 1.0:
            total_return_pct = (
                (metric_equities[-1] / first_eval_eq - 1.0) * 100.0
                if first_eval_eq
                else 0.0
            )
    else:
        total_return_pct = (final_equity / initial_cash - 1.0) * 100.0

    wins = sum(1 for p in closed_pnls if p > 0)
    losses = sum(1 for p in closed_pnls if p < 0)
    closed = len(closed_pnls)
    win_rate = (wins / closed) if closed else 0.0
    sharpe, sortino = _sharpe_sortino(metric_returns)
    avg_dur = (
        round(float(np.mean(closed_durations)), 2) if closed_durations else 0.0
    )

    equities_for_dd = metric_equities if metric_equities else [p["equity"] for p in equity_curve]

    final_price = float(df["close"].iloc[-1])
    unrealized = 0.0
    unrealized_pct = 0.0
    if position != 0 and entry_price > 0:
        if position > 0:
            unrealized = round(position * (final_price - entry_price), 2)
            unrealized_pct = round((final_price / entry_price - 1.0) * 100.0, 2)
        else:
            qty_abs = abs(position)
            unrealized = round(qty_abs * (entry_price - final_price), 2)
            unrealized_pct = round((entry_price - final_price) / entry_price * 100.0, 2)

    return {
        "symbol": None,
        "bars": len(df),
        "warmup_bars": warmup,
        "evaluated_bars": len(metric_equities),
        "start": _ts_str(eval_bars.index[0]) if len(eval_bars) else _ts_str(df.index[warmup]),
        "end": _ts_str(df.index[-1]),
        "initial_cash": round(float(initial_cash), 2),
        "final_equity": round(final_equity, 2),
        "final_cash": round(cash, 2),
        "open_qty": round(position, 6),
        "open_side": side,
        "open_entry": round(entry_price, 4) if position != 0 else None,
        "open_mark": round(final_price, 4) if position != 0 else None,
        "open_reason": entry_reason or None,
        "open_group_id": open_group_id,
        "unrealized_pnl": unrealized,
        "unrealized_pnl_pct": unrealized_pct,
        "total_return_pct": round(total_return_pct, 2),
        "buy_hold_return_pct": bh.get("total_return_pct"),
        "buy_hold_max_drawdown_pct": bh.get("max_drawdown_pct"),
        "buy_hold_sharpe": bh.get("sharpe"),
        "buy_hold_sortino": bh.get("sortino"),
        "max_drawdown_pct": _max_drawdown_pct(equities_for_dd),
        "sharpe": sharpe,
        "sortino": sortino,
        "profit_factor": _profit_factor(closed_pnls),
        "trades": len(trades),
        "trade_legs": len(trades),
        "round_trips": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "avg_trade_duration_bars": avg_dur,
        "realized_pnl": round(sum(closed_pnls), 2),
        "total_commission": round(total_commission, 2),
        "total_slip_cost": round(total_slip_cost, 2),
        "total_borrow_fees": round(total_borrow, 2),
        "killed": killed,
        "risk": asdict(risk),
        "strategy": {
            "ema_fast": strategy.ema_fast,
            "ema_slow": strategy.ema_slow,
            "adx_period": strategy.adx_period,
            "adx_min": strategy.adx_min,
            "atr_period": strategy.atr_period,
            "macd": [strategy.macd_fast, strategy.macd_slow, strategy.macd_signal],
        },
        "trade_list": trades,
        "equity_curve": _downsample_curve(equity_curve),
        "daily_returns": [round(r, 6) for r in metric_returns],
    }


def run_ls_portfolio_backtest(
    bars_by_symbol: dict[str, pd.DataFrame],
    strategy: LongShortRegimeStrategy,
    risk: LSRiskParams | None = None,
    *,
    initial_cash: float = 10_000.0,
    eval_start: pd.Timestamp | None = None,
    max_concurrent: int = 4,
) -> dict[str, Any]:
    """Equal-capital sleeve per symbol (1/N), vol-parity entry scaling via ATR%.

    Each symbol runs an independent sleeve with cash = initial_cash / N.
    Portfolio equity = sum of sleeve equities. Kill switch applies per sleeve
    via LSRiskParams; portfolio-level DD reported on summed curve.

    ``max_concurrent`` softens per-sleeve risk (risk_pct scaled by
    min(1, max_concurrent / N)) — it is not a hard cross-asset position gate.
    """
    risk = risk or LSRiskParams()
    symbols = [s for s, b in bars_by_symbol.items() if b is not None and not b.empty]
    if not symbols:
        raise ValueError("No symbols with bars")

    n = len(symbols)
    sleeve_cash = float(initial_cash) / n
    per: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        # Vol-parity: scale risk_pct by relative inverse ATR%
        frame = strategy.indicator_frame(bars_by_symbol[sym])
        atr_pct = float(
            (frame["atr"] / frame["close"]).dropna().iloc[-1]
        ) if len(frame["atr"].dropna()) else 0.02
        # Higher vol → lower risk_pct; normalize later across sleeves by using
        # risk_pct * (median_atr_pct / atr_pct)
        per[sym] = {
            "atr_pct": max(atr_pct, 1e-4),
            "bars": bars_by_symbol[sym],
        }

    median_atr = float(np.median([per[s]["atr_pct"] for s in symbols]))
    results: list[dict[str, Any]] = []
    for sym in symbols:
        scale = median_atr / per[sym]["atr_pct"]
        scale = float(np.clip(scale, 0.5, 2.0))
        sleeve_risk = LSRiskParams(
            **{
                **asdict(risk),
                "risk_pct": float(risk.risk_pct) * scale,
            }
        )
        # Soft concurrent: reduce risk when many names — approx by risk_pct / sqrt cap
        sleeve_risk = LSRiskParams(
            **{
                **asdict(sleeve_risk),
                "risk_pct": sleeve_risk.risk_pct
                * min(1.0, max_concurrent / max(n, 1)),
            }
        )
        r = run_ls_backtest(
            per[sym]["bars"],
            strategy,
            sleeve_risk,
            initial_cash=sleeve_cash,
            eval_start=eval_start,
        )
        r["symbol"] = sym
        results.append(r)

    # Align equity curves by date (sum)
    curves: dict[str, float] = {}
    for r in results:
        for pt in r.get("equity_curve") or []:
            t = pt["t"]
            curves[t] = curves.get(t, 0.0) + float(pt["equity"])
    # Also build from daily_returns approximately via final equities path:
    # Prefer reconstructing from per-symbol full curves — downsample may misalign.
    # Re-run lightweight mark from stored trade paths is heavy; use final + BH aggregate.

    sorted_ts = sorted(curves.keys())
    port_equity = [curves[t] for t in sorted_ts]
    port_rets: list[float] = []
    for i in range(1, len(port_equity)):
        prev = port_equity[i - 1]
        port_rets.append((port_equity[i] / prev - 1.0) if prev else 0.0)

    final_equity = sum(float(r["final_equity"]) for r in results)
    if port_equity:
        # Prefer sum of last points
        final_equity = port_equity[-1]
        start_eq = port_equity[0]
        total_return_pct = (final_equity / start_eq - 1.0) * 100.0 if start_eq else 0.0
    else:
        total_return_pct = (final_equity / initial_cash - 1.0) * 100.0

    # Equal-weight buy & hold portfolio
    bh_rets: list[float] = []
    bh_curves: dict[str, list[float]] = {}
    for sym in symbols:
        b = bars_by_symbol[sym].sort_index()
        if eval_start is not None:
            es = pd.Timestamp(eval_start)
            if es.tzinfo is None and getattr(b.index[0], "tzinfo", None):
                es = es.tz_localize(b.index[0].tzinfo)
            b = b.loc[b.index >= es]
        if b.empty:
            continue
        closes = b["close"].astype(float)
        first = float(closes.iloc[0])
        series = (closes / first) * (sleeve_cash)
        for ts, val in series.items():
            key = _ts_str(ts)
            bh_curves.setdefault(key, [])
            # store later sum
            bh_curves[key].append(float(val))
    bh_equity = []
    for t in sorted(bh_curves.keys()):
        vals = bh_curves[t]
        # If some symbols missing a day, scale available
        bh_equity.append(sum(vals) * (n / max(len(vals), 1)))
    bh_ret = (
        (bh_equity[-1] / bh_equity[0] - 1.0) * 100.0
        if len(bh_equity) >= 2 and bh_equity[0]
        else 0.0
    )
    sharpe, sortino = _sharpe_sortino(port_rets)
    all_pnls: list[float] = []
    all_durs: list[float] = []
    total_trades = 0
    wins = losses = 0
    for r in results:
        total_trades += int(r.get("round_trips") or 0)
        wins += int(r.get("wins") or 0)
        losses += int(r.get("losses") or 0)
        for t in r.get("trade_list") or []:
            if t.get("pnl") is not None:
                all_pnls.append(float(t["pnl"]))
                all_durs.append(float(t.get("bars_held") or 0))

    closed = wins + losses
    return {
        "symbol": "PORTFOLIO",
        "symbols": symbols,
        "initial_cash": round(float(initial_cash), 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "buy_hold_return_pct": round(bh_ret, 2),
        "buy_hold_max_drawdown_pct": _max_drawdown_pct(bh_equity),
        "max_drawdown_pct": _max_drawdown_pct(port_equity),
        "sharpe": sharpe,
        "sortino": sortino,
        "profit_factor": _profit_factor(all_pnls),
        "round_trips": total_trades,
        "trades": sum(int(r.get("trades") or 0) for r in results),
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / closed) if closed else 0.0, 4),
        "realized_pnl": round(sum(float(r.get("realized_pnl") or 0) for r in results), 2),
        "avg_trade_duration_bars": round(float(np.mean(all_durs)), 2) if all_durs else 0.0,
        "per_symbol": results,
        "equity_curve": [
            {"t": t, "equity": round(curves[t], 2)} for t in sorted_ts
        ][:400],
        "max_concurrent": max_concurrent,
    }
