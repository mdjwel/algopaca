#!/usr/bin/env python3
"""CLI: fetch Alpaca bars and validate Regime Dual Momentum long/short strategy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as `python scripts/run_ls_validation.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backtest & validate dual long/short Regime Dual Momentum on the "
            "target universe (last 3 months, IS 60% / OOS 40%)."
        )
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols (default: plan universe)",
    )
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--eval-days", type=int, default=90)
    parser.add_argument("--lookback-days", type=int, default=180)
    parser.add_argument(
        "--json-out",
        default="",
        help="Optional path to write full JSON report",
    )
    args = parser.parse_args()

    from bot.config import Config, live_allowed_from_env
    from bot.client import AlpacaService
    from bot.ls_validate import (
        DEFAULT_UNIVERSE,
        fetch_universe_bars,
        format_report,
        validate_universe,
    )

    try:
        config = Config.from_env()
    except Exception as exc:
        print(
            f"ERROR: Could not load Alpaca config ({exc}).\n"
            "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env, then retry.",
            file=sys.stderr,
        )
        return 1

    if not config.paper:
        if not live_allowed_from_env():
            print(
                "ERROR: Live mode requires ALPACA_ALLOW_LIVE=true.",
                file=sys.stderr,
            )
            return 1
        print("WARNING: Using LIVE Alpaca credentials for bar fetch.", file=sys.stderr)

    if args.symbols.strip():
        symbols = tuple(
            s.strip().upper()
            for s in args.symbols.replace(";", ",").split(",")
            if s.strip()
        )
    else:
        symbols = DEFAULT_UNIVERSE

    print("Fetching bars from Alpaca (IEX)…")
    service = AlpacaService(config)
    bars_by_symbol = fetch_universe_bars(
        service,
        symbols,
        lookback_days=args.lookback_days,
    )
    for sym, df in bars_by_symbol.items():
        n = 0 if df is None or df.empty else len(df)
        print(f"  {sym}: {n} bars")

    print("\nRunning IS/OOS validation…")
    report = validate_universe(
        bars_by_symbol,
        eval_days=args.eval_days,
        initial_cash=args.cash,
    )
    print(format_report(report))

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"Wrote JSON → {out}")

    return 0 if report.get("recommendation") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
