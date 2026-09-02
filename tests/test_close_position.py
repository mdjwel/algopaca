import unittest
from unittest.mock import MagicMock, patch

from bot.alpaca_errors import humanize_alpaca_error
from bot.client import AlpacaService
from bot.config import Config


class TestClosePosition(unittest.TestCase):
    def test_humanize_alpaca_error_insufficient_qty_with_held_orders(self):
        raw_error = (
            '{"available":"12","code":40310000,"existing_qty":"62",'
            '"held_for_orders":"50","message":"insufficient qty available for order '
            '(requested: 34, available: 12)","symbol":"INTW"}'
        )
        humanized = humanize_alpaca_error(raw_error)
        self.assertIn("Insufficient qty available for order", humanized)
        self.assertIn("50 shares", humanized)
        self.assertIn("12 available", humanized)
        self.assertIn("Cancel resting open orders", humanized)

    def test_humanize_alpaca_error_buying_power(self):
        raw_error = '{"code":40310000,"message":"insufficient buying power"}'
        humanized = humanize_alpaca_error(raw_error)
        self.assertIn("Insufficient buying power.", humanized)
        self.assertIn("Lower the risk % or free up cash", humanized)

    def test_humanize_alpaca_error_fractional(self):
        raw_error = '{"code":42210000,"message":"fractional orders must be simple orders"}'
        humanized = humanize_alpaca_error(raw_error)
        self.assertIn("Fractional orders must be simple orders.", humanized)
        self.assertIn("whole shares", humanized)

    def test_humanize_alpaca_error_plain_text(self):
        self.assertEqual(humanize_alpaca_error("Simple error message"), "Simple error message")

    @patch("bot.client.TradingClient")
    def test_close_position_cancels_orders_and_legs(self, mock_trading_cls):
        mock_trading = MagicMock()
        cfg = Config.default()
        service = AlpacaService(cfg)
        service._trading = mock_trading

        parent_order = MagicMock()
        parent_order.id = "parent_1"
        leg_order = MagicMock()
        leg_order.id = "leg_1"
        parent_order.legs = [leg_order]

        mock_trading.get_orders.return_value = [parent_order]
        mock_trading.close_position.return_value = {"id": "close_order_id", "symbol": "INTW", "status": "submitted"}

        res = service.close_position("INTW", qty=34, cancel_orders=True)
        self.assertEqual(res["symbol"], "INTW")

        # Verify cancel_order_by_id was called for both parent and child leg
        mock_trading.cancel_order_by_id.assert_any_call("parent_1")
        mock_trading.cancel_order_by_id.assert_any_call("leg_1")
        mock_trading.close_position.assert_called_once()

    @patch("bot.client.TradingClient")
    @patch("bot.client._time.sleep")
    def test_close_position_retries_on_held_orders(self, mock_sleep, mock_trading_cls):
        mock_trading = MagicMock()
        cfg = Config.default()
        service = AlpacaService(cfg)
        service._trading = mock_trading

        mock_trading.get_orders.return_value = []
        # First call fails with 40310000, second call succeeds
        err = Exception('{"available":"12","code":40310000,"existing_qty":"62","held_for_orders":"50","message":"insufficient qty available for order"}')
        mock_trading.close_position.side_effect = [
            err,
            {"id": "close_order_id", "symbol": "INTW", "status": "submitted"},
        ]

        res = service.close_position("INTW", qty=34, cancel_orders=True)
        self.assertEqual(res["symbol"], "INTW")
        self.assertEqual(mock_trading.close_position.call_count, 2)
        mock_sleep.assert_called()


if __name__ == "__main__":
    unittest.main()
