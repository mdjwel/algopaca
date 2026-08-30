"""Tests for AlgoPaca Authentication, Session Store, and Auth API Endpoints."""

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from starlette.testclient import TestClient

from bot.auth import AUTH_STORE, AuthStore
from bot.webapp import app, SESSION_COOKIE_NAME


class TestAuthStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_auth.db"
        self.store = AuthStore(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_demo_user_created_automatically(self):
        demo = self.store.get_or_create_demo_user()
        self.assertEqual(demo["username"], "demo")
        self.assertEqual(demo["email"], "demo@algopaca.local")

    def test_register_user_success(self):
        user = self.store.register_user(
            username="test_trader",
            email="trader@example.com",
            password="StrongPassword123!",
            display_name="Test Trader",
        )
        self.assertEqual(user["username"], "test_trader")
        self.assertEqual(user["email"], "trader@example.com")
        self.assertEqual(user["display_name"], "Test Trader")
        self.assertIn("id", user)

    def test_register_user_validation(self):
        # Short password
        with self.assertRaises(ValueError) as ctx:
            self.store.register_user("user1", "user1@example.com", "short")
        self.assertIn("at least 8 characters", str(ctx.exception))

        # Overlong password (unbounded PBKDF2 input is a DoS vector)
        with self.assertRaises(ValueError) as ctx:
            self.store.register_user("user1", "user1@example.com", "a" * 129)
        self.assertIn("128 characters or fewer", str(ctx.exception))

        # Invalid username
        with self.assertRaises(ValueError) as ctx:
            self.store.register_user("ab", "user1@example.com", "validpassword123")
        self.assertIn("Username must be 3-30 characters", str(ctx.exception))

        # Invalid email
        with self.assertRaises(ValueError) as ctx:
            self.store.register_user("validuser", "not-an-email", "validpassword123")
        self.assertIn("valid email address", str(ctx.exception))

    def test_duplicate_username_and_email(self):
        self.store.register_user("unique_trader", "unique@example.com", "password123")

        with self.assertRaises(ValueError) as ctx:
            self.store.register_user("unique_trader", "other@example.com", "password123")
        self.assertIn("username already exists", str(ctx.exception).lower())

        with self.assertRaises(ValueError) as ctx:
            self.store.register_user("other_trader", "unique@example.com", "password123")
        self.assertIn("email address already exists", str(ctx.exception).lower())

    def test_authenticate_user_by_username_and_email(self):
        self.store.register_user("alpha_trader", "alpha@desk.com", "SecureAlpha2026!")

        # Auth by username
        user1 = self.store.authenticate_user("alpha_trader", "SecureAlpha2026!")
        self.assertEqual(user1["username"], "alpha_trader")
        self.assertIsNotNone(user1["last_login_at"])

        # Auth by email (case insensitive)
        user2 = self.store.authenticate_user("ALPHA@desk.com", "SecureAlpha2026!")
        self.assertEqual(user2["username"], "alpha_trader")

        # Bad password
        with self.assertRaises(ValueError):
            self.store.authenticate_user("alpha_trader", "wrong_password")

        # Unknown user
        with self.assertRaises(ValueError):
            self.store.authenticate_user("non_existent", "password123")

    def test_login_rejects_absurdly_long_password(self):
        self.store.register_user("long_pw_user", "longpw@desk.com", "SecurePass2026!")
        with self.assertRaises(ValueError) as ctx:
            self.store.authenticate_user("long_pw_user", "a" * 2048)
        # Same generic wording as any other bad credential, so nothing leaks.
        self.assertIn("Invalid username/email or password", str(ctx.exception))

    def test_session_lifecycle(self):
        user = self.store.register_user("session_user", "session@desk.com", "password123")
        token, expires_dt = self.store.create_session(user["id"], remember_me=False)
        self.assertIsInstance(token, str)
        self.assertEqual(len(token), 64)

        # Lookup session
        session_user = self.store.get_user_by_session(token)
        self.assertIsNotNone(session_user)
        self.assertEqual(session_user["username"], "session_user")

        # Invalidate / delete session
        deleted = self.store.delete_session(token)
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get_user_by_session(token))


