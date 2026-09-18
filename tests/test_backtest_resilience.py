"""Regression coverage for bounded, multi-symbol backtest data reads."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
from fastapi import HTTPException

from bot.client import AlpacaService
from bot.webapp import BacktestIn, backtest
from bot.web_state import AppState


class TestBacktestResilience(unittest.TestCase):
    def test_backtest_rejects_missing_active_alpaca_credentials_before_data_read(self) -> None:
        state = AppState(user_id="test_backtest_missing_credentials")

        with patch.object(state, "_base_config") as mock_config, patch(
            "bot.web_state.AlpacaService"
        ) as service:
            mock_config.return_value = SimpleNamespace(
                api_key="",
                secret_key="",
                paper=True,
            )
            with self.assertRaisesRegex(
                ValueError,
                "Alpaca Paper credentials are incomplete.*Open API Keys",
            ):
                state.require_backtest_market_data_credentials()

        service.assert_not_called()

    def test_backtest_endpoint_returns_missing_credentials_message_immediately(self) -> None:
        state = MagicMock()
        state.require_backtest_market_data_credentials.side_effect = ValueError(
            "Alpaca Paper credentials are incomplete (missing API key and secret key). "
            "Open API Keys, add both credentials for the active environment, then run the backtest again."
        )
        with patch("bot.webapp.get_user_state", return_value=state):
            with self.assertRaises(HTTPException) as ctx:
                backtest(BacktestIn(mode="sma"), {"id": 99})

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Open API Keys", str(ctx.exception.detail))
        state.run_strategy_backtest.assert_not_called()

    def test_market_data_session_gets_a_default_timeout(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.kwargs = None

            def request(self, *args, **kwargs):
                self.kwargs = kwargs

        session = Session()
        client = SimpleNamespace(_session=session)

        AlpacaService._set_market_data_timeout(client)
        session.request("GET", "https://example.test/data")

        self.assertEqual(session.kwargs["timeout"], 20.0)

    def test_get_bars_range_many_uses_one_request_and_splits_symbols(self) -> None:
        index = pd.MultiIndex.from_product(
            [
                ["GLD", "SLV"],
                pd.date_range("2026-01-02 14:30", periods=2, freq="h", tz="UTC"),
            ],
            names=["symbol", "timestamp"],
        )
        frame = pd.DataFrame(
            {"open": [1.0, 2.0, 3.0, 4.0], "close": [1.0, 2.0, 3.0, 4.0]},
            index=index,
        )
        service = AlpacaService.__new__(AlpacaService)
        service.config = SimpleNamespace(bar_timeframe="1Hour")
        service.data = MagicMock()
        service.data.get_stock_bars.return_value = SimpleNamespace(df=frame)

        end = datetime(2026, 1, 3, tzinfo=timezone.utc)
        result = service.get_bars_range_many(
            ["GLD", "SLV", "GLD"],
            start=end - timedelta(days=1),
            end=end,
            timeframe="1Hour",
        )

        self.assertEqual(service.data.get_stock_bars.call_count, 1)
        self.assertEqual(set(result), {"GLD", "SLV"})
        self.assertEqual(len(result["GLD"]), 2)
        self.assertEqual(len(result["SLV"]), 2)

    def test_ai_metals_backtest_batches_primary_and_macro_symbols(self) -> None:
        index = pd.date_range("2026-01-02 14:30", periods=80, freq="h", tz="UTC")
        bars = pd.DataFrame(
            {
                "open": [100.0] * len(index),
                "high": [101.0] * len(index),
                "low": [99.0] * len(index),
                "close": [100.5] * len(index),
                "volume": [1000.0] * len(index),
            },
            index=index,
        )
        state = AppState(user_id="test_backtest_batch")
        service = MagicMock()
        service.get_bars_range_many.return_value = {
            symbol: bars for symbol in ("GLD", "SLV", "GLL", "TLT", "UUP", "USO")
        }

        with patch("bot.web_state.AlpacaService", return_value=service), patch(
            "bot.web_state.fetch_economic_calendar", return_value=[]
        ), patch("bot.web_state.run_ai_backtest", return_value={"trades": 0}):
            state._run_ai_backtest(
                days=30,
                bar_timeframe="1Hour",
                initial_cash=20_000.0,
                symbols="GLD, SLV, GLL",
                symbol="GLD",
                ai_preset="gold_silver_macro",
            )

        service.get_bars_range_many.assert_called_once()
        requested = service.get_bars_range_many.call_args.args[0]
        self.assertEqual(requested, ["GLD", "SLV", "GLL", "TLT", "UUP", "USO"])

    def test_backtest_client_has_network_recovery_message(self) -> None:
        script = Path("web/static/js/backtest.js").read_text(encoding="utf-8")

        self.assertIn("Cannot reach the AlgoPaca server", script)
        self.assertIn("You appear to be offline", script)
        self.assertIn("bt-error-api-keys", script)


if __name__ == "__main__":
    unittest.main()
