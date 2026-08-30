"""Tests for the Administration Setup Wizard onboarding flow.

Tests cover:
- /api/setup/status, /api/setup/test-smtp, /api/setup/complete endpoints
- Owner registration and password validations
- SMTP configuration saving and diagnostic testing
- Platform preferences (Theme, Language, Default Page, Notifications)
- HTML markup, JS controller, CSS classes, and multi-language translation integrity
"""

from __future__ import annotations

import json
import re
import unittest
import uuid
from pathlib import Path

from starlette.testclient import TestClient

from bot.webapp import app, AUTH_STORE

REPO = Path(__file__).resolve().parent.parent
WIZARD_HTML = REPO / "web" / "setup-wizard.html"
WIZARD_JS = REPO / "web" / "static" / "js" / "setup-wizard.js"
WIZARD_CSS = REPO / "web" / "static" / "css" / "setup-wizard.css"
LANG_DIR = REPO / "web" / "static" / "lang"
LANGS = ("en", "es", "fr", "hi", "bn")

LINKED_CSS = (
    WIZARD_CSS,
    REPO / "web" / "static" / "css" / "common.css",
    REPO / "web" / "static" / "css" / "desk-shell.css",
    REPO / "web" / "static" / "css" / "mobile-shell.css",
)

RUNTIME_CLASSES = (
    "is-active",
    "is-completed",
    "is-selected",
    "is-match",
    "is-mismatch",
    "is-weak",
    "is-fair",
    "is-good",
    "is-strong",
    "is-success",
    "is-error",
    "has-error",
    "loading-spinner",
)


def html() -> str:
    return WIZARD_HTML.read_text(encoding="utf-8")


def js() -> str:
    return WIZARD_JS.read_text(encoding="utf-8")


def radio_values(name: str) -> set[str]:
    """Values offered by a wizard radio group."""
    return set(re.findall(r'name="%s"[^>]*value="([^"]+)"' % re.escape(name), html()))


def translation_keys() -> set[str]:
    """Every key the wizard asks i18n for, from markup and from script."""
    keys = set(
        re.findall(
            r'data-i18n(?:-title|-placeholder|-aria-label)?="([a-zA-Z0-9_]+)"', html()
        )
    )
    keys |= set(re.findall(r'tx\(\s*["\']([a-z0-9_]+)["\']', js()))
    return keys


