"""Tests for AlgoPaca standalone web trading desk and API."""

from __future__ import annotations

import unittest
from starlette.testclient import TestClient

from bot.webapp import app


class DeskWebappTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, follow_redirects=False)

    def test_root_redirects_to_auto_trade(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/auto-trade")

    def test_desk_pages_serve_successfully(self):
        pages = [
            "/auto-trade",
            "/backtest",
            "/backtest/history",
            "/backtest/compare",
            "/manual-order",
            "/positions",
            "/orders",
            "/history",
            "/configuration",
        ]
        for path in pages:
            with self.subTest(path=path):
                res = self.client.get(path)
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
                res = self.client.get(asset)
                self.assertEqual(res.status_code, 200)

    def test_api_status(self):
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("settings", data)
        self.assertIn("alpaca_key_status", data)
        self.assertIn("ai_key_status", data)
        self.assertNotIn("saas", data)
        self.assertIn("options_enabled", data["settings"])
        self.assertTrue(data["settings"]["options_enabled"])

    def test_api_settings_update(self):
        res = self.client.post("/api/settings", json={"sma_preset": "custom", "fast_sma": 12, "slow_sma": 26})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["settings"]["fast_sma"], 12)
        self.assertEqual(data["settings"]["slow_sma"], 26)

    def test_api_lang(self):
        res = self.client.post("/api/lang", json={"lang": "en"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["lang"], "en")

    def test_api_history_lessons(self):
        res = self.client.get("/api/history/lessons")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("lessons", data)

    def test_api_loop_state(self):
        res = self.client.get("/api/loop/state")
        self.assertEqual(res.status_code, 200)
        self.assertIn("loop_running", res.json())

    def test_removed_pages_return_404(self):
        removed = [
            "/admin",
            "/pricing",
            "/login",
            "/signup",
            "/forgot",
            "/reset",
            "/legal",
            "/team",
            "/account",
        ]
        for path in removed:
            with self.subTest(path=path):
                res = self.client.get(path)
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
                res = self.client.get(path)
                self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
