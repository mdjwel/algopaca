import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from bot.auth import AuthStore
from bot.reinvest_store import save_plans, load_plans
from bot.web_state import AppState
from bot.webapp import ReinvestIn


class ReinvestTestCase(unittest.TestCase):
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

    def test_reinvest_in_model_bracket_fields(self):
        reinvest = ReinvestIn(
            enabled=True,
            limit_price=100.0,
            bracket_enabled=True,
            stop_loss_pct=3.0,
            take_profit_pct=6.0,
        )
        self.assertTrue(reinvest.enabled)
        self.assertTrue(reinvest.bracket_enabled)
        self.assertEqual(reinvest.stop_loss_pct, 3.0)
        self.assertEqual(reinvest.take_profit_pct, 6.0)
        self.assertIsNone(reinvest.stop_loss_price)
        self.assertIsNone(reinvest.take_profit_price)

    def test_normalize_reinvest_request_with_bracket_pct(self):
        raw = {
            "enabled": True,
            "qty_mode": "match",
            "limit_price": 50.0,
            "expire_minutes": 60,
            "bracket_enabled": True,
            "stop_loss_pct": 4.0,  # 4% below $50 = $48.00
            "take_profit_pct": 8.0,  # 8% above $50 = $54.00
        }
        normalized = AppState.normalize_reinvest_request(raw, side="sell")
        self.assertIsNotNone(normalized)
        self.assertTrue(normalized["bracket_enabled"])
        self.assertEqual(normalized["stop_loss_pct"], 4.0)
        self.assertEqual(normalized["stop_loss_price"], 48.0)
        self.assertEqual(normalized["take_profit_pct"], 8.0)
        self.assertEqual(normalized["take_profit_price"], 54.0)

    def test_normalize_reinvest_request_with_bracket_prices(self):
        raw = {
            "enabled": True,
            "qty_mode": "match",
            "limit_price": 100.0,
            "expire_minutes": 60,
            "bracket_enabled": True,
            "stop_loss_price": 95.0,
            "take_profit_price": 110.0,
        }
        normalized = AppState.normalize_reinvest_request(raw, side="sell")
        self.assertIsNotNone(normalized)
        self.assertTrue(normalized["bracket_enabled"])
        self.assertEqual(normalized["stop_loss_price"], 95.0)
        self.assertEqual(normalized["take_profit_price"], 110.0)
        # Check computed pcts: (100 - 95)/100 = 5%, (110 - 100)/100 = 10%
        self.assertEqual(normalized["stop_loss_pct"], 5.0)
        self.assertEqual(normalized["take_profit_pct"], 10.0)

    def test_normalize_reinvest_request_invalid_bracket_prices(self):
        # Stop loss >= limit price
        raw_invalid_sl = {
            "enabled": True,
            "limit_price": 100.0,
            "bracket_enabled": True,
            "stop_loss_price": 105.0,
        }
        with self.assertRaises(ValueError):
            AppState.normalize_reinvest_request(raw_invalid_sl, side="sell")

        # Take profit <= limit price
        raw_invalid_tp = {
            "enabled": True,
            "limit_price": 100.0,
            "bracket_enabled": True,
            "take_profit_price": 95.0,
        }
        with self.assertRaises(ValueError):
            AppState.normalize_reinvest_request(raw_invalid_tp, side="sell")

    def test_normalize_reinvest_request_bracket_extended_hours_and_tif(self):
        # Bracket cannot attach in extended hours / 24h market
        raw_extended = {
            "enabled": True,
            "limit_price": 100.0,
            "bracket_enabled": True,
            "stop_loss_pct": 3.0,
            "extended_hours": True,
            "time_in_force": "day",
        }
        with self.assertRaises(ValueError) as ctx:
            AppState.normalize_reinvest_request(raw_extended, side="sell")
        self.assertIn("24-hour market", str(ctx.exception))

        # Bracket requires Day or GTC time in force
        raw_ioc = {
            "enabled": True,
            "limit_price": 100.0,
            "bracket_enabled": True,
            "stop_loss_pct": 3.0,
            "time_in_force": "ioc",
        }
        with self.assertRaises(ValueError) as ctx:
            AppState.normalize_reinvest_request(raw_ioc, side="sell")
        self.assertIn("Day or GTC", str(ctx.exception))

    def test_normalize_reinvest_request_percentage_bounds(self):
        # Stop loss > 50%
        raw_high_sl = {
            "enabled": True,
            "limit_price": 100.0,
            "bracket_enabled": True,
            "stop_loss_pct": 55.0,
        }
        with self.assertRaises(ValueError):
            AppState.normalize_reinvest_request(raw_high_sl, side="sell")

        # Stop loss price that results in > 50% drop
        raw_deep_sl = {
            "enabled": True,
            "limit_price": 100.0,
            "bracket_enabled": True,
            "stop_loss_price": 40.0,  # 60% drop
        }
        with self.assertRaises(ValueError):
            AppState.normalize_reinvest_request(raw_deep_sl, side="sell")

        # Take profit > 500%
        raw_high_tp = {
            "enabled": True,
            "limit_price": 100.0,
            "bracket_enabled": True,
            "stop_loss_pct": 3.0,
            "take_profit_pct": 600.0,
        }
        with self.assertRaises(ValueError):
            AppState.normalize_reinvest_request(raw_high_tp, side="sell")

    def test_reinvest_store_persists_bracket_fields(self):
        store_path = self.workspace_dir / "reinvest_plans.json"
        plan = {
            "id": "reinvest-123",
            "symbol": "AAPL",
            "sell_order_id": "sell-1",
            "side": "buy",
            "qty": 10.0,
            "limit_price": 150.0,
            "expire_minutes": 120,
            "bracket_enabled": True,
            "stop_loss_pct": 3.0,
            "stop_loss_price": 145.5,
            "take_profit_pct": 6.0,
            "take_profit_price": 159.0,
            "time_in_force": "day",
            "status": "waiting",
            "created_at": 1000.0,
        }
        with patch("bot.reinvest_store.plans_path_for", return_value=store_path):
            save_plans({"reinvest-123": plan}, paper=True)
            loaded_plans = load_plans(paper=True)

        loaded = loaded_plans.get("reinvest-123")
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.get("bracket_enabled"))
        self.assertEqual(loaded.get("stop_loss_pct"), 3.0)
        self.assertEqual(loaded.get("stop_loss_price"), 145.5)
        self.assertEqual(loaded.get("take_profit_pct"), 6.0)
        self.assertEqual(loaded.get("take_profit_price"), 159.0)

    def test_place_reinvest_buy_with_bracket(self):
        plan = {
            "id": "reinvest-456",
            "symbol": "MSFT",
            "sell_order_id": "sell-1",
            "side": "buy",
            "qty_mode": "custom",
            "qty": 5.5,  # Fractional qty should be floored to 5.0 for attached bracket
            "limit_price": 400.0,
            "bracket_enabled": True,
            "stop_loss_price": 388.0,
            "take_profit_price": 424.0,
            "time_in_force": "day",
            "status": "waiting",
        }
        self.state.reinvest_plans["reinvest-456"] = plan

        mock_submitted = MagicMock()
        mock_submitted.id = "buy-order-99"
        mock_service = MagicMock()
        mock_service.market_session.return_value = {"is_open": True}
        mock_service.submit_manual_order.return_value = (mock_submitted, None)

        with patch.object(self.state, "_require_live_execution"), \
             patch.object(self.state, "_persist_reinvest_plans"), \
             patch.object(self.state, "_record_trade_history"):
            self.state._place_reinvest_buy("reinvest-456", plan, mock_service, filled_qty=5.5)

        mock_service.submit_manual_order.assert_called_once()
        call_args, call_kwargs = mock_service.submit_manual_order.call_args
        self.assertEqual(call_args[0], "MSFT")
        self.assertEqual(call_args[1], 5.0)  # Fractional floored to whole
        self.assertEqual(call_kwargs["limit_price"], 400.0)
        self.assertEqual(call_kwargs["stop_loss_price"], 388.0)
        self.assertEqual(call_kwargs["take_profit_price"], 424.0)
        self.assertEqual(plan.get("buy_order_id"), "buy-order-99")
        self.assertEqual(plan.get("status"), "awaiting_fill")
