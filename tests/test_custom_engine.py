"""Unit tests for Custom Trading Engine builder, store, and REST API."""

import tempfile
import unittest
from pathlib import Path
from starlette.testclient import TestClient

from bot.auth import AuthStore
from bot import custom_engine_store
from bot.webapp import app, AUTH_STORE


class TestCustomEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "auth.db"
        self.auth_store = AuthStore(db_path=self.db_path)

        # Patch custom engine store ROOT to temporary dir for clean test isolation
        self._orig_root = custom_engine_store.ROOT
        custom_engine_store.ROOT = Path(self.tmp_dir.name)

        # Patch global store for webapp testing
        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        webapp_module.AUTH_STORE = self.auth_store
        web_state_module.AUTH_STORE = self.auth_store
        auth_module.AUTH_STORE = self.auth_store

        self.client = TestClient(app)

        # Register test users
        self.trader1 = self.auth_store.register_user(
            username="trader1",
            email="trader1@algopaca.local",
            password="TraderPassword123!",
            display_name="Trader One",
            role="trader",
        )
        self.trader2 = self.auth_store.register_user(
            username="trader2",
            email="trader2@algopaca.local",
            password="TraderPassword123!",
            display_name="Trader Two",
            role="trader",
        )

        # Create session tokens
        self.token1, _ = self.auth_store.create_session(
            self.trader1["id"], user_agent="Mozilla/5.0"
        )
        self.token2, _ = self.auth_store.create_session(
            self.trader2["id"], user_agent="Mozilla/5.0"
        )

    def tearDown(self) -> None:
        custom_engine_store.ROOT = self._orig_root
        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        webapp_module.AUTH_STORE = AUTH_STORE
        web_state_module.AUTH_STORE = AUTH_STORE
        auth_module.AUTH_STORE = AUTH_STORE
        self.tmp_dir.cleanup()

    def test_starter_blueprints_listed(self) -> None:
        engines = custom_engine_store.list_custom_engines(self.trader1["id"])
        self.assertGreaterEqual(len(engines), 6)
        blueprint_ids = [e["id"] for e in engines if e.get("is_blueprint")]
        self.assertIn("blueprint_ai_trend", blueprint_ids)
        self.assertIn("blueprint_tech_crossover", blueprint_ids)
        self.assertIn("blueprint_quant_dip_hunter", blueprint_ids)
        self.assertIn("blueprint_relative_strength_pair", blueprint_ids)

    def test_save_and_get_custom_engine(self) -> None:
        payload = {
            "name": "My Custom Scalper",
            "description": "High frequency scalp engine",
            "base_engine": "ai",
            "instructions": "Open long on ADX >= 25, exit on 1.5R target.",
            "choices": {
                "strategy_mode": "ai",
                "symbol": "TSLA",
                "symbols": "TSLA, NVDA",
                "ai_risk_pct": 0.5,
                "ai_take_profit_r": 1.5,
            },
        }
        saved = custom_engine_store.save_custom_engine(payload, self.trader1["id"])
        self.assertTrue(saved["id"].startswith("ce_"))
        self.assertEqual(saved["name"], "My Custom Scalper")
        self.assertEqual(saved["choices"]["symbol"], "TSLA")

        # Retrieve
        fetched = custom_engine_store.get_custom_engine(saved["id"], self.trader1["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["name"], "My Custom Scalper")

    def test_save_blueprint_directly_creates_user_engine(self) -> None:
        blueprint = custom_engine_store.get_custom_engine("blueprint_quant_dip_hunter", self.trader1["id"])
        self.assertIsNotNone(blueprint)
        self.assertTrue(blueprint.get("is_blueprint"))

        payload = {
            "id": blueprint["id"],
            "name": blueprint["name"],
            "description": blueprint.get("description", ""),
            "base_engine": "dip",
            "instructions": "Modified dip instructions",
            "choices": {"strategy_mode": "dip", "dip_rsi_buy": 22},
        }
        saved = custom_engine_store.save_custom_engine(payload, self.trader1["id"])
        self.assertTrue(saved["id"].startswith("ce_"))
        self.assertFalse(saved.get("is_blueprint", False))
        self.assertEqual(saved["instructions"], "Modified dip instructions")
        self.assertEqual(saved["choices"]["dip_rsi_buy"], 22)

        # Original blueprint remains unchanged
        original_bp = custom_engine_store.get_custom_engine("blueprint_quant_dip_hunter", self.trader1["id"])
        self.assertTrue(original_bp.get("is_blueprint"))
        self.assertNotEqual(original_bp.get("instructions"), "Modified dip instructions")

    def test_duplicate_and_delete_engine(self) -> None:
        payload = {
            "name": "Original Engine",
            "base_engine": "sma",
            "instructions": "Quantitative SMA",
            "choices": {"fast_sma": 5, "slow_sma": 15},
        }
        saved = custom_engine_store.save_custom_engine(payload, self.trader1["id"])
        engine_id = saved["id"]

        # Duplicate
        cloned = custom_engine_store.duplicate_custom_engine(engine_id, self.trader1["id"])
        self.assertIsNotNone(cloned)
        self.assertEqual(cloned["name"], "Original Engine (Copy)")
        self.assertNotEqual(cloned["id"], engine_id)

        # Delete original
        deleted = custom_engine_store.delete_custom_engine(engine_id, self.trader1["id"])
        self.assertTrue(deleted)
        self.assertIsNone(custom_engine_store.get_custom_engine(engine_id, self.trader1["id"]))

        # Cannot delete blueprints
        self.assertFalse(custom_engine_store.delete_custom_engine("blueprint_ai_trend", self.trader1["id"]))

    def test_user_isolation(self) -> None:
        payload = {
            "name": "Trader1 Secret Engine",
            "base_engine": "ai",
            "instructions": "Confidential playbook",
        }
        saved = custom_engine_store.save_custom_engine(payload, self.trader1["id"])

        # Trader2 should not see Trader1's custom engine in their list
        trader2_engines = custom_engine_store.list_custom_engines(self.trader2["id"], include_blueprints=False)
        trader2_ids = [e["id"] for e in trader2_engines]
        self.assertNotIn(saved["id"], trader2_ids)

        # Trader2 should not be able to delete Trader1's engine
        self.assertFalse(custom_engine_store.delete_custom_engine(saved["id"], self.trader2["id"]))

    def test_api_endpoints_crud(self) -> None:
        headers = {"Authorization": f"Bearer {self.token1}"}

        # 1. Unauthenticated request fails
        res_unauth = self.client.get("/api/custom-engines")
        self.assertEqual(res_unauth.status_code, 401)

        # 2. List engines
        res_list = self.client.get("/api/custom-engines", headers=headers)
        self.assertEqual(res_list.status_code, 200)
        data = res_list.json()
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["engines"], list)

        # 3. Create new custom engine
        new_payload = {
            "name": "Alpha Momentum V1",
            "description": "Custom momentum engine",
            "base_engine": "ai",
            "instructions": "Follow trend structure with 2R profit target.",
            "choices": {
                "symbol": "QQQ",
                "symbols": "QQQ, SPY",
                "trade_qty": 2,
            },
        }
        res_create = self.client.post("/api/custom-engines", json=new_payload, headers=headers)
        self.assertEqual(res_create.status_code, 200)
        created = res_create.json()["engine"]
        engine_id = created["id"]
        self.assertEqual(created["name"], "Alpha Momentum V1")

        # 4. Get single engine
        res_get = self.client.get(f"/api/custom-engines/{engine_id}", headers=headers)
        self.assertEqual(res_get.status_code, 200)
        self.assertEqual(res_get.json()["engine"]["name"], "Alpha Momentum V1")

        # 5. Duplicate engine
        res_dup = self.client.post(f"/api/custom-engines/{engine_id}/duplicate", headers=headers)
        self.assertEqual(res_dup.status_code, 200)
        duplicated = res_dup.json()["engine"]
        self.assertEqual(duplicated["name"], "Alpha Momentum V1 (Copy)")

        # 6. Delete engine
        res_del = self.client.delete(f"/api/custom-engines/{engine_id}", headers=headers)
        self.assertEqual(res_del.status_code, 200)
        self.assertTrue(res_del.json()["ok"])

    def test_all_strategy_types_serialization(self) -> None:
        strategies = [
            ("sma", {"fast_sma": 8, "slow_sma": 24, "symbol": "SPY"}),
            ("dip", {"dip_rsi_buy": 25, "dip_rsi_sell": 65, "dip_skip_bearish": True, "symbol": "NVDA"}),
            ("ls", {"ls_ema_fast": 20, "ls_ema_slow": 50, "ls_adx_min": 25.0, "ls_atr_stop_mult": 1.6, "symbol": "QQQ"}),
            ("pair", {"pair_sma_period": 40, "pair_lookback": 5, "pair_impulse_pct": 4.5, "pair_weak_side": "SHORT", "symbols": "SPY,QQQ"}),
            ("ai", {"ai_provider": "gemini", "ai_min_confidence": 0.70, "symbols": "AAPL, MSFT"}),
        ]
        for base_engine, choices in strategies:
            payload = {
                "name": f"Test {base_engine.upper()} Strategy",
                "base_engine": base_engine,
                "instructions": f"Playbook for {base_engine}",
                "choices": choices,
            }
            saved = custom_engine_store.save_custom_engine(payload, self.trader1["id"])
            self.assertEqual(saved["base_engine"], base_engine)
            self.assertEqual(saved["choices"]["strategy_mode"], base_engine)
            self.assertEqual(saved["instructions"], f"Playbook for {base_engine}")

    def test_options_and_risk_choices_preserved(self) -> None:
        payload = {
            "name": "High Convexity Engine",
            "base_engine": "ai",
            "instructions": "Buy calls on breakouts",
            "choices": {
                "strategy_mode": "ai",
                "size_mode": "notional",
                "trade_qty": 1,
                "trade_notional": 500.0,
                "risk_engine_enabled": True,
                "ai_risk_pct": 0.75,
                "ai_atr_stop_mult": 2.2,
                "ai_take_profit_r": 3.0,
                "ai_trail_after_r": 1.5,
                "options_enabled": True,
                "options_style": "vertical",
                "options_dte_min": 14,
                "options_dte_max": 60,
                "options_otm_pct": 7.5,
                "options_max_contracts": 3,
                "options_max_premium_pct": 2.5,
                "require_approval": True,
                "notify_browser": True,
                "notify_email": True,
                "notification_email": "trader@example.com",
            },
        }
        saved = custom_engine_store.save_custom_engine(payload, self.trader1["id"])
        c = saved["choices"]
        self.assertEqual(c["trade_notional"], 500.0)
        self.assertEqual(c["options_otm_pct"], 7.5)
        self.assertEqual(c["options_max_premium_pct"], 2.5)
        self.assertEqual(c["notification_email"], "trader@example.com")
        self.assertTrue(c["options_enabled"])
        self.assertTrue(c["require_approval"])

    def test_run_settings_custom_engine_id_persistence(self) -> None:
        from bot.web_state import AppState
        state = AppState(user_id=self.trader1["id"], workspace_dir=Path(self.tmp_dir.name))
        
        # Update settings with a custom_engine_id
        updated = state.update_settings({
            "strategy_mode": "ai",
            "symbol": "AAPL",
            "symbols": "AAPL, MSFT",
            "custom_engine_id": "ce_9988776655",
        })
        self.assertEqual(updated.custom_engine_id, "ce_9988776655")
        self.assertEqual(state.settings.custom_engine_id, "ce_9988776655")

        # Verify it appears in status payload
        status = state.snapshot()
        self.assertEqual(status["settings"]["custom_engine_id"], "ce_9988776655")
        self.assertIn("custom_engines", status)
        self.assertIsInstance(status["custom_engines"], list)

    def test_api_validation_errors(self) -> None:
        headers = {"Authorization": f"Bearer {self.token1}"}

        # Empty name fails
        res_empty = self.client.post("/api/custom-engines", json={"name": ""}, headers=headers)
        self.assertEqual(res_empty.status_code, 422)

        # Deleting non-existent returns 400
        res_del_bad = self.client.delete("/api/custom-engines/non_existent_id", headers=headers)
        self.assertEqual(res_del_bad.status_code, 400)

        # Duplicating non-existent returns 404
        res_dup_bad = self.client.post("/api/custom-engines/non_existent_id/duplicate", headers=headers)
        self.assertEqual(res_dup_bad.status_code, 404)

    def test_nested_choices_strategy_mode_and_engine_id_synchronization(self) -> None:
        # User provides strategy_mode only in choices without top-level base_engine
        payload = {
            "name": "Nested Choice Engine",
            "choices": {
                "strategy_mode": "sma",
                "fast_sma": 9,
                "slow_sma": 21,
                "symbol": "SPY",
            },
        }
        saved = custom_engine_store.save_custom_engine(payload, self.trader1["id"])
        self.assertEqual(saved["base_engine"], "sma")
        self.assertEqual(saved["choices"]["strategy_mode"], "sma")
        self.assertEqual(saved["choices"]["custom_engine_id"], saved["id"])
        self.assertEqual(saved["choices"]["symbols"], "SPY")

    def test_ai_model_and_pair_choices_preserved(self) -> None:
        payload = {
            "name": "Multi-Model AI Engine",
            "base_engine": "ai",
            "choices": {
                "strategy_mode": "ai",
                "openai_model": "gpt-4o-mini",
                "gemini_model": "gemini-2.5-flash",
                "anthropic_model": "claude-3-7-sonnet-20250219",
                "xai_model": "grok-2",
                "pair_preset": "custom",
            },
        }
        saved = custom_engine_store.save_custom_engine(payload, self.trader1["id"])
        c = saved["choices"]
        self.assertEqual(c["openai_model"], "gpt-4o-mini")
        self.assertEqual(c["gemini_model"], "gemini-2.5-flash")
        self.assertEqual(c["anthropic_model"], "claude-3-7-sonnet-20250219")
        self.assertEqual(c["xai_model"], "grok-2")
        self.assertEqual(c["pair_preset"], "custom")


if __name__ == "__main__":
    unittest.main()