class TestAuthAPI(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "auth.db"
        self.auth_store = AuthStore(db_path=self.db_path)

        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        self._orig_webapp_auth = webapp_module.AUTH_STORE
        self._orig_web_state_auth = web_state_module.AUTH_STORE
        self._orig_auth_store = auth_module.AUTH_STORE

        webapp_module.AUTH_STORE = self.auth_store
        web_state_module.AUTH_STORE = self.auth_store
        auth_module.AUTH_STORE = self.auth_store

        # Create an owner user so setup wizard does not intercept login/signup page tests
        self.owner = self.auth_store.register_user(
            username="test_owner",
            email="owner@test.local",
            password="OwnerPassword123!",
            role="owner",
        )

        self.client = TestClient(app)

    def tearDown(self):
        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        webapp_module.AUTH_STORE = self._orig_webapp_auth
        web_state_module.AUTH_STORE = self._orig_web_state_auth
        auth_module.AUTH_STORE = self._orig_auth_store

        self.tmp_dir.cleanup()

    def test_page_routes_return_200(self):
        login_res = self.client.get("/login")
        self.assertEqual(login_res.status_code, 200)
        self.assertIn("Sign In", login_res.text)

        signup_res = self.client.get("/signup")
        self.assertEqual(signup_res.status_code, 200)
        self.assertIn("Create Account", signup_res.text)

    def test_api_auth_me_unauthenticated(self):
        res = self.client.get("/api/auth/me")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertFalse(data.get("authenticated"))
        self.assertIsNone(data.get("user"))

    def test_api_auth_signup_and_me(self):
        import uuid
        unique_id = uuid.uuid4().hex[:8]
        username = f"api_user_{unique_id}"
        email = f"api_{unique_id}@example.com"

        res = self.client.post(
            "/api/auth/signup",
            json={
                "username": username,
                "email": email,
                "password": "Password123!",
                "display_name": "API Trader",
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["user"]["username"], username)
        self.assertIn(SESSION_COOKIE_NAME, res.cookies)

        token = data.get("token")
        self.assertTrue(token)

        # GET /api/auth/me using cookie
        me_res = self.client.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: token})
        self.assertEqual(me_res.status_code, 200)
        me_data = me_res.json()
        self.assertTrue(me_data.get("authenticated"))
        self.assertEqual(me_data["user"]["username"], username)

        # GET /api/auth/me using Authorization Bearer Header
        header_res = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(header_res.status_code, 200)
        header_data = header_res.json()
        self.assertTrue(header_data.get("authenticated"))
        self.assertEqual(header_data["user"]["username"], username)

    def test_api_auth_login_and_logout(self):
        import uuid
        unique_id = uuid.uuid4().hex[:8]
        username = f"login_user_{unique_id}"
        email = f"login_{unique_id}@example.com"
        password = "SecureLogin123!"

        # Register first
        self.client.post(
            "/api/auth/signup",
            json={"username": username, "email": email, "password": password},
        )

        # Login with username
        login_res = self.client.post(
            "/api/auth/login",
            json={"identifier": username, "password": password, "remember_me": True},
        )
        self.assertEqual(login_res.status_code, 200)
        token = login_res.json()["token"]

        # Logout
        logout_res = self.client.post(
            "/api/auth/logout",
            cookies={SESSION_COOKIE_NAME: token},
        )
        self.assertEqual(logout_res.status_code, 200)

        # After logout, token should no longer work
        me_res = self.client.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: token})
        self.assertFalse(me_res.json().get("authenticated"))

    def test_login_throttled_after_repeated_failures(self):
        import uuid
        from bot import webapp

        identifier = f"throttle_{uuid.uuid4().hex[:8]}"
        webapp._login_attempts.clear()
        self.addCleanup(webapp._login_attempts.clear)

        for _ in range(webapp.LOGIN_MAX_ATTEMPTS):
            res = self.client.post(
                "/api/auth/login",
                json={"identifier": identifier, "password": "wrong-password"},
            )
            self.assertEqual(res.status_code, 401)

        blocked = self.client.post(
            "/api/auth/login",
            json={"identifier": identifier, "password": "wrong-password"},
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)
        self.assertGreater(int(blocked.headers["Retry-After"]), 0)

    def test_successful_login_clears_the_throttle_counter(self):
        import uuid
        from bot import webapp

        unique_id = uuid.uuid4().hex[:8]
        username = f"reset_user_{unique_id}"
        password = "ResetCounter2026!"
        webapp._login_attempts.clear()
        self.addCleanup(webapp._login_attempts.clear)

        self.client.post(
            "/api/auth/signup",
            json={"username": username, "email": f"reset_{unique_id}@example.com", "password": password},
        )

        for _ in range(webapp.LOGIN_MAX_ATTEMPTS - 1):
            self.client.post(
                "/api/auth/login",
                json={"identifier": username, "password": "nope"},
            )

        good = self.client.post(
            "/api/auth/login",
            json={"identifier": username, "password": password},
        )
        self.assertEqual(good.status_code, 200)

        # The counter is reset, so the next wrong guess is a plain 401, not 429.
        after = self.client.post(
            "/api/auth/login",
            json={"identifier": username, "password": "nope"},
        )
        self.assertEqual(after.status_code, 401)

    def test_session_cookie_is_httponly(self):
        import uuid

        unique_id = uuid.uuid4().hex[:8]
        res = self.client.post(
            "/api/auth/signup",
            json={
                "username": f"cookie_user_{unique_id}",
                "email": f"cookie_{unique_id}@example.com",
                "password": "CookieFlags2026!",
            },
        )
        self.assertEqual(res.status_code, 200)
        set_cookie = res.headers.get("set-cookie", "")
        self.assertIn(SESSION_COOKIE_NAME, set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=lax", set_cookie)

    def test_api_auth_demo_quick_login_disabled(self):
        res = self.client.post("/api/auth/demo")
        self.assertEqual(res.status_code, 400)
        self.assertIn("disabled", res.json().get("detail", "").lower())

    def test_login_page_structure_and_i18n(self):
        res = self.client.get("/login")
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn('data-page="login"', html)
        self.assertIn('id="lang-select"', html)
        self.assertIn('data-i18n="auth_login_title"', html)
        self.assertIn('data-i18n-placeholder="auth_identifier_placeholder"', html)
        self.assertIn('data-i18n-placeholder="auth_password_placeholder"', html)
        self.assertIn('id="link-to-signup"', html)
        # Escape hatch for a visitor who is already signed in as someone else.
        self.assertIn('id="link-switch-account"', html)
        self.assertIn('data-i18n="auth_skip_to_signin_form"', html)

    def test_signup_page_structure_and_i18n(self):
        res = self.client.get("/signup")
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn('data-page="signup"', html)
        self.assertIn('id="lang-select"', html)
        self.assertIn('data-i18n="auth_signup_title"', html)
        self.assertIn('data-i18n-placeholder="auth_username_placeholder"', html)
        self.assertIn('data-i18n="auth_username_hint"', html)
        self.assertIn('data-i18n-placeholder="auth_email_placeholder"', html)
        self.assertIn('data-i18n-placeholder="auth_password_hint_placeholder"', html)
        self.assertIn('data-i18n-placeholder="auth_confirm_placeholder"', html)
        self.assertIn('id="strength-meter"', html)
        self.assertIn('data-i18n="auth_rule_len"', html)
        self.assertIn('data-i18n="auth_rule_upper"', html)
        self.assertIn('data-i18n="auth_rule_lower"', html)
        self.assertIn('data-i18n="auth_rule_num"', html)
        self.assertIn('id="link-to-login"', html)
        self.assertIn('id="link-switch-account"', html)
        self.assertIn('data-i18n="auth_skip_to_signup_form"', html)
        # Each password field needs its own Caps Lock hint.
        self.assertIn('id="caps-lock-hint"', html)
        self.assertIn('id="caps-lock-hint-confirm"', html)
        # Paper-trading disclosure before the account is created.
        self.assertIn('data-i18n="auth_paper_notice"', html)
        self.assertIn('data-i18n="auth_signup_session_note"', html)

    def test_auth_pages_only_reference_defined_i18n_keys(self):
        import json
        import re

        web_dir = Path(__file__).resolve().parent.parent / "web"
        lang_dir = web_dir / "static" / "lang"
        en = json.loads((lang_dir / "en.json").read_text("utf-8"))
        pattern = re.compile(r'data-i18n(?:-placeholder|-title|-aria-label)?="([^"]+)"')

        for page in ("login.html", "signup.html", "reset-password.html"):
            html = (web_dir / page).read_text("utf-8")
            for key in sorted(set(pattern.findall(html))):
                self.assertIn(key, en, f"{page} references undefined i18n key: {key}")

    def test_all_auth_language_keys_exist(self):
        import json
        lang_dir = Path(__file__).resolve().parent.parent / "web" / "static" / "lang"
        langs = ["en", "bn", "es", "fr", "hi"]
        dicts = {l: json.loads((lang_dir / f"{l}.json").read_text("utf-8")) for l in langs}
        auth_keys = {k for k in dicts["en"].keys() if k.startswith("auth_") or k.startswith("nav_sign_")}

        self.assertGreaterEqual(len(auth_keys), 50)
        for l in langs:
            missing = auth_keys - set(dicts[l].keys())
            self.assertEqual(len(missing), 0, f"Language {l} is missing auth keys: {missing}")

    def test_password_reset_token_generation_and_usage(self):
        uid = uuid.uuid4().hex[:6]
        user = self.auth_store.register_user(
            username=f"reset_user_{uid}",
            email=f"reset_{uid}@example.com",
            password="OriginalPassword123!",
        )
        # Create token
        token_info = self.auth_store.create_password_reset_token(f"reset_user_{uid}")
        self.assertIsNotNone(token_info)
        token = token_info["token"]

        # Reset password
        updated_user = self.auth_store.verify_and_use_reset_token(token, "NewPassword456!")
        self.assertEqual(updated_user["id"], user["id"])

        # Authenticate with new password
        auth_user = self.auth_store.authenticate_user(f"reset_user_{uid}", "NewPassword456!")
        self.assertEqual(auth_user["id"], user["id"])

        # Old password should no longer work
        with self.assertRaises(ValueError):
            self.auth_store.authenticate_user(f"reset_user_{uid}", "OriginalPassword123!")

        # Reusing the token must fail
        with self.assertRaises(ValueError):
            self.auth_store.verify_and_use_reset_token(token, "AnotherPassword789!")

    def test_api_forgot_and_reset_password(self):
        from unittest.mock import patch

        uid = uuid.uuid4().hex[:6]
        self.client.post(
            "/api/auth/signup",
            json={
                "username": f"api_reset_{uid}",
                "email": f"api_reset_{uid}@example.com",
                "password": "InitialPassword123!",
            },
        )

        with patch("bot.webapp.send_password_reset_email") as mock_email:
            mock_email.return_value = True
            # Request forgot password
            res = self.client.post(
                "/api/auth/forgot-password",
                json={"identifier": f"api_reset_{uid}"},
            )
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json().get("ok"))
            mock_email.assert_called_once()

        # Retrieve token directly from auth store to simulate clicking link
        token_info = self.auth_store.create_password_reset_token(f"api_reset_{uid}")
        token = token_info["token"]

        # Reset password via API
        reset_res = self.client.post(
            "/api/auth/reset-password",
            json={"token": token, "password": "UpdatedPassword456!"},
        )
        self.assertEqual(reset_res.status_code, 200)
        self.assertTrue(reset_res.json().get("ok"))

        # Login with new password
        login_res = self.client.post(
            "/api/auth/login",
            json={"identifier": f"api_reset_{uid}", "password": "UpdatedPassword456!"},
        )
        self.assertEqual(login_res.status_code, 200)
        self.assertTrue(login_res.json().get("ok"))

    def test_send_password_reset_email_formatting(self):
        from unittest.mock import patch
        from bot.email_service import send_password_reset_email, render_password_reset_email

        mock_config = {
            "host": "smtp.example.com",
            "port": 465,
            "username": "no-reply@example.com",
            "password": "secretpassword",
            "from_email": "no-reply@example.com",
            "use_ssl": True,
            "configured": True,
        }
        with patch("bot.email_service.get_smtp_config", return_value=mock_config), \
             patch("smtplib.SMTP_SSL") as mock_smtp_ssl:
            mock_server = mock_smtp_ssl.return_value
            result = send_password_reset_email(
                to_email="test@example.com",
                username="testtrader",
                reset_url="https://algopaca.local/reset-password?token=sample123",
                lang="en",
            )
            self.assertTrue(result)
            mock_server.sendmail.assert_called_once()
            args, _ = mock_server.sendmail.call_args
            self.assertIn("test@example.com", args[1])

        # Verify English template rendering
        subj_en, text_en, html_en = render_password_reset_email(
            username="TraderAlpha",
            reset_url="https://algopaca.local/reset-password?token=tok123",
            lang="en",
        )
        self.assertEqual(subj_en, "AlgoPaca - Password Reset Request")
        self.assertIn("TraderAlpha", text_en)
        self.assertIn("TraderAlpha", html_en)
        self.assertIn("AlgoPaca", html_en)
        self.assertIn("Reset Password", html_en)
        self.assertIn("QUANTITATIVE TRADING DESK", html_en)
        self.assertIn("https://algopaca.local/reset-password?token=tok123", html_en)

        # Verify Bengali template rendering
        subj_bn, text_bn, html_bn = render_password_reset_email(
            username="TraderBeta",
            reset_url="https://algopaca.local/reset-password?token=tok456",
            lang="bn",
        )
        self.assertEqual(subj_bn, "AlgoPaca - পাসওয়ার্ড রিসেট লিংক")
        self.assertIn("TraderBeta", text_bn)
        self.assertIn("TraderBeta", html_bn)
        self.assertIn("পাসওয়ার্ড রিসেট অনুরোধ", html_bn)
        self.assertIn("https://algopaca.local/reset-password?token=tok456", html_bn)

        # Verify HTML escaping
        _, _, html_xss = render_password_reset_email(
            username="<script>alert(1)</script>",
            reset_url="https://algopaca.local/reset?foo=1&bar=2",
            lang="en",
        )
        self.assertNotIn("<script>", html_xss)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_xss)
        self.assertIn("https://algopaca.local/reset?foo=1&amp;bar=2", html_xss)


if __name__ == "__main__":
    unittest.main()


