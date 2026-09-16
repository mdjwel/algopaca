"""Tests for real-time aware Gold and Silver AI strategy and market intelligence."""

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pandas as pd

from bot.ai_presets import get_preset as get_ai_preset, list_presets as list_ai_presets
from bot.custom_engine_store import STARTER_BLUEPRINTS
from bot.day_presets import get_preset as get_day_preset, list_presets as list_day_presets
from bot.dip_presets import get_preset as get_dip_preset
from bot.metals_intel import (
    FACTOR_WEIGHTS,
    calculate_event_stop_limit_prices,
    calculate_gsr,
    check_imminent_economic_events,
    classify_bias,
    clip_unit,
    combine_macro_score,
    compute_historical_macro_series,
    fetch_metals_macro_context,
    get_metal_category,
    is_precious_metal,
    momentum,
    protect_metals_position_before_event,
    score_dollar,
    score_gsr,
    score_rates,
    score_trend,
    zscore,
)
from bot.pair_presets import get_preset as get_pair_preset, list_presets as list_pair_presets
from bot.sma_presets import get_preset as get_sma_preset


class TestMetalsIntelligence(unittest.TestCase):
    def test_is_precious_metal(self):
        self.assertTrue(is_precious_metal("GLD"))
        self.assertTrue(is_precious_metal("gld"))
        self.assertTrue(is_precious_metal("SLV"))
        self.assertTrue(is_precious_metal("IAU"))
        self.assertTrue(is_precious_metal("GDX"))
        self.assertTrue(is_precious_metal("AGQ"))
        self.assertTrue(is_precious_metal("UGL"))
        self.assertTrue(is_precious_metal("GDXD"))
        self.assertTrue(is_precious_metal("gdxd"))
        self.assertTrue(is_precious_metal("GDXU"))
        self.assertTrue(is_precious_metal("gdxu"))

        self.assertFalse(is_precious_metal("AAPL"))
        self.assertFalse(is_precious_metal("SPY"))
        self.assertFalse(is_precious_metal("QQQ"))
        self.assertFalse(is_precious_metal(""))
        self.assertFalse(is_precious_metal(None))

    def test_get_metal_category(self):
        self.assertEqual(get_metal_category("GLD"), "gold")
        self.assertEqual(get_metal_category("IAU"), "gold")
        self.assertEqual(get_metal_category("SLV"), "silver")
        self.assertEqual(get_metal_category("AGQ"), "silver")
        self.assertEqual(get_metal_category("GDX"), "miners")
        self.assertEqual(get_metal_category("GDXD"), "miners")
        self.assertEqual(get_metal_category("GDXU"), "miners")
        self.assertEqual(get_metal_category("MSFT"), "other")

    def test_calculate_gsr(self):
        ratio = calculate_gsr(240.0, 30.0)
        self.assertEqual(ratio, 8.0)
        self.assertIsNone(calculate_gsr(0, 30.0))
        self.assertIsNone(calculate_gsr(240.0, -1.0))

    def test_fetch_metals_macro_context_calculation(self):
        service = MagicMock()
        # Mock mark prices
        service.get_mark_price.side_effect = lambda sym: (
            {"price": 240.0} if sym == "GLD" else ({"price": 28.0} if sym == "SLV" else {})
        )

        # Mock 25 daily bars for historical GSR
        dates = pd.date_range(end=datetime.now(timezone.utc), periods=25, freq="D")
        gld_series = [230.0 + i for i in range(25)]
        slv_series = [30.0 for _ in range(25)]  # historic GSR was ~7.6 - 8.4

        service.get_bars.side_effect = lambda sym, limit=35, timeframe="1Day": (
            pd.DataFrame({"close": gld_series}, index=dates)
            if sym == "GLD"
            else (
                pd.DataFrame({"close": slv_series}, index=dates)
                if sym == "SLV"
                else pd.DataFrame()
            )
        )

        calendar = [
            {
                "title": "FOMC Statement & Federal Funds Rate",
                "impact": "High",
                "when_utc": (datetime.now(timezone.utc) + timedelta(minutes=25)).isoformat(),
            }
        ]

        context = fetch_metals_macro_context(service, "GLD", calendar=calendar)
        self.assertTrue(context["is_precious_metal"])
        self.assertEqual(context["symbol"], "GLD")
        self.assertEqual(context["category"], "gold")
        self.assertEqual(context["price_gld"], 240.0)
        self.assertEqual(context["price_slv"], 28.0)
        self.assertIsNotNone(context["gsr_live"])
        self.assertIsNotNone(context["gsr_sma20"])
        self.assertIsNotNone(context["gsr_z_score"])
        self.assertEqual(context["macro_risk_level"], "imminent_release")
        self.assertTrue(len(context["relevant_macro_events"]) > 0)
        self.assertIn("macro_composite_score", context)
        self.assertIn("metals_macro_bias", context)
        # Score stays inside the documented -3..+3 band and carries its breakdown.
        self.assertGreaterEqual(context["macro_composite_score"], -3.0)
        self.assertLessEqual(context["macro_composite_score"], 3.0)
        self.assertIn("factor_scores", context)
        self.assertIn("trend_regime", context)
        self.assertEqual(context["factor_weights"], dict(FACTOR_WEIGHTS))

    def test_short_history_degrades_without_raising(self):
        """A thin or failing data feed must yield a neutral score, not an error."""
        service = MagicMock()
        service.get_mark_price.return_value = {}
        service.get_bars.side_effect = RuntimeError("data feed down")

        context = fetch_metals_macro_context(service, "SLV", calendar=[])
        self.assertTrue(context["is_precious_metal"])
        self.assertEqual(context["category"], "silver")
        self.assertEqual(context["macro_composite_score"], 0.0)
        self.assertEqual(context["metals_macro_bias"], "neutral")
        self.assertEqual(context["trend_regime"], "unknown")
        self.assertIsNone(context["gsr_live"])

    def test_intraday_execution_uses_daily_macro_windows(self):
        """A 250-day GSR window must not collapse to 250 hourly bars."""
        days = pd.bdate_range("2023-01-02", periods=300)

        def frame(closes, index):
            series = pd.Series(closes, index=index, dtype=float)
            return pd.DataFrame(
                {
                    "open": series,
                    "high": series * 1.002,
                    "low": series * 0.998,
                    "close": series,
                    "volume": 1_000_000,
                },
                index=index,
            )

        gld = frame([170 + i * 0.18 for i in range(len(days))], days)
        slv = frame([20 + (i % 23) * 0.06 for i in range(len(days))], days)
        tip = frame([105 + i * 0.03 for i in range(len(days))], days)
        tlt = frame([100 + i * 0.02 for i in range(len(days))], days)
        uup = frame([28 + (i % 17) * 0.02 for i in range(len(days))], days)
        macro = {"GLD": gld, "SLV": slv, "TIP": tip, "TLT": tlt, "UUP": uup}

        daily = compute_historical_macro_series(gld, symbol="GLD", macro_bars=macro)
        hourly_index = pd.DatetimeIndex(
            [day + pd.Timedelta(hours=hour) for day in days for hour in (14, 15)]
        )
        hourly_gld = frame(
            [float(gld.loc[stamp.normalize(), "close"]) for stamp in hourly_index],
            hourly_index,
        )
        hourly = compute_historical_macro_series(hourly_gld, symbol="GLD", macro_bars=macro)

        last_day = days[-1]
        prior_day = days[-2]
        hourly_last = hourly.loc[hourly.index.normalize() == last_day].iloc[-1]
        hourly_first = hourly.loc[hourly.index.normalize() == last_day].iloc[0]
        self.assertAlmostEqual(
            float(hourly_last["gsr_z"]), float(daily.loc[prior_day, "gsr_z"]), places=2
        )
        self.assertAlmostEqual(
            float(hourly_last["rates_score"]), float(daily.loc[prior_day, "rates_score"]), places=3
        )
        self.assertEqual(hourly_last["trend_regime"], daily.loc[prior_day, "trend_regime"])
        self.assertEqual(hourly_first["trend_regime"], hourly_last["trend_regime"])


