"""Named Day Trading strategy presets for AlgoPaca."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DayTradingPreset:
    id: str
    label: str
    summary: str
    sub_mode: str  # vwap_trend | orb | momentum_scalp | vwap_fade
    ema_fast: int = 9
    ema_slow: int = 21
    orb_minutes: int = 15
    open_buffer_mins: int = 15
    eod_flatten_mins: int = 15
    eod_flatten: bool = True
    max_trades_per_day: int = 5
    profit_target_r: float = 2.0
    stop_atr_mult: float = 1.5
    side: str = "long_only"  # long_only | short_only | long_short
    use_ai_confirm: bool = False
    ai_min_confidence: float = 0.65


_PRESETS: tuple[DayTradingPreset, ...] = (
    DayTradingPreset(
        id="ai_vwap_momentum",
        label="AI Institutional VWAP & Momentum",
        summary="Intraday VWAP trend & 9/21 EMA momentum filtered by real-time AI news sentiment & macro catalyst confirmation.",
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
        use_ai_confirm=True,
        ai_min_confidence=0.70,
    ),
    DayTradingPreset(
        id="ai_orb_breakout",
        label="AI Opening Range Sniper (15m)",
        summary="15-minute Opening Range Breakout, volume-confirmed, with AI false-breakout veto and volatility expansion detection.",
        sub_mode="orb",
        ema_fast=9,
        ema_slow=21,
        orb_minutes=15,
        open_buffer_mins=15,
        eod_flatten_mins=15,
        eod_flatten=True,
        max_trades_per_day=4,
        profit_target_r=2.5,
        stop_atr_mult=1.8,
        side="long_only",
        use_ai_confirm=True,
        ai_min_confidence=0.70,
    ),
    DayTradingPreset(
        id="ai_adaptive_scalp",
        label="AI Adaptive Intraday Scalper",
        summary="Fast 9/21 EMA momentum scalper with ADX regime filter and AI veto on counter-trend and chop setups.",
        sub_mode="momentum_scalp",
        ema_fast=9,
        ema_slow=21,
        orb_minutes=15,
        open_buffer_mins=10,
        eod_flatten_mins=15,
        eod_flatten=True,
        max_trades_per_day=8,
        profit_target_r=1.5,
        stop_atr_mult=1.2,
        side="long_only",
        use_ai_confirm=True,
        ai_min_confidence=0.65,
    ),
    DayTradingPreset(
        id="vwap_trend",
        label="VWAP Trend Rider",
        summary="Trend following above intraday VWAP with 9/21 EMA momentum and 2R profit target.",
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
        use_ai_confirm=False,
        ai_min_confidence=0.65,
    ),
    DayTradingPreset(
        id="orb_breakout",
        label="Opening Range Breakout (15m)",
        summary="Trades volume-confirmed 15-minute opening range high breakouts with ATR trailing stop.",
        sub_mode="orb",
        ema_fast=9,
        ema_slow=21,
        orb_minutes=15,
        open_buffer_mins=15,
        eod_flatten_mins=15,
        eod_flatten=True,
        max_trades_per_day=3,
        profit_target_r=2.5,
        stop_atr_mult=1.8,
        side="long_only",
        use_ai_confirm=False,
        ai_min_confidence=0.65,
    ),
    DayTradingPreset(
        id="momentum_scalp",
        label="Intraday Momentum Scalp",
        summary="Fast 9/21 EMA crossovers confirmed by RSI > 55 and an ADX trend filter for quick scalps.",
        sub_mode="momentum_scalp",
        ema_fast=9,
        ema_slow=21,
        orb_minutes=15,
        open_buffer_mins=10,
        eod_flatten_mins=15,
        eod_flatten=True,
        max_trades_per_day=8,
        profit_target_r=1.5,
        stop_atr_mult=1.2,
        side="long_only",
        use_ai_confirm=False,
        ai_min_confidence=0.65,
    ),
    DayTradingPreset(
        id="vwap_fade",
        label="VWAP Mean Reversion (Fade)",
        summary="Buys confirmed oversold bounces at the lower VWAP band in range-bound sessions only, targeting the VWAP midline.",
        sub_mode="vwap_fade",
        ema_fast=9,
        ema_slow=21,
        orb_minutes=15,
        open_buffer_mins=20,
        eod_flatten_mins=15,
        eod_flatten=True,
        max_trades_per_day=4,
        profit_target_r=1.8,
        stop_atr_mult=1.5,
        side="long_only",
        use_ai_confirm=False,
        ai_min_confidence=0.65,
    ),
    DayTradingPreset(
        id="ai_orb_breakout_ls",
        label="AI Opening Range Sniper (Long & Short)",
        summary="15-minute Opening Range Breakout & Breakdown, volume-confirmed, with AI false-breakout veto.",
        sub_mode="orb",
        ema_fast=9,
        ema_slow=21,
        orb_minutes=15,
        open_buffer_mins=15,
        eod_flatten_mins=15,
        eod_flatten=True,
        max_trades_per_day=6,
        profit_target_r=2.5,
        stop_atr_mult=1.8,
        side="long_short",
        use_ai_confirm=True,
        ai_min_confidence=0.70,
    ),
    DayTradingPreset(
        id="ai_vwap_momentum_ls",
        label="AI Two-Way VWAP & Momentum (Long & Short)",
        summary="Intraday VWAP trend & 9/21 EMA momentum (long & short) filtered by AI sentiment confirmation.",
        sub_mode="vwap_trend",
        ema_fast=9,
        ema_slow=21,
        orb_minutes=15,
        open_buffer_mins=15,
        eod_flatten_mins=15,
        eod_flatten=True,
        max_trades_per_day=6,
        profit_target_r=2.0,
        stop_atr_mult=1.5,
        side="long_short",
        use_ai_confirm=True,
        ai_min_confidence=0.70,
    ),
    DayTradingPreset(
        id="ai_orb_breakdown_short",
        label="AI Opening Range Breakdown (Short Only)",
        summary="Trades volume-confirmed 15-minute opening range low breakdowns with AI false-breakdown veto.",
        sub_mode="orb",
        ema_fast=9,
        ema_slow=21,
        orb_minutes=15,
        open_buffer_mins=15,
        eod_flatten_mins=15,
        eod_flatten=True,
        max_trades_per_day=4,
        profit_target_r=2.5,
        stop_atr_mult=1.8,
        side="short_only",
        use_ai_confirm=True,
        ai_min_confidence=0.70,
    ),
    DayTradingPreset(
        id="vwap_trend_short",
        label="VWAP Downtrend Rider (Short Only)",
        summary="Trend-following short entries below intraday VWAP with 9/21 EMA momentum and 2R profit target.",
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
        side="short_only",
        use_ai_confirm=False,
        ai_min_confidence=0.65,
    ),
    DayTradingPreset(
        id="custom",
        label="Custom",
        summary="Tune your own intraday EMA, VWAP, ORB, AI confirmation, and EOD square-off rules.",
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
        use_ai_confirm=False,
        ai_min_confidence=0.65,
    ),
)

_BY_ID = {p.id: p for p in _PRESETS}
DEFAULT_PRESET_ID = "ai_vwap_momentum"


def list_presets() -> list[dict[str, Any]]:
    return [asdict(p) for p in _PRESETS]


def get_preset(preset_id: str | None) -> DayTradingPreset:
    key = (preset_id or DEFAULT_PRESET_ID).strip().lower()
    return _BY_ID.get(key, _BY_ID[DEFAULT_PRESET_ID])


def resolve_preset_id(preset_id: str | None) -> str:
    key = (preset_id or DEFAULT_PRESET_ID).strip().lower()
    return key if key in _BY_ID else DEFAULT_PRESET_ID


def match_preset_id(
    sub_mode: str,
    ema_fast: int,
    ema_slow: int,
    orb_minutes: int,
    open_buffer_mins: int,
    eod_flatten_mins: int,
    eod_flatten: bool,
    max_trades_per_day: int,
    profit_target_r: float,
    stop_atr_mult: float,
    side: str = "long_only",
    use_ai_confirm: bool = False,
    ai_min_confidence: float = 0.65,
) -> str:
    for preset in _PRESETS:
        if preset.id == "custom":
            continue
        if (
            preset.sub_mode == sub_mode
            and preset.ema_fast == ema_fast
            and preset.ema_slow == ema_slow
            and preset.orb_minutes == orb_minutes
            and preset.open_buffer_mins == open_buffer_mins
            and preset.eod_flatten_mins == eod_flatten_mins
            and preset.eod_flatten == eod_flatten
            and preset.max_trades_per_day == max_trades_per_day
            and abs(preset.profit_target_r - profit_target_r) < 0.05
            and abs(preset.stop_atr_mult - stop_atr_mult) < 0.05
            and preset.side == side
            and preset.use_ai_confirm == use_ai_confirm
            and abs(preset.ai_min_confidence - ai_min_confidence) < 0.05
        ):
            return preset.id
    return "custom"
