import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from bot.auth import AuthStore
from bot.web_state import AppState


class TestPositionLotsDeskAverage(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.tmp_dir.name)
        self.state = AppState(user_id="test_user", workspace_dir=self.workspace_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    @patch("bot.web_state.AlpacaService")
    def test_position_lots_calculates_desk_average_entry(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service

        # Alpaca reports position with incorrect avg_entry_price 532.33 for DELL short 9 shares
        mock_service.get_all_positions.return_value = [
            {
                "symbol": "DELL",
                "qty": 9,
                "side": "short",
                "current_price": 526.84,
                "avg_entry_price": 532.33,
            }
        ]

        # 6 lots matching user screenshot
        # Total cost: 514.50 + 513.25 + 1064.00 + 1086.00 + 1124.26 + 565.00 = 4867.01
        # Average: 4867.01 / 9 = 540.77888... (~540.7789)
        mock_service.fetch_closed_order_window.return_value = {
            "orders": [],
            "truncated": False,
        }
        mock_service_cls.open_lots_from_orders.return_value = {
            "DELL": [
                {"direction": -1, "qty": 1.0, "price": 514.50, "order_id": "1", "opened_at": "2026-09-11T15:00:00Z"},
                {"direction": -1, "qty": 1.0, "price": 513.25, "order_id": "2", "opened_at": "2026-09-11T15:08:00Z"},
                {"direction": -1, "qty": 2.0, "price": 532.00, "order_id": "3", "opened_at": "2026-09-11T19:32:00Z"},
                {"direction": -1, "qty": 2.0, "price": 543.00, "order_id": "4", "opened_at": "2026-09-11T19:44:00Z"},
                {"direction": -1, "qty": 2.0, "price": 562.13, "order_id": "5", "opened_at": "2026-09-11T20:10:00Z"},
                {"direction": -1, "qty": 1.0, "price": 565.00, "order_id": "6", "opened_at": "2026-09-11T20:26:00Z"},
            ]
        }

        data = self.state.position_lots("DELL")

        self.assertEqual(data["symbol"], "DELL")
        self.assertEqual(data["lot_count"], 6)
        self.assertEqual(data["total_qty"], 9.0)
        self.assertAlmostEqual(data["total_cost_basis"], 4867.01, places=2)

        # Desk calculated average entry must be ~540.7789 ($540.78), NOT Alpaca's 532.33
        self.assertAlmostEqual(data["avg_entry_price"], 540.7789, places=3)
        self.assertAlmostEqual(data["desk_avg_entry_price"], 540.7789, places=3)
        self.assertAlmostEqual(data["weighted_avg_price"], 540.7789, places=3)
        self.assertEqual(data["alpaca_avg_entry_price"], 532.33)

        # Cache must be populated
        self.assertIn("DELL", self.state._desk_avg_entries)
        self.assertAlmostEqual(self.state._desk_avg_entries["DELL"]["avg_entry_price"], 540.7789, places=3)

    @patch("bot.web_state.AlpacaService")
    def test_positions_overview_uses_desk_average_entry(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service

        mock_service.get_all_positions.return_value = [
            {
                "symbol": "DELL",
                "qty": 9,
                "side": "short",
                "current_price": 526.84,
                "avg_entry_price": 532.33,
                "cost_basis": 4790.97,
                "unrealized_pl": 49.41,
                "market_value": -4741.56,
            }
        ]
        mock_service.account_summary.return_value = {
            "equity": 25000.0,
            "cash": 20000.0,
            "buying_power": 50000.0,
        }
        mock_service.get_open_orders_summary.return_value = {}

        # Pre-seed cached desk calculation
        self.state._desk_avg_entries["DELL"] = {
            "avg_entry_price": 540.7789,
            "cost_basis": 4867.01,
            "unrealized_pl": 125.48,
            "qty": 9.0,
            "side": "short",
        }

        overview = self.state.positions_overview()
        dell = next(p for p in overview["positions"] if p["symbol"] == "DELL")

        # avg_entry_price in overview must be the desk's average entry
        self.assertAlmostEqual(dell["avg_entry_price"], 540.7789, places=3)
        self.assertAlmostEqual(dell["desk_avg_entry_price"], 540.7789, places=3)
        self.assertEqual(dell["alpaca_avg_entry_price"], 532.33)
        self.assertAlmostEqual(dell["cost_basis"], 4867.01, places=2)
        self.assertAlmostEqual(dell["unrealized_pl"], 125.48, places=2)


if __name__ == "__main__":
    unittest.main()
