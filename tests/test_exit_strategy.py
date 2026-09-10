import unittest
from unittest.mock import MagicMock, patch

from bot.webapp import ManageStopIn
from bot.web_state import AppState


class TestExitStrategy(unittest.TestCase):
    def setUp(self):
        self.state = AppState(user_id="test_user")
        self.state.loop_running = False

    def test_manage_stop_in_schema_validation(self):
        # Valid actions
        m1 = ManageStopIn(symbol="AAPL", action="breakeven")
        self.assertEqual(m1.action, "breakeven")

        m2 = ManageStopIn(symbol="AAPL", action="take_profit", take_profit_price=160.0)
        self.assertEqual(m2.take_profit_price, 160.0)

        m3 = ManageStopIn(symbol="AAPL", action="bracket", stop_price=140.0, take_profit_price=160.0)
        self.assertEqual(m3.stop_price, 140.0)
        self.assertEqual(m3.take_profit_price, 160.0)

        m4 = ManageStopIn(symbol="AAPL", action="cancel_all")
        self.assertEqual(m4.action, "cancel_all")

        # Invalid action should raise ValueError
        with self.assertRaises(Exception):
            ManageStopIn(symbol="AAPL", action="invalid_action")

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_price(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0
        mock_service.get_mark_price.return_value = {"price": 150.0}
        mock_service.replace_stop_loss.return_value = {"id": "ord_1", "stop_price": 145.0}

        res = self.state.manage_position_stop(
            symbol="AAPL",
            action="price",
            stop_price=145.0,
        )
        self.assertTrue(res["stop"])
        self.assertEqual(res["stop"]["stop_price"], 145.0)
        mock_service.replace_stop_loss.assert_called_once_with("AAPL", 145.0)

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_breakeven_success(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0  # Long
        mock_service.get_mark_price.return_value = {"price": 160.0}
        mock_service.get_avg_entry_price.return_value = 150.0
        mock_service.replace_stop_loss.return_value = {"id": "ord_be", "stop_price": 149.99}

        res = self.state.manage_position_stop(
            symbol="AAPL",
            action="breakeven",
        )
        self.assertEqual(res["action"], "breakeven")
        mock_service.replace_stop_loss.assert_called_once_with("AAPL", 149.99)

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_breakeven_underwater(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0  # Long
        mock_service.get_mark_price.return_value = {"price": 140.0}  # Underwater
        mock_service.get_avg_entry_price.return_value = 150.0

        with self.assertRaises(ValueError) as ctx:
            self.state.manage_position_stop(
                symbol="AAPL",
                action="breakeven",
            )
        self.assertIn("underwater", str(ctx.exception).lower())

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_take_profit_long(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0  # Long
        mock_service.get_mark_price.return_value = {"price": 150.0}
        mock_service.arm_take_profit.return_value = {"id": "ord_tp", "limit_price": 165.0}

        res = self.state.manage_position_stop(
            symbol="AAPL",
            action="take_profit",
            take_profit_price=165.0,
        )
        self.assertEqual(res["action"], "take_profit")
        self.assertEqual(res["take_profit"]["limit_price"], 165.0)
        mock_service.arm_take_profit.assert_called_once_with("AAPL", 165.0)

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_take_profit_invalid_price(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0  # Long
        mock_service.get_mark_price.return_value = {"price": 150.0}

        # Long take profit below market should fail
        with self.assertRaises(ValueError) as ctx:
            self.state.manage_position_stop(
                symbol="AAPL",
                action="take_profit",
                take_profit_price=140.0,
            )
        self.assertIn("above the market", str(ctx.exception))

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_bracket(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0  # Long
        mock_service.get_mark_price.return_value = {"price": 150.0}
        mock_service.arm_bracket_exit.return_value = {
            "symbol": "AAPL",
            "action": "bracket",
            "order_id": "ord_oco_1",
            "order_class": "oco",
            "stop": {"id": "ord_sl", "stop_price": 145.0},
            "take_profit": {"id": "ord_tp", "limit_price": 165.0},
        }

        res = self.state.manage_position_stop(
            symbol="AAPL",
            action="bracket",
            stop_price=145.0,
            take_profit_price=165.0,
        )
        self.assertEqual(res["action"], "bracket")
        self.assertIsNotNone(res["stop"])
        self.assertIsNotNone(res["take_profit"])
        self.assertEqual(res["order_id"], "ord_oco_1")
        self.assertEqual(res["order_class"], "oco")
        mock_service.arm_bracket_exit.assert_called_once_with(
            "AAPL",
            stop_price=145.0,
            take_profit_price=165.0,
            trail_percent=None,
            qty=None,
        )

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_cancellations(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.cancel_open_stop_orders.return_value = 1
        mock_service.cancel_open_take_profit_orders.return_value = 1
        mock_service.cancel_open_exit_orders.return_value = {
            "stops_cancelled": 1,
            "take_profits_cancelled": 1,
        }

        r1 = self.state.manage_position_stop(symbol="AAPL", action="cancel_stops")
        self.assertEqual(r1["cancelled_count"], 1)

        r2 = self.state.manage_position_stop(symbol="AAPL", action="cancel_take_profit")
        self.assertEqual(r2["cancelled_count"], 1)

        r3 = self.state.manage_position_stop(symbol="AAPL", action="cancel_all")
        self.assertEqual(r3["stops_cancelled"], 1)
        self.assertEqual(r3["take_profits_cancelled"], 1)

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_with_custom_qty(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 240.0
        mock_service.get_mark_price.return_value = {"price": 150.0}
        mock_service.replace_stop_loss.return_value = {"id": "ord_1", "stop_price": 145.0, "qty": 120.0}

        # Sizing partial 120 shares of 240 held
        res = self.state.manage_position_stop(
            symbol="AAPL",
            action="price",
            stop_price=145.0,
            qty=120.0,
        )
        self.assertTrue(res["stop"])
        mock_service.replace_stop_loss.assert_called_once_with("AAPL", 145.0, qty=120.0)

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_invalid_qty(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 50.0
        mock_service.get_mark_price.return_value = {"price": 150.0}

        # Quantity exceeds position
        with self.assertRaises(ValueError) as ctx:
            self.state.manage_position_stop(
                symbol="AAPL",
                action="price",
                stop_price=145.0,
                qty=100.0,
            )
        self.assertIn("cannot exceed", str(ctx.exception).lower())

        # Quantity is zero or negative
        with self.assertRaises(ValueError) as ctx2:
            self.state.manage_position_stop(
                symbol="AAPL",
                action="price",
                stop_price=145.0,
                qty=0.0,
            )
        self.assertIn("greater than 0", str(ctx2.exception).lower())


class TestArmBracketExit(unittest.TestCase):
    def setUp(self):
        from bot.client import AlpacaService
        from bot.config import Config
        self.cfg = Config.default()
        self.service = AlpacaService(self.cfg)
        self.mock_trading = MagicMock()
        self.service._trading = self.mock_trading

    def test_arm_bracket_exit_oco_success(self):
        from alpaca.trading.enums import OrderClass, OrderSide, OrderType

        self.service.get_position_qty = MagicMock(return_value=6.0)
        self.service.cancel_open_exit_orders = MagicMock(return_value={"stops_cancelled": 0, "take_profits_cancelled": 0})
        mock_submitted = MagicMock()
        mock_submitted.id = "order_oco_123"
        mock_submitted.legs = []
        self.mock_trading.submit_order.return_value = mock_submitted

        res = self.service.arm_bracket_exit(
            "DELL",
            stop_price=494.02,
            take_profit_price=560.23,
            qty=6.0,
        )

        self.service.cancel_open_exit_orders.assert_called_once_with("DELL")
        self.mock_trading.submit_order.assert_called_once()
        submitted_req = self.mock_trading.submit_order.call_args[0][0]

        self.assertEqual(submitted_req.symbol, "DELL")
        self.assertEqual(submitted_req.qty, 6.0)
        self.assertEqual(submitted_req.side, OrderSide.SELL)
        self.assertEqual(submitted_req.type, OrderType.LIMIT)
        self.assertEqual(submitted_req.order_class, OrderClass.OCO)
        self.assertEqual(submitted_req.take_profit.limit_price, 560.23)
        self.assertEqual(submitted_req.stop_loss.stop_price, 494.02)

        self.assertEqual(res["symbol"], "DELL")
        self.assertEqual(res["order_class"], "oco")
        self.assertEqual(res["order_id"], "order_oco_123")
        self.assertEqual(res["stop"]["stop_price"], 494.02)
        self.assertEqual(res["take_profit"]["limit_price"], 560.23)

    def test_arm_bracket_exit_short_position(self):
        from alpaca.trading.enums import OrderSide

        self.service.get_position_qty = MagicMock(return_value=-10.0)
        self.service.cancel_open_exit_orders = MagicMock(return_value={})
        mock_submitted = MagicMock()
        mock_submitted.id = "order_short_oco"
        mock_submitted.legs = []
        self.mock_trading.submit_order.return_value = mock_submitted

        res = self.service.arm_bracket_exit(
            "SPY",
            stop_price=550.0,
            take_profit_price=500.0,
            qty=5.0,
        )

        submitted_req = self.mock_trading.submit_order.call_args[0][0]
        self.assertEqual(submitted_req.side, OrderSide.BUY)
        self.assertEqual(submitted_req.qty, 5.0)

    def test_arm_bracket_exit_trail_and_tp_conflict(self):
        self.service.get_position_qty = MagicMock(return_value=10.0)
        with self.assertRaises(ValueError) as ctx:
            self.service.arm_bracket_exit(
                "AAPL",
                trail_percent=3.0,
                take_profit_price=160.0,
            )
        self.assertIn("trailing stop cannot be combined", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()


