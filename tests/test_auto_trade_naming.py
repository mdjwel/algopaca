import unittest
from unittest.mock import MagicMock, patch

from bot.multi_trader import MultiTradeManager, TickerRunner, generate_auto_trade_name
from bot.web_state import AppState


class TestAutoTradeNaming(unittest.TestCase):
    """Test suite for auto-trade runner name generation, manual renaming, and persistence."""

    def test_generate_auto_trade_name_sma(self):
        name = generate_auto_trade_name(
            symbols=["AAPL"],
            strategy_mode="sma",
            settings={"fast_period": 10, "slow_period": 30, "bar_timeframe": "15Min"},
        )
        self.assertEqual(name, "AAPL · SMA 10/30 (15Min)")

    def test_generate_auto_trade_name_dip(self):
        name = generate_auto_trade_name(
            symbols="NVDA",
            strategy_mode="dip",
            settings={"dip_percentage": 2.5, "bar_timeframe": "5Min"},
        )
        self.assertEqual(name, "NVDA · Dip 2.5% (5Min)")

    def test_generate_auto_trade_name_day_strategies(self):
        types = [
            ("vwap", "Day VWAP"),
            ("orb", "Day ORB"),
            ("ema_cross", "Day EMA Cross"),
            ("mean_reversion", "Day Mean Reversion"),
        ]
        for day_type, expected_part in types:
            with self.subTest(day_type=day_type):
                name = generate_auto_trade_name(
                    symbols=["SPY"],
                    strategy_mode="day",
                    settings={"day_strategy_type": day_type, "bar_timeframe": "1Min"},
                )
                self.assertEqual(name, f"SPY · {expected_part} (1Min)")

    def test_generate_auto_trade_name_ai(self):
        name_grok = generate_auto_trade_name(
            symbols="TSLA",
            strategy_mode="ai",
            settings={"ai_provider": "xai", "bar_timeframe": "15Min"},
        )
        self.assertEqual(name_grok, "TSLA · AI Grok (15Min)")

        name_gemini = generate_auto_trade_name(
            symbols="TSLA",
            strategy_mode="ai",
            settings={"ai_provider": "gemini", "bar_timeframe": "1Hour"},
        )
        self.assertEqual(name_gemini, "TSLA · AI Gemini (1Hour)")

    def test_generate_auto_trade_name_long_short(self):
        name = generate_auto_trade_name(
            symbols="QQQ",
            strategy_mode="ls",
            settings={"bar_timeframe": "15Min"},
        )
        self.assertEqual(name, "QQQ · Long/Short (15Min)")

    def test_generate_auto_trade_name_multiple_symbols(self):
        # 2 symbols
        name_2 = generate_auto_trade_name(
            symbols=["AAPL", "MSFT"],
            strategy_mode="sma",
            settings={"fast_period": 5, "slow_period": 20, "bar_timeframe": "5Min"},
        )
        self.assertEqual(name_2, "AAPL, MSFT · SMA 5/20 (5Min)")

        # 3 symbols
        name_3 = generate_auto_trade_name(
            symbols="AAPL, MSFT, NVDA",
            strategy_mode="sma",
            settings={"fast_period": 5, "slow_period": 20, "bar_timeframe": "5Min"},
        )
        self.assertEqual(name_3, "AAPL, MSFT, NVDA · SMA 5/20 (5Min)")

        # 4 symbols (overflow)
        name_4 = generate_auto_trade_name(
            symbols=["AAPL", "MSFT", "NVDA", "AMZN"],
            strategy_mode="sma",
            settings={"fast_period": 5, "slow_period": 20, "bar_timeframe": "5Min"},
        )
        self.assertEqual(name_4, "AAPL, MSFT +2 · SMA 5/20 (5Min)")

    def test_generate_auto_trade_name_custom_engine(self):
        name = generate_auto_trade_name(
            symbols="AAPL",
            strategy_mode="custom",
            settings={"custom_engine_id": "eng_123", "bar_timeframe": "15Min"},
            engine_name="Alpha Tech Pulse",
        )
        self.assertEqual(name, "AAPL · Alpha Tech Pulse (15Min)")

    def test_ticker_runner_name_and_rename(self):
        mock_app = MagicMock()
        mock_app.loop_running = False
        mock_app._is_symbol_in_loop.return_value = False
        runner = TickerRunner(
            symbols=["AAPL"],
            strategy_mode="sma",
            app_state=mock_app,
            settings={"fast_period": 10, "slow_period": 30, "bar_timeframe": "15Min"},
        )
        # Default generated name
        self.assertEqual(runner.name, "AAPL · SMA 10/30 (15Min)")
        snap = runner.snapshot()
        self.assertEqual(snap["name"], "AAPL · SMA 10/30 (15Min)")

        # Manual rename
        new_name = runner.rename("My Apple Scalper")
        self.assertEqual(new_name, "My Apple Scalper")
        self.assertEqual(runner.name, "My Apple Scalper")
        self.assertEqual(runner.settings["name"], "My Apple Scalper")
        self.assertEqual(runner.snapshot()["name"], "My Apple Scalper")

        # Empty rename restores generated name
        restored = runner.rename("")
        self.assertEqual(restored, "AAPL · SMA 10/30 (15Min)")
        self.assertEqual(runner.name, "AAPL · SMA 10/30 (15Min)")

    def test_ticker_runner_custom_name_at_init(self):
        mock_app = MagicMock()
        mock_app.loop_running = False
        mock_app._is_symbol_in_loop.return_value = False
        runner = TickerRunner(
            symbols=["NVDA"],
            strategy_mode="dip",
            app_state=mock_app,
            settings={"name": "Night Hunter Dip"},
        )
        self.assertEqual(runner.name, "Night Hunter Dip")
        self.assertEqual(runner.snapshot()["name"], "Night Hunter Dip")

    def test_multi_trader_rename_runner(self):
        mock_app_state = MagicMock()
        mock_app_state.loop_running = False
        mock_app_state._is_symbol_in_loop.return_value = False
        mock_app_state.get_client.return_value = MagicMock()
        mt = MultiTradeManager(app_state=mock_app_state)
        with patch.object(TickerRunner, "start"):
            runner = mt.start_runner(
                symbols=["AAPL"],
                strategy_mode="sma",
                settings={"fast_period": 10, "slow_period": 30},
                name="Custom AAPL Name",
            )
        self.assertEqual(runner.name, "Custom AAPL Name")

        # Rename by symbol
        renamed = mt.rename_runner("AAPL", "Renamed AAPL")
        self.assertTrue(renamed)
        self.assertEqual(runner.name, "Renamed AAPL")

        # Rename by runner id
        renamed_id = mt.rename_runner(runner.id, "ID Renamed AAPL")
        self.assertTrue(renamed_id)
        self.assertEqual(runner.name, "ID Renamed AAPL")

        # Set desired_running and check config serialization preserves name
        runner.desired_running = True
        configs = mt.get_active_runners_config()
        self.assertTrue(any(c.get("name") == "ID Renamed AAPL" for c in configs))

        # Check restore_from_config restores the name
        with patch.object(TickerRunner, "start"):
            restored_runners = mt.restore_from_config(configs)
        self.assertEqual(len(restored_runners), 1)
        self.assertEqual(restored_runners[0].name, "ID Renamed AAPL")

    def test_app_state_rename_main_loop_and_runner(self):
        state = AppState()
        state.loop_running = True
        mock_app_state = MagicMock()
        mock_app_state.loop_running = False
        mock_app_state._is_symbol_in_loop.return_value = False
        state.multi_trader = MultiTradeManager(app_state=mock_app_state)

        # Rename main loop
        ok = state.rename_auto_trade("main-loop", "Primary Algo Engine")
        self.assertTrue(ok)
        self.assertEqual(state.main_loop_name, "Primary Algo Engine")

        # Snapshot reflects the custom name
        snap = state._main_loop_runner_snapshot()
        self.assertIsNotNone(snap)
        self.assertEqual(snap["name"], "Primary Algo Engine")

        # Clear name restores generated name
        ok_clear = state.rename_auto_trade("main-loop", "")
        self.assertTrue(ok_clear)
        self.assertIsNone(state.main_loop_name)
        snap_gen = state._main_loop_runner_snapshot()
        self.assertIsNotNone(snap_gen)
        self.assertIn("·", snap_gen["name"])


if __name__ == "__main__":
    unittest.main()