class TestMetalsScoring(unittest.TestCase):
    """The composite score is calibrated, so its shape is worth pinning down."""

    def test_clip_unit_bounds_every_factor(self):
        self.assertEqual(clip_unit(5.0), 1.0)
        self.assertEqual(clip_unit(-5.0), -1.0)
        self.assertEqual(clip_unit(0.4), 0.4)

    def test_weights_match_the_calibration(self):
        # Rates and the Gold/Silver ratio carry the measured edge; miners get none.
        self.assertEqual(FACTOR_WEIGHTS["rates"], 0.40)
        self.assertEqual(FACTOR_WEIGHTS["gsr"], 0.35)
        self.assertEqual(FACTOR_WEIGHTS["trend"], 0.15)
        self.assertEqual(FACTOR_WEIGHTS["dollar"], 0.10)
        self.assertNotIn("miners", FACTOR_WEIGHTS)
        self.assertAlmostEqual(sum(FACTOR_WEIGHTS.values()), 1.0)

    def test_combine_macro_score_is_monotonic_and_bounded(self):
        all_bull = combine_macro_score({"rates": 1.0, "gsr": 1.0, "trend": 1.0, "dollar": 1.0})
        all_bear = combine_macro_score({"rates": -1.0, "gsr": -1.0, "trend": -1.0, "dollar": -1.0})
        mixed = combine_macro_score({"rates": 0.5, "gsr": 0.0, "trend": 1.0, "dollar": -0.5})
        self.assertEqual(all_bull, 3.0)
        self.assertEqual(all_bear, -3.0)
        self.assertLess(all_bear, mixed)
        self.assertLess(mixed, all_bull)

    def test_combine_macro_score_renormalizes_missing_factors(self):
        # A failed TLT fetch must not drag a bullish read toward zero.
        partial = combine_macro_score({"rates": None, "gsr": 1.0, "trend": 1.0, "dollar": 1.0})
        self.assertEqual(partial, 3.0)
        self.assertEqual(combine_macro_score({}), 0.0)

    def test_rates_outweighs_the_dollar(self):
        """Rates carry 4x the dollar's weight — the measured, not folkloric, order."""
        rates_only = combine_macro_score({"rates": 1.0, "dollar": -1.0})
        self.assertGreater(rates_only, 0.0)

    def test_classify_bias_bands(self):
        self.assertEqual(classify_bias(2.0), "strong_bullish_tailwind")
        self.assertEqual(classify_bias(1.5), "strong_bullish_tailwind")
        self.assertEqual(classify_bias(0.9), "moderate_bullish")
        self.assertEqual(classify_bias(0.0), "neutral")
        self.assertEqual(classify_bias(-0.9), "moderate_bearish")
        self.assertEqual(classify_bias(-2.0), "strong_bearish_headwind")

    def test_score_rates_sign(self):
        rising_tlt = pd.Series([100.0 + i for i in range(70)])
        falling_tlt = pd.Series([100.0 - i * 0.5 for i in range(70)])
        # Rising TLT = falling yields = bullish gold.
        self.assertGreater(score_rates(rising_tlt), 0.0)
        self.assertLess(score_rates(falling_tlt), 0.0)
        self.assertIsNone(score_rates(None))

    def test_score_gsr_and_dollar_signs(self):
        # A stretched Gold/Silver ratio is a risk-off bid — bullish for metals.
        self.assertGreater(score_gsr(2.0), 0.0)
        self.assertLess(score_gsr(-2.0), 0.0)
        # A strong dollar is a (mild) headwind, so the sign is inverted.
        self.assertLess(score_dollar(2.0), 0.0)
        self.assertGreater(score_dollar(-2.0), 0.0)
        self.assertIsNone(score_gsr(None))
        self.assertIsNone(score_dollar(None))

    def test_score_trend_is_the_sma200_gate(self):
        uptrend = pd.Series([100.0 + i for i in range(220)])
        downtrend = pd.Series([300.0 - i for i in range(220)])
        self.assertEqual(score_trend(uptrend), 1.0)
        self.assertEqual(score_trend(downtrend), -1.0)
        self.assertIsNone(score_trend(None))

    def test_zscore_and_momentum_handle_thin_history(self):
        self.assertIsNone(zscore(None, 20))
        self.assertIsNone(zscore(pd.Series([1.0, 2.0]), 20))
        self.assertIsNone(zscore(pd.Series([5.0] * 30), 20))  # zero variance
        self.assertAlmostEqual(momentum(pd.Series([100.0, 110.0]), 1), 0.1)
        # Window shrinks to the history available rather than returning None.
        self.assertAlmostEqual(momentum(pd.Series([100.0, 110.0]), 60), 0.1)
        self.assertIsNone(momentum(None, 20))


class TestMetalsPresets(unittest.TestCase):
    def test_ai_preset_gold_silver_macro(self):
        preset = get_ai_preset("gold_silver_macro")
        self.assertEqual(preset.id, "gold_silver_macro")
        # Calibrated 1.6 ATR stop and 3.2R target with 2.0R breakeven trail ratchet.
        self.assertEqual(preset.atr_stop_mult, 1.6)
        self.assertEqual(preset.take_profit_r, 3.2)
        self.assertEqual(preset.trail_after_r, 2.0)
        self.assertEqual(preset.min_confidence, 0.70)
        self.assertEqual(preset.risk_pct, 1.8)
        self.assertEqual(preset.max_positions, 2)
        self.assertIn("Gold/Silver ratio", preset.instructions)
        # The playbook must carry the calibrated regime gate, not a breakout gate.
        self.assertIn("bullish_above_sma200", preset.instructions)
        self.assertIn("mean-reverting", preset.instructions)

        all_presets = [p["id"] for p in list_ai_presets()]
        self.assertIn("gold_silver_macro", all_presets)

    def test_custom_engine_starter_blueprint(self):
        bp = next((b for b in STARTER_BLUEPRINTS if b["id"] == "blueprint_ai_gold_silver"), None)
        self.assertIsNotNone(bp)
        self.assertEqual(bp["base_engine"], "ai")
        self.assertEqual(bp["choices"]["ai_preset"], "gold_silver_macro")
        self.assertEqual(bp["choices"]["symbols"], "GLD, SLV, GLL")
        self.assertFalse(bp["choices"]["metals_reversal_buy_on_stop"])
        self.assertEqual(bp["choices"]["ai_take_profit_r"], 3.2)
        self.assertEqual(bp["choices"]["ai_atr_stop_mult"], 1.6)
        self.assertEqual(bp["choices"]["ai_risk_pct"], 1.8)
        self.assertEqual(bp["choices"]["ai_trail_after_r"], 2.0)
        self.assertEqual(bp["choices"]["ai_min_confidence"], 0.70)
        self.assertEqual(bp["choices"]["bar_timeframe"], "1Hour")
        self.assertEqual(bp["choices"]["poll_seconds"], 120)

    def test_day_trading_preset(self):
        preset = get_day_preset("ai_metals_breakout")
        self.assertEqual(preset.id, "ai_metals_breakout")
        self.assertTrue(preset.use_ai_confirm)
        self.assertEqual(preset.side, "long_only")
        self.assertEqual(preset.profit_target_r, 3.8)
        self.assertEqual(preset.stop_atr_mult, 2.0)
        self.assertEqual(preset.open_buffer_mins, 20)
        self.assertEqual(preset.max_trades_per_day, 2)
        self.assertEqual(preset.ema_fast, 13)
        self.assertEqual(preset.ema_slow, 34)
        self.assertEqual(preset.ai_min_confidence, 0.72)

        all_day_presets = [p["id"] for p in list_day_presets()]
        self.assertIn("ai_metals_breakout", all_day_presets)


