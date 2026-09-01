import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import MagicMock, patch

from bot.auth import AuthStore
from bot.web_state import AppState
from bot.webapp import FollowOnIn, ManualOrderIn


class FollowOnTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.tmp_dir.name)
        self.auth_db_path = self.workspace_dir / "auth.db"
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
        self.state = AppState(workspace_dir=self.workspace_dir, user_id=self.user_id)

    def tearDown(self):
        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        webapp_module.AUTH_STORE = self._orig_webapp_auth
        web_state_module.AUTH_STORE = self._orig_web_state_auth
        auth_module.AUTH_STORE = self._orig_auth_store

        self.tmp_dir.cleanup()

    def test_followon_in_model_without_expire_minutes(self):
        followon = FollowOnIn(
            enabled=True,
            kind="rotate",
            target_symbol="GDXU",
            qty_mode="custom",
            qty=10,
            order_type="market",
            market=True,
        )
        self.assertTrue(followon.enabled)
        self.assertEqual(followon.kind, "rotate")
        self.assertEqual(followon.target_symbol, "GDXU")
        self.assertEqual(followon.qty, 10.0)
        self.assertEqual(followon.order_type, "market")
        self.assertIsNone(followon.expire_minutes)

    def test_normalize_followon_request_no_automatic_expire(self):
        raw = {
            "enabled": True,
            "kind": "rotate",
            "target_symbol": "GDXU",
            "qty_mode": "custom",
            "qty": 10,
            "order_type": "market",
            "market": True,
        }
        normalized = AppState.normalize_followon_request(
            raw, side="sell", close_symbol="GDXD"
        )
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["target_symbol"], "GDXU")
        self.assertEqual(normalized["next_side"], "buy")
        self.assertEqual(normalized["order_type"], "market")
        self.assertIsNone(normalized.get("expire_minutes"))

    def test_followon_plan_stays_waiting_and_does_not_expire(self):
        normalized = AppState.normalize_followon_request(
            {
                "enabled": True,
                "kind": "rotate",
                "target_symbol": "GDXU",
                "qty_mode": "custom",
                "qty": 10,
                "order_type": "market",
                "market": True,
            },
            side="sell",
            close_symbol="GDXD",
        )
        with patch.object(self.state, "_start_followon_watcher"):
            entry = self.state._register_followon_plan(
                symbol="GDXD",
                close_side="sell",
                close_order_id="order-sell-123",
                close_qty=60,
                close_limit_price=17.15,
                plan=normalized,
            )
            self.assertEqual(entry["status"], "waiting")
            self.assertIsNone(entry["expires_at"])
            self.assertFalse(AppState._followon_send_window_expired(entry))

            # Test reload/bootstrap does not expire the plan
            self.state.followon_plans.clear()
            resumed = self.state.bootstrap_followon_plans()
            self.assertEqual(resumed, 1)
            plan = self.state.followon_plans[entry["id"]]
            self.assertEqual(plan["status"], "waiting")
            self.assertIsNone(plan["expires_at"])

    @patch("bot.web_state.AlpacaService")
    def test_advance_followon_places_buy_order_after_sell_fills(self, mock_alpaca_cls):
        mock_service = MagicMock()
        mock_alpaca_cls.return_value = mock_service

        # Mock close order filled
        mock_service.get_order_snapshot.return_value = {
            "id": "order-sell-123",
            "status": "filled",
            "filled_qty": 60,
            "is_terminal": True,
        }
        mock_service.market_session.return_value = {
            "is_open": True,
            "session": "regular",
        }
        mock_service.get_position_qty_strict.return_value = 0.0
        mock_submitted = MagicMock()
        mock_submitted.id = "order-buy-456"
        mock_service.submit_manual_order.return_value = (mock_submitted, None)

        normalized = AppState.normalize_followon_request(
            {
                "enabled": True,
                "kind": "rotate",
                "target_symbol": "GDXU",
                "qty_mode": "custom",
                "qty": 10,
                "order_type": "market",
                "market": True,
            },
            side="sell",
            close_symbol="GDXD",
        )
        with patch.object(self.state, "_start_followon_watcher"):
            entry = self.state._register_followon_plan(
                symbol="GDXD",
                close_side="sell",
                close_order_id="order-sell-123",
                close_qty=60,
                close_limit_price=17.15,
                plan=normalized,
            )

            # Advance followon plan
            self.state._advance_followon_plan(entry["id"])

        # Verify buy order was placed
        plan = self.state.followon_plans[entry["id"]]
        self.assertEqual(plan["status"], "placed")
        self.assertEqual(plan["next_order_id"], "order-buy-456")
        self.assertEqual(plan["next_qty"], 10.0)
        self.assertIn("Buy 10 GDXU at market", plan["message"])


if __name__ == "__main__":
    unittest.main()
