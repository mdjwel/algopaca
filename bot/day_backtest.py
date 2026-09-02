"""Intraday backtest engine for the Day Trading desk.

Day Trading was the only engine with no way to measure itself — sma, dip, pair
and ls all have one. This replays `DayTradingStrategy` bar by bar with the same
session rules the live bot applies (open buffer, ATR stop, R target, daily trade
cap, EOD square-off) so a rule change can be judged instead of guessed at.

No look-ahead: a signal is computed from bars up to and including bar *i* and
filled at bar *i+1*'s open. Stops and targets are checked against a bar's own
high/low, and when both are touched in one bar the stop is assumed first.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import time as dtime
from typing import Any

import pandas as pd

from bot.day_strategy import (
    DayTradingStrategy,
    MARKET_OPEN_ET,
    eastern_index,
)
from bot.strategy import Signal

# A round turn is never free. Charging it is the point: the churn this engine is
# meant to expose only looks expensive once spread and slippage are paid.
DEFAULT_SLIPPAGE_BPS = 1.0

# A regular US session is 390 minutes. The window handed to the strategy has to
# span one whole session, because intraday VWAP and the opening range anchor to
# the current Eastern date — but no more than that, or the replay crawls.
SESSION_MINUTES = 390
SESSION_SPAN = 1.6


def lookback_bars(bars: pd.DataFrame, warmup: int) -> int:
    """Bars to hand the strategy each step, sized from the data's own spacing."""
    step_minutes = 5.0
    if isinstance(bars.index, pd.DatetimeIndex) and len(bars) > 2:
        deltas = pd.Series(bars.index[1:]) - pd.Series(bars.index[:-1])
        median = deltas.median().total_seconds() / 60.0
        if median and median > 0:
            step_minutes = float(median)
    per_session = max(1.0, SESSION_MINUTES / step_minutes)
    return max(warmup + 10, int(per_session * SESSION_SPAN))


@dataclass
class DayBacktestParams:
    """Execution and risk rules applied on top of the strategy's signals."""

    sub_mode: str = "vwap_trend"
    ema_fast: int = 9
    ema_slow: int = 21
    orb_minutes: int = 15
    side: str = "long_only"
    quality_filters: bool = True

    stop_atr_mult: float = 1.0
    profit_target_r: float = 1.2
    max_trades_per_day: int = 3
    open_buffer_mins: int = 15
    entry_cutoff_mins: int = 75
    eod_flatten_mins: int = 15
    eod_flatten: bool = True

    initial_cash: float = 25_000.0
    risk_pct: float = 0.5
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS


@dataclass
class DayTrade:
    symbol: str
    side: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    qty: float
    pnl: float
    r_multiple: float
    exit_reason: str
    entry_reason: str = ""


@dataclass
class _OpenPosition:
    side: str
    entry_price: float
    entry_time: str
    entry_bar: int
    qty: float
    stop: float
    target: float
    stop_distance: float
    entry_reason: str


