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
    compute_three_confirmation_matrix,
    detect_metals_divergence,
    fetch_metals_macro_context,
    get_metal_category,
    is_precious_metal,
    momentum,
    protect_metals_position_before_event,
    score_dollar,
    score_gsr,
    score_oil,
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
        # Calibrated 1.6 ATR stop and 4.0R target with 2.0R breakeven trail ratchet.
        self.assertEqual(preset.atr_stop_mult, 1.6)
        self.assertEqual(preset.take_profit_r, 4.0)
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
        self.assertEqual(bp["choices"]["symbols"], "GLD, SLV, GDXU, GLL, GDXD")
        self.assertEqual(bp["choices"]["ai_take_profit_r"], 4.0)
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
        self.assertTrue(ctx["short_reversal_to_long"])
        self.assertIsNotNone(ctx["short_reversal_reason"])

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

        # Condition 4: Asset Selection: GDXU requires macro > 1.5 & strong trend
        row["rsi14"] = 48.0
        row["close"] = 200.0
        row["adx14"] = 25.0
        sig, conf, thesis = evaluate_ai_signal(row, prev, params, symbol="GDXU", macro_score=1.2)
        self.assertEqual(sig, Signal.HOLD)
        sig, conf, thesis = evaluate_ai_signal(row, prev, params, symbol="GDXU", macro_score=1.8)
        self.assertEqual(sig, Signal.BUY)

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
        """Rule 7: Take-profit 4.0R, trailing stop ratchet after 2.0R."""
        preset = get_ai_preset("gold_silver_macro")
        self.assertEqual(preset.take_profit_r, 4.0)
        self.assertEqual(preset.trail_after_r, 2.0)
        self.assertEqual(preset.atr_stop_mult, 1.6)
        self.assertEqual(preset.risk_pct, 1.8)
        # Verify auto-defaults via __post_init__
        auto_params = AiBacktestParams(preset="gold_silver_macro")
        self.assertEqual(auto_params.take_profit_r, 4.0)
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

    def test_miners_and_dust_strictly_excluded_from_metal_strategy(self):
        """Verify that GDX, GDXJ and DUST are strictly excluded from AI Gold & Silver Macro playbook."""
        params = AiBacktestParams(preset="gold_silver_macro")
        row = self.gld_bars.iloc[-1].copy()
        prev = self.gld_bars.iloc[-2].copy()
        row["macro_composite_score"] = 2.0
        row["trend_regime"] = "bullish_above_sma200"

        for sym in ["GDX", "GDXJ", "DUST", "UGL"]:
            sig, conf, thesis = evaluate_ai_signal(
                row, prev, params, symbol=sym, macro_score=2.0
            )
            self.assertEqual(sig, Signal.HOLD)
            self.assertEqual(conf, 0.0)
            self.assertIn("strictly excluded", thesis.lower())

    def test_gdxu_and_gdxd_allowed_in_metal_strategy(self):
        """Verify that GDXU and GDXD are kept and allowed in the metal strategy."""
        params = AiBacktestParams(preset="gold_silver_macro")
        frame = compute_ai_indicator_frame(self.gld_bars, symbol="GDXU")
        row = frame.iloc[-1].copy()
        prev = frame.iloc[-2].copy()
        row["macro_composite_score"] = 1.8
        row["trend_regime"] = "bullish_above_sma200"
        row["close"] = 100.0
        row["open"] = 98.0
        row["high"] = 102.0
        row["low"] = 97.0
        row["sma200"] = 80.0
        row["sma50"] = 90.0
        row["sma20"] = 99.0
        row["rsi14"] = 50.0
        row["adx14"] = 25.0
        row["dist_sma50_atr"] = 1.0

        # GDXU in bull regime with high macro score
        sig, conf, thesis = evaluate_ai_signal(
            row, prev, params, symbol="GDXU", macro_score=1.8
        )
        self.assertEqual(sig, Signal.BUY)
        self.assertGreaterEqual(conf, 0.70)

        # GDXD as inverse ETF in inverse trend
        sig, conf, thesis = evaluate_ai_signal(
            row, prev, params, symbol="GDXD"
        )
        self.assertEqual(sig, Signal.BUY)
        self.assertGreaterEqual(conf, 0.70)

    def test_ai_trader_live_excludes_gdx_gdxj_dust(self):
        """Verify AiTradingBot skips GDX, GDXJ, and DUST in gold_silver_macro without querying LLM."""
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

        for excluded_sym in ("GDX", "GDXJ", "DUST", "UGL"):
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
                self.assertIn("GDXU", [s.strip() for s in symbols.split(",")])
                self.assertIn("GDXD", [s.strip() for s in symbols.split(",")])
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

        # Case C: rising Dollar while Gold holds bullish structure prevents
        # fresh bearish exposure in the live/paper path too.
        ctx_divergence = {
            "symbol": "GLD",
            "mark": {"bid": 240.0, "ask": 240.05},
            "precious_metals_intel": {
                "macro_composite_score": 0.15,
                "gold_dollar_divergence": True,
            },
        }
        gate = entry_gates(
            config, ctx_divergence, open_positions=0, day_pl_pct=0.0, action="sell"
        )
        self.assertFalse(gate.allowed)
        self.assertIn("decoupling", gate.reason)

        ctx_divergence["symbol"] = "GLL"
        gate = entry_gates(
            config, ctx_divergence, open_positions=0, day_pl_pct=0.0, action="buy"
        )
        self.assertFalse(gate.allowed)
        self.assertIn("decoupling", gate.reason)

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

    @patch("bot.macro_releases.fetch_fomc_live_release")
    def test_fomc_post_release_catalyst_waits_for_market_confirmation(self, mock_fomc):
        """A released hike is a catalyst, not evidence of a bearish reaction by itself."""
        mock_fomc.return_value = {
            "title": "Federal Funds Rate",
            "statement_title": "Federal Reserve issues FOMC statement",
            "actual": "4.00%",
            "action": "hike",
            "change_bps": 25,
            "bias": "hawkish",
            "metals_impact": "Bearish headwind for GLD/SLV",
            "status": "released",
            "released": True,
        }
        service = MagicMock()
        service.get_mark_price.side_effect = lambda s: {
            "GLD": {"price": 240.0},
            "SLV": {"price": 28.0},
            "GDX": {"price": 35.0},
            "UUP": {"price": 28.5},
            "TLT": {"price": 92.0},
        }.get(s, {"price": 100.0})

        idx = pd.date_range("2024-01-01", periods=60, freq="B", tz="UTC")
        series = pd.Series(100.0, index=idx)
        service.get_bars.return_value = pd.DataFrame(
            {"close": series, "open": series, "high": series, "low": series, "volume": 1000}
        )

        now = datetime.now(timezone.utc)
        calendar = [
            {
                "title": "Federal Funds Rate",
                "impact": "High",
                "when_utc": (now - timedelta(minutes=15)).isoformat(),
                "forecast": "4.00%",
                "previous": "3.75%",
                "actual": "4.00%",
                "status": "released",
                "released": True,
            },
            {
                "title": "FOMC Press Conference",
                "impact": "High",
                "when_utc": (now + timedelta(minutes=15)).isoformat(),
                "rate_already_released": True,
                "status": "released",
                "released": True,
            },
        ]

        context = fetch_metals_macro_context(service, "GLD", calendar=calendar)
        self.assertEqual(context["macro_risk_level"], "post_release_catalyst")
        self.assertIsNotNone(context.get("active_catalyst"))
        self.assertEqual(context["active_catalyst"]["actual"], "4.00%")
        self.assertEqual(context["active_catalyst"]["action"], "hike")
        self.assertEqual(
            context["active_catalyst"]["market_reaction"],
            "awaiting_cross_asset_confirmation",
        )
        self.assertEqual(context["factor_scores"]["rates"], 0.0)
        self.assertEqual(context["yield_trend"], "neutral")

    def test_day_backtest_ai_metals_breakout_defaults(self):
        """Verify DayBacktestParams(preset='ai_metals_breakout') syncs with Auto Trade day_preset."""
        from bot.day_backtest import DayBacktestParams
        from bot.day_presets import get_preset as get_day_preset

        preset = get_day_preset("ai_metals_breakout")
        params = DayBacktestParams(preset="ai_metals_breakout")
        self.assertEqual(params.sub_mode, preset.sub_mode)
        self.assertEqual(params.sub_mode, "vwap_trend")
        self.assertEqual(params.ema_fast, preset.ema_fast)
        self.assertEqual(params.ema_fast, 13)
        self.assertEqual(params.ema_slow, preset.ema_slow)
        self.assertEqual(params.ema_slow, 34)
        self.assertEqual(params.profit_target_r, preset.profit_target_r)
        self.assertEqual(params.profit_target_r, 3.8)
        self.assertEqual(params.stop_atr_mult, preset.stop_atr_mult)
        self.assertEqual(params.stop_atr_mult, 2.0)
        self.assertEqual(params.side, preset.side)
        self.assertEqual(params.side, "long_only")
        self.assertEqual(params.max_trades_per_day, preset.max_trades_per_day)
        self.assertEqual(params.max_trades_per_day, 2)
        self.assertEqual(params.open_buffer_mins, preset.open_buffer_mins)
        self.assertEqual(params.open_buffer_mins, 20)

    def test_web_state_day_backtest_resolves_metals_preset_defaults(self):
        """Verify web_state _run_day_backtest populates preset defaults when fields are not overridden."""
        from bot.web_state import AppState, RunSettings

        state = AppState()
        # Ensure desk settings have standard 9/21 defaults
        state.settings = RunSettings()
        self.assertEqual(state.settings.day_ema_fast, 9)
        self.assertEqual(state.settings.day_ema_slow, 21)

        # Mock service.get_bars_range and _trim_backtest_bars
        mock_bars = pd.DataFrame(
            {
                "open": [100.0] * 50,
                "high": [101.0] * 50,
                "low": [99.0] * 50,
                "close": [100.5] * 50,
                "volume": [1000] * 50,
            },
            index=pd.date_range("2026-03-02 09:30", periods=50, freq="5min", tz="America/New_York"),
        )
        with patch.object(AppState, "_base_config"), \
             patch("bot.web_state.AlpacaService") as mock_srv_cls, \
             patch("bot.web_state.run_day_backtest") as mock_run:
            srv = mock_srv_cls.return_value
            srv.get_bars_range.return_value = mock_bars
            mock_run.return_value = {"trades": 0, "win_rate_pct": 0.0}

            state._run_day_backtest(
                days=5,
                bar_timeframe="15Min",
                initial_cash=10000.0,
                symbols="GLD",
                symbol="GLD",
                day_preset="ai_metals_breakout",
            )
            self.assertTrue(mock_run.called)
            called_params = mock_run.call_args[1]["params"]
            self.assertEqual(called_params.ema_fast, 13)
            self.assertEqual(called_params.ema_slow, 34)
            self.assertEqual(called_params.profit_target_r, 3.8)
            self.assertEqual(called_params.stop_atr_mult, 2.0)
            self.assertEqual(called_params.max_trades_per_day, 2)
            self.assertEqual(called_params.open_buffer_mins, 20)
            self.assertEqual(called_params.side, "long_only")

    def test_web_state_ai_backtest_metals_reversal_buy_sync(self):
        """Verify web_state _run_ai_backtest passes metals_reversal_buy_on_stop to AiBacktestParams."""
        from bot.web_state import AppState, RunSettings

        state = AppState()
        state.settings = RunSettings()
        state.settings.metals_reversal_buy_on_stop = True

        mock_daily_bars = pd.DataFrame(
            {
                "open": [100.0] * 70,
                "high": [101.0] * 70,
                "low": [99.0] * 70,
                "close": [100.5] * 70,
                "volume": [1000] * 70,
            },
            index=pd.date_range("2026-01-01", periods=70, freq="B", tz="UTC"),
        )
        with patch.object(AppState, "_base_config"), \
             patch("bot.web_state.AlpacaService") as mock_srv_cls, \
             patch("bot.web_state.run_ai_backtest") as mock_run:
            srv = mock_srv_cls.return_value
            srv.get_bars_range.return_value = mock_daily_bars
            mock_run.return_value = {"trades": 0}

            state._run_ai_backtest(
                days=30,
                bar_timeframe="1Day",
                initial_cash=10000.0,
                symbols="GLD",
                symbol="GLD",
                ai_preset="gold_silver_macro",
            )
            self.assertTrue(mock_run.called)
            params = mock_run.call_args[1]["params"]
            self.assertTrue(params.reversal_buy_on_stop)
            self.assertEqual(params.take_profit_r, 4.0)
            self.assertEqual(params.atr_stop_mult, 1.6)
            self.assertEqual(params.trail_after_r, 2.0)

    def test_run_strategy_backtest_accepts_metals_reversal_buy_on_stop(self):
        """Verify AppState.run_strategy_backtest accepts metals_reversal_buy_on_stop without TypeError."""
        from bot.web_state import AppState, RunSettings
        from bot.webapp import BacktestIn
        from unittest.mock import patch

        state = AppState()
        state.settings = RunSettings()

        # 1. Verify BacktestIn model dumps metals_reversal_buy_on_stop and run_strategy_backtest accepts it
        backtest_payload = BacktestIn(
            mode="ai",
            symbol="GLD",
            symbols="GLD",
            days=30,
            ai_preset="gold_silver_macro",
            metals_reversal_buy_on_stop=False,
        ).model_dump()

        self.assertIn("metals_reversal_buy_on_stop", backtest_payload)
        self.assertIs(backtest_payload["metals_reversal_buy_on_stop"], False)

        mock_daily_bars = pd.DataFrame(
            {
                "open": [100.0] * 70,
                "high": [101.0] * 70,
                "low": [99.0] * 70,
                "close": [100.5] * 70,
                "volume": [1000] * 70,
            },
            index=pd.date_range("2026-01-01", periods=70, freq="B", tz="UTC"),
        )
        with patch.object(AppState, "_base_config"), \
             patch("bot.web_state.AlpacaService") as mock_srv_cls, \
             patch("bot.web_state.run_ai_backtest") as mock_run:
            srv = mock_srv_cls.return_value
            srv.get_bars_range.return_value = mock_daily_bars
            mock_run.return_value = {"trades": 0}

            # Unpack all fields from BacktestIn.model_dump() - exactly what /api/backtest does
            state.run_strategy_backtest(**backtest_payload)
            self.assertTrue(mock_run.called)
            called_params = mock_run.call_args[1]["params"]
            self.assertFalse(called_params.reversal_buy_on_stop)

            # Also verify unexpected future kwargs do not raise TypeError
            mock_run.reset_mock()
            backtest_payload_with_extra = dict(backtest_payload)
            backtest_payload_with_extra["unknown_future_field"] = 12345
            state.run_strategy_backtest(**backtest_payload_with_extra)
            self.assertTrue(mock_run.called)

    def test_js_backtest_metals_defaults_sync(self):
        """Verify backtest.js DAY_PRESET_DEFAULTS contains the exact values from day_presets.py."""
        from pathlib import Path
        import re

        js_path = Path("web/static/js/backtest.js")
        self.assertTrue(js_path.exists())
        js_content = js_path.read_text(encoding="utf-8")

        # Check ai_metals_breakout line
        match = re.search(r'ai_metals_breakout:\s*\{([^}]+)\}', js_content)
        self.assertIsNotNone(match)
        chunk = match.group(1)
        self.assertIn('"long_only"', chunk)
        self.assertIn('tp_r: 3.8', chunk)
        self.assertIn('stop_atr: 2.0', chunk)
        self.assertIn('fast: 13', chunk)
        self.assertIn('slow: 34', chunk)
        self.assertIn('max_trades: 2', chunk)


