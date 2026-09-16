"""Unit tests for Auto-Trade Self-Healing Watchdog & Desired State Persistence."""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot.auth import AuthStore
from bot.multi_trader import MultiTradeManager, TickerRunner
from bot.web_state import AppState, USER_STATE_REGISTRY


class TestAutoTradeWatchdog(unittest.TestCase):
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
            username="watchdog_trader",
            email="watchdog@example.com",
            password="Password123!",
            role="trader",
        )
        self.user_id = user["id"]
        self.state = AppState(workspace_dir=self.tmp_dir, user_id=self.user_id)

    def tearDown(self):
        if hasattr(self.state, "stop_watchdog"):
            self.state.stop_watchdog()
        if hasattr(self.state, "stop_loop"):
            self.state.stop_loop()
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

    def test_user_start_and_stop_sets_desired_state(self):
        with patch.object(self.state, "_require_live_execution"), \
             patch.object(self.state, "_loop_worker"):
            self.state.start_loop()
            self.assertTrue(self.state.auto_trade_desired)
            self.assertTrue(self.state.loop_running)

            state_file = self.tmp_dir / ".auto_trade_state.json"
            self.assertTrue(state_file.is_file())
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertTrue(data.get("auto_trade_desired"))

            # Explicit user stop
            self.state.stop_loop()
            self.assertFalse(self.state.auto_trade_desired)
            with open(state_file, "r", encoding="utf-8") as f:
                data2 = json.load(f)
            self.assertFalse(data2.get("auto_trade_desired"))

    def test_watchdog_recovers_unexpectedly_stopped_loop(self):
        with patch.object(self.state, "_require_live_execution"):
            self.state.auto_trade_desired = True
            self.state.loop_running = False
            self.state.loop_stopping = False
            self.state._thread = None
            self.state._last_auto_restart_attempt = 0.0

            with patch.object(self.state, "start_loop") as mock_start:
                self.state._check_and_recover_auto_trade()
                mock_start.assert_called_once_with(from_watchdog=True)
                self.assertEqual(self.state._auto_recovery_count, 1)
                self.assertIsNotNone(self.state._last_auto_recovery_at)

            # Check audit event
            events = [e for e in self.state.desk_events if e.get("kind") == "watchdog"]
            self.assertTrue(len(events) > 0)
            self.assertIn("automatically restarted", events[0]["reason"])

    def test_watchdog_does_not_recover_if_user_stopped(self):
        with patch.object(self.state, "_require_live_execution"):
            self.state.auto_trade_desired = False
            self.state.loop_running = False
            self.state.loop_stopping = False
            self.state._thread = None

            with patch.object(self.state, "start_loop") as mock_start:
                self.state._check_and_recover_auto_trade()
                mock_start.assert_not_called()
                self.assertEqual(self.state._auto_recovery_count, 0)

    def test_bootstrap_restores_desired_state(self):
        state_file = self.tmp_dir / ".auto_trade_state.json"
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({
                "auto_trade_desired": True,
                "strategy_mode": "sma",
                "multi_runners": [],
            }, f)

        new_state = AppState(workspace_dir=self.tmp_dir, user_id=self.user_id)
        try:
            self.assertTrue(new_state.auto_trade_desired)
        finally:
            new_state.stop_watchdog()

    def test_multi_runner_watchdog_recovery(self):
        with patch.object(TickerRunner, "_run_cycle", return_value=None):
            runner = self.state.multi_trader.start_runner("TSLA", "sma")
            self.assertTrue(runner.desired_running)
            self.assertTrue(runner.is_running)

            # Simulate unexpected thread termination
            runner._stop_event.set()
            if runner._thread and runner._thread.is_alive():
                runner._thread.join(timeout=2.0)
            runner._thread = None
            runner.status = "stopped"
            # Crucially: user did not stop it, desired_running is still True
            runner.desired_running = True
            self.assertFalse(runner.is_running)
            self.assertTrue(runner.desired_running)

            # Watchdog recovers
            runner._last_restart_attempt = 0.0
            recovered = self.state.multi_trader.check_and_recover_runners()
            self.assertEqual(recovered, ["TSLA"])
            self.assertTrue(runner.is_running)

            # Explicit user stop
            self.state.multi_trader.stop_runner("TSLA")
            self.assertFalse(runner.desired_running)
            recovered_after_stop = self.state.multi_trader.check_and_recover_runners()
            self.assertEqual(recovered_after_stop, [])

    def test_snapshot_includes_watchdog_metrics(self):
        snap = self.state.snapshot()
        self.assertIn("auto_trade_desired", snap)
        self.assertIn("auto_recovery_active", snap)
        self.assertIn("auto_recovery_count", snap)
        self.assertTrue(snap["auto_recovery_active"])

    def test_restart_recovery_message_differentiated(self):
        with patch.object(self.state, "_require_live_execution"):
            self.state.auto_trade_desired = True
            self.state._resumed_from_restart = True
            self.state.loop_running = False
            self.state.loop_stopping = False
            self.state._thread = None
            self.state._last_auto_restart_attempt = 0.0

            with patch.object(self.state, "start_loop"):
                self.state._check_and_recover_auto_trade()

            events = [e for e in self.state.desk_events if e.get("kind") == "watchdog"]
            self.assertTrue(len(events) > 0)
            self.assertIn("resumed automatically after server restart", events[0]["reason"])

    def test_resume_all_desired_scans_workspaces(self):
        workspaces_dir = self.tmp_dir / "workspaces"
        workspaces_dir.mkdir(parents=True, exist_ok=True)
        user_ws = workspaces_dir / "user_99"
        user_ws.mkdir(parents=True, exist_ok=True)
        with open(user_ws / ".auto_trade_state.json", "w", encoding="utf-8") as f:
            json.dump({"auto_trade_desired": True}, f)

        with patch("bot.web_state.DB_DIR", self.tmp_dir):
            resumed = USER_STATE_REGISTRY.resume_all_desired()
            self.assertIn(99, resumed)
            st = USER_STATE_REGISTRY.get(99)
            try:
                self.assertTrue(st.auto_trade_desired)
            finally:
                USER_STATE_REGISTRY.remove(99)
