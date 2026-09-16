"""Unit tests for bot.macro_releases."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot.macro_releases import (
    enrich_economic_calendar_realtime,
    extract_fomc_statement_details,
    get_active_macro_catalyst,
    parse_rate_fraction,
)


class TestMacroReleases(unittest.TestCase):
    def test_parse_rate_fraction(self) -> None:
        self.assertEqual(parse_rate_fraction("4"), 4.0)
        self.assertEqual(parse_rate_fraction("4.25%"), 4.25)
        self.assertEqual(parse_rate_fraction("3-3/4"), 3.75)
        self.assertEqual(parse_rate_fraction("5-1/4 percent"), 5.25)
        self.assertEqual(parse_rate_fraction("5 1/2"), 5.5)

    def test_extract_fomc_statement_details_hike(self) -> None:
        text = (
            "The Committee decided to raise the target range for the federal funds rate "
            "by 1/4 percentage point to 3-3/4 to 4 percent, in support of its dual mandate."
        )
        res = extract_fomc_statement_details(text)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["action"], "hike")
        self.assertEqual(res["change_bps"], 25)
        self.assertEqual(res["rate"], "4.00%")
        self.assertEqual(res["target_rate"], 4.0)
        self.assertEqual(res["target_rate_lower"], 3.75)

    def test_extract_fomc_statement_details_cut(self) -> None:
        text = (
            "The Committee decided to lower the target range for the federal funds rate "
            "by 1/2 percentage point to 4-1/2 to 4-3/4 percent."
        )
        res = extract_fomc_statement_details(text)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["action"], "cut")
        self.assertEqual(res["change_bps"], -50)
        self.assertEqual(res["rate"], "4.75%")

    def test_extract_fomc_statement_details_maintain(self) -> None:
        text = (
            "The Committee decided to maintain the target range for the federal funds rate "
            "at 5-1/4 to 5-1/2 percent."
        )
        res = extract_fomc_statement_details(text)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["action"], "maintain")
        self.assertEqual(res["change_bps"], 0)
        self.assertEqual(res["rate"], "5.50%")

    @patch("bot.macro_releases.fetch_fomc_live_release")
    def test_enrich_economic_calendar_realtime(self, mock_fomc) -> None:
        mock_fomc.return_value = {
            "title": "Federal Funds Rate",
            "actual": "4.00%",
            "action": "hike",
            "change_bps": 25,
            "bias": "hawkish",
            "metals_impact": "Bearish headwind for GLD/SLV; favorable for inverse ETFs (GLL/GDXD).",
            "status": "released",
            "released": True,
        }
        now = datetime.now(timezone.utc)
        calendar = [
            {
                "title": "Federal Funds Rate",
                "impact": "High",
                "when_utc": (now - timedelta(minutes=15)).isoformat(),
                "forecast": "4.00%",
                "previous": "3.75%",
                "actual": "",
            },
            {
                "title": "FOMC Press Conference",
                "impact": "High",
                "when_utc": (now + timedelta(minutes=15)).isoformat(),
                "actual": "",
            },
            {
                "title": "Philly Fed Manufacturing Index",
                "impact": "Medium",
                "when_utc": (now + timedelta(hours=18)).isoformat(),
                "actual": "",
            },
        ]

        enriched = enrich_economic_calendar_realtime(calendar, now_utc=now)
        self.assertEqual(len(enriched), 3)

        fed_event = enriched[0]
        self.assertEqual(fed_event["status"], "released")
        self.assertTrue(fed_event["released"])
        self.assertEqual(fed_event["actual"], "4.00%")
        self.assertEqual(fed_event["action"], "hike")
        self.assertIn("HIKE (+25 bps) to 4.00%", fed_event.get("outcome", ""))

        press_event = enriched[1]
        self.assertTrue(press_event.get("rate_already_released"))

        future_event = enriched[2]
        self.assertEqual(future_event["status"], "upcoming")
        self.assertFalse(future_event["released"])

    @patch("bot.macro_releases.fetch_fomc_live_release")
    def test_get_active_macro_catalyst(self, mock_fomc) -> None:
        mock_fomc.return_value = {
            "title": "Federal Funds Rate",
            "actual": "4.00%",
            "action": "hike",
            "change_bps": 25,
            "bias": "hawkish",
            "metals_impact": "Bearish headwind for GLD/SLV",
            "status": "released",
            "released": True,
        }
        now = datetime.now(timezone.utc)
        calendar = [
            {
                "title": "Federal Funds Rate",
                "impact": "High",
                "when_utc": (now - timedelta(minutes=20)).isoformat(),
                "actual": "4.00%",
                "released": True,
            }
        ]
        catalyst = get_active_macro_catalyst(calendar, now_utc=now)
        self.assertIsNotNone(catalyst)
        assert catalyst is not None
        self.assertEqual(catalyst["actual"], "4.00%")
        self.assertEqual(catalyst["action"], "hike")
        self.assertEqual(catalyst["bias"], "hawkish")


if __name__ == "__main__":
    unittest.main()
