"""Full-capital rotation backtest for a long/short (or inverse) pair."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from bot.backtest import _downsample_curve, _max_drawdown_pct, _ts_str
from bot.pair_strategy import (
    PairTarget,
    SoxRegimeImpulseStrategy,
    adjust_inverse_splits,
)


def run_pair_backtest(
    long_bars: pd.DataFrame,
    short_bars: pd.DataFrame,
    strategy: SoxRegimeImpulseStrategy,
    *,
    initial_cash: float = 10_000.0,
    slip_bps: float = 5.0,
    adjust_splits: bool = True,
) -> dict[str, Any]:
    """Walk-forward full-equity rotation between long leg, inverse leg, or cash.

    Decision at close t earns the return from t → t+1 (same info timing as the
    research harness). Optional per-side slippage on switches. Inverse-leg
    reverse splits are detected via pair mismatch and backward-adjusted.
    """
    if long_bars is None or long_bars.empty or "close" not in long_bars.columns:
        raise ValueError("Need long-leg bars with a close column")
    if short_bars is None or short_bars.empty or "close" not in short_bars.columns:
        raise ValueError("Need short-leg bars with a close column")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be > 0")

    long_px = long_bars["close"].astype(float).sort_index()
    short_raw = short_bars["close"].astype(float).sort_index()
    idx = long_px.index.intersection(short_raw.index)
    if len(idx) < strategy.bars_needed + 2:
        raise ValueError(
            f"Need at least {strategy.bars_needed + 2} overlapping bars, got {len(idx)}"
        )
    long_px = long_px.loc[idx]
    short_raw = short_raw.loc[idx]

    split_meta: list[dict] = []
    if adjust_splits:
        short_px, split_meta = adjust_inverse_splits(long_px, short_raw)
    else:
        short_px = short_raw

    signals = strategy.signal_series(long_px)
    warmup = strategy.bars_needed
    sig = signals.iloc[warmup:]
    if len(sig) < 3:
        raise ValueError("Not enough bars after warmup")

    slip = max(0.0, float(slip_bps)) / 10_000.0
    rets: list[float] = []
    equity_curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    closed_pnls: list[float] = []
    switches = 0
    short_days = 0
    equity = float(initial_cash)
    position: str | None = None  # long | short | None
    entry_equity = equity
    entry_price = 0.0
    position_qty = 0.0
    next_group_id = 1
    open_group_id: int | None = None
    next_rotation_id = 1

    long_s = strategy.long_symbol
    short_s = strategy.short_symbol

    def _leg_price(target: str, ts: pd.Timestamp) -> float:
        if target == PairTarget.LONG.value:
            return float(long_px.loc[ts])
        if target == PairTarget.SHORT.value:
            return float(short_px.loc[ts])
        return 0.0

    def _target_symbol(target: str | None) -> str | None:
        if target == PairTarget.LONG.value:
            return long_s
        if target == PairTarget.SHORT.value:
            return short_s
        return None

    def _shares_from_equity(px: float, book: float) -> float:
        """Share count for full-equity sizing at a trade print."""
        if px <= 0 or book <= 0:
            return 0.0
        return round(book / px, 6)

    def _cash_balance() -> float:
        # Full-equity model: cash is 0 while a leg is open.
        return float(equity if position is None else 0.0)

    def _append_trade(
        *,
        side: str,
        symbol: str | None,
        time: str,
        price: float,
        qty: float,
        reason: str,
        pnl: float | None,
        pnl_pct: float | None,
        group_id: int | None,
        rotation_id: int | None,
        equity_before: float,
        equity_after: float,
        cash_before: float,
        cash_after: float,
        slip_cost: float,
    ) -> None:
        trades.append(
            {
                "side": side,
                "symbol": symbol,
                "time": time,
                "price": round(price, 4),
                "qty": round(float(qty), 6),
                "reason": reason,
                "pnl": None if pnl is None else round(pnl, 2),
                "pnl_pct": None if pnl_pct is None else round(pnl_pct, 2),
                "group_id": group_id,
                "rotation_id": rotation_id,
                "equity_before": round(equity_before, 2),
                "equity_after": round(equity_after, 2),
                "cash_before": round(cash_before, 2),
                "cash_after": round(cash_after, 2),
                "slip_cost": round(slip_cost, 2),
                "slip_bps": float(slip_bps),
                "initial_cash": round(float(initial_cash), 2),
            }
        )

    for i in range(len(sig) - 1):
        ts = sig.index[i]
        nxt = sig.index[i + 1]
        target = str(sig.iloc[i])

        if target == PairTarget.LONG.value:
            r = float(long_px.loc[nxt] / long_px.loc[ts] - 1.0)
        elif target == PairTarget.SHORT.value:
            r = float(short_px.loc[nxt] / short_px.loc[ts] - 1.0)
            short_days += 1
        else:
            r = 0.0

        prev = str(sig.iloc[i - 1]) if i > 0 else None
        switched = i > 0 and target != prev

        if switched:
            switches += 1
            assert prev is not None
            rotation_id: int | None = None
            selling = prev != PairTarget.CASH.value and position is not None
            buying = target != PairTarget.CASH.value
            if selling and buying:
                rotation_id = next_rotation_id
                next_rotation_id += 1

            if selling:
                r -= slip
                sell_px = _leg_price(prev, ts)
                sell_qty = position_qty if position_qty > 0 else _shares_from_equity(
                    sell_px, equity
                )
                leg_pnl = equity - entry_equity
                closed_pnls.append(leg_pnl)
                slip_cost = equity * slip
                # Slippage is applied via the bar return; show effective book after friction.
                equity_after_sell = equity - slip_cost
                from_sym = _target_symbol(prev)
                to_sym = _target_symbol(target) if buying else "CASH"
                reason = (
                    f"Rotated from {from_sym} to {to_sym}"
                    if buying
                    else f"rotate out of {prev} → cash"
                )
                _append_trade(
                    side="sell",
                    symbol=from_sym,
                    time=_ts_str(ts),
                    price=sell_px,
                    qty=sell_qty,
                    reason=reason,
                    pnl=leg_pnl,
                    pnl_pct=(leg_pnl / entry_equity) * 100.0 if entry_equity else 0.0,
                    group_id=open_group_id,
                    rotation_id=rotation_id,
                    equity_before=equity,
                    equity_after=equity_after_sell,
                    cash_before=_cash_balance(),
                    cash_after=equity_after_sell,  # flat after exit (pre-rebuy)
                    slip_cost=slip_cost,
                )
                position = None
                position_qty = 0.0
                open_group_id = None
                entry_price = 0.0

            if buying:
                r -= slip
                buy_px = _leg_price(target, ts)
                # Share qty is notional (returns apply to the equity scalar).
                buy_qty = _shares_from_equity(buy_px, equity)
                slip_cost = equity * slip
                group_id = next_group_id
                next_group_id += 1
                from_sym = _target_symbol(prev) if selling else "CASH"
                to_sym = _target_symbol(target)
                reason = (
                    f"Rotated from {from_sym} to {to_sym}"
                    if selling
                    else f"rotate into {target}"
                )
                _append_trade(
                    side="buy",
                    symbol=to_sym,
                    time=_ts_str(ts),
                    price=buy_px,
                    qty=buy_qty,
                    reason=reason,
                    pnl=None,
                    pnl_pct=None,
                    group_id=group_id,
                    rotation_id=rotation_id,
                    equity_before=equity,
                    equity_after=equity,
                    cash_before=equity if not selling else 0.0,
                    cash_after=0.0,
                    slip_cost=slip_cost,
                )
                position = target
                position_qty = buy_qty
                open_group_id = group_id
                entry_price = buy_px
                entry_equity = equity
            else:
                entry_price = 0.0
                entry_equity = equity
                position = None
                position_qty = 0.0
                open_group_id = None

        # First evaluation bar: open initial position (counts as a trade, not a switch).
        if i == 0 and target != PairTarget.CASH.value and position is None:
            buy_px = _leg_price(target, ts)
            buy_qty = _shares_from_equity(buy_px, equity)
            slip_cost = equity * slip if slip > 0 else 0.0
            group_id = next_group_id
            next_group_id += 1
            _append_trade(
                side="buy",
                symbol=_target_symbol(target),
                time=_ts_str(ts),
                price=buy_px,
                qty=buy_qty,
                reason=f"initial {target}",
                pnl=None,
                pnl_pct=None,
                group_id=group_id,
                rotation_id=None,
                equity_before=equity,
                equity_after=equity,
                cash_before=equity,
                cash_after=0.0,
                slip_cost=slip_cost,
            )
            if slip > 0:
                r -= slip
            position = target
            position_qty = buy_qty
            open_group_id = group_id
            entry_equity = equity
            entry_price = buy_px

        equity *= 1.0 + r
        rets.append(r)
        equity_curve.append(
            {
                "t": _ts_str(nxt),
                "equity": round(equity, 2),
                "cash": round(float(equity if position is None else 0.0), 2),
                "price": round(float(long_px.loc[nxt]), 4),
                "position": _target_symbol(position) or "",
                "position_qty": round(position_qty, 6) if position is not None else 0.0,
            }
        )

    first_ts = sig.index[0]
    last_ts = sig.index[-1]
    bh_start = float(long_px.loc[first_ts])
    bh_end = float(long_px.loc[last_ts])
    buy_hold_pct = (bh_end / bh_start - 1.0) * 100.0 if bh_start else 0.0

    total_return_pct = (equity / initial_cash - 1.0) * 100.0 if initial_cash else 0.0
    equities = [p["equity"] for p in equity_curve]
    wins = sum(1 for p in closed_pnls if p > 0)
    losses = sum(1 for p in closed_pnls if p < 0)
    closed = len(closed_pnls)
    win_rate = (wins / closed) if closed else 0.0

    open_qty = 0.0
    open_entry = None
    open_reason = None
    open_mark = None
    unrealized_pnl = 0.0
    unrealized_pnl_pct = 0.0
    if position is not None:
        mark_px = _leg_price(position, last_ts)
        open_qty = (
            position_qty
            if position_qty > 0
            else _shares_from_equity(mark_px, equity)
        )
        open_entry = round(entry_price, 4) if entry_price > 0 else None
        open_mark = round(mark_px, 4)
        open_reason = f"open {position}"
        unrealized_pnl = round(equity - entry_equity, 2)
        unrealized_pnl_pct = round(
            (unrealized_pnl / entry_equity) * 100.0 if entry_equity else 0.0, 2
        )

    counts = sig.iloc[:-1].value_counts(normalize=True) if len(sig) > 1 else pd.Series()
    alloc = {
        "long_pct": round(float(counts.get(PairTarget.LONG.value, 0.0)) * 100, 1),
        "short_pct": round(float(counts.get(PairTarget.SHORT.value, 0.0)) * 100, 1),
        "cash_pct": round(float(counts.get(PairTarget.CASH.value, 0.0)) * 100, 1),
    }

    return {
        "bars": len(idx),
        "warmup_bars": warmup,
        "evaluated_bars": max(0, len(sig) - 1),
        "start": _ts_str(first_ts),
        "end": _ts_str(last_ts),
        "initial_cash": round(float(initial_cash), 2),
        "final_equity": round(float(equity), 2),
        # Full-equity model: cash is 0 while a leg is open; else entire book is cash.
        "final_cash": round(float(equity if position is None else 0.0), 2),
        "qty": None,  # full-equity rotation (not fixed share size)
        "open_qty": round(open_qty, 6) if open_qty else 0.0,
        "open_entry": open_entry,
        "open_mark": open_mark,
        "open_reason": open_reason,
        "open_symbol": _target_symbol(position),
        "open_group_id": open_group_id,
        "unrealized_pnl": unrealized_pnl if position is not None else 0.0,
        "unrealized_pnl_pct": unrealized_pnl_pct if position is not None else 0.0,
        "total_return_pct": round(total_return_pct, 2),
        "buy_hold_return_pct": round(buy_hold_pct, 2),
        "max_drawdown_pct": _max_drawdown_pct(equities),
        "trades": len(trades),
        "trade_legs": len(trades),
        "round_trips": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "realized_pnl": round(sum(closed_pnls), 2),
        "trade_list": trades,
        "equity_curve": _downsample_curve(equity_curve),
        "slip_bps": float(slip_bps),
        "splits_adjusted": split_meta,
        "allocation": alloc,
        # Percent of evaluated days spent on the short leg (legacy alias: soxs_days_pct).
        "short_days_pct": round(100.0 * short_days / max(len(rets), 1), 1),
        "soxs_days_pct": round(100.0 * short_days / max(len(rets), 1), 1),
        "long_symbol": long_s,
        "short_symbol": short_s,
        "switches": switches,
        "mean_daily_return_pct": round(float(np.mean(rets) * 100), 4) if rets else 0.0,
    }


def build_pair_strategy(
    *,
    sma_period: int = 50,
    lookback: int = 7,
    impulse_pct: float = 5.0,
    weak_side: str = "LONG",
    long_symbol: str,
    short_symbol: str,
) -> SoxRegimeImpulseStrategy:
    return SoxRegimeImpulseStrategy(
        sma_period=sma_period,
        lookback=lookback,
        impulse_pct=impulse_pct,
        weak_side=weak_side,
        long_symbol=long_symbol,
        short_symbol=short_symbol,
    )
