"""Persist backtest run history to a local gitignored JSON file."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.day_presets import get_preset as get_day_preset

ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = ROOT / ".backtest_history.json"


def _path(path: Path | None = None) -> Path:
    return path or HISTORY_PATH
MAX_ENTRIES = 40

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty() -> dict[str, Any]:
    return {"seq": 0, "entries": []}


def load_raw(path: Path | None = None) -> dict[str, Any]:
    target = _path(path)
    if not target.exists():
        return _empty()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    seq = int(raw.get("seq") or 0)
    entries = raw.get("entries")
    if not isinstance(entries, list):
        entries = []
    clean: list[dict[str, Any]] = []
    for item in entries:
        if isinstance(item, dict) and item.get("id") is not None:
            clean.append(item)
    return {"seq": seq, "entries": clean[:MAX_ENTRIES]}


def save_raw(data: dict[str, Any], path: Path | None = None) -> Path:
    target = _path(path)
    payload = {
        "seq": int(data.get("seq") or 0),
        "entries": list(data.get("entries") or [])[:MAX_ENTRIES],
    }
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def summarize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
    params = result.get("params") if isinstance(result.get("params"), dict) else {}
    strat = _as_float(result.get("total_return_pct"))
    hold = _as_float(result.get("buy_hold_return_pct"))
    alpha = None
    if strat is not None and hold is not None:
        alpha = round(strat - hold, 2)
    mode = result.get("mode") or entry.get("mode")
    symbols = result.get("symbols")
    if not isinstance(symbols, list):
        symbols = None
    run_kind = result.get("run_kind") or "per_symbol"
    day_preset_id = result.get("day_preset")
    day_preset_label = get_day_preset(day_preset_id).label if day_preset_id else None
    return {
        "id": entry.get("id"),
        "created_at": entry.get("created_at"),
        "symbol": result.get("symbol") or entry.get("symbol"),
        "symbols": symbols,
        "run_kind": run_kind,
        "mode": mode,
        "label": params.get("label") or result.get("mode") or "—",
        "day_preset": day_preset_id,
        "day_preset_label": day_preset_label,
        "bar_timeframe": result.get("bar_timeframe"),
        "days": result.get("days"),
        "qty": result.get("qty"),
        "stop_loss_pct": result.get("stop_loss_pct"),
        "initial_cash": result.get("initial_cash"),
        "final_equity": result.get("final_equity"),
        "total_return_pct": result.get("total_return_pct"),
        "buy_hold_return_pct": result.get("buy_hold_return_pct"),
        "alpha_pct": alpha,
        "max_drawdown_pct": result.get("max_drawdown_pct"),
        "realized_pnl": result.get("realized_pnl"),
        "win_rate": result.get("win_rate"),
        "wins": result.get("wins"),
        "losses": result.get("losses"),
        "round_trips": result.get("round_trips"),
        "trades": result.get("trades"),
        "start": result.get("start"),
        "end": result.get("end"),
        "fast_sma": params.get("fast_sma"),
        "slow_sma": params.get("slow_sma"),
        "dip_rsi_buy": params.get("dip_rsi_buy"),
        "dip_rsi_sell": params.get("dip_rsi_sell"),
        "dip_skip_bearish": params.get("dip_skip_bearish"),
        "pair_sma_period": params.get("pair_sma_period"),
        "pair_lookback": params.get("pair_lookback"),
        "pair_impulse_pct": params.get("pair_impulse_pct"),
        "pair_long_symbol": params.get("pair_long_symbol"),
        "pair_short_symbol": params.get("pair_short_symbol"),
        "ls_ema_fast": params.get("ls_ema_fast"),
        "ls_ema_slow": params.get("ls_ema_slow"),
        "ls_adx_min": params.get("ls_adx_min"),
        "ls_rr": params.get("ls_rr"),
        "ls_risk_pct": params.get("ls_risk_pct"),
        "sharpe": result.get("sharpe"),
        "sortino": result.get("sortino"),
        "profit_factor": result.get("profit_factor"),
        "symbol_count": len(symbols) if symbols else 1,
    }


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def list_summaries(path: Path | None = None) -> list[dict[str, Any]]:
    with _lock:
        data = load_raw(path)
        return [summarize_entry(e) for e in data["entries"]]


def get_entry(entry_id: int, path: Path | None = None) -> dict[str, Any] | None:
    with _lock:
        data = load_raw(path)
        for entry in data["entries"]:
            if int(entry.get("id", -1)) == int(entry_id):
                return entry
        return None


def append_result(result: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Store a completed backtest result; newest first. Returns the saved entry."""
    with _lock:
        data = load_raw(path)
        data["seq"] = int(data.get("seq") or 0) + 1
        entry = {
            "id": data["seq"],
            "created_at": _now_iso(),
            "symbol": result.get("symbol"),
            "mode": result.get("mode"),
            "result": result,
        }
        entries = [entry, *list(data.get("entries") or [])]
        data["entries"] = entries[:MAX_ENTRIES]
        save_raw(data, path)
        return entry


def delete_entry(entry_id: int, path: Path | None = None) -> bool:
    with _lock:
        data = load_raw(path)
        before = len(data["entries"])
        data["entries"] = [
            e for e in data["entries"] if int(e.get("id", -1)) != int(entry_id)
        ]
        if len(data["entries"]) == before:
            return False
        save_raw(data, path)
        return True


def clear_all(path: Path | None = None) -> None:
    with _lock:
        save_raw(_empty(), path)


def compare_entries(
    ids: list[int],
    path: Path | None = None,
    *,
    max_runs: int = 4,
) -> list[dict[str, Any]]:
    """Return full entries for the given ids (stable request order), capped."""
    want = []
    seen: set[int] = set()
    for raw_id in ids:
        try:
            eid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if eid in seen:
            continue
        seen.add(eid)
        want.append(eid)
        if len(want) >= max_runs:
            break
    if len(want) < 2:
        raise ValueError("Select at least 2 backtest runs to compare")

    with _lock:
        data = load_raw(path)
        by_id = {int(e["id"]): e for e in data["entries"] if e.get("id") is not None}
        missing = [i for i in want if i not in by_id]
        if missing:
            raise ValueError(f"Unknown backtest id(s): {', '.join(map(str, missing))}")
        return [by_id[i] for i in want]
