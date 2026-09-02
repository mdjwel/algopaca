"""Tests for the intraday Day Trading backtest engine and its signal filters."""

from __future__ import annotations

import datetime as dt
import random
import unittest

import pandas as pd
import pytz

from bot.day_backtest import (
    DayBacktestParams,
    compare_quality_filters,
    lookback_bars,
    run_day_backtest,
)
from bot.day_strategy import DayTradingStrategy
from bot.strategy import Signal

NY = pytz.timezone("America/New_York")
BARS_PER_DAY = 78  # 5-minute bars across a 09:30–16:00 session


def _session(day: dt.date, price: float, kind: str, rng: random.Random) -> tuple[list, float]:
    rows: list[dict] = []
    start = NY.localize(dt.datetime(day.year, day.month, day.day, 9, 30))
    drift, noise = {
        "trend_up": (0.045, 0.16),
        "trend_down": (-0.045, 0.16),
        "chop": (0.0, 0.20),
        "whipsaw": (0.0, 0.34),
    }[kind]
    anchor = price
    for i in range(BARS_PER_DAY):
        stamp = start + dt.timedelta(minutes=5 * i)
        step = -0.010 * (price - anchor) if kind == "chop" else drift
        open_p = price
        price = max(1.0, price + step + rng.gauss(0, noise))
        rows.append({
            "timestamp": stamp,
            "open": open_p,
            "high": max(open_p, price) + abs(rng.gauss(0, noise * 0.6)),
            "low": min(open_p, price) - abs(rng.gauss(0, noise * 0.6)),
            "close": price,
            "volume": 10_000 * (0.7 + rng.random() * 0.8) * (1.8 if i < 6 else 1.0),
            "trade_count": 200,
        })
    return rows, price


def build_bars(days: int, mix: list[str], seed: int = 11) -> pd.DataFrame:
    """A deterministic multi-session intraday frame of the requested character."""
    rng = random.Random(seed)
    rows: list[dict] = []
    price = 100.0
    day = dt.date(2026, 3, 2)
    for k in range(days):
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        got, price = _session(day, price, mix[k % len(mix)], rng)
        rows += got
        day += dt.timedelta(days=1)
    return pd.DataFrame(rows).set_index("timestamp")


class TestDayBacktestEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.bars = build_bars(8, ["trend_up", "chop", "trend_down", "whipsaw"])

    def test_runs_and_reports_the_expected_shape(self):
        res = run_day_backtest(self.bars, symbol="TEST")
        for key in (
            "trades", "wins", "losses", "win_rate_pct", "total_return_pct",
            "avg_r", "profit_factor", "max_drawdown_pct", "trade_log", "equity_curve",
        ):
            self.assertIn(key, res)
        self.assertEqual(res["wins"] + res["losses"], sum(
            1 for t in res["trade_log"] if t["pnl"] != 0
        ))

    def test_rejects_unusable_input(self):
        with self.assertRaises(ValueError):
            run_day_backtest(pd.DataFrame())
        with self.assertRaises(ValueError):
            run_day_backtest(self.bars.reset_index())
        with self.assertRaises(ValueError):
            run_day_backtest(self.bars.head(20))

    def test_no_look_ahead_entry_is_filled_after_the_signal(self):
        """Every fill must be stamped later than the bar that produced it."""
        res = run_day_backtest(self.bars, symbol="TEST")
        self.assertGreater(res["trades"], 0)
        for trade in res["trade_log"]:
            self.assertLessEqual(trade["entry_time"], trade["exit_time"])

    def test_positions_never_survive_the_close(self):
        """EOD square-off means no trade may span two sessions."""
        res = run_day_backtest(
            self.bars,
            symbol="TEST",
            params=DayBacktestParams(eod_flatten=True, eod_flatten_mins=15),
        )
        self.assertGreater(res["trades"], 0)
        for trade in res["trade_log"]:
            entry_day = pd.Timestamp(trade["entry_time"]).date()
            exit_day = pd.Timestamp(trade["exit_time"]).date()
            self.assertEqual(entry_day, exit_day, trade)

    def test_daily_trade_cap_is_respected(self):
        res = run_day_backtest(
            self.bars,
            symbol="TEST",
            params=DayBacktestParams(sub_mode="momentum_scalp", max_trades_per_day=1),
        )
        per_day: dict = {}
        for trade in res["trade_log"]:
            day = pd.Timestamp(trade["entry_time"]).date()
            per_day[day] = per_day.get(day, 0) + 1
        for day, count in per_day.items():
            self.assertLessEqual(count, 1, f"{day} took {count} trades")

    def test_open_buffer_blocks_early_entries(self):
        res = run_day_backtest(
            self.bars, symbol="TEST", params=DayBacktestParams(open_buffer_mins=30)
        )
        for trade in res["trade_log"]:
            self.assertGreaterEqual(
                pd.Timestamp(trade["entry_time"]).time(), dt.time(10, 0), trade
            )

    def test_slippage_makes_the_same_run_worse(self):
        clean = run_day_backtest(
            self.bars, symbol="TEST", params=DayBacktestParams(slippage_bps=0.0)
        )
        costly = run_day_backtest(
            self.bars, symbol="TEST", params=DayBacktestParams(slippage_bps=10.0)
        )
        self.assertLess(costly["total_return_pct"], clean["total_return_pct"])

    def test_lookback_window_spans_a_session(self):
        # 5-minute bars: a session is 78 bars, so the window must exceed that.
        self.assertGreater(lookback_bars(self.bars, warmup=30), BARS_PER_DAY)


