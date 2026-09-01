"""Unit tests for AI trader parallel concurrency, caching, and progress updates."""

import time
import unittest
from unittest.mock import MagicMock, patch

from bot.ai_providers import AiDecision
from bot.ai_trader import AiTradingBot, CycleStopped
from bot.config import Config
from bot.econ_calendar import fetch_economic_calendar, reset_calendar_cache


class TestAiConcurrencyAndCaching(unittest.TestCase):
    def setUp(self):
        reset_calendar_cache()

    def tearDown(self):
        reset_calendar_cache()

    @patch("urllib.request.urlopen")
    def test_economic_calendar_caching(self, mock_urlopen):
        # Mock HTTP response
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'[{"title": "CPI", "country": "USD", "impact": "High", "date": "2026-09-01T12:00:00Z"}]'
        )
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        # First call fetches from network
        res1 = fetch_economic_calendar()
        self.assertEqual(len(res1), 1)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Second call hits in-memory cache without hitting network
        res2 = fetch_economic_calendar()
        self.assertEqual(len(res2), 1)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Reset cache causes fresh network fetch
        reset_calendar_cache()
        res3 = fetch_economic_calendar()
        self.assertEqual(len(res3), 1)
        self.assertEqual(mock_urlopen.call_count, 2)

    def test_ai_trading_bot_parallel_multi_symbol(self):
        symbols = ("AAPL", "MSFT", "GOOGL", "NVDA", "TSLA")
        config = Config.default(
            strategy_mode="ai",
            symbol="AAPL",
            symbols=symbols,
            openai_api_key="test-key",
            paper=True,
        )

        mock_service = MagicMock()
        mock_service.account_summary.return_value = {"equity": 100000, "cash": 50000, "day_pl_pct": 0.5}
        mock_service.get_position_qty.return_value = 0
        mock_service.get_position_detail.return_value = {"qty": 0}
        mock_service.get_mark_price.return_value = {"price": 150.0, "session": "regular", "source": "sip", "asof": "now"}
        mock_service.market_session.return_value = {"session": "open", "is_open": True}
        mock_service.has_open_orders.return_value = False
        mock_service.recent_activity.return_value = {}

        bot = AiTradingBot(config, service=mock_service)

        # Mock brain.build_context and brain.decide to simulate ~0.1s latency per symbol
        def fake_build_context(sym, **kwargs):
            return {
                "symbol": sym,
                "position_qty": 0,
                "technicals": {"ok": True, "price": 150.0, "sma": {"10": 148, "50": 145}, "rsi_14": 55},
                "mark": {"price": 150.0, "is_open": True},
                "session": {"session": "open"},
            }

        def fake_decide(sym, ctx):
            time.sleep(0.05)  # simulate LLM processing time
            return (
                AiDecision(
                    action="hold",
                    confidence=0.75,
                    qty=0,
                    thesis=f"Thesis for {sym}",
                    risks="None",
                    thesis_en=f"Thesis for {sym}",
                    risks_en="None",
                    news_bias="neutral",
                    ta_bias="bullish",
                    raw={},
                    provider="openai",
                    model="gpt-5.6-luna",
                ),
                ctx,
            )

        bot.brain.build_context = fake_build_context
        bot.brain.decide = fake_decide

        progress_calls = []

        def on_progress(partial_results):
            progress_calls.append(len(partial_results))

        start_time = time.time()
        bundle = bot.run_once(on_progress=on_progress)
        elapsed = time.time() - start_time

        # 5 symbols * 0.05s = 0.25s sequentially, but with parallel workers it should be ~0.05-0.12s
        self.assertLess(elapsed, 0.25)

        # Order must be strictly preserved
        results = bundle["results"]
        self.assertEqual(len(results), 5)
        result_symbols = [r["symbol"] for r in results]
        self.assertEqual(result_symbols, list(symbols))
        self.assertEqual(bundle["primary"]["symbol"], "AAPL")

        # on_progress should have been called as symbols finished
        self.assertGreaterEqual(len(progress_calls), 1)
        self.assertEqual(progress_calls[-1], 5)


if __name__ == "__main__":
    unittest.main()