class TestMetalsDollarIndexOnly(unittest.TestCase):
    def test_macro_context_dollar_only_mode(self):
        service = MagicMock()
        service.get_mark_price.return_value = {"price": 26.5}
        dates = pd.date_range(end=datetime.now(timezone.utc), periods=80, freq="D")
        gld_series = [240.0 for _ in range(80)]
        # Falling dollar (UUP dropping from 29 to 27)
        uup_series = [29.0 - (i * 0.025) for i in range(80)]

        service.get_bars.side_effect = lambda sym, **kw: pd.DataFrame(
            {"close": uup_series if sym == "UUP" else gld_series}, index=dates
        )

        dummy_events = [
            {"event": "FOMC Rate Decision", "time_utc": datetime.now(timezone.utc).isoformat(), "impact": "HIGH"}
        ]

        # Call with track_dollar_only=True
        ctx = fetch_metals_macro_context(service, "GLD", calendar=dummy_events, track_dollar_only=True)
        self.assertTrue(ctx["dollar_only_mode"])
        self.assertTrue(ctx["track_dollar_only"])
        self.assertEqual(ctx["relevant_macro_events"], [])
        self.assertEqual(ctx["events_5m_imminent"], [])
        self.assertEqual(ctx["macro_risk_level"], "normal")
        self.assertEqual(ctx["macro_composite_score"], round(ctx["factor_scores"]["dollar"] * 3.0, 2))
        self.assertIn("dollar_signal", ctx)
        self.assertEqual(ctx["dollar_signal"], "bullish")
        self.assertEqual(ctx["dollar_action"], "buy")

    def test_ai_brain_dollar_only_prompt(self):
        from bot.ai_brain import AiBrain
        from bot.config import Config

        config = Config.default(
            strategy_mode="ai",
            ai_preset="gold_silver_macro",
            ai_min_confidence=0.60,
            metals_dollar_index_only=True,
        )
        service = MagicMock()
        service.get_bars.return_value = pd.DataFrame()
        service.get_position_detail.return_value = {"qty": 0}
        service.account_summary.return_value = {"equity": 10000}
        service.recent_activity.return_value = {}
        service.get_mark_price.return_value = {"price": 240.0}
        service.market_session.return_value = {"session": "open"}

        with patch("bot.ai_brain.fetch_metals_macro_context") as mock_metals, \
             patch("bot.ai_brain.compute_technicals") as mock_tech, \
             patch("bot.ai_brain.fetch_news") as mock_news, \
             patch("bot.ai_brain.fetch_earnings") as mock_earn:
            mock_tech.return_value = {"ok": True, "atr_14": 2.5}
            mock_news.return_value = []
            mock_earn.return_value = {}
            mock_metals.return_value = {
                "is_precious_metal": True,
                "dollar_only_mode": True,
                "track_dollar_only": True,
                "dollar_score": 0.8,
                "dollar_trend": "falling",
                "dollar_signal": "bullish",
                "dollar_action": "buy",
                "imminent_event_risk": False,
                "macro_composite_score": 0.8,
                "dollar_live_price": 27.5,
                "dollar_change_pct": -0.5,
            }

            brain = AiBrain(config, service, MagicMock())
            ctx = brain.build_context("GLD")
            prompt = brain._format_prompt("GLD", ctx)

            self.assertIn("MANDATORY: REAL-TIME DOLLAR INDEX", prompt)
            self.assertIn("SOLELY DRIVEN BY THE US DOLLAR INDEX", prompt)
            self.assertIn("EXPLICITLY BYPASSED AND IGNORED", prompt)

    def test_ai_risk_dollar_only_entry_and_reversal(self):
        from bot.ai_risk import entry_gates, reversal_gate
        from bot.config import Config

        config = Config.default(
            strategy_mode="ai",
            ai_preset="gold_silver_macro",
            metals_dollar_index_only=True,
            ai_min_hold_minutes=60,
        )
        context_long = {
            "symbol": "GLD",
            "precious_metals_intel": {
                "is_precious_metal": True,
                "dollar_only_mode": True,
                "macro_composite_score": 0.75,
                "track_dollar_only": True,
            },
            "news": [{"impact": "extreme_negative"}],
            "activity": {"last_fill_age_min": 5},
            "position": {"qty": 10},
        }

        # Long GLD allowed on positive dollar macro score (weak dollar)
        gate_long = entry_gates(config, context_long, open_positions=0, day_pl_pct=0.0)
        self.assertTrue(gate_long.allowed, f"Expected allowed but got: {gate_long.reason}")

        # Long GLD blocked on negative dollar macro score (strong dollar)
        context_strong_dollar = {
            "symbol": "GLD",
            "precious_metals_intel": {
                "is_precious_metal": True,
                "dollar_only_mode": True,
                "macro_composite_score": -0.75,
                "track_dollar_only": True,
            },
        }
        gate_blocked = entry_gates(config, context_strong_dollar, open_positions=0, day_pl_pct=0.0)
        self.assertFalse(gate_blocked.allowed)
        self.assertIn("Dollar Index is strengthening", gate_blocked.reason)

        # Inverse ETF GLL allowed on strong dollar (negative macro score for metals)
        context_strong_dollar["symbol"] = "GLL"
        gate_gll = entry_gates(config, context_strong_dollar, open_positions=0, day_pl_pct=0.0)
        self.assertTrue(gate_gll.allowed)

        # Inverse ETF GLL blocked on weak dollar (positive macro score for metals)
        context_long["symbol"] = "GLL"
        gate_gll_blocked = entry_gates(config, context_long, open_positions=0, day_pl_pct=0.0)
        self.assertFalse(gate_gll_blocked.allowed)
        self.assertIn("Dollar Index is weakening", gate_gll_blocked.reason)

        # Reversal gate bypasses min_hold_minutes when in dollar_only_mode
        rev_gate = reversal_gate(config, context_long, confidence=0.70)
        self.assertTrue(rev_gate.allowed)

    def test_backtest_ai_dollar_only_bypasses_events(self):
        from bot.ai_backtest import evaluate_ai_signal, AiBacktestParams
        from bot.strategy import Signal

        params = AiBacktestParams(
            preset="gold_silver_macro",
            metals_dollar_index_only=True,
        )

        base_data = {
            "close": 240.0,
            "open": 239.5,
            "high": 241.0,
            "low": 239.0,
            "sma10": 239.0,
            "sma20": 238.0,
            "sma50": 235.0,
            "sma200": 220.0,
            "rsi14": 50.0,
            "adx14": 25.0,
            "atr_pct": 1.5,
            "dist_sma50_atr": 0.5,
            "macd_hist": 0.2,
            "vol_ratio": 1.0,
            "bb_pct_b": 0.5,
        }

        row_weak_dollar = pd.Series({
            **base_data,
            "macro_composite_score": 0.7,
            "dollar_trend": "falling",
            "dollar_score": 0.7,
        })

        # Bypasses event_imminent_45m=True because metals_dollar_index_only is True
        sig, conf, reason = evaluate_ai_signal(
            row_weak_dollar, None, params, symbol="GLD", event_imminent_45m=True
        )
        self.assertEqual(sig, Signal.BUY)
        self.assertIn("Dollar falling", reason)

        row_strong_dollar = pd.Series({
            **base_data,
            "macro_composite_score": -0.7,
            "dollar_trend": "rising",
            "dollar_score": -0.7,
        })

        # Strong dollar triggers SELL on GLD
        sig2, conf2, reason2 = evaluate_ai_signal(
            row_strong_dollar, None, params, symbol="GLD", event_imminent_45m=True
        )
        self.assertEqual(sig2, Signal.SELL)
        self.assertIn("Dollar rising", reason2)

        # Strong dollar triggers BUY on inverse ETF GLL
        sig3, conf3, reason3 = evaluate_ai_signal(
            row_strong_dollar, None, params, symbol="GLL", event_imminent_45m=True
        )
        self.assertEqual(sig3, Signal.BUY)
        self.assertIn("BUY inverse ETF", reason3)

    def test_web_state_event_protection_bypassed(self):
        from bot.web_state import AppState, RunSettings

        state = AppState()
        state.settings = RunSettings()
        state.settings.metals_dollar_index_only = True

        # When metals_dollar_index_only is enabled, check_metals_event_protection returns [] immediately
        prot = state.check_metals_event_protection()
        self.assertEqual(prot, [])

    def test_ai_trader_and_desk_risk_dollar_only_bypasses_event_protection(self):
        from bot.ai_trader import AiTradingBot
        from bot.desk_risk import manage_open_position

        config = MagicMock()
        config.metals_dollar_index_only = True
        config.paper = True
        config.stop_loss_pct = 2.0
        config.take_profit_r = 4.0
        config.trail_after_r = 2.0

        service = MagicMock()
        service.market_session.return_value = {"is_open": True}

        context = {
            "position": {"qty": 10.0, "side": "long", "avg_entry": 240.0, "r_multiple": 0.5},
            "mark": {"price": 242.0},
            "technicals": {"rsi_14": 52.0},
            "economic_calendar": [
                {
                    "title": "FOMC Rate Decision",
                    "when_utc": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
                    "impact": "High",
                }
            ],
        }

        bot = AiTradingBot.__new__(AiTradingBot)
        bot.config = config
        bot.service = service

        with patch("bot.ai_trader.protect_metals_position_before_event") as mock_protect_ai, \
             patch("bot.desk_risk.protect_metals_position_before_event") as mock_protect_desk:
            out_ai = bot._manage_open_position("GLD", context)
            self.assertNotIn("event_protection", out_ai)
            mock_protect_ai.assert_not_called()

            out_desk = manage_open_position(
                config=config,
                service=service,
                symbol="GLD",
                side="long",
                entry=240.0,
                price=242.0,
                qty=10.0,
                stop_distance=2.5,
                current_stop=237.5,
            )
            self.assertNotIn("event_protection", out_desk)
            mock_protect_desk.assert_not_called()


    def test_ai_brain_dollar_only_context_bypass(self):
        import dataclasses
        from bot.ai_brain import AiBrain
        from bot.config import Config

        cfg = dataclasses.replace(Config.from_env(), metals_dollar_index_only=True)
        brain = AiBrain(cfg, service=MagicMock(), provider=MagicMock())
        brain.service.get_bars.return_value = pd.DataFrame()
        brain.service.get_position_detail.return_value = {"qty": 0}
        brain.service.account_summary.return_value = {"equity": 10000}
        brain.service.recent_activity.return_value = {}
        brain.service.get_mark_price.return_value = {"price": 100.0}

        with patch("bot.ai_brain.fetch_news") as mock_news, \
             patch("bot.ai_brain.fetch_economic_calendar") as mock_cal, \
             patch("bot.ai_brain.fetch_earnings") as mock_earn, \
             patch("bot.ai_brain.fetch_metals_macro_context") as mock_macro:
            mock_macro.return_value = {"dollar_score": 0.5, "track_dollar_only": True}
            ctx = brain.build_context("GLD")

            # External calls must be completely bypassed
            mock_news.assert_not_called()
            mock_cal.assert_not_called()
            mock_earn.assert_not_called()
            self.assertEqual(ctx["news"], [])
            self.assertEqual(ctx["economic_calendar"], [])
            self.assertEqual(ctx["earnings"], {})
            self.assertEqual(ctx["desk_lessons"], [])
            mock_macro.assert_called_once()
            self.assertTrue(mock_macro.call_args[1].get("track_dollar_only"))


