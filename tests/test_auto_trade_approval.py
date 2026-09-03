"""Unit tests for Auto-Trade user approval and notification controls."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot.approval_store import approvals_path_for, load_approvals, save_approvals
from bot.auth import AuthStore
from bot.config import Config
from bot.email_service import render_order_approval_email, render_trade_notification_email
from bot.settings_store import load_settings, save_settings
from bot.strategy import Signal, StrategyResult
from bot.trader import TradingBot
from bot.web_state import AppState
from bot.webapp import SettingsIn


class TestApprovalStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_save_and_load_approvals(self):
        path = approvals_path_for(self.tmp_dir, paper=True)
        records = {
            "appr_1": {
                "id": "appr_1",
                "symbol": "AAPL",
                "action": "BUY",
                "qty": 10.0,
                "price": 150.0,
                "status": "pending",
            },
            "appr_2": {
                "id": "appr_2",
                "symbol": "MSFT",
                "action": "SELL",
                "qty": 5.0,
                "price": 300.0,
                "status": "approved",
            },
        }
        save_approvals(records, path)
        loaded = load_approvals(path)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded["appr_1"]["symbol"], "AAPL")
        self.assertEqual(loaded["appr_1"]["status"], "pending")
        self.assertEqual(loaded["appr_2"]["symbol"], "MSFT")
        self.assertEqual(loaded["appr_2"]["status"], "approved")

    def test_paper_and_live_ledgers_are_separate(self):
        paper = approvals_path_for(self.tmp_dir, paper=True)
        live = approvals_path_for(self.tmp_dir, paper=False)
        self.assertNotEqual(paper, live)
        save_approvals(
            {"appr_p": {"id": "appr_p", "symbol": "AAPL", "action": "BUY"}}, paper
        )
        self.assertEqual(load_approvals(live), {})

    def test_execution_fields_survive_a_round_trip(self):
        """`protect`/`stop_price` decide whether Alpaca attaches an OTO leg."""
        path = approvals_path_for(self.tmp_dir, paper=True)
        record = {
            "id": "appr_cover",
            "symbol": "TSLA",
            "action": "COVER",
            "qty": 3.0,
            "price": 250.0,
            "protect": False,
            "cancel_stops": True,
            "stop_price": 262.5,
            "stop_distance": 12.5,
            "take_profit": 220.0,
            "engine": "ai",
            "status": "pending",
        }
        save_approvals({record["id"]: record}, path)
        loaded = load_approvals(path)["appr_cover"]
        self.assertIs(loaded["protect"], False)
        self.assertIs(loaded["cancel_stops"], True)
        self.assertEqual(loaded["stop_price"], 262.5)
        self.assertEqual(loaded["stop_distance"], 12.5)
        self.assertEqual(loaded["take_profit"], 220.0)
        self.assertEqual(loaded["side"], "cover")

    def test_created_at_is_not_restamped_on_reload(self):
        path = approvals_path_for(self.tmp_dir, paper=True)
        record = {
            "id": "appr_time",
            "symbol": "AAPL",
            "action": "BUY",
            "created_at": "2026-01-02T03:04:05+00:00",
        }
        save_approvals({record["id"]: record}, path)
        first = load_approvals(path)["appr_time"]
        self.assertEqual(first["created_at"], "2026-01-02T03:04:05+00:00")
        # Re-saving what we loaded must not shift the timestamp either.
        save_approvals({first["id"]: first}, path)
        second = load_approvals(path)["appr_time"]
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(second["created_ts"], first["created_ts"])


class TestConfigAndSettings(unittest.TestCase):
    def test_config_approval_defaults(self):
        cfg = Config.default()
        self.assertFalse(cfg.require_approval)
        self.assertTrue(cfg.notify_browser)
        self.assertFalse(cfg.notify_email)
        self.assertEqual(cfg.notification_email, "")

    def test_settings_store_roundtrip(self):
        tmp_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp_path = Path(tmp_file.name)
        tmp_file.close()
        try:
            settings_in = {
                "symbol": "TSLA",
                "require_approval": True,
                "notify_browser": True,
                "notify_email": True,
                "notification_email": "trader@example.com",
            }
            save_settings(settings_in, tmp_path)
            loaded = load_settings(tmp_path)
            self.assertTrue(loaded["require_approval"])
            self.assertTrue(loaded["notify_browser"])
            self.assertTrue(loaded["notify_email"])
            self.assertEqual(loaded["notification_email"], "trader@example.com")
        finally:
            if tmp_path.exists():
                tmp_path.unlink()


class TestAuthPreferences(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.tmp_dir) / "test_users.db"
        self.auth = AuthStore(db_path=self.db_path)
        self.user = self.auth.register_user("trader", "trader@example.com", "Password123!")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_user_preferences_approval_fields(self):
        user_id = self.user["id"]
        prefs = self.auth.get_user_preferences(user_id)
        self.assertFalse(prefs["require_approval"])
        self.assertTrue(prefs["notify_browser"])
        self.assertFalse(prefs["notify_email"])
        self.assertEqual(prefs["notification_email"], "")

        updated = self.auth.save_user_preferences(user_id, {
            "require_approval": True,
            "notify_browser": True,
            "notify_email": True,
            "notification_email": "alerts@example.com",
        })
        self.assertTrue(updated["require_approval"])
        self.assertTrue(updated["notify_browser"])
        self.assertTrue(updated["notify_email"])
        self.assertEqual(updated["notification_email"], "alerts@example.com")

        fetched = self.auth.get_user_preferences(user_id)
        self.assertTrue(fetched["require_approval"])
        self.assertTrue(fetched["notify_browser"])
        self.assertTrue(fetched["notify_email"])
        self.assertEqual(fetched["notification_email"], "alerts@example.com")


class TestEmailTemplates(unittest.TestCase):
    def test_render_order_approval_email(self):
        order_details = {
            "id": "appr_12345",
            "symbol": "NVDA",
            "action": "BUY",
            "qty": 25,
            "price": 120.50,
            "estimated_value": 3012.50,
            "reason": "SMA Golden Crossover 10/30",
            "engine": "sma",
            "environment": "paper",
        }
        subject, text_body, html_body = render_order_approval_email(
            to_email="trader@example.com",
            order_details=order_details,
            desk_url="http://localhost:8000/auto-trade?approval=appr_12345",
            lang="en",
        )
        self.assertIn("NVDA", subject)
        self.assertIn("BUY", subject)
        self.assertIn("NVDA", html_body)
        self.assertIn("BUY", html_body)
        self.assertIn("3,012.50", html_body)
        self.assertIn("http://localhost:8000/auto-trade?approval=appr_12345", html_body)
        self.assertIn("Review &amp; Approve Order", html_body)
        self.assertIn("NVDA", text_body)

    def test_render_trade_notification_email_en_and_bn(self):
        order_details = {
            "symbol": "TSLA",
            "action": "BUY",
            "qty": 10,
            "price": 240.0,
            "order_id": "ord_987654",
            "engine": "ai",
            "reason": "Bullish momentum breakout",
        }
        # English
        sub_en, text_en, html_en = render_trade_notification_email(
            to_email="trader@example.com",
            order_details=order_details,
            desk_url="http://localhost:8000/orders",
            lang="en",
        )
        self.assertIn("TSLA", sub_en)
        self.assertIn("BUY", sub_en)
        self.assertIn("Trade Executed", sub_en)
        self.assertIn("$240.00", html_en)
        self.assertIn("ord_987654", html_en)
        self.assertIn("View Orders &amp; Positions", html_en)

        # Bengali
        sub_bn, text_bn, html_bn = render_trade_notification_email(
            to_email="trader@example.com",
            order_details=order_details,
            desk_url="http://localhost:8000/orders",
            lang="bn",
        )
        self.assertIn("TSLA", sub_bn)
        self.assertIn("ট্রেড কার্যকর", sub_bn)
        self.assertIn("অর্ডার ও পজিশন দেখুন", html_bn)
        self.assertIn("ord_987654", text_bn)


class TestTraderApprovalStaging(unittest.TestCase):
    def _service(self):
        service = MagicMock()
        service.get_mark_price.return_value = {"price": 150.0, "is_open": True, "session": "regular"}
        service.market_session.return_value = {"session": "regular", "is_open": True}
        service.get_position_detail.return_value = {"qty": 0, "avg_entry": 0.0}
        service.get_position_qty.return_value = 0
        service.account_summary.return_value = {"equity": 100000.0, "day_pl_pct": 0.0}
        service.list_orders.return_value = []
        service.has_open_orders.return_value = False
        service.current_stop_price.return_value = None
        return service

    @patch("bot.trader.manage_open_position", return_value={})
    @patch("bot.trader.entry_gates")
    def test_trading_bot_approval_handler_called(self, mock_gates, mock_manage):
        mock_gate_res = MagicMock()
        mock_gate_res.__bool__.return_value = True
        mock_gates.return_value = mock_gate_res

        cfg = Config.default(
            symbol="AAPL",
            require_approval=True,
            trade_qty=10,
        )
        service = self._service()

        approval_mock = MagicMock(return_value={"id": "appr_999"})
        bot = TradingBot(config=cfg, service=service, approval_handler=approval_mock)

        # Force strategy result to BUY
        bot.strategy.evaluate = MagicMock(return_value=StrategyResult(
            signal=Signal.BUY,
            price=150.0,
            fast_sma=155.0,
            slow_sma=145.0,
            reason="Test signal",
        ))

        res = bot._run_symbol("AAPL", open_positions=0, day_pl_pct=0.0, equity=100000.0)
        self.assertTrue(res.get("approval_required"))
        self.assertEqual(res.get("pending_approval_id"), "appr_999")
        approval_mock.assert_called_once()
        service.submit_order.assert_not_called()

    @patch("bot.trader.manage_open_position", return_value={})
    @patch("bot.trader.reversal_gate")
    @patch("bot.trader.entry_gates")
    def test_staged_exit_leaves_the_protective_stop_in_place(
        self, mock_gates, mock_reversal, mock_manage
    ):
        """A queued exit may wait for hours — the stop must not be pulled yet."""
        gate_res = MagicMock()
        gate_res.__bool__.return_value = True
        mock_gates.return_value = gate_res
        mock_reversal.return_value = gate_res

        cfg = Config.default(symbol="AAPL", require_approval=True, trade_qty=5)
        service = self._service()
        service.get_position_qty.return_value = 5
        service.get_position_detail.return_value = {"qty": 5, "avg_entry": 140.0}

        approval_mock = MagicMock(return_value={"id": "appr_exit"})
        bot = TradingBot(config=cfg, service=service, approval_handler=approval_mock)
        bot.strategy.evaluate = MagicMock(return_value=StrategyResult(
            signal=Signal.SELL,
            price=150.0,
            fast_sma=145.0,
            slow_sma=155.0,
            reason="Death cross",
        ))

        res = bot._run_symbol("AAPL", open_positions=1, day_pl_pct=0.0, equity=100000.0)
        self.assertTrue(res.get("approval_required"))
        service.submit_order.assert_not_called()
        service.cancel_open_stop_orders.assert_not_called()
        # The cancellation is deferred to the approve path instead.
        self.assertTrue(approval_mock.call_args.kwargs["cancel_stops"])


class TestAppStateApprovals(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.state = AppState(user_id=1, workspace_dir=self.tmp_dir)
        self.state.settings.notify_email = False

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _stage(self, **kwargs):
        payload = {
            "symbol": "AAPL",
            "action": "BUY",
            "qty": 10.0,
            "price": 150.0,
            "reason": "Test BUY",
        }
        payload.update(kwargs)
        return self.state.create_pending_approval(**payload)

    def _patched_service(self, order_id="ord_1"):
        service = MagicMock()
        service.submit_order.return_value = MagicMock(id=order_id)
        service.cancel_open_stop_orders.return_value = 0
        service.ensure_stop_loss.return_value = None
        return service

    def test_create_and_deduplicate_approval(self):
        item1 = self._stage()
        self.assertEqual(item1["status"], "pending")
        self.assertEqual(item1["symbol"], "AAPL")

        # Same symbol and action while one is pending must not queue a second.
        item2 = self._stage(reason="Test BUY Duplicate")
        self.assertEqual(item1["id"], item2["id"])
        self.assertEqual(len(self.state.list_pending_approvals()), 1)

    def test_approvals_are_written_to_the_workspace(self):
        self._stage()
        path = approvals_path_for(self.tmp_dir, paper=True)
        self.assertTrue(path.exists(), "approvals must persist inside the user workspace")
        self.assertEqual(len(load_approvals(path)), 1)

    def test_approve_pending_order_submits_and_records_history(self):
        item = self._stage(symbol="MSFT", price=400.0, qty=3.0)
        service = self._patched_service("ord_msft")
        with patch("bot.web_state.AlpacaService", return_value=service), \
                patch.object(AppState, "_base_config", return_value=Config.default()), \
                patch.object(AppState, "_require_live_execution"):
            res = self.state.approve_pending_order(item["id"])

        self.assertTrue(res["ok"])
        self.assertEqual(res["approval"]["status"], "approved")
        self.assertEqual(res["order_id"], "ord_msft")
        service.submit_order.assert_called_once()
        self.assertEqual(self.state.list_pending_approvals(), [])

        history = list(self.state.result_history)
        self.assertTrue(
            any(t.get("order_id") == "ord_msft" for t in history),
            f"approved order missing from trade history: {history}",
        )

    def test_approve_cancels_resting_stop_for_a_staged_exit(self):
        item = self._stage(symbol="TSLA", action="SELL", cancel_stops=True)
        service = self._patched_service()
        with patch("bot.web_state.AlpacaService", return_value=service), \
                patch.object(AppState, "_base_config", return_value=Config.default()), \
                patch.object(AppState, "_require_live_execution"):
            self.state.approve_pending_order(item["id"])
        service.cancel_open_stop_orders.assert_called_once_with("TSLA")

    def test_approving_twice_is_rejected(self):
        item = self._stage()
        service = self._patched_service()
        with patch("bot.web_state.AlpacaService", return_value=service), \
                patch.object(AppState, "_base_config", return_value=Config.default()), \
                patch.object(AppState, "_require_live_execution"):
            self.state.approve_pending_order(item["id"])
            with self.assertRaises(ValueError):
                self.state.approve_pending_order(item["id"])
        service.submit_order.assert_called_once()

    def test_reject_pending_order(self):
        item = self._stage(symbol="MSFT", action="SELL", qty=5.0, price=400.0)
        res = self.state.reject_pending_order(item["id"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["approval"]["status"], "rejected")
        self.assertEqual(len(self.state.list_pending_approvals()), 0)

    def test_reject_does_not_reach_the_broker(self):
        item = self._stage(symbol="NVDA")
        with patch("bot.web_state.AlpacaService") as svc_cls:
            self.state.reject_pending_order(item["id"])
        svc_cls.assert_not_called()

    def test_approve_all_pending_orders(self):
        self._stage(symbol="AAPL")
        self._stage(symbol="MSFT", price=400.0)
        service = self._patched_service()
        with patch("bot.web_state.AlpacaService", return_value=service), \
                patch.object(AppState, "_base_config", return_value=Config.default()), \
                patch.object(AppState, "_require_live_execution"):
            res = self.state.approve_all_pending_orders()

        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(len(res["approved"]), 2)
        self.assertEqual(service.submit_order.call_count, 2)
        self.assertEqual(self.state.list_pending_approvals(), [])

    def test_reject_all_pending_orders(self):
        self._stage(symbol="AAPL")
        self._stage(symbol="MSFT", price=400.0)
        res = self.state.reject_all_pending_orders()
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(len(res["rejected"]), 2)
        self.assertEqual(self.state.list_pending_approvals(), [])

    def test_clear_resolved_approvals(self):
        item1 = self._stage(symbol="TSLA", qty=5.0, price=200.0)
        item2 = self._stage(symbol="GOOGL", qty=2.0, price=175.0)
        self.state.reject_pending_order(item1["id"])
        self.state.clear_resolved_approvals()

        remaining = self.state.list_all_approvals()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], item2["id"])

    def test_pending_approvals_reload_from_disk(self):
        staged = self._stage(symbol="AMD", price=90.0, protect=True, stop_price=85.0)
        reloaded = AppState(user_id=1, workspace_dir=self.tmp_dir)
        pending = reloaded.list_pending_approvals()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], staged["id"])
        self.assertIs(pending[0]["protect"], True)
        self.assertEqual(pending[0]["stop_price"], 85.0)
        self.assertEqual(pending[0]["created_at"], staged["created_at"])

    def test_snapshot_exposes_pending_approvals(self):
        item = self._stage(symbol="COIN", price=210.0)
        snap = self.state.snapshot()
        ids = [a["id"] for a in snap["pending_approvals"]]
        self.assertIn(item["id"], ids)

    def test_email_dispatched_when_notify_email_enabled(self):
        self.state.settings.notify_email = True
        self.state.settings.notification_email = "alerts@example.com"
        sent = {}

        def _capture(to_email, order_details, desk_url, lang):
            sent["to"] = to_email
            sent["symbol"] = order_details["symbol"]
            sent["url"] = desk_url
            return True

        with patch("bot.email_service.send_order_approval_email", _capture):
            item = self._stage(symbol="SHOP", price=75.0)
            # The dispatch runs on a daemon thread; wait for it to land.
            for thread in list(__import__("threading").enumerate()):
                if thread.daemon and thread is not __import__("threading").current_thread():
                    thread.join(timeout=2.0)

        self.assertEqual(sent.get("to"), "alerts@example.com")
        self.assertEqual(sent.get("symbol"), "SHOP")
        self.assertIn(item["id"], sent.get("url", ""))

    def test_no_email_when_notify_email_disabled(self):
        with patch("bot.email_service.send_order_approval_email") as mock_send:
            self._stage(symbol="RIVN", price=15.0)
        mock_send.assert_not_called()

    def test_auto_executed_trades_dispatch_email_when_notify_email_enabled(self):
        self.state.settings.notify_email = True
        self.state.settings.notification_email = "autotrader@example.com"
        sent_trades = []

        def _capture_trade(to_email, order_details, desk_url, lang):
            sent_trades.append({
                "to": to_email,
                "symbol": order_details.get("symbol"),
                "order_id": order_details.get("order_id"),
            })
            return True

        with patch("bot.email_service.send_trade_notification_email", _capture_trade):
            # Execute automated trade record directly (as happens during automated cycles)
            trade_result = {
                "signal": "buy",
                "symbol": "MSFT",
                "order_id": "ord_auto_111",
                "order_qty": 5,
                "price": 420.0,
            }
            self.state._record_trade_history([trade_result])

            # Wait for daemon thread
            for thread in list(__import__("threading").enumerate()):
                if thread.daemon and thread is not __import__("threading").current_thread():
                    thread.join(timeout=2.0)

        self.assertEqual(len(sent_trades), 1)
        self.assertEqual(sent_trades[0]["to"], "autotrader@example.com")
        self.assertEqual(sent_trades[0]["symbol"], "MSFT")
        self.assertEqual(sent_trades[0]["order_id"], "ord_auto_111")

    def test_settings_in_validates_notification_email(self):
        # Valid email
        s_valid = SettingsIn(notification_email="valid@example.com")
        self.assertEqual(s_valid.notification_email, "valid@example.com")

        # Empty string
        s_empty = SettingsIn(notification_email="   ")
        self.assertEqual(s_empty.notification_email, "")

        # None
        s_none = SettingsIn(notification_email=None)
        self.assertIsNone(s_none.notification_email)

        # Invalid email
        with self.assertRaises(ValueError):
            SettingsIn(notification_email="invalid-not-an-email")


if __name__ == "__main__":
    unittest.main()
