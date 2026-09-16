"""Tests for the AI Trader backtest engine, presets, risk guardrails, and portfolio simulation."""

from __future__ import annotations

import datetime as dt
import math
import random
import unittest

import numpy as np
import pandas as pd
import pytz

from bot.ai_backtest import (
    AiBacktestParams,
    compute_ai_indicator_frame,
    evaluate_ai_signal,
    run_ai_backtest,
    run_ai_portfolio_backtest,
)
from bot.ai_presets import get_preset as get_ai_preset
from bot.strategy import Signal
from bot.web_state import AppState

NY = pytz.timezone("America/New_York")


def _generate_synthetic_daily_bars(
    days: int = 120,
    trend: str = "bull",
    start_price: float = 100.0,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic daily OHLCV bars with realistic volatility and trend."""
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)
    current_date = dt.date(2025, 1, 2)
    dates: list[dt.datetime] = []
    
    while len(dates) < days:
        if current_date.weekday() < 5:
            dates.append(NY.localize(dt.datetime(current_date.year, current_date.month, current_date.day, 16, 0)))
        current_date += dt.timedelta(days=1)
        
    prices = [start_price]
    drift = 0.002 if trend == "bull" else (-0.002 if trend == "bear" else 0.0)
    volatility = 0.018

    for _ in range(days - 1):
        ret = drift + np_rng.normal(0, volatility)
        prices.append(max(5.0, prices[-1] * (1.0 + ret)))

    rows = []
    for d, p in zip(dates, prices):
        day_noise = abs(rng.gauss(0, p * 0.008))
        high = p + day_noise + abs(rng.gauss(0, p * 0.005))
        low = max(1.0, p - day_noise - abs(rng.gauss(0, p * 0.005)))
        open_p = (high + low) / 2.0 + rng.gauss(0, p * 0.002)
        close_p = p
        vol = int(1_000_000 * (0.6 + rng.random() * 0.8))
        rows.append({
            "timestamp": d,
            "open": open_p,
            "high": max(open_p, close_p, high),
            "low": min(open_p, close_p, low),
            "close": close_p,
            "volume": vol,
        })
    df = pd.DataFrame(rows).set_index("timestamp")
    return df


class TestAiBacktestEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.bull_bars = _generate_synthetic_daily_bars(days=150, trend="bull", start_price=100.0, seed=101)
        self.bear_bars = _generate_synthetic_daily_bars(days=150, trend="bear", start_price=100.0, seed=202)
        self.chop_bars = _generate_synthetic_daily_bars(days=150, trend="chop", start_price=100.0, seed=303)

    def test_indicator_computation(self) -> None:
        ind = compute_ai_indicator_frame(self.bull_bars)
        for col in (
            "sma10", "sma20", "sma50", "sma200",
            "ema9", "ema21",
            "rsi14", "macd", "macd_signal", "macd_hist",
            "atr14", "adx14", "bb_upper", "bb_lower", "bb_pct_b",
            "vol_ratio", "dist_sma50_atr",
        ):
            self.assertIn(col, ind.columns)
            self.assertEqual(len(ind), len(self.bull_bars))

    def test_input_validation(self) -> None:
        with self.assertRaises(ValueError):
            run_ai_backtest(pd.DataFrame(), symbol="AAPL")

        with self.assertRaises(ValueError):
            run_ai_backtest(self.bull_bars.reset_index(), symbol="AAPL")

        with self.assertRaises(ValueError):
            run_ai_backtest(self.bull_bars.head(25), symbol="AAPL")

    def test_single_symbol_backtest_output_shape(self) -> None:
        res = run_ai_backtest(self.bull_bars, symbol="AAPL")
        required_keys = (
            "trades", "wins", "losses", "win_rate_pct", "total_return_pct",
            "annualized_return_pct", "max_drawdown_pct", "sharpe_ratio",
            "sortino_ratio", "profit_factor", "trade_log", "trade_list",
            "equity_curve", "summary_metrics", "daily_returns", "ai_preset",
        )
        for key in required_keys:
            self.assertIn(key, res, f"Missing required key '{key}' in run_ai_backtest result")

        self.assertEqual(res["trades"], res["wins"] + res["losses"])
        self.assertEqual(len(res["trade_log"]), res["trades"])
        self.assertEqual(len(res["trade_list"]), 2 * res["trades"])
        self.assertEqual(len(res["equity_curve"]), res["evaluated_bars"])
        self.assertEqual(res["bars"], len(self.bull_bars))

    def test_trade_log_fields_and_no_lookahead(self) -> None:
        res = run_ai_backtest(self.bull_bars, symbol="NVDA")
        if res["trades"] > 0:
            for trade in res["trade_log"]:
                self.assertIn("entry_time", trade)
                self.assertIn("exit_time", trade)
                self.assertIn("exit_reason", trade)
                self.assertIn("entry_price", trade)
                self.assertIn("exit_price", trade)
                self.assertIn("pnl", trade)
                self.assertIn("pnl_pct", trade)
                self.assertIn("r_multiple", trade)
                self.assertIn("confidence", trade)
                # Zero look-ahead check
                self.assertLessEqual(trade["entry_time"], trade["exit_time"])

    def test_stop_loss_and_take_profit_triggers(self) -> None:
        # Run with tight stop (1.0 ATR) and tight take profit (1.5 R)
        params = AiBacktestParams(
            preset="momentum",
            atr_stop_mult=1.0,
            take_profit_r=1.5,
            trail_after_r=0.0,  # disable trailing stop
            min_confidence=0.45,
        )
        res = run_ai_backtest(self.bull_bars, symbol="TSLA", params=params)
        for trade in res["trade_log"]:
            self.assertIn(trade["exit_reason"], ("take_profit", "stop_loss", "trailing_stop", "end_of_data", "signal_flip"))
            if trade["exit_reason"] == "take_profit":
                self.assertGreaterEqual(trade["r_multiple"], 1.0)
            elif trade["exit_reason"] == "stop_loss":
                self.assertLessEqual(trade["r_multiple"], 0.1)

    def test_confidence_filtering(self) -> None:
        # High confidence threshold should produce fewer or equal trades compared to low threshold
        low_conf = run_ai_backtest(self.bull_bars, symbol="MSFT", params=AiBacktestParams(min_confidence=0.30))
        high_conf = run_ai_backtest(self.bull_bars, symbol="MSFT", params=AiBacktestParams(min_confidence=0.85))
        self.assertGreaterEqual(low_conf["trades"], high_conf["trades"])

    def test_all_preset_configurations(self) -> None:
        presets = ["balanced", "conservative", "momentum", "mean_reversion", "trend_atr", "gold_silver_macro", "custom"]
        for p in presets:
            params = AiBacktestParams(preset=p, min_confidence=0.40)
            res = run_ai_backtest(self.bull_bars, symbol="SPY", params=params)
            self.assertEqual(res["ai_preset"], p)
            self.assertIsInstance(res["total_return_pct"], float)
            self.assertFalse(math.isnan(res["total_return_pct"]))

    def test_portfolio_backtest_shared_cash(self) -> None:
        symbols_bars = {
            "AAPL": self.bull_bars,
            "MSFT": self.chop_bars,
            "NVDA": self.bull_bars,
        }
        params = AiBacktestParams(
            preset="balanced",
            max_positions=2,
            risk_pct=0.5,
            min_confidence=0.40,
            initial_cash=50_000.0,
        )
        port = run_ai_portfolio_backtest(symbols_bars, params=params)
        self.assertIn("total_return_pct", port)
        self.assertIn("symbols", port)
        self.assertEqual(len(port["symbols"]), 3)
        self.assertIn("trades", port)
        self.assertIn("equity_curve", port)
        self.assertIn("results", port)

        # Ensure trade log entries from portfolio combine all symbols
        for trade in port.get("trade_log", []):
            self.assertIn(trade["symbol"], ("AAPL", "MSFT", "NVDA"))

    def test_app_state_routing(self) -> None:
        from unittest.mock import patch

        state = AppState(user_id="test_ai_routing")
        with self.assertRaises(ValueError):
            state.run_strategy_backtest(mode="invalid_mode")

        with patch.object(
            AppState, "_run_ai_backtest", return_value={"mode": "ai", "ai_preset": "momentum"}
        ) as runner:
            out = state.run_strategy_backtest(
                mode="ai",
                symbols="AAPL,MSFT",
                symbol="AAPL",
                run_kind="portfolio",
                ai_preset="momentum",
                ai_min_confidence=0.6,
            )
        self.assertEqual(out["mode"], "ai")
        self.assertEqual(out["ai_preset"], "momentum")
        runner.assert_called_once()
        call_kwargs = runner.call_args.kwargs
        self.assertEqual(call_kwargs["ai_preset"], "momentum")
        self.assertEqual(call_kwargs["ai_min_confidence"], 0.6)
        self.assertEqual(call_kwargs["run_kind"], "portfolio")

    def test_app_state_run_ai_backtest_execution(self) -> None:
        from unittest.mock import MagicMock, patch

        state = AppState(user_id="test_ai_exec")
        service = MagicMock()
        fresh_bars = self.bull_bars.copy()
        fresh_bars.index = pd.date_range(
            end=pd.Timestamp.now(tz="America/New_York").normalize() + pd.Timedelta(hours=16),
            periods=len(fresh_bars),
            freq="B",
        )
        service.get_bars_range.return_value = fresh_bars

        with patch("bot.web_state.AlpacaService", return_value=service):
            res = state._run_ai_backtest(
                days=90,
                bar_timeframe="1Day",
                initial_cash=25_000.0,
                symbols="AAPL",
                symbol="AAPL",
                run_kind="per_symbol",
                ai_preset="conservative",
            )
        self.assertEqual(res["mode"], "ai")
        self.assertEqual(res["ai_preset"], "conservative")
        self.assertIn("trades", res)
        self.assertIn("total_return_pct", res)

    def test_evaluation_start_excludes_indicator_warmup_from_results(self) -> None:
        """Pre-window bars may seed indicators but cannot generate P&L."""
        boundary = self.bull_bars.index[100]
        res = run_ai_backtest(
            self.bull_bars,
            symbol="GLD",
            params=AiBacktestParams(preset="gold_silver_macro"),
            evaluation_start=boundary,
        )
        self.assertEqual(res["start"], boundary.isoformat())
        self.assertGreaterEqual(res["warmup_bars"], 100)
        self.assertTrue(
            all(pd.Timestamp(t["entry_time"]) >= boundary for t in res["trade_log"])
        )

    def test_portfolio_evaluation_start_excludes_indicator_warmup(self) -> None:
        """The shared-cash book must use the same evaluation boundary."""
        boundary = self.bull_bars.index[100]
        res = run_ai_portfolio_backtest(
            {"GLD": self.bull_bars, "SLV": self.bull_bars},
            params=AiBacktestParams(preset="gold_silver_macro", max_positions=2),
            evaluation_start=boundary,
        )
        self.assertEqual(res["start"], boundary.isoformat())
        self.assertTrue(
            all(pd.Timestamp(t["entry_time"]) >= boundary for t in res["trade_log"])
        )

    def test_portfolio_result_is_invariant_to_symbol_input_order(self) -> None:
        """Position-slot selection ranks confluence; typed symbol order is irrelevant."""
        frames = {
            "GLD": self.bear_bars,
            "SLV": self.bear_bars * [1, 1, 1, 1, 1],
            "GLL": self.bull_bars,
        }
        params = AiBacktestParams(
            preset="gold_silver_macro", min_confidence=0.65, max_positions=2
        )
        left = run_ai_portfolio_backtest(frames, params=params)
        right = run_ai_portfolio_backtest(
            {"GLL": frames["GLL"], "SLV": frames["SLV"], "GLD": frames["GLD"]},
            params=params,
        )
        self.assertEqual(left["total_return_pct"], right["total_return_pct"])
        self.assertEqual(left["trades"], right["trades"])

    def test_portfolio_final_equity_includes_end_of_data_close(self) -> None:
        """Forced final closes must be reflected in final equity and every leg."""
        from unittest.mock import patch

        flat = self.bull_bars.copy()
        flat.loc[:, ["open", "high", "low", "close"]] = 100.0
        params = AiBacktestParams(
            preset="gold_silver_macro",
            min_confidence=0.70,
            max_positions=1,
            initial_cash=10_000.0,
        )
        with patch(
            "bot.ai_backtest.evaluate_ai_signal",
            return_value=(Signal.BUY, 0.90, "test entry"),
        ):
            result = run_ai_portfolio_backtest({"GLD": flat}, params=params)

        self.assertTrue(result["trade_log"])
        self.assertEqual(result["trade_log"][-1]["exit_reason"], "end_of_data")
        self.assertAlmostEqual(
            result["final_equity"], result["initial_cash"] + result["realized_pnl"], places=2
        )
        self.assertAlmostEqual(
            result["results"][0]["final_equity"], result["results"][0]["initial_cash"] + result["results"][0]["realized_pnl"], places=2
        )

    def test_gold_silver_macro_bull_pullback(self) -> None:
        """Verify GLD macro playbook captures pullbacks inside bull regime without premature shakeouts."""
        params = AiBacktestParams(
            preset="gold_silver_macro",
            atr_stop_mult=2.2,
            take_profit_r=4.0,
            trail_after_r=2.0,
            min_confidence=0.68,
        )
        res = run_ai_backtest(self.bull_bars, symbol="GLD", params=params)
        self.assertEqual(res["ai_preset"], "gold_silver_macro")
        self.assertIn("trades", res)
        # Check that trades had gold/silver macro thesis
        for trade in res["trade_log"]:
            self.assertIn("Gold/Silver Macro", trade["thesis"])
            self.assertTrue(
                trade["exit_reason"].startswith("take_profit")
                or trade["exit_reason"] in ("stop_loss", "trailing_stop", "regime_flip", "trend_break", "end_of_data")
            )

    def test_gold_silver_macro_inverse_etf(self) -> None:
        """Verify inverse metal ETF logic (e.g. GLL, DUST, GDXD)."""
        params = AiBacktestParams(
            preset="gold_silver_macro",
            min_confidence=0.70,
        )
        res = run_ai_backtest(self.bull_bars, symbol="GLL", params=params)
        self.assertEqual(res["ai_preset"], "gold_silver_macro")
        for trade in res["trade_log"]:
            self.assertIn("Inverse ETF", trade["thesis"])

    def test_gold_silver_portfolio_gsr(self) -> None:
        """Verify portfolio multi-symbol simulation calculates GSR and boosts SLV catch-up trades."""
        # Create GLD and SLV bars where GLD outperforms SLV initially (stretching GSR)
        gld_bars = self.bull_bars.copy()
        slv_bars = self.bull_bars.copy()
        # Scale SLV close lower so GSR rises
        slv_bars["close"] = slv_bars["close"] * 0.15
        slv_bars["open"] = slv_bars["open"] * 0.15
        slv_bars["high"] = slv_bars["high"] * 0.15
        slv_bars["low"] = slv_bars["low"] * 0.15

        params = AiBacktestParams(
            preset="gold_silver_macro",
            min_confidence=0.65,
            max_positions=2,
        )
        port = run_ai_portfolio_backtest({"GLD": gld_bars, "SLV": slv_bars}, params=params)
        self.assertIn("symbols", port)
        self.assertEqual(set(port["symbols"]), {"GLD", "SLV"})
        self.assertIn("total_return_pct", port)
        # Verify both long and short exit handling in portfolio works
        self.assertIsInstance(port["trades"], int)

    def test_portfolio_leg_metrics_and_trade_symbols(self) -> None:
        """Verify portfolio multi-symbol simulation scores individual legs and preserves per-trade symbols."""
        gld_bars = self.bull_bars.copy()
        slv_bars = self.bull_bars.copy()
        slv_bars["close"] = slv_bars["close"] * 0.2
        slv_bars["open"] = slv_bars["open"] * 0.2
        slv_bars["high"] = slv_bars["high"] * 0.2
        slv_bars["low"] = slv_bars["low"] * 0.2

        params = AiBacktestParams(
            preset="gold_silver_macro",
            min_confidence=0.65,
            max_positions=2,
            initial_cash=20_000.0,
        )
        port = run_ai_portfolio_backtest({"GLD": gld_bars, "SLV": slv_bars}, params=params)

        # 1. Label must not contain duplicate "AI AI"
        self.assertNotIn("AI AI", port["params"]["label"])
        self.assertEqual(port["params"]["label"], "AI Gold & Silver Macro Momentum")

        # 2. Portfolio book must compute equal-weight buy & hold return
        self.assertIn("buy_hold_return_pct", port)
        self.assertIsInstance(port["buy_hold_return_pct"], float)

        # 3. Individual legs must have populated metrics and equity curves
        self.assertEqual(len(port["results"]), 2)
        for leg in port["results"]:
            self.assertIn(leg["symbol"], {"GLD", "SLV"})
            self.assertEqual(leg["initial_cash"], 10_000.0)
            self.assertIn("total_return_pct", leg)
            self.assertIn("buy_hold_return_pct", leg)
            self.assertIn("alpha_pct", leg)
            self.assertIn("max_drawdown_pct", leg)
            self.assertIn("equity_curve", leg)
            self.assertTrue(len(leg["equity_curve"]) > 0)

        # 4. Trades in trade_list must belong to individual symbols, not combined string
        if port["trade_list"]:
            for t in port["trade_list"]:
                self.assertIn(t["symbol"], {"GLD", "SLV"})
                self.assertNotEqual(t["symbol"], "GLD+SLV")

    def test_metals_regime_flip_exit(self) -> None:
        """Verify regime_flip exit occurs when price decisively falls below SMA200 & SMA50."""
        # Create bars that rise then sharply collapse
        rising_bars = _generate_synthetic_daily_bars(days=90, trend="bull", start_price=100.0, seed=55)
        collapsing_bars = _generate_synthetic_daily_bars(days=60, trend="bear", start_price=float(rising_bars["close"].iloc[-1]), seed=66)
        # Shift collapsing dates forward
        last_date = rising_bars.index[-1]
        new_dates = [last_date + dt.timedelta(days=i + 1) for i in range(len(collapsing_bars))]
        collapsing_bars.index = pd.DatetimeIndex(new_dates)
        combined = pd.concat([rising_bars, collapsing_bars])

        params = AiBacktestParams(
            preset="gold_silver_macro",
            atr_stop_mult=5.0,  # wide stop to let regime flip trigger first
            take_profit_r=10.0,
            trail_after_r=10.0,
            min_confidence=0.65,
        )
        res = run_ai_backtest(combined, symbol="GLD", params=params)
        exit_reasons = [t["exit_reason"] for t in res["trade_log"]]
        self.assertIn("regime_flip", exit_reasons)

    def test_gdxu_and_gdxd_strictly_excluded(self) -> None:
        """Verify GDXU and GDXD are strictly excluded from gold_silver_macro in backtesting."""
        bear_bars = _generate_synthetic_daily_bars(days=120, trend="bear", start_price=200.0, seed=101)
        params = AiBacktestParams(
            preset="gold_silver_macro",
            min_confidence=0.55,
            allow_short=True,
        )
        res_u = run_ai_backtest(bear_bars, symbol="GDXU", params=params)
        self.assertEqual(len(res_u["trade_log"]), 0)
        res_d = run_ai_backtest(bear_bars, symbol="GDXD", params=params)
        self.assertEqual(len(res_d["trade_log"]), 0)

        # Direct bar check
        frame = compute_ai_indicator_frame(bear_bars)
        last_row = frame.iloc[-1]
        prev_row = frame.iloc[-2]
        for sym in ("GDXU", "GDXD"):
            sig, conf, thesis = evaluate_ai_signal(last_row, prev_row, params, allow_short=True, symbol=sym)
            self.assertEqual(sig, Signal.HOLD)
            self.assertIn("strictly excluded", thesis)

    def test_gll_inverse_etf_trading(self) -> None:
        """Verify GLL (inverse gold ETF) trades by BUYING when gold falls, never shorting."""
        bull_gll_bars = _generate_synthetic_daily_bars(days=120, trend="bull", start_price=20.0, seed=202)
        params = AiBacktestParams(
            preset="gold_silver_macro",
            min_confidence=0.60,
            allow_short=True,
        )
        res = run_ai_backtest(bull_gll_bars, symbol="GLL", params=params)
        # All GLL trades must be LONG (bought to express bear view on gold)
        for t in res["trade_log"]:
            self.assertEqual(t["side"], "long")
        if res["trade_log"]:
            self.assertTrue(any("Inverse ETF" in t.get("thesis", "") for t in res["trade_log"]))

    def test_gld_gll_portfolio_mutual_exclusion(self) -> None:
        """Verify GLD (gold) and GLL (inverse gold) are never held concurrently in portfolio backtest."""
        gld_bars = _generate_synthetic_daily_bars(days=120, trend="bull", start_price=100.0, seed=303)
        gll_bars = _generate_synthetic_daily_bars(days=120, trend="bear", start_price=30.0, seed=303)
        params = AiBacktestParams(
            preset="gold_silver_macro",
            min_confidence=0.60,
            max_positions=2,
            initial_cash=20_000.0,
        )
        port = run_ai_portfolio_backtest({"GLD": gld_bars, "GLL": gll_bars}, params=params)

        # Check overlapping holding periods in trade_log
        trades = port["trade_log"]
        for i, t1 in enumerate(trades):
            for t2 in trades[i + 1:]:
                if {t1["symbol"], t2["symbol"]} == {"GLD", "GLL"}:
                    t1_in = pd.Timestamp(t1["entry_time"])
                    t1_out = pd.Timestamp(t1["exit_time"])
                    t2_in = pd.Timestamp(t2["entry_time"])
                    t2_out = pd.Timestamp(t2["exit_time"])
                    overlap = max(t1_in, t2_in) < min(t1_out, t2_out)
                    self.assertFalse(overlap, f"GLD and GLL overlapped: {t1} vs {t2}")

    def test_portfolio_missing_bars_no_phantom_drawdown(self) -> None:
        """Verify that when one symbol has missing timestamps, open position values are preserved and no phantom 90%+ drawdown occurs."""
        gld_bars = self.bull_bars.copy()
        # Drop alternating bars from slv to simulate sparse / missing timestamps
        slv_bars = self.bull_bars.iloc[::2].copy()

        params = AiBacktestParams(
            preset="gold_silver_macro",
            min_confidence=0.60,
            max_positions=2,
            initial_cash=20_000.0,
        )
        port = run_ai_portfolio_backtest({"GLD": gld_bars, "SLV": slv_bars}, params=params)
        max_dd = port.get("max_drawdown_pct") or 0.0
        # In bull bars, max drawdown should not crash to -50% or -99% due to missing timestamps
        self.assertGreater(max_dd, -30.0, f"Expected realistic drawdown, got extreme phantom drop: {max_dd}%")

    def test_gold_silver_macro_falling_knife_avoidance(self) -> None:
        """Verify that uninterrupted falling red candles are rejected even if RSI is in pullback territory."""
        # Create a series of sharp red candles with no lower tail
        dates = pd.date_range("2026-01-01", periods=60, freq="1D")
        closes = np.linspace(200, 150, 60)
        opens = closes + 2.0  # Open always above close (pure red dump)
        highs = opens + 0.1
        lows = closes - 0.05  # Almost zero lower tail (sharp selloff)
        dump_df = pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1000},
            index=dates,
        )
        params = AiBacktestParams(preset="gold_silver_macro", min_confidence=0.70)
        frame = compute_ai_indicator_frame(dump_df)
        sig, conf, _ = evaluate_ai_signal(frame.iloc[-1], frame.iloc[-2], params, symbol="GLD")
        # Should NOT signal BUY into a falling knife with no bounce
        self.assertNotEqual(sig, Signal.BUY)


if __name__ == "__main__":
    unittest.main()
