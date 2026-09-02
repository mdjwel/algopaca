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
        mock_service.replace_stop_loss.return_value = {"id": "ord_sl", "stop_price": 145.0}
        mock_service.arm_take_profit.return_value = {"id": "ord_tp", "limit_price": 165.0}

        res = self.state.manage_position_stop(
            symbol="AAPL",
            action="bracket",
            stop_price=145.0,
            take_profit_price=165.0,
        )
        self.assertEqual(res["action"], "bracket")
        self.assertIsNotNone(res["stop"])
        self.assertIsNotNone(res["take_profit"])
        mock_service.replace_stop_loss.assert_called_once_with("AAPL", 145.0)
        mock_service.arm_take_profit.assert_called_once_with("AAPL", 165.0)

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


if __name__ == "__main__":
    unittest.main()
