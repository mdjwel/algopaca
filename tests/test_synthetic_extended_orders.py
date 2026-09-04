import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from bot.auth import AuthStore
from bot.config import Config
from bot.web_state import AppState
import bot.synthetic_order_store as synthetic_order_store


class SyntheticExtendedOrdersTestCase(unittest.TestCase):
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

    def test_place_synthetic_stop_limit_extended(self):
        with patch.object(self.state, "_base_config", return_value=Config.default()), \
             patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            service_instance.has_position.return_value = True
            service_instance.get_position_qty.return_value = 10.0
            service_instance.get_mark_price.return_value = {"price": 155.0}

            res = self.state.place_manual_order(
                symbol="AAPL",
                side="sell",
                order_type="stop_limit",
                qty=5.0,
                stop_price=150.0,
                limit_price=149.0,
                time_in_force="day",
                extended_hours=True,
            )

            self.assertTrue(res["order_id"].startswith("synth_"))
            self.assertEqual(res["submitted_type"], "synthetic_stop_limit")
            self.assertIn(res["order_id"], self.state.synthetic_orders)

            synth = self.state.synthetic_orders[res["order_id"]]
            self.assertEqual(synth["symbol"], "AAPL")
            self.assertEqual(synth["side"], "sell")
            self.assertEqual(synth["stop_price"], 150.0)
            self.assertEqual(synth["limit_price"], 149.0)
            self.assertTrue(synth["extended_hours"])
            self.assertEqual(synth["status"], "waiting")

            # Verify disk persistence
            loaded = synthetic_order_store.load_orders(self.workspace_dir, paper=True)
            self.assertIn(res["order_id"], loaded)

    def test_place_synthetic_trailing_stop_extended(self):
        with patch.object(self.state, "_base_config", return_value=Config.default()), \
             patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            service_instance.has_position.return_value = True
            service_instance.get_position_qty.return_value = 10.0
            service_instance.get_mark_price.return_value = {"price": 100.0}

            res = self.state.place_manual_order(
                symbol="MSFT",
                side="sell",
                order_type="trailing_stop",
                qty=10.0,
                trail_percent=5.0,
                time_in_force="gtc",
                extended_hours=True,
            )

            self.assertTrue(res["order_id"].startswith("synth_"))
            self.assertEqual(res["submitted_type"], "synthetic_trailing_stop")
            synth = self.state.synthetic_orders[res["order_id"]]
            self.assertEqual(synth["trail_percent"], 5.0)
            self.assertEqual(synth["high_water_mark"], 100.0)

    def test_cancel_synthetic_order(self):
        synth = self.state._register_synthetic_order(
            symbol="NVDA",
            side="sell",
            qty=2.0,
            order_type="stop_limit",
            time_in_force="day",
            stop_price=120.0,
            limit_price=119.0,
        )
        oid = synth["id"]
        self.assertIn(oid, self.state.synthetic_orders)

        cancel_res = self.state.cancel_manual_order(order_id=oid)
        self.assertEqual(cancel_res["cancelled"], 1)
        self.assertEqual(self.state.synthetic_orders[oid]["status"], "cancelled")

    def test_cancel_synthetic_orders_by_symbol(self):
        self.state._register_synthetic_order(
            symbol="TSLA",
            side="sell",
            qty=5.0,
            order_type="stop_limit",
            time_in_force="day",
            stop_price=200.0,
            limit_price=198.0,
        )
        with patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            service_instance.cancel_open_order_ids_for_symbol.return_value = []
            res = self.state.cancel_manual_order(symbol="TSLA")
            self.assertEqual(res["synthetic_cancelled"], 1)

    def test_synthetic_stop_limit_trigger_and_execution(self):
        synth = self.state._register_synthetic_order(
            symbol="AAPL",
            side="sell",
            qty=10.0,
            order_type="stop_limit",
            time_in_force="day",
            stop_price=150.0,
            limit_price=149.0,
        )
        oid = synth["id"]

        with patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            # Price above stop: should not trigger
            service_instance.get_mark_price.return_value = {"price": 152.0}
            self.state._advance_synthetic_order(oid)
            self.assertEqual(self.state.synthetic_orders[oid]["status"], "waiting")
            service_instance.trading.submit_order.assert_not_called()

            # Price drops to stop price: triggers!
            service_instance.get_mark_price.return_value = {"price": 149.5}
            mock_submitted = MagicMock()
            mock_submitted.id = "alpaca_order_123"
            service_instance.trading.submit_order.return_value = mock_submitted

            self.state._advance_synthetic_order(oid)

            self.assertEqual(self.state.synthetic_orders[oid]["status"], "triggered")
            self.assertEqual(self.state.synthetic_orders[oid]["alpaca_order_id"], "alpaca_order_123")
            service_instance.trading.submit_order.assert_called_once()
            called_req = service_instance.trading.submit_order.call_args[0][0]
            self.assertIsInstance(called_req, LimitOrderRequest)
            self.assertEqual(called_req.symbol, "AAPL")
            self.assertEqual(called_req.qty, 10.0)
            self.assertEqual(called_req.limit_price, 149.0)
            self.assertTrue(called_req.extended_hours)

    def test_synthetic_trailing_stop_trigger_and_execution(self):
        synth = self.state._register_synthetic_order(
            symbol="AMD",
            side="sell",
            qty=20.0,
            order_type="trailing_stop",
            time_in_force="gtc",
            trail_percent=10.0,
        )
        oid = synth["id"]

        with patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value

            # Price moves up to 100: high water mark = 100, stop = 90
            service_instance.get_mark_price.return_value = {"price": 100.0}
            self.state._advance_synthetic_order(oid)
            self.assertEqual(self.state.synthetic_orders[oid]["high_water_mark"], 100.0)
            self.assertEqual(self.state.synthetic_orders[oid]["stop_price"], 90.0)
            self.assertEqual(self.state.synthetic_orders[oid]["status"], "waiting")

            # Price moves up to 120: high water mark = 120, stop = 108
            service_instance.get_mark_price.return_value = {"price": 120.0}
            self.state._advance_synthetic_order(oid)
            self.assertEqual(self.state.synthetic_orders[oid]["high_water_mark"], 120.0)
            self.assertEqual(self.state.synthetic_orders[oid]["stop_price"], 108.0)
            self.assertEqual(self.state.synthetic_orders[oid]["status"], "waiting")

            # Price drops to 107 (below stop 108): triggers limit order!
            service_instance.get_mark_price.return_value = {"price": 107.0}
            mock_submitted = MagicMock()
            mock_submitted.id = "alpaca_amd_456"
            service_instance.trading.submit_order.return_value = mock_submitted

            self.state._advance_synthetic_order(oid)
            self.assertEqual(self.state.synthetic_orders[oid]["status"], "triggered")
            self.assertEqual(self.state.synthetic_orders[oid]["alpaca_order_id"], "alpaca_amd_456")

    def test_blotter_list_orders_includes_synthetic(self):
        synth = self.state._register_synthetic_order(
            symbol="QQQ",
            side="sell",
            qty=15.0,
            order_type="stop_limit",
            time_in_force="day",
            stop_price=450.0,
            limit_price=448.0,
        )
        with patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            service_instance.list_orders.return_value = []
            data = self.state.list_orders(status="open")
            self.assertTrue(any(o["id"] == synth["id"] for o in data["orders"]))
            found = next(o for o in data["orders"] if o["id"] == synth["id"])
            self.assertTrue(found["is_synthetic"])
            self.assertTrue(found["extended_hours"])

    def test_synthetic_day_order_expiry(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")

        # Order created Monday 2:00 PM ET
        created_dt = datetime(2026, 9, 7, 14, 0, tzinfo=et)
        created_ts = created_dt.timestamp()

        # Before 8:00 PM: not expired
        now_dt_before = datetime(2026, 9, 7, 19, 59, tzinfo=et)
        self.assertFalse(synthetic_order_store.is_day_order_expired(created_ts, now_dt_before))

        # At or after 8:00 PM: expired
        now_dt_after = datetime(2026, 9, 7, 20, 0, tzinfo=et)
        self.assertTrue(synthetic_order_store.is_day_order_expired(created_ts, now_dt_after))

        # Order created in state and advanced past expiry
        synth = self.state._register_synthetic_order(
            symbol="SPY",
            side="sell",
            qty=10.0,
            order_type="stop_limit",
            time_in_force="day",
            stop_price=500.0,
            limit_price=498.0,
        )
        oid = synth["id"]
        # Set created_at to 2 days ago
        with self.state.lock:
            self.state.synthetic_orders[oid]["created_at"] = time.time() - (86400 * 2)

        with patch("bot.web_state.AlpacaService") as MockService:
            self.state._advance_synthetic_order(oid)
            self.assertEqual(self.state.synthetic_orders[oid]["status"], "expired")

    def test_replace_synthetic_order(self):
        synth = self.state._register_synthetic_order(
            symbol="META",
            side="sell",
            qty=10.0,
            order_type="stop_limit",
            time_in_force="day",
            stop_price=500.0,
            limit_price=495.0,
        )
        oid = synth["id"]

        with patch.object(self.state, "_require_manual_book_control"), \
             patch.object(self.state, "_require_live_execution"):
            res = self.state.replace_manual_order(
                order_id=oid,
                qty=12.0,
                stop_price=505.0,
                limit_price=500.0,
                time_in_force="gtc",
            )
            self.assertEqual(res["order"]["id"], oid)
            self.assertEqual(self.state.synthetic_orders[oid]["qty"], 12.0)
            self.assertEqual(self.state.synthetic_orders[oid]["stop_price"], 505.0)
            self.assertEqual(self.state.synthetic_orders[oid]["limit_price"], 500.0)
            self.assertEqual(self.state.synthetic_orders[oid]["time_in_force"], "gtc")

    def test_cancel_triggered_synthetic_cancels_broker_order(self):
        synth = self.state._register_synthetic_order(
            symbol="GOOGL",
            side="sell",
            qty=5.0,
            order_type="stop_limit",
            time_in_force="day",
            stop_price=160.0,
            limit_price=159.0,
        )
        oid = synth["id"]
        with self.state.lock:
            self.state.synthetic_orders[oid]["status"] = "triggered"
            self.state.synthetic_orders[oid]["alpaca_order_id"] = "alpaca_googl_999"

        with patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            self.state.cancel_synthetic_order(oid)
            service_instance.cancel_order.assert_called_once_with("alpaca_googl_999")
            self.assertEqual(self.state.synthetic_orders[oid]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
