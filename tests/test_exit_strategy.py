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

        m5 = ManageStopIn(
            symbol="AAPL",
            action="price",
            stop_price=145.0,
            dip_hunt={"enabled": True, "wait_minutes": 15, "dip_pct": 4.5},
        )
        self.assertEqual(m5.dip_hunt.enabled, True)
        self.assertEqual(m5.dip_hunt.wait_minutes, 15)
        self.assertEqual(m5.dip_hunt.dip_pct, 4.5)
        self.assertTrue(m5.extended_hours)

        m6 = ManageStopIn(symbol="AAPL", action="breakeven", extended_hours=False)
        self.assertFalse(m6.extended_hours)

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
    def test_manage_position_stop_breakeven_without_entry_is_refused(self, mock_service_cls):
        """No average entry must not turn into a stop a cent under the mark."""
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0
        mock_service.get_mark_price.return_value = {"price": 160.0}
        mock_service.get_avg_entry_price.return_value = None

        with self.assertRaises(ValueError) as ctx:
            self.state.manage_position_stop(symbol="AAPL", action="breakeven")
        self.assertIn("average entry", str(ctx.exception))
        mock_service.replace_stop_loss.assert_not_called()
        mock_service.cancel_open_exit_orders.assert_not_called()

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_price_keeps_sub_dollar_ticks(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 1000.0
        mock_service.get_mark_price.return_value = {"price": 0.4580}
        mock_service.replace_stop_loss.return_value = {"id": "ord_p", "stop_price": 0.4567}

        self.state.manage_position_stop(symbol="PENY", action="price", stop_price=0.4567)
        mock_service.replace_stop_loss.assert_called_once_with("PENY", 0.4567)

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

    def test_is_symbol_in_auto_trade(self):
        # 1. Idle state
        self.state.loop_running = False
        self.assertFalse(self.state.is_symbol_in_auto_trade("AAPL"))
        self.assertFalse(self.state.is_symbol_in_auto_trade("INTW"))

        # 2. Main loop running for AAPL
        self.state.loop_running = True
        self.state.settings.symbols = "AAPL"
        self.state.settings.symbol = "AAPL"
        self.state.settings.strategy_mode = "sma"
        self.assertTrue(self.state.is_symbol_in_auto_trade("AAPL"))
        self.assertFalse(self.state.is_symbol_in_auto_trade("INTW"))

        # 3. Main loop in pair mode
        self.state.settings.strategy_mode = "pair"
        self.state.settings.pair_long_symbol = "GLD"
        self.state.settings.pair_short_symbol = "SLV"
        self.assertTrue(self.state.is_symbol_in_auto_trade("GLD"))
        self.assertTrue(self.state.is_symbol_in_auto_trade("SLV"))
        self.assertFalse(self.state.is_symbol_in_auto_trade("INTW"))

        # 4. Multi auto-trade runner
        self.state.loop_running = False
        mock_runner = MagicMock()
        mock_runner.is_running = True
        with patch.object(self.state.multi_trader, "get_runner", return_value=mock_runner):
            self.assertTrue(self.state.is_symbol_in_auto_trade("TSLA"))

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_allowed_when_not_in_auto_trade(self, mock_service_cls):
        """If ticker is not in auto-trade, exit strategy applies even if loop is running."""
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 238.0
        mock_service.get_mark_price.return_value = {"price": 23.14}
        mock_service.replace_stop_loss.return_value = {"id": "ord_intw", "stop_price": 22.45}

        # Loop is running for AAPL, but user applies exit strategy for INTW
        self.state.loop_running = True
        self.state.settings.symbols = "AAPL"
        self.state.settings.symbol = "AAPL"

        res = self.state.manage_position_stop(
            symbol="INTW",
            action="price",
            stop_price=22.45,
            qty=238.0,
        )
        self.assertTrue(res["stop"])
        self.assertEqual(res["stop"]["stop_price"], 22.45)
        mock_service.replace_stop_loss.assert_called_once_with("INTW", 22.45, qty=238.0)

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_blocked_when_in_auto_trade_loop(self, mock_service_cls):
        """If ticker IS in auto-trade loop, manual exit strategy changes are blocked."""
        self.state.loop_running = True
        self.state.settings.symbols = "AAPL"
        self.state.settings.symbol = "AAPL"

        with self.assertRaises(ValueError) as ctx:
            self.state.manage_position_stop(
                symbol="AAPL",
                action="price",
                stop_price=145.0,
            )
        self.assertIn("Stop the strategy loop before changing exit strategies by hand", str(ctx.exception))

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_blocked_when_in_multi_auto_trade(self, mock_service_cls):
        """If ticker IS in isolated multi auto-trade, manual exit strategy changes are blocked."""
        self.state.loop_running = False
        mock_runner = MagicMock()
        mock_runner.is_running = True
        with patch.object(self.state.multi_trader, "get_runner", return_value=mock_runner):
            with self.assertRaises(ValueError) as ctx:
                self.state.manage_position_stop(
                    symbol="MSFT",
                    action="price",
                    stop_price=300.0,
                )
            self.assertIn("Stop auto-trade for MSFT", str(ctx.exception))

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_fractional_qty_rejected(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0
        mock_service.get_mark_price.return_value = {"price": 150.0}

        # Fractional quantity requested
        with self.assertRaises(ValueError) as ctx:
            self.state.manage_position_stop(
                symbol="AAPL",
                action="price",
                stop_price=145.0,
                qty=1.5,
            )
        self.assertIn("require whole shares", str(ctx.exception).lower())

        # Total position is fractional (< 1)
        mock_service.get_position_qty.return_value = 0.5
        with self.assertRaises(ValueError) as ctx2:
            self.state.manage_position_stop(
                symbol="AAPL",
                action="price",
                stop_price=145.0,
            )
        self.assertIn("less than 1 whole share", str(ctx2.exception).lower())

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_bracket_r_multiple_with_ref_stop(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0
        mock_service.get_mark_price.return_value = {"price": 100.0}
        mock_service.get_avg_entry_price.return_value = 100.0
        mock_service.get_open_orders_summary.return_value = {}
        mock_service.arm_bracket_exit.return_value = {
            "symbol": "AAPL",
            "action": "bracket",
            "order_id": "ord_oco_r",
            "order_class": "oco",
            "stop": {"id": "ord_sl", "stop_price": 90.0},
            "take_profit": {"id": "ord_tp", "limit_price": 120.0},
        }

        # Entry = 100, Stop = 90 (risk unit = 10). R-multiple = 2.0 -> TP should be 120.0
        res = self.state.manage_position_stop(
            symbol="AAPL",
            action="bracket",
            stop_price=90.0,
            take_profit_r=2.0,
        )
        self.assertEqual(res["action"], "bracket")
        mock_service.arm_bracket_exit.assert_called_once_with(
            "AAPL",
            stop_price=90.0,
            take_profit_price=120.0,
            trail_percent=None,
            qty=None,
        )

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_with_dip_hunt_long_position(self, mock_service_cls):
        """Long position setting stop loss with dip hunt arms the dip hunt plan."""
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0  # Long
        mock_service.get_mark_price.return_value = {"price": 100.0}
        mock_service.replace_stop_loss.return_value = {"id": "ord_sl_100", "stop_price": 95.0}

        res = self.state.manage_position_stop(
            symbol="AAPL",
            action="price",
            stop_price=95.0,
            dip_hunt={"enabled": True, "wait_minutes": 15, "dip_pct": 5.0},
        )
        self.assertIn("dip_hunt", res)
        self.assertEqual(res["dip_hunt"]["symbol"], "AAPL")
        self.assertEqual(res["dip_hunt"]["stop_order_id"], "ord_sl_100")
        self.assertEqual(res["dip_hunt"]["wait_minutes"], 15.0)
        self.assertEqual(res["dip_hunt"]["dip_pct"], 5.0)
        self.assertEqual(res["dip_hunt"]["status"], "watching_stop")
        self.assertIn(res["dip_hunt"]["id"], self.state.dip_hunt_plans)

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_with_dip_hunt_short_supported(self, mock_service_cls):
        """Short position setting dip hunt arms the dip hunt plan."""
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = -10.0  # Short
        mock_service.get_mark_price.return_value = {"price": 100.0}
        mock_service.replace_stop_loss.return_value = {"id": "ord_sl_short", "stop_price": 105.0}

        res = self.state.manage_position_stop(
            symbol="AAPL",
            action="price",
            stop_price=105.0,
            dip_hunt={"enabled": True, "wait_minutes": 10, "dip_pct": 5.0},
        )
        self.assertIn("dip_hunt", res)
        self.assertEqual(res["dip_hunt"]["symbol"], "AAPL")
        self.assertEqual(res["dip_hunt"]["qty"], 10.0)
        self.assertEqual(res["dip_hunt"]["status"], "watching_stop")

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_with_dip_hunt_disabled_cancels_existing(self, mock_service_cls):
        """Setting dip_hunt enabled: False cancels any active dip hunt for symbol."""
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0
        mock_service.get_mark_price.return_value = {"price": 100.0}
        mock_service.replace_stop_loss.return_value = {"id": "ord_sl_2", "stop_price": 95.0}

        # Arm a dip hunt first
        self.state.manage_position_stop(
            symbol="AAPL",
            action="price",
            stop_price=95.0,
            dip_hunt={"enabled": True, "wait_minutes": 10, "dip_pct": 5.0},
        )
        active_plans = [p for p in self.state.dip_hunt_plans.values() if p["symbol"] == "AAPL" and p["status"] == "watching_stop"]
        self.assertEqual(len(active_plans), 1)

        # Now update exit strategy with dip hunt disabled
        res = self.state.manage_position_stop(
            symbol="AAPL",
            action="price",
            stop_price=96.0,
            dip_hunt={"enabled": False},
        )
        self.assertNotIn("dip_hunt", res)
        cancelled_plans = [p for p in self.state.dip_hunt_plans.values() if p["symbol"] == "AAPL" and p["status"] == "cancelled"]
        self.assertEqual(len(cancelled_plans), 1)

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_cancel_all_disarms_dip_hunt(self, mock_service_cls):
        """Action cancel_all disarms any active dip hunt for the symbol."""
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0
        mock_service.get_mark_price.return_value = {"price": 100.0}
        mock_service.replace_stop_loss.return_value = {"id": "ord_sl_3", "stop_price": 95.0}
        mock_service.cancel_open_exit_orders.return_value = {"stops_cancelled": 1, "take_profits_cancelled": 0}

        # Arm dip hunt
        self.state.manage_position_stop(
            symbol="AAPL",
            action="price",
            stop_price=95.0,
            dip_hunt={"enabled": True, "wait_minutes": 10, "dip_pct": 5.0},
        )
        self.assertTrue(any(p["symbol"] == "AAPL" and p["status"] == "watching_stop" for p in self.state.dip_hunt_plans.values()))

        # Cancel all
        self.state.manage_position_stop(symbol="AAPL", action="cancel_all")
        self.assertFalse(any(p["symbol"] == "AAPL" and p["status"] == "watching_stop" for p in self.state.dip_hunt_plans.values()))

    @patch("bot.web_state.AlpacaService")
    def test_manage_position_stop_extended_hours(self, mock_service_cls):
        """Extended hours defaults to True and passes stop_loss_24h to AlpacaService config."""
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_position_qty.return_value = 10.0
        mock_service.get_mark_price.return_value = {"price": 100.0}
        mock_service.replace_stop_loss.return_value = {"id": "ord_sl_ext", "stop_price": 95.0}

        # 1. Default (extended_hours omitted -> True)
        res_default = self.state.manage_position_stop(
            symbol="AAPL",
            action="price",
            stop_price=95.0,
        )
        self.assertTrue(res_default.get("extended_hours"))
        config_passed = mock_service_cls.call_args[0][0]
        self.assertTrue(getattr(config_passed, "stop_loss_24h", False))

        # 2. Explicit extended_hours=False
        res_off = self.state.manage_position_stop(
            symbol="AAPL",
            action="price",
            stop_price=95.0,
            extended_hours=False,
        )
        self.assertFalse(res_off.get("extended_hours"))
        config_passed_off = mock_service_cls.call_args[0][0]
        self.assertFalse(getattr(config_passed_off, "stop_loss_24h", True))

    @patch("bot.web_state.AlpacaService")
    def test_positions_overview_includes_dip_hunt_info(self, mock_service_cls):
        """positions_overview attaches has_dip_hunt and dip_hunt_plan metadata."""
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_all_positions.return_value = [
            {
                "symbol": "AAPL",
                "qty": "10",
                "side": "long",
                "market_value": "1500.00",
                "cost_basis": "1400.00",
                "unrealized_pl": "100.00",
                "current_price": "150.00",
            }
        ]
        mock_service.account_summary.return_value = {"equity": 10000.0, "cash": 5000.0, "buying_power": 10000.0}
        mock_service.get_open_orders_summary.return_value = {}

        # Initially no dip hunt
        ov1 = self.state.positions_overview()
        self.assertFalse(ov1["positions"][0]["has_dip_hunt"])
        self.assertIsNone(ov1["positions"][0]["dip_hunt_plan"])

        # Register a dip hunt for AAPL
        self.state.dip_hunt_plans["dh-test"] = {
            "id": "dh-test",
            "symbol": "AAPL",
            "status": "watching_stop",
            "wait_minutes": 10.0,
            "dip_pct": 5.0,
            "cycle": 1,
            "target_price": 95.0,
        }

        ov2 = self.state.positions_overview()
        self.assertTrue(ov2["positions"][0]["has_dip_hunt"])
        self.assertIsNotNone(ov2["positions"][0]["dip_hunt_plan"])
        self.assertEqual(ov2["positions"][0]["dip_hunt_plan"]["wait_minutes"], 10.0)
        self.assertEqual(ov2["positions"][0]["dip_hunt_plan"]["dip_pct"], 5.0)

    @patch("bot.web_state.AlpacaService")
    def test_positions_overview_detects_synthetic_stop_loss(self, mock_service_cls):
        """positions_overview detects 24h synthetic stop order with waiting status."""
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_all_positions.return_value = [
            {
                "symbol": "TSLA",
                "qty": "10",
                "side": "long",
                "market_value": "2500.00",
                "cost_basis": "2400.00",
                "unrealized_pl": "100.00",
                "current_price": "250.00",
            }
        ]
        mock_service.account_summary.return_value = {"equity": 10000.0, "cash": 5000.0, "buying_power": 10000.0}
        mock_service.get_open_orders_summary.return_value = {}

        # Synthetic order with waiting status
        self.state.synthetic_orders["synth_tsla_1"] = {
            "id": "synth_tsla_1",
            "symbol": "TSLA",
            "side": "sell",
            "qty": 10.0,
            "stop_price": 240.0,
            "status": "waiting",
            "extended_hours": True,
        }

        ov = self.state.positions_overview()
        pos = ov["positions"][0]
        self.assertTrue(pos["has_stop_loss"])
        self.assertTrue(pos["has_synthetic_stop"])
        self.assertEqual(pos["stop_loss_price"], 240.0)
        self.assertEqual(pos["stop_distance_pct"], 4.0)

    @patch("bot.web_state.AlpacaService")
    def test_close_single_position_disarms_dip_hunt(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        mock_service.close_position.return_value = {"id": "ord_close_1", "status": "filled"}

        self.state.dip_hunt_plans["dh-close-test"] = {
            "id": "dh-close-test",
            "symbol": "AAPL",
            "status": "watching_stop",
            "wait_minutes": 10.0,
            "dip_pct": 5.0,
        }

        self.state.close_single_position("AAPL", cancel_orders=True)
        self.assertEqual(self.state.dip_hunt_plans["dh-close-test"]["status"], "cancelled")

    def test_dip_hunt_store_custom_path_save_and_load(self):
        import tempfile
        from pathlib import Path
        from bot import dip_hunt_store

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "test_plans.json"
            plans = {
                "dh-1": {
                    "id": "dh-1",
                    "symbol": "NVDA",
                    "status": "watching_stop",
                    "qty": 10.0,
                    "stop_loss_pct": 3.0,
                    "take_profit_r": 2.0,
                    "wait_minutes": 15.0,
                    "dip_pct": 4.0,
                    "cycle": 1,
                    "created_at": 1000.0,
                }
            }
            dip_hunt_store.save_plans(plans, paper=True, path=tmp_path)
            self.assertTrue(tmp_path.exists())

            loaded = dip_hunt_store.load_plans(paper=True, path=tmp_path)
            self.assertIn("dh-1", loaded)
            self.assertEqual(loaded["dh-1"]["symbol"], "NVDA")
            self.assertEqual(loaded["dh-1"]["wait_minutes"], 15.0)


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

    def test_positions_overview_bracket_oco_detection_short(self):
        from alpaca.trading.enums import OrderSide, OrderType, OrderClass
        mock_service = MagicMock()
        mock_service.get_all_positions.return_value = [
            {
                "symbol": "SNDK",
                "side": "short",
                "qty": -2.0,
                "avg_entry_price": 1705.0,
                "current_price": 1726.0,
                "market_value": -3452.0,
                "unrealized_pl": -42.0,
            }
        ]
        # Simulating get_open_orders_summary with normalized enums from Alpaca
        mock_service.get_open_orders_summary.return_value = {
            "SNDK": [
                {
                    "id": "ord_tp",
                    "type": "limit",
                    "side": "buy",
                    "qty": 2.0,
                    "stop_price": None,
                    "limit_price": 1553.40,
                    "is_stop": False,
                    "order_class": "oco",
                },
                {
                    "id": "ord_sl",
                    "type": "stop",
                    "side": "buy",
                    "qty": 2.0,
                    "stop_price": 1777.78,
                    "limit_price": None,
                    "is_stop": True,
                    "order_class": "oco",
                },
            ]
        }
        mock_service.get_account.return_value = {"equity": 50000.0}
        state = AppState(user_id="test_user")
        state.multi_trader = MagicMock()
        state.multi_trader.get_runner_summary.return_value = None
        state.loop_running = False

        with patch("bot.web_state.AlpacaService", return_value=mock_service):
            overview = state.positions_overview()
            pos = overview["positions"][0]
            self.assertTrue(pos["has_stop_loss"])
            self.assertEqual(pos["stop_loss_price"], 1777.78)
            self.assertTrue(pos["has_take_profit"])
            self.assertEqual(pos["take_profit_price"], 1553.40)
            self.assertIsNotNone(pos["stop_distance_pct"])
            # For short, gap = (1726 - 1777.78) / 1726 * 100 = -3.00%, distance = -gap = +3.00%
            self.assertAlmostEqual(pos["stop_distance_pct"], 3.00, places=1)

    def test_get_open_orders_summary_normalizes_enums_and_legs(self):
        from alpaca.trading.enums import OrderSide, OrderType, OrderClass
        parent = MagicMock()
        parent.symbol = "SNDK"
        parent.type = OrderType.LIMIT
        parent.side = OrderSide.BUY
        parent.order_class = OrderClass.OCO
        parent.id = "parent_oco"
        parent.qty = 2.0
        parent.stop_price = None
        parent.limit_price = 1553.40

        leg = MagicMock()
        leg.symbol = "SNDK"
        leg.type = OrderType.STOP
        leg.side = OrderSide.BUY
        leg.order_class = OrderClass.OCO
        leg.id = "leg_stop"
        leg.qty = 2.0
        leg.stop_price = 1777.78
        leg.limit_price = None

        parent.legs = [leg]
        self.mock_trading.get_orders.return_value = [parent]

        summary = self.service.get_open_orders_summary()
        self.assertIn("SNDK", summary)
        orders = summary["SNDK"]
        self.assertEqual(len(orders), 2)
        # Parent limit leg
        self.assertEqual(orders[0]["side"], "buy")
        self.assertEqual(orders[0]["type"], "limit")
        self.assertFalse(orders[0]["is_stop"])
        self.assertEqual(orders[0]["limit_price"], 1553.40)
        # Child stop leg
        self.assertEqual(orders[1]["side"], "buy")
        self.assertEqual(orders[1]["type"], "stop")
        self.assertTrue(orders[1]["is_stop"])
        self.assertEqual(orders[1]["stop_price"], 1777.78)

    def test_current_stop_price_inspects_child_legs(self):
        from alpaca.trading.enums import OrderSide, OrderType, OrderClass
        parent = MagicMock()
        parent.symbol = "SNDK"
        parent.type = OrderType.LIMIT
        parent.side = OrderSide.BUY
        parent.order_class = OrderClass.OCO
        parent.stop_price = None

        leg = MagicMock()
        leg.symbol = "SNDK"
        leg.type = OrderType.STOP
        leg.side = OrderSide.BUY
        leg.stop_price = 1777.78
        parent.legs = [leg]

        self.service._open_orders = MagicMock(return_value=[parent])
        px = self.service.current_stop_price("SNDK")
        self.assertEqual(px, 1777.78)

    def test_submit_manual_order_attaches_stop_with_price_only(self):
        from alpaca.trading.enums import OrderSide, OrderClass
        self.service.market_session = MagicMock(return_value={"session": "regular"})
        self.service.get_mark_price = MagicMock(return_value={"price": 100.0})
        mock_submitted = MagicMock()
        mock_submitted.id = "ord_market_oto"
        self.mock_trading.submit_order.return_value = mock_submitted

        # Pass stop_loss_price directly, with stop_loss_pct = 0.0
        submitted, attached = self.service.submit_manual_order(
            symbol="AAPL",
            qty=5.0,
            side="buy",
            order_type="market",
            stop_loss_pct=0.0,
            stop_loss_price=95.0,
        )
        self.assertIsNotNone(attached)
        self.assertEqual(attached["stop_price"], 95.0)
        req = self.mock_trading.submit_order.call_args[0][0]
        self.assertEqual(req.order_class, OrderClass.OTO)
        self.assertEqual(req.stop_loss.stop_price, 95.0)

    def test_arm_bracket_exit_fractional_qty_rejected(self):
        self.service.get_position_qty = MagicMock(return_value=0.5)
        with self.assertRaises(ValueError) as ctx:
            self.service.arm_bracket_exit(
                "AAPL",
                stop_price=140.0,
                take_profit_price=160.0,
            )
        self.assertIn("needs at least 1 whole share", str(ctx.exception))

    def test_replace_stop_loss_fractional_qty_rejected(self):
        self.service.get_position_qty = MagicMock(return_value=0.75)
        with self.assertRaises(ValueError) as ctx:
            self.service.replace_stop_loss(
                "AAPL",
                145.0,
            )
    def test_arm_bracket_exit_crossing_rejected(self):
        # Long position: stop sitting at or above take profit
        self.service.get_position_qty = MagicMock(return_value=10.0)
        with self.assertRaises(ValueError) as ctx:
            self.service.arm_bracket_exit(
                "AAPL",
                stop_price=160.0,
                take_profit_price=150.0,
            )
        self.assertIn("must sit below take profit", str(ctx.exception))

        # Short position: stop sitting at or below take profit
        self.service.get_position_qty = MagicMock(return_value=-10.0)
        with self.assertRaises(ValueError) as ctx2:
            self.service.arm_bracket_exit(
                "AAPL",
                stop_price=140.0,
                take_profit_price=150.0,
            )
    def test_exit_leg_ids_with_enums_and_prices(self):
        from alpaca.trading.enums import OrderType
        parent = MagicMock()
        stop_leg = MagicMock()
        stop_leg.id = "stop_leg_123"
        stop_leg.type = OrderType.STOP
        stop_leg.stop_price = 145.0
        stop_leg.limit_price = None

        tp_leg = MagicMock()
        tp_leg.id = "tp_leg_456"
        tp_leg.type = OrderType.LIMIT
        tp_leg.stop_price = None
        tp_leg.limit_price = 165.0

        parent.legs = [stop_leg, tp_leg]
        legs = self.service.exit_leg_ids(parent)
        self.assertEqual(legs["stop_order_id"], "stop_leg_123")
        self.assertEqual(legs["take_profit_order_id"], "tp_leg_456")

    def test_open_protective_stop_id_returns_child_leg_id(self):
        from alpaca.trading.enums import OrderType, OrderSide
        self.service.get_position_qty = MagicMock(return_value=10.0)  # Long
        parent = MagicMock()
        parent.id = "parent_oco"
        parent.type = OrderType.LIMIT
        parent.side = OrderSide.SELL

        stop_leg = MagicMock()
        stop_leg.id = "child_stop_leg"
        stop_leg.type = OrderType.STOP
        stop_leg.side = OrderSide.SELL
        stop_leg.stop_price = 140.0
        stop_leg.legs = []

        parent.legs = [stop_leg]
        self.service._open_orders = MagicMock(return_value=[parent])

        stop_id = self.service.open_protective_stop_id("AAPL")
        self.assertEqual(stop_id, "child_stop_leg")

    def test_is_protective_stop_side_awareness(self):
        from alpaca.trading.enums import OrderType, OrderSide
        buy_stop = MagicMock()
        buy_stop.type = OrderType.STOP
        buy_stop.side = OrderSide.BUY
        buy_stop.stop_price = 200.0
        buy_stop.legs = []

        sell_stop = MagicMock()
        sell_stop.type = OrderType.STOP
        sell_stop.side = OrderSide.SELL
        sell_stop.stop_price = 140.0
        sell_stop.legs = []

        # For a long position, a buy stop is an entry order, NOT a protective stop
        self.assertFalse(self.service._is_protective_stop(buy_stop, position_side="long"))
        self.assertTrue(self.service._is_protective_stop(sell_stop, position_side="long"))

        # For a short position, a sell stop is an entry/breakdown order, NOT a protective stop
        self.assertFalse(self.service._is_protective_stop(sell_stop, position_side="short"))
        self.assertTrue(self.service._is_protective_stop(buy_stop, position_side="short"))

    def test_current_stop_price_short_picks_nearest_min_stop(self):
        from alpaca.trading.enums import OrderType, OrderSide
        self.service.get_position_qty = MagicMock(return_value=-10.0)  # Short
        order1 = MagicMock()
        order1.type = OrderType.STOP
        order1.side = OrderSide.BUY
        order1.stop_price = 110.0
        order1.legs = []

        order2 = MagicMock()
        order2.type = OrderType.STOP
        order2.side = OrderSide.BUY
        order2.stop_price = 105.0
        order2.legs = []

        self.service._open_orders = MagicMock(return_value=[order1, order2])
        # For short, 105 is closer to market (tighter stop) than 110
        px = self.service.current_stop_price("XYZ")
        self.assertEqual(px, 105.0)

    def test_ensure_stop_loss_rejects_fractional_long_shares(self):
        self.service.get_position_qty = MagicMock(return_value=0.5)  # Fractional long
        self.service.has_open_stop_sell = MagicMock(return_value=False)
        self.service.get_avg_entry_price = MagicMock(return_value=100.0)
        res = self.service.ensure_stop_loss("AAPL", pct=3.0)
        self.assertIsNone(res)

    def test_submit_exit_order_with_retry_on_insufficient_qty(self):
        req = MagicMock()
        success_order = MagicMock()
        success_order.id = "ord_success"

        # Simulate Alpaca raising 40310000 / insufficient qty on first attempt, then succeeding
        attempts = 0
        def side_effect(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise Exception('{"code":40310000,"message":"insufficient qty available for order"}')
            return success_order

        self.mock_trading.submit_order.side_effect = side_effect
        with patch("time.sleep", return_value=None):
            res = self.service._submit_exit_order_with_retry(req, "AAPL", max_attempts=3)
            self.assertEqual(res.id, "ord_success")
            self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()


