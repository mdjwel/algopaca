"""Unit tests for stock ticker search engine and API endpoint."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

from starlette.testclient import TestClient

from bot.auth import AuthStore
from bot.ticker_search import search_tickers, CURATED_TICKERS
import bot.webapp as webapp_module


class TestTickerSearch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_auth.db"
        self.auth_store = AuthStore(db_path=self.db_path)

        self.patcher1 = patch("bot.auth.AUTH_STORE", self.auth_store)
        self.patcher2 = patch("bot.webapp.AUTH_STORE", self.auth_store)
        self.patcher1.start()
        self.patcher2.start()

        self.client = TestClient(webapp_module.app)

        self.user = self.auth_store.register_user(
            username="trader_search",
            email="search@algopaca.local",
            password="StrongPassword123!",
            display_name="Search Trader",
            role="trader",
        )
        self.user_token, _ = self.auth_store.create_session(self.user["id"])

    def tearDown(self):
        self.patcher1.stop()
        self.patcher2.stop()

    def test_search_curated_symbol(self):
        results = search_tickers("AAPL")
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0]["symbol"], "AAPL")
        self.assertEqual(results[0]["name"], "Apple Inc.")
        self.assertEqual(results[0]["exchange"], "NASDAQ")

    def test_search_company_name(self):
        results = search_tickers("Apple")
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0]["symbol"], "AAPL")

        nvda_results = search_tickers("NVIDIA")
        self.assertTrue(len(nvda_results) > 0)
        self.assertEqual(nvda_results[0]["symbol"], "NVDA")

    def test_search_empty_returns_defaults(self):
        results = search_tickers("", limit=5)
        self.assertEqual(len(results), 5)
        symbols = [r["symbol"] for r in results]
        self.assertIn("AAPL", symbols)

    def test_search_portfolio_priority(self):
        positions = [
            {"symbol": "TSLA", "qty": 10.0, "unrealized_pl": 150.0, "unrealized_plpc": 0.05, "current_price": 250.0}
        ]
        results = search_tickers("T", positions=positions, limit=10)
        # TSLA should be prioritized because it is in the portfolio
        top_symbols = [r["symbol"] for r in results[:3]]
        self.assertIn("TSLA", top_symbols)
        tsla_item = next(r for r in results if r["symbol"] == "TSLA")
        self.assertTrue(tsla_item["in_portfolio"])
        self.assertEqual(tsla_item["holding_qty"], 10.0)

    def test_api_endpoint_unauthenticated(self):
        res = self.client.get("/api/tickers/search?q=AAPL")
        self.assertEqual(res.status_code, 401)

    def test_api_endpoint_authenticated(self):
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = self.client.get("/api/tickers/search?q=MSFT&limit=5", headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("query"), "MSFT")
        results = data.get("results")
        self.assertIsInstance(results, list)
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0]["symbol"], "MSFT")
        self.assertEqual(results[0]["name"], "Microsoft Corporation")


if __name__ == "__main__":
    unittest.main()