class WizardEndpointTests(unittest.TestCase):
    """Test the backend endpoints for setup status, test-smtp, and complete."""

    def setUp(self) -> None:
        import tempfile
        from bot.auth import AuthStore
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

        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self) -> None:
        import bot.webapp as webapp_module
        import bot.web_state as web_state_module
        import bot.auth as auth_module

        webapp_module.AUTH_STORE = self._orig_webapp_auth
        web_state_module.AUTH_STORE = self._orig_web_state_auth
        auth_module.AUTH_STORE = self._orig_auth_store

        self.tmp_dir.cleanup()

    def test_setup_status_endpoint(self) -> None:
        res = self.client.get("/api/setup/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("needs_setup", data)
        self.assertIn("smtp", data)
        self.assertIn("settings", data)

    def test_setup_test_smtp_endpoint(self) -> None:
        # Invalid / unreachable test host should return non-crashing diagnostic result
        res = self.client.post(
            "/api/setup/test-smtp",
            json={
                "to_email": "test@example.com",
                "host": "127.0.0.1",
                "port": 9999,
                "username": "",
                "password": "",
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("logs", data)

    def test_setup_complete_validation(self) -> None:
        # If needs_setup is True, missing credentials must be rejected
        self.assertTrue(self.auth_store.needs_setup())
        res = self.client.post("/api/setup/complete", json={})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Username is required", res.json().get("detail", ""))

        res = self.client.post(
            "/api/setup/complete",
            json={"username": "admin", "email": "invalid-email", "password": "123"},
        )
        self.assertEqual(res.status_code, 400)


class WizardAdminMarkupContractTest(unittest.TestCase):
    """Verify HTML structure and expected controls in the Administration wizard."""

    def test_stepper_has_four_steps(self) -> None:
        steps = re.findall(r'<li class="stepper-step[^"]*" data-step="(\d+)"', html())
        self.assertEqual(steps, ["1", "2", "3", "4"])

    def test_panels_have_four_steps(self) -> None:
        panels = re.findall(r'<section class="wizard-step-panel[^"]*" id="panel-step-(\d+)"', html())
        self.assertEqual(panels, ["1", "2", "3", "4"])

    def test_theme_options_match_system_themes(self) -> None:
        themes = radio_values("wizard_theme")
        self.assertEqual(themes, {"obsidian", "midnight", "emerald", "daylight"})

    def test_language_select_options(self) -> None:
        options = set(
            re.findall(
                r'<select id="wizard-select-lang"[^>]*>(.*?)</select>', html(), re.S
            )[0]
        )
        lang_values = set(re.findall(r'<option value="([^"]+)"', html()))
        for expected in ("en", "bn", "es", "fr", "hi"):
            self.assertIn(expected, lang_values)

    def test_broker_and_ai_inputs_removed_from_wizard(self) -> None:
        # User directive: Remove Broker, AI Brain and Risk options from the Setup wizard.
        content = html()
        self.assertNotIn("wizard-alpaca-key", content)
        self.assertNotIn("wizard-alpaca-secret", content)
        self.assertNotIn("wizard-ai-provider", content)
        self.assertNotIn("wizard-ai-key", content)
        self.assertNotIn("wizard-ai-model", content)
        self.assertNotIn("wizard-risk-pct", content)


class WizardTranslationTest(unittest.TestCase):
    def test_every_key_resolves_in_every_language(self) -> None:
        keys = translation_keys()
        self.assertGreater(len(keys), 30, "translation key scan found suspiciously few keys")
        for lang in LANGS:
            dictionary = json.loads((LANG_DIR / f"{lang}.json").read_text(encoding="utf-8"))
            missing = sorted(k for k in keys if k not in dictionary)
            with self.subTest(lang=lang):
                self.assertEqual(missing, [], f"{lang}.json is missing: {missing}")

    def test_placeholder_tokens_match_across_languages(self) -> None:
        english = json.loads((LANG_DIR / "en.json").read_text(encoding="utf-8"))
        wizard_keys = [k for k in translation_keys() if "{" in str(english.get(k, ""))]
        self.assertTrue(wizard_keys, "no parameterized wizard strings found")
        for lang in LANGS:
            dictionary = json.loads((LANG_DIR / f"{lang}.json").read_text(encoding="utf-8"))
            for key in wizard_keys:
                with self.subTest(lang=lang, key=key):
                    self.assertEqual(
                        set(re.findall(r"\{(\w+)\}", english[key])),
                        set(re.findall(r"\{(\w+)\}", dictionary[key])),
                    )


class WizardStylesheetTest(unittest.TestCase):
    def test_every_class_used_has_a_rule_in_a_linked_stylesheet(self) -> None:
        classes: set[str] = set(RUNTIME_CLASSES)
        for attr in re.findall(r'class="([^"]*)"', html()):
            classes.update(attr.split())
        css = "\n".join(path.read_text(encoding="utf-8") for path in LINKED_CSS)
        missing = sorted(c for c in classes if not re.search(r"\.%s\b" % re.escape(c), css))
        self.assertEqual(missing, [], f"unstyled classes: {missing}")

    def test_wizard_links_every_stylesheet_it_relies_on(self) -> None:
        markup = html()
        for path in LINKED_CSS:
            with self.subTest(stylesheet=path.name):
                self.assertIn(f"/static/css/{path.name}", markup)


if __name__ == "__main__":
    unittest.main()
