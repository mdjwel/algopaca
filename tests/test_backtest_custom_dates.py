import unittest
from datetime import datetime, timezone, timedelta
import pandas as pd
from pydantic import ValidationError

from bot.webapp import BacktestIn
from bot.web_state import AppState


class TestBacktestCustomDates(unittest.TestCase):
    def test_backtest_in_model_dates(self):
        # Valid custom dates
        payload = BacktestIn(
            mode="sma",
            symbol="AAPL",
            days=90,
            start_date="2024-01-01",
            end_date="2024-03-31",
        )
        self.assertEqual(payload.start_date, "2024-01-01")
        self.assertEqual(payload.end_date, "2024-03-31")

        # Invalid date format
        with self.assertRaises(ValidationError):
            BacktestIn(
                mode="sma",
                symbol="AAPL",
                start_date="01-01-2024",
                end_date="2024-03-31",
            )

    def test_parse_backtest_dates_default_days(self):
        days_i, cutoff, end, s_iso, e_iso = AppState._parse_backtest_dates(
            days=45,
            bar_timeframe="1Day",
        )
        self.assertEqual(days_i, 45)
        self.assertIsNone(s_iso)
        self.assertIsNone(e_iso)
        self.assertAlmostEqual((end - cutoff).total_seconds(), 45 * 86400, delta=2)

    def test_parse_backtest_dates_custom_range(self):
        days_i, cutoff, end, s_iso, e_iso = AppState._parse_backtest_dates(
            days=365,
            start_date="2023-01-01",
            end_date="2023-03-02",
            bar_timeframe="1Day",
        )
        self.assertEqual(days_i, 60)
        self.assertEqual(s_iso, "2023-01-01")
        self.assertEqual(e_iso, "2023-03-02")
        self.assertEqual(cutoff, datetime(2023, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2023, 3, 2, 23, 59, 59, tzinfo=timezone.utc))

    def test_parse_backtest_dates_inverted_dates_raises(self):
        with self.assertRaises(ValueError) as ctx:
            AppState._parse_backtest_dates(
                days=30,
                start_date="2023-05-01",
                end_date="2023-01-01",
            )
        self.assertIn("cannot be after", str(ctx.exception))

    def test_parse_backtest_dates_future_dates_raises(self):
        future_year = datetime.now(timezone.utc).year + 2
        with self.assertRaises(ValueError) as ctx:
            AppState._parse_backtest_dates(
                days=30,
                start_date=f"{future_year}-01-01",
                end_date=f"{future_year}-02-01",
            )
        self.assertIn("cannot be in the future", str(ctx.exception))

    def test_parse_backtest_dates_intraday_cap(self):
        # 45 days is fine for intraday
        days_i, _, _, _, _ = AppState._parse_backtest_dates(
            days=365,
            start_date="2023-01-01",
            end_date="2023-02-14",
            bar_timeframe="15Min",
        )
        self.assertEqual(days_i, 44)

        # > 60 days raises for intraday
        with self.assertRaises(ValueError) as ctx:
            AppState._parse_backtest_dates(
                days=365,
                start_date="2023-01-01",
                end_date="2023-04-01",
                bar_timeframe="15Min",
            )
        self.assertIn("Intraday backtests are limited to 60 days", str(ctx.exception))

    def test_trim_backtest_bars_with_start_cutoff(self):
        state = AppState.__new__(AppState)
        dates = pd.date_range("2023-01-01", "2023-06-30", freq="D", tz=timezone.utc)
        df = pd.DataFrame({"close": range(len(dates))}, index=dates)

        start_cutoff = datetime(2023, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2023, 6, 30, 23, 59, 59, tzinfo=timezone.utc)

        trimmed = state._trim_backtest_bars(
            df,
            days_i=90,
            end=end,
            start_cutoff=start_cutoff,
        )
        self.assertFalse(trimmed.empty)
        # Verify rows on/after start_cutoff are preserved
        eval_window = trimmed[trimmed.index >= start_cutoff]
        self.assertEqual(len(eval_window), len(df[df.index >= start_cutoff]))
        # Verify warmup rows before start_cutoff are preserved (up to 150)
        warmup = trimmed[trimmed.index < start_cutoff]
        self.assertGreater(len(warmup), 0)

    def test_run_strategy_backtest_forwards_custom_dates(self):
        from unittest.mock import patch

        state = AppState(user_id="test_custom_dates_routing")
        with patch.object(
            AppState, "_run_day_backtest", return_value={"mode": "day", "start_date": "2024-01-01", "end_date": "2024-01-20"}
        ) as mock_day:
            res = state.run_strategy_backtest(
                mode="day",
                days=30,
                start_date="2024-01-01",
                end_date="2024-01-20",
                bar_timeframe="15Min",
            )
            self.assertEqual(res["start_date"], "2024-01-01")
            self.assertEqual(res["end_date"], "2024-01-20")
            mock_day.assert_called_once()
            _, kwargs = mock_day.call_args
            self.assertEqual(kwargs.get("start_date"), "2024-01-01")
            self.assertEqual(kwargs.get("end_date"), "2024-01-20")


if __name__ == "__main__":
    unittest.main()
