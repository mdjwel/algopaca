"""Persistence store for user-defined Custom Trading Engines."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

STARTER_BLUEPRINTS: list[dict[str, Any]] = [
    {
        "id": "blueprint_ai_gold_silver",
        "name": "AI Real-Time Gold & Silver Macro Momentum",
        "description": "Calibrated macro strategy for GLD, SLV, GDX, UGL & inverse short ETFs (GLL, DUST): buys pullbacks inside confirmed gold uptrends, gated by a rates- and GSR-weighted macro score, with a wide ATR trail that lets trends run.",
        "base_engine": "ai",
        "is_blueprint": True,
        "instructions": (
            "Specialized Gold & Silver (GLD, SLV, GDX, UGL, GLL, DUST) macro playbook, calibrated on 2007-2025 GLD forward returns.\n"
            "LONG gates: trend_regime 'bullish_above_sma200' AND macro_composite_score >= +0.5. Enter on a PULLBACK (RSI 38-58 or price at/below SMA20) — gold's 5-20 day momentum is negatively correlated with next-month returns, so fresh breakouts are a measured losing entry. Step up to UGL/GDX only when the score is >= +1.5.\n"
            "SHORT & INVERSE gates: trend_regime 'bearish_below_sma200' AND macro_composite_score <= -0.5, ideally with rising yields. Size smaller than an equivalent long — shorting gold fights a positive long-run drift. Keep inverse ETFs (GLL, DUST) short-dated; their daily reset erodes multi-week holds.\n"
            "RISK & SAFETY: Hold into high-impact FOMC/CPI release windows (within 45 mins) to avoid whipsaws. Rates (0.40) and the Gold/Silver ratio (0.35) drive the macro score; miner leadership has no measured predictive power — treat it as colour only.\n"
            "PATIENCE: overtrading is what destroys returns in this asset. On an open, working position the default answer is HOLD; exit on a regime flip or a macro score below -0.5, not on a shallow pullback."
        ),
        "choices": {
            "strategy_mode": "ai",
            "ai_provider": "gemini",
            "ai_preset": "gold_silver_macro",
            "ai_min_confidence": 0.62,
            "symbol": "GLD",
            "symbols": "GLD, SLV, GDX, UGL, GLL, DUST",
            # Every factor behind this playbook was measured on daily bars, and
            # the 15-minute frame it used to run on multiplied the decision count
            # without adding signal — the one change the study argued for loudest.
            "bar_timeframe": "1Day",
            "size_mode": "notional",
            "trade_qty": 2.0,
            "trade_notional": 300.0,
            "poll_seconds": 300,
            "risk_engine_enabled": True,
            "ai_risk_pct": 0.6,
            "ai_atr_stop_mult": 2.2,
            "ai_take_profit_r": 3.0,
            "ai_trail_after_r": 1.2,
            "ai_max_positions": 2,
            "ai_daily_loss_limit_pct": 3.0,
            "ai_min_hold_minutes": 15,
            "ai_cooldown_minutes": 45,
            "ai_max_spread_bps": 25.0,
            "stop_limit_offset_pct": 0.08,
            "options_enabled": False,
            "require_approval": False,
            "notify_browser": True,
            "notify_email": False,
            "notification_email": "",
        },
    },
    {
        "id": "blueprint_ai_trend",
        "name": "AI Trend & Volatility Surfer",
        "description": "Multi-timeframe trend following with dynamic ATR trailing stops, ADX regime filter, and news sentiment checks.",
        "base_engine": "ai",
        "is_blueprint": True,
        "instructions": (
            "Trend following with momentum and risk controls.\n"
            "LONG gates: trend_bias bullish, ADX >= 22 (trending regime), price above SMA20, MACD histogram positive, higher_timeframe bullish or neutral.\n"
            "SHORT gates: trend_bias bearish, ADX >= 22, price below SMA20, MACD histogram negative, higher_timeframe bearish or neutral.\n"
            "SKIP in choppy markets (ADX < 20), wide spreads (>20 bps), or during High-impact USD event windows.\n"
            "EXITS: Hold through trend noise; trailing stop trails after 1R profit. Scale out at 2.5R target. Flatten immediately on structure break."
        ),
        "choices": {
            "strategy_mode": "ai",
            "ai_provider": "openai",
            "ai_preset": "custom",
            "ai_min_confidence": 0.60,
            "symbol": "AAPL",
            "symbols": "AAPL, MSFT, NVDA, GOOGL, AMZN",
            "bar_timeframe": "15Min",
            "size_mode": "qty",
            "trade_qty": 1.0,
            "trade_notional": 150.0,
            "poll_seconds": 20,
            "risk_engine_enabled": True,
            "ai_risk_pct": 0.5,
            "ai_atr_stop_mult": 1.8,
            "ai_take_profit_r": 2.5,
            "ai_trail_after_r": 1.0,
            "ai_max_positions": 3,
            "ai_daily_loss_limit_pct": 3.0,
            "ai_min_hold_minutes": 15,
            "ai_cooldown_minutes": 45,
            "ai_max_spread_bps": 20.0,
            "stop_limit_offset_pct": 0.1,
            "options_enabled": False,
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
        },
    },
    {
        "id": "blueprint_ai_dip_hunter",
        "name": "AI Deep Dip & Washout Hunter",
        "description": "Fades extreme oversold pullbacks into Bollinger lower bands with confirmation of stabilization.",
        "base_engine": "ai",
        "is_blueprint": True,
        "instructions": (
            "Oversold dip hunter playbook.\n"
            "LONG gates: RSI <= 30 or Bollinger pct_b <= 0.08, dist_sma50_atr <= -2.0, tape stabilizing on decreasing selling volume.\n"
            "Never buy falling knives without technical stabilization.\n"
            "Skip during fresh unpriced earnings misses or negative breaking news.\n"
            "Target is mean reversion back to SMA20 or mid-Bollinger band. Exit quickly when target is reached."
        ),
        "choices": {
            "strategy_mode": "ai",
            "ai_provider": "gemini",
            "ai_preset": "custom",
            "ai_min_confidence": 0.65,
            "symbol": "NVDA",
            "symbols": "NVDA, TSLA, AMD, META",
            "bar_timeframe": "15Min",
            "size_mode": "notional",
            "trade_qty": 1.0,
            "trade_notional": 200.0,
            "poll_seconds": 20,
            "risk_engine_enabled": True,
            "ai_risk_pct": 0.4,
            "ai_atr_stop_mult": 2.2,
            "ai_take_profit_r": 1.8,
            "ai_trail_after_r": 0.8,
            "ai_max_positions": 2,
            "ai_daily_loss_limit_pct": 2.5,
            "ai_min_hold_minutes": 20,
            "ai_cooldown_minutes": 60,
            "ai_max_spread_bps": 25.0,
            "stop_limit_offset_pct": 0.0,
            "options_enabled": False,
            "require_approval": False,
            "notify_browser": True,
            "notify_email": False,
            "notification_email": "",
        },
    },
    {
        "id": "blueprint_catalyst_drift",
        "name": "Earnings Catalyst & News Momentum",
        "description": "Trades post-earnings announcement drift (PEAD) and breaking macro catalysts in the direction of the surprise.",
        "base_engine": "ai",
        "is_blueprint": True,
        "instructions": (
            "Trade high-conviction catalysts and post-earnings drift.\n"
            "LONG: Earnings stance 'react' with EPS beat or strong positive headline; price holding above pre-market opening range.\n"
            "SHORT: Earnings stance 'react' with EPS miss or severe negative catalyst; price breaking breakdown levels.\n"
            "Blackout rule: Do not enter new positions inside an active earnings blackout window.\n"
            "Exits: Ride drift for multi-bar continuation until trend structure breaks."
        ),
        "choices": {
            "strategy_mode": "ai",
            "ai_provider": "anthropic",
            "ai_preset": "custom",
            "ai_min_confidence": 0.62,
            "symbol": "MSFT",
            "symbols": "MSFT, AAPL, AMZN, GOOGL, NFLX",
            "bar_timeframe": "1Hour",
            "size_mode": "qty",
            "trade_qty": 2.0,
            "trade_notional": 300.0,
            "poll_seconds": 30,
            "risk_engine_enabled": True,
            "ai_risk_pct": 0.6,
            "ai_atr_stop_mult": 2.0,
            "ai_take_profit_r": 2.8,
            "ai_trail_after_r": 1.2,
            "ai_max_positions": 3,
            "ai_daily_loss_limit_pct": 3.5,
            "ai_min_hold_minutes": 30,
            "ai_cooldown_minutes": 60,
            "ai_max_spread_bps": 30.0,
            "stop_limit_offset_pct": 0.1,
            "options_enabled": True,
            "options_style": "vertical",
            "options_dte_min": 21,
            "options_dte_max": 45,
            "options_otm_pct": 5.0,
            "options_max_contracts": 2,
            "options_max_premium_pct": 1.5,
            "require_approval": True,
            "notify_browser": True,
            "notify_email": False,
            "notification_email": "",
        },
    },
    {
        "id": "blueprint_tech_crossover",
        "name": "Custom Dynamic SMA & ATR Shield",
        "description": "Customizable moving average crossover paired with AlgoPaca dynamic ATR risk engine and stop limits.",
        "base_engine": "sma",
        "is_blueprint": True,
        "instructions": "Quantitative rule engine: Long when Fast SMA crosses above Slow SMA; exit or reverse when Fast crosses below Slow. Risk engine manages lot sizing and ATR stop.",
        "choices": {
            "strategy_mode": "sma",
            "sma_preset": "custom",
            "fast_sma": 12,
            "slow_sma": 36,
            "symbol": "SPY",
            "symbols": "SPY, QQQ, IWM",
            "bar_timeframe": "15Min",
            "size_mode": "qty",
            "trade_qty": 5.0,
            "trade_notional": 500.0,
            "poll_seconds": 20,
            "risk_engine_enabled": True,
            "ai_risk_pct": 0.5,
            "ai_atr_stop_mult": 1.5,
            "ai_take_profit_r": 2.0,
            "ai_trail_after_r": 1.0,
            "ai_max_positions": 2,
            "ai_daily_loss_limit_pct": 2.0,
            "ai_min_hold_minutes": 10,
            "ai_cooldown_minutes": 30,
            "ai_max_spread_bps": 15.0,
            "stop_limit_offset_pct": 0.05,
            "options_enabled": False,
            "require_approval": False,
            "notify_browser": True,
            "notify_email": False,
            "notification_email": "",
        },
    },
    {
        "id": "blueprint_regime_momentum",
        "name": "Custom Regime Momentum (Long/Short)",
        "description": "EMA trend regimes with ADX strength filter and MACD momentum triggers for two-way long/short execution.",
        "base_engine": "ls",
        "is_blueprint": True,
        "instructions": "Regime Dual Momentum engine: Long in bull EMA/ADX regimes, short in bear regimes on MACD confirmation. Strict reward:risk with ATR exit stops.",
        "choices": {
            "strategy_mode": "ls",
            "ls_ema_fast": 20,
            "ls_ema_slow": 50,
            "ls_adx_min": 22.0,
            "ls_atr_stop_mult": 1.6,
            "ls_risk_pct": 1.0,
            "ls_rr": 2.2,
            "ls_time_stop_bars": 14,
            "symbol": "QQQ",
            "symbols": "QQQ, SPY, DIA",
            "bar_timeframe": "1Day",
            "size_mode": "qty",
            "trade_qty": 2.0,
            "trade_notional": 300.0,
            "poll_seconds": 30,
            "require_approval": False,
            "notify_browser": True,
            "notify_email": False,
            "notification_email": "",
        },
    },
    {
        "id": "blueprint_quant_dip_hunter",
        "name": "Quantitative RSI Dip Hunter",
        "description": "Systematic RSI oversold dip-buying on high-liquidity stocks with bearish filter guard.",
        "base_engine": "dip",
        "is_blueprint": True,
        "instructions": "Quantitative rule engine: Buys when RSI drops below oversold threshold with bearish trend filter; takes profit when RSI rebounds.",
        "choices": {
            "strategy_mode": "dip",
            "dip_preset": "deep",
            "dip_rsi_buy": 28,
            "dip_rsi_sell": 65,
            "dip_skip_bearish": True,
            "symbol": "NVDA",
            "symbols": "NVDA, AAPL, MSFT, AMZN, GOOGL",
            "bar_timeframe": "15Min",
            "size_mode": "qty",
            "trade_qty": 2.0,
            "trade_notional": 250.0,
            "poll_seconds": 20,
            "risk_engine_enabled": True,
            "ai_risk_pct": 0.5,
            "ai_atr_stop_mult": 2.0,
            "ai_take_profit_r": 2.0,
            "ai_trail_after_r": 1.0,
            "ai_max_positions": 3,
            "ai_daily_loss_limit_pct": 3.0,
            "ai_min_hold_minutes": 15,
            "ai_cooldown_minutes": 45,
            "ai_max_spread_bps": 20.0,
            "stop_limit_offset_pct": 0.05,
            "options_enabled": False,
            "require_approval": False,
            "notify_browser": True,
            "notify_email": False,
            "notification_email": "",
        },
    },
    {
        "id": "blueprint_relative_strength_pair",
        "name": "Statistical Relative Strength Pair",
        "description": "Trades diverging performance between index leaders and sector proxies with statistical impulse gates.",
        "base_engine": "pair",
        "is_blueprint": True,
        "instructions": "Pair trading engine: Computes ratio spread between Long and Short legs against moving average impulse threshold.",
        "choices": {
            "strategy_mode": "pair",
            "pair_preset": "research_max",
            "pair_sma_period": 50,
            "pair_lookback": 7,
            "pair_impulse_pct": 5.0,
            "pair_weak_side": "LONG",
            "pair_long_symbol": "QQQ",
            "pair_short_symbol": "SPY",
            "symbol": "QQQ",
            "symbols": "QQQ,SPY",
            "bar_timeframe": "1Day",
            "size_mode": "qty",
            "trade_qty": 2.0,
            "trade_notional": 300.0,
            "poll_seconds": 30,
            "require_approval": False,
            "notify_browser": True,
            "notify_email": False,
            "notification_email": "",
        },
    },
    {
        "id": "blueprint_ai_day_sniper",
        "name": "AI Intraday VWAP & ORB Sniper",
        "description": "Combines institutional VWAP volume flows with real-time AI news sentiment and macro-catalyst confirmation to filter out false intraday breakouts.",
        "base_engine": "day",
        "is_blueprint": True,
        "instructions": "Intraday AI sniper: Evaluates 15m Opening Range Breakouts and VWAP trend alignment. Queries AI to confirm institutional tape momentum and veto fakeouts or trades before high-impact economic news.",
        "choices": {
            "strategy_mode": "day",
            "day_preset": "ai_vwap_momentum",
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
            "symbol": "QQQ",
            "symbols": "QQQ, SPY, NVDA, TSLA, AAPL",
            "bar_timeframe": "5Min",
            "size_mode": "qty",
            "trade_qty": 2.0,
            "trade_notional": 300.0,
            "poll_seconds": 20,
            "risk_engine_enabled": True,
            "ai_risk_pct": 0.5,
            "ai_atr_stop_mult": 1.5,
            "ai_take_profit_r": 2.0,
            "ai_trail_after_r": 1.0,
            "ai_max_positions": 3,
            "ai_daily_loss_limit_pct": 3.0,
            "ai_min_hold_minutes": 10,
            "ai_cooldown_minutes": 30,
            "ai_max_spread_bps": 20.0,
            "stop_limit_offset_pct": 0.05,
            "options_enabled": False,
            "require_approval": False,
            "notify_browser": True,
            "notify_email": False,
            "notification_email": "",
        },
    },
    {
        "id": "blueprint_vwap_momentum_rider",
        "name": "Intraday VWAP & ORB Momentum Surfer",
        "description": "Exploits intraday institutional volume flows with VWAP deviation bands, 15m Opening Range Breakouts, and automatic EOD flattening.",
        "base_engine": "day",
        "is_blueprint": True,
        "instructions": "Intraday momentum playbook: Long when price confirms above VWAP and breaks above the 15-minute Opening Range High (ORH). Scale out at 2R target; auto-square-off before market close.",
        "choices": {
            "strategy_mode": "day",
            "day_preset": "vwap_trend",
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
            "day_use_ai_confirm": False,
            "day_ai_min_confidence": 0.65,
            "symbol": "QQQ",
            "symbols": "QQQ, SPY, NVDA, TSLA, AAPL",
            "bar_timeframe": "5Min",
            "size_mode": "qty",
            "trade_qty": 2.0,
            "trade_notional": 300.0,
            "poll_seconds": 20,
            "risk_engine_enabled": True,
            "ai_risk_pct": 0.5,
            "ai_atr_stop_mult": 1.5,
            "ai_take_profit_r": 2.0,
            "ai_trail_after_r": 1.0,
            "ai_max_positions": 3,
            "ai_daily_loss_limit_pct": 3.0,
            "ai_min_hold_minutes": 10,
            "ai_cooldown_minutes": 30,
            "ai_max_spread_bps": 20.0,
            "stop_limit_offset_pct": 0.05,
            "options_enabled": False,
            "require_approval": False,
            "notify_browser": True,
            "notify_email": False,
            "notification_email": "",
        },
    },
]


def _user_store_path(user_id: str | None = None) -> Path:
    if user_id:
        clean_user = "".join(c for c in str(user_id) if c.isalnum() or c in "-_")
        if clean_user:
            return ROOT / f".custom_engines.{clean_user}.json"
    return ROOT / ".custom_engines.json"


def _sanitize_engine(data: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or "").strip()
    if not name:
        return None

    engine_id = str(data.get("id") or "").strip()
    if not engine_id:
        engine_id = f"ce_{uuid.uuid4().hex[:10]}"

    raw_choices = data.get("choices")
    choices: dict[str, Any] = dict(raw_choices) if isinstance(raw_choices, dict) else {}

    base_engine = str(
        data.get("base_engine")
        or data.get("strategy_mode")
        or choices.get("strategy_mode")
        or "ai"
    ).strip().lower()
    if base_engine not in {"ai", "sma", "dip", "pair", "ls", "day"}:
        base_engine = "ai"

    description = str(data.get("description") or "").strip()
    instructions = str(data.get("instructions") or "").strip()
    is_blueprint = bool(data.get("is_blueprint", False))

    now = time.time()
    created_at = float(data.get("created_at") or now)
    updated_at = float(data.get("updated_at") or now)

    # Guarantee core choice fields and bidirectional synchronization
    choices["strategy_mode"] = base_engine
    choices["custom_engine_id"] = engine_id

    if not instructions:
        if choices.get("ai_instructions"):
            instructions = str(choices["ai_instructions"]).strip()
        elif choices.get("instructions"):
            instructions = str(choices["instructions"]).strip()
    if instructions:
        choices["ai_instructions"] = instructions

    if not choices.get("symbol") and choices.get("symbols"):
        first_sym = str(choices["symbols"]).split(",")[0].strip().upper()
        if first_sym:
            choices["symbol"] = first_sym
    elif choices.get("symbol") and not choices.get("symbols"):
        choices["symbols"] = str(choices["symbol"]).strip().upper()

    return {
        "id": engine_id,
        "name": name,
        "description": description,
        "base_engine": base_engine,
        "instructions": instructions,
        "is_blueprint": is_blueprint,
        "created_at": created_at,
        "updated_at": updated_at,
        "choices": choices,
    }


def list_custom_engines(user_id: str | None = None, include_blueprints: bool = True) -> list[dict[str, Any]]:
    """Return all custom engines saved by the user, preceded by starter blueprints."""
    path = _user_store_path(user_id)
    user_engines: list[dict[str, Any]] = []

    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    clean = _sanitize_engine(item)
                    if clean:
                        user_engines.append(clean)
        except Exception as exc:
            logger.warning("Could not read custom engines from %s: %s", path, exc)

    # Sort user engines by updated_at descending
    user_engines.sort(key=lambda e: float(e.get("updated_at") or 0), reverse=True)

    if include_blueprints:
        blueprints = [_sanitize_engine(b) for b in STARTER_BLUEPRINTS if _sanitize_engine(b)]
        return blueprints + user_engines
    return user_engines


def get_custom_engine(engine_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    """Find custom engine by ID across blueprints and user storage."""
    clean_id = str(engine_id or "").strip()
    if not clean_id:
        return None

    # Check blueprints first
    for bp in STARTER_BLUEPRINTS:
        if bp.get("id") == clean_id:
            return _sanitize_engine(bp)

    # Check user engines
    for engine in list_custom_engines(user_id, include_blueprints=False):
        if engine.get("id") == clean_id:
            return engine
    return None


def save_custom_engine(engine_data: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
    """Create or update a custom engine in user's isolated storage."""
    clean = _sanitize_engine(engine_data)
    if not clean:
        raise ValueError("Custom engine must have a valid name and parameters.")

    existing = list_custom_engines(user_id, include_blueprints=False)

    # If it's a blueprint ID, check if user already has an engine with this name to update/replace
    if clean["id"].startswith("blueprint_") or clean.get("is_blueprint"):
        match = next(
            (e for e in existing if e.get("name", "").strip().lower() == clean["name"].strip().lower()),
            None
        )
        if match:
            clean["id"] = match["id"]
        else:
            clean["id"] = f"ce_{uuid.uuid4().hex[:10]}"
        clean["is_blueprint"] = False

    clean["updated_at"] = time.time()
    updated = False
    new_list: list[dict[str, Any]] = []

    for item in existing:
        if item.get("id") == clean["id"]:
            new_list.append(clean)
            updated = True
        else:
            new_list.append(item)

    if not updated:
        clean["created_at"] = time.time()
        new_list.insert(0, clean)

    path = _user_store_path(user_id)
    try:
        path.write_text(json.dumps(new_list, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to save custom engine to %s: %s", path, exc)
        raise OSError(f"Could not persist custom engine: {exc}") from exc

    return clean


def delete_custom_engine(engine_id: str, user_id: str | None = None) -> bool:
    """Delete a user's custom engine by ID. Blueprints cannot be deleted."""
    clean_id = str(engine_id or "").strip()
    if not clean_id or clean_id.startswith("blueprint_"):
        return False

    existing = list_custom_engines(user_id, include_blueprints=False)
    filtered = [item for item in existing if item.get("id") != clean_id]

    if len(filtered) == len(existing):
        return False

    path = _user_store_path(user_id)
    try:
        path.write_text(json.dumps(filtered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return True
    except Exception as exc:
        logger.error("Failed to delete custom engine %s from %s: %s", clean_id, path, exc)
        return False


def duplicate_custom_engine(engine_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    """Clone an existing engine or blueprint with a copy suffix."""
    source = get_custom_engine(engine_id, user_id)
    if not source:
        return None

    clone_data = dict(source)
    clone_data["id"] = f"ce_{uuid.uuid4().hex[:10]}"
    clone_data["name"] = f"{source['name']} (Copy)"
    clone_data["is_blueprint"] = False
    clone_data["created_at"] = time.time()
    clone_data["updated_at"] = time.time()

    return save_custom_engine(clone_data, user_id)
