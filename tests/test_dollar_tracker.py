"""Unit tests for dedicated real-time US Dollar Index tracker (bot/dollar_tracker.py)."""

import unittest
from unittest.mock import MagicMock
import numpy as np
import pandas as pd

from bot.dollar_tracker import (
    DEFAULT_DOLLAR_SYMBOL,
    DOLLAR_Z_SCALE,
    DOLLAR_Z_WINDOW,
    blend_realtime_dollar_movement,
    classify_dollar_trend,
    clip_unit,
    compute_dollar_series,
    evaluate_dollar_action,
    fetch_live_dollar_snapshot,
    score_dollar,
    zscore,
)


class TestDollarTracker(unittest.TestCase):
    def test_clip_unit(self):
        self.assertEqual(clip_unit(0.5), 0.5)
        self.assertEqual(clip_unit(2.5), 1.0)
        self.assertEqual(clip_unit(-3.0), -1.0)
        self.assertEqual(clip_unit("invalid"), 0.0)

    def test_zscore(self):
        self.assertIsNone(zscore(None, 20))
        self.assertIsNone(zscore(pd.Series([1.0, 2.0]), 20))

        s = pd.Series([10.0, 10.0, 10.0, 10.0, 10.0])
        self.assertIsNone(zscore(s, 5))  # zero std

        s_vary = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
        z = zscore(s_vary, 5)
        self.assertIsNotNone(z)
        self.assertGreater(z, 1.0)

    def test_score_dollar(self):
        self.assertIsNone(score_dollar(None))
        # Cheap/falling dollar (negative z) -> positive score (bullish for metals)
        self.assertAlmostEqual(score_dollar(-1.5), 1.0)
        # Strong/rising dollar (positive z) -> negative score (bearish for metals)
        self.assertAlmostEqual(score_dollar(1.5), -1.0)
        self.assertAlmostEqual(score_dollar(0.0), 0.0)

    def test_blend_realtime_dollar_movement(self):
        # Drop in intraday dollar lifts metals
        score_drop = blend_realtime_dollar_movement(0.0, -0.25)
        self.assertGreaterEqual(score_drop, 0.45)

        # Rally in intraday dollar suppresses metals
        score_rally = blend_realtime_dollar_movement(0.0, 0.25)
        self.assertLessEqual(score_rally, -0.45)

        # If base_score is None, calculate directly from change_pct
        score_none = blend_realtime_dollar_movement(None, -0.30)
        self.assertAlmostEqual(score_none, 0.6)

    def test_classify_dollar_trend(self):
        trend, is_mixed = classify_dollar_trend(None)
        self.assertEqual(trend, "unknown")
        self.assertTrue(is_mixed)

        trend, is_mixed = classify_dollar_trend(0.40)
        self.assertEqual(trend, "falling")
        self.assertFalse(is_mixed)

        trend, is_mixed = classify_dollar_trend(-0.40)
        self.assertEqual(trend, "rising")
        self.assertFalse(is_mixed)

        trend, is_mixed = classify_dollar_trend(0.10)
        self.assertEqual(trend, "neutral")
        self.assertTrue(is_mixed)

    def test_evaluate_dollar_action(self):
        # Bullish setup (falling dollar)
        sig, act, rev, reason = evaluate_dollar_action(0.45, "falling", False, track_dollar_only=True)
        self.assertEqual(sig, "bullish")
        self.assertEqual(act, "buy")
        self.assertTrue(rev)
        self.assertIn("falling / weakening", reason or "")

        # Bearish setup (rising dollar)
        sig, act, rev, reason = evaluate_dollar_action(-0.45, "rising", False, track_dollar_only=True)
        self.assertEqual(sig, "bearish")
        self.assertEqual(act, "sell")
        self.assertFalse(rev)

    def test_compute_dollar_series(self):
        empty = compute_dollar_series(pd.Series([], dtype=float))
        self.assertTrue(empty.empty)

        prices = pd.Series(np.linspace(25.0, 30.0, 30))
        series = compute_dollar_series(prices, window=15)
        self.assertEqual(len(series), 30)
        # Upward price trend in dollar should produce negative scores for metals
        self.assertLess(series.iloc[-1], 0.0)

    def test_fetch_live_dollar_snapshot(self):
        service = MagicMock()
        service.get_mark_price.return_value = {
            "symbol": "UUP",
            "price": 28.30,
            "asof": "2026-09-17T14:00:00Z",
            "source": "nasdaq_live",
            "bid": 28.29,
            "ask": 28.31,
        }

        # Daily bars with prev close at 28.50 (so change is -0.7%)
        closes = [28.0] * 50 + [28.50, 28.50]
        service.get_bars.return_value = pd.DataFrame({"close": closes})

        snap = fetch_live_dollar_snapshot(service, "UUP", track_dollar_only=True)
        self.assertEqual(snap["symbol"], "UUP")
        self.assertEqual(snap["price"], 28.30)
        self.assertEqual(snap["prev_close"], 28.50)
        self.assertAlmostEqual(snap["change_pct"], -0.702, places=2)
        self.assertEqual(snap["dollar_trend"], "falling")
        self.assertEqual(snap["dollar_signal"], "bullish")
        self.assertEqual(snap["dollar_action"], "buy")
        self.assertTrue(snap["short_reversal_to_long"])
        self.assertGreater(snap["macro_composite_score"], 0.0)


if __name__ == "__main__":
    unittest.main()
