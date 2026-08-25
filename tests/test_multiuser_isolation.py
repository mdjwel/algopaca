"""Tests verifying strict per-user portfolio, settings, and credential isolation in AlgoPaca."""

from __future__ import annotations

import unittest
import uuid
from starlette.testclient import TestClient

from bot.auth import AUTH_STORE
from bot.webapp import app


class MultiUserIsolationTestCase(unittest.TestCase):
    def setUp(self):
        self.anon_client = TestClient(app, follow_redirects=False)
        self.user1_client = TestClient(app, follow_redirects=False)
        self.user2_client = TestClient(app, follow_redirects=False)

        uid = uuid.uuid4().hex[:6]
        # Register User 1
        u1_res = self.user1_client.post(
            "/api/auth/signup",
            json={
                "username": f"trader_one_{uid}",
                "email": f"trader1_{uid}@example.com",
                "password": "Password123!",
                "display_name": "Trader One",
            },
        )
        self.assertEqual(u1_res.status_code, 200)
        self.u1_data = u1_res.json()["user"]

        # Register User 2
        u2_res = self.user2_client.post(
            "/api/auth/signup",
            json={
                "username": f"trader_two_{uid}",
                "email": f"trader2_{uid}@example.com",
                "password": "Password123!",
                "display_name": "Trader Two",
            },
        )
        self.assertEqual(u2_res.status_code, 200)
        self.u2_data = u2_res.json()["user"]

    def test_unauthenticated_requests_blocked(self):
        res = self.anon_client.get("/api/status")
        self.assertEqual(res.status_code, 401)

        res = self.anon_client.get("/auto-trade")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login", res.headers["location"])

    def test_separate_desk_settings_isolation(self):
        # User 1 sets symbol to TSLA and fast_sma to 15
        u1_set = self.user1_client.post(
            "/api/settings",
            json={
                "symbol": "TSLA",
                "symbols": "TSLA",
                "sma_preset": "custom",
                "fast_sma": 15,
                "slow_sma": 40,
                "trade_qty": 5.0,
            },
        )
        self.assertEqual(u1_set.status_code, 200)

        # User 2 sets symbol to NVDA and fast_sma to 8
        u2_set = self.user2_client.post(
            "/api/settings",
            json={
                "symbol": "NVDA",
                "symbols": "NVDA",
                "sma_preset": "custom",
                "fast_sma": 8,
                "slow_sma": 21,
                "trade_qty": 10.0,
            },
        )
        self.assertEqual(u2_set.status_code, 200)

        # Check User 1 status
        u1_status = self.user1_client.get("/api/status").json()
        self.assertEqual(u1_status["settings"]["symbol"], "TSLA")
        self.assertEqual(u1_status["settings"]["fast_sma"], 15)
        self.assertEqual(u1_status["settings"]["trade_qty"], 5.0)

        # Check User 2 status
        u2_status = self.user2_client.get("/api/status").json()
        self.assertEqual(u2_status["settings"]["symbol"], "NVDA")
        self.assertEqual(u2_status["settings"]["fast_sma"], 8)
        self.assertEqual(u2_status["settings"]["trade_qty"], 10.0)

    def test_credentials_encryption_and_user_isolation(self):
        # User 1 saves Paper keys
        u1_key = self.user1_client.post(
            "/api/alpaca-keys",
            json={
                "alpaca_api_key": "PK_USER1_KEY_12345678",
                "alpaca_secret_key": "SK_USER1_SECRET_ABCDEFGH",
                "environment": "paper",
            },
        )
        self.assertEqual(u1_key.status_code, 200)

        # User 2 saves Paper keys
        u2_key = self.user2_client.post(
            "/api/alpaca-keys",
            json={
                "alpaca_api_key": "PK_USER2_KEY_87654321",
                "alpaca_secret_key": "SK_USER2_SECRET_HGFEDCBA",
                "environment": "paper",
            },
        )
        self.assertEqual(u2_key.status_code, 200)

        # Verify key status from each user's perspective
        u1_status = self.user1_client.get("/api/status").json()
        self.assertTrue(u1_status["alpaca_key_status"]["set"])
        self.assertEqual(u1_status["alpaca_key_status"]["api_key_hint"], "PK_U…5678")

        u2_status = self.user2_client.get("/api/status").json()
        self.assertTrue(u2_status["alpaca_key_status"]["set"])
        self.assertEqual(u2_status["alpaca_key_status"]["api_key_hint"], "PK_U…4321")

        # Verify credentials stored in DB are encrypted
        db_creds_u1 = AUTH_STORE.get_user_credentials(self.u1_data["id"])
        self.assertEqual(db_creds_u1["alpaca_paper_api_key"], "PK_USER1_KEY_12345678")
        self.assertEqual(db_creds_u1["alpaca_paper_secret_key"], "SK_USER1_SECRET_ABCDEFGH")

        # Direct SQL inspection: raw secrets must NOT be in plaintext in SQLite
        from bot.auth import _get_connection
        with _get_connection(AUTH_STORE.db_path) as conn:
            row = conn.execute(
                "SELECT alpaca_paper_secret_key FROM user_credentials WHERE user_id = ?",
                (self.u1_data["id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertNotIn("SK_USER1_SECRET_ABCDEFGH", row["alpaca_paper_secret_key"])

    def test_lessons_isolation(self):
        # User 1 saves a lesson
        u1_lesson = self.user1_client.post(
            "/api/history/lessons",
            json={
                "text": "User 1 custom trading rule: do not chase pumps.",
                "scope": "global",
            },
        )
        self.assertEqual(u1_lesson.status_code, 200)

        # User 1 should see 1 lesson
        u1_lessons = self.user1_client.get("/api/history/lessons").json()["lessons"]
        self.assertEqual(len(u1_lessons), 1)
        self.assertEqual(u1_lessons[0]["text"], "User 1 custom trading rule: do not chase pumps.")

        # User 2 should see 0 lessons
        u2_lessons = self.user2_client.get("/api/history/lessons").json()["lessons"]
        self.assertEqual(len(u2_lessons), 0)

    def test_unconfigured_user_blocked_from_trading(self):
        # Fresh user without Alpaca keys
        uid = uuid.uuid4().hex[:6]
        new_client = TestClient(app, follow_redirects=False)
        new_client.post(
            "/api/auth/signup",
            json={
                "username": f"fresh_user_{uid}",
                "email": f"fresh_{uid}@example.com",
                "password": "Password123!",
            },
        )
        # Attempting account refresh or manual order without keys raises clear 400 error
        res = new_client.post("/api/account")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Alpaca API credentials are not configured", res.json().get("detail", ""))

        # Manual order attempt also cleanly fails
        order_res = new_client.post(
            "/api/order",
            json={
                "symbol": "AAPL",
                "side": "buy",
                "order_type": "market",
                "qty": 1.0,
            },
        )
        self.assertEqual(order_res.status_code, 400)
        self.assertIn("Alpaca API credentials are not configured", order_res.json().get("detail", ""))

    def test_backtest_history_isolation(self):
        # User 1 backtest history should be empty initially
        u1_hist = self.user1_client.get("/api/backtest/history").json()["history"]
        u2_hist = self.user2_client.get("/api/backtest/history").json()["history"]
        self.assertEqual(len(u1_hist), 0)
        self.assertEqual(len(u2_hist), 0)

    def test_plans_isolation(self):
        # User 1 reinvest / followon / dip-hunt plans
        u1_reinvest = self.user1_client.get("/api/reinvest").json()["plans"]
        u2_reinvest = self.user2_client.get("/api/reinvest").json()["plans"]
        self.assertEqual(len(u1_reinvest), 0)
        self.assertEqual(len(u2_reinvest), 0)


if __name__ == "__main__":
    unittest.main()
