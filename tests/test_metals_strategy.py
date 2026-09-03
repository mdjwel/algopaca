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
    calculate_gsr,
    classify_bias,
    clip_unit,
    combine_macro_score,
    fetch_metals_macro_context,
    get_metal_category,
    is_precious_metal,
    momentum,
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
        # Wider stop and a shorter R target: gridding the exits on GLD showed
        # returns improving as the stop widened and decaying past a ~3R target.
        self.assertEqual(preset.atr_stop_mult, 2.2)
        self.assertEqual(preset.take_profit_r, 3.0)
        self.assertEqual(preset.trail_after_r, 1.2)
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
        self.assertEqual(bp["choices"]["symbols"], "GLD, SLV, GDX, UGL, GLL, DUST")
        self.assertEqual(bp["choices"]["ai_take_profit_r"], 3.0)
        self.assertEqual(bp["choices"]["ai_atr_stop_mult"], 2.2)
        self.assertEqual(bp["choices"]["ai_trail_after_r"], 1.2)
        # The factors are daily; running the blueprint intraday only multiplied
        # decisions without adding signal.
        self.assertEqual(bp["choices"]["bar_timeframe"], "1Day")

    def test_day_trading_preset(self):
        preset = get_day_preset("ai_metals_breakout")
        self.assertEqual(preset.id, "ai_metals_breakout")
        self.assertTrue(preset.use_ai_confirm)
        self.assertEqual(preset.side, "long_short")
        self.assertEqual(preset.profit_target_r, 2.8)
        self.assertEqual(preset.stop_atr_mult, 1.3)
        self.assertEqual(preset.open_buffer_mins, 12)

        all_day_presets = [p["id"] for p in list_day_presets()]
        self.assertIn("ai_metals_breakout", all_day_presets)

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


if __name__ == "__main__":
    unittest.main()