class TestEconomicEventProtection(unittest.TestCase):
    def test_calculate_event_stop_limit_prices(self):
        # Long position: stop below market, limit slightly below stop
        stop_long, limit_long = calculate_event_stop_limit_prices(
            current_price=250.0, side="long", stop_buffer_pct=0.8, limit_offset_pct=0.5
        )
        self.assertEqual(stop_long, 248.0)
        self.assertEqual(limit_long, 246.76)

        # Short position: stop above market, limit slightly above stop
        stop_short, limit_short = calculate_event_stop_limit_prices(
            current_price=100.0, side="short", stop_buffer_pct=0.8, limit_offset_pct=0.5
        )
        self.assertEqual(stop_short, 100.8)
        self.assertEqual(limit_short, 101.3)

    def test_check_imminent_economic_events(self):
        now = datetime.now(timezone.utc)
        calendar = [
            {
                "title": "CPI m/m",
                "impact": "High",
                "when_utc": (now + timedelta(minutes=4)).isoformat(),
            },
            {
                "title": "FOMC Statement",
                "impact": "High",
                "when_utc": (now + timedelta(minutes=25)).isoformat(),
            },
            {
                "title": "Minor Report",
                "impact": "Low",
                "when_utc": (now + timedelta(minutes=2)).isoformat(),
            },
        ]
        imminent = check_imminent_economic_events(calendar, window_minutes=5.0)
        self.assertEqual(len(imminent), 1)
        self.assertEqual(imminent[0]["title"], "CPI m/m")
        self.assertAlmostEqual(imminent[0]["minutes_away"], 4.0, delta=0.5)

    def test_protect_metals_position_before_event_long(self):
        service = MagicMock()
        service.get_position_qty.return_value = 10.0
        service.get_mark_price.return_value = {"price": 250.0}
        service.current_stop_price.return_value = 240.0
        service.replace_stop_loss.return_value = {"id": "ord_123", "stop_price": 248.0}

        synthetic_handler = MagicMock()
        synthetic_handler.sync_strategy_stop.return_value = {"id": "synth_456"}

        event = {"title": "CPI m/m", "minutes_away": 4.5}
        result = protect_metals_position_before_event(
            service=service,
            symbol="GLD",
            event=event,
            stop_buffer_pct=0.8,
            limit_offset_pct=0.5,
            synthetic_handler=synthetic_handler,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["symbol"], "GLD")
        self.assertEqual(result["side"], "long")
        self.assertEqual(result["stop_price"], 248.0)
        self.assertEqual(result["limit_price"], 246.76)
        self.assertEqual(result["action_taken"], "updated")
        self.assertEqual(result["alpaca_order_id"], "ord_123")
        service.replace_stop_loss.assert_called_once_with("GLD", 248.0)
        synthetic_handler.sync_strategy_stop.assert_called_once()

    def test_protect_metals_position_before_event_short_reversal_buy(self):
        service = MagicMock()
        service.get_position_qty.return_value = -15.0  # Short position
        service.get_mark_price.return_value = {"price": 250.0}
        service.current_stop_price.return_value = 260.0  # Looser stop above
        service.replace_stop_loss.return_value = {"id": "ord_short_123", "stop_price": 252.0}

        synthetic_handler = MagicMock()
        synthetic_handler.sync_strategy_stop.return_value = {"id": "synth_rev_456"}

        event = {"title": "CPI m/m", "minutes_away": 4.5}
        result = protect_metals_position_before_event(
            service=service,
            symbol="GLD",
            event=event,
            stop_buffer_pct=0.8,
            limit_offset_pct=0.5,
            synthetic_handler=synthetic_handler,
            reversal_buy=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["symbol"], "GLD")
        self.assertEqual(result["side"], "short")
        self.assertEqual(result["qty"], 15.0)
        self.assertEqual(result["stop_price"], 252.0)
        self.assertEqual(result["limit_price"], 253.26)
        self.assertEqual(result["action_taken"], "updated")
        self.assertTrue(result["reversal_buy"])
        self.assertEqual(result["reversal_qty"], 15.0)
        self.assertEqual(result["reversal_event_title"], "CPI m/m")

        synthetic_handler.sync_strategy_stop.assert_called_once_with(
            symbol="GLD",
            side="buy",
            qty=15.0,
            stop_price=252.0,
            limit_price=253.26,
            source="event_5m_CPI m/m",
            reversal_buy=True,
            reversal_qty=15.0,
            reversal_event_title="CPI m/m",
        )

    def test_protect_metals_position_maintains_tighter_stop(self):
        service = MagicMock()
        service.get_position_qty.return_value = 10.0
        service.get_mark_price.return_value = {"price": 250.0}
        # Current stop is at 249.0 (already higher than 248.0 target stop)
        service.current_stop_price.return_value = 249.0

        synthetic_handler = MagicMock()
        event = {"title": "CPI m/m", "minutes_away": 3.0}
        result = protect_metals_position_before_event(
            service=service,
            symbol="GLD",
            event=event,
            stop_buffer_pct=0.8,
            synthetic_handler=synthetic_handler,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["action_taken"], "maintained_existing_tighter")
        # Native replace_stop_loss should NOT be called to avoid loosening stop
        service.replace_stop_loss.assert_not_called()

    def test_protect_metals_ignores_non_metals(self):
        service = MagicMock()
        event = {"title": "CPI m/m", "minutes_away": 3.0}
        result = protect_metals_position_before_event(
            service=service, symbol="AAPL", event=event
        )
        self.assertIsNone(result)

    @patch("bot.metals_intel.fetch_economic_calendar")
    @patch("bot.web_state.AlpacaService")
    def test_web_state_check_metals_event_protection(self, mock_service_cls, mock_cal):
        import tempfile
        from pathlib import Path
        from bot.web_state import AppState
        from bot.auth import AuthStore

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            auth_store = AuthStore(db_path=tmp_path / "auth.db")
            user = auth_store.register_user("trader2", "trader2@example.com", "Password123!")

            with patch("bot.web_state.AUTH_STORE", auth_store):
                state = AppState(workspace_dir=tmp_path, user_id=user["id"])

                svc = mock_service_cls.return_value
                svc.list_positions.return_value = [{"symbol": "GLD", "qty": 10.0}]
                svc.get_position_qty.return_value = 10.0
                svc.get_mark_price.return_value = {"price": 250.0}
                svc.current_stop_price.return_value = 240.0
                svc.replace_stop_loss.return_value = {"id": "ord_event", "stop_price": 248.0}

                now = datetime.now(timezone.utc)
                mock_cal.return_value = [
                    {
                        "title": "FOMC Statement",
                        "impact": "High",
                        "when_utc": (now + timedelta(minutes=4)).isoformat(),
                    }
                ]

                # Run check
                armed = state.check_metals_event_protection()
                self.assertEqual(len(armed), 1)
                self.assertEqual(armed[0]["symbol"], "GLD")
                self.assertEqual(armed[0]["stop_price"], 248.0)

                # Check deduplication: running a second time should not duplicate
                armed_2 = state.check_metals_event_protection()
                self.assertEqual(len(armed_2), 0)

    def test_pair_presets(self):
        # All three were retuned toward slower filters and far fewer switches —
        # the shipped 50-day / 4% settings churned and, for GLD/SLV, lost money
        # across 2005-2025.
        preset_rot = get_pair_preset("gold_silver_rotation")
        self.assertEqual(preset_rot.id, "gold_silver_rotation")
        self.assertEqual(preset_rot.long_symbol, "GLD")
        self.assertEqual(preset_rot.short_symbol, "SLV")
        self.assertEqual(preset_rot.sma_period, 100)
        self.assertEqual(preset_rot.lookback, 15)
        self.assertEqual(preset_rot.impulse_pct, 10.0)
        # Parking in gold beat parking in cash by a wide margin on both halves.
        self.assertEqual(preset_rot.weak_side, "LONG")

        preset_inv = get_pair_preset("gold_inverse_hedge")
        self.assertEqual(preset_inv.id, "gold_inverse_hedge")
        self.assertEqual(preset_inv.long_symbol, "GLD")
        self.assertEqual(preset_inv.short_symbol, "GLL")
        self.assertEqual(preset_inv.sma_period, 150)
        self.assertEqual(preset_inv.weak_side, "CASH")

        preset_min = get_pair_preset("gold_miners_rotator")
        self.assertEqual(preset_min.id, "gold_miners_rotator")
        self.assertEqual(preset_min.long_symbol, "GLD")
        self.assertEqual(preset_min.short_symbol, "GDX")
        self.assertEqual(preset_min.sma_period, 200)
        self.assertEqual(preset_min.lookback, 10)
        self.assertEqual(preset_min.impulse_pct, 5.0)

        all_pair_presets = [p["id"] for p in list_pair_presets()]
        self.assertIn("gold_silver_rotation", all_pair_presets)
        self.assertIn("gold_inverse_hedge", all_pair_presets)
        self.assertIn("gold_miners_rotator", all_pair_presets)

    def test_sma_preset_gold_trend(self):
        # 50/150 beat the old 16/64 in both halves of a 2005-2025 GLD grid
        # (Sharpe 0.72 vs 0.51) with a third of the round trips.
        preset = get_sma_preset("gold_trend")
        self.assertEqual(preset.id, "gold_trend")
        self.assertEqual(preset.fast_sma, 50)
        self.assertEqual(preset.slow_sma, 150)

    def test_dip_preset_gold_dip(self):
        # Selling at RSI 62 cut winners short and left the preset in cash ~70%
        # of the time; holding to a genuine overbought reading at 80 tripled
        # per-share P&L across 2005-2025.
        preset = get_dip_preset("gold_dip")
        self.assertEqual(preset.id, "gold_dip")
        self.assertEqual(preset.rsi_buy, 45.0)
        self.assertEqual(preset.rsi_sell, 80.0)
        self.assertTrue(preset.skip_bearish)


class TestAiBrainMetalsIntegration(unittest.TestCase):
    @patch("bot.ai_brain.fetch_metals_macro_context")
    @patch("bot.ai_brain.compute_technicals")
    @patch("bot.ai_brain.fetch_news")
    @patch("bot.ai_brain.fetch_earnings")
    def test_ai_brain_attaches_metals_intel(self, mock_earn, mock_news, mock_tech, mock_metals):
        from bot.ai_brain import AiBrain
        from bot.config import Config

        mock_tech.return_value = {"ok": True, "atr_14": 2.5}
        mock_news.return_value = []
        mock_earn.return_value = {}
        mock_metals.return_value = {"is_precious_metal": True, "gsr_live": 8.5}

        config = Config.default(
            strategy_mode="ai",
            ai_preset="gold_silver_macro",
            ai_min_confidence=0.60,
        )
        service = MagicMock()
        service.get_bars.return_value = pd.DataFrame()
        service.get_position_detail.return_value = {"qty": 0}
        service.account_summary.return_value = {"equity": 10000}
        service.recent_activity.return_value = {}
        service.get_mark_price.return_value = {"price": 240.0}
        service.market_session.return_value = {"session": "open"}

        provider = MagicMock()
        brain = AiBrain(config, service, provider)

        # 1. Precious metal symbol GLD -> attaches precious_metals_intel
        ctx_gld = brain.build_context("GLD")
        self.assertIn("precious_metals_intel", ctx_gld)
        self.assertTrue(ctx_gld["precious_metals_intel"]["is_precious_metal"])

        # Check prompt formatting contains PRECIOUS METALS guidance
        prompt_gld = brain._format_prompt("GLD", ctx_gld)
        self.assertIn("PRECIOUS METALS & CROSS-ASSET MACRO INTELLIGENCE", prompt_gld)

        # 2. Non-metal symbol AAPL -> does NOT attach precious_metals_intel
        ctx_aapl = brain.build_context("AAPL")
        self.assertNotIn("precious_metals_intel", ctx_aapl)
        prompt_aapl = brain._format_prompt("AAPL", ctx_aapl)
        self.assertNotIn("PRECIOUS METALS & CROSS-ASSET MACRO INTELLIGENCE", prompt_aapl)


