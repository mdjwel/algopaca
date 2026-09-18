"""Regression coverage for bounded Alpaca credential verification."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bot.auth import AuthStore
from bot.client import AlpacaService
from bot.web_state import AppState


class TestAlpacaKeySaveTimeout(unittest.TestCase):
    def test_trading_session_gets_a_short_timeout(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.kwargs = None

            def request(self, *args, **kwargs):
                self.kwargs = kwargs

        session = Session()
        client = SimpleNamespace(_session=session)

        AlpacaService._set_trading_request_timeout(client)
        session.request("GET", "https://example.test/account")

        self.assertEqual(session.kwargs["timeout"], 12.0)

    def test_explicit_trading_timeout_is_preserved(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.kwargs = None

            def request(self, *args, **kwargs):
                self.kwargs = kwargs

        session = Session()
        client = SimpleNamespace(_session=session)

        AlpacaService._set_trading_request_timeout(client)
        session.request("GET", "https://example.test/account", timeout=3.0)

        self.assertEqual(session.kwargs["timeout"], 3.0)

    def test_credentials_remain_saved_when_account_verification_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = AuthStore(db_path=Path(tmp_dir) / "auth.db")
            user = store.register_user(
                username="timeout_user",
                email="timeout@example.test",
                password="Password123!",
                display_name="Timeout User",
            )
            with patch("bot.web_state.AUTH_STORE", store):
                state = AppState(user_id=user["id"])
                with patch.object(
                    state,
                    "refresh_account",
                    side_effect=TimeoutError("Alpaca request timed out"),
                ):
                    status = state.apply_alpaca_keys(
                        alpaca_api_key="PK_TEST_KEY",
                        alpaca_secret_key="SK_TEST_SECRET",
                        environment="paper",
                        save_to_env=False,
                    )

            saved = store.get_user_credentials(user["id"])
            self.assertEqual(saved["alpaca_paper_api_key"], "PK_TEST_KEY")
            self.assertEqual(saved["alpaca_paper_secret_key"], "SK_TEST_SECRET")
            self.assertTrue(status["paper_keys"]["set"])
            self.assertIn("timed out", status["account_error"])

    def test_app_state_lock_allows_nested_snapshot_helpers(self) -> None:
        """A snapshot may call helpers that read state again on the same thread."""
        state = AppState(user_id="reentrant_lock_test")

        self.assertTrue(state.lock.acquire(timeout=0.1))
        try:
            self.assertTrue(state.lock.acquire(timeout=0.1))
        finally:
            state.lock.release()
            state.lock.release()


if __name__ == "__main__":
    unittest.main()
