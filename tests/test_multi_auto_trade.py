"""Tests for Multi Auto-Trade system (TickerRunner, MultiTradeManager, API endpoints)."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from bot.auth import AuthStore
from bot.multi_trader import MultiTradeManager, TickerRunner
from bot.web_state import AppState
from bot.webapp import app, require_auth


class TestMultiAutoTrade(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.auth_db_path = self.tmp_dir / "auth.db"
        self.auth_store = AuthStore(db_path=self.auth_db_path)

        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        self._orig_webapp_auth = webapp_module.AUTH_STORE
        self._orig_web_state_auth = web_state_module.AUTH_STORE
        self._orig_auth_store = auth_module.AUTH_STORE

        webapp_module.AUTH_STORE = self.auth_store
        web_state_module.AUTH_STORE = self.auth_store
        auth_module.AUTH_STORE = self.auth_store

        user = self.auth_store.register_user(
            username="trader",
            email="trader@example.com",
            password="Password123!",
            role="trader",
        )
        self.user_id = user["id"]
        self.state = AppState(workspace_dir=self.tmp_dir, user_id=self.user_id)

    def tearDown(self):
        if hasattr(self.state, "multi_trader"):
            self.state.multi_trader.stop_all()
        if hasattr(self.state, "_synthetic_order_stop"):
            self.state._synthetic_order_stop.set()

        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        webapp_module.AUTH_STORE = self._orig_webapp_auth
        web_state_module.AUTH_STORE = self._orig_web_state_auth
        auth_module.AUTH_STORE = self._orig_auth_store
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_manager_start_and_stop_single_runner(self):
        mgr = self.state.multi_trader
        self.assertEqual(len(mgr.list_runners()), 0)

        # Mock _run_cycle so it doesn't make broker calls while keeping loop alive
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = mgr.start_runner(
                symbol="AAPL",
                strategy_mode="sma",
                settings={"bar_timeframe": "5Min", "poll_seconds": 15},
            )
            self.assertEqual(runner.symbol, "AAPL")
            self.assertEqual(runner.strategy_mode, "sma")
            self.assertEqual(runner.engine_name, "SMA Crossover")
            self.assertEqual(runner.status, "running")

            # Check snapshot
            snap = runner.snapshot()
            self.assertEqual(snap["symbol"], "AAPL")
            self.assertEqual(snap["timeframe"], "5Min")
            self.assertEqual(snap["poll_seconds"], 15)

            # Check manager queries
            summary = mgr.get_runner_summary("AAPL")
            self.assertIsNotNone(summary)
            self.assertEqual(summary["symbol"], "AAPL")

            active_map = mgr.active_symbols_map()
            self.assertIn("AAPL", active_map)

            # Stop runner
            stopped = mgr.stop_runner("AAPL")
            self.assertTrue(stopped)
            self.assertEqual(runner.status, "stopped")

    def test_manager_start_batch(self):
        mgr = self.state.multi_trader
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runners = mgr.start_batch(
                symbols=["AAPL", "NVDA", "TSLA"],
                strategy_mode="ai",
                settings={"poll_seconds": 30},
            )
            self.assertEqual(len(runners), 3)
            symbols = [r.symbol for r in runners]
            self.assertIn("AAPL", symbols)
            self.assertIn("NVDA", symbols)
            self.assertIn("TSLA", symbols)

            for r in runners:
                self.assertEqual(r.strategy_mode, "ai")
                self.assertEqual(r.engine_name, "AI Momentum")

            # Stop all
            count = mgr.stop_all()
            self.assertEqual(count, 3)
            for r in runners:
                self.assertEqual(r.status, "stopped")

    def test_app_state_multi_auto_trade_methods(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            started = self.state.start_multi_auto_trade(
                symbols="GLD, SLV",
                strategy_mode="dip",
                settings={"trade_qty": 2.0},
            )
            self.assertEqual(len(started), 2)
            active = self.state.list_multi_auto_trades(active_only=True)
            self.assertEqual(len(active), 2)

            stopped = self.state.stop_multi_auto_trade("GLD")
            self.assertTrue(stopped)
            self.assertEqual(len(self.state.list_multi_auto_trades(active_only=True)), 1)

            total_stopped = self.state.stop_all_multi_auto_trades()
            self.assertEqual(total_stopped, 1)

    def test_custom_engine_resolves_base_mode_and_choices(self):
        engine = {
            "id": "eng_test",
            "name": "Momentum Sniper",
            "base_engine": "ai",
            "choices": {
                "strategy_mode": "ai",
                "ai_preset": "swing_trader",
                "ai_min_confidence": 0.72,
                "symbol": "IGNORED",
            },
        }
        with patch(
            "bot.multi_trader.custom_engine_store.get_custom_engine",
            return_value=engine,
        ):
            runner = TickerRunner(
                symbol="AAPL",
                app_state=self.state,
                custom_engine_id="eng_test",
            )

        # The engine's own base strategy wins when no standard mode was picked.
        self.assertEqual(runner.strategy_mode, "ai")
        self.assertEqual(runner.engine_name, "Momentum Sniper")
        self.assertEqual(runner.settings["ai_preset"], "swing_trader")
        # Symbol is owned by the runner, never by the engine's saved choices.
        self.assertNotIn("symbol", runner.settings)

        cfg = runner._build_config()
        self.assertEqual(cfg.strategy_mode, "ai")
        self.assertEqual(cfg.symbol, "AAPL")
        self.assertEqual(cfg.ai_preset, "swing_trader")
        self.assertAlmostEqual(cfg.ai_min_confidence, 0.72)

    def test_explicit_mode_overrides_custom_engine_base(self):
        engine = {"id": "eng_test", "name": "Dip Engine", "base_engine": "dip", "choices": {}}
        with patch(
            "bot.multi_trader.custom_engine_store.get_custom_engine",
            return_value=engine,
        ):
            runner = TickerRunner(
                symbol="AAPL",
                strategy_mode="day",
                app_state=self.state,
                custom_engine_id="eng_test",
            )
        self.assertEqual(runner.strategy_mode, "day")

    def test_unsupported_modes_are_rejected(self):
        with self.assertRaises(ValueError):
            TickerRunner(symbol="AAPL", strategy_mode="pair", app_state=self.state)
        with self.assertRaises(ValueError):
            TickerRunner(symbol="AAPL", strategy_mode="nonsense", app_state=self.state)

        mgr = self.state.multi_trader
        with self.assertRaises(ValueError):
            mgr.start_runner(symbol="AAPL", strategy_mode="pair")
        self.assertEqual(len(mgr.list_runners()), 0)

    def test_build_config_applies_settings_and_guards_protected_fields(self):
        runner = TickerRunner(
            symbol="nvda",
            strategy_mode="ls",
            app_state=self.state,
            settings={
                "bar_timeframe": "1Hour",
                "poll_seconds": 45,
                "trade_qty": "3",
                "size_mode": "qty",
                # Beyond the sizing basics: engine tuning must survive too.
                "ls_ema_fast": "8",
                "ls_rr": 2.5,
                "day_eod_flatten": "false",
                "symbol": "HACK",
                "api_key": "leaked",
                "unknown_field": 1,
            },
        )
        cfg = runner._build_config()

        self.assertEqual(cfg.symbol, "NVDA")
        self.assertEqual(cfg.symbols, ("NVDA",))
        self.assertEqual(cfg.strategy_mode, "ls")
        self.assertEqual(cfg.bar_timeframe, "1Hour")
        self.assertEqual(cfg.poll_seconds, 45)
        self.assertEqual(cfg.trade_qty, 3.0)
        self.assertEqual(cfg.ls_ema_fast, 8)
        self.assertAlmostEqual(cfg.ls_rr, 2.5)
        self.assertIs(cfg.day_eod_flatten, False)
        self.assertNotEqual(cfg.api_key, "leaked")

    def test_day_mode_forces_intraday_timeframe(self):
        runner = TickerRunner(
            symbol="TSLA",
            strategy_mode="day",
            app_state=self.state,
            settings={"bar_timeframe": "1Day"},
        )
        cfg = runner._build_config()
        self.assertIn(cfg.bar_timeframe, {"1Min", "5Min", "15Min", "30Min", "1Hour"})
        self.assertEqual(runner.timeframe, cfg.bar_timeframe)

    def test_snapshot_exposes_runners_for_auto_trade_page(self):
        """The Auto Trade page's monitoring panel renders straight off snapshot()."""
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            self.state.start_multi_auto_trade(symbols=["AAPL"], strategy_mode="sma")

            snap = self.state.snapshot()
            self.assertEqual(snap["active_auto_trades_count"], 1)
            runner = snap["active_auto_trades"][0]
            self.assertEqual(runner["symbol"], "AAPL")
            self.assertEqual(runner["engine_name"], "SMA Crossover")
            self.assertTrue(runner["is_running"])
            # Fields the panel reads for each row.
            for key in ("timeframe", "poll_seconds", "cycles_count", "trades_count", "uptime_seconds"):
                self.assertIn(key, runner)

            self.state.stop_all_multi_auto_trades()
            self.assertEqual(self.state.snapshot()["active_auto_trades_count"], 0)

    def test_positions_overview_annotates_auto_trade(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            self.state.start_multi_auto_trade(
                symbols=["AAPL"],
                strategy_mode="sma",
            )

            # Mock AlpacaService positions
            mock_positions = [
                {
                    "symbol": "AAPL",
                    "qty": 10,
                    "side": "long",
                    "market_value": 1500.0,
                    "cost_basis": 1400.0,
                    "unrealized_pl": 100.0,
                    "current_price": 150.0,
                },
                {
                    "symbol": "MSFT",
                    "qty": 5,
                    "side": "long",
                    "market_value": 1600.0,
                    "cost_basis": 1500.0,
                    "unrealized_pl": 100.0,
                    "current_price": 320.0,
                },
            ]

            with patch("bot.web_state.AlpacaService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service_cls.return_value = mock_service
                mock_service.get_all_positions.return_value = mock_positions
                mock_service.account_summary.return_value = {"equity": 10000.0, "cash": 5000.0, "buying_power": 10000.0}
                mock_service.get_open_orders_summary.return_value = {}

                overview = self.state.positions_overview()
                self.assertIn("active_auto_trades", overview)
                self.assertEqual(len(overview["active_auto_trades"]), 1)

                pos_map = {p["symbol"]: p for p in overview["positions"]}
                self.assertTrue(pos_map["AAPL"]["is_auto_trading"])
                self.assertEqual(pos_map["AAPL"]["auto_trade"]["strategy_mode"], "sma")
                self.assertFalse(pos_map["MSFT"]["is_auto_trading"])


class TestMultiAutoTradeApi(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.auth_db_path = self.tmp_dir / "auth.db"
        self.auth_store = AuthStore(db_path=self.auth_db_path)

        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        self._orig_webapp_auth = webapp_module.AUTH_STORE
        self._orig_web_state_auth = web_state_module.AUTH_STORE
        self._orig_auth_store = auth_module.AUTH_STORE

        webapp_module.AUTH_STORE = self.auth_store
        web_state_module.AUTH_STORE = self.auth_store
        auth_module.AUTH_STORE = self.auth_store

        user = self.auth_store.register_user(
            username="testuser",
            email="testuser@example.com",
            password="Password123!",
            role="trader",
        )
        self.user_id = user["id"]
        self.state = AppState(workspace_dir=self.tmp_dir, user_id=self.user_id)

        # Override auth dependency
        app.dependency_overrides[require_auth] = lambda: {"id": self.user_id, "username": "testuser"}
        self.client = TestClient(app)

    def tearDown(self):
        if hasattr(self.state, "multi_trader"):
            self.state.multi_trader.stop_all()
        if hasattr(self.state, "_synthetic_order_stop"):
            self.state._synthetic_order_stop.set()

        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        webapp_module.AUTH_STORE = self._orig_webapp_auth
        web_state_module.AUTH_STORE = self._orig_web_state_auth
        auth_module.AUTH_STORE = self._orig_auth_store

        app.dependency_overrides.clear()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("bot.webapp.get_user_state")
    def test_api_multi_auto_trade_lifecycle(self, mock_get_state):
        mock_get_state.return_value = self.state

        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            # 1. Start multi auto-trade for AAPL & MSFT
            res = self.client.post(
                "/api/auto-trade/multi/start",
                json={
                    "symbols": ["AAPL", "MSFT"],
                    "strategy_mode": "day",
                    "bar_timeframe": "5Min",
                    "poll_seconds": 20,
                    "trade_qty": 5.0,
                },
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["started"]), 2)
            self.assertIn("AAPL", data["active_symbols"])
            self.assertIn("MSFT", data["active_symbols"])

            # 2. Get status
            res_status = self.client.get("/api/auto-trade/multi")
            self.assertEqual(res_status.status_code, 200)
            status_data = res_status.json()
            self.assertEqual(len(status_data["active_runners"]), 2)

            # 3. Stop one ticker
            res_stop = self.client.post(
                "/api/auto-trade/multi/stop",
                json={"symbol": "AAPL"},
            )
            self.assertEqual(res_stop.status_code, 200)
            stop_data = res_stop.json()
            self.assertTrue(stop_data["ok"])
            self.assertNotIn("AAPL", stop_data["active_symbols"])
            self.assertIn("MSFT", stop_data["active_symbols"])

            # 4. Stop all
            res_stop_all = self.client.post("/api/auto-trade/multi/stop-all")
            self.assertEqual(res_stop_all.status_code, 200)
            stop_all_data = res_stop_all.json()
            self.assertEqual(stop_all_data["stopped_count"], 1)
            self.assertEqual(len(stop_all_data["active_runners"]), 0)


if __name__ == "__main__":
    unittest.main()