class TestMetalsTranslations(unittest.TestCase):
    def test_all_languages_have_metals_keys(self):
        import json
        from pathlib import Path

        lang_dir = Path(__file__).resolve().parent.parent / "web" / "static" / "lang"
        required_keys = [
            "preset_gold_silver_macro",
            "preset_summary_gold_silver_macro",
            "preset_day_ai_metals_breakout",
            "day_preset_summary_ai_metals_breakout",
            "preset_pair_gold_silver_rotation",
            "pair_preset_summary_gold_silver_rotation",
            "preset_pair_gold_inverse_hedge",
            "pair_preset_summary_gold_inverse_hedge",
            "preset_pair_gold_miners_rotator",
            "pair_preset_summary_gold_miners_rotator",
            "preset_sma_gold_trend",
            "sma_preset_summary_gold_trend",
            "preset_dip_gold_dip",
            "dip_preset_summary_gold_dip",
        ]

        for path in sorted(lang_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in required_keys:
                self.assertIn(key, data, f"{path.name} is missing translation key: {key}")


class TestMixedDollarIndexReversalRule(unittest.TestCase):
    def test_dollar_mixed_flag_in_context(self):
        service = MagicMock()
        service.get_mark_price.return_value = {"price": 240.0}
        dates = pd.date_range(end=datetime.now(timezone.utc), periods=80, freq="D")
        gld_series = [240.0 for _ in range(80)]
        # Alternating series ending with mean value 28.0 so std > 0 and z-score == 0.0
        uup_series = ([27.9, 28.1] * 39) + [28.0, 28.0]

        service.get_bars.side_effect = lambda sym, **kw: pd.DataFrame(
            {"close": uup_series if sym == "UUP" else gld_series}, index=dates
        )

        ctx = fetch_metals_macro_context(service, "GLD", calendar=[])
        # With neutral UUP series, dollar trend is neutral -> dollar_mixed is True
        self.assertTrue(ctx["dollar_mixed"])
        self.assertEqual(ctx["dollar_economic_data_status"], "mixed")
        self.assertFalse(ctx["short_reversal_to_long"])
        self.assertTrue(ctx["close_short_to_cash"])
        self.assertIsNotNone(ctx["short_reversal_reason"])
        self.assertIn("close short to cash", ctx["short_reversal_reason"])

    def test_reversal_gate_bypasses_min_hold_on_dollar_mixed(self):
        from bot.ai_risk import reversal_gate
        config = MagicMock()
        config.ai_min_hold_minutes = 45
        config.ai_reversal_conf_bump = 0.20
        config.ai_min_confidence = 0.60

        # Position is short (-5 shares) and was opened just 2 minutes ago
        context = {
            "position": {"qty": -5.0},
            "activity": {"last_fill_age_min": 2.0},
            "precious_metals_intel": {"dollar_mixed": True},
        }
        gate = reversal_gate(config, context, confidence=0.50)
        # Should allow reversal to protect from short squeeze on mixed dollar data
        self.assertTrue(gate.allowed)

    def test_gold_silver_macro_instructions_contain_mixed_dollar_reversal(self):
        preset = get_ai_preset("gold_silver_macro")
        self.assertIn("REVERSAL RULE", preset.instructions)
        self.assertIn("Dollar Index is mixed", preset.instructions)

    def test_ai_brain_prompt_contains_mandatory_dollar_mixed_reversal(self):
        from bot.ai_brain import AiBrain

        config = MagicMock()
        config.strategy_mode = "ai"
        config.ai_preset = "gold_silver_macro"
        config.stop_loss_pct = 2.0
        config.symbol = "GLD"
        config.symbols = "GLD, SLV"
        config.lang = "en"
        config.size_mode = "qty"
        config.trade_qty = 2.0
        config.trade_notional = 200.0
        config.ai_risk_pct = 0.5
        config.account_buying_power = 10000.0
        config.ai_qty_for_risk = None
        config.order_qty_for_price = None
        config.ai_stop_distance = None

        service = MagicMock()
        service.get_position_qty.return_value = -2.0
        service.get_mark_price.return_value = {"price": 240.0}
        service.market_session.return_value = {"session": "open"}
        service.recent_activity.return_value = {}

        dates = pd.date_range(end=datetime.now(timezone.utc), periods=30, freq="D")
        service.get_bars.return_value = pd.DataFrame({"close": [240.0] * 30}, index=dates)

        brain = AiBrain(config, service, MagicMock())
        ctx = brain.build_context("GLD")
        prompt = brain._format_prompt("GLD", ctx)
        self.assertIn("MANDATORY DOLLAR INDEX & ECONOMIC DATA MIXED REVERSAL RULE", prompt)

    def test_ai_trader_reversal_to_long_on_dollar_mixed(self):
        from bot.ai_trader import AiTradingBot
        from bot.ai_providers import AiDecision

        config = MagicMock()
        config.require_approval = False
        config.symbol = "GLD"
        config.symbols = "GLD"
        config.stop_loss_pct = 2.0
        config.ai_daily_loss_limit_pct = 0
        config.ai_max_positions = 0
        config.ai_max_spread_bps = 0
        config.ai_cooldown_minutes = 0

        service = MagicMock()
        service.get_position_qty.return_value = -3.0
        service.has_open_orders.return_value = False
        service.market_session.return_value = {"session": "open", "is_open": True}
        service.get_mark_price.return_value = {"price": 240.0}
        service.ensure_stop_loss.return_value = None

        order_cover = MagicMock(id="order_cover_123")
        order_long = MagicMock(id="order_long_456")
        service.submit_order.side_effect = [order_cover, order_long]

        brain = MagicMock()
        context = {
            "position": {"qty": -3.0},
            "position_qty": -3.0,
            "mark": {"price": 240.0},
            "risk": {"stop_distance": 2.5},
            "precious_metals_intel": {"dollar_mixed": True},
        }
        brain.build_context.return_value = context
        brain.hold_decision.side_effect = lambda d, r: d
        decision = AiDecision(
            action="buy",
            confidence=0.85,
            qty=3.0,
            thesis="Dollar index economic data is mixed, closing short and entering long.",
            risks="Yield spikes.",
            thesis_en="Dollar index economic data is mixed, closing short and entering long.",
            risks_en="Yield spikes.",
            news_bias="neutral",
            ta_bias="bullish",
            raw={"action": "buy", "reverse_to_long": True},
            provider="openai",
            model="gpt-5.6-luna",
        )
        brain.decide.return_value = (decision, context)

        import threading
        bot = AiTradingBot.__new__(AiTradingBot)
        bot.config = config
        bot.service = service
        bot.approval_handler = None
        bot.brain = brain
        bot._execution_lock = threading.Lock()
        bot._open_positions = 1
        payload = bot._run_symbol("GLD")

        self.assertTrue(payload.get("reversal_to_long"))
        self.assertEqual(payload.get("long_order_id"), "order_long_456")
        self.assertEqual(payload.get("intent"), "reverse_to_long")
        # Assert two orders were submitted: 1st to COVER, 2nd to open LONG
        self.assertEqual(service.submit_order.call_count, 2)

    def test_ai_trader_cover_to_cash_on_dollar_mixed_without_forced_long(self):
        from bot.ai_trader import AiTradingBot
        from bot.ai_providers import AiDecision

        config = MagicMock()
        config.require_approval = False
        config.symbol = "GLD"
        config.symbols = "GLD"
        config.stop_loss_pct = 2.0
        config.ai_daily_loss_limit_pct = 0
        config.ai_max_positions = 0
        config.ai_max_spread_bps = 0
        config.ai_cooldown_minutes = 0

        service = MagicMock()
        service.get_position_qty.return_value = -3.0
        service.has_open_orders.return_value = False
        service.market_session.return_value = {"session": "open", "is_open": True}
        service.get_mark_price.return_value = {"price": 240.0}
        service.ensure_stop_loss.return_value = None

        order_cover = MagicMock(id="order_cover_789")
        service.submit_order.return_value = order_cover

        brain = MagicMock()
        context = {
            "position": {"qty": -3.0},
            "position_qty": -3.0,
            "mark": {"price": 240.0},
            "risk": {"stop_distance": 2.5},
            "precious_metals_intel": {"dollar_mixed": True},
        }
        brain.build_context.return_value = context
        brain.hold_decision.side_effect = lambda d, r: d
        # Decision has NO explicit reverse_to_long, should cover to cash flat
        decision = AiDecision(
            action="buy",
            confidence=0.80,
            qty=3.0,
            thesis="Dollar index economic data is mixed, closing short to cash.",
            risks="None.",
            thesis_en="Dollar index economic data is mixed, closing short to cash.",
            risks_en="None.",
            news_bias="neutral",
            ta_bias="neutral",
            raw={"action": "buy"},
            provider="openai",
            model="gpt-5.6-luna",
        )
        brain.decide.return_value = (decision, context)

        import threading
        bot = AiTradingBot.__new__(AiTradingBot)
        bot.config = config
        bot.service = service
        bot.approval_handler = None
        bot.brain = brain
        bot._execution_lock = threading.Lock()
        bot._open_positions = 1
        payload = bot._run_symbol("GLD")

        # Must NOT reverse to long
        self.assertFalse(payload.get("reversal_to_long", False))
        self.assertEqual(payload.get("intent"), "cover")
        # Exactly ONE order submitted (the cover order)
        self.assertEqual(service.submit_order.call_count, 1)
        self.assertEqual(payload.get("order_id"), "order_cover_789")


from bot.metals_intel import compute_historical_macro_series
from bot.ai_backtest import (
    AiBacktestParams,
    Signal,
    compute_ai_indicator_frame,
    evaluate_ai_signal,
    run_ai_backtest,
    run_ai_portfolio_backtest,
)
from tests.test_ai_backtest import _generate_synthetic_daily_bars


class TestMetalsBacktestMacroRules(unittest.TestCase):
    """Verify the 7 macro trading and risk rules in backtesting."""

    def setUp(self):
        self.gld_bars = _generate_synthetic_daily_bars(days=160, trend="bull", start_price=180.0, seed=10)
        self.tlt_bars = _generate_synthetic_daily_bars(days=160, trend="bull", start_price=95.0, seed=11)
        self.uup_bars = _generate_synthetic_daily_bars(days=160, trend="bear", start_price=28.0, seed=12)
        self.slv_bars = _generate_synthetic_daily_bars(days=160, trend="bull", start_price=24.0, seed=13)

    def test_rule1_macro_composite_score_computation(self):
        """Rule 1: Verify Macro Composite Score (-3 to +3) with factor weighting."""
        macro_bars = {
            "TLT": self.tlt_bars,
            "UUP": self.uup_bars,
            "SLV": self.slv_bars,
        }
        res = compute_historical_macro_series(self.gld_bars, symbol="GLD", macro_bars=macro_bars)
        self.assertIn("macro_composite_score", res.columns)
        self.assertIn("rates_score", res.columns)
        self.assertIn("gsr_score", res.columns)
        self.assertIn("trend_score", res.columns)
        self.assertIn("dollar_score", res.columns)
        self.assertIn("yield_trend", res.columns)
        self.assertIn("dollar_trend", res.columns)
        self.assertIn("dollar_mixed", res.columns)

        valid_scores = res["macro_composite_score"].dropna()
        self.assertTrue((valid_scores >= -3.0).all())
        self.assertTrue((valid_scores <= 3.0).all())

    def test_rule2_long_entry_macro_and_pullback(self):
        """Rule 2: Long entry requires bullish regime, macro score >= +0.5, pullback, and confidence >= 0.70."""
        params = AiBacktestParams(preset="gold_silver_macro", min_confidence=0.70)
        frame = compute_ai_indicator_frame(self.gld_bars, symbol="GLD")
        row = frame.iloc[-1].copy()
        prev = frame.iloc[-2].copy()

        # Set up a bullish regime above SMA 200
        row["close"] = 200.0
        row["sma200"] = 180.0
        row["sma50"] = 190.0
        row["sma20"] = 199.0
        row["open"] = 198.0
        row["high"] = 202.0
        row["low"] = 197.0
        row["rsi14"] = 48.0  # In pullback zone [38, 58]
        row["dist_sma50_atr"] = 1.0
        row["trend_regime"] = "bullish_above_sma200"

        # Condition 1: Macro Score < 0.0 -> Must reject (HOLD)
        row["macro_composite_score"] = -0.2
        sig, conf, thesis = evaluate_ai_signal(row, prev, params, symbol="GLD", macro_score=-0.2)
        self.assertEqual(sig, Signal.HOLD)

        # Condition 2: Macro Score >= +0.5 -> Approved (BUY, conf >= 0.70)
        row["macro_composite_score"] = 1.2
        sig, conf, thesis = evaluate_ai_signal(row, prev, params, symbol="GLD", macro_score=1.2)
        self.assertEqual(sig, Signal.BUY)
        self.assertGreaterEqual(conf, 0.70)

        # Condition 3: Breakout Chase rejection (RSI > 58 AND price > SMA20 * 1.01)
        row["rsi14"] = 68.0
        row["close"] = 215.0  # Much higher than SMA20 (199 * 1.01 = 200.99)
        sig, conf, thesis = evaluate_ai_signal(row, prev, params, symbol="GLD", macro_score=1.2)
        self.assertEqual(sig, Signal.HOLD)

        # Condition 4: Asset Selection: GDXU is strictly excluded, GLD buys on strong macro
        row["rsi14"] = 48.0
        row["close"] = 200.0
        row["adx14"] = 25.0
        sig, conf, thesis = evaluate_ai_signal(row, prev, params, symbol="GDXU", macro_score=1.8)
        self.assertEqual(sig, Signal.HOLD)
        self.assertIn("strictly excluded", thesis)
        sig_gld, conf_gld, _ = evaluate_ai_signal(row, prev, params, symbol="GLD", macro_score=1.8)
        self.assertEqual(sig_gld, Signal.BUY)

    def test_rule3_short_entry_and_dollar_mixed_ban(self):
        """Rule 3: Short requires price < 200 SMA, macro <= -0.5, rising yields, and strictly bans if dollar is mixed."""
        params = AiBacktestParams(preset="gold_silver_macro", min_confidence=0.70, allow_short=True)
        frame = compute_ai_indicator_frame(self.gld_bars, symbol="GLD")
        row = frame.iloc[-1].copy()
        prev = frame.iloc[-2].copy()

        # Bear regime below SMA 200
        row["close"] = 160.0
        row["sma200"] = 180.0
        row["sma50"] = 175.0
        row["sma20"] = 168.0
        row["rsi14"] = 35.0
        row["macd_hist"] = -0.5
        row["adx14"] = 22.0
        row["dist_sma50_atr"] = -1.5
        row["macro_composite_score"] = -1.0
        row["trend_regime"] = "bearish_below_sma200"
        row["yield_trend"] = "rising_yields"
        row["dollar_trend"] = "bullish"
        row["dollar_mixed"] = False

        # Should be approved for short when dollar is trending/bullish
        sig, conf, thesis = evaluate_ai_signal(row, prev, params, symbol="GLD", macro_score=-1.0, allow_short=True)
        self.assertEqual(sig, Signal.SELL)
        self.assertGreaterEqual(conf, 0.70)

        # Strict ban: If dollar is Mixed or Neutral -> MUST return Signal.HOLD
        row["dollar_mixed"] = True
        sig, conf, thesis = evaluate_ai_signal(row, prev, params, symbol="GLD", macro_score=-1.0, allow_short=True)
        self.assertEqual(sig, Signal.HOLD)
        self.assertIn("mixed or neutral", thesis.lower())

        row["dollar_mixed"] = False
        row["dollar_trend"] = "neutral"
        sig, conf, thesis = evaluate_ai_signal(row, prev, params, symbol="GLD", macro_score=-1.0, allow_short=True)
        self.assertEqual(sig, Signal.HOLD)
        self.assertIn("mixed or neutral", thesis.lower())

    def test_rule4_and_rule6_short_reversals_in_backtest(self):
        """Rule 4 & 6: Verify dollar mixed reversal and short stop reversal buy mechanics."""
        # Create bars that trigger short then reversal
        bear_bars = _generate_synthetic_daily_bars(days=120, trend="bear", start_price=200.0, seed=42)
        macro_bars = {
            "TLT": _generate_synthetic_daily_bars(days=120, trend="bear", start_price=90.0, seed=43),
            "UUP": _generate_synthetic_daily_bars(days=120, trend="flat", start_price=28.0, seed=44),
        }
        params = AiBacktestParams(
            preset="gold_silver_macro",
            allow_short=True,
            min_confidence=0.60,
            reversal_buy_on_stop=True,
        )
        res = run_ai_backtest(bear_bars, symbol="GLD", params=params, macro_bars=macro_bars)
        self.assertIsInstance(res["trades"], int)
        self.assertIn("trade_log", res)

    def test_rule5_event_protection_and_45m_filter(self):
        """Rule 5 & Event filter: 45m entry freeze and 5m stop loss tightening."""
        params = AiBacktestParams(preset="gold_silver_macro", min_confidence=0.70)
        frame = compute_ai_indicator_frame(self.gld_bars, symbol="GLD")
        row = frame.iloc[-1].copy()
        prev = frame.iloc[-2].copy()
        row["macro_composite_score"] = 1.5
        row["close"] = 200.0
        row["sma200"] = 180.0
        row["rsi14"] = 45.0
        row["trend_regime"] = "bullish_above_sma200"

        # Within 45m of event -> evaluate_ai_signal must return HOLD
        sig, conf, thesis = evaluate_ai_signal(
            row, prev, params, symbol="GLD", macro_score=1.5, event_imminent_45m=True
        )
        self.assertEqual(sig, Signal.HOLD)
        self.assertIn("within 45 minutes", thesis.lower())

    def test_rule7_preset_defaults_and_scale_out_configuration(self):
        """Rule 7: Take-profit 3.2R, trailing stop ratchet after 2.0R."""
        preset = get_ai_preset("gold_silver_macro")
        self.assertEqual(preset.take_profit_r, 3.2)
        self.assertEqual(preset.trail_after_r, 2.0)
        self.assertEqual(preset.atr_stop_mult, 1.6)
        self.assertEqual(preset.risk_pct, 1.8)
        # Verify auto-defaults via __post_init__
        auto_params = AiBacktestParams(preset="gold_silver_macro")
        self.assertEqual(auto_params.take_profit_r, 3.2)
        self.assertEqual(auto_params.trail_after_r, 2.0)
        self.assertEqual(auto_params.min_confidence, 0.70)
        self.assertEqual(auto_params.risk_pct, 1.8)
        self.assertEqual(auto_params.atr_stop_mult, 1.6)

    def test_rule7_portfolio_backtest_with_macro_rules(self):
        """Rule 7: Verify portfolio backtest accepts macro_bars and executes multi-symbol simulation."""
        bars_by_sym = {
            "GLD": self.gld_bars,
            "SLV": self.slv_bars,
        }
        macro_bars = {
            "TLT": self.tlt_bars,
            "UUP": self.uup_bars,
            "GLD": self.gld_bars,
            "SLV": self.slv_bars,
        }
        params = AiBacktestParams(preset="gold_silver_macro", min_confidence=0.60)
        res = run_ai_portfolio_backtest(bars_by_sym, params=params, macro_bars=macro_bars)
        self.assertEqual(res["run_kind"], "portfolio")
        self.assertIn("symbols", res)
        self.assertIn("results", res)
        self.assertEqual(len(res["results"]), 2)

    def test_metals_tip_real_yields_blended_in_rates_scoring(self):
        """Verify TIP real yields (65%) blend with TLT nominal yields (35%) in score_rates."""
        import numpy as np
        from bot.metals_intel import score_rates

        dates = pd.date_range("2025-01-01", periods=100, freq="D")
        tip_up = pd.Series(np.linspace(100.0, 115.0, 100), index=dates)
        tlt_up = pd.Series(np.linspace(90.0, 95.0, 100), index=dates)
        tlt_down = pd.Series(np.linspace(95.0, 88.0, 100), index=dates)

        # Both up: very strong rates score
        score_both = score_rates(tlt_closes=tlt_up, tip_closes=tip_up)
        self.assertIsNotNone(score_both)
        self.assertGreater(score_both, 0.5)

        # TIP up, TLT down: TIP 65% dominates
        score_mixed = score_rates(tlt_closes=tlt_down, tip_closes=tip_up)
        self.assertIsNotNone(score_mixed)
        self.assertGreater(score_mixed, 0.0)

        # Only TIP: works cleanly
        score_tip_only = score_rates(tlt_closes=None, tip_closes=tip_up)
        self.assertIsNotNone(score_tip_only)
        self.assertGreater(score_tip_only, 0.5)

    def test_metals_3_tier_profit_scaling(self):
        """Verify 3-tier profit scaling (2.0R Tier 1, 3.2R Tier 2, trailing stop Tier 3)."""
        bull_bars = _generate_synthetic_daily_bars(days=150, trend="bull", start_price=100.0, seed=101)
        params = AiBacktestParams(
            preset="gold_silver_macro",
            atr_stop_mult=2.2,
            take_profit_r=3.2,
            trail_after_r=2.0,
            min_confidence=0.68,
        )
        res = run_ai_backtest(bull_bars, symbol="GLD", params=params)
        exit_reasons = [t["exit_reason"] for t in res["trade_log"]]
        self.assertTrue(any("take_profit_tier1_2.0r" in r for r in exit_reasons))

    def test_miners_and_dust_strictly_excluded_from_metal_strategy(self):
        """Verify that GDX, GDXJ and DUST are strictly excluded from AI Gold & Silver Macro playbook."""
        params = AiBacktestParams(preset="gold_silver_macro")
        row = self.gld_bars.iloc[-1].copy()
        prev = self.gld_bars.iloc[-2].copy()
        row["macro_composite_score"] = 2.0
        row["trend_regime"] = "bullish_above_sma200"

        for sym in ["GDX", "GDXJ", "DUST", "UGL", "GDXU", "GDXD"]:
            sig, conf, thesis = evaluate_ai_signal(
                row, prev, params, symbol=sym, macro_score=2.0
            )
            self.assertEqual(sig, Signal.HOLD)
            self.assertEqual(conf, 0.0)
            self.assertIn("strictly excluded", thesis.lower())

    def test_gdxu_and_gdxd_strictly_excluded_from_metal_strategy(self):
        """Verify that GDXU and GDXD are strictly excluded from the metal strategy."""
        params = AiBacktestParams(preset="gold_silver_macro")
        row = self.gld_bars.iloc[-1].copy()
        prev = self.gld_bars.iloc[-2].copy()
        row["macro_composite_score"] = 1.8
        row["trend_regime"] = "bullish_above_sma200"

        for sym in ["GDXU", "GDXD"]:
            sig, conf, thesis = evaluate_ai_signal(
                row, prev, params, symbol=sym, macro_score=1.8
            )
            self.assertEqual(sig, Signal.HOLD)
            self.assertEqual(conf, 0.0)
            self.assertIn("strictly excluded", thesis.lower())

    def test_ai_trader_live_excludes_gdx_gdxj_dust(self):
        """Verify AiTradingBot skips GDX, GDXJ, DUST, GDXU, GDXD in gold_silver_macro without querying LLM."""
        from bot.ai_trader import AiTradingBot

        config = MagicMock()
        config.ai_preset = "gold_silver_macro"
        config.symbol = "GDX"
        config.paper = True

        service = MagicMock()
        brain = MagicMock()
        brain.build_context.return_value = {
            "position_qty": 0.0,
            "mark": {"price": 35.0},
            "session": {"session": "open"},
        }

        bot = AiTradingBot.__new__(AiTradingBot)
        bot.config = config
        bot.service = service
        bot.brain = brain

        for excluded_sym in ("GDX", "GDXJ", "DUST", "UGL", "GDXU", "GDXD"):
            res = bot._run_symbol(excluded_sym)
            self.assertEqual(res["signal"], Signal.HOLD.value)
            self.assertIn("strictly excluded", res["reason"])
            brain.decide.assert_not_called()

    def test_ai_trader_inverse_decay_protection_in_management(self):
        """Verify AiTradingBot._manage_open_position trims inverse ETF when RSI >= 65."""
        from bot.ai_trader import AiTradingBot

        config = MagicMock()
        config.ai_preset = "gold_silver_macro"
        config.take_profit_r = 4.0
        config.trail_after_r = 2.0
        config.paper = True

        service = MagicMock()
        service.market_session.return_value = {"is_open": True}
        order_mock = MagicMock(id="order_trim_999")
        service.submit_order.return_value = order_mock

        bot = AiTradingBot.__new__(AiTradingBot)
        bot.config = config
        bot.service = service

        context = {
            "position": {"qty": 10.0, "side": "long", "avg_entry": 30.0, "r_multiple": 0.8},
            "mark": {"price": 31.0},
            "technicals": {"rsi_14": 68.0},
        }

        out = bot._manage_open_position("GLL", context)
        self.assertIn("scale_out", out)
        self.assertEqual(out["scale_out"]["id"], "order_trim_999")
        self.assertTrue(any("inverse decay protection trim" in a for a in out["actions"]))

    def test_saved_custom_engine_68_integrity(self):
        """Verify saved custom engine ce_978e8cd9cc does not contain GDX, DUST, or UGL and has 1Hour timeframe."""
        import json
        from pathlib import Path

        path = Path(".custom_engines.68.json")
        if path.exists():
            engines = json.loads(path.read_text(encoding="utf-8"))
            match = next((e for e in engines if e.get("id") == "ce_978e8cd9cc"), None)
            if match:
                symbols = match["choices"]["symbols"]
                self.assertNotIn("GDX", [s.strip() for s in symbols.split(",")])
                self.assertNotIn("GDXJ", [s.strip() for s in symbols.split(",")])
                self.assertNotIn("DUST", [s.strip() for s in symbols.split(",")])
                self.assertNotIn("UGL", [s.strip() for s in symbols.split(",")])
                self.assertNotIn("GDXU", [s.strip() for s in symbols.split(",")])
                self.assertNotIn("GDXD", [s.strip() for s in symbols.split(",")])
                self.assertIn("GLL", [s.strip() for s in symbols.split(",")])
                self.assertFalse(match["choices"]["metals_reversal_buy_on_stop"])
                self.assertEqual(match["choices"]["bar_timeframe"], "1Hour")
    
    def test_gold_silver_macro_entry_gates(self):
        """Verify Auto Trade entry_gates enforce macro thresholds and late-session inverse block."""
        from datetime import datetime, timezone
        from unittest.mock import patch
        from bot.ai_risk import entry_gates

        config = MagicMock()
        config.ai_preset = "gold_silver_macro"
        config.ai_daily_loss_limit_pct = 3.0
        config.ai_max_positions = 2
        config.ai_max_spread_bps = 25.0
        config.ai_cooldown_minutes = 60

        # GLD: negative macro score (< 0.0) should be blocked
        ctx_gld_neg = {
            "symbol": "GLD",
            "mark": {"bid": 240.0, "ask": 240.05},
            "precious_metals_intel": {"macro_composite_score": -0.25},
        }
        gate = entry_gates(config, ctx_gld_neg, open_positions=0, day_pl_pct=0.0)
        self.assertFalse(gate.allowed)
        self.assertIn("GLD macro score", gate.reason)

        # GLD: constructive macro score (>= 0.0) should be allowed
        ctx_gld_pos = {
            "symbol": "GLD",
            "mark": {"bid": 240.0, "ask": 240.05},
            "precious_metals_intel": {"macro_composite_score": 0.15},
        }
        gate = entry_gates(config, ctx_gld_pos, open_positions=0, day_pl_pct=0.0)
        self.assertTrue(gate.allowed)

        # SLV: macro < 0.2 and gsr_z < 0.8 should be blocked
        ctx_slv_unsupportive = {
            "symbol": "SLV",
            "mark": {"bid": 28.0, "ask": 28.01},
            "precious_metals_intel": {"macro_composite_score": 0.10, "gsr_z_score": 0.40},
        }
        gate = entry_gates(config, ctx_slv_unsupportive, open_positions=0, day_pl_pct=0.0)
        self.assertFalse(gate.allowed)
        self.assertIn("SLV macro score", gate.reason)

        # SLV: stretched GSR (gsr_z >= 0.8) should be allowed
        ctx_slv_stretched = {
            "symbol": "SLV",
            "mark": {"bid": 28.0, "ask": 28.01},
            "precious_metals_intel": {"macro_composite_score": 0.10, "gsr_z_score": 0.95},
        }
        gate = entry_gates(config, ctx_slv_stretched, open_positions=0, day_pl_pct=0.0)
        self.assertTrue(gate.allowed)

        # GLL: late session entry (>= 19 UTC / 3 PM ET) should be blocked
        late_dt = datetime(2026, 9, 15, 19, 15, tzinfo=timezone.utc)
        early_dt = datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc)
        ctx_gll = {
            "symbol": "GLL",
            "mark": {"bid": 25.0, "ask": 25.02},
            "precious_metals_intel": {"macro_composite_score": -0.80},
        }
        with patch("bot.ai_risk.datetime") as mock_dt:
            mock_dt.now.return_value = late_dt
            gate = entry_gates(config, ctx_gll, open_positions=0, day_pl_pct=0.0)
            self.assertFalse(gate.allowed)
            self.assertIn("Late-session entry", gate.reason)

        with patch("bot.ai_risk.datetime") as mock_dt:
            mock_dt.now.return_value = early_dt
            gate = entry_gates(config, ctx_gll, open_positions=0, day_pl_pct=0.0)
            self.assertTrue(gate.allowed)

    def test_ai_qty_for_risk_balanced_allocation_cap(self):
        """Verify ai_qty_for_risk caps single position allocation to 55% when max_positions >= 2."""
        from bot.config import Config

        cfg = Config.default(
            ai_risk_pct=1.8,
            ai_atr_stop_mult=1.6,
            ai_max_positions=2,
            risk_engine_enabled=True,
        )
        equity = 20000.0
        price = 100.0
        # Extremely small stop distance so target_qty = (20000 * 0.018) / 0.10 = 3600 shares ($360,000)
        stop_distance = 0.10
        qty = cfg.ai_qty_for_risk(price=price, stop_distance=stop_distance, equity=equity)
        # 55% of $20,000 = $11,000 / $100 = 110 shares
        self.assertAlmostEqual(qty, 110.0, places=2)

    def test_ai_brain_silver_gsr_boost(self):
        """Verify AiBrain._max_qty boosts SLV position by 25% when GSR z >= 0.8."""
        from bot.ai_brain import AiBrain
        from bot.config import Config

        cfg = Config.default(
            ai_preset="gold_silver_macro",
            ai_risk_pct=1.8,
            ai_atr_stop_mult=1.6,
            ai_max_positions=2,
            risk_engine_enabled=True,
            size_mode="ai",
        )
        brain = AiBrain.__new__(AiBrain)
        brain.config = cfg

        equity = 20000.0
        price = 30.0
        stop_dist = 1.50
        # Normal risk qty = (20000 * 0.018) / 1.50 = 240 shares (under 55% cap of 366.6 shares)

        # Normal GSR (z = 0.3)
        ctx_normal = {
            "symbol": "SLV",
            "mark": {"price": price},
            "account": {"equity": equity},
            "risk": {"stop_distance": stop_dist},
            "precious_metals_intel": {"gsr_z_score": 0.3},
        }
        qty_normal = brain._max_qty(ctx_normal)
        self.assertAlmostEqual(qty_normal, 240.0, places=1)

        # Stretched GSR (z = 1.1) -> 25% boost = 240 * 1.25 = 300 shares
        ctx_stretched = {
            "symbol": "SLV",
            "mark": {"price": price},
            "account": {"equity": equity},
            "risk": {"stop_distance": stop_dist},
            "precious_metals_intel": {"gsr_z_score": 1.1},
        }
        qty_stretched = brain._max_qty(ctx_stretched)
        self.assertAlmostEqual(qty_stretched, 300.0, places=1)

    def test_ai_trader_opposing_metals_position_blocks_buy(self):
        """Verify AiTradingBot skips BUY when an opposing metal/inverse position is active."""
        from bot.ai_trader import AiTradingBot
        from bot.ai_brain import AiDecision

        config = MagicMock()
        config.ai_preset = "gold_silver_macro"
        config.ai_min_confidence = 0.65
        config.require_approval = False
        config.cooldown_minutes = 0

        service = MagicMock()
        service.has_open_orders.return_value = False
        # Mock that GLL is currently open with 50 shares
        service.get_position_qty.side_effect = lambda sym: 50.0 if sym.upper() == "GLL" else 0.0

        brain = MagicMock()
        decision = AiDecision(
            action="buy",
            confidence=0.85,
            qty=10.0,
            thesis="Bullish gold setup",
            risks="Rates spike",
            thesis_en="Bullish gold setup",
            risks_en="Rates spike",
            news_bias="bullish",
            ta_bias="bullish",
            raw={"take_profit_target": 250.0, "stop_loss_target": 230.0},
            provider="openai",
            model="gpt-5.6-luna",
        )

        import threading
        bot = AiTradingBot.__new__(AiTradingBot)
        bot.config = config
        bot.service = service
        bot.approval_handler = None
        bot.brain = brain
        bot._execution_lock = threading.Lock()
        bot._open_positions = 0
        bot._arm_stop = MagicMock()

        context = {
            "symbol": "GLD",
            "position": {"qty": 0.0},
            "position_qty": 0.0,
            "mark": {"price": 240.0},
            "risk": {"stop_distance": 2.5},
            "technicals": {"close": 240.0, "atr_14": 3.0},
            "bars": [{"open": 239.0, "high": 241.0, "low": 238.0, "close": 240.0, "volume": 1000}],
        }
        brain.build_context.return_value = context
        brain.decide.return_value = (decision, context)
        brain.hold_decision.side_effect = lambda d, r: d

        with patch.object(bot, "_qty_for_session", return_value=10.0):
            res = bot._run_symbol("GLD")

        self.assertIn("skipped", res["reason"])
        self.assertIn("opposing", res["reason"].lower())
        self.assertIn("GLL", res["reason"])
        # Ensure no buy order was submitted
        service.submit_order.assert_not_called()

        # Reverse check: Holding GLD must block BUY on GLL
        service.get_position_qty.side_effect = lambda sym: 10.0 if sym.upper() == "GLD" else 0.0
        context_gll = {
            "symbol": "GLL",
            "position": {"qty": 0.0},
            "position_qty": 0.0,
            "mark": {"price": 25.0},
            "risk": {"stop_distance": 1.0},
            "technicals": {"close": 25.0, "atr_14": 0.8},
            "bars": [{"open": 24.5, "high": 25.5, "low": 24.0, "close": 25.0, "volume": 1000}],
        }
        decision_gll = AiDecision(
            action="buy",
            confidence=0.80,
            qty=20.0,
            thesis="Bearish gold setup, buy inverse GLL",
            risks="Gold bounce",
            thesis_en="Bearish gold setup, buy inverse GLL",
            risks_en="Gold bounce",
            news_bias="bearish",
            ta_bias="bearish",
            raw={"take_profit_target": 28.0, "stop_loss_target": 24.0},
            provider="openai",
            model="gpt-5.6-luna",
        )
        brain.build_context.return_value = context_gll
        brain.decide.return_value = (decision_gll, context_gll)

        with patch.object(bot, "_qty_for_session", return_value=20.0):
            res_gll = bot._run_symbol("GLL")

        self.assertIn("skipped", res_gll["reason"])
        self.assertIn("opposing", res_gll["reason"].lower())
        self.assertIn("GLD", res_gll["reason"])
        service.submit_order.assert_not_called()