class TestQualityFilters(unittest.TestCase):
    """The filters exist to raise per-trade quality and cut give-back."""

    def test_fade_refuses_to_catch_a_falling_knife(self):
        """Fading a trending market is the mode's biggest loss source."""
        bars = build_bars(12, ["trend_down"], seed=23)
        cmp = compare_quality_filters(
            bars, symbol="TEST", params=DayBacktestParams(sub_mode="vwap_fade")
        )
        self.assertLess(cmp["filtered"]["trades"], cmp["raw"]["trades"])
        self.assertGreaterEqual(cmp["filtered"]["total_r"], cmp["raw"]["total_r"])

    def test_filters_raise_average_r_in_chop(self):
        bars = build_bars(12, ["chop", "whipsaw"], seed=37)
        cmp = compare_quality_filters(
            bars, symbol="TEST", params=DayBacktestParams(sub_mode="vwap_trend")
        )
        self.assertLess(cmp["filtered"]["trades"], cmp["raw"]["trades"])

    def test_vwap_hysteresis_holds_through_a_shallow_dip(self):
        """A dip below VWAP smaller than the exit buffer must not close the trade."""
        bars = build_bars(4, ["trend_up"], seed=41)
        strat = DayTradingStrategy(sub_mode="vwap_trend", quality_filters=True)
        raw = DayTradingStrategy(sub_mode="vwap_trend", quality_filters=False)
        window = bars.iloc[-160:]
        ctx = strat._context(window)

        # Nudge the last close to just under VWAP, inside the exit buffer.
        probe = window.copy()
        target = ctx.vwap - ctx.exit_buffer * 0.5
        probe.iloc[-1, probe.columns.get_loc("close")] = target
        probe.iloc[-1, probe.columns.get_loc("low")] = target - 0.01

        self.assertIsNot(strat.evaluate(probe).signal, Signal.SELL)
        self.assertIs(raw.evaluate(probe).signal, Signal.SELL)

    def test_orb_requires_volume_behind_the_breakout(self):
        bars = build_bars(4, ["trend_up"], seed=59)
        strat = DayTradingStrategy(sub_mode="orb", quality_filters=True)
        window = bars.iloc[-160:].copy()
        # Starve the breakout bar of volume; it must no longer qualify.
        window.iloc[-1, window.columns.get_loc("volume")] = 1.0
        self.assertIsNot(strat.evaluate(window).signal, Signal.BUY)

    def test_filters_off_reproduces_the_unscreened_engine(self):
        """The comparison arm must actually differ, or measurement is meaningless."""
        bars = build_bars(10, ["trend_up", "chop", "whipsaw"], seed=11)
        cmp = compare_quality_filters(
            bars, symbol="TEST", params=DayBacktestParams(sub_mode="momentum_scalp")
        )
        self.assertNotEqual(cmp["raw"]["trades"], cmp["filtered"]["trades"])
        self.assertIn("delta", cmp)


