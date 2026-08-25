"""OCC parsing, vertical selection, and options overlay behavior."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from bot import options_overlay as overlay
from bot.options_chain import (
    is_occ_symbol,
    normalize_options_style,
    occ_root,
    option_label,
    parse_occ,
    pick_expiration,
    pick_vertical,
)
from bot.options_overlay import (
    apply_options_overlay,
    apply_options_overlays,
    apply_pair_options_overlay,
    desired_overlay_side,
)


def _occ(root: str, exp: date, cp: str, strike: float) -> str:
    return f"{root}{exp.strftime('%y%m%d')}{cp}{int(round(strike * 1000)):08d}"


def _contract(root: str, exp: date, cp: str, strike: float, oi: int = 50) -> dict:
    occ = _occ(root, exp, cp, strike)
    return {
        "symbol": occ,
        "root": root,
        "type": "call" if cp == "C" else "put",
        "strike": float(strike),
        "expiration": exp.isoformat(),
        "open_interest": oi,
    }


class FakeService:
    def __init__(self) -> None:
        self.session = "regular"
        self.is_open = True
        self.equity = 100_000.0
        self.spot = 150.0
        self.spots: dict[str, float] = {}
        self.chain: list[dict] = []
        self.positions: list[dict] = []
        self.quotes: dict[str, float] = {}
        self.default_mid: float | None = 2.0
        self.submitted: list[tuple] = []
        self.closed: list[str] = []
        self.session_calls = 0
        self.position_list_calls = 0

    def market_session(self) -> dict:
        self.session_calls += 1
        return {"session": self.session, "is_open": self.is_open}

    def account_summary(self) -> dict:
        return {"equity": self.equity}

    def get_mark_price(self, symbol: str) -> dict:
        price = self.spots.get(symbol, self.spot)
        return {"price": price, "session": self.session, "is_open": self.is_open}

    def get_position_qty(self, symbol: str) -> float:
        for pos in self.positions:
            if pos.get("symbol") == symbol and not pos.get("is_option"):
                return float(pos.get("signed_qty") or pos.get("qty") or 0)
        return 0.0

    def get_all_positions(self) -> list[dict]:
        self.position_list_calls += 1
        return list(self.positions)

    def list_option_contracts(self, underlying, **kwargs) -> list[dict]:
        return list(self.chain)

    def option_quote_mid(self, occ: str) -> float | None:
        if occ in self.quotes:
            return self.quotes[occ]
        return self.default_mid

    def option_positions_for_underlying(self, underlying: str) -> list[dict]:
        root = occ_root(underlying)
        return [
            p
            for p in self.positions
            if occ_root(p.get("option_root") or "") == root
        ]

    def submit_option_spread(self, long_symbol: str, short_symbol: str, qty: int = 1):
        self.submitted.append(("spread", long_symbol, short_symbol, qty))
        return SimpleNamespace(id="spread-1")

    def submit_option_order(self, symbol: str, qty: int, side: str):
        self.submitted.append(("single", symbol, qty, side))
        return SimpleNamespace(id="opt-1")

    def close_position(self, symbol: str):
        self.closed.append(symbol)
        self.positions = [p for p in self.positions if p.get("symbol") != symbol]
        return {"order_id": "close-1", "status": "submitted"}


class OptionsChainTests(unittest.TestCase):
    def test_parse_occ(self):
        parsed = parse_occ("AAPL250117C00150000")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["root"], "AAPL")
        self.assertEqual(parsed["type"], "call")
        self.assertEqual(parsed["strike"], 150.0)
        self.assertEqual(parsed["expiration"], date(2025, 1, 17))
        self.assertTrue(is_occ_symbol("AAPL250117P00142500"))
        self.assertFalse(is_occ_symbol("AAPL"))
        self.assertEqual(occ_root("BRK.B"), "BRKB")
        self.assertIn("AAPL", option_label("AAPL250117C00150000"))

    def test_normalize_style(self):
        self.assertEqual(normalize_options_style("debit"), "vertical")
        self.assertEqual(normalize_options_style("covered"), "hedge")
        self.assertEqual(normalize_options_style("nope"), "vertical")

    def test_pick_vertical_call_spread(self):
        exp = date(2026, 10, 16)
        chain = [
            _contract("AAPL", exp, "C", 145),
            _contract("AAPL", exp, "C", 150),
            _contract("AAPL", exp, "C", 157.5),
            _contract("AAPL", exp, "C", 165),
        ]
        pair = pick_vertical(chain, 150.0, option_type="call", otm_pct=5.0)
        self.assertIsNotNone(pair)
        assert pair is not None
        long_leg, short_leg = pair
        self.assertEqual(long_leg["strike"], 150.0)
        self.assertGreater(short_leg["strike"], long_leg["strike"])

    def test_pick_expiration_window(self):
        today = date(2026, 8, 25)
        dates = [
            today + timedelta(days=7),
            today + timedelta(days=30),
            today + timedelta(days=90),
        ]
        picked = pick_expiration(dates, min_dte=21, max_dte=45, today=today)
        self.assertEqual(picked, today + timedelta(days=30))


class OverlaySideTests(unittest.TestCase):
    def test_intent_and_qty(self):
        self.assertEqual(desired_overlay_side({"intent": "open_long"}), "long")
        self.assertEqual(desired_overlay_side({"intent": "open_short"}), "short")
        self.assertEqual(desired_overlay_side({"intent": "close_long"}), "flat")
        self.assertEqual(desired_overlay_side({"intent": "cover"}), "flat")
        self.assertEqual(desired_overlay_side({"position": 10}), "long")
        self.assertEqual(desired_overlay_side({"position": -4}), "short")
        self.assertEqual(desired_overlay_side({"position": 0, "signal": "hold"}), "flat")


class OverlayApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        overlay.PAPER_STATE_PATH = Path(self.tmp.name) / "overlay.json"
        self.service = FakeService()
        exp = date.today() + timedelta(days=30)
        self.exp = exp
        self.service.chain = [
            _contract("AAPL", exp, "C", 145),
            _contract("AAPL", exp, "C", 150),
            _contract("AAPL", exp, "C", 157.5),
            _contract("AAPL", exp, "P", 150),
            _contract("AAPL", exp, "P", 142.5),
        ]
        self.config = SimpleNamespace(
            options_enabled=True,
            options_style="vertical",
            options_dte_min=21,
            options_dte_max=45,
            options_otm_pct=5.0,
            options_max_contracts=1,
            options_max_premium_pct=5.0,
            paper=True,
        )

    def test_disabled_skips(self):
        self.config.options_enabled = False
        payload = {"symbol": "AAPL", "intent": "open_long", "price": 150, "session": "regular"}
        apply_options_overlay(self.config, self.service, payload)
        self.assertEqual(payload["options"]["skipped"], "disabled")
        self.assertEqual(self.service.submitted, [])

    def test_open_long_submits_bull_call(self):
        payload = {
            "symbol": "AAPL",
            "intent": "open_long",
            "price": 150.0,
            "session": "regular",
            "position": 0,
            "reason": "sma buy",
        }
        apply_options_overlay(self.config, self.service, payload)
        self.assertEqual(payload["options"]["action"], "open")
        self.assertEqual(self.service.submitted[0][0], "spread")
        long_occ, short_occ = self.service.submitted[0][1], self.service.submitted[0][2]
        self.assertTrue(long_occ.endswith("C00150000") or "C" in long_occ)
        self.assertNotEqual(long_occ, short_occ)
        self.assertIn("options:", payload["reason"])

    def test_flat_closes_existing(self):
        occ = _occ("AAPL", self.exp, "C", 150)
        self.service.positions = [
            {
                "symbol": occ,
                "option_root": "AAPL",
                "is_option": True,
                "qty": 1,
                "signed_qty": 1,
            }
        ]
        payload = {
            "symbol": "AAPL",
            "intent": "close_long",
            "price": 150.0,
            "session": "regular",
            "position": 0,
            "reason": "sma sell",
        }
        apply_options_overlay(self.config, self.service, payload)
        self.assertEqual(self.service.closed, [occ])
        self.assertEqual(payload["options"]["action"], "close")
        self.assertEqual(payload["actions"][0]["order_id"], "close-1")
        self.assertIn("options:", payload["reason"])

    def test_hold_does_not_duplicate(self):
        payload = {
            "symbol": "AAPL",
            "intent": "open_long",
            "price": 150.0,
            "session": "regular",
            "position": 5,
            "reason": "buy",
        }
        apply_options_overlay(self.config, self.service, payload)
        self.assertEqual(len(self.service.submitted), 1)
        self.service.positions = [
            {
                "symbol": self.service.submitted[0][1],
                "option_root": "AAPL",
                "is_option": True,
                "qty": 1,
            }
        ]
        payload2 = {
            "symbol": "AAPL",
            "position": 5,
            "price": 150.0,
            "session": "regular",
            "signal": "hold",
            "reason": "holding",
        }
        apply_options_overlay(self.config, self.service, payload2)
        self.assertEqual(payload2["options"]["action"], "hold")
        self.assertEqual(len(self.service.submitted), 1)
        self.assertEqual(payload2["reason"], "holding")

    def test_afterhours_skips(self):
        payload = {
            "symbol": "AAPL",
            "intent": "open_long",
            "price": 150.0,
            "session": "afterhours",
            "is_open": False,
            "reason": "sma buy",
        }
        apply_options_overlay(self.config, self.service, payload)
        self.assertEqual(payload["options"]["skipped"], "market closed")
        self.assertEqual(self.service.submitted, [])
        self.assertEqual(payload["reason"], "sma buy")

    def test_flat_without_overlay_keeps_reason(self):
        payload = {
            "symbol": "AAPL",
            "intent": "close_long",
            "price": 150.0,
            "session": "regular",
            "position": 0,
            "reason": "sma hold",
        }
        apply_options_overlay(self.config, self.service, payload)
        self.assertEqual(payload["options"]["action"], "flat")
        self.assertEqual(payload["reason"], "sma hold")
        self.assertEqual(self.service.closed, [])

    def test_existing_without_state_holds(self):
        occ = _occ("AAPL", self.exp, "C", 150)
        self.service.positions = [
            {
                "symbol": occ,
                "option_root": "AAPL",
                "is_option": True,
                "qty": 1,
                "signed_qty": 1,
            }
        ]
        payload = {
            "symbol": "AAPL",
            "intent": "open_long",
            "price": 150.0,
            "session": "regular",
            "position": 5,
            "reason": "sma buy",
        }
        apply_options_overlay(self.config, self.service, payload)
        self.assertEqual(payload["options"]["action"], "hold")
        self.assertEqual(self.service.submitted, [])
        self.assertEqual(self.service.closed, [])
        self.assertEqual(payload["reason"], "sma buy")

    def test_premium_skip_without_quote(self):
        self.service.default_mid = None
        payload = {
            "symbol": "AAPL",
            "intent": "open_long",
            "price": 150.0,
            "session": "regular",
            "reason": "sma buy",
        }
        apply_options_overlay(self.config, self.service, payload)
        self.assertEqual(payload["options"]["skipped"], "no option quote")
        self.assertEqual(self.service.submitted, [])
        self.assertIn("no option quote", payload["reason"])

    def test_error_row_skips_overlay(self):
        payload = {
            "symbol": "AAPL",
            "error": "bars failed",
            "reason": "error: bars failed",
        }
        apply_options_overlay(self.config, self.service, payload)
        self.assertNotIn("options", payload)
        self.assertEqual(self.service.submitted, [])

    def test_pair_uses_per_symbol_spot(self):
        exp = self.exp
        self.service.spots = {"QLD": 50.0, "QURL": 200.0}
        self.service.spot = 50.0
        self.service.chain = [
            _contract("QURL", exp, "C", 190),
            _contract("QURL", exp, "C", 200),
            _contract("QURL", exp, "C", 210),
            _contract("QURL", exp, "P", 200),
            _contract("QURL", exp, "P", 190),
        ]
        self.service.positions = [
            {"symbol": "QURL", "qty": 10, "signed_qty": 10, "is_option": False},
        ]
        primary = {
            "symbol": "QURL",
            "long_symbol": "QLD",
            "short_symbol": "QURL",
            "price": 50.0,
            "session": "regular",
            "is_open": True,
            "reason": "pair hold QURL",
        }
        apply_pair_options_overlay(self.config, self.service, primary)
        self.assertEqual(self.service.submitted[0][0], "spread")
        long_occ = self.service.submitted[0][1]
        self.assertIn("C00200000", long_occ)

    def test_batch_overlay_lists_positions_once(self):
        rows = [
            {
                "symbol": "AAPL",
                "intent": "open_long",
                "price": 150.0,
                "session": "regular",
                "is_open": True,
                "reason": "buy",
            },
            {
                "symbol": "MSFT",
                "intent": "open_long",
                "price": 150.0,
                "session": "regular",
                "is_open": True,
                "reason": "buy",
            },
        ]
        apply_options_overlays(self.config, self.service, rows)
        self.assertEqual(self.service.session_calls, 1)
        self.assertEqual(self.service.position_list_calls, 1)
        self.assertEqual(len(self.service.submitted), 2)


if __name__ == "__main__":
    unittest.main()
