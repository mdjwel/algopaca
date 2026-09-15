"""Unit tests for strategy trade dollar sizing in Auto Trade."""

import unittest
from unittest.mock import MagicMock

from bot.config import Config
from bot.trader import TradingBot
from bot.day_trader import DayTradingBot
from bot.ls_trader import LsTradingBot
from bot.pair_trader import PairTradingBot
from bot.multi_trader import TickerRunner


def _make_config(**kwargs) -> Config:
    defaults = {
        "api_key": "test_key",
        "secret_key": "test_secret",
        "symbol": "AAPL",
        "symbols": ("AAPL",),
        "trade_qty": 1.0,
        "size_mode": "qty",
        "trade_notional": 100.0,
        "risk_engine_enabled": True,
        "ai_risk_pct": 0.5,
        "ai_atr_stop_mult": 1.8,
        "paper": True,
    }
    defaults.update(kwargs)
    return Config.default(**defaults)


class TestStrategyDollarSizing(unittest.TestCase):
    def test_trading_bot_notional_dollar_sizing(self):
        """When size_mode='notional' and trade_notional=500, order size is 500 / price."""
        cfg = _make_config(
            size_mode="notional",
            trade_notional=500.0,
            risk_engine_enabled=True,
            ai_risk_pct=1.0,
        )
        bot = TradingBot(cfg, service=MagicMock())
        # price = 100, stop_dist = 5, equity = 10000
        qty = bot._entry_qty(price=100.0, stop_distance=5.0, equity=10000.0)
        self.assertAlmostEqual(qty, 5.0)

    def test_trading_bot_flat_qty_sizing(self):
        """When size_mode='qty', flat trade_qty is respected."""
        cfg = _make_config(
            size_mode="qty",
            trade_qty=3.0,
            risk_engine_enabled=True,
            ai_risk_pct=1.0,
        )
        bot = TradingBot(cfg, service=MagicMock())
        qty = bot._entry_qty(price=100.0, stop_distance=5.0, equity=10000.0)
        self.assertAlmostEqual(qty, 3.0)

    def test_day_trading_bot_notional_dollar_sizing(self):
        """DayTradingBot respects notional dollar sizing."""
        cfg = _make_config(
            strategy_mode="day",
            size_mode="notional",
            trade_notional=300.0,
            risk_engine_enabled=True,
            ai_risk_pct=1.0,
        )
        bot = DayTradingBot(cfg, service=MagicMock())
        qty = bot._entry_qty(price=50.0, stop_distance=2.0, equity=10000.0)
        self.assertAlmostEqual(qty, 6.0)

    def test_ls_trading_bot_notional_dollar_sizing(self):
        """LsTradingBot respects notional dollar sizing when specified."""
        cfg = _make_config(
            strategy_mode="ls",
            size_mode="notional",
            trade_notional=250.0,
        )
        bot = LsTradingBot(cfg, service=MagicMock())
        qty = bot._entry_qty(price=25.0, atr=1.5)
        self.assertAlmostEqual(qty, 10.0)

    def test_pair_trading_bot_notional_dollar_sizing(self):
        """PairTradingBot deploys configured dollar amount when notional."""
        cfg = _make_config(
            strategy_mode="pair",
            size_mode="notional",
            trade_notional=1000.0,
            symbol="GLD",
            symbols=("GLD", "SLV"),
            pair_long_symbol="GLD",
            pair_short_symbol="SLV",
        )
        service = MagicMock()
        service.account_summary.return_value = {"cash": 50000.0, "equity": 50000.0}
        bot = PairTradingBot(cfg, service=service)
        qty = bot._size_qty(price=200.0)
        self.assertAlmostEqual(qty, 5.0)

    def test_ticker_runner_snapshot_sizing_display(self):
        """TickerRunner snapshot includes size_display and trade_notional."""
        runner = TickerRunner(
            symbol="NVDA",
            strategy_mode="sma",
            settings={"size_mode": "notional", "trade_notional": 750.0},
        )
        snap = runner.snapshot()
        self.assertEqual(snap["size_mode"], "notional")
        self.assertEqual(snap["size_display"], "$750.00")
        self.assertAlmostEqual(snap["trade_notional"], 750.0)

        runner_qty = TickerRunner(
            symbol="TSLA",
            strategy_mode="dip",
            settings={"size_mode": "qty", "trade_qty": 4.0},
        )
        snap_qty = runner_qty.snapshot()
        self.assertEqual(snap_qty["size_mode"], "qty")
        self.assertEqual(snap_qty["size_display"], "4 sh")
        self.assertAlmostEqual(snap_qty["trade_qty"], 4.0)


if __name__ == "__main__":
    unittest.main()
