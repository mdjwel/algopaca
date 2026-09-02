"""Unit tests for User Settings, Profile Management, Preferences, and Sessions."""

import tempfile
import unittest
from pathlib import Path
from starlette.testclient import TestClient

from bot.auth import AuthStore
from bot.webapp import app, AUTH_STORE


class TestUserSettings(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "auth.db"
        self.auth_store = AuthStore(db_path=self.db_path)

        # Patch global store for webapp testing
        self._orig_auth_store = app.dependency_overrides.get("AUTH_STORE")
        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module
        webapp_module.AUTH_STORE = self.auth_store
        web_state_module.AUTH_STORE = self.auth_store
        auth_module.AUTH_STORE = self.auth_store

        self.client = TestClient(app)

        # Register test users
        self.owner = self.auth_store.register_user(
            username="owner_user",
            email="owner@algopaca.local",
            password="Password123!",
            display_name="Main Owner",
            role="owner",
        )
        self.trader = self.auth_store.register_user(
            username="test_trader",
            email="trader@algopaca.local",
            password="TraderPassword123!",
            display_name="Alpha Trader",
            role="trader",
        )

        # Create session tokens
        self.trader_token, _ = self.auth_store.create_session(self.trader["id"], user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
        self.owner_token, _ = self.auth_store.create_session(self.owner["id"], user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

    def tearDown(self) -> None:
        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module
        webapp_module.AUTH_STORE = AUTH_STORE
        web_state_module.AUTH_STORE = AUTH_STORE
        auth_module.AUTH_STORE = AUTH_STORE
        self.tmp_dir.cleanup()

    def test_unauthenticated_requests_blocked(self) -> None:
        res = self.client.get("/api/user/profile")
        self.assertEqual(res.status_code, 401)

        res = self.client.get("/api/user/preferences")
        self.assertEqual(res.status_code, 401)

        res = self.client.get("/api/user/sessions")
        self.assertEqual(res.status_code, 401)

    def test_get_and_update_profile(self) -> None:
        # Get profile
        res = self.client.get(
            "/api/user/profile",
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["profile"]["username"], "test_trader")
        self.assertEqual(data["profile"]["display_name"], "Alpha Trader")

        # Update display name and email
        res = self.client.put(
            "/api/user/profile",
            json={"display_name": "Pro Trader X", "email": "new_email@algopaca.local"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["profile"]["display_name"], "Pro Trader X")
        self.assertEqual(data["profile"]["email"], "new_email@algopaca.local")

        # Invalid email rejected
        res = self.client.put(
            "/api/user/profile",
            json={"email": "not-an-email"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 400)

        # Duplicate email rejected
        res = self.client.put(
            "/api/user/profile",
            json={"email": "owner@algopaca.local"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 400)

    def test_profile_exposes_fields_the_hero_card_renders(self) -> None:
        """The Profile tab hero reads these directly; losing one blanks the card."""
        res = self.client.get(
            "/api/user/profile",
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        profile = res.json()["profile"]

        for field in ("id", "username", "display_name", "role", "status",
                      "created_at", "active_sessions", "trading_mode"):
            self.assertIn(field, profile, f"hero card needs profile.{field}")

        self.assertEqual(profile["status"], "active")
        self.assertEqual(profile["trading_mode"], "paper")

        # active_sessions must be a real count — the UI used to fall back to a
        # hardcoded "1" whenever this was falsy, which lied on a zero count.
        self.assertIsInstance(profile["active_sessions"], int)
        self.assertEqual(profile["active_sessions"], 1)

        self.auth_store.create_session(self.trader["id"], user_agent="Mozilla/5.0 (X11; Linux x86_64)")
        res = self.client.get(
            "/api/user/profile",
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.json()["profile"]["active_sessions"], 2)

    def test_blank_display_name_falls_back_to_username(self) -> None:
        """The form now blocks this client-side; the server must still be safe."""
        res = self.client.put(
            "/api/user/profile",
            json={"display_name": "   "},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["profile"]["display_name"], "test_trader")

    def test_overlong_display_name_rejected(self) -> None:
        """Pydantic caps this at 50, so the rejection is a 422 with a list detail.

        The UI must not stringify that list straight into a toast — settings.js
        runs it through extractApiError() to pull out the `msg` fields.
        """
        res = self.client.put(
            "/api/user/profile",
            json={"display_name": "x" * 51},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 422)
        detail = res.json()["detail"]
        self.assertIsInstance(detail, list)
        self.assertTrue(all("msg" in item for item in detail))

    def test_change_password(self) -> None:
        # Wrong current password
        res = self.client.post(
            "/api/user/change-password",
            json={"current_password": "WrongPassword1!", "new_password": "BrandNewPassword123!"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Incorrect", res.json()["detail"])

        # Short new password
        res = self.client.post(
            "/api/user/change-password",
            json={"current_password": "TraderPassword123!", "new_password": "short"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 400)

        # Correct password update
        res = self.client.post(
            "/api/user/change-password",
            json={"current_password": "TraderPassword123!", "new_password": "BrandNewPassword123!"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])

        # Verify authentication works with new password
        auth_res = self.auth_store.authenticate_user("test_trader", "BrandNewPassword123!")
        self.assertEqual(auth_res["id"], self.trader["id"])

    def test_user_preferences(self) -> None:
        # Get defaults
        res = self.client.get(
            "/api/user/preferences",
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        prefs = res.json()["preferences"]
        self.assertEqual(prefs["theme"], "obsidian")
        self.assertEqual(prefs["language"], "en")
        self.assertEqual(prefs.get("timezone_display"), "local")
        self.assertEqual(prefs.get("time_format"), "12h")
        self.assertTrue(prefs["sound_alerts"])

        # Update preferences with custom IANA timezone and 24h time format
        res = self.client.put(
            "/api/user/preferences",
            json={
                "theme": "emerald",
                "language": "es",
                "default_page": "positions",
                "timezone_display": "Asia/Dhaka",
                "time_format": "24h",
                "sound_alerts": False,
                "confirm_orders": True,
                "chart_refresh_interval": 30,
                "compact_mode": True,
                "default_size_mode": "notional",
                "default_trade_notional": 250.0,
            },
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        saved = res.json()["preferences"]
        self.assertEqual(saved["theme"], "emerald")
        self.assertEqual(saved["language"], "es")
        self.assertEqual(saved["default_page"], "positions")
        self.assertEqual(saved["timezone_display"], "Asia/Dhaka")
        self.assertEqual(saved["time_format"], "24h")
        self.assertFalse(saved["sound_alerts"])
        self.assertTrue(saved["compact_mode"])
        self.assertEqual(saved["default_size_mode"], "notional")
        self.assertEqual(saved["default_trade_notional"], 250.0)

        # Update preferences with invalid timezone -> fallbacks to 'local'
        res = self.client.put(
            "/api/user/preferences",
            json={"timezone_display": "Invalid/Unknown_Timezone_123", "time_format": "invalid_format"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["preferences"]["timezone_display"], "local")
        self.assertEqual(res.json()["preferences"]["time_format"], "12h")

        # Update trading defaults with valid notification email
        res = self.client.put(
            "/api/user/preferences",
            json={
                "default_size_mode": "qty",
                "default_trade_qty": 5.0,
                "default_trade_notional": 500.0,
                "require_approval": True,
                "notify_browser": True,
                "notify_email": True,
                "notification_email": "trader.alerts@example.com",
            },
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        saved = res.json()["preferences"]
        self.assertEqual(saved["default_trade_qty"], 5.0)
        self.assertEqual(saved["default_trade_notional"], 500.0)
        self.assertTrue(saved["require_approval"])
        self.assertTrue(saved["notify_email"])
        self.assertEqual(saved["notification_email"], "trader.alerts@example.com")

        # Invalid notification email format
        res = self.client.put(
            "/api/user/preferences",
            json={"notification_email": "not-an-email"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 422)

        # Invalid trade qty (<= 0)
        res = self.client.put(
            "/api/user/preferences",
            json={"default_trade_qty": 0},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 422)

        # Invalid trade notional (<= 0)
        res = self.client.put(
            "/api/user/preferences",
            json={"default_trade_notional": -10.0},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 422)

    def test_user_sessions_management(self) -> None:
        # Add another session for trader
        second_token, _ = self.auth_store.create_session(self.trader["id"], user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0)")

        res = self.client.get(
            "/api/user/sessions",
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        sessions = res.json()["sessions"]
        self.assertEqual(len(sessions), 2)
        # Current token marked
        current_sess = [s for s in sessions if s["is_current"]]
        self.assertEqual(len(current_sess), 1)

        # Terminate other sessions
        res = self.client.post(
            "/api/user/sessions/terminate",
            json={"terminate_others": True},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["revoked_count"], 1)

        # Check only current remains
        res = self.client.get(
            "/api/user/sessions",
            cookies={"algopaca_session": self.trader_token},
        )
        sessions = res.json()["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["is_current"])

    def test_export_account_data(self) -> None:
        res = self.client.get(
            "/api/user/export",
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertIn("profile", data)
        self.assertIn("preferences", data)
        self.assertIn("integrations", data)
        self.assertIn("active_sessions", data)
        self.assertEqual(data["profile"]["username"], "test_trader")

    def test_delete_user_account(self) -> None:
        # Wrong password fails
        res = self.client.request(
            "DELETE",
            "/api/user/account",
            json={"password": "WrongPassword!"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 400)

        # Owner cannot delete if sole owner
        res = self.client.request(
            "DELETE",
            "/api/user/account",
            json={"password": "Password123!"},
            cookies={"algopaca_session": self.owner_token},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Owner", res.json()["detail"])

        # Trader deletion succeeds with valid password
        res = self.client.request(
            "DELETE",
            "/api/user/account",
            json={"password": "TraderPassword123!"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])

        # User is gone
        self.assertIsNone(self.auth_store.get_user_by_id(self.trader["id"]))

    def test_save_ai_provider_selection(self) -> None:
        """Verify selecting active AI provider updates desk settings and snapshot."""
        # Switch provider to gemini
        res = self.client.post(
            "/api/keys",
            json={"ai_provider": "gemini"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["state"]["settings"]["ai_provider"], "gemini")

        # Invalid provider rejected
        res = self.client.post(
            "/api/keys",
            json={"ai_provider": "invalid_provider"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 400)

        # Save both key and provider
        res = self.client.post(
            "/api/keys",
            json={"ai_provider": "anthropic", "anthropic_api_key": "sk-ant-testkey123"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["state"]["settings"]["ai_provider"], "anthropic")
        self.assertTrue(data["ai_key_status"]["anthropic"]["set"])

    def test_openai_api_key_persistence_across_settings_save(self) -> None:
        """Verify saved OpenAI key is not wiped when settings or other desk forms are updated."""
        # 1. Save OpenAI API key
        res = self.client.post(
            "/api/keys",
            json={"ai_provider": "openai", "openai_api_key": "sk-proj-test-1234567890abcdef"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["ai_key_status"]["openai"]["set"])
        self.assertTrue(data["state"]["ai_ready"]["openai"])

        # 2. Update desk settings (e.g., auto-trade form auto-save with empty key fields)
        res = self.client.post(
            "/api/settings",
            json={"symbol": "NVDA", "strategy_mode": "ai", "ai_provider": "openai"},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        # Ensure OpenAI key remains saved and NOT wiped
        self.assertTrue(data["ai_key_status"]["openai"]["set"])

        # 3. Check status snapshot & base config
        res = self.client.get(
            "/api/status",
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        status_data = res.json()
        self.assertTrue(status_data["ai_key_status"]["openai"]["set"])
        self.assertTrue(status_data["ai_ready"]["openai"])

        # 4. Explicit clear removes the key
        res = self.client.post(
            "/api/keys/clear",
            json={"openai": True},
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        clear_data = res.json()
        self.assertFalse(clear_data["ai_key_status"]["openai"]["set"])
        self.assertFalse(clear_data["state"]["ai_ready"]["openai"])


    def test_setup_wizard_page_routing(self) -> None:
        """Verify /setup-wizard and /wizard routes require auth and render for authenticated user."""
        # Unauthenticated redirect
        res = self.client.get("/setup-wizard", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertTrue("/login" in res.headers["location"])

        res = self.client.get("/wizard", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertTrue("/login" in res.headers["location"])

        # Authenticated access renders HTML
        res = self.client.get(
            "/setup-wizard",
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Setup Wizard", res.text)
        self.assertIn("setup-wizard.js", res.text)
        self.assertIn("setup-wizard.css", res.text)

        res = self.client.get(
            "/wizard",
            cookies={"algopaca_session": self.trader_token},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Setup Wizard", res.text)


class TestFreshInstanceSetup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "auth.db"
        self.auth_store = AuthStore(db_path=self.db_path)

        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module
        webapp_module.AUTH_STORE = self.auth_store
        web_state_module.AUTH_STORE = self.auth_store
        auth_module.AUTH_STORE = self.auth_store

        self.client = TestClient(app)

    def tearDown(self) -> None:
        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module
        webapp_module.AUTH_STORE = AUTH_STORE
        web_state_module.AUTH_STORE = AUTH_STORE
        auth_module.AUTH_STORE = AUTH_STORE
        self.tmp_dir.cleanup()

    def test_fresh_instance_redirections(self) -> None:
        """In a fresh instance without an owner, all pages redirect to /setup-wizard."""
        self.assertTrue(self.auth_store.needs_setup())

        # / redirects to /setup-wizard
        res = self.client.get("/", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/setup-wizard")

        # /login redirects to /setup-wizard
        res = self.client.get("/login", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/setup-wizard")

        # /signup redirects to /setup-wizard
        res = self.client.get("/signup", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/setup-wizard")

        # /auto-trade redirects to /setup-wizard
        res = self.client.get("/auto-trade", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/setup-wizard")

        # /setup-wizard renders 200 without login
        res = self.client.get("/setup-wizard")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Setup Wizard", res.text)
        self.assertIn("wizard-owner-card", res.text)

    def test_setup_status_api(self) -> None:
        """API setup status reports needs_setup=True in fresh environment."""
        res = self.client.get("/api/setup/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["needs_setup"])
        self.assertFalse(data["is_authenticated"])
        self.assertIsNone(data["user"])

    def test_complete_setup_flow(self) -> None:
        """Completing setup creates owner account, returns session cookie, and clears needs_setup."""
        payload = {
            "username": "superowner",
            "email": "superowner@example.com",
            "password": "Password12345!",
            "display_name": "Super Owner",
            "environment": "paper",
            "style": "ai",
            "ai_provider": "openai",
            "ai_api_key": "sk-test12345",
            "strategy": "ai",
            "symbols": "AAPL, NVDA",
            "theme": "copper",
            "lang": "en",
            "default_page": "auto-trade",
        }
        res = self.client.post("/api/setup/complete", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["redirect"], "/auto-trade")
        self.assertIn("algopaca_session", res.cookies)

        # After setup completion, needs_setup is False
        self.assertFalse(self.auth_store.needs_setup())
        owner = self.auth_store.get_user_by_session(res.cookies["algopaca_session"])
        self.assertIsNotNone(owner)
        self.assertEqual(owner["username"], "superowner")
        self.assertEqual(owner["role"], "owner")

        # Visiting / with the session cookie redirects to /auto-trade
        res = self.client.get("/", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/auto-trade")

        # Visiting /login when unauthenticated now serves login page
        anon = TestClient(app)
        res = anon.get("/login", follow_redirects=False)
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()