class TestMetalsLiveBacktestParity(unittest.TestCase):
    """Tests verifying 100% parity between live Auto Trade and backtesting for gold_silver_macro."""

    def test_entry_gates_sma200_and_rsi_pullback(self):
        from bot.ai_risk import entry_gates

        config = MagicMock()
        config.ai_preset = "gold_silver_macro"
        config.ai_daily_loss_limit_pct = 3.0
        config.ai_max_positions = 2
        config.ai_max_spread_bps = 25.0
        config.ai_cooldown_minutes = 60

        # 1. Bearish below SMA200 must block GLD long
        ctx_bearish = {
            "symbol": "GLD",
            "mark": {"bid": 240.0, "ask": 240.05, "price": 240.0},
            "precious_metals_intel": {
                "macro_composite_score": 0.5,
                "trend_regime": "bearish_below_sma200",
            },
            "technicals": {"rsi_14": 45.0, "adx_14": 20.0, "price": 240.0, "sma": {"20": 242.0}},
        }
        res = entry_gates(config, ctx_bearish, open_positions=0, day_pl_pct=0.0, action="buy")
        self.assertFalse(res.allowed)
        self.assertIn("bearish_below_sma200", res.reason)

        # 2. Bullish above SMA200 but RSI overbought chase (RSI=70) must block long
        ctx_chase = {
            "symbol": "GLD",
            "mark": {"bid": 240.0, "ask": 240.05, "price": 250.0},
            "precious_metals_intel": {
                "macro_composite_score": 0.5,
                "trend_regime": "bullish_above_sma200",
            },
            "technicals": {"rsi_14": 70.0, "adx_14": 20.0, "price": 250.0, "sma": {"20": 240.0}},
        }
        res = entry_gates(config, ctx_chase, open_positions=0, day_pl_pct=0.0, action="buy")
        self.assertFalse(res.allowed)
        self.assertIn("extended above pullback zone", res.reason)

        # 3. Bullish above SMA200 with RSI=62 and high ADX (28.0) is allowed (expanded momentum window up to 65)
        ctx_momentum = {
            "symbol": "GLD",
            "mark": {"bid": 240.0, "ask": 240.05, "price": 240.0},
            "precious_metals_intel": {
                "macro_composite_score": 0.5,
                "trend_regime": "bullish_above_sma200",
            },
            "technicals": {"rsi_14": 62.0, "adx_14": 28.0, "price": 240.0, "sma": {"20": 240.0}},
        }
        res = entry_gates(config, ctx_momentum, open_positions=0, day_pl_pct=0.0, action="buy")
        self.assertTrue(res.allowed)

        # 4. Overextended above 50 SMA (dist_sma50_atr > 3.2) must block long
        ctx_overextended = {
            "symbol": "GLD",
            "mark": {"bid": 255.0, "ask": 255.05, "price": 255.0},
            "precious_metals_intel": {
                "macro_composite_score": 0.5,
                "trend_regime": "bullish_above_sma200",
            },
            "technicals": {"rsi_14": 55.0, "adx_14": 20.0, "price": 255.0, "sma": {"20": 250.0}, "dist_sma50_atr": 3.6},
        }
        res = entry_gates(config, ctx_overextended, open_positions=0, day_pl_pct=0.0, action="buy")
        self.assertFalse(res.allowed)
        self.assertIn("overextended", res.reason)

    def test_entry_gates_dollar_mixed_blocks_short(self):
        from bot.ai_risk import entry_gates

        config = MagicMock()
        config.ai_preset = "gold_silver_macro"
        config.ai_daily_loss_limit_pct = 3.0
        config.ai_max_positions = 2
        config.ai_max_spread_bps = 25.0
        config.ai_cooldown_minutes = 60

        ctx_short = {
            "symbol": "GLD",
            "mark": {"bid": 240.0, "ask": 240.05},
            "precious_metals_intel": {
                "macro_composite_score": -0.8,
                "trend_regime": "bearish_below_sma200",
                "dollar_mixed": True,
            },
            "technicals": {"rsi_14": 45.0},
        }
        res = entry_gates(config, ctx_short, open_positions=0, day_pl_pct=0.0, action="sell")
        self.assertFalse(res.allowed)
        self.assertIn("US Dollar Index data is mixed", res.reason)

    def test_should_scale_out_tier_logic(self):
        from bot.ai_risk import should_scale_out_tier

        # Standard preset (e.g. balanced) uses single tier @ take_profit_r (2.0R)
        cfg_balanced = MagicMock(ai_preset="balanced", ai_take_profit_r=2.0)
        should_trim, next_tier, trim_frac, _ = should_scale_out_tier(cfg_balanced, r=2.1, scale_tier=0)
        self.assertTrue(should_trim)
        self.assertEqual(next_tier, 1)
        self.assertEqual(trim_frac, 0.5)

        # gold_silver_macro Tier 1 @ 2.0R -> 25% trim
        cfg_macro = MagicMock(ai_preset="gold_silver_macro", ai_take_profit_r=3.2)
        should_trim, next_tier, trim_frac, _ = should_scale_out_tier(cfg_macro, r=2.05, scale_tier=0)
        self.assertTrue(should_trim)
        self.assertEqual(next_tier, 1)
        self.assertEqual(trim_frac, 0.25)

        # Already scaled Tier 1, currently at 2.5R -> should NOT scale out again
        should_trim, next_tier, trim_frac, _ = should_scale_out_tier(cfg_macro, r=2.5, scale_tier=1)
        self.assertFalse(should_trim)
        self.assertEqual(next_tier, 1)

        # Reaches Tier 2 @ 3.2R with normal ADX (20.0) -> triggers Tier 2
        should_trim, next_tier, trim_frac, _ = should_scale_out_tier(cfg_macro, r=3.25, scale_tier=1, adx=20.0)
        self.assertTrue(should_trim)
        self.assertEqual(next_tier, 2)
        self.assertAlmostEqual(trim_frac, 0.3333, places=3)

        # With strong ADX (30.0), Tier 2 expands to 4.0R. At 3.3R it should NOT trigger yet
        should_trim, next_tier, trim_frac, _ = should_scale_out_tier(cfg_macro, r=3.3, scale_tier=1, adx=30.0)
        self.assertFalse(should_trim)

        # But once it hits 4.0R, it triggers Tier 2!
        should_trim, next_tier, trim_frac, _ = should_scale_out_tier(cfg_macro, r=4.05, scale_tier=1, adx=30.0)
        self.assertTrue(should_trim)
        self.assertEqual(next_tier, 2)

        # Once scale_tier = 2, runner trails (no further partial scale-outs)
        should_trim, next_tier, trim_frac, _ = should_scale_out_tier(cfg_macro, r=5.0, scale_tier=2, adx=30.0)
        self.assertFalse(should_trim)
        self.assertEqual(next_tier, 2)

    def test_live_trader_manage_open_position_tier1_and_breakeven_stop(self):
        from bot.ai_trader import AiTradingBot
        from bot.config import Config

        cfg = Config.default(ai_preset="gold_silver_macro", paper=True)
        service = MagicMock()
        service.replace_stop_loss.return_value = True

        bot = AiTradingBot.__new__(AiTradingBot)
        bot.config = cfg
        bot.service = service
        bot._arm_stop = MagicMock()
        bot._scale_out = MagicMock(return_value={"status": "scaled_out", "qty": 25.0})

        context = {
            "symbol": "GLD",
            "position": {
                "side": "long",
                "qty": 100.0,
                "avg_entry": 100.0,
                "r_multiple": 2.1,
            },
            "mark": {"price": 110.5},
            "technicals": {"adx_14": 20.0},
            "precious_metals_intel": {"macro_composite_score": 0.5},
            "risk": {"stop_distance": 5.0},
        }

        mock_state = {"scale_tier": 0, "scaled_out": False, "peak_r": 0.0}
        with patch("bot.ai_trader.load_trade_state", return_value=mock_state), \
             patch("bot.ai_trader.save_trade_state") as mock_save:
            res = bot._manage_open_position("GLD", context)

        # Verify scale_out was triggered for 25 shares (25% of 100)
        bot._scale_out.assert_called_once_with("GLD", context["position"], trim_qty=25.0)
        # Verify state updated to next_tier=1
        self.assertEqual(mock_state["scale_tier"], 1)
        self.assertTrue(mock_state["scaled_out"])
        mock_save.assert_called()
        # Verify replace_stop_loss was called with entry price (100.0)
        service.replace_stop_loss.assert_called_once_with("GLD", 100.0)
        self.assertIn("scale_out", res)
        self.assertTrue(any("stop ratcheted to breakeven" in a for a in res.get("actions", [])))


if __name__ == "__main__":
    unittest.main()

