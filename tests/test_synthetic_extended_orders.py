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
        self.state._synthetic_order_stop.set()
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
        with patch.object(self.state, "_start_synthetic_order_watcher"):
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
            # Set created_at to 7 days ago (guaranteed expired even across weekends)
            with self.state.lock:
                self.state.synthetic_orders[oid]["created_at"] = time.time() - (86400 * 7)

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

    def test_strategy_ensure_stop_loss_syncs_24h_synthetic_stop(self):
        from bot.client import AlpacaService
        config = Config.default(stop_loss_pct=5.0, stop_loss_24h=True)
        service = AlpacaService(config, synthetic_order_handler=self.state)

        with patch.object(self.state, "_start_synthetic_order_watcher"), \
             patch.object(service, "get_position_qty", return_value=10.0), \
             patch.object(service, "get_avg_entry_price", return_value=200.0), \
             patch.object(service, "has_open_stop_sell", return_value=False), \
             patch.object(service, "_trading") as mock_trading:
            mock_trading.submit_order.return_value = MagicMock(id="alpaca_stop_123")

            info = service.ensure_stop_loss("TSLA", pct=5.0)
            self.assertIsNotNone(info)
            self.assertIn("synthetic_order_id", info)

            synth_id = info["synthetic_order_id"]
            self.assertIn(synth_id, self.state.synthetic_orders)
            synth = self.state.synthetic_orders[synth_id]
            self.assertEqual(synth["symbol"], "TSLA")
            self.assertEqual(synth["side"], "sell")
            self.assertEqual(synth["qty"], 10.0)
            self.assertEqual(synth["stop_price"], 190.0)
            self.assertEqual(synth["time_in_force"], "gtc")
            self.assertTrue(synth["extended_hours"])

    def test_strategy_replace_stop_loss_syncs_24h_synthetic_stop(self):
        from bot.client import AlpacaService
        config = Config.default(stop_loss_24h=True)
        service = AlpacaService(config, synthetic_order_handler=self.state)

        with patch.object(self.state, "_start_synthetic_order_watcher"):
            # Initial synthetic stop
            self.state.sync_strategy_stop(
                symbol="TSLA",
                side="sell",
                qty=10.0,
                stop_price=190.0,
                limit_price=189.0,
            )

            with patch.object(service, "get_position_qty", return_value=10.0), \
                 patch.object(service, "cancel_open_stop_orders", return_value=1), \
                 patch.object(service, "_trading") as mock_trading:
                mock_trading.submit_order.return_value = MagicMock(id="alpaca_stop_moved")

                info = service.replace_stop_loss("TSLA", 195.0)
                self.assertIsNotNone(info)
                self.assertIn("synthetic_order_id", info)

                synth = self.state.synthetic_orders[info["synthetic_order_id"]]
                self.assertEqual(synth["stop_price"], 195.0)

    def test_strategy_trailing_stop_syncs_24h_synthetic_stop(self):
        from bot.client import AlpacaService
        config = Config.default(stop_loss_24h=True)
        service = AlpacaService(config, synthetic_order_handler=self.state)

        with patch.object(self.state, "_start_synthetic_order_watcher"), \
             patch.object(service, "get_position_qty", return_value=15.0), \
             patch.object(service, "cancel_open_stop_orders", return_value=0), \
             patch.object(service, "get_mark_price", return_value={"price": 120.0}), \
             patch.object(service, "_trading") as mock_trading:
            mock_trading.submit_order.return_value = MagicMock(id="alpaca_trail_123")

            info = service.arm_trailing_stop("NVDA", trail_percent=3.5)
            self.assertIsNotNone(info)
            self.assertIn("synthetic_order_id", info)

            synth = self.state.synthetic_orders[info["synthetic_order_id"]]
            self.assertEqual(synth["order_type"], "trailing_stop")
            self.assertEqual(synth["trail_percent"], 3.5)
            self.assertEqual(synth["time_in_force"], "gtc")
            self.assertTrue(synth["extended_hours"])

    def test_strategy_cancel_open_stops_cleans_synthetic_orders(self):
        from bot.client import AlpacaService
        config = Config.default(stop_loss_24h=True)
        service = AlpacaService(config, synthetic_order_handler=self.state)

        with patch.object(self.state, "_start_synthetic_order_watcher"):
            self.state.sync_strategy_stop(
                symbol="AMD",
                side="sell",
                qty=20.0,
                stop_price=140.0,
                limit_price=139.0,
            )

            with patch.object(service, "_open_orders", return_value=[]):
                cancelled = service.cancel_open_stop_orders("AMD")
                self.assertGreaterEqual(cancelled, 1)

                # Check that synthetic order is cancelled
                for o in self.state.synthetic_orders.values():
                    if o.get("symbol") == "AMD":
                        self.assertEqual(o["status"], "cancelled")

    def test_synthetic_advance_settles_if_position_closed(self):
        from bot.client import AlpacaService
        synth = self.state._register_synthetic_order(
            symbol="INTC",
            side="sell",
            qty=50.0,
            order_type="stop_limit",
            time_in_force="gtc",
            stop_price=20.0,
            limit_price=19.9,
        )
        oid = synth["id"]

        with patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            # Position is already closed at broker (qty = 0)
            service_instance.get_position_qty.return_value = 0.0

            self.state._advance_synthetic_order(oid)
            self.assertEqual(self.state.synthetic_orders[oid]["status"], "cancelled")
            self.assertIn("Position for INTC is closed", self.state.synthetic_orders[oid]["message"])

    def test_synthetic_trigger_cancels_resting_stops_and_submits_limit(self):
        synth = self.state._register_synthetic_order(
            symbol="PLTR",
            side="sell",
            qty=30.0,
            order_type="stop_limit",
            time_in_force="gtc",
            stop_price=30.0,
            limit_price=29.5,
        )
        oid = synth["id"]

        with patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            service_instance.get_position_qty.return_value = 30.0
            service_instance.get_mark_price.return_value = {"price": 29.80}  # Below 30.0 -> trigger
            service_instance.trading.submit_order.return_value = MagicMock(id="alpaca_limit_exec_999")

            self.state._advance_synthetic_order(oid)

            # Check that resting stops were cancelled first to free shares
            service_instance.cancel_open_stop_orders.assert_called_once_with("PLTR")
            # Check limit order submitted with DAY and extended_hours=True
            args, _ = service_instance.trading.submit_order.call_args
            submitted_req = args[0]
            self.assertIsInstance(submitted_req, LimitOrderRequest)
            self.assertEqual(submitted_req.symbol, "PLTR")
            self.assertEqual(submitted_req.time_in_force, TimeInForce.DAY)
            self.assertTrue(submitted_req.extended_hours)
            self.assertEqual(self.state.synthetic_orders[oid]["status"], "triggered")
            self.assertEqual(self.state.synthetic_orders[oid]["alpaca_order_id"], "alpaca_limit_exec_999")

    def test_sync_strategy_stop_with_reversal_buy(self):
        registered = self.state.sync_strategy_stop(
            symbol="GLD",
            side="buy",
            qty=12.0,
            stop_price=250.0,
            limit_price=251.25,
            source="event_5m_CPI",
            reversal_buy=True,
            reversal_qty=12.0,
            reversal_event_title="CPI Report",
        )
        self.assertIsNotNone(registered)
        oid = registered["id"]
        live = self.state.synthetic_orders[oid]
        self.assertTrue(live["reversal_buy"])
        self.assertEqual(live["reversal_qty"], 12.0)
        self.assertEqual(live["reversal_event_title"], "CPI Report")

    def test_reversal_buy_on_stop_fill(self):
        registered = self.state.sync_strategy_stop(
            symbol="GLD",
            side="buy",
            qty=10.0,
            stop_price=250.0,
            limit_price=251.25,
            source="event_5m_CPI",
            reversal_buy=True,
            reversal_qty=10.0,
            reversal_event_title="CPI Report",
        )
        oid = registered["id"]
        self.state.synthetic_orders[oid]["status"] = "triggered"
        self.state.synthetic_orders[oid]["alpaca_order_id"] = "alpaca_cover_777"

        with patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            # The stop-loss limit order covering the short fills:
            service_instance.get_order_snapshot.return_value = {
                "id": "alpaca_cover_777",
                "status": "filled",
                "filled_avg_price": 250.50,
            }
            service_instance.get_position_qty.return_value = 0.0  # Flat after cover
            service_instance.get_mark_price.return_value = {"price": 250.50}
            mock_buy_order = MagicMock()
            mock_buy_order.id = "reversal_buy_ord_888"
            service_instance.submit_order.return_value = mock_buy_order

            self.state._advance_synthetic_order(oid)

            # Synthetic order settled as filled
            self.assertEqual(self.state.synthetic_orders[oid]["status"], "filled")

            # Reversal buy order submitted
            service_instance.submit_order.assert_called_once()
            call_kwargs = service_instance.submit_order.call_args[1]
            self.assertEqual(call_kwargs["symbol"], "GLD")
            self.assertEqual(call_kwargs["qty"], 10.0)
            self.assertEqual(call_kwargs["side"], OrderSide.BUY)
            self.assertTrue(call_kwargs["protect"])
            # Stop price calculated below 250.50 (0.8% below = ~248.50)
            self.assertAlmostEqual(call_kwargs["stop_price"], 248.50, delta=0.2)

            # Verify trade was recorded in session results
            all_trades = [t for s in self.state.loop_sessions for t in s.get("results", [])]
            reversal_trades = [t for t in all_trades if t.get("symbol") == "GLD" and t.get("intent") == "reversal_buy"]
            self.assertEqual(len(reversal_trades), 1)
            self.assertEqual(reversal_trades[0]["symbol"], "GLD")
            self.assertEqual(reversal_trades[0]["signal"], "buy")
            self.assertEqual(reversal_trades[0]["order_id"], "reversal_buy_ord_888")
            self.assertIn("Event Stop Loss Reversal", reversal_trades[0]["reason"])

            # A protective 24h stop is armed for the fresh long position, and it
            # must not itself carry a reversal flag (no infinite flip loop).
            long_stops = [
                o
                for o in self.state.synthetic_orders.values()
                if o.get("symbol") == "GLD"
                and o.get("side") == "sell"
                and o.get("status") in synthetic_order_store.ACTIVE_STATUSES
            ]
            self.assertEqual(len(long_stops), 1)
            self.assertEqual(long_stops[0]["qty"], 10.0)
            self.assertAlmostEqual(long_stops[0]["stop_price"], 248.50, delta=0.2)
            self.assertLess(long_stops[0]["limit_price"], long_stops[0]["stop_price"])
            self.assertFalse(long_stops[0].get("reversal_buy"))

    def test_reversal_buy_fires_only_once_per_stop(self):
        registered = self.state.sync_strategy_stop(
            symbol="GLD",
            side="buy",
            qty=10.0,
            stop_price=250.0,
            limit_price=251.25,
            source="event_5m_CPI",
            reversal_buy=True,
            reversal_qty=10.0,
            reversal_event_title="CPI Report",
        )
        oid = registered["id"]
        snapshot = dict(self.state.synthetic_orders[oid])

        with patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            service_instance.get_position_qty.return_value = 0.0
            service_instance.get_mark_price.return_value = {"price": 250.50}
            mock_buy_order = MagicMock()
            mock_buy_order.id = "reversal_buy_ord_999"
            service_instance.submit_order.return_value = mock_buy_order

            first = self.state._execute_reversal_buy(
                order_id=oid,
                snapshot=snapshot,
                service=service_instance,
                fill_price=250.50,
            )
            second = self.state._execute_reversal_buy(
                order_id=oid,
                snapshot=snapshot,
                service=service_instance,
                fill_price=250.50,
            )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        service_instance.submit_order.assert_called_once()

    def test_place_manual_order_while_loop_running(self):
        """Placing an advanced order should not be blocked even when auto trade is running."""
        self.state.loop_running = True
        with patch.object(self.state, "_base_config", return_value=Config.default()), \
             patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            service_instance.has_position.return_value = False
            service_instance.get_mark_price.return_value = {"price": 100.0}
            mock_order = MagicMock()
            mock_order.id = "mock_order_123"
            mock_order.status = "new"
            mock_order.symbol = "AAPL"
            mock_order.side = MagicMock()
            mock_order.side.value = "buy"
            mock_order.qty = 10.0
            mock_order.type = "limit"
            mock_order.limit_price = 99.0
            service_instance.submit_manual_order.return_value = (mock_order, None)

            res = self.state.place_manual_order(
                symbol="AAPL",
                side="buy",
                order_type="limit",
                limit_price=99.0,
                qty=10.0,
                time_in_force="day",
                stop_loss_pct=0,
            )
            self.assertEqual(res["order_id"], "mock_order_123")
            self.assertEqual(res["symbol"], "AAPL")

    def test_place_stop_order_trigger_direction_validation(self):
        """Buy stop trigger must be above mark price, sell stop trigger must be below mark price."""
        with patch.object(self.state, "_base_config", return_value=Config.default()), \
             patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            service_instance.has_position.return_value = True
            service_instance.get_position_qty.return_value = 10.0
            service_instance.get_mark_price.return_value = {"price": 150.0}

            # 1. Buy stop with trigger below mark price should fail
            with self.assertRaises(ValueError) as ctx:
                self.state.place_manual_order(
                    symbol="AAPL",
                    side="buy",
                    order_type="stop_limit",
                    qty=5.0,
                    stop_price=145.0,
                    limit_price=146.0,
                    time_in_force="day",
                    extended_hours=True,
                )
            self.assertIn("must be above current market price", str(ctx.exception))

            # 2. Sell stop with trigger above mark price should fail
            with self.assertRaises(ValueError) as ctx:
                self.state.place_manual_order(
                    symbol="AAPL",
                    side="sell",
                    order_type="stop_limit",
                    qty=5.0,
                    stop_price=155.0,
                    limit_price=154.0,
                    time_in_force="day",
                    extended_hours=True,
                )
            self.assertIn("must be below current market price", str(ctx.exception))

            # 3. Valid Buy stop with trigger above mark price should succeed
            buy_res = self.state.place_manual_order(
                symbol="AAPL",
                side="buy",
                order_type="stop_limit",
                qty=5.0,
                stop_price=155.0,
                limit_price=156.0,
                time_in_force="day",
                extended_hours=True,
            )
            self.assertTrue(buy_res["order_id"].startswith("synth_"))
            self.assertEqual(buy_res["submitted_type"], "synthetic_stop_limit")

            # 4. Valid Sell stop with trigger below mark price should succeed
            sell_res = self.state.place_manual_order(
                symbol="AAPL",
                side="sell",
                order_type="stop_limit",
                qty=5.0,
                stop_price=145.0,
                limit_price=144.0,
                time_in_force="day",
                extended_hours=True,
            )
            self.assertTrue(sell_res["order_id"].startswith("synth_"))
            self.assertEqual(sell_res["submitted_type"], "synthetic_stop_limit")

    def test_sync_strategy_take_profit(self):
        """sync_strategy_take_profit registers and updates 24h synthetic take profit orders."""
        res = self.state.sync_strategy_take_profit(
            symbol="AAPL",
            side="sell",
            qty=10.0,
            limit_price=160.0,
            source="test_tp",
        )
        self.assertIsNotNone(res)
        order_id = res["id"]
        self.assertEqual(res["order_type"], "take_profit")
        self.assertEqual(res["limit_price"], 160.0)
        self.assertTrue(res["extended_hours"])
        self.assertEqual(res["status"], "waiting")
        self.assertIn(order_id, self.state.synthetic_orders)

        # Update existing take profit order
        updated = self.state.sync_strategy_take_profit(
            symbol="AAPL",
            side="sell",
            qty=10.0,
            limit_price=165.0,
            source="test_tp_update",
        )
        self.assertEqual(updated["id"], order_id)
        self.assertEqual(updated["limit_price"], 165.0)

    def test_advance_synthetic_take_profit_triggered(self):
        """Synthetic take profit triggers when mark price hits target price in extended hours."""
        synth = self.state._register_synthetic_order(
            symbol="NVDA",
            side="sell",
            qty=5.0,
            order_type="take_profit",
            time_in_force="gtc",
            limit_price=120.0,
        )
        order_id = synth["id"]

        with patch("bot.web_state.AlpacaService") as MockService:
            service_instance = MockService.return_value
            service_instance.get_position_qty.return_value = 5.0
            # Price below target: should not trigger
            service_instance.get_mark_price.return_value = {"price": 118.0}
            self.state._advance_synthetic_order(order_id)
            self.assertEqual(self.state.synthetic_orders[order_id]["status"], "waiting")
            service_instance.trading.submit_order.assert_not_called()

            # Price rises above target: triggers!
            service_instance.get_mark_price.return_value = {"price": 121.0}
            fake_submitted = MagicMock()
            fake_submitted.id = "alp_tp_sub_123"
            service_instance.trading.submit_order.return_value = fake_submitted

            self.state._advance_synthetic_order(order_id)

            service_instance.trading.submit_order.assert_called_once()
            called_req = service_instance.trading.submit_order.call_args[0][0]
            self.assertEqual(called_req.symbol, "NVDA")
            self.assertEqual(called_req.qty, 5.0)
            self.assertEqual(called_req.side, OrderSide.SELL)
            self.assertEqual(called_req.limit_price, 120.0)
            self.assertEqual(called_req.time_in_force, TimeInForce.DAY)
            self.assertTrue(called_req.extended_hours)

            order = self.state.synthetic_orders[order_id]
            self.assertEqual(order["status"], "triggered")
            self.assertEqual(order["alpaca_order_id"], "alp_tp_sub_123")


if __name__ == "__main__":
    unittest.main()