def _clock(total_minutes: int) -> dtime:
    total = max(0, min(24 * 60 - 1, total_minutes))
    return dtime(total // 60, total % 60)


def _atr_series(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    from bot.analysis import _atr

    closes = bars["close"].astype(float)
    highs = bars["high"].astype(float) if "high" in bars.columns else closes
    lows = bars["low"].astype(float) if "low" in bars.columns else closes
    return _atr(highs, lows, closes, period)


def _max_drawdown_pct(equity: list[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak * 100.0)
    return round(worst, 2)


def run_day_backtest(
    bars: pd.DataFrame,
    *,
    symbol: str = "",
    params: DayBacktestParams | None = None,
) -> dict[str, Any]:
    """Replay the Day Trading engine over `bars` and score the result."""
    p = params or DayBacktestParams()
    if bars is None or bars.empty or "close" not in bars.columns:
        raise ValueError("day backtest needs bars with a close column")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("day backtest needs a DatetimeIndex of intraday bars")

    strategy = DayTradingStrategy(
        sub_mode=p.sub_mode,
        ema_fast=p.ema_fast,
        ema_slow=p.ema_slow,
        orb_minutes=p.orb_minutes,
        side=p.side,
        quality_filters=p.quality_filters,
    )

    idx_et = eastern_index(bars.index)
    atr = _atr_series(bars)
    opens = bars["open"].astype(float) if "open" in bars.columns else bars["close"].astype(float)
    highs = bars["high"].astype(float) if "high" in bars.columns else bars["close"].astype(float)
    lows = bars["low"].astype(float) if "low" in bars.columns else bars["close"].astype(float)
    closes = bars["close"].astype(float)

    slip = max(0.0, p.slippage_bps) / 10_000.0
    warmup = max(strategy.bars_needed(), 30)
    window_bars = lookback_bars(bars, warmup)
    n = len(bars)
    if n <= warmup + 2:
        raise ValueError(f"day backtest needs more than {warmup + 2} bars, got {n}")

    open_minutes = MARKET_OPEN_ET.hour * 60 + MARKET_OPEN_ET.minute
    buffer_until = _clock(open_minutes + max(0, p.open_buffer_mins))
    # Sessions are assumed to close at 16:00 ET; a backtest has no broker clock.
    flatten_from = _clock(16 * 60 - max(0, p.eod_flatten_mins))
    entry_cutoff = _clock(16 * 60 - max(0, p.entry_cutoff_mins))

    cash = float(p.initial_cash)
    equity = cash
    position: _OpenPosition | None = None
    trades: list[DayTrade] = []
    equity_curve: list[dict[str, Any]] = []
    trades_today = 0
    current_date = None
    blocked_by_filter = 0
    signals_seen = 0

    def _fill(price: float, side: str, direction: str) -> float:
        """Apply slippage against us on both legs."""
        adverse = 1.0 + slip if (side == "long") == (direction == "in") else 1.0 - slip
        return price * adverse

    def _close(bar_i: int, price: float, reason: str) -> None:
        nonlocal position, cash, equity
        assert position is not None
        exit_price = _fill(price, position.side, "out")
        direction = 1.0 if position.side == "long" else -1.0
        pnl = (exit_price - position.entry_price) * position.qty * direction
        cash += pnl
        equity = cash
        r = (
            pnl / (position.stop_distance * position.qty)
            if position.stop_distance > 0 and position.qty > 0
            else 0.0
        )
        trades.append(
            DayTrade(
                symbol=symbol,
                side=position.side,
                entry_time=position.entry_time,
                entry_price=round(position.entry_price, 4),
                exit_time=str(idx_et[bar_i]),
                exit_price=round(exit_price, 4),
                qty=round(position.qty, 4),
                pnl=round(pnl, 2),
                r_multiple=round(r, 3),
                exit_reason=reason,
                entry_reason=position.entry_reason,
            )
        )
        position = None

    for i in range(warmup, n - 1):
        stamp = idx_et[i]
        bar_date = stamp.date()
        bar_time = stamp.time()

        if bar_date != current_date:
            current_date = bar_date
            trades_today = 0

        # 1. Manage an open position against this bar's range. A position opened
        #    on this same bar's open can still be stopped inside it.
        if position is not None and i >= position.entry_bar:
            if position.side == "long":
                if lows.iloc[i] <= position.stop:
                    _close(i, position.stop, "stop")
                elif position.target > 0 and highs.iloc[i] >= position.target:
                    _close(i, position.target, "target")
            else:
                if highs.iloc[i] >= position.stop:
                    _close(i, position.stop, "stop")
                elif position.target > 0 and lows.iloc[i] <= position.target:
                    _close(i, position.target, "target")

        # 2. EOD square-off, filled at this bar's close without next-day rollover.
        next_is_new_day = (i + 1 < n) and (idx_et[i + 1].date() != bar_date)
        if (
            position is not None
            and p.eod_flatten
            and p.eod_flatten_mins > 0
            and (bar_time >= flatten_from or next_is_new_day)
        ):
            _close(i, float(closes.iloc[i]), "eod_flatten")
            equity_curve.append({"t": str(stamp), "equity": round(equity, 2)})
            continue

        # 3. Signal from data up to and including this bar; act at the next open.
        #    The window is bounded the way the live bot bounds it, which also
        #    keeps the replay linear instead of quadratic.
        window = bars.iloc[max(0, i + 1 - window_bars) : i + 1]
        result = strategy.evaluate(window)
        next_open = float(opens.iloc[i + 1])

        if position is not None:
            wants_out = (position.side == "long" and result.signal is Signal.SELL) or (
                position.side == "short" and result.signal is Signal.BUY
            )
            if wants_out:
                _close(i + 1, next_open, "signal")
        else:
            long_entry = result.signal is Signal.BUY
            short_entry = result.signal is Signal.SELL and p.side == "long_short"
            if long_entry or short_entry:
                signals_seen += 1
            if (long_entry or short_entry) and bar_time >= MARKET_OPEN_ET:
                blocked = (
                    bar_time < buffer_until
                    or (p.max_trades_per_day > 0 and trades_today >= p.max_trades_per_day)
                    or (p.eod_flatten and p.eod_flatten_mins > 0 and bar_time >= flatten_from)
                    or (p.entry_cutoff_mins > 0 and bar_time >= entry_cutoff)
                )
                if blocked:
                    blocked_by_filter += 1
                else:
                    bar_atr = float(atr.iloc[i])
                    if math.isfinite(bar_atr) and bar_atr > 0:
                        side = "long" if long_entry else "short"
                        entry_price = _fill(next_open, side, "in")
                        stop_distance = bar_atr * max(0.1, p.stop_atr_mult)
                        risk_budget = max(0.0, equity * max(0.0, p.risk_pct) / 100.0)
                        raw_qty = risk_budget / stop_distance if stop_distance > 0 else 0.0
                        if raw_qty * next_open > equity and next_open > 0:
                            raw_qty = equity / next_open
                        qty = round(raw_qty, 4)
                        if qty >= 0.001:
                            direction = 1.0 if side == "long" else -1.0
                            position = _OpenPosition(
                                side=side,
                                entry_price=entry_price,
                                entry_time=str(idx_et[i + 1]),
                                entry_bar=i + 1,
                                qty=float(qty),
                                stop=entry_price - direction * stop_distance,
                                target=(
                                    entry_price
                                    + direction * stop_distance * max(0.0, p.profit_target_r)
                                    if p.profit_target_r > 0
                                    else 0.0
                                ),
                                stop_distance=stop_distance,
                                entry_reason=result.reason,
                            )
                            trades_today += 1

        equity_curve.append({"t": str(stamp), "equity": round(equity, 2)})

    if position is not None:
        _close(n - 1, float(bars["close"].astype(float).iloc[-1]), "end_of_data")
        equity_curve.append({"t": str(idx_et[-1]), "equity": round(equity, 2)})

    first_price = float(bars["close"].astype(float).iloc[warmup]) if warmup < n else 0.0
    last_price = float(bars["close"].astype(float).iloc[-1]) if n else 0.0
    buy_hold_pct = round((last_price / first_price - 1.0) * 100.0, 2) if first_price > 0 else 0.0

    return _score(
        trades,
        equity_curve,
        p,
        symbol,
        signals_seen,
        blocked_by_filter,
        start_time=str(idx_et[warmup]) if warmup < len(idx_et) else str(idx_et[0]),
        end_time=str(idx_et[-1]) if len(idx_et) else "",
        total_bars=n,
        warmup_bars=warmup,
        buy_hold_pct=buy_hold_pct,
    )


def _score(
    trades: list[DayTrade],
    equity_curve: list[dict[str, Any]],
    p: DayBacktestParams,
    symbol: str,
    signals_seen: int,
    blocked_by_filter: int,
    *,
    start_time: str = "",
    end_time: str = "",
    total_bars: int = 0,
    warmup_bars: int = 0,
    buy_hold_pct: float = 0.0,
) -> dict[str, Any]:
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    final_equity = equity_curve[-1]["equity"] if equity_curve else p.initial_cash
    r_values = [t.r_multiple for t in trades]
    closed = len(trades)
    win_rate = round(len(wins) / closed, 4) if closed else 0.0

    trade_list: list[dict[str, Any]] = []
    for idx, t in enumerate(trades, start=1):
        entry_side = "buy" if t.side == "long" else "short"
        exit_side = "sell" if t.side == "long" else "cover"
        pnl_pct = (
            round((t.exit_price / t.entry_price - 1.0) * 100.0 * (1.0 if t.side == "long" else -1.0), 2)
            if t.entry_price
            else 0.0
        )
        trade_list.append(
            {
                "symbol": symbol,
                "side": entry_side,
                "time": t.entry_time,
                "price": t.entry_price,
                "qty": t.qty,
                "reason": t.entry_reason or f"entry {t.side}",
                "pnl": None,
                "pnl_pct": None,
                "group_id": idx,
            }
        )
        trade_list.append(
            {
                "symbol": symbol,
                "side": exit_side,
                "time": t.exit_time,
                "price": t.exit_price,
                "qty": t.qty,
                "reason": t.exit_reason or "exit",
                "pnl": t.pnl,
                "pnl_pct": pnl_pct,
                "group_id": idx,
            }
        )

    return {
        "symbol": symbol,
        "sub_mode": p.sub_mode,
        "quality_filters": p.quality_filters,
        "trades": closed,
        "round_trips": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "win_rate_pct": round(win_rate * 100.0, 2),
        "total_return_pct": round((final_equity / p.initial_cash - 1.0) * 100.0, 2),
        "buy_hold_return_pct": buy_hold_pct,
        "initial_cash": round(float(p.initial_cash), 2),
        "final_equity": round(final_equity, 2),
        "realized_pnl": round(sum(t.pnl for t in trades), 2),
        "open_qty": 0.0,
        "open_entry": None,
        "open_mark": None,
        "unrealized_pnl": 0.0,
        "unrealized_pnl_pct": 0.0,
        "start": start_time,
        "end": end_time,
        "bars": total_bars,
        "warmup_bars": warmup_bars,
        "evaluated_bars": max(0, total_bars - warmup_bars),
        "avg_r": round(sum(r_values) / len(r_values), 3) if r_values else 0.0,
        "total_r": round(sum(r_values), 2),
        "expectancy": round(sum(t.pnl for t in trades) / len(trades), 2) if trades else 0.0,
        "profit_factor": (
            round(gross_win / gross_loss, 2)
            if gross_loss > 0
            else (float("inf") if gross_win > 0 else 0.0)
        ),
        "max_drawdown_pct": _max_drawdown_pct([e["equity"] for e in equity_curve]),
        "exit_breakdown": _exit_breakdown(trades),
        "signals_seen": signals_seen,
        "blocked_by_session_rules": blocked_by_filter,
        "trade_list": trade_list,
        "trade_log": [asdict(t) for t in trades],
        "equity_curve": equity_curve,
        "params": asdict(p),
    }


def _exit_breakdown(trades: list[DayTrade]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in trades:
        out[t.exit_reason] = out.get(t.exit_reason, 0) + 1
    return out


def compare_quality_filters(
    bars: pd.DataFrame, *, symbol: str = "", params: DayBacktestParams | None = None
) -> dict[str, Any]:
    """Run the same bars with the quality filters off and on."""
    base = params or DayBacktestParams()
    raw = run_day_backtest(
        bars, symbol=symbol, params=DayBacktestParams(**{**asdict(base), "quality_filters": False})
    )
    filtered = run_day_backtest(
        bars, symbol=symbol, params=DayBacktestParams(**{**asdict(base), "quality_filters": True})
    )
    return {
        "raw": raw,
        "filtered": filtered,
        "delta": {
            key: round(filtered[key] - raw[key], 3)
            for key in ("trades", "total_return_pct", "avg_r", "total_r", "max_drawdown_pct")
        },
    }


def run_day_portfolio_backtest(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    params: DayBacktestParams | None = None,
) -> dict[str, Any]:
    """Shared-cash walk-forward Day Trading backtest across multiple symbols."""
    p = params or DayBacktestParams()
    from bot.backtest import summary_from_result

    valid_frames: dict[str, pd.DataFrame] = {}
    warmups: dict[str, int] = {}
    window_bars_map: dict[str, int] = {}
    atrs: dict[str, pd.Series] = {}
    strategies: dict[str, DayTradingStrategy] = {}

    for sym, df in bars_by_symbol.items():
        if df is None or df.empty or "close" not in df.columns or not isinstance(df.index, pd.DatetimeIndex):
            continue
        strat = DayTradingStrategy(
            sub_mode=p.sub_mode,
            ema_fast=p.ema_fast,
            ema_slow=p.ema_slow,
            orb_minutes=p.orb_minutes,
            side=p.side,
            quality_filters=p.quality_filters,
        )
        w = max(strat.bars_needed(), 30)
        if len(df) <= w + 2:
            continue
        valid_frames[sym] = df
        warmups[sym] = w
        window_bars_map[sym] = lookback_bars(df, w)
        atrs[sym] = _atr_series(df)
        strategies[sym] = strat

    if not valid_frames:
        raise ValueError("Need enough intraday bar data for at least one symbol")

    symbols = list(valid_frames.keys())
    slip = max(0.0, p.slippage_bps) / 10_000.0
    open_minutes = MARKET_OPEN_ET.hour * 60 + MARKET_OPEN_ET.minute
    buffer_until = _clock(open_minutes + max(0, p.open_buffer_mins))
    flatten_from = _clock(16 * 60 - max(0, p.eod_flatten_mins))
    entry_cutoff = _clock(16 * 60 - max(0, p.entry_cutoff_mins))

    cash = float(p.initial_cash)
    positions: dict[str, _OpenPosition | None] = {s: None for s in symbols}
    trades_by_sym: dict[str, list[DayTrade]] = {s: [] for s in symbols}
    trades_today: dict[str, int] = {s: 0 for s in symbols}
    current_date = None
    next_trade_id = 1
    trade_list: list[dict[str, Any]] = []

    # Map each frame by ET timestamp
    et_frames: dict[str, pd.DataFrame] = {}
    idx_et_map: dict[str, pd.DatetimeIndex] = {}
    for s, df in valid_frames.items():
        et_idx = eastern_index(df.index)
        idx_et_map[s] = et_idx
        f = df.copy()
        f.index = et_idx
        et_frames[s] = f

    all_stamps = sorted({ts for f in et_frames.values() for ts in f.index})
    last_close: dict[str, float] = {}

    def _fill(price: float, side: str, direction: str) -> float:
        adverse = 1.0 + slip if (side == "long") == (direction == "in") else 1.0 - slip
        return price * adverse

    def _close_pos(sym: str, bar_time: Any, price: float, reason: str) -> None:
        nonlocal cash, next_trade_id
        pos = positions[sym]
        assert pos is not None
        exit_price = _fill(price, pos.side, "out")
        direction = 1.0 if pos.side == "long" else -1.0
        pnl = (exit_price - pos.entry_price) * pos.qty * direction
        cash += pnl
        r = (
            pnl / (pos.stop_distance * pos.qty)
            if pos.stop_distance > 0 and pos.qty > 0
            else 0.0
        )
        trades_by_sym[sym].append(
            DayTrade(
                symbol=sym,
                side=pos.side,
                entry_time=pos.entry_time,
                entry_price=round(pos.entry_price, 4),
                exit_time=str(bar_time),
                exit_price=round(exit_price, 4),
                qty=round(pos.qty, 4),
                pnl=round(pnl, 2),
                r_multiple=round(r, 3),
                exit_reason=reason,
                entry_reason=pos.entry_reason,
            )
        )
        gid = next_trade_id
        next_trade_id += 1
        entry_side = "buy" if pos.side == "long" else "short"
        exit_side = "sell" if pos.side == "long" else "cover"
        pnl_pct = (
            round((exit_price / pos.entry_price - 1.0) * 100.0 * direction, 2)
            if pos.entry_price
            else 0.0
        )
        trade_list.append(
            {
                "symbol": sym,
                "side": entry_side,
                "time": pos.entry_time,
                "price": round(pos.entry_price, 4),
                "qty": round(pos.qty, 4),
                "reason": pos.entry_reason or f"entry {pos.side}",
                "pnl": None,
                "pnl_pct": None,
                "group_id": gid,
            }
        )
        trade_list.append(
            {
                "symbol": sym,
                "side": exit_side,
                "time": str(bar_time),
                "price": round(exit_price, 4),
                "qty": round(pos.qty, 4),
                "reason": reason,
                "pnl": round(pnl, 2),
                "pnl_pct": pnl_pct,
                "group_id": gid,
            }
        )
        positions[sym] = None

    equity_curve: list[dict[str, Any]] = []

    # Equal-weight hold calculation
    first_prices = {
        s: float(et_frames[s]["close"].iloc[warmups[s]])
        for s in symbols
    }
    hold_alloc = float(p.initial_cash) / max(1, len(symbols))
    hold_shares = {s: hold_alloc / first_prices[s] if first_prices[s] > 0 else 0.0 for s in symbols}

    for stamp in all_stamps:
        b_date = stamp.date()
        b_time = stamp.time()
        if b_date != current_date:
            current_date = b_date
            for s in symbols:
                trades_today[s] = 0

        # Update last prices
        for s in symbols:
            f = et_frames[s]
            if stamp in f.index:
                last_close[s] = float(f.loc[stamp, "close"])

        # 1. Manage open positions
        for s in symbols:
            pos = positions[s]
            if pos is None:
                continue
            f = et_frames[s]
            if stamp not in f.index:
                continue
            row = f.loc[stamp]
            low_val = float(row["low"]) if "low" in row else float(row["close"])
            high_val = float(row["high"]) if "high" in row else float(row["close"])

            if pos.side == "long":
                if low_val <= pos.stop:
                    _close_pos(s, stamp, pos.stop, "stop")
                elif pos.target > 0 and high_val >= pos.target:
                    _close_pos(s, stamp, pos.target, "target")
            else:
                if high_val >= pos.stop:
                    _close_pos(s, stamp, pos.stop, "stop")
                elif pos.target > 0 and low_val <= pos.target:
                    _close_pos(s, stamp, pos.target, "target")

        # 2. EOD square-off
        if p.eod_flatten and p.eod_flatten_mins > 0 and b_time >= flatten_from:
            for s in symbols:
                if positions[s] is not None:
                    _close_pos(s, stamp, last_close.get(s, positions[s].entry_price), "eod_flatten")

        # 3. New entries
        # Calculate current equity for sizing
        book_eq = cash + sum(
            (last_close.get(s, pos.entry_price) - pos.entry_price) * pos.qty * (1.0 if pos.side == "long" else -1.0)
            for s, pos in positions.items()
            if pos is not None
        )

        for s in symbols:
            if positions[s] is not None:
                continue
            f = et_frames[s]
            if stamp not in f.index:
                continue
            loc_idx = f.index.get_loc(stamp)
            if isinstance(loc_idx, slice) or not isinstance(loc_idx, int):
                continue
            w = warmups[s]
            if loc_idx < w or loc_idx >= len(f) - 1:
                continue

            strat = strategies[s]
            wb = window_bars_map[s]
            window = valid_frames[s].iloc[max(0, loc_idx + 1 - wb) : loc_idx + 1]
            res = strat.evaluate(window)
            next_open = float(valid_frames[s]["open"].iloc[loc_idx + 1]) if "open" in valid_frames[s] else float(valid_frames[s]["close"].iloc[loc_idx + 1])
            next_stamp = f.index[loc_idx + 1]

            long_entry = res.signal is Signal.BUY
            short_entry = res.signal is Signal.SELL and p.side == "long_short"

            if (long_entry or short_entry) and b_time >= MARKET_OPEN_ET:
                blocked = (
                    b_time < buffer_until
                    or (p.max_trades_per_day > 0 and trades_today[s] >= p.max_trades_per_day)
                    or (p.eod_flatten and p.eod_flatten_mins > 0 and b_time >= flatten_from)
                    or (p.entry_cutoff_mins > 0 and b_time >= entry_cutoff)
                )
                if not blocked:
                    bar_atr = float(atrs[s].iloc[loc_idx]) if loc_idx < len(atrs[s]) else 0.0
                    if math.isfinite(bar_atr) and bar_atr > 0:
                        side = "long" if long_entry else "short"
                        entry_price = _fill(next_open, side, "in")
                        stop_dist = bar_atr * max(0.1, p.stop_atr_mult)
                        risk_budget = max(0.0, book_eq * max(0.0, p.risk_pct) / 100.0)
                        raw_qty = risk_budget / stop_dist if stop_dist > 0 else 0.0
                        if entry_price > 0 and cash > 0 and raw_qty * entry_price > cash:
                            raw_qty = cash / entry_price
                        qty = round(raw_qty, 4)
                        if qty >= 0.001:
                            direction = 1.0 if side == "long" else -1.0
                            positions[s] = _OpenPosition(
                                side=side,
                                entry_price=entry_price,
                                entry_time=str(next_stamp),
                                entry_bar=loc_idx + 1,
                                qty=float(qty),
                                stop=entry_price - direction * stop_dist,
                                target=(
                                    entry_price + direction * stop_dist * max(0.0, p.profit_target_r)
                                    if p.profit_target_r > 0
                                    else 0.0
                                ),
                                stop_distance=stop_dist,
                                entry_reason=res.reason,
                            )
                            trades_today[s] += 1

        book_eq = cash + sum(
            (last_close.get(s, pos.entry_price) - pos.entry_price) * pos.qty * (1.0 if pos.side == "long" else -1.0)
            for s, pos in positions.items()
            if pos is not None
        )
        equity_curve.append({"t": str(stamp), "equity": round(book_eq, 2)})

    # Flatten open positions at end
    for s in symbols:
        if positions[s] is not None:
            _close_pos(s, all_stamps[-1], last_close.get(s, positions[s].entry_price), "end_of_data")

    final_equity = equity_curve[-1]["equity"] if equity_curve else p.initial_cash
    all_trades: list[DayTrade] = [t for sub in trades_by_sym.values() for t in sub]
    wins = [t for t in all_trades if t.pnl > 0]
    losses = [t for t in all_trades if t.pnl < 0]
    closed = len(all_trades)
    win_rate = round(len(wins) / closed, 4) if closed else 0.0

    # Build per-symbol leg summaries
    legs: list[dict[str, Any]] = []
    for s in symbols:
        s_trades = trades_by_sym[s]
        s_wins = [t for t in s_trades if t.pnl > 0]
        s_losses = [t for t in s_trades if t.pnl < 0]
        s_closed = len(s_trades)
        s_wr = round(len(s_wins) / s_closed, 4) if s_closed else 0.0
        s_pnl = round(sum(t.pnl for t in s_trades), 2)
        s_f = et_frames[s]
        s_w = warmups[s]
        s_first_p = float(s_f["close"].iloc[s_w]) if s_w < len(s_f) else 0.0
        s_last_p = float(s_f["close"].iloc[-1]) if len(s_f) else 0.0
        s_bh = round((s_last_p / s_first_p - 1.0) * 100.0, 2) if s_first_p > 0 else 0.0
        s_ret = round((s_pnl / hold_alloc) * 100.0, 2) if hold_alloc > 0 else 0.0
        s_trade_list = [t for t in trade_list if t.get("symbol") == s]

        legs.append(
            {
                "symbol": s,
                "mode": "day",
                "days": len(all_stamps),
                "run_kind": "portfolio_leg",
                "trades": s_closed,
                "round_trips": s_closed,
                "wins": len(s_wins),
                "losses": len(s_losses),
                "win_rate": s_wr,
                "win_rate_pct": round(s_wr * 100.0, 2),
                "realized_pnl": s_pnl,
                "total_return_pct": s_ret,
                "buy_hold_return_pct": s_bh,
                "initial_cash": hold_alloc,
                "final_equity": round(hold_alloc + s_pnl, 2),
                "trade_list": s_trade_list,
            }
        )

    # Equal-weight buy & hold return
    last_hold_equity = sum(hold_shares[s] * last_close.get(s, first_prices[s]) for s in symbols)
    buy_hold_pct = round((last_hold_equity / p.initial_cash - 1.0) * 100.0, 2)

    return {
        "symbol": "+".join(symbols),
        "mode": "day",
        "run_kind": "portfolio",
        "symbols": symbols,
        "initial_cash": round(float(p.initial_cash), 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity / p.initial_cash - 1.0) * 100.0, 2),
        "buy_hold_return_pct": buy_hold_pct,
        "max_drawdown_pct": _max_drawdown_pct([e["equity"] for e in equity_curve]),
        "realized_pnl": round(sum(t.pnl for t in all_trades), 2),
        "trades": closed,
        "round_trips": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "win_rate_pct": round(win_rate * 100.0, 2),
        "start": str(all_stamps[0]) if all_stamps else None,
        "end": str(all_stamps[-1]) if all_stamps else None,
        "evaluated_bars": len(all_stamps),
        "trade_list": trade_list,
        "equity_curve": equity_curve,
        "results": legs,
        "summary": [summary_from_result(r) for r in legs],
        "params": asdict(p),
    }

