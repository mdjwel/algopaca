"""Unit tests for the Day Trading Strategy Engine in AlgoPaca."""

from __future__ import annotations

import datetime as dt
import random
import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import pytz

from bot.config import Config
from bot.day_presets import (
    DEFAULT_PRESET_ID,
    get_preset,
    list_presets,
    match_preset_id,
    resolve_preset_id,
)
from bot.day_strategy import (
    DayTradingStrategy,
    compute_intraday_vwap,
    compute_opening_range,
)
from bot.day_trader import (
    DayTradingBot,
    get_daily_trades_count,
    increment_daily_trades_count,
    reset_daily_trades,
)
from bot.strategy import Signal
from bot.web_state import AppState


def _make_intraday_bars(
    count: int = 50,
    base_price: float = 100.0,
    trend: float = 0.5,
    start_time: dt.datetime | None = None,
    step_minutes: int = 5,
) -> pd.DataFrame:
    """Helper to generate mock intraday bar data."""
    if start_time is None:
        ny = pytz.timezone("America/New_York")
        today = dt.date.today()
        start_time = ny.localize(dt.datetime(today.year, today.month, today.day, 9, 30))

    times = [start_time + dt.timedelta(minutes=step_minutes * i) for i in range(count)]
    data = []
    price = base_price
    for t in times:
        open_p = price
        high_p = price + 0.5
        low_p = price - 0.3
        close_p = price + trend
        vol = 10000.0
        data.append({
            "timestamp": t,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": vol,
            "trade_count": 100,
            "vwap": (open_p + high_p + low_p + close_p) / 4.0,
        })
        price = close_p

    df = pd.DataFrame(data)
    df.set_index("timestamp", inplace=True)
    return df


def _make_session_bars(
    count: int = 40,
    base_price: float = 100.0,
    drift: float = 0.10,
    noise: float = 0.35,
    seed: int = 7,
    step_minutes: int = 5,
    start_time: dt.datetime | None = None,
    volume_scale: float = 1.0,
) -> pd.DataFrame:
    """A seeded, realistic intraday session.

    `_make_intraday_bars` draws a perfectly straight ramp, which is fine for the
    VWAP/ORB window maths but useless for the signal filters: a line with no
    pullbacks sits permanently extended above its own fast EMA, so the regime and
    extension screens reject every bar. This walks with drift and noise instead,
    so pullbacks exist and the filters see something a real session would produce.
    """
    rng = random.Random(seed)
    if start_time is None:
        ny = pytz.timezone("America/New_York")
        today = dt.date.today()
        start_time = ny.localize(dt.datetime(today.year, today.month, today.day, 9, 30))

    rows = []
    price = base_price
    for i in range(count):
        stamp = start_time + dt.timedelta(minutes=step_minutes * i)
        open_p = price
        price = max(1.0, price + drift + rng.gauss(0, noise))
        high_p = max(open_p, price) + abs(rng.gauss(0, noise * 0.5))
        low_p = min(open_p, price) - abs(rng.gauss(0, noise * 0.5))
        rows.append({
            "timestamp": stamp,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": price,
            "volume": 10000.0 * volume_scale * (0.8 + rng.random() * 0.6),
            "trade_count": 100,
        })

    df = pd.DataFrame(rows)
    df.set_index("timestamp", inplace=True)
    return df


