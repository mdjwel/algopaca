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


if __name__ == "__main__":
    unittest.main()
