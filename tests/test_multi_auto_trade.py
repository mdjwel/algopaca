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
            self.assertEqual(len(runners), 1)
            runner = runners[0]
            self.assertEqual(runner.symbols, ["AAPL", "NVDA", "TSLA"])
            self.assertEqual(runner.symbol, "AAPL")
            self.assertEqual(runner.strategy_mode, "ai")
            self.assertEqual(runner.engine_name, "AI Momentum")

            # Stop all
            count = mgr.stop_all()
            self.assertEqual(count, 1)
            self.assertEqual(runner.status, "stopped")

    def test_partial_basket_replacement_is_rejected_without_stopping_other_symbols(self):
        """Launching one basket member must not silently stop its siblings."""
        mgr = self.state.multi_trader
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            basket = mgr.start_batch(["AAPL", "MSFT"], strategy_mode="sma")[0]

            with self.assertRaisesRegex(ValueError, "would also stop MSFT"):
                mgr.start_runner("AAPL", strategy_mode="dip")

            self.assertTrue(basket.is_running)
            self.assertTrue(mgr.is_running("AAPL"))
            self.assertTrue(mgr.is_running("MSFT"))

    def test_app_state_multi_auto_trade_methods(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            started = self.state.start_multi_auto_trade(
                symbols="GLD, SLV",
                strategy_mode="dip",
                settings={"trade_qty": 2.0},
            )
            self.assertEqual(len(started), 1)
            self.assertEqual(started[0]["symbols"], ["GLD", "SLV"])
            active = self.state.list_multi_auto_trades(active_only=True)
            self.assertEqual(len(active), 1)

            stopped = self.state.stop_multi_auto_trade("GLD")
            self.assertTrue(stopped)
            self.assertEqual(len(self.state.list_multi_auto_trades(active_only=True)), 0)

            total_stopped = self.state.stop_all_multi_auto_trades()
            self.assertEqual(total_stopped, 0)

    def test_clear_stopped_runners(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            r1 = self.state.multi_trader.start_runner(symbol="AAPL", strategy_mode="sma")
            r2 = self.state.multi_trader.start_runner(symbol="MSFT", strategy_mode="sma")

            # Stop AAPL, leave MSFT running
            self.state.multi_trader.stop_runner("AAPL")
            self.assertFalse(r1.is_running)
            self.assertTrue(r2.is_running)

            # Insert an extra dummy entry into history
            self.state.multi_trader._history.append({"id": "dummy-hist", "symbol": "NVDA", "is_running": False})

            # Clear stopped runners
            cleared = self.state.multi_trader.clear_stopped_runners()
            self.assertEqual(cleared, 2)

            # AAPL should be gone, MSFT should remain
            all_runners = self.state.multi_trader.list_runners(active_only=False)
            symbols_left = [r["symbol"] for r in all_runners]
            self.assertIn("MSFT", symbols_left)
            self.assertNotIn("AAPL", symbols_left)
            self.assertNotIn("NVDA", symbols_left)
            self.assertEqual(len(self.state.multi_trader._history), 0)

            # Stop MSFT cleanly
            self.state.multi_trader.stop_runner("MSFT")

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

    def test_positions_overview_and_orders_reflect_trading_mode(self):
        with patch("bot.web_state.AlpacaService") as mock_service_cls:
            mock_service = MagicMock()
            mock_service_cls.return_value = mock_service
            mock_service.get_all_positions.return_value = []
            mock_service.account_summary.return_value = {"equity": 10000.0, "cash": 5000.0, "buying_power": 10000.0}
            mock_service.get_open_orders_summary.return_value = {}
            mock_service.list_orders.return_value = []

            # 1. Default: paper mode
            overview = self.state.positions_overview()
            self.assertEqual(overview["trading_mode"], "paper")
            self.assertTrue(overview["paper"])
            self.assertEqual(overview["account"]["trading_mode"], "paper")
            self.assertTrue(overview["account"]["paper"])

            orders_data = self.state.list_orders()
            self.assertEqual(orders_data["trading_mode"], "paper")
            self.assertTrue(orders_data["paper"])

            # 2. Switch to live mode in auth credentials
            self.auth_store.save_user_credentials(
                self.user_id,
                {
                    "trading_mode": "live",
                    "allow_live": True,
                    "alpaca_live_api_key": "live-key",
                    "alpaca_live_secret_key": "live-secret",
                },
            )
            overview_live = self.state.positions_overview()
            self.assertEqual(overview_live["trading_mode"], "live")
            self.assertFalse(overview_live["paper"])
            self.assertEqual(overview_live["account"]["trading_mode"], "live")
            self.assertFalse(overview_live["account"]["paper"])

            orders_live = self.state.list_orders()
            self.assertEqual(orders_live["trading_mode"], "live")
            self.assertFalse(orders_live["paper"])

    def test_two_way_collision_between_loop_and_multi_trader(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            # 1. Start multi auto trade on AAPL
            self.state.start_multi_auto_trade(symbols=["AAPL"], strategy_mode="sma")
            self.assertTrue(self.state.is_symbol_in_auto_trade("AAPL"))

            # 2. Main strategy loop targeting AAPL should fail to start
            self.state.settings.symbol = "AAPL"
            self.state.settings.symbols = "AAPL"
            with self.assertRaises(ValueError) as ctx:
                self.state.start_loop()
            self.assertIn("already actively managed by an isolated Multi Auto-Trade runner", str(ctx.exception))

            # Stop multi auto trade
            self.state.stop_all_multi_auto_trades()

            # 3. Now start main loop on AAPL
            with patch.object(self.state, "_loop_worker", return_value=None):
                self.state.start_loop()
                self.assertTrue(self.state.loop_running)

                # Attempting to start AAPL in multi trader should fail
                with self.assertRaises(ValueError) as ctx2:
                    self.state.start_multi_auto_trade(symbols=["AAPL"], strategy_mode="sma")
                self.assertIn("already actively traded by the running main Auto Trade loop", str(ctx2.exception))

                self.state.stop_loop()

    def test_manual_close_guarded_against_multi_trader(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            self.state.start_multi_auto_trade(symbols=["AAPL"], strategy_mode="sma")

            # Single position close
            with self.assertRaises(ValueError) as ctx:
                self.state.close_single_position("AAPL")
            self.assertIn("Stop Auto-Trade / Multi Auto-Trade for [AAPL]", str(ctx.exception))

            # Batch position close
            with self.assertRaises(ValueError) as ctx_batch:
                self.state.close_batch_positions(["AAPL", "TSLA"])
            self.assertIn("Stop Auto-Trade / Multi Auto-Trade for [AAPL]", str(ctx_batch.exception))

            # Close all positions
            with self.assertRaises(ValueError) as ctx_all:
                self.state.close_all_positions()
            self.assertIn("Stop active Multi Auto-Trade runner(s) [AAPL]", str(ctx_all.exception))

            self.state.stop_all_multi_auto_trades()

    def test_empty_symbols_rejected_in_start_batch(self):
        mgr = self.state.multi_trader
        with self.assertRaises(ValueError) as ctx:
            mgr.start_batch(symbols=[" ", ""], strategy_mode="sma")
        self.assertIn("At least one valid ticker symbol is required", str(ctx.exception))

    def test_size_mode_resolution_and_protection(self):
        # AI size mode is only permitted when strategy is ai
        runner_non_ai = TickerRunner(
            symbol="NVDA",
            strategy_mode="sma",
            app_state=self.state,
            settings={"size_mode": "ai"},
        )
        cfg_non_ai = runner_non_ai._build_config()
        self.assertEqual(cfg_non_ai.size_mode, "qty")

        runner_ai = TickerRunner(
            symbol="NVDA",
            strategy_mode="ai",
            app_state=self.state,
            settings={"size_mode": "ai"},
        )
        cfg_ai = runner_ai._build_config()
        self.assertEqual(cfg_ai.size_mode, "ai")

    def test_history_persistence_and_restore(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            # Start and stop a runner
            runner = self.state.multi_trader.start_runner(symbol="TSLA", strategy_mode="dip")
            runner_id = runner.id
            self.state.multi_trader.stop_runner(runner_id)

            # Save state to disk
            self.state._save_auto_trade_state()

            # Create a new AppState pointing to the same workspace
            new_state = AppState(workspace_dir=self.tmp_dir, user_id=self.user_id)
            new_state.bootstrap_auto_trade()

            # Verify stopped runner is in history of new_state
            snap = new_state.snapshot()
            hist = snap.get("all_auto_trades", [])
            matching = [r for r in hist if r["id"] == runner_id]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["symbol"], "TSLA")
            self.assertEqual(matching[0]["strategy_mode"], "dip")
            self.assertFalse(matching[0]["is_running"])

    def test_trade_history_records_runner_engine_mode(self):
        # When loop_running is False, trade with engine="dip" must record mode="dip"
        fake_trade = [
            {
                "symbol": "AMD",
                "signal": "buy",
                "order_id": "order-123",
                "engine": "dip",
                "qty": 10,
                "price": 100.0,
            }
        ]
        self.state.settings.strategy_mode = "sma"  # global setting is sma
        self.state.loop_running = False
        self.state._record_trade_history(fake_trade)

        # Check recorded session in loop_sessions
        sessions = self.state.loop_sessions
        self.assertTrue(len(sessions) > 0)
        recent_session = sessions[0]
        self.assertEqual(recent_session["mode"], "dip")


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
            self.assertEqual(len(data["started"]), 1)
            self.assertEqual(data["started"][0]["symbols"], ["AAPL", "MSFT"])
            self.assertIn("AAPL", data["active_symbols"])
            self.assertIn("MSFT", data["active_symbols"])

            # 2. Get status
            res_status = self.client.get("/api/auto-trade/multi")
            self.assertEqual(res_status.status_code, 200)
            status_data = res_status.json()
            self.assertEqual(len(status_data["active_runners"]), 1)

            # 3. Stop by symbol
            res_stop = self.client.post(
                "/api/auto-trade/multi/stop",
                json={"symbol": "AAPL"},
            )
            self.assertEqual(res_stop.status_code, 200)
            stop_data = res_stop.json()
            self.assertTrue(stop_data["ok"])
            self.assertNotIn("AAPL", stop_data["active_symbols"])
            self.assertNotIn("MSFT", stop_data["active_symbols"])

            # 4. Stop all
            res_stop_all = self.client.post("/api/auto-trade/multi/stop-all")
            self.assertEqual(res_stop_all.status_code, 200)
            stop_all_data = res_stop_all.json()
            self.assertEqual(stop_all_data["stopped_count"], 0)
            self.assertEqual(len(stop_all_data["active_runners"]), 0)

    @patch("bot.webapp.get_user_state")
    def test_api_multi_start_with_string_symbols_and_custom_settings(self, mock_get_state):
        mock_get_state.return_value = self.state

        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            # Test launching multi auto-trade with comma-separated string symbols
            res = self.client.post(
                "/api/auto-trade/multi/start",
                json={
                    "symbols": "AAPL, NVDA, TSLA",
                    "strategy_mode": "sma",
                    "bar_timeframe": "15Min",
                    "poll_seconds": 25,
                    "size_mode": "notional",
                    "trade_notional": 250.0,
                },
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["started"]), 1)
            self.assertEqual(data["started"][0]["symbols"], ["AAPL", "NVDA", "TSLA"])
            self.assertIn("AAPL", data["active_symbols"])
            self.assertIn("NVDA", data["active_symbols"])
            self.assertIn("TSLA", data["active_symbols"])

            # Verify runner settings
            runners = self.state.multi_trader.list_active()
            self.assertEqual(len(runners), 1)
            for r in runners:
                self.assertEqual(r["strategy_mode"], "sma")
                self.assertEqual(r["timeframe"], "15Min")
                self.assertEqual(r["poll_seconds"], 25)
                self.assertEqual(r["size_mode"], "notional")
                self.assertEqual(r["trade_notional"], 250.0)

            # Clean up: stop all
            res_stop_all = self.client.post("/api/auto-trade/multi/stop-all")
            self.assertEqual(res_stop_all.status_code, 200)
            self.assertEqual(res_stop_all.json()["stopped_count"], 1)

    @patch("bot.webapp.get_user_state")
    def test_multi_auto_trade_remove_and_restart(self, mock_get_state):
        mock_get_state.return_value = self.state

        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            # Start a runner
            res = self.client.post(
                "/api/auto-trade/multi/start",
                json={
                    "symbols": "NVDA",
                    "strategy_mode": "dip",
                    "bar_timeframe": "5Min",
                    "poll_seconds": 20,
                    "trade_qty": 5.0,
                },
            )
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

            # Stop NVDA
            res_stop = self.client.post(
                "/api/auto-trade/multi/stop",
                json={"symbol": "NVDA"},
            )
            self.assertEqual(res_stop.status_code, 200)

            # Check all runners in snapshot contains NVDA
            snap = self.state.snapshot()
            self.assertIn("all_auto_trades", snap)
            nvda_runners = [r for r in snap["all_auto_trades"] if r["symbol"] == "NVDA"]
            self.assertTrue(len(nvda_runners) >= 1)
            old_nvda_id = nvda_runners[0]["id"]

            # Restart it by ID
            res_restart = self.client.post(
                "/api/auto-trade/multi/restart",
                json={"id": old_nvda_id},
            )
            self.assertEqual(res_restart.status_code, 200)
            self.assertTrue(res_restart.json()["ok"])
            self.assertTrue(self.state.multi_trader.is_running("NVDA"))
            new_nvda_id = res_restart.json()["runner"]["id"]

            # Stop it again
            self.client.post("/api/auto-trade/multi/stop", json={"id": new_nvda_id})
            self.assertFalse(self.state.multi_trader.is_running("NVDA"))

            # Start another runner on NVDA - verify old one is preserved in history
            self.client.post("/api/auto-trade/multi/start", json={"symbols": ["NVDA"], "strategy_mode": "sma"})
            snap = self.state.snapshot()
            all_nvda_ids = [r["id"] for r in snap["all_auto_trades"] if r["symbol"] == "NVDA"]
            self.assertIn(new_nvda_id, all_nvda_ids)

            # Remove old stopped runner by ID
            res_remove = self.client.post(
                "/api/auto-trade/multi/remove",
                json={"id": new_nvda_id},
            )
            self.assertEqual(res_remove.status_code, 200)
            self.assertTrue(res_remove.json()["removed"])
            snap_after = self.state.snapshot()
            remaining_nvda_ids = [r["id"] for r in snap_after["all_auto_trades"] if r["symbol"] == "NVDA"]
            self.assertNotIn(new_nvda_id, remaining_nvda_ids)

    @patch("bot.webapp.get_user_state")
    def test_api_multi_auto_trade_clear_stopped(self, mock_get_state):
        mock_get_state.return_value = self.state

        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            # Start two runners
            self.client.post("/api/auto-trade/multi/start", json={"symbols": "AAPL", "strategy_mode": "sma", "poll_seconds": 15})
            self.client.post("/api/auto-trade/multi/start", json={"symbols": "GOOGL", "strategy_mode": "sma", "poll_seconds": 15})

            # Stop AAPL only
            self.client.post("/api/auto-trade/multi/stop", json={"symbol": "AAPL"})

            # Clear stopped runners via API
            res = self.client.post("/api/auto-trade/multi/clear-stopped")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            self.assertGreaterEqual(data["cleared_count"], 1)

            # Check remaining runners in multi_trader: GOOGL is active, AAPL is gone
            all_runners = self.state.multi_trader.list_runners(active_only=False)
            all_syms = [r["symbol"] for r in all_runners]
            self.assertIn("GOOGL", all_syms)
            self.assertNotIn("AAPL", all_syms)

            # Clean up: stop all
            self.client.post("/api/auto-trade/multi/stop-all")

    def test_runner_pnl_tracking_and_snapshot(self):
        mgr = self.state.multi_trader
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = mgr.start_runner(symbol="MSFT", strategy_mode="sma")
            runner.realized_pl = 45.50
            runner.unrealized_pl = 12.25
            runner.total_pl = 57.75
            runner.unrealized_pct = 2.45
            runner.position_qty = 10.0
            runner.position_side = "LONG"
            runner.position_avg_entry = 400.0
            runner.position_cost_basis = 4000.0

            snap = runner.snapshot()
            self.assertEqual(snap["realized_pl"], 45.50)
            self.assertEqual(snap["unrealized_pl"], 12.25)
            self.assertEqual(snap["total_pl"], 57.75)
            self.assertEqual(snap["unrealized_pct"], 2.45)
            self.assertEqual(snap["position_qty"], 10.0)
            self.assertEqual(snap["position_side"], "LONG")
            self.assertEqual(snap["position_avg_entry"], 400.0)
            self.assertEqual(snap["position_cost_basis"], 4000.0)

            mgr.stop_runner("MSFT")

    def test_runner_stop_with_close_position(self):
        mgr = self.state.multi_trader
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = mgr.start_runner(symbol="NVDA", strategy_mode="sma")
            runner.position_qty = 5.0
            runner.position_side = "LONG"

            with patch.object(self.state, "close_single_position", return_value={"ok": True}) as mock_close:
                stopped = mgr.stop_runner("NVDA", close_position=True)
                self.assertTrue(stopped)
                mock_close.assert_called_once_with("NVDA", cancel_orders=True, bypass_autotrade_check=True)

    @patch("bot.webapp.get_user_state")
    def test_api_multi_stop_with_close_positions(self, mock_get_state):
        mock_get_state.return_value = self.state
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = self.state.multi_trader.start_runner(symbol="AAPL", strategy_mode="sma")
            runner.position_qty = 10.0

            with patch.object(self.state, "close_single_position", return_value={"ok": True}) as mock_close:
                res = self.client.post(
                    "/api/auto-trade/multi/stop",
                    json={"symbol": "AAPL", "close_positions": True},
                )
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertTrue(data["ok"])
                self.assertTrue(data["stopped"])
                mock_close.assert_called_once_with("AAPL", cancel_orders=True, bypass_autotrade_check=True)

    @patch("bot.webapp.get_user_state")
    def test_api_loop_stop_with_close_positions(self, mock_get_state):
        mock_get_state.return_value = self.state
        with patch.object(self.state, "_loop_worker", return_value=None):
            self.state.settings.symbols = "SPY"
            self.state.settings.symbol = "SPY"
            self.state.start_loop()
            self.assertTrue(self.state.loop_running)

            with patch.object(self.state, "_close_loop_positions_now") as mock_close_loop:
                res = self.client.post(
                    "/api/loop/stop",
                    json={"close_positions": True},
                )
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertTrue(data["ok"])
                self.assertFalse(data["state"]["loop_running"])
                mock_close_loop.assert_called_once()

    def test_bypass_autotrade_check_on_close_position(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = self.state.multi_trader.start_runner("AAPL", strategy_mode="sma")
            self.assertTrue(self.state.is_symbol_in_auto_trade("AAPL"))

            # Without bypass, closing manually raises ValueError
            with self.assertRaises(ValueError):
                self.state.close_single_position("AAPL")

            # With bypass, manual book control requirement is bypassed
            with patch("bot.web_state.AlpacaService") as mock_service_cls:
                mock_svc = MagicMock()
                mock_svc.close_position.return_value = {"status": "closed", "symbol": "AAPL"}
                mock_service_cls.return_value = mock_svc

                res = self.state.close_single_position("AAPL", bypass_autotrade_check=True)
                self.assertEqual(res["symbol"], "AAPL")
                mock_svc.close_position.assert_called_once()

    def test_flexible_delimiter_symbol_parsing(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            # Test comma, semicolon, space separated symbols
            runners = self.state.start_multi_auto_trade("MSFT, TSLA; GOOG   AMZN")
            self.assertEqual(len(runners), 1)
            self.assertEqual(runners[0]["symbols"], ["MSFT", "TSLA", "GOOG", "AMZN"])

    def test_flat_runner_snapshot_not_corrupted_by_stale_desk_cache(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = self.state.multi_trader.start_runner("META", strategy_mode="sma")
            runner.position_qty = 0.0
            runner.position_side = "flat"
            runner.realized_pl = 50.0

            # Stale desk cache containing old non-zero position
            self.state._desk_avg_entries["META"] = {
                "qty": 10.0,
                "avg_entry_price": 300.0,
                "cost_basis": 3000.0,
                "unrealized_pl": -150.0,
                "side": "long",
            }

            snap = runner.snapshot()
            # Must remain flat and not revived from cache
            self.assertEqual(snap["position_qty"], 0.0)
            self.assertEqual(snap["position_side"], "flat")
            self.assertIsNone(snap["position_avg_entry"])
            self.assertEqual(snap["cost_basis"] if "cost_basis" in snap else snap["position_cost_basis"], 0.0)
            self.assertEqual(snap["unrealized_pl"], 0.0)
            self.assertEqual(snap["total_pl"], 50.0)

    def test_invalid_custom_engine_id_resets_to_none(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = self.state.multi_trader.start_runner(
                "NFLX",
                strategy_mode="dip",
                custom_engine_id="non_existent_engine_id_xyz",
            )
            self.assertIsNone(runner.custom_engine_id)
            self.assertEqual(runner.strategy_mode, "dip")

    def test_timeframe_consistency(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            self.state.settings.bar_timeframe = "1Hour"
            runner = self.state.multi_trader.start_runner("INTC", strategy_mode="sma")
            # Without explicit settings['bar_timeframe'], should pick desk timeframe
            self.assertEqual(runner.timeframe, "1Hour")
            cfg = runner._build_config()
            self.assertEqual(cfg.bar_timeframe, "1Hour")
            snap = runner.snapshot()
            self.assertEqual(snap["timeframe"], "1Hour")

    def test_win_rate_and_performance_analytics(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = self.state.multi_trader.start_runner("AMD", strategy_mode="sma")
            # Initial snapshot
            snap = runner.snapshot()
            self.assertEqual(snap["trades_won"], 0)
            self.assertEqual(snap["trades_lost"], 0)
            self.assertIsNone(snap["win_rate"])
            self.assertIsNone(snap["profit_factor"])

            # Simulate 3 winning trades and 1 losing trade
            with runner._lock:
                runner.trades_won = 3
                runner.gross_profit = 350.0
                runner.trades_lost = 1
                runner.gross_loss = 100.0
                runner.realized_pl = 250.0

            snap = runner.snapshot()
            self.assertEqual(snap["trades_won"], 3)
            self.assertEqual(snap["trades_lost"], 1)
            self.assertEqual(snap["win_rate"], 75.0)  # 3/4 = 75.0%
            self.assertEqual(snap["profit_factor"], 3.5)  # 350 / 100 = 3.5
            self.assertEqual(snap["gross_profit"], 350.0)
            self.assertEqual(snap["gross_loss"], 100.0)

    def test_circuit_breaker_triggers_and_auto_closes_position(self):
        runner = self.state.multi_trader.start_runner(
            "TSLA",
            strategy_mode="sma",
            settings={"max_loss_limit": 50.0},
        )
        self.assertEqual(runner.max_loss_limit, 50.0)
        self.assertFalse(runner.circuit_breaker_triggered)

        # Mock bot run_once and get_position_detail with loss exceeding $50
        fake_bundle = {"primary": {"signal": "BUY", "price": 200.0}, "results": []}
        fake_pos = {
            "qty": 5.0,
            "side": "long",
            "avg_entry": 200.0,
            "current_price": 185.0,
            "market_value": 925.0,
            "unrealized_pl": -75.0,  # -75 exceeds limit of -50
            "unrealized_pct": -7.5,
        }

        mock_bot = MagicMock()
        mock_bot.run_once.return_value = fake_bundle
        mock_bot.service.get_position_detail.return_value = fake_pos

        with patch.object(runner, "_build_bot", return_value=mock_bot):
            with patch.object(self.state, "close_single_position") as mock_close:
                runner._run_cycle()
                self.assertTrue(runner.circuit_breaker_triggered)
                self.assertIn("Circuit breaker", str(runner.circuit_breaker_reason))
                self.assertFalse(runner.desired_running)
                mock_close.assert_called_once_with("TSLA", cancel_orders=True, bypass_autotrade_check=True)

        snap = runner.snapshot()
        self.assertTrue(snap["circuit_breaker_triggered"])
        self.assertEqual(snap["max_loss_limit"], 50.0)

        # Ensure watchdog does not auto-restart circuit breaker tripped runners
        recovered = self.state.multi_trader.check_and_recover_runners()
        self.assertNotIn("TSLA", recovered)

    def test_capital_allocation_cap(self):
        runner = self.state.multi_trader.start_runner(
            "NVDA",
            strategy_mode="sma",
            settings={
                "size_mode": "notional",
                "trade_notional": 2000.0,
                "max_notional_cap": 500.0,
            },
        )
        self.assertEqual(runner.max_notional_cap, 500.0)
        cfg = runner._build_config()
        # trade_notional should be capped to 500.0 instead of 2000.0
        self.assertEqual(cfg.trade_notional, 500.0)
        snap = runner.snapshot()
        self.assertEqual(snap["max_notional_cap"], 500.0)

    def test_session_hours_guard(self):
        runner = self.state.multi_trader.start_runner(
            "AAPL",
            strategy_mode="sma",
            settings={"session_hours": "regular"},
        )
        self.assertEqual(runner.session_hours, "regular")

        mock_service = MagicMock()
        # Mock market as closed
        mock_service.market_session.return_value = {
            "session": "closed",
            "is_open": False,
        }

        with patch("bot.multi_trader.AlpacaService", return_value=mock_service):
            with patch.object(runner, "_build_bot") as mock_build_bot:
                runner._run_cycle()
                # Bot execution should have been skipped entirely
                mock_build_bot.assert_not_called()
                self.assertEqual(runner.last_signal, "HOLD")
                self.assertIn("Session filter", str(runner.last_reason))

    @patch("bot.webapp.get_user_state")
    def test_curated_baskets_endpoint_and_batch_start(self, mock_get_state):
        mock_get_state.return_value = self.state
        client = TestClient(app)
        app.dependency_overrides[require_auth] = lambda: {
            "id": self.user_id,
            "username": "trader",
            "role": "trader",
        }

        # 1. Test GET /api/auto-trade/baskets
        res = client.get("/api/auto-trade/baskets")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["baskets"], list)
        self.assertTrue(any(b["id"] == "mega_tech" for b in data["baskets"]))

        # 2. Test POST /api/auto-trade/multi/start with basket_id
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            start_res = client.post(
                "/api/auto-trade/multi/start",
                json={
                    "basket_id": "semis",
                    "poll_seconds": 15,
                    "max_loss_limit": 100.0,
                    "session_hours": "regular",
                },
            )
            self.assertEqual(start_res.status_code, 200)
            start_data = start_res.json()
            self.assertTrue(start_data["ok"])
            self.assertEqual(len(start_data["started"]), 1)
            started_syms = start_data["started"][0]["symbols"]
            self.assertIn("NVDA", started_syms)
            self.assertIn("AMD", started_syms)

            # Check runner settings
            nvda_runner = self.state.multi_trader.get_runner("NVDA")
            self.assertIsNotNone(nvda_runner)
            self.assertEqual(nvda_runner.max_loss_limit, 100.0)
            self.assertEqual(nvda_runner.session_hours, "regular")

        app.dependency_overrides.clear()

    def test_multi_symbol_snapshot_pl_aggregation(self):
        mgr = self.state.multi_trader
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = mgr.start_runner(["AAPL", "MSFT"], strategy_mode="sma")
            self.state._desk_avg_entries = {
                "AAPL": {"qty": 10.0, "cost_basis": 1500.0, "unrealized_pl": 50.0, "avg_entry_price": 150.0},
                "MSFT": {"qty": 5.0, "cost_basis": 2000.0, "unrealized_pl": 100.0, "avg_entry_price": 400.0},
            }
            snap = runner.snapshot()
            self.assertEqual(snap["unrealized_pl"], 150.0)
            self.assertEqual(snap["position_cost_basis"], 3500.0)
            self.assertIn("AAPL", snap["symbols_data"])
            self.assertIn("MSFT", snap["symbols_data"])
            self.assertEqual(snap["symbols_data"]["AAPL"]["unrealized_pl"], 50.0)
            self.assertEqual(snap["symbols_data"]["MSFT"]["unrealized_pl"], 100.0)
            runner.stop(wait=False)

    def test_restart_and_config_preserves_performance_metrics(self):
        mgr = self.state.multi_trader
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = mgr.start_runner(["GOOGL"], strategy_mode="sma")
            runner.realized_pl = 125.75
            runner.trades_count = 7
            runner.cycles_count = 30
            runner.trades_won = 5
            runner.trades_lost = 2
            runner.gross_profit = 200.0
            runner.gross_loss = 74.25

            configs = mgr.get_active_runners_config()
            self.assertEqual(len(configs), 1)
            cfg = configs[0]
            settings = cfg["settings"]
            self.assertEqual(settings.get("realized_pl"), 125.75)
            self.assertEqual(settings.get("trades_count"), 7)
            self.assertEqual(settings.get("cycles_count"), 30)
            self.assertEqual(settings.get("trades_won"), 5)
            self.assertEqual(settings.get("trades_lost"), 2)

            runner.stop(wait=False)
            restarted = mgr.restart_runner("GOOGL")
            self.assertEqual(restarted.realized_pl, 125.75)
            self.assertEqual(restarted.trades_count, 7)
            self.assertEqual(restarted.cycles_count, 30)
            self.assertEqual(restarted.trades_won, 5)
            self.assertEqual(restarted.trades_lost, 2)
            self.assertEqual(restarted.gross_profit, 200.0)
            self.assertEqual(restarted.gross_loss, 74.25)
            restarted.stop(wait=False)

    def test_positions_overview_includes_main_loop_and_stop(self):
        self.state.settings.symbol = "SPY"
        self.state.settings.strategy_mode = "sma"
        self.state.loop_running = True
        self.state.loop_started_at = 1000.0

        with patch("bot.web_state.AlpacaService") as mock_alpaca_cls:
            mock_alpaca = mock_alpaca_cls.return_value
            mock_alpaca.account_summary.return_value = {
                "equity": "100000",
                "cash": "50000",
                "buying_power": "200000",
                "status": "ACTIVE",
                "currency": "USD",
            }
            mock_alpaca.get_all_positions.return_value = []
            mock_alpaca.get_open_orders_summary.return_value = {}
            overview = self.state.positions_overview()
            active = overview.get("active_auto_trades", [])
            self.assertTrue(any(r.get("id") == "main-loop" and r.get("symbol") == "SPY" for r in active))

        # Test stopping via stop_multi_auto_trade targeting the main loop
        stopped = self.state.stop_multi_auto_trade("SPY")
        self.assertTrue(stopped)
        self.assertFalse(self.state.loop_running)

    def test_stop_all_auto_trades_also_stops_the_visible_main_loop(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = self.state.multi_trader.start_runner("AAPL", strategy_mode="sma")
            self.state.settings.symbol = "SPY"
            self.state.settings.symbols = "SPY"
            self.state.loop_running = True
            self.state._thread = None

            stopped_count = self.state.stop_all_multi_auto_trades()

            self.assertEqual(stopped_count, 2)
            self.assertFalse(runner.is_running)
            self.assertFalse(self.state.loop_running)

if __name__ == "__main__":
    unittest.main()
