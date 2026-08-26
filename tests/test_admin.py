"""Unit tests for the Administrator and Owner Dashboard."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import unittest

from starlette.testclient import TestClient

from bot.auth import AuthStore
from bot.email_service import get_smtp_config, save_smtp_config, test_smtp_connection
import bot.webapp as webapp_module


class TestAdminDashboard(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_auth.db"
        self.auth_store = AuthStore(db_path=self.db_path)

        # Patch global AUTH_STORE in auth and webapp
        self.patcher1 = patch("bot.auth.AUTH_STORE", self.auth_store)
        self.patcher2 = patch("bot.webapp.AUTH_STORE", self.auth_store)
        self.patcher1.start()
        self.patcher2.start()

        self.client = TestClient(webapp_module.app)

        # Create an Owner user (first user gets owner automatically)
        self.owner = self.auth_store.register_user(
            username="owner_user",
            email="owner@algopaca.local",
            password="StrongPassword123!",
            display_name="Desk Owner",
            role="owner",
        )
        self.owner_token, _ = self.auth_store.create_session(self.owner["id"])

        # Create an Admin user
        self.admin = self.auth_store.register_user(
            username="admin_user",
            email="admin@algopaca.local",
            password="StrongPassword123!",
            display_name="Desk Admin",
            role="admin",
        )
        self.admin_token, _ = self.auth_store.create_session(self.admin["id"])

        # Create a regular Trader user
        self.trader = self.auth_store.register_user(
            username="trader_user",
            email="trader@algopaca.local",
            password="StrongPassword123!",
            display_name="Standard Trader",
            role="trader",
        )
        self.trader_token, _ = self.auth_store.create_session(self.trader["id"])

    def tearDown(self):
        self.patcher2.stop()
        self.patcher1.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_admin_route_guards(self):
        """Test access controls on /admin and /api/admin/*."""
        # Unauthenticated access to /admin redirects to login
        res = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login", res.headers.get("location", ""))

        # Trader access to /admin gives 403 Forbidden
        self.client.cookies.set("algopaca_session", self.trader_token)
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 403)

        # Admin access to /admin gives 200 OK
        self.client.cookies.set("algopaca_session", self.admin_token)
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 200)

        # Owner access to /admin gives 200 OK
        self.client.cookies.set("algopaca_session", self.owner_token)
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 200)

    def test_admin_stats_endpoint(self):
        """Test GET /api/admin/stats returns analytics and system specs."""
        self.client.cookies.set("algopaca_session", self.owner_token)
        res = self.client.get("/api/admin/stats")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("analytics", data)
        self.assertIn("system", data)

        analytics = data["analytics"]
        self.assertGreaterEqual(analytics["overview"]["total_users"], 3)
        self.assertGreaterEqual(analytics["overview"]["total_owners"], 1)
        self.assertGreaterEqual(analytics["overview"]["total_admins"], 1)
        self.assertGreaterEqual(analytics["overview"]["total_traders"], 1)
        self.assertEqual(len(analytics["daily_signups"]), 14)

    def test_admin_users_list_and_search(self):
        """Test GET /api/admin/users with search and role filter."""
        self.client.cookies.set("algopaca_session", self.admin_token)

        # List all
        res = self.client.get("/api/admin/users")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertGreaterEqual(data["total"], 3)

        # Filter by role
        res = self.client.get("/api/admin/users?role=trader")
        data = res.json()
        for u in data["users"]:
            self.assertEqual(u["role"], "trader")

        # Search by username
        res = self.client.get("/api/admin/users?search=owner")
        data = res.json()
        self.assertEqual(len(data["users"]), 1)
        self.assertEqual(data["users"][0]["username"], "owner_user")

    def test_admin_user_role_update(self):
        """Test updating user roles and permission boundaries."""
        # Admin promotes trader to admin -> allowed
        self.client.cookies.set("algopaca_session", self.admin_token)
        res = self.client.put(
            f"/api/admin/users/{self.trader['id']}/role",
            json={"role": "admin"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["user"]["role"], "admin")

        # Admin tries to promote user to owner -> forbidden (400)
        res = self.client.put(
            f"/api/admin/users/{self.trader['id']}/role",
            json={"role": "owner"},
        )
        self.assertEqual(res.status_code, 400)

        # Owner promotes user to owner -> allowed
        self.client.cookies.set("algopaca_session", self.owner_token)
        res = self.client.put(
            f"/api/admin/users/{self.trader['id']}/role",
            json={"role": "owner"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["user"]["role"], "owner")

        # Owner cannot demote last owner if only 1 remains
        # Reset trader back to trader
        self.auth_store.update_user_role(self.trader["id"], "trader", self.owner)
        res = self.client.put(
            f"/api/admin/users/{self.owner['id']}/role",
            json={"role": "trader"},
        )
        self.assertEqual(res.status_code, 400)

    def test_admin_revoke_sessions(self):
        """Test terminating user sessions."""
        self.client.cookies.set("algopaca_session", self.owner_token)

        # Create additional session for trader
        token2, _ = self.auth_store.create_session(self.trader["id"])
        self.assertIsNotNone(self.auth_store.get_user_by_session(token2))

        # Revoke sessions
        res = self.client.post(f"/api/admin/users/{self.trader['id']}/revoke-sessions")
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(res.json()["revoked_count"], 1)

        # Verify trader sessions no longer valid
        self.assertIsNone(self.auth_store.get_user_by_session(self.trader_token))
        self.assertIsNone(self.auth_store.get_user_by_session(token2))

    def test_admin_delete_user(self):
        """Test deleting a user account."""
        self.client.cookies.set("algopaca_session", self.owner_token)

        # Delete trader user
        res = self.client.delete(f"/api/admin/users/{self.trader['id']}")
        self.assertEqual(res.status_code, 200)

        # Verify user is gone
        self.assertIsNone(self.auth_store.get_user_by_id(self.trader["id"]))

        # Owner cannot delete themselves
        res = self.client.delete(f"/api/admin/users/{self.owner['id']}")
        self.assertEqual(res.status_code, 400)

    def test_admin_smtp_config_endpoints(self):
        """Test GET & POST /api/admin/smtp."""
        self.client.cookies.set("algopaca_session", self.owner_token)

        # Save SMTP config
        res = self.client.post(
            "/api/admin/smtp",
            json={
                "host": "smtp.mailgun.org",
                "port": 587,
                "username": "postmaster@example.com",
                "password": "SuperSecretPassword123!",
                "from_email": "notifications@example.com",
                "sender_name": "AlgoPaca Desk",
                "use_ssl": False,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("ok"))

        # Retrieve masked config
        res = self.client.get("/api/admin/smtp")
        self.assertEqual(res.status_code, 200)
        smtp = res.json()["smtp"]
        self.assertEqual(smtp["host"], "smtp.mailgun.org")
        self.assertEqual(smtp["password"], "••••••••")
        self.assertTrue(smtp["has_password"])
        self.assertTrue(smtp["configured"])

    def test_admin_smtp_live_diagnostics(self):
        """Test live SMTP diagnostic runner."""
        self.client.cookies.set("algopaca_session", self.owner_token)

        with patch("smtplib.SMTP") as mock_smtp:
            mock_inst = MagicMock()
            mock_inst.has_extn.return_value = True
            mock_smtp.return_value = mock_inst

            res = self.client.post(
                "/api/admin/smtp/test",
                json={
                    "to_email": "test@example.com",
                    "host": "smtp.example.com",
                    "port": 587,
                    "username": "tester",
                    "password": "secretpassword",
                    "from_email": "tester@example.com",
                    "sender_name": "AlgoPaca Test",
                    "use_ssl": False,
                },
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data.get("ok"))
            self.assertIn("logs", data)
            self.assertGreaterEqual(len(data["logs"]), 3)

    def test_admin_maintenance_endpoints(self):
        """Test purging expired tokens and running vacuum."""
        self.client.cookies.set("algopaca_session", self.owner_token)

        # Purge
        res = self.client.post("/api/admin/maintenance/purge-expired")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("ok"))

        # Vacuum
        res = self.client.post("/api/admin/maintenance/vacuum")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("ok"))
        self.assertEqual(res.json()["integrity"], "ok")

    def test_stats_does_not_vacuum(self):
        """/api/admin/stats must stay a cheap read.

        It is polled on every tab switch, so it must not run VACUUM — that
        takes an exclusive write lock and costs O(database size).
        """
        self.client.cookies.set("algopaca_session", self.owner_token)

        with patch.object(self.auth_store, "admin_vacuum_db") as vacuum:
            res = self.client.get("/api/admin/stats")
            self.assertEqual(res.status_code, 200)
            vacuum.assert_not_called()

        system = res.json()["system"]
        self.assertIn("db_path", system)
        self.assertTrue(system["db_path"], "db_path must be reported, not hardcoded in markup")
        self.assertNotIn("db_integrity", system)

    def test_trading_mode_segments_sum_to_total(self):
        """Paper + Live + Not-configured must equal total_users.

        Otherwise the KPI card and the integration bars beside it divide by
        different denominators and users with no credentials row vanish.
        """
        self.client.cookies.set("algopaca_session", self.owner_token)
        data = self.client.get("/api/admin/stats").json()["analytics"]

        modes = data["trading_modes"]
        total = data["overview"]["total_users"]
        self.assertEqual(modes["paper"] + modes["live"] + modes["unconfigured"], total)
        self.assertEqual(modes["total"], total)

    def test_users_sorting_is_whitelisted(self):
        """The sort key reaches SQL, so anything off the whitelist must fall back."""
        self.client.cookies.set("algopaca_session", self.owner_token)

        res = self.client.get("/api/admin/users?sort=id;DROP TABLE users--")
        self.assertEqual(res.status_code, 200)
        # The resolved key is echoed, never the caller's raw string.
        self.assertEqual(res.json()["sort"], "created_at")
        self.assertTrue(self.client.get("/api/admin/users").json()["users"])

        res = self.client.get("/api/admin/users?sort=last_login_at&direction=asc")
        self.assertEqual(res.json()["sort"], "last_login_at")
        self.assertEqual(res.json()["direction"], "asc")

    def test_users_expose_last_login_and_status(self):
        """The table needs both columns; they used to be fetched and dropped."""
        self.client.cookies.set("algopaca_session", self.owner_token)
        row = self.client.get("/api/admin/users").json()["users"][0]
        for key in ("last_login_at", "status", "has_credentials"):
            self.assertIn(key, row)

    def test_suspend_blocks_login_and_reinstate_restores_it(self):
        self.client.cookies.set("algopaca_session", self.owner_token)
        tid = self.trader["id"]

        res = self.client.put(f"/api/admin/users/{tid}/status", json={"status": "suspended"})
        self.assertEqual(res.status_code, 200)

        self.client.cookies.clear()
        res = self.client.post(
            "/api/auth/login",
            json={"identifier": "trader_user", "password": "StrongPassword123!"},
        )
        self.assertEqual(res.status_code, 401)
        self.assertIn("suspended", res.json()["detail"].lower())

        self.client.cookies.set("algopaca_session", self.owner_token)
        self.client.put(f"/api/admin/users/{tid}/status", json={"status": "active"})

        self.client.cookies.clear()
        res = self.client.post(
            "/api/auth/login",
            json={"identifier": "trader_user", "password": "StrongPassword123!"},
        )
        self.assertEqual(res.status_code, 200)

    def test_cannot_suspend_own_account(self):
        self.client.cookies.set("algopaca_session", self.owner_token)
        res = self.client.put(
            f"/api/admin/users/{self.owner['id']}/status", json={"status": "suspended"}
        )
        self.assertEqual(res.status_code, 400)

    def test_admin_actions_are_audited(self):
        """Role changes, suspensions, revocations and deletions leave a trail."""
        self.client.cookies.set("algopaca_session", self.owner_token)
        tid = self.trader["id"]

        self.client.put(f"/api/admin/users/{tid}/role", json={"role": "admin"})
        self.client.post(f"/api/admin/users/{tid}/revoke-sessions")
        self.client.put(f"/api/admin/users/{tid}/status", json={"status": "suspended"})

        entries = self.client.get("/api/admin/audit").json()["entries"]
        actions = [e["action"] for e in entries]
        self.assertIn("user.role_change", actions)
        self.assertIn("session.revoke_all", actions)
        self.assertIn("user.suspended", actions)

        role_entry = next(e for e in entries if e["action"] == "user.role_change")
        self.assertEqual(role_entry["actor_username"], "owner_user")
        self.assertEqual(role_entry["target_username"], "trader_user")
        self.assertIn("admin", role_entry["detail"])

    def test_audit_survives_user_deletion(self):
        """The trail must outlive the row it describes."""
        self.client.cookies.set("algopaca_session", self.owner_token)
        victim = self.auth_store.register_user(
            username="doomed_user", email="doomed@example.com",
            password="StrongPassword123!", role="trader",
        )

        self.client.delete(f"/api/admin/users/{victim['id']}")
        entries = self.client.get("/api/admin/audit").json()["entries"]
        deletion = next(e for e in entries if e["action"] == "user.delete")
        self.assertEqual(deletion["target_username"], "doomed_user")

    def test_admin_create_user_returns_setup_link(self):
        self.client.cookies.set("algopaca_session", self.owner_token)
        res = self.client.post(
            "/api/admin/users",
            json={"username": "invited_one", "email": "invited@example.com", "role": "trader"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["user"]["role"], "trader")
        # The link is always returned: with SMTP down it is the only way in.
        self.assertIn("/reset-password?token=", data["setup_url"])

    def test_admin_cannot_create_owner(self):
        self.client.cookies.set("algopaca_session", self.admin_token)
        res = self.client.post(
            "/api/admin/users",
            json={"username": "sneaky_owner", "email": "sneaky@example.com", "role": "owner"},
        )
        self.assertEqual(res.status_code, 400)

    def test_session_inspector_lists_and_revokes_one(self):
        self.client.cookies.set("algopaca_session", self.owner_token)
        tid = self.trader["id"]
        self.auth_store.create_session(tid, user_agent="Mozilla/5.0 Chrome/120 Windows")
        self.auth_store.create_session(tid, user_agent="Mozilla/5.0 Safari/17 Macintosh")

        # setUp already created one session for this trader.
        sessions = self.client.get(f"/api/admin/users/{tid}/sessions").json()["sessions"]
        self.assertEqual(len(sessions), 3)
        # Only a short prefix is exposed — never a usable token.
        self.assertEqual(len(sessions[0]["id"]), 12)
        self.assertIn("user_agent", sessions[0])

        res = self.client.delete(f"/api/admin/users/{tid}/sessions/{sessions[0]['id']}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.client.get(f"/api/admin/users/{tid}/sessions").json()["sessions"]), 2)

    def test_smtp_rejects_blank_host(self):
        """A blank host used to save 'successfully' and then read UNCONFIGURED."""
        self.client.cookies.set("algopaca_session", self.owner_token)
        res = self.client.post("/api/admin/smtp", json={"host": "", "port": 587})
        self.assertEqual(res.status_code, 422)

        res = self.client.post("/api/admin/smtp", json={"host": "smtp.example.com", "port": 70000})
        self.assertEqual(res.status_code, 422)

    def test_smtp_ssl_choice_is_not_overridden_by_port(self):
        """Unticking SSL on port 465 must survive a save/reload round trip."""
        with patch.dict(os.environ, {
            "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "465",
            "SMTP_USERNAME": "u", "SMTP_USE_SSL": "false",
        }, clear=False):
            self.assertFalse(get_smtp_config()["use_ssl"])

        # With no explicit choice recorded, the port is still a sane default.
        with patch.dict(os.environ, {
            "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "465",
            "SMTP_USERNAME": "u", "SMTP_USE_SSL": "",
        }, clear=False):
            self.assertTrue(get_smtp_config()["use_ssl"])

    def test_email_log_records_attempts(self):
        self.client.cookies.set("algopaca_session", self.owner_token)
        self.client.post(f"/api/admin/users/{self.trader['id']}/send-reset")

        entries = self.client.get("/api/admin/email-log").json()["entries"]
        self.assertTrue(entries)
        self.assertEqual(entries[0]["recipient"], "trader@algopaca.local")
        self.assertEqual(entries[0]["kind"], "password_reset")


if __name__ == "__main__":
    unittest.main()
