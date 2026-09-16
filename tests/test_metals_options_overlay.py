"""Unit tests for precious metals options overlay adaptations."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from bot import options_overlay as overlay
from bot.options_overlay import apply_options_overlay
from tests.test_options import FakeService, _contract


class TestMetalsOptionsOverlay(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        overlay.PAPER_STATE_PATH = Path(self.tmp.name) / "overlay.json"
        self.service = FakeService()
        self.service.spot = 250.0
        self.service.spots["GLD"] = 250.0
        self.service.spots["SLV"] = 30.0
        self.service.spots["GDXU"] = 40.0
        self.service.spots["GLL"] = 25.0
        self.service.spots["GDXD"] = 15.0

        today = date.today()
        # Create expirations at 10d, 25d, 40d, and 70d
        exp_10 = today + timedelta(days=10)
        exp_25 = today + timedelta(days=25)
        exp_40 = today + timedelta(days=40)
        exp_70 = today + timedelta(days=70)

        # Build GLD contracts for exp_40 (in the 30-60 DTE window)
        chain: list[dict] = []
        for strike in [240.0, 245.0, 250.0, 255.0, 258.0, 260.0, 265.0]:
            chain.append(_contract("GLD", exp_40, "C", strike))
            chain.append(_contract("GLD", exp_40, "P", strike))
            chain.append(_contract("GLD", exp_25, "C", strike))
            chain.append(_contract("GLD", exp_25, "P", strike))
            chain.append(_contract("SLV", exp_40, "C", strike / 8.0))
            chain.append(_contract("SLV", exp_40, "P", strike / 8.0))
        self.service.chain = chain

    def test_gld_adaptive_dte_and_otm(self) -> None:
        """GLD with default config (21-45 DTE, 5% OTM) automatically tunes to 30-60 DTE and 3.5% OTM."""
        config = SimpleNamespace(
            options_enabled=True,
            options_style="vertical",
            options_dte_min=21,
            options_dte_max=45,
            options_otm_pct=5.0,
            options_max_contracts=1,
            options_max_premium_pct=1.0,
            paper=True,
            ai_preset="gold_silver_macro",
        )
        payload = {
            "symbol": "GLD",
            "price": 250.0,
            "session": "regular",
            "is_open": True,
            "intent": "open_long",
        }

        res = apply_options_overlay(config, self.service, payload)
        opt = res.get("options") or {}
        self.assertEqual(opt.get("action"), "open")
        self.assertEqual(opt.get("style"), "vertical")
        self.assertEqual(opt.get("desired"), "long")

        # Verify that exp_40 was chosen (because min_dte was adjusted to 30, so exp_25 was skipped)
        legs = opt.get("state", {}).get("legs", [])
        self.assertEqual(len(legs), 2)
        # Spot is 250. Long strike should be ATM (250).
        # Short target = 250 * (1 + 0.035) = 258.75 -> nearest contract is 258.0
        self.assertEqual(legs[0]["strike"], 250.0)
        self.assertEqual(legs[1]["strike"], 258.0)

    def test_leveraged_and_inverse_metals_etf_skipped(self) -> None:
        """GDXU, GLL, GDXD, DUST, UGL must be skipped for options overlay to prevent illiquid trades."""
        config = SimpleNamespace(
            options_enabled=True,
            options_style="vertical",
            options_dte_min=30,
            options_dte_max=60,
            options_otm_pct=3.5,
            options_max_contracts=1,
            options_max_premium_pct=1.0,
            paper=True,
            ai_preset="gold_silver_macro",
        )

        for sym in ["GDXU", "GLL", "GDXD", "DUST", "UGL"]:
            payload = {
                "symbol": sym,
                "price": 25.0,
                "session": "regular",
                "is_open": True,
                "intent": "open_long",
            }
            res = apply_options_overlay(config, self.service, payload)
            opt = res.get("options") or {}
            self.assertEqual(opt.get("action"), "skip")
            self.assertIn("leveraged/inverse metal ETF", opt.get("skipped", ""))

    def test_imminent_macro_event_freezes_options_entry(self) -> None:
        """When macro_risk_level is imminent_release, options entry is skipped to avoid spread spikes and IV crush."""
        config = SimpleNamespace(
            options_enabled=True,
            options_style="vertical",
            options_dte_min=30,
            options_dte_max=60,
            options_otm_pct=3.5,
            options_max_contracts=1,
            options_max_premium_pct=1.0,
            paper=True,
            ai_preset="gold_silver_macro",
        )
        payload = {
            "symbol": "GLD",
            "price": 250.0,
            "session": "regular",
            "is_open": True,
            "intent": "open_long",
            "precious_metals_intel": {
                "macro_risk_level": "imminent_release",
            },
        }

        res = apply_options_overlay(config, self.service, payload)
        opt = res.get("options") or {}
        self.assertEqual(opt.get("action"), "skip")
        self.assertIn("frozen 45m before high-impact macro release", opt.get("skipped", ""))

    def test_slv_allows_options_overlay(self) -> None:
        """Core silver ETF SLV is permitted to trade options overlay."""
        config = SimpleNamespace(
            options_enabled=True,
            options_style="vertical",
            options_dte_min=30,
            options_dte_max=60,
            options_otm_pct=5.0,
            options_max_contracts=1,
            options_max_premium_pct=1.0,
            paper=True,
            ai_preset="gold_silver_macro",
        )
        payload = {
            "symbol": "SLV",
            "price": 30.0,
            "session": "regular",
            "is_open": True,
            "intent": "open_long",
        }

        res = apply_options_overlay(config, self.service, payload)
        opt = res.get("options") or {}
        self.assertEqual(opt.get("action"), "open")
        self.assertEqual(opt.get("desired"), "long")


if __name__ == "__main__":
    unittest.main()