class TestMetalsStrategyEnhancements(unittest.TestCase):
    """Tests for Sept 17/18 macro learnings: oil inflation tell, DXY divergence, 3-confirmation matrix, and dynamic catalyst."""

    def test_score_oil_and_trend(self):
        # Falling oil (USO drops) -> positive score (inflation cooling tailwind for gold)
        falling_oil = pd.Series([100.0 - i * 0.4 for i in range(70)])
        score_fall, trend_fall = score_oil(falling_oil)
        self.assertIsNotNone(score_fall)
        self.assertGreater(score_fall, 0.25)
        self.assertEqual(trend_fall, "cooling_inflation")

        # Rising oil (USO rallies) -> negative score (heating inflation)
        rising_oil = pd.Series([70.0 + i * 0.5 for i in range(70)])
        score_rise, trend_rise = score_oil(rising_oil)
        self.assertIsNotNone(score_rise)
        self.assertLess(score_rise, -0.25)
        self.assertEqual(trend_rise, "heating_inflation")

        # Insufficient data
        score_empty, trend_empty = score_oil(pd.Series([100.0]))
        self.assertIsNone(score_empty)
        self.assertEqual(trend_empty, "unknown")

    def test_detect_metals_divergence(self):
        # Case C Divergence: DXY is rising, but Gold is holding strong above 200 SMA
        gld_series = pd.Series([200.0 + i * 0.5 for i in range(20)])
        div = detect_metals_divergence(
            dollar_score=-0.40,
            dollar_trend="rising",
            gld_closes=gld_series,
            trend_regime="bullish_above_sma200",
        )
        self.assertTrue(div["divergence_active"])
        self.assertTrue(div["prohibit_shorts"])
        self.assertEqual(div["divergence_type"], "bullish_decoupling")

        # Normal correlation: DXY is falling, Gold is bullish
        no_div = detect_metals_divergence(
            dollar_score=0.40,
            dollar_trend="falling",
            gld_closes=gld_series,
            trend_regime="bullish_above_sma200",
        )
        self.assertFalse(no_div["divergence_active"])
        self.assertFalse(no_div["prohibit_shorts"])

        # A stale long-term bull regime is not enough.  A fresh five-day
        # breakdown must leave the short side available despite DXY strength.
        declining_gold = pd.Series([200.0 + i * 0.5 for i in range(15)] + [207.0 - i * 1.5 for i in range(5)])
        breakdown = detect_metals_divergence(
            dollar_score=-0.40,
            dollar_trend="rising",
            gld_closes=declining_gold,
            trend_regime="bullish_above_sma200",
        )
        self.assertFalse(breakdown["divergence_active"])

    def test_compute_three_confirmation_matrix(self):
        # All three aligned: DXY falling, Yields falling, Gold bullish
        all_aligned = compute_three_confirmation_matrix(
            dollar_score=0.35,
            dollar_trend="falling",
            rates_score=0.40,
            yield_trend="falling_yields",
            trend_regime="bullish_above_sma200",
        )
        self.assertTrue(all_aligned["conf_dollar"])
        self.assertTrue(all_aligned["conf_rates"])
        self.assertTrue(all_aligned["conf_gold"])
        self.assertEqual(all_aligned["confirmations_count"], 3)
        self.assertEqual(all_aligned["alignment_state"], "all_three_aligned")

        # Two aligned: DXY neutral, Yields falling, Gold bullish
        two_aligned = compute_three_confirmation_matrix(
            dollar_score=0.0,
            dollar_trend="neutral",
            rates_score=0.30,
            yield_trend="falling_yields",
            trend_regime="bullish_above_sma200",
        )
        self.assertFalse(two_aligned["conf_dollar"])
        self.assertTrue(two_aligned["conf_rates"])
        self.assertTrue(two_aligned["conf_gold"])
        self.assertEqual(two_aligned["confirmations_count"], 2)
        self.assertEqual(two_aligned["alignment_state"], "moderate_aligned")

        # Divergence warning overrides
        div_state = compute_three_confirmation_matrix(
            dollar_score=-0.40,
            dollar_trend="rising",
            rates_score=0.20,
            yield_trend="falling_yields",
            trend_regime="bullish_above_sma200",
            divergence_active=True,
        )
        self.assertEqual(div_state["alignment_state"], "divergent_warning")
        self.assertIn("prohibit shorts", div_state["summary"].lower())
    def test_dynamic_post_release_catalyst_hike_absorbed_rebound(self):
        """When FOMC hikes rate but yields fall post-announcement, detect absorbed rebound."""
        service = MagicMock()
        service.get_mark_price.side_effect = lambda s: {
            "GLD": {"price": 240.0},
            "SLV": {"price": 28.0},
            "UUP": {"price": 28.0},
            "TLT": {"price": 95.0},
            "USO": {"price": 70.0},
        }.get(s, {"price": 100.0})

        idx = pd.date_range("2024-01-01", periods=70, freq="B", tz="UTC")
        # TLT is strongly rising (yields falling)
        tlt_series = pd.Series([85.0 + i * 0.2 for i in range(70)], index=idx)
        gld_series = pd.Series([220.0 + i * 0.3 for i in range(70)], index=idx)
        slv_series = pd.Series([25.0 + i * 0.05 for i in range(70)], index=idx)

        def mock_bars(sym, limit=None, timeframe=None):
            if sym == "TLT":
                return pd.DataFrame({"close": tlt_series})
            if sym == "GLD":
                return pd.DataFrame({"close": gld_series})
            if sym == "SLV":
                return pd.DataFrame({"close": slv_series})
            return pd.DataFrame({"close": pd.Series(100.0, index=idx)})

        service.get_bars.side_effect = mock_bars

        now = datetime.now(timezone.utc)
        calendar = [
            {
                "title": "Federal Funds Rate",
                "impact": "High",
                "when_utc": (now - timedelta(minutes=15)).isoformat(),
                "forecast": "4.00%",
                "previous": "3.75%",
                "actual": "4.00%",
                "status": "released",
                "released": True,
            }
        ]

        context = fetch_metals_macro_context(service, "GLD", calendar=calendar)
        self.assertEqual(context["macro_risk_level"], "post_release_catalyst")
        self.assertIsNotNone(context.get("active_catalyst"))
        self.assertEqual(context["active_catalyst"]["action"], "hike")
        # In Sept 17 dynamic: yields falling -> absorbed rebound
        self.assertTrue(context["active_catalyst"].get("absorbed_rebound"))
        self.assertEqual(context["active_catalyst"].get("market_reaction"), "hike_absorbed_yields_falling")
        self.assertGreater(context["factor_scores"]["rates"], 0.0)
        self.assertEqual(context["yield_trend"], "falling_yields")
        self.assertIn("oil_score", context)
        self.assertIn("three_confirmation", context)
        self.assertIn("gold_dollar_divergence", context)

    def test_compute_historical_macro_series_has_enhanced_columns(self):
        """Historical macro series must include oil, divergence, and 3-confirmation columns for backtest."""
        idx = pd.date_range("2024-01-01", periods=50, freq="B", tz="UTC")
        bars = pd.DataFrame(
            {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000},
            index=idx,
        )
        macro_bars = {
            "GLD": bars,
            "TLT": bars,
            "UUP": bars,
            "USO": pd.DataFrame({"close": pd.Series(70.0, index=idx)}, index=idx),
        }
        df = compute_historical_macro_series(bars, symbol="GLD", macro_bars=macro_bars)
        self.assertIn("oil_score", df.columns)
        self.assertIn("oil_trend", df.columns)
        self.assertIn("gold_dollar_divergence", df.columns)
        self.assertIn("confirmations_count", df.columns)
        self.assertIn("three_confirmation_state", df.columns)

    def test_historical_divergence_requires_current_gold_strength(self):
        """A recently falling GLD cannot be flagged as bullish decoupling solely from its 200-day regime."""
        idx = pd.date_range("2024-01-01", periods=250, freq="B", tz="UTC")
        gold = pd.Series([100.0 + i * 0.5 for i in range(250)], index=idx)
        gold.iloc[-5:] = [221.0, 220.0, 219.0, 218.0, 217.0]
        bars = pd.DataFrame({"close": gold}, index=idx)
        uup = pd.Series([28.0 + i * 0.03 for i in range(250)], index=idx)

        macro = compute_historical_macro_series(
            bars,
            symbol="GLD",
            macro_bars={
                "GLD": bars,
                "UUP": pd.DataFrame({"close": uup}, index=idx),
            },
        )
        self.assertEqual(macro.iloc[-1]["trend_regime"], "bullish_above_sma200")
        self.assertEqual(macro.iloc[-1]["dollar_trend"], "rising")
        self.assertFalse(macro.iloc[-1]["gold_dollar_divergence"])

    def test_ai_backtest_divergence_blocks_short_and_three_aligned_boosts_long(self):
        """Strategy Dual-Sync: ai_backtest evaluate_ai_signal must respect divergence and 3-confirmation."""
        from bot.ai_backtest import AiBacktestParams, evaluate_ai_signal
        from bot.strategy import Signal

        params = AiBacktestParams(preset="gold_silver_macro")
        row = pd.Series(
            {
                "open": 200.0,
                "high": 202.0,
                "low": 199.5,
                "close": 201.0,
                "volume": 10000,
                "rsi14": 50.0,
                "adx14": 25.0,
                "sma10": 200.0,
                "sma20": 198.0,
                "sma50": 195.0,
                "sma200": 180.0,
                "dist_sma50_atr": 1.0,
                "macd_hist": 0.5,
                "trend_regime": "bullish_above_sma200",
                "macro_composite_score": 1.2,
                "gold_dollar_divergence": True,
                "three_confirmation_state": "all_three_aligned",
                "confirmations_count": 3,
                "oil_trend": "cooling_inflation",
                "yield_trend": "falling_yields",
                "dollar_trend": "rising",
                "dollar_mixed": False,
            }
        )

        # 1. Long signal receives confirmation boost
        sig_long, conf_long, thesis_long = evaluate_ai_signal(
            row, None, params, symbol="GLD", allow_short=True
        )
        self.assertEqual(sig_long, Signal.BUY)
        self.assertIn("All 3 Macro Confirmations Aligned", thesis_long)

        # 2. Short attempt during divergence is strictly blocked
        bear_row = row.copy()
        bear_row["close"] = 175.0
        bear_row["sma200"] = 190.0
        bear_row["trend_regime"] = "bearish_below_sma200"
        bear_row["macro_composite_score"] = -1.5
        bear_row["yield_trend"] = "rising_yields"
        bear_row["gold_dollar_divergence"] = True
        sig_short, conf_short, thesis_short = evaluate_ai_signal(
            bear_row, None, params, symbol="GLD", allow_short=True
        )
        self.assertEqual(sig_short, Signal.HOLD)
        self.assertIn("Bullish Decoupling Divergence", thesis_short)

        # 3. Inverse ETF during divergence is also blocked
        sig_inv, _, thesis_inv = evaluate_ai_signal(
            row, None, params, symbol="GLL", allow_short=True
        )
        self.assertEqual(sig_inv, Signal.HOLD)
        self.assertIn("Bullish Decoupling Divergence", thesis_inv)


if __name__ == "__main__":
    unittest.main()
