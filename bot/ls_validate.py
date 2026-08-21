"""In-sample / out-of-sample validation for Regime Dual Momentum long/short.

Protocol (last ~3 months evaluation window):
  - Fetch ~180d history for indicator warmup
  - Evaluate on last 90 calendar days
  - IS = first 60% of eval bars (grid search)
  - OOS = last 40% (frozen params)
  - PASS if portfolio OOS return > OOS Buy&Hold AND OOS MaxDD < OOS B&H MaxDD
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import product
from typing import Any

import pandas as pd

from bot.ls_backtest import (
    LSRiskParams,
    run_ls_backtest,
    run_ls_portfolio_backtest,
)
from bot.ls_strategy import LongShortRegimeStrategy

DEFAULT_UNIVERSE = (
    "AAPL",
    "MU",
    "CBRS",
    "WDC",
    "SNDK",
    "SPCX",
    "SPCE",
    "VWAV",
    "SOXX",
    "OKLO",
)

# Small IS grid (plan)
_ADX_GRID = (15.0, 20.0, 25.0)
_ATR_STOP_GRID = (1.2, 1.5, 2.0)
_EMA_GRID = ((13, 34), (21, 55))


@dataclass(frozen=True)
class LSParams:
    ema_fast: int = 21
    ema_slow: int = 55
    adx_min: float = 20.0
    atr_stop_mult: float = 1.5

    def strategy(self) -> LongShortRegimeStrategy:
        return LongShortRegimeStrategy(
            ema_fast=self.ema_fast,
            ema_slow=self.ema_slow,
            adx_min=self.adx_min,
        )

    def risk(self, base: LSRiskParams | None = None) -> LSRiskParams:
        base = base or LSRiskParams()
        return replace(base, atr_stop_mult=self.atr_stop_mult)


def _is_score(result: dict[str, Any]) -> float:
    ret = float(result.get("total_return_pct") or 0.0)
    dd = float(result.get("max_drawdown_pct") or 0.0)
    return ret - 0.5 * dd


def split_eval_window(
    bars: pd.DataFrame,
    *,
    eval_start: pd.Timestamp,
) -> tuple[pd.DatetimeIndex, pd.Timestamp, pd.Timestamp]:
    """Return (eval_index, oos_start, eval_end) with 60/40 IS/OOS split."""
    idx = bars.sort_index().loc[bars.index >= eval_start].index
    if len(idx) < 10:
        raise ValueError(f"Need at least 10 eval bars, got {len(idx)}")
    split = max(1, int(len(idx) * 0.60))
    # Ensure at least a few OOS bars
    if split >= len(idx) - 2:
        split = max(1, len(idx) - 3)
    oos_start = idx[split]
    return idx, oos_start, idx[-1]


def optimize_params_is(
    bars: pd.DataFrame,
    *,
    eval_start: pd.Timestamp,
    oos_start: pd.Timestamp,
    initial_cash: float = 10_000.0,
    base_risk: LSRiskParams | None = None,
) -> tuple[LSParams, dict[str, Any]]:
    """Grid-search on IS window only (bars before oos_start)."""
    base_risk = base_risk or LSRiskParams()
    # Restrict evaluation to IS by truncating bars at oos_start (exclusive)
    is_bars = bars.loc[bars.index < oos_start]
    if is_bars.empty or len(is_bars.loc[is_bars.index >= eval_start]) < 5:
        raise ValueError("Insufficient in-sample bars")

    best: LSParams | None = None
    best_result: dict[str, Any] | None = None
    best_score = float("-inf")

    for adx_min, atr_mult, (ef, es) in product(_ADX_GRID, _ATR_STOP_GRID, _EMA_GRID):
        params = LSParams(
            ema_fast=ef, ema_slow=es, adx_min=adx_min, atr_stop_mult=atr_mult
        )
        try:
            result = run_ls_backtest(
                is_bars,
                params.strategy(),
                params.risk(base_risk),
                initial_cash=initial_cash,
                eval_start=eval_start,
            )
        except ValueError:
            continue
        score = _is_score(result)
        if score > best_score:
            best_score = score
            best = params
            best_result = result

    if best is None or best_result is None:
        best = LSParams()
        best_result = run_ls_backtest(
            is_bars,
            best.strategy(),
            best.risk(base_risk),
            initial_cash=initial_cash,
            eval_start=eval_start,
        )
    best_result = dict(best_result)
    best_result["is_score"] = round(best_score if best_score != float("-inf") else _is_score(best_result), 3)
    best_result["params"] = asdict(best)
    return best, best_result


def passes_oos(result: dict[str, Any]) -> bool:
    """OOS pass: strategy return > B&H and MaxDD < B&H MaxDD."""
    try:
        ret = float(result["total_return_pct"])
        bh = float(result["buy_hold_return_pct"])
        dd = float(result["max_drawdown_pct"])
        bh_dd = float(result["buy_hold_max_drawdown_pct"])
    except (KeyError, TypeError, ValueError):
        return False
    return ret > bh and dd < bh_dd


def validate_symbol(
    symbol: str,
    bars: pd.DataFrame,
    *,
    eval_start: pd.Timestamp,
    initial_cash: float = 10_000.0,
    base_risk: LSRiskParams | None = None,
) -> dict[str, Any]:
    """Full IS optimize + OOS validate for one ticker."""
    base_risk = base_risk or LSRiskParams()
    try:
        _, oos_start, _ = split_eval_window(bars, eval_start=eval_start)
    except ValueError as exc:
        return {
            "symbol": symbol,
            "error": str(exc),
            "passed": False,
        }

    try:
        best, is_result = optimize_params_is(
            bars,
            eval_start=eval_start,
            oos_start=oos_start,
            initial_cash=initial_cash,
            base_risk=base_risk,
        )
    except ValueError as exc:
        return {"symbol": symbol, "error": str(exc), "passed": False}

    oos_result = run_ls_backtest(
        bars,
        best.strategy(),
        best.risk(base_risk),
        initial_cash=initial_cash,
        eval_start=oos_start,
    )
    oos_result["symbol"] = symbol
    passed = passes_oos(oos_result)

    # Full-window (IS+OOS) reference with frozen params
    full = run_ls_backtest(
        bars,
        best.strategy(),
        best.risk(base_risk),
        initial_cash=initial_cash,
        eval_start=eval_start,
    )
    full["symbol"] = symbol

    return {
        "symbol": symbol,
        "params": asdict(best),
        "is": {
            "total_return_pct": is_result.get("total_return_pct"),
            "buy_hold_return_pct": is_result.get("buy_hold_return_pct"),
            "max_drawdown_pct": is_result.get("max_drawdown_pct"),
            "sharpe": is_result.get("sharpe"),
            "sortino": is_result.get("sortino"),
            "win_rate": is_result.get("win_rate"),
            "profit_factor": is_result.get("profit_factor"),
            "round_trips": is_result.get("round_trips"),
            "avg_trade_duration_bars": is_result.get("avg_trade_duration_bars"),
            "is_score": is_result.get("is_score"),
            "start": is_result.get("start"),
            "end": is_result.get("end"),
        },
        "oos": {
            "total_return_pct": oos_result.get("total_return_pct"),
            "buy_hold_return_pct": oos_result.get("buy_hold_return_pct"),
            "max_drawdown_pct": oos_result.get("max_drawdown_pct"),
            "buy_hold_max_drawdown_pct": oos_result.get("buy_hold_max_drawdown_pct"),
            "sharpe": oos_result.get("sharpe"),
            "sortino": oos_result.get("sortino"),
            "win_rate": oos_result.get("win_rate"),
            "profit_factor": oos_result.get("profit_factor"),
            "round_trips": oos_result.get("round_trips"),
            "avg_trade_duration_bars": oos_result.get("avg_trade_duration_bars"),
            "trades": oos_result.get("trades"),
            "start": oos_result.get("start"),
            "end": oos_result.get("end"),
            "passed": passed,
        },
        "full": {
            "total_return_pct": full.get("total_return_pct"),
            "buy_hold_return_pct": full.get("buy_hold_return_pct"),
            "max_drawdown_pct": full.get("max_drawdown_pct"),
            "buy_hold_max_drawdown_pct": full.get("buy_hold_max_drawdown_pct"),
            "sharpe": full.get("sharpe"),
            "sortino": full.get("sortino"),
            "win_rate": full.get("win_rate"),
            "profit_factor": full.get("profit_factor"),
            "round_trips": full.get("round_trips"),
            "avg_trade_duration_bars": full.get("avg_trade_duration_bars"),
            "trades": full.get("trades"),
        },
        "passed": passed,
        "oos_start": str(oos_start),
        "eval_start": str(eval_start),
    }


def _majority_params(rows: list[dict[str, Any]]) -> LSParams:
    """Pick portfolio params from the most common IS winners (fallback defaults)."""
    from collections import Counter

    keys: list[tuple] = []
    for r in rows:
        p = r.get("params") or {}
        if not p:
            continue
        keys.append(
            (
                int(p.get("ema_fast", 21)),
                int(p.get("ema_slow", 55)),
                float(p.get("adx_min", 20)),
                float(p.get("atr_stop_mult", 1.5)),
            )
        )
    if not keys:
        return LSParams()
    (ef, es, adx, atr_m), _ = Counter(keys).most_common(1)[0]
    return LSParams(ema_fast=ef, ema_slow=es, adx_min=adx, atr_stop_mult=atr_m)


def validate_universe(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    eval_days: int = 90,
    fetch_end: datetime | None = None,
    initial_cash: float = 10_000.0,
    base_risk: LSRiskParams | None = None,
) -> dict[str, Any]:
    """Validate each available symbol + consolidated portfolio OOS."""
    base_risk = base_risk or LSRiskParams()
    end = fetch_end or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    eval_start = pd.Timestamp(end - timedelta(days=eval_days))

    unavailable: list[str] = []
    available: dict[str, pd.DataFrame] = {}
    for sym, bars in bars_by_symbol.items():
        if bars is None or bars.empty or "close" not in bars.columns:
            unavailable.append(sym)
            continue
        # Need enough history for warmup + eval
        strat = LongShortRegimeStrategy()
        if len(bars) < strat.bars_needed + 15:
            unavailable.append(sym)
            continue
        available[sym] = bars.sort_index()

    per_symbol: list[dict[str, Any]] = []
    for sym, bars in available.items():
        per_symbol.append(
            validate_symbol(
                sym,
                bars,
                eval_start=eval_start,
                initial_cash=initial_cash,
                base_risk=base_risk,
            )
        )

    ok_rows = [r for r in per_symbol if not r.get("error")]
    port_params = _majority_params(ok_rows)

    # Shared OOS start: use median split across symbols with data
    oos_starts: list[pd.Timestamp] = []
    for sym, bars in available.items():
        try:
            _, oos_s, _ = split_eval_window(bars, eval_start=eval_start)
            oos_starts.append(pd.Timestamp(oos_s))
        except ValueError:
            continue
    if oos_starts:
        oos_starts_sorted = sorted(oos_starts)
        port_oos_start = oos_starts_sorted[len(oos_starts_sorted) // 2]
    else:
        port_oos_start = eval_start + pd.Timedelta(days=int(eval_days * 0.6))

    portfolio_oos: dict[str, Any] | None = None
    portfolio_full: dict[str, Any] | None = None
    portfolio_passed = False
    if available:
        portfolio_oos = run_ls_portfolio_backtest(
            available,
            port_params.strategy(),
            port_params.risk(base_risk),
            initial_cash=initial_cash,
            eval_start=port_oos_start,
            max_concurrent=4,
        )
        portfolio_full = run_ls_portfolio_backtest(
            available,
            port_params.strategy(),
            port_params.risk(base_risk),
            initial_cash=initial_cash,
            eval_start=eval_start,
            max_concurrent=4,
        )
        portfolio_passed = passes_oos(portfolio_oos)

    summary_rows = []
    for r in per_symbol:
        if r.get("error"):
            summary_rows.append(
                {
                    "symbol": r["symbol"],
                    "error": r["error"],
                    "passed": False,
                }
            )
            continue
        oos = r["oos"]
        full = r["full"]
        summary_rows.append(
            {
                "symbol": r["symbol"],
                "params": r.get("params"),
                "oos_return_pct": oos.get("total_return_pct"),
                "oos_buy_hold_pct": oos.get("buy_hold_return_pct"),
                "oos_max_dd_pct": oos.get("max_drawdown_pct"),
                "oos_bh_max_dd_pct": oos.get("buy_hold_max_drawdown_pct"),
                "oos_sharpe": oos.get("sharpe"),
                "oos_sortino": oos.get("sortino"),
                "oos_win_rate": oos.get("win_rate"),
                "oos_profit_factor": oos.get("profit_factor"),
                "oos_trades": oos.get("round_trips"),
                "oos_avg_duration_bars": oos.get("avg_trade_duration_bars"),
                "full_return_pct": full.get("total_return_pct"),
                "full_buy_hold_pct": full.get("buy_hold_return_pct"),
                "full_max_dd_pct": full.get("max_drawdown_pct"),
                "full_sharpe": full.get("sharpe"),
                "full_sortino": full.get("sortino"),
                "full_win_rate": full.get("win_rate"),
                "full_profit_factor": full.get("profit_factor"),
                "full_trades": full.get("round_trips"),
                "full_avg_duration_bars": full.get("avg_trade_duration_bars"),
                "passed": r.get("passed"),
            }
        )

    recommendation = (
        "PASS"
        if portfolio_passed
        else "FAIL"
    )
    caveat = (
        "Daily 3-month sample is small (~60 bars; IS≈36 / OOS≈24). "
        "Treat results as exploratory, not production-ready."
    )

    return {
        "strategy": "Regime Dual Momentum (long/short)",
        "logic": {
            "long": "EMA_fast > EMA_slow AND ADX >= adx_min AND MACD hist cross up",
            "short": "EMA_fast < EMA_slow AND ADX >= adx_min AND MACD hist cross down",
            "exits": "ATR/pct stop, 2R target, ATR trail after +1R, regime flip, 15-bar time stop",
            "frictions": "0.05% commission, 0.02% slippage, 5% APR short borrow",
        },
        "universe_requested": list(bars_by_symbol.keys()),
        "unavailable": unavailable,
        "available": list(available.keys()),
        "eval_start": str(eval_start),
        "eval_end": str(end),
        "portfolio_params": asdict(port_params),
        "portfolio_oos_start": str(port_oos_start),
        "per_symbol": per_symbol,
        "summary_table": summary_rows,
        "portfolio_oos": {
            "total_return_pct": None if not portfolio_oos else portfolio_oos.get("total_return_pct"),
            "buy_hold_return_pct": None if not portfolio_oos else portfolio_oos.get("buy_hold_return_pct"),
            "max_drawdown_pct": None if not portfolio_oos else portfolio_oos.get("max_drawdown_pct"),
            "buy_hold_max_drawdown_pct": None if not portfolio_oos else portfolio_oos.get("buy_hold_max_drawdown_pct"),
            "sharpe": None if not portfolio_oos else portfolio_oos.get("sharpe"),
            "sortino": None if not portfolio_oos else portfolio_oos.get("sortino"),
            "win_rate": None if not portfolio_oos else portfolio_oos.get("win_rate"),
            "profit_factor": None if not portfolio_oos else portfolio_oos.get("profit_factor"),
            "round_trips": None if not portfolio_oos else portfolio_oos.get("round_trips"),
            "avg_trade_duration_bars": None if not portfolio_oos else portfolio_oos.get("avg_trade_duration_bars"),
            "passed": portfolio_passed,
        },
        "portfolio_full": None
        if not portfolio_full
        else {
            "total_return_pct": portfolio_full.get("total_return_pct"),
            "buy_hold_return_pct": portfolio_full.get("buy_hold_return_pct"),
            "max_drawdown_pct": portfolio_full.get("max_drawdown_pct"),
            "buy_hold_max_drawdown_pct": portfolio_full.get("buy_hold_max_drawdown_pct"),
            "sharpe": portfolio_full.get("sharpe"),
            "sortino": portfolio_full.get("sortino"),
            "win_rate": portfolio_full.get("win_rate"),
            "profit_factor": portfolio_full.get("profit_factor"),
            "round_trips": portfolio_full.get("round_trips"),
            "avg_trade_duration_bars": portfolio_full.get("avg_trade_duration_bars"),
        },
        "recommendation": recommendation,
        "caveat": caveat,
    }


def fetch_universe_bars(
    service: Any,
    symbols: tuple[str, ...] | list[str] = DEFAULT_UNIVERSE,
    *,
    lookback_days: int = 180,
    end: datetime | None = None,
    timeframe: str = "1Day",
) -> dict[str, pd.DataFrame]:
    """Pull daily bars from Alpaca for each symbol (empty DF if missing)."""
    end = end or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(days=lookback_days)
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            bars = service.get_bars_range(
                sym, start=start, end=end, timeframe=timeframe
            )
            out[sym] = bars if bars is not None else pd.DataFrame()
        except Exception:
            out[sym] = pd.DataFrame()
    return out


def format_report(report: dict[str, Any]) -> str:
    """Human-readable CLI report."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"Strategy: {report.get('strategy')}")
    lines.append("=" * 72)
    logic = report.get("logic") or {}
    lines.append("Logic:")
    lines.append(f"  LONG : {logic.get('long')}")
    lines.append(f"  SHORT: {logic.get('short')}")
    lines.append(f"  EXIT : {logic.get('exits')}")
    lines.append(f"  COST : {logic.get('frictions')}")
    lines.append("")
    lines.append(
        f"Eval window: {report.get('eval_start')} → {report.get('eval_end')}"
    )
    lines.append(f"Available: {', '.join(report.get('available') or []) or '(none)'}")
    unav = report.get("unavailable") or []
    if unav:
        lines.append(f"Unavailable: {', '.join(unav)}")
    lines.append(f"Portfolio params: {report.get('portfolio_params')}")
    lines.append("")
    lines.append("--- Ticker summary (OOS | Full 3m) ---")
    hdr = (
        f"{'Sym':6} {'OOS%':>7} {'BH%':>7} {'DD%':>6} {'BHDD':>6} "
        f"{'Sh':>6} {'WR%':>5} {'PF':>5} {'N':>3} {'Pass':>5} "
        f"| {'Full%':>7} {'FBH%':>7}"
    )
    lines.append(hdr)
    for row in report.get("summary_table") or []:
        if row.get("error"):
            lines.append(f"{row['symbol']:6} ERROR: {row['error']}")
            continue
        wr = row.get("oos_win_rate")
        wr_s = f"{wr * 100:.0f}" if isinstance(wr, (int, float)) else "—"
        pf = row.get("oos_profit_factor")
        pf_s = f"{pf:.2f}" if isinstance(pf, (int, float)) else "—"
        sh = row.get("oos_sharpe")
        sh_s = f"{sh:.2f}" if isinstance(sh, (int, float)) else "—"
        lines.append(
            f"{row['symbol']:6} "
            f"{row.get('oos_return_pct', 0):7.2f} "
            f"{row.get('oos_buy_hold_pct', 0):7.2f} "
            f"{row.get('oos_max_dd_pct', 0):6.2f} "
            f"{row.get('oos_bh_max_dd_pct', 0):6.2f} "
            f"{sh_s:>6} "
            f"{wr_s:>5} "
            f"{pf_s:>5} "
            f"{int(row.get('oos_trades') or 0):3d} "
            f"{'YES' if row.get('passed') else 'NO':>5} "
            f"| {row.get('full_return_pct', 0):7.2f} "
            f"{row.get('full_buy_hold_pct', 0):7.2f}"
        )
    lines.append("")
    po = report.get("portfolio_oos") or {}
    pf = report.get("portfolio_full") or {}
    lines.append("--- Portfolio ---")
    lines.append(
        f"OOS  return {po.get('total_return_pct')}% vs B&H {po.get('buy_hold_return_pct')}% | "
        f"MaxDD {po.get('max_drawdown_pct')}% vs B&H DD {po.get('buy_hold_max_drawdown_pct')}% | "
        f"Sharpe {po.get('sharpe')} Sortino {po.get('sortino')} | "
        f"WR {po.get('win_rate')} PF {po.get('profit_factor')} trades {po.get('round_trips')} "
        f"avgDur {po.get('avg_trade_duration_bars')}"
    )
    if pf:
        lines.append(
            f"Full return {pf.get('total_return_pct')}% vs B&H {pf.get('buy_hold_return_pct')}% | "
            f"MaxDD {pf.get('max_drawdown_pct')}% | Sharpe {pf.get('sharpe')} "
            f"trades {pf.get('round_trips')}"
        )
    lines.append("")
    lines.append(f"FINAL RECOMMENDATION: {report.get('recommendation')}")
    lines.append(f"Caveat: {report.get('caveat')}")
    lines.append("=" * 72)
    return "\n".join(lines)
