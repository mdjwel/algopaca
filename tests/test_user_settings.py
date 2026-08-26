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
        webapp_module.AUTH_STORE = self.auth_store

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
        webapp_module.AUTH_STORE = AUTH_STORE
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
        self.assertTrue(prefs["sound_alerts"])

        # Update preferences
        res = self.client.put(
            "/api/user/preferences",
            json={
                "theme": "emerald",
                "language": "es",
                "default_page": "positions",
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
        self.assertFalse(saved["sound_alerts"])
        self.assertTrue(saved["compact_mode"])
        self.assertEqual(saved["default_size_mode"], "notional")
        self.assertEqual(saved["default_trade_notional"], 250.0)

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


if __name__ == "__main__":
    unittest.main()
