"""Persist AlgoPaca UI settings to a local gitignored JSON file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bot.ai_models import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_XAI_MODEL,
)
from bot.day_presets import DEFAULT_PRESET_ID as DEFAULT_DAY_PRESET_ID

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / ".desk_settings.json"


def _path(path: Path | None = None) -> Path:
    return path or SETTINGS_PATH

# Never persist secrets in the JSON settings file.
_SECRET_KEYS = frozenset(
    {
        "openai_api_key",
        "gemini_api_key",
        "anthropic_api_key",
        "xai_api_key",
        "api_key",
        "secret_key",
        "save_keys_to_env",
        "save_to_env",
    }
)

_DEFAULTS: dict[str, Any] = {
    "symbol": "AAPL",
    "symbols": "AAPL",
    "fast_sma": 10,
    "slow_sma": 30,
    "sma_preset": "classic",
    "dip_preset": "deep",
    "dip_rsi_buy": 30.0,
    "dip_rsi_sell": 60.0,
    "dip_skip_bearish": True,
    "trade_qty": 1.0,
    "size_mode": "qty",
    "trade_notional": 100.0,
    "bar_timeframe": "15Min",
    "poll_seconds": 20,
    "strategy_mode": "sma",
    "pair_preset": "research_max",
    "pair_sma_period": 50,
    "pair_lookback": 7,
    "pair_impulse_pct": 5.0,
    "pair_weak_side": "LONG",
    "pair_long_symbol": "",
    "pair_short_symbol": "",
    "ls_ema_fast": 21,
    "ls_ema_slow": 55,
    "ls_adx_min": 20.0,
    "ls_atr_stop_mult": 1.5,
    "ls_risk_pct": 1.0,
    "ls_rr": 2.0,
    "ls_time_stop_bars": 15,
    "day_preset": DEFAULT_DAY_PRESET_ID,
    "day_sub_mode": "vwap_trend",
    "day_side": "long_only",
    "day_ema_fast": 9,
    "day_ema_slow": 21,
    "day_orb_minutes": 15,
    "day_open_buffer_mins": 15,
    "day_eod_flatten_mins": 15,
    "day_eod_flatten": True,
    "day_max_trades_per_day": 3,
    "day_profit_target_r": 1.2,
    "day_stop_atr_mult": 1.0,
    "day_use_ai_confirm": True,
    "day_ai_min_confidence": 0.70,
    "ai_provider": "openai",
    "ai_preset": "balanced",
    "ai_instructions": "",
    "ai_min_confidence": 0.55,
    "openai_model": DEFAULT_OPENAI_MODEL,
    "gemini_model": DEFAULT_GEMINI_MODEL,
    "anthropic_model": DEFAULT_ANTHROPIC_MODEL,
    "xai_model": DEFAULT_XAI_MODEL,
    "stop_loss_pct": 0.0,
    "risk_engine_enabled": True,
    "ai_risk_pct": 0.5,
    "ai_atr_stop_mult": 1.8,
    "ai_take_profit_r": 2.0,
    "ai_trail_after_r": 1.0,
    "ai_max_positions": 3,
    "ai_daily_loss_limit_pct": 3.0,
    "ai_min_hold_minutes": 15,
    "ai_cooldown_minutes": 60,
    "ai_max_spread_bps": 25.0,
    "stop_limit_offset_pct": 0.0,
    "lang": "en",
    "options_enabled": True,
    "options_style": "vertical",
    "options_dte_min": 21,
    "options_dte_max": 45,
    "options_otm_pct": 5.0,
    "options_max_contracts": 1,
    "options_max_premium_pct": 1.0,
    "require_approval": False,
    "notify_browser": True,
    "notify_email": False,
    "notification_email": "",
    "custom_engine_id": "",
}


def load_settings(path: Path | None = None) -> dict[str, Any]:
    data = dict(_DEFAULTS)
    target = _path(path)
    if not target.exists():
        return data
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    if not isinstance(raw, dict):
        return data
    for key, value in raw.items():
        if key in _SECRET_KEYS:
            continue
        if key in _DEFAULTS or key in {
            "symbol",
            "symbols",
            "fast_sma",
            "slow_sma",
            "sma_preset",
            "dip_preset",
            "dip_rsi_buy",
            "dip_rsi_sell",
            "dip_skip_bearish",
            "trade_qty",
            "size_mode",
            "trade_notional",
            "bar_timeframe",
            "poll_seconds",
            "strategy_mode",
            "pair_preset",
            "pair_sma_period",
            "pair_lookback",
            "pair_impulse_pct",
            "pair_weak_side",
            "pair_long_symbol",
            "pair_short_symbol",
            "ls_ema_fast",
            "ls_ema_slow",
            "ls_adx_min",
            "ls_atr_stop_mult",
            "ls_risk_pct",
            "ls_rr",
            "ls_time_stop_bars",
            "ai_provider",
            "ai_preset",
            "ai_instructions",
            "ai_min_confidence",
            "openai_model",
            "gemini_model",
            "anthropic_model",
            "xai_model",
            "stop_loss_pct",
        }:
            data[key] = value
    return data


def save_settings(data: dict[str, Any], path: Path | None = None) -> Path:
    target = _path(path)
    clean = {k: v for k, v in data.items() if k not in _SECRET_KEYS}
    # Keep a stable subset.
    payload = {k: clean.get(k, _DEFAULTS.get(k)) for k in _DEFAULTS}
    # Allow extra known fields already filtered.
    for key, value in clean.items():
        if key in payload:
            payload[key] = value
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
