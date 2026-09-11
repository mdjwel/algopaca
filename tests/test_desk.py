import tempfile
from pathlib import Path
import unittest
from pydantic import ValidationError
from starlette.testclient import TestClient

from bot.auth import AuthStore
from bot.config import MAX_ATR_STOP_MULT, MIN_ATR_STOP_MULT
from bot.webapp import ManualOrderIn, app


class DeskWebappTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp_dir.name) / "auth.db"
        cls.auth_store = AuthStore(db_path=cls.db_path)

        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        cls._orig_webapp_auth = webapp_module.AUTH_STORE
        cls._orig_web_state_auth = web_state_module.AUTH_STORE
        cls._orig_auth_store = auth_module.AUTH_STORE

        webapp_module.AUTH_STORE = cls.auth_store
        web_state_module.AUTH_STORE = cls.auth_store
        auth_module.AUTH_STORE = cls.auth_store

        cls.anon_client = TestClient(app, follow_redirects=False)
        cls.auth_client = TestClient(app, follow_redirects=False)
        # Register and login a dedicated test user for authenticated tests
        import uuid
        uid = uuid.uuid4().hex[:8]
        cls.auth_client.post(
            "/api/auth/signup",
            json={
                "username": f"desk_{uid}",
                "email": f"desk_{uid}@example.com",
                "password": "Password123!",
                "display_name": "Desk Tester",
            },
        )

    @classmethod
    def tearDownClass(cls):
        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        webapp_module.AUTH_STORE = cls._orig_webapp_auth
        web_state_module.AUTH_STORE = cls._orig_web_state_auth
        auth_module.AUTH_STORE = cls._orig_auth_store

        cls.tmp_dir.cleanup()

    def test_unauthenticated_root_redirects_to_login(self):
        res = self.anon_client.get("/")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/login")

    def test_authenticated_root_redirects_to_auto_trade(self):
        res = self.auth_client.get("/")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/auto-trade")

    def test_unauthenticated_desk_pages_redirect_to_login(self):
        pages = [
            "/auto-trade",
            "/backtest",
            "/backtest/history",
            "/backtest/compare",
            "/manual-order",
            "/advanced-order",
            "/positions",
            "/orders",
            "/history",
            "/api-keys",
            "/configuration",
        ]
        for path in pages:
            with self.subTest(path=path):
                res = self.anon_client.get(path)
                self.assertEqual(res.status_code, 302)
                self.assertTrue(res.headers["location"].startswith("/login"))

    def test_unauthenticated_apis_return_401(self):
        apis = [
            "/api/status",
            "/api/positions",
            "/api/orders",
            "/api/history/lessons",
            "/api/loop/state",
        ]
        for path in apis:
            with self.subTest(path=path):
                res = self.anon_client.get(path)
                self.assertEqual(res.status_code, 401)

    def test_desk_pages_serve_successfully(self):
        pages = [
            "/auto-trade",
            "/backtest",
            "/backtest/history",
            "/backtest/compare",
            "/manual-order",
            "/advanced-order",
            "/positions",
            "/orders",
            "/history",
            "/api-keys",
            "/configuration",
        ]
        for path in pages:
            with self.subTest(path=path):
                res = self.auth_client.get(path)
                self.assertEqual(res.status_code, 200)
                self.assertIn("text/html", res.headers.get("content-type", ""))

    def test_static_assets_serve_successfully(self):
        assets = [
            "/static/css/common.css",
            "/static/js/common.js",
            "/static/lang/en.json",
            "/static/lang/es.json",
        ]
        for asset in assets:
            with self.subTest(asset=asset):
                res = self.auth_client.get(asset)
                self.assertEqual(res.status_code, 200)

    def test_api_status(self):
        res = self.auth_client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("settings", data)
        self.assertIn("alpaca_key_status", data)
        self.assertIn("ai_key_status", data)
        self.assertNotIn("saas", data)
        self.assertIn("options_enabled", data["settings"])
        self.assertTrue(data["settings"]["options_enabled"])
        self.assertIn("risk_engine_enabled", data["settings"])
        self.assertIsInstance(data["settings"]["risk_engine_enabled"], bool)

    def test_api_settings_update(self):
        res = self.auth_client.post(
            "/api/settings",
            json={
                "sma_preset": "custom",
                "fast_sma": 12,
                "slow_sma": 26,
                "risk_engine_enabled": False,
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["settings"]["fast_sma"], 12)
        self.assertEqual(data["settings"]["slow_sma"], 26)
        self.assertFalse(data["settings"]["risk_engine_enabled"])

        # Reset back to True
        res_reset = self.auth_client.post(
            "/api/settings",
            json={
                "risk_engine_enabled": True,
            },
        )
        self.assertEqual(res_reset.status_code, 200)
        self.assertTrue(res_reset.json()["settings"]["risk_engine_enabled"])

    def test_api_settings_day_ai_confirmation_round_trip(self):
        """The Day Trading AI confirm knobs must survive POST /api/settings."""
        res = self.auth_client.post(
            "/api/settings",
            json={
                "strategy_mode": "day",
                "day_preset": "custom",
                "day_use_ai_confirm": True,
                "day_ai_min_confidence": 0.85,
                "bar_timeframe": "1Day",
            },
        )
        self.assertEqual(res.status_code, 200)
        settings = res.json()["settings"]
        self.assertTrue(settings["day_use_ai_confirm"])
        self.assertAlmostEqual(settings["day_ai_min_confidence"], 0.85)
        # Day Trading is intraday-only, so daily bars are coerced.
        self.assertEqual(settings["bar_timeframe"], "5Min")

        status = self.auth_client.get("/api/status").json()["settings"]
        self.assertTrue(status["day_use_ai_confirm"])
        self.assertAlmostEqual(status["day_ai_min_confidence"], 0.85)

        self.auth_client.post(
            "/api/settings", json={"strategy_mode": "sma", "bar_timeframe": "15Min"}
        )

    def test_api_lang(self):
        res = self.auth_client.post("/api/lang", json={"lang": "en"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["lang"], "en")

    def test_api_history_lessons(self):
        res = self.auth_client.get("/api/history/lessons")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("lessons", data)

    def test_api_loop_state(self):
        res = self.auth_client.get("/api/loop/state")
        self.assertEqual(res.status_code, 200)
        self.assertIn("loop_running", res.json())

    def test_login_and_signup_pages_return_200(self):
        for path in ["/login", "/signup"]:
            with self.subTest(path=path):
                res = self.anon_client.get(path)
                self.assertEqual(res.status_code, 200)
                self.assertIn("text/html", res.headers.get("content-type", ""))

    def test_removed_pages_return_404(self):
        removed = [
            "/pricing",
            "/forgot",
            "/reset",
            "/legal",
            "/team",
            "/account",
        ]
        for path in removed:
            with self.subTest(path=path):
                res = self.anon_client.get(path)
                self.assertEqual(res.status_code, 404)

    def test_removed_saas_api_routes_return_404(self):
        removed_api = [
            "/api/saas/session",
            "/api/saas/meta",
            "/api/saas/signup",
            "/api/saas/login",
            "/api/saas/workspace/switch",
        ]
        for path in removed_api:
            with self.subTest(path=path):
                res = self.anon_client.get(path)
                self.assertEqual(res.status_code, 404)


class ManualOrderValidationTestCase(unittest.TestCase):
    """The ATR multiple prices the stop, and risk sizing divides by it."""

    def _ticket(self, **overrides):
        payload = {"symbol": "AAPL", "side": "buy", "qty": 1}
        payload.update(overrides)
        return ManualOrderIn(**payload)

    def test_atr_mult_between_zero_and_floor_is_rejected(self):
        # ge=0.0 alone let these through, and a 0.01 ATR stop sizes a position
        # roughly 180x the one the 1.8 default would have bought.
        for value in (0.001, 0.01, 0.05, 0.099):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self._ticket(ai_atr_stop_mult=value)

    def test_atr_mult_floor_and_ceiling_are_accepted(self):
        for value in (MIN_ATR_STOP_MULT, 1.8, MAX_ATR_STOP_MULT):
            with self.subTest(value=value):
                self.assertEqual(
                    self._ticket(ai_atr_stop_mult=value).ai_atr_stop_mult, value
                )

    def test_atr_mult_zero_stays_valid(self):
        """0 is a setting, not a typo: it hands the stop to flat stop_loss_pct."""
        self.assertEqual(self._ticket(ai_atr_stop_mult=0).ai_atr_stop_mult, 0.0)

    def test_atr_mult_omitted_keeps_desk_default(self):
        self.assertIsNone(self._ticket().ai_atr_stop_mult)

    def test_atr_mult_above_ceiling_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._ticket(ai_atr_stop_mult=10.1)

    def test_bracket_price_fields_accepted(self):
        ticket = self._ticket(
            stop_loss_price=150.0,
            take_profit_price=180.0,
            take_profit_pct=10.0,
        )
        self.assertEqual(ticket.stop_loss_price, 150.0)
        self.assertEqual(ticket.take_profit_price, 180.0)
        self.assertEqual(ticket.take_profit_pct, 10.0)

    def test_place_manual_order_take_profit_price_preview(self):
        from unittest.mock import patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 100.0, "source": "test"}
            srv.get_position_qty.return_value = 0.0
            srv.account_summary.return_value = {"equity": 100000.0}
            srv.stop_price_for_entry.return_value = 95.0
            res = state.place_manual_order(
                symbol="AAPL",
                side="buy",
                order_type="market",
                qty=10.0,
                stop_loss_pct=5.0,
                take_profit_price=110.0,
                preview=True,
            )
            self.assertEqual(res["take_profit_price"], 110.0)
            self.assertEqual(res["stop_preview"], 95.0)
            self.assertEqual(res["take_profit_r"], 2.0)

    def test_place_manual_order_cover_preview(self):
        from unittest.mock import patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 150.0, "source": "test"}
            srv.get_position_qty.return_value = -20.0
            srv.account_summary.return_value = {"equity": 100000.0}
            res = state.place_manual_order(
                symbol="AAPL",
                side="cover",
                order_type="market",
                qty=12.0,
                preview=True,
            )
            self.assertEqual(res["side"], "cover")
            self.assertEqual(res["broker_side"], "buy")
            self.assertEqual(res["order_qty"], 12.0)
            self.assertEqual(res["position"], -20.0)
            self.assertEqual(res["price"], 150.0)

    def test_place_manual_order_cover_validation(self):
        from unittest.mock import patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 100.0, "source": "test"}
            srv.account_summary.return_value = {"equity": 100000.0}

            # 1. Covering when flat should fail
            srv.get_position_qty.return_value = 0.0
            with self.assertRaises(ValueError) as ctx:
                state.place_manual_order(
                    symbol="AAPL",
                    side="cover",
                    order_type="market",
                    qty=5.0,
                    preview=True,
                )
            self.assertIn("No short position", str(ctx.exception))

            # 2. Covering when long should fail
            srv.get_position_qty.return_value = 10.0
            with self.assertRaises(ValueError) as ctx:
                state.place_manual_order(
                    symbol="AAPL",
                    side="cover",
                    order_type="market",
                    qty=5.0,
                    preview=True,
                )
            self.assertIn("use Sell to close a long", str(ctx.exception))

            # 3. Buying when short should fail and suggest Cover
            srv.get_position_qty.return_value = -10.0
            with self.assertRaises(ValueError) as ctx:
                state.place_manual_order(
                    symbol="AAPL",
                    side="buy",
                    order_type="market",
                    qty=5.0,
                    preview=True,
                )
            self.assertIn("use Cover to buy those back", str(ctx.exception))

    def test_place_manual_order_cover_with_followon_reverse(self):
        from unittest.mock import patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 150.0, "source": "test"}
            srv.get_position_qty.return_value = -20.0
            srv.account_summary.return_value = {"equity": 100000.0}

            # 1. Full cover with reverse: should succeed and next_side should be "buy"
            res = state.place_manual_order(
                symbol="AAPL",
                side="cover",
                order_type="market",
                qty=20.0,
                followon={
                    "enabled": True,
                    "kind": "reverse",
                    "qty_mode": "match",
                    "order_type": "limit",
                    "limit_price": 145.0,
                },
                preview=True,
            )
            self.assertEqual(res["side"], "cover")
            self.assertIn("followon", res)
            self.assertEqual(res["followon"]["kind"], "reverse")
            self.assertEqual(res["followon"]["next_side"], "buy")
            self.assertEqual(res["followon"]["target_symbol"], "AAPL")
            self.assertEqual(res["followon"]["limit_price"], 145.0)

            # 2. Partial cover with reverse: should raise error requiring full close
            with self.assertRaises(ValueError) as ctx:
                state.place_manual_order(
                    symbol="AAPL",
                    side="cover",
                    order_type="market",
                    qty=10.0,
                    followon={
                        "enabled": True,
                        "kind": "reverse",
                        "qty_mode": "match",
                        "order_type": "limit",
                        "limit_price": 145.0,
                    },
                    preview=True,
                )
            self.assertIn("needs the whole position closed", str(ctx.exception))

    def test_place_manual_order_cover_with_followon_rotate(self):
        from unittest.mock import patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 150.0, "source": "test"}
            srv.get_position_qty.return_value = -20.0
            srv.account_summary.return_value = {"equity": 100000.0}

            res = state.place_manual_order(
                symbol="AAPL",
                side="cover",
                order_type="market",
                qty=10.0,
                followon={
                    "enabled": True,
                    "kind": "rotate",
                    "target_symbol": "MSFT",
                    "qty_mode": "custom",
                    "qty": 5.0,
                    "order_type": "market",
                },
                preview=True,
            )
            self.assertEqual(res["side"], "cover")
            self.assertIn("followon", res)
            self.assertEqual(res["followon"]["kind"], "rotate")
            self.assertEqual(res["followon"]["target_symbol"], "MSFT")
            self.assertEqual(res["followon"]["next_side"], "buy")
            self.assertEqual(res["followon"]["qty"], 5.0)

    def test_place_manual_order_short_custom_qty(self):
        from unittest.mock import patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 100.0, "source": "test"}
            srv.get_position_qty.return_value = 0.0
            srv.account_summary.return_value = {"equity": 100000.0}
            srv._client = None
            res = state.place_manual_order(
                symbol="AAPL",
                side="short",
                order_type="market",
                qty=25.0,
                preview=True,
            )
            self.assertEqual(res["side"], "short")
            self.assertEqual(res["broker_side"], "sell")
            self.assertEqual(res["order_qty"], 25.0)
            self.assertEqual(res["price"], 100.0)

    def test_place_manual_order_short_with_bracket(self):
        from unittest.mock import patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 100.0, "source": "test"}
            srv.get_position_qty.return_value = 0.0
            srv.account_summary.return_value = {"equity": 100000.0}
            srv.stop_price_for_entry.return_value = 105.0
            srv.market_session.return_value = {"session": "regular", "is_open": True}
            res = state.place_manual_order(
                symbol="AAPL",
                side="short",
                order_type="limit",
                limit_price=100.0,
                qty=15.0,
                stop_loss_price=105.0,
                take_profit_price=90.0,
                preview=True,
            )
            self.assertEqual(res["side"], "short")
            self.assertEqual(res["order_qty"], 15.0)
            self.assertEqual(res["stop_preview"], 105.0)
            self.assertEqual(res["take_profit_price"], 90.0)
            self.assertEqual(res["stop_distance"], 5.0)
            self.assertEqual(res["take_profit_r"], 2.0)
            self.assertEqual(res["ticket_risk"], 75.0)  # 15 shares * $5 risk

    def test_place_manual_order_short_notional_mode(self):
        from unittest.mock import patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 100.0, "source": "test"}
            srv.get_position_qty.return_value = 0.0
            srv.account_summary.return_value = {"equity": 100000.0}
            srv.market_session.return_value = {"session": "regular", "is_open": True}
            # $1,250 notional at $100/share -> 12.5 shares, floored to 12 whole shares for short
            res = state.place_manual_order(
                symbol="AAPL",
                side="short",
                order_type="market",
                size_mode="notional",
                notional=1250.0,
                preview=True,
            )
            self.assertEqual(res["side"], "short")
            self.assertEqual(res["order_qty"], 12.0)
            self.assertTrue(res["qty_whole_for_short"])

    def test_place_manual_order_short_fractional_rejected(self):
        from unittest.mock import patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 100.0, "source": "test"}
            srv.get_position_qty.return_value = 0.0
            srv.account_summary.return_value = {"equity": 100000.0}
            srv.market_session.return_value = {"session": "regular", "is_open": True}
            # $50 notional at $100/share -> 0.5 shares -> whole = 0 -> raises ValueError
            with self.assertRaises(ValueError) as ctx:
                state.place_manual_order(
                    symbol="AAPL",
                    side="short",
                    order_type="market",
                    size_mode="notional",
                    notional=50.0,
                    preview=True,
                )
            self.assertIn("does not short fractional shares", str(ctx.exception))

    def test_place_manual_order_short_stop_limit_cushion(self):
        from unittest.mock import patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 100.0, "source": "test"}
            srv.get_position_qty.return_value = 0.0
            srv.account_summary.return_value = {"equity": 100000.0}
            srv.stop_price_for_entry.return_value = 105.0
            srv.market_session.return_value = {"session": "regular", "is_open": True}
            # Cover limit must sit AT OR ABOVE stop (e.g. 106 >= 105)
            res = state.place_manual_order(
                symbol="AAPL",
                side="short",
                order_type="limit",
                limit_price=100.0,
                qty=10.0,
                stop_loss_price=105.0,
                stop_limit_price=106.0,
                preview=True,
            )
            self.assertEqual(res["stop_limit_preview"], 106.0)

            # Invalid: cover limit below stop (e.g. 104 < 105)
            with self.assertRaises(ValueError) as ctx:
                state.place_manual_order(
                    symbol="AAPL",
                    side="short",
                    order_type="limit",
                    limit_price=100.0,
                    qty=10.0,
                    stop_loss_price=105.0,
                    stop_limit_price=104.0,
                    preview=True,
                )
            self.assertIn("at or above the stop price", str(ctx.exception))

    def test_place_manual_order_short_live_oto_stop_reason(self):
        from unittest.mock import MagicMock, patch
        from bot.web_state import AppState
        state = AppState(user_id="test_user")
        state.settings.manual_orders_enabled = True
        with patch("bot.web_state.AlpacaService") as MockService:
            srv = MockService.return_value
            srv.get_mark_price.return_value = {"price": 100.0, "source": "test"}
            srv.get_position_qty.return_value = 0.0
            srv.account_summary.return_value = {"equity": 100000.0}
            srv.market_session.return_value = {"session": "regular", "is_open": True}
            mock_submitted = MagicMock()
            mock_submitted.id = "short_order_123"
            mock_submitted.client_order_id = "test_ticket"
            mock_submitted.status = "new"
            mock_submitted.filled_qty = "0"
            mock_submitted.filled_avg_price = None
            oto_stop = {
                "stop_price": 105.0,
                "pct": 5.0,
                "attached": "bracket",
                "side": "buy",
            }
            srv.submit_manual_order.return_value = (mock_submitted, oto_stop)
            srv.exit_leg_ids.return_value = {"stop_order_id": "stop_leg_1", "take_profit_order_id": "tp_leg_1"}

            res = state.place_manual_order(
                symbol="AAPL",
                side="short",
                order_type="limit",
                limit_price=100.0,
                qty=10.0,
                stop_loss_price=105.0,
                take_profit_price=90.0,
                preview=False,
            )
            self.assertEqual(res["side"], "short")
            self.assertIn("OTO stop @105.00 (+5.00%)", res["reason"])
            self.assertEqual(res["stop_loss"]["side"], "buy")


if __name__ == "__main__":
    unittest.main()