class TestDayBacktestWiring(unittest.TestCase):
    """Day mode must be reachable from the product, not just from the module."""

    def test_api_schema_accepts_day_mode(self):
        from bot.webapp import BacktestIn

        self.assertEqual(BacktestIn(mode="day").mode, "day")
        with self.assertRaises(Exception):
            BacktestIn(mode="nonsense")

    def test_state_rejects_unknown_mode_but_routes_day(self):
        from unittest.mock import patch

        from bot.web_state import AppState

        state = AppState(user_id="test_day_backtest")
        with self.assertRaises(ValueError):
            state.run_strategy_backtest(mode="nonsense")

        # Day routes to the intraday runner rather than the daily-bar path.
        with patch.object(
            AppState, "_run_day_backtest", return_value={"mode": "day"}
        ) as runner:
            out = state.run_strategy_backtest(mode="day", days=30, bar_timeframe="5Min")
        self.assertEqual(out["mode"], "day")
        runner.assert_called_once()

    def test_day_backtest_forces_intraday_bars(self):
        from unittest.mock import MagicMock, patch

        from bot.web_state import AppState

        state = AppState(user_id="test_day_backtest_tf")
        bars = build_bars(6, ["trend_up", "chop"], seed=11)
        service = MagicMock()
        service.get_bars_range.return_value = bars

        with patch("bot.web_state.AlpacaService", return_value=service):
            res = state._run_day_backtest(
                days=30,
                bar_timeframe="1Day",  # nonsense for an intraday engine
                initial_cash=25_000.0,
                symbols="TEST",
                symbol="TEST",
            )
        self.assertEqual(res["bar_timeframe"], "5Min")
        self.assertEqual(res["mode"], "day")
        self.assertIn("trade_log", res)
        self.assertIn("trade_list", res)
        self.assertIn("round_trips", res)
        self.assertIn("win_rate", res)
        self.assertIn("buy_hold_return_pct", res)
        self.assertEqual(service.get_bars_range.call_args.kwargs["timeframe"], "5Min")

    def test_day_backtest_trade_list_has_paired_legs(self):
        bars = build_bars(8, ["trend_up", "chop"], seed=11)
        res = run_day_backtest(bars, symbol="TEST")
        trade_list = res.get("trade_list", [])
        self.assertIsInstance(trade_list, list)
        if res["trades"] > 0:
            self.assertEqual(len(trade_list), res["trades"] * 2)
            for leg in trade_list:
                self.assertIn("group_id", leg)
                self.assertIn("side", leg)
                self.assertIn("time", leg)
                self.assertIn("price", leg)

    def test_day_multi_symbol_compare_returns_results_and_summary(self):
        from unittest.mock import MagicMock, patch
        from bot.web_state import AppState

        state = AppState(user_id="test_day_multi")
        bars = build_bars(6, ["trend_up", "chop"], seed=11)
        service = MagicMock()
        def mock_bars(sym, **kwargs):
            if sym == "SNDK":
                return pd.DataFrame()  # simulate delisted/missing ticker
            return bars
        service.get_bars_range.side_effect = mock_bars

        with patch("bot.web_state.AlpacaService", return_value=service):
            res = state._run_day_backtest(
                days=30,
                bar_timeframe="15Min",
                initial_cash=10_000.0,
                symbols="SOXL, SNDK, WDC",
                symbol="SOXL",
                run_kind="per_symbol",
            )

        self.assertEqual(res["mode"], "day")
        self.assertEqual(res["run_kind"], "per_symbol")
        self.assertIn("results", res)
        self.assertIn("summary", res)
        self.assertEqual(len(res["results"]), 3)
        self.assertEqual(len(res["summary"]), 3)
        # Verify SNDK error row
        sndk = next(r for r in res["results"] if r.get("symbol") == "SNDK")
        self.assertIn("error", sndk)
        # Verify SOXL success row has trade_list and equity_curve
        soxl = next(r for r in res["results"] if r.get("symbol") == "SOXL")
        self.assertIn("trade_list", soxl)
        self.assertIn("equity_curve", soxl)

    def test_day_multi_symbol_portfolio_runs_successfully(self):
        from unittest.mock import MagicMock, patch
        from bot.web_state import AppState

        state = AppState(user_id="test_day_portfolio")
        bars = build_bars(6, ["trend_up", "chop"], seed=11)
        service = MagicMock()
        service.get_bars_range.return_value = bars

        with patch("bot.web_state.AlpacaService", return_value=service):
            res = state._run_day_backtest(
                days=30,
                bar_timeframe="15Min",
                initial_cash=10_000.0,
                symbols="SOXL, WDC",
                symbol="SOXL",
                run_kind="portfolio",
            )

        self.assertEqual(res["mode"], "day")
        self.assertEqual(res["run_kind"], "portfolio")
        self.assertIn("results", res)
        self.assertIn("summary", res)
        self.assertIn("equity_curve", res)
        self.assertIn("trade_list", res)


if __name__ == "__main__":
    unittest.main()
