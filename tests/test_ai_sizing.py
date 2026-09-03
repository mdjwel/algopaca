"""Sizing fallback when the model returns a decision without a usable qty."""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from bot.ai_providers import (
    AI_SIZED_MISSING_QTY_SCALE,
    DEFAULT_MISSING_QTY_SCALE,
    normalize_decision,
)


def _decide(raw, max_qty, **kwargs):
    return normalize_decision(
        raw, provider="openai", model="test-model", max_qty=max_qty, **kwargs
    )


class MissingQtyFallbackTestCase(unittest.TestCase):
    def test_desk_size_modes_keep_the_half_cap_fallback(self):
        """`max_qty` is the intended size there, so half of it stays the rule."""
        decision = _decide({"action": "buy", "confidence": 0.8}, 40)
        self.assertEqual(decision.qty, 20.0)

    def test_ai_size_mode_takes_a_starter_position(self):
        """A ceiling is not an intent — a skipped size must not buy half the book."""
        decision = _decide(
            {"action": "buy", "confidence": 0.8},
            40,
            qty_fallback_scale=AI_SIZED_MISSING_QTY_SCALE,
        )
        self.assertEqual(decision.qty, 10.0)

    def test_whole_share_floor_survives_the_smaller_scale(self):
        """Shorts and extended-hours orders cannot be fractional."""
        decision = _decide(
            {"action": "sell", "confidence": 0.9},
            2,
            qty_fallback_scale=AI_SIZED_MISSING_QTY_SCALE,
        )
        self.assertEqual(decision.qty, 1.0)

    def test_sub_share_cap_stays_fractional(self):
        decision = _decide(
            {"action": "buy", "confidence": 0.9},
            0.8,
            qty_fallback_scale=AI_SIZED_MISSING_QTY_SCALE,
        )
        self.assertAlmostEqual(decision.qty, 0.2)

    def test_a_supplied_qty_is_never_replaced(self):
        decision = _decide(
            {"action": "buy", "qty": 7, "confidence": 0.8},
            40,
            qty_fallback_scale=AI_SIZED_MISSING_QTY_SCALE,
        )
        self.assertEqual(decision.qty, 7.0)

    def test_hold_stays_flat(self):
        decision = _decide(
            {"action": "hold", "confidence": 0.8},
            40,
            qty_fallback_scale=AI_SIZED_MISSING_QTY_SCALE,
        )
        self.assertEqual(decision.qty, 0.0)

    def test_out_of_range_scale_is_clamped(self):
        self.assertEqual(_decide({"action": "buy"}, 40, qty_fallback_scale=9).qty, 40.0)
        self.assertEqual(_decide({"action": "buy"}, 40, qty_fallback_scale=-1).qty, 1.0)

    def test_ai_scale_is_smaller_than_the_desk_default(self):
        self.assertLess(AI_SIZED_MISSING_QTY_SCALE, DEFAULT_MISSING_QTY_SCALE)


class BrainPassesTheRightScaleTestCase(unittest.TestCase):
    """The scale is only useful if `AiBrain` picks it from the desk size mode."""

    def _brain(self, size_mode, raw):
        from bot.ai_brain import AiBrain
        from bot.config import Config

        config = Config.default(
            strategy_mode="ai",
            size_mode=size_mode,
            trade_qty=8,
            ai_min_confidence=0.5,
            # Risk sizing off so `max_qty` is the predictable desk/ceiling value.
            ai_risk_pct=0,
        )
        service = MagicMock()
        service.get_bars.return_value = pd.DataFrame()
        service.get_position_detail.return_value = {"qty": 0}
        service.account_summary.return_value = {
            "equity": 10_000,
            "buying_power": 10_000,
        }
        service.recent_activity.return_value = {}
        service.get_mark_price.return_value = {"price": 100.0}
        service.market_session.return_value = {"session": "open"}
        service.current_stop_price.return_value = None

        provider = MagicMock()
        provider.name = "openai"
        provider.model = "test-model"
        provider.complete_json.return_value = raw
        return AiBrain(config, service, provider)

    @patch("bot.ai_brain.fetch_economic_calendar", return_value=[])
    @patch("bot.ai_brain.fetch_earnings", return_value={})
    @patch("bot.ai_brain.fetch_news", return_value=[])
    @patch("bot.ai_brain.compute_technicals", return_value={"ok": True, "atr_14": 2.5})
    def test_ai_size_mode_uses_a_quarter_of_the_ceiling(self, *_mocks):
        # Ceiling with risk sizing off: 25% of $10,000 buying power at $100.
        brain = self._brain("ai", {"action": "buy", "confidence": 0.9})
        decision, _context = brain.decide("AAPL")
        self.assertEqual(decision.qty, 25.0 * AI_SIZED_MISSING_QTY_SCALE)

    @patch("bot.ai_brain.fetch_economic_calendar", return_value=[])
    @patch("bot.ai_brain.fetch_earnings", return_value={})
    @patch("bot.ai_brain.fetch_news", return_value=[])
    @patch("bot.ai_brain.compute_technicals", return_value={"ok": True, "atr_14": 2.5})
    def test_qty_size_mode_is_unchanged(self, *_mocks):
        brain = self._brain("qty", {"action": "buy", "confidence": 0.9})
        decision, _context = brain.decide("AAPL")
        # Desk qty 8 is the intent; the fallback halves it, then confidence
        # scaling applies as it always has outside AI size mode.
        self.assertLessEqual(decision.qty, 8 * DEFAULT_MISSING_QTY_SCALE)
        self.assertGreater(decision.qty, 0)


if __name__ == "__main__":
    unittest.main()