class TestDayTradingPresets(unittest.TestCase):
    def test_list_presets(self):
        presets = list_presets()
        self.assertGreaterEqual(len(presets), 4)
        ids = {p["id"] for p in presets}
        self.assertIn("vwap_trend", ids)
        self.assertIn("orb_breakout", ids)
        self.assertIn("momentum_scalp", ids)
        self.assertIn("vwap_fade", ids)

    def test_resolve_and_get_preset(self):
        self.assertEqual(resolve_preset_id("vwap_trend"), "vwap_trend")
        self.assertEqual(resolve_preset_id("unknown_id"), DEFAULT_PRESET_ID)
        preset = get_preset("orb_breakout")
        self.assertEqual(preset.sub_mode, "orb")
        self.assertEqual(preset.orb_minutes, 15)

    def test_match_preset_id(self):
        matched = match_preset_id(
            sub_mode="vwap_trend",
            ema_fast=9,
            ema_slow=21,
            orb_minutes=15,
            open_buffer_mins=15,
            eod_flatten_mins=15,
            eod_flatten=True,
            max_trades_per_day=5,
            profit_target_r=2.0,
            stop_atr_mult=1.5,
            side="long_only",
        )
        self.assertEqual(matched, "vwap_trend")

    def test_match_preset_id_resolves_ai_twin(self):
        """The AI presets differ from their technical twins only by the AI knobs."""
        common = dict(
            sub_mode="vwap_trend",
            ema_fast=9,
            ema_slow=21,
            orb_minutes=15,
            open_buffer_mins=15,
            eod_flatten_mins=15,
            eod_flatten=True,
            max_trades_per_day=5,
            profit_target_r=2.0,
            stop_atr_mult=1.5,
            side="long_only",
        )
        self.assertEqual(
            match_preset_id(**common, use_ai_confirm=True, ai_min_confidence=0.70),
            "ai_vwap_momentum",
        )
        self.assertEqual(
            match_preset_id(**common, use_ai_confirm=False, ai_min_confidence=0.65),
            "vwap_trend",
        )

    def test_preset_copy_matches_the_ui_strings(self):
        """Preset ids, labels and summaries must line up with every language file.

        This drifted before: five presets had i18n keys that did not match their
        ids, so their names never translated, and two summaries described rules
        the engine does not run.
        """
        import json
        import pathlib

        lang_dir = pathlib.Path(__file__).resolve().parent.parent / "web/static/lang"
        english = json.loads((lang_dir / "en.json").read_text(encoding="utf-8"))

        for preset in list_presets():
            name_key = f"preset_day_{preset['id']}"
            summary_key = f"day_preset_summary_{preset['id']}"
            self.assertIn(name_key, english, f"missing name key for {preset['id']}")
            self.assertIn(summary_key, english, f"missing summary key for {preset['id']}")
            if preset["id"] != "custom":
                self.assertEqual(
                    english[summary_key],
                    preset["summary"],
                    f"{preset['id']} summary drifted from its English copy",
                )

        for path in sorted(lang_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            missing = [k for k in english if k.startswith(("preset_day_", "day_preset_summary_"))
                       and k not in data]
            self.assertEqual(missing, [], f"{path.name} is missing {missing}")

    def test_default_preset_id_is_consistent(self):
        """Every layer must default to the id day_presets declares."""
        from bot.settings_store import _DEFAULTS
        from bot.web_state import RunSettings
        from bot.webapp import SettingsIn

        self.assertEqual(_DEFAULTS["day_preset"], DEFAULT_PRESET_ID)
        self.assertEqual(RunSettings().day_preset, DEFAULT_PRESET_ID)
        self.assertEqual(SettingsIn().day_preset, DEFAULT_PRESET_ID)


class TestDayTradingStrategy(unittest.TestCase):
    def test_compute_intraday_vwap(self):
        bars = _make_intraday_bars(count=20, base_price=150.0, trend=0.2)
        vwap_data = compute_intraday_vwap(bars)
        self.assertIsNotNone(vwap_data["vwap"])
        self.assertIsNotNone(vwap_data["upper"])
        self.assertIsNotNone(vwap_data["lower"])
        self.assertGreater(vwap_data["vwap"], 0)
        self.assertGreaterEqual(vwap_data["upper"], vwap_data["vwap"])
        self.assertLessEqual(vwap_data["lower"], vwap_data["vwap"])

    def test_compute_opening_range(self):
        bars = _make_intraday_bars(count=20, base_price=100.0, trend=1.0)
        orb_data = compute_opening_range(bars, orb_minutes=15)
        self.assertIsNotNone(orb_data["orh"])
        self.assertIsNotNone(orb_data["orl"])
        self.assertGreaterEqual(orb_data["orh"], orb_data["orl"])
        self.assertTrue(orb_data["is_established"])

    def test_opening_range_is_established_by_the_clock(self):
        """Establishment must follow the ET clock, not a bar count."""
        # 1-minute bars: 09:30-09:43 is still inside a 15m opening range.
        early = _make_intraday_bars(count=14, base_price=100.0, trend=0.1, step_minutes=1)
        self.assertFalse(compute_opening_range(early, orb_minutes=15)["is_established"])

        # One more bar clears 09:45 and locks the range in.
        late = _make_intraday_bars(count=20, base_price=100.0, trend=0.1, step_minutes=1)
        self.assertTrue(compute_opening_range(late, orb_minutes=15)["is_established"])

    def test_opening_range_works_on_coarse_bars(self):
        """15Min bars used to leave the range permanently unestablished."""
        bars = _make_intraday_bars(count=8, base_price=100.0, trend=0.4, step_minutes=15)
        orb_data = compute_opening_range(bars, orb_minutes=15)
        self.assertTrue(orb_data["is_established"])
        self.assertIsNotNone(orb_data["orh"])

    def test_opening_range_spans_only_the_orb_window(self):
        """The range must come from the window, not the whole session."""
        bars = _make_intraday_bars(count=30, base_price=100.0, trend=1.0, step_minutes=5)
        orb_data = compute_opening_range(bars, orb_minutes=15)
        session_high = float(bars["high"].max())
        self.assertLess(orb_data["orh"], session_high)

    def test_vwap_trend_bullish_signal(self):
        bars = _make_intraday_bars(count=40, base_price=100.0, trend=0.8)
        strat = DayTradingStrategy(
            sub_mode="vwap_trend",
            side="long_only",
            ema_fast=9,
            ema_slow=21,
        )
        res = strat.evaluate(bars)
        self.assertIn(res.signal, {Signal.BUY, Signal.HOLD})
        self.assertGreater(res.fast_sma, 0)
        self.assertGreater(res.slow_sma, 0)

    def test_orb_breakout_signal(self):
        bars = _make_intraday_bars(count=30, base_price=100.0, trend=1.2)
        strat = DayTradingStrategy(
            sub_mode="orb",
            side="long_only",
            orb_minutes=15,
        )
        res = strat.evaluate(bars)
        self.assertIn(res.signal, {Signal.BUY, Signal.HOLD})

    def test_momentum_scalp_signal(self):
        bars = _make_intraday_bars(count=35, base_price=200.0, trend=0.5)
        strat = DayTradingStrategy(
            sub_mode="momentum_scalp",
            side="long_short",
            ema_fast=9,
            ema_slow=21,
        )
        res = strat.evaluate(bars)
        self.assertIn(res.signal, {Signal.BUY, Signal.SELL, Signal.HOLD})


class TestDayTradingBotAndTiming(unittest.TestCase):
    def setUp(self):
        reset_daily_trades()

    def test_daily_trades_counter(self):
        self.assertEqual(get_daily_trades_count("AAPL"), 0)
        increment_daily_trades_count("AAPL")
        self.assertEqual(get_daily_trades_count("AAPL"), 1)
        increment_daily_trades_count("AAPL")
        self.assertEqual(get_daily_trades_count("AAPL"), 2)

    @patch("bot.day_trader.AlpacaService")
    def test_session_timing_buffer_and_eod(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service_cls.return_value = mock_service
        config = Config.default(
            strategy_mode="day",
            day_preset="vwap_trend",
            day_open_buffer_mins=15,
            day_eod_flatten_mins=15,
            day_eod_flatten=True,
        )
        bot = DayTradingBot(config, service=mock_service)
        
        # Test Market Open Buffer (9:35 AM ET)
        ny = pytz.timezone("America/New_York")
        now_open_buffer = ny.localize(dt.datetime(2026, 9, 2, 9, 35))
        is_buf, is_eod, msg = bot._check_session_timing(now_open_buffer)
        self.assertTrue(is_buf)
        self.assertFalse(is_eod)
        self.assertIn("Market open buffer active", msg)

        # Test Regular Trading Window (11:00 AM ET)
        now_trading = ny.localize(dt.datetime(2026, 9, 2, 11, 0))
        is_buf, is_eod, msg = bot._check_session_timing(now_trading)
        self.assertFalse(is_buf)
        self.assertFalse(is_eod)
        self.assertEqual(msg, "")

        # Test EOD Flatten Window (3:50 PM ET)
        now_eod = ny.localize(dt.datetime(2026, 9, 2, 15, 50))
        is_buf, is_eod, msg = bot._check_session_timing(now_eod)
        self.assertFalse(is_buf)
        self.assertTrue(is_eod)
        self.assertIn("EOD auto-flatten window active", msg)

    @patch("bot.day_trader.AlpacaService")
    def test_eod_auto_flatten_in_run_symbol(self, mock_service_cls):
        mock_service = MagicMock()
        mock_service.get_bars.return_value = _make_intraday_bars(count=30)
        mock_service.get_position_detail.return_value = {"qty": 10.0, "avg_entry": 150.0}
        mock_service.get_position_qty.return_value = 10.0
        mock_service.get_mark_price.return_value = {"price": 155.0, "session": "regular"}
        mock_service.market_session.return_value = {"is_open": True, "session": "regular"}
        mock_service.submit_order.return_value = MagicMock(id="eod_order_123")
        
        config = Config.default(
            strategy_mode="day",
            symbol="AAPL",
            day_eod_flatten=True,
            day_eod_flatten_mins=15,
        )
        bot = DayTradingBot(config, service=mock_service)
        
        # Patch timing to be inside EOD flatten window
        with patch.object(bot, "_check_session_timing", return_value=(False, True, "EOD test")):
            res = bot._run_symbol("AAPL")
            self.assertEqual(res["signal"], "sell")
            self.assertIn("EOD auto-flatten", res["reason"])
            mock_service.submit_order.assert_called_once()

    @patch("bot.day_trader.AlpacaService")
    def test_eod_flatten_does_not_fire_when_market_closed(self, mock_service_cls):
        """The EOD window is clock maths, so it also matches weekends and holidays."""
        mock_service = MagicMock()
        mock_service.get_bars.return_value = _make_intraday_bars(count=30)
        mock_service.get_position_detail.return_value = {"qty": 10.0, "avg_entry": 150.0}
        mock_service.get_position_qty.return_value = 10.0
        mock_service.get_mark_price.return_value = {"price": 155.0, "session": "closed"}
        mock_service.market_session.return_value = {"is_open": False, "session": "closed"}
        mock_service.current_stop_price.return_value = None

        config = Config.default(strategy_mode="day", symbol="AAPL", day_eod_flatten=True)
        bot = DayTradingBot(config, service=mock_service)

        with patch.object(bot, "_check_session_timing", return_value=(False, True, "EOD test")):
            res = bot._run_symbol("AAPL")
            self.assertNotIn("EOD auto-flatten", res["reason"])
            mock_service.submit_order.assert_not_called()

    @patch("bot.day_trader.AlpacaService")
    def test_eod_window_follows_the_real_closing_bell(self, mock_service_cls):
        """A 13:00 ET half-day must flatten at 12:45, not 15:45."""
        config = Config.default(
            strategy_mode="day", day_eod_flatten=True, day_eod_flatten_mins=15
        )
        bot = DayTradingBot(config, service=MagicMock())
        ny = pytz.timezone("America/New_York")
        half_day_close = dt.time(13, 0)

        _, is_eod, _ = bot._check_session_timing(
            ny.localize(dt.datetime(2026, 11, 27, 12, 50)), close_time=half_day_close
        )
        self.assertTrue(is_eod)

        _, is_eod, _ = bot._check_session_timing(
            ny.localize(dt.datetime(2026, 11, 27, 12, 30)), close_time=half_day_close
        )
        self.assertFalse(is_eod)

        # Past the bell the window is over, not still open.
        _, is_eod, _ = bot._check_session_timing(
            ny.localize(dt.datetime(2026, 11, 27, 13, 30)), close_time=half_day_close
        )
        self.assertFalse(is_eod)

    @patch("bot.day_trader.AlpacaService")
    def test_eod_window_blocks_new_entries(self, mock_service_cls):
        """Opening a position minutes before the square-off defeats the point."""
        mock_service = MagicMock()
        mock_service.get_bars.return_value = _make_session_bars(count=40, drift=0.10)
        mock_service.get_position_detail.return_value = {"qty": 0.0}
        mock_service.get_position_qty.return_value = 0.0
        mock_service.get_mark_price.return_value = {"price": 120.0, "session": "regular"}
        mock_service.market_session.return_value = {"is_open": True, "session": "regular"}
        mock_service.has_open_orders.return_value = False
        mock_service.recent_activity.return_value = {}

        config = Config.default(strategy_mode="day", symbol="AAPL", day_open_buffer_mins=0)
        bot = DayTradingBot(config, service=mock_service)

        with patch.object(bot, "_check_session_timing", return_value=(False, True, "EOD test")):
            res = bot._run_symbol("AAPL")
            self.assertIn("EOD test", res["reason"])
            mock_service.submit_order.assert_not_called()

    def test_daily_trade_cap_counts_entries_only(self):
        """The cap gates new positions, so exits must not consume it."""
        scope = "paper:test"
        self.assertEqual(get_daily_trades_count("AAPL", scope), 0)
        increment_daily_trades_count("AAPL", scope)
        self.assertEqual(get_daily_trades_count("AAPL", scope), 1)
        # A different account's cap is independent.
        self.assertEqual(get_daily_trades_count("AAPL", "paper:other"), 0)


class TestConfigAndWebStateIntegration(unittest.TestCase):
    def test_config_day_fields(self):
        cfg = Config.default(
            strategy_mode="day",
            day_preset="orb_breakout",
            day_orb_minutes=15,
            day_max_trades_per_day=4,
        )
        self.assertEqual(cfg.strategy_mode, "day")
        self.assertEqual(cfg.day_orb_minutes, 15)
        self.assertEqual(cfg.day_max_trades_per_day, 4)

    def test_web_state_update_settings_day_mode(self):
        ws = AppState(user_id="test_user")
        updated = ws.update_settings({
            "strategy_mode": "day",
            "day_preset": "custom",
            "day_side": "long_short",
            "day_ema_fast": 8,
            "day_ema_slow": 21,
            "day_orb_minutes": 15,
            "day_open_buffer_mins": 10,
            "day_eod_flatten_mins": 10,
            "day_eod_flatten": True,
            "day_max_trades_per_day": 6,
            "day_profit_target_r": 2.5,
            "day_stop_atr_mult": 1.8,
        })
        self.assertEqual(updated.strategy_mode, "day")
        self.assertEqual(updated.day_ema_fast, 8)
        self.assertEqual(updated.day_ema_slow, 21)
        self.assertEqual(updated.day_side, "long_short")
        self.assertEqual(updated.day_max_trades_per_day, 6)

    def test_web_state_persists_ai_confirmation_settings(self):
        """The AI confirm knobs must survive the desk round trip into Config."""
        ws = AppState(user_id="test_user_ai_confirm")
        updated = ws.update_settings({
            "strategy_mode": "day",
            "day_preset": "custom",
            "day_use_ai_confirm": True,
            "day_ai_min_confidence": 0.8,
        })
        self.assertTrue(updated.day_use_ai_confirm)
        self.assertAlmostEqual(updated.day_ai_min_confidence, 0.8)

        config = ws._base_config()
        self.assertTrue(config.day_use_ai_confirm)
        self.assertAlmostEqual(config.day_ai_min_confidence, 0.8)
        self.assertTrue(DayTradingBot(config, service=MagicMock()).use_ai_confirm)

    def test_web_state_resolves_the_ai_preset(self):
        """Preset matching must see the AI knobs, or the AI presets are unreachable."""
        ws = AppState(user_id="test_user_ai_preset")
        updated = ws.update_settings({
            "strategy_mode": "day",
            "day_preset": "custom",
            "day_sub_mode": "vwap_trend",
            "day_side": "long_only",
            "day_ema_fast": 9,
            "day_ema_slow": 21,
            "day_orb_minutes": 15,
            "day_open_buffer_mins": 15,
            "day_eod_flatten_mins": 15,
            "day_eod_flatten": True,
            "day_max_trades_per_day": 5,
            "day_profit_target_r": 2.0,
            "day_stop_atr_mult": 1.5,
            "day_use_ai_confirm": True,
            "day_ai_min_confidence": 0.70,
        })
        self.assertEqual(updated.day_preset, "ai_vwap_momentum")

    def test_day_mode_forces_intraday_bars(self):
        """VWAP, the opening range and the EOD square-off need intraday bars."""
        cfg = Config.default(strategy_mode="day", bar_timeframe="1Day")
        self.assertEqual(cfg.bar_timeframe, "5Min")
        self.assertEqual(cfg.override(bar_timeframe="15Min").bar_timeframe, "15Min")

        ws = AppState(user_id="test_user_day_tf")
        updated = ws.update_settings({"strategy_mode": "day", "bar_timeframe": "1Day"})
        self.assertEqual(updated.bar_timeframe, "5Min")

    def test_day_profit_target_reaches_the_risk_engine(self):
        """`day_profit_target_r` used to be collected and then ignored."""
        from bot.ai_risk import should_scale_out

        config = Config.default(
            strategy_mode="day", day_profit_target_r=1.5, ai_take_profit_r=3.0
        )
        bot = DayTradingBot(config, service=MagicMock())
        self.assertEqual(bot._profit_target_r(), 1.5)
        # 2R clears the day target even though the desk-wide target is 3R.
        self.assertTrue(
            should_scale_out(config, r=2.0, already_scaled=False, target_r=bot._profit_target_r())
        )
        self.assertFalse(should_scale_out(config, r=2.0, already_scaled=False))


from bot.day_ai import DayAiBrain, DayAiDecision, normalize_day_ai_decision


class TestDayAiBrain(unittest.TestCase):
    def setUp(self):
        self.config = Config.default(
            strategy_mode="day",
            day_preset="ai_vwap_momentum",
            day_use_ai_confirm=True,
            day_ai_min_confidence=0.70,
        )
        self.mock_service = MagicMock()
        self.mock_service.get_bars.return_value = _make_intraday_bars(count=30)
        self.mock_service.get_mark_price.return_value = {"price": 150.0}

    def test_normalize_day_ai_decision(self):
        raw = {
            "confirm": True,
            "confidence": 0.85,
            "action_bias": "bullish",
            "thesis": "Strong VWAP trend continuation with volume surge",
            "thesis_en": "Strong VWAP trend continuation with volume surge",
            "risk_warning": "RSI nearing 68",
            "risk_warning_en": "RSI nearing 68",
            "target_r_adjustment": 2.2,
        }
        dec = normalize_day_ai_decision(raw, provider="openai", model="gpt-5.6-sol")
        self.assertTrue(dec.confirm)
        self.assertEqual(dec.confidence, 0.85)
        self.assertEqual(dec.action_bias, "bullish")
        self.assertEqual(dec.target_r_adjustment, 2.2)

    @patch("bot.day_ai.fetch_earnings", return_value={"blackout": False, "last_result": None, "plan": ""})
    @patch("bot.day_ai.fetch_economic_calendar", return_value=[])
    @patch("bot.day_ai.fetch_news", return_value=[])
    def test_evaluate_signal_with_mock_provider_confirm(self, mock_news, mock_cal, mock_earn):
        mock_provider = MagicMock()
        mock_provider.name = "mock_ai"
        mock_provider.model = "test_model"
        mock_provider.complete_json.return_value = {
            "confirm": True,
            "confidence": 0.88,
            "action_bias": "bullish",
            "thesis": "Clean breakout with catalyst alignment",
            "thesis_en": "Clean breakout with catalyst alignment",
            "risk_warning": "None",
            "risk_warning_en": "None",
        }

        brain = DayAiBrain(self.config, self.mock_service, provider=mock_provider)
        bars = _make_intraday_bars(count=30)
        dec = brain.evaluate_signal(
            symbol="AAPL",
            signal=Signal.BUY,
            trigger_price=150.0,
            trigger_reason="bullish VWAP crossover",
            bars=bars,
        )
        self.assertTrue(dec.confirm)
        self.assertGreaterEqual(dec.confidence, 0.80)
        self.assertIn("Clean breakout", dec.thesis)

    @patch("bot.day_ai.fetch_earnings", return_value={"blackout": False, "last_result": None, "plan": ""})
    @patch("bot.day_ai.fetch_economic_calendar", return_value=[])
    @patch("bot.day_ai.fetch_news", return_value=[])
    def test_evaluate_signal_with_mock_provider_veto(self, mock_news, mock_cal, mock_earn):
        mock_provider = MagicMock()
        mock_provider.name = "mock_ai"
        mock_provider.model = "test_model"
        mock_provider.complete_json.return_value = {
            "confirm": False,
            "confidence": 0.40,
            "action_bias": "neutral",
            "thesis": "High risk of bull trap into daily resistance",
            "thesis_en": "High risk of bull trap into daily resistance",
            "risk_warning": "FOMC rate decision in 45 minutes",
            "risk_warning_en": "FOMC rate decision in 45 minutes",
        }

        brain = DayAiBrain(self.config, self.mock_service, provider=mock_provider)
        bars = _make_intraday_bars(count=30)
        dec = brain.evaluate_signal(
            symbol="AAPL",
            signal=Signal.BUY,
            trigger_price=150.0,
            trigger_reason="bullish VWAP crossover",
            bars=bars,
        )
        self.assertFalse(dec.confirm)
        self.assertEqual(dec.confidence, 0.40)
        self.assertIn("bull trap", dec.thesis)


class TestDayTradingBotAiConfirmation(unittest.TestCase):
    def setUp(self):
        reset_daily_trades()
        self.mock_service = MagicMock()
        self.mock_service.get_bars.return_value = _make_session_bars(count=40, drift=0.10)
        self.mock_service.get_position_detail.return_value = {"qty": 0.0}
        self.mock_service.get_position_qty.return_value = 0.0
        self.mock_service.get_mark_price.return_value = {"price": 120.0, "session": "regular"}
        self.mock_service.market_session.return_value = {"is_open": True, "session": "regular"}
        self.mock_service.has_open_orders.return_value = False
        self.mock_service.recent_activity.return_value = {}
        self.mock_service.account_summary.return_value = {"equity": 25000.0, "day_pl_pct": 0.5}
        self.mock_service.submit_order.return_value = MagicMock(id="order_ai_day_123")
        self.mock_service.ensure_stop_loss.return_value = {"qty": 1.0, "stop_price": 115.0, "pct": 4.17, "side": "sell"}
        self.mock_service.arm_protective_stop.return_value = {"stop_price": 115.0, "pct": 4.17}

    def test_ai_veto_blocks_new_entry(self):
        config = Config.default(
            strategy_mode="day",
            symbol="AAPL",
            day_preset="ai_vwap_momentum",
            day_use_ai_confirm=True,
            day_ai_min_confidence=0.70,
            day_open_buffer_mins=0,
        )
        mock_brain = MagicMock()
        mock_brain.evaluate_signal.return_value = DayAiDecision(
            confirm=False,
            confidence=0.45,
            action_bias="neutral",
            thesis="Vetoed: CPI announcement in 20 minutes",
            thesis_en="Vetoed: CPI announcement in 20 minutes",
            risk_warning="High CPI volatility",
            risk_warning_en="High CPI volatility",
            target_r_adjustment=None,
            raw={},
        )

        bot = DayTradingBot(config, service=self.mock_service, ai_brain=mock_brain)
        with patch.object(bot, "_check_session_timing", return_value=(False, False, "")):
            res = bot._run_symbol("AAPL")
            self.assertEqual(res["signal"], Signal.HOLD.value)
            self.assertIn("AI Vetoed", res["reason"])
            self.assertIn("risk_blocked", res)
            self.mock_service.submit_order.assert_not_called()

    def test_ai_confirm_allows_new_entry(self):
        config = Config.default(
            strategy_mode="day",
            symbol="AAPL",
            day_preset="ai_vwap_momentum",
            day_use_ai_confirm=True,
            day_ai_min_confidence=0.70,
            day_open_buffer_mins=0,
        )
        mock_brain = MagicMock()
        mock_brain.evaluate_signal.return_value = DayAiDecision(
            confirm=True,
            confidence=0.85,
            action_bias="bullish",
            thesis="Confirmed strong institutional tape momentum",
            thesis_en="Confirmed strong institutional tape momentum",
            risk_warning="",
            risk_warning_en="",
            target_r_adjustment=None,
            raw={},
        )

        bot = DayTradingBot(config, service=self.mock_service, ai_brain=mock_brain)
        with patch.object(bot, "_check_session_timing", return_value=(False, False, "")):
            res = bot._run_symbol("AAPL")
            self.assertEqual(res["signal"], Signal.BUY.value)
            self.assertIn("AI Confirmed 85%", res["reason"])
            self.assertTrue(res.get("ai_confirmed"))
            self.assertEqual(res.get("ai_confidence"), 0.85)
            self.mock_service.submit_order.assert_called_once()


class TestDayTradingShortSupport(unittest.TestCase):
    def test_config_and_web_state_short_only(self):
        cfg = Config.default(strategy_mode="day", day_side="short_only")
        self.assertEqual(cfg.day_side, "short_only")

        ws = AppState(user_id="test_short_user")
        updated = ws.update_settings({"strategy_mode": "day", "day_preset": "custom", "day_side": "short_only"})
        self.assertEqual(updated.day_side, "short_only")

    def test_strategy_vwap_trend_short_only_entry(self):
        bars = _make_intraday_bars(count=40, base_price=150.0, trend=-0.6)
        strat_short = DayTradingStrategy(sub_mode="vwap_trend", side="short_only", quality_filters=False)
        res = strat_short.evaluate(bars)
        self.assertEqual(res.signal, Signal.SELL)
        self.assertIn("VWAP", res.reason)

        # In long_only mode, a downtrend must not produce a BUY entry
        strat_long = DayTradingStrategy(sub_mode="vwap_trend", side="long_only", quality_filters=False)
        res_long = strat_long.evaluate(bars)
        self.assertNotEqual(res_long.signal, Signal.BUY)

    def test_strategy_vwap_trend_short_only_cover(self):
        # Uptrending bars: short_only mode produces BUY as cover signal
        bars = _make_intraday_bars(count=40, base_price=100.0, trend=0.8)
        strat_short = DayTradingStrategy(sub_mode="vwap_trend", side="short_only", quality_filters=False)
        res = strat_short.evaluate(bars)
        self.assertEqual(res.signal, Signal.BUY)
        self.assertIn("cover", res.reason)

    def test_strategy_orb_breakdown_short(self):
        ny = pytz.timezone("America/New_York")
        today = dt.date.today()
        start = ny.localize(dt.datetime(today.year, today.month, today.day, 9, 30))
        # 34 bars inside 98.0 - 102.0
        bars_list = []
        for i in range(34):
            t = start + dt.timedelta(minutes=5 * i)
            bars_list.append({"timestamp": t, "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 10000.0})
        # 35th bar breaks down below ORL (98.0)
        t_last = start + dt.timedelta(minutes=5 * 34)
        bars_list.append({"timestamp": t_last, "open": 98.5, "high": 98.5, "low": 96.0, "close": 96.5, "volume": 15000.0})
        df = pd.DataFrame(bars_list).set_index("timestamp")

        strat = DayTradingStrategy(sub_mode="orb", orb_minutes=15, side="short_only", quality_filters=False)
        res = strat.evaluate(df)
        self.assertEqual(res.signal, Signal.SELL)
        self.assertIn("breakdown", res.reason)

    def test_bot_short_only_execution_and_stop(self):
        mock_service = MagicMock()
        mock_service.get_bars.return_value = _make_intraday_bars(count=40, base_price=150.0, trend=-0.6)
        mock_service.get_position_detail.return_value = {"qty": 0.0, "avg_entry": 0.0}
        mock_service.get_position_qty.return_value = 0.0
        mock_service.get_mark_price.return_value = {"price": 125.0, "session": "regular"}
        mock_service.market_session.return_value = {"is_open": True, "session": "regular"}
        mock_service.account_summary.return_value = {"equity": 25000.0, "day_pl_pct": 0.0}
        mock_service.recent_activity.return_value = {}
        mock_service.submit_order.return_value = MagicMock(id="short_order_123")
        mock_service.has_open_orders.return_value = False
        mock_service.current_stop_price.return_value = None
        mock_service.ensure_stop_loss.return_value = {"qty": 10.0, "stop_price": 130.0, "pct": 4.0, "side": "buy"}
        mock_service.arm_protective_stop.return_value = {"stop_price": 130.0, "pct": 4.0}

        config = Config.default(
            strategy_mode="day",
            symbol="AAPL",
            day_preset="custom",
            day_sub_mode="vwap_trend",
            day_side="short_only",
            day_open_buffer_mins=0,
            day_use_ai_confirm=False,
        )
        bot = DayTradingBot(config, service=mock_service)
        bot.strategy.quality_filters = False
        with patch.object(bot, "_check_session_timing", return_value=(False, False, "")):
            res = bot._run_symbol("AAPL")
            self.assertEqual(res["signal"], "sell")
            self.assertEqual(res["intent"], "open_short")
            mock_service.submit_order.assert_called_once()
            # Verify protective stop loss armed
            mock_service.ensure_stop_loss.assert_called_once()

    def test_bot_short_only_skips_buy_entry(self):
        mock_service = MagicMock()
        mock_service.get_bars.return_value = _make_intraday_bars(count=40, base_price=100.0, trend=0.8)
        mock_service.get_position_detail.return_value = {"qty": 0.0, "avg_entry": 0.0}
        mock_service.get_position_qty.return_value = 0.0
        mock_service.get_mark_price.return_value = {"price": 130.0, "session": "regular"}
        mock_service.market_session.return_value = {"is_open": True, "session": "regular"}
        mock_service.account_summary.return_value = {"equity": 25000.0, "day_pl_pct": 0.0}
        mock_service.recent_activity.return_value = {}
        mock_service.has_open_orders.return_value = False

        config = Config.default(
            strategy_mode="day",
            symbol="AAPL",
            day_preset="custom",
            day_sub_mode="vwap_trend",
            day_side="short_only",
            day_open_buffer_mins=0,
            day_use_ai_confirm=False,
        )
        bot = DayTradingBot(config, service=mock_service)
        bot.strategy.quality_filters = False
        with patch.object(bot, "_check_session_timing", return_value=(False, False, "")):
            res = bot._run_symbol("AAPL")
            self.assertIn("short-only mode", res["reason"])
            mock_service.submit_order.assert_not_called()

    def test_bot_short_cover_on_buy(self):
        mock_service = MagicMock()
        mock_service.get_bars.return_value = _make_intraday_bars(count=40, base_price=100.0, trend=0.8)
        mock_service.get_position_detail.return_value = {"qty": -10.0, "avg_entry": 140.0}
        mock_service.get_position_qty.return_value = -10.0
        # Price at 140 (break-even) so scale-out does not trigger
        mock_service.get_mark_price.return_value = {"price": 140.0, "session": "regular"}
        mock_service.market_session.return_value = {"is_open": True, "session": "regular"}
        mock_service.account_summary.return_value = {"equity": 25000.0, "day_pl_pct": 0.0}
        mock_service.recent_activity.return_value = {}
        mock_service.submit_order.return_value = MagicMock(id="cover_order_123")
        mock_service.has_open_orders.return_value = False

        config = Config.default(
            strategy_mode="day",
            symbol="AAPL",
            day_preset="custom",
            day_sub_mode="vwap_trend",
            day_side="short_only",
            day_open_buffer_mins=0,
            day_use_ai_confirm=False,
        )
        bot = DayTradingBot(config, service=mock_service)
        bot.strategy.quality_filters = False
        with patch.object(bot, "_check_session_timing", return_value=(False, False, "")):
            res = bot._run_symbol("AAPL")
            self.assertEqual(res["intent"], "close_short")
            mock_service.submit_order.assert_called_once()


if __name__ == "__main__":
    unittest.main()
