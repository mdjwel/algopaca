"""Historical walk-forward backtest engine for the AI Trader desk.

Replays the AI Trader multi-factor reasoning and risk rules over historical bars:
- Multi-factor indicator synthesis: SMA 10/20/50/200, EMA 9/21, RSI 14, MACD,
  Bollinger Bands, ATR 14, ADX 14, Volume Ratio, and Higher Timeframe Trend.
- Preset playbook rules matching `bot/ai_presets.py`: Balanced, Conservative,
  Momentum, Mean Reversion, Trend + ATR Trail, Gold & Silver Macro, and Custom.
- Mechanical risk guardrails from `bot/ai_risk.py`: dynamic ATR stop loss,
  R-multiple take profit, trailing stop ratcheting, and confidence-scaled sizing.
- Deterministic, high-speed, zero-cost historical replay with zero look-ahead bias.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any

import numpy as np
import pandas as pd

from bot.ai_presets import (
    DEFAULT_PRESET_ID,
    AiPreset,
    get_preset,
    resolve_preset_id,
    risk_profile_for,
)
from bot.analysis import _adx, _atr, _ema, _macd_series, _rsi
from bot.backtest import _downsample_curve, _ts_str
from bot.metals_intel import compute_historical_macro_series, is_precious_metal
from bot.strategy import Signal

DEFAULT_SLIPPAGE_BPS = 1.0

# Assets that cannot be sold short (3x/2x leveraged bull ETFs unshortable at Alpaca,
# and inverse ETFs which are bought long to express a bearish view).
UNSHORTABLE_SYMBOLS: set[str] = {
    # Leveraged Bull ETFs / ETNs (borrow unavailable on Alpaca; long only)
    "GDXU", "UGL", "AGQ", "NUGT", "JNUG", "TQQQ", "UPRO", "SOXL", "FAS", "LABU",
    # Inverse ETFs / ETNs (bought long to express short view; never shorted)
    "GDXD", "DUST", "GLL", "ZSL", "JDST", "SQQQ", "SPXU", "SOXS", "FAZ", "LABD",
}

# Opposing leveraged bull / inverse pairs: holding both simultaneously creates mutual drag and counter-decay.
OPPOSING_METALS_PAIRS: dict[str, str] = {
    "GDXU": "GDXD",
    "GDXD": "GDXU",
    "NUGT": "DUST",
    "DUST": "NUGT",
    "UGL": "GLL",
    "GLL": "UGL",
    "AGQ": "ZSL",
    "ZSL": "AGQ",
}


@dataclass
class AiBacktestParams:
    """Parameters for AI Trader backtest simulation."""

    preset: str = DEFAULT_PRESET_ID
    min_confidence: float = 0.55
    atr_stop_mult: float = 1.8
    take_profit_r: float = 2.0
    trail_after_r: float = 1.0
    risk_pct: float = 0.5
    max_positions: int = 3
    initial_cash: float = 10_000.0
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    qty: float | None = None
    allow_short: bool = True
    ai_provider: str | None = None
    ai_model: str | None = None
    reversal_buy_on_stop: bool = True
    metals_dollar_index_only: bool = False

    def __post_init__(self) -> None:
        if self.preset == "gold_silver_macro":
            if self.take_profit_r == 2.0:
                self.take_profit_r = 4.0
            if self.trail_after_r == 1.0:
                self.trail_after_r = 2.0
            if self.min_confidence == 0.55:
                self.min_confidence = 0.70
            if self.risk_pct in (0.5, 0.6, 1.5):
                self.risk_pct = 1.8
            if self.atr_stop_mult == 1.8:
                self.atr_stop_mult = 1.6


@dataclass
class AiBacktestTrade:
    symbol: str
    side: str  # "long" or "short"
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    r_multiple: float
    exit_reason: str
    entry_reason: str = ""
    confidence: float = 0.0
    thesis: str = ""


@dataclass
class _OpenPosition:
    side: str  # "long" or "short"
    entry_price: float
    entry_time: str
    entry_bar: int
    qty: float
    stop: float
    target: float
    stop_distance: float
    peak_price: float
    trough_price: float
    entry_reason: str = ""
    confidence: float = 0.0
    thesis: str = ""
    trail_armed: bool = False
    scaled_out: bool = False
    event_protected: bool = False


def _max_drawdown_pct(equity: list[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak * 100.0)
    return round(worst, 2)


def _sharpe_sortino(equity: list[float], period_returns_per_year: float = 252.0) -> tuple[float | None, float | None]:
    if len(equity) < 5:
        return None, None
    s = pd.Series(equity)
    rets = s.pct_change().dropna()
    if len(rets) < 4 or rets.std() == 0:
        return None, None
    rf = 0.0
    mean = float(rets.mean())
    std = float(rets.std())
    downside = rets[rets < 0]
    downside_std = float(downside.std()) if len(downside) > 1 and downside.std() > 0 else std
    sharpe = round(((mean - rf) / std) * math.sqrt(period_returns_per_year), 2)
    sortino = round(((mean - rf) / downside_std) * math.sqrt(period_returns_per_year), 2)
    return sharpe, sortino


def compute_ai_indicator_frame(
    bars: pd.DataFrame,
    symbol: str = "",
    macro_bars: dict[str, pd.DataFrame] | None = None,
    track_dollar_only: bool = False,
) -> pd.DataFrame:
    """Vectorized indicator pre-computation for AI rule evaluation."""
    df = bars.sort_index().copy()
    closes = df["close"].astype(float)
    highs = df["high"].astype(float) if "high" in df.columns else closes
    lows = df["low"].astype(float) if "low" in df.columns else closes
    opens = df["open"].astype(float) if "open" in df.columns else closes
    volumes = df["volume"].astype(float) if "volume" in df.columns else pd.Series(1.0, index=df.index)

    # Moving averages
    sma10 = closes.rolling(10).mean()
    sma20 = closes.rolling(20).mean()
    sma50 = closes.rolling(50).mean()
    sma200 = closes.rolling(200).mean() if len(closes) >= 200 else closes.rolling(max(10, len(closes) // 2)).mean()
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)

    # Oscillators & Momentum
    # Wilder RSI 14
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi14 = 100 - (100 / (1 + rs))
    rsi14 = rsi14.fillna(50.0)

    # MACD 12, 26, 9
    macd_frame = _macd_series(closes, fast=12, slow=26, signal=9)

    # ATR 14
    atr14 = _atr(highs, lows, closes, 14)
    atr_pct = (atr14 / closes.replace(0, np.nan)) * 100.0

    # ADX 14
    adx14 = _adx(highs, lows, closes, 14)

    # Bollinger Bands 20, 2
    bb_mid = sma20
    bb_std = closes.rolling(20).std()
    bb_upper = bb_mid + 2.0 * bb_std
    bb_lower = bb_mid - 2.0 * bb_std
    bb_width = bb_upper - bb_lower
    bb_pct_b = (closes - bb_lower) / bb_width.replace(0, np.nan)

    # Volume Ratio
    vol_sma20 = volumes.rolling(20).mean()
    vol_ratio = volumes / vol_sma20.replace(0, np.nan)

    # Distance to SMA50 in ATR units
    dist_sma50_atr = (closes - sma50) / atr14.replace(0, np.nan)

    # Trend structure bias
    trend_bullish = (closes > sma20) & (sma10 >= sma20)
    trend_bearish = (closes < sma20) & (sma10 <= sma20)

    res = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "sma10": sma10,
            "sma20": sma20,
            "sma50": sma50,
            "sma200": sma200,
            "ema9": ema9,
            "ema21": ema21,
            "rsi14": rsi14,
            "macd": macd_frame["macd"],
            "macd_signal": macd_frame["signal"],
            "macd_hist": macd_frame["histogram"],
            "atr14": atr14,
            "atr_pct": atr_pct,
            "adx14": adx14,
            "bb_upper": bb_upper,
            "bb_mid": bb_mid,
            "bb_lower": bb_lower,
            "bb_pct_b": bb_pct_b,
            "vol_ratio": vol_ratio.fillna(1.0),
            "dist_sma50_atr": dist_sma50_atr.fillna(0.0),
            "trend_bullish": trend_bullish,
            "trend_bearish": trend_bearish,
        },
        index=df.index,
    )

    sym_upper = (symbol or "").upper().strip()
    if is_precious_metal(sym_upper) or macro_bars:
        try:
            macro_df = compute_historical_macro_series(bars, symbol=sym_upper, macro_bars=macro_bars, track_dollar_only=track_dollar_only)
            for col in macro_df.columns:
                res[col] = macro_df[col]
        except Exception:
            pass

    return res


def evaluate_ai_signal(
    row: pd.Series,
    prev_row: pd.Series | None,
    params: AiBacktestParams,
    *,
    allow_short: bool = True,
    symbol: str | None = None,
    gsr_z: float | None = None,
    macro_score: float | None = None,
    event_imminent_45m: bool = False,
    spread_bps: float = 0.0,
) -> tuple[Signal, float, str]:
    """Evaluate AI playbook rules at a single historical bar.

    Returns:
        (Signal, confidence [0.0..1.0], thesis)
    """
    if event_imminent_45m and not getattr(params, "metals_dollar_index_only", False):
        return Signal.HOLD, 0.20, "Imminent high-impact macro event (within 45 minutes) — hold"
    if spread_bps > 25.0:
        return Signal.HOLD, 0.20, f"Excessive spread ({spread_bps:.1f} bps > 25 bps limit) — skip"

    preset_id = (params.preset or DEFAULT_PRESET_ID).strip().lower()
    sym = (symbol or "").upper().strip()
    if sym in UNSHORTABLE_SYMBOLS:
        allow_short = False

    if preset_id == "gold_silver_macro" and sym in {"GDX", "GDXJ", "DUST", "UGL"}:
        return Signal.HOLD, 0.0, f"{sym} is strictly excluded from the AI Gold & Silver Macro playbook"

    close = float(row["close"])
    sma10 = float(row["sma10"]) if "sma10" in row and not np.isnan(row["sma10"]) else close
    sma20 = float(row["sma20"]) if "sma20" in row and not np.isnan(row["sma20"]) else close
    sma50 = float(row["sma50"]) if "sma50" in row and not np.isnan(row["sma50"]) else close
    sma200 = float(row["sma200"]) if "sma200" in row and not np.isnan(row["sma200"]) else close
    rsi = float(row["rsi14"]) if "rsi14" in row and not np.isnan(row["rsi14"]) else 50.0
    adx = float(row["adx14"]) if "adx14" in row and not np.isnan(row["adx14"]) else 20.0
    atr_pct = float(row["atr_pct"]) if "atr_pct" in row and not np.isnan(row["atr_pct"]) else 2.0
    dist_sma50 = float(row["dist_sma50_atr"]) if "dist_sma50_atr" in row and not np.isnan(row["dist_sma50_atr"]) else 0.0
    macd_hist = float(row["macd_hist"]) if "macd_hist" in row and not np.isnan(row["macd_hist"]) else 0.0
    prev_hist = float(prev_row["macd_hist"]) if prev_row is not None and "macd_hist" in prev_row and not np.isnan(prev_row["macd_hist"]) else macd_hist
    vol_ratio = float(row["vol_ratio"]) if "vol_ratio" in row and not np.isnan(row["vol_ratio"]) else 1.0
    pct_b = float(row["bb_pct_b"]) if "bb_pct_b" in row and not np.isnan(row["bb_pct_b"]) else 0.5

    # 1. Hard global safety gates
    if atr_pct > 6.5:
        return Signal.HOLD, 0.20, f"Volatile tape (ATR {atr_pct:.1f}% > 6.5%)"

    # Higher timeframe trend proxy: price vs SMA50 and SMA200
    htf_bullish = close >= sma50 and (close >= sma200 or np.isnan(row["sma200"]))
    htf_bearish = close <= sma50 and (close <= sma200 or np.isnan(row["sma200"]))

    # Playbook routing
    if preset_id == "conservative":
        # Strict alignment: trend, HTF, ADX >= 25, RSI bounds, low extension
        if adx < 25.0:
            return Signal.HOLD, 0.30, f"ADX {adx:.1f} < 25 (conservative requires trending regime)"
        if abs(dist_sma50) > 2.5:
            return Signal.HOLD, 0.35, f"Extended from SMA50 ({dist_sma50:.1f} ATR)"
        if atr_pct > 4.0:
            return Signal.HOLD, 0.30, f"ATR {atr_pct:.1f}% > 4.0% limit"

        if close > sma20 and htf_bullish and 40.0 <= rsi <= 68.0 and macd_hist > 0:
            conf = 0.72 + (0.08 if vol_ratio >= 1.1 else 0.0) + (0.05 if adx >= 30 else 0.0)
            return Signal.BUY, min(conf, 0.92), f"Conservative Long: Full trend & HTF alignment (ADX={adx:.1f}, RSI={rsi:.1f})"
        if allow_short and close < sma20 and htf_bearish and 32.0 <= rsi <= 58.0 and macd_hist < 0:
            conf = 0.70 + (0.08 if vol_ratio >= 1.1 else 0.0) + (0.05 if adx >= 30 else 0.0)
            return Signal.SELL, min(conf, 0.90), f"Conservative Short: Full bear & HTF alignment (ADX={adx:.1f}, RSI={rsi:.1f})"

    elif preset_id == "momentum":
        # Follow strength: ADX >= 22, above SMA10 & SMA20, expanding MACD hist
        if adx < 20.0:
            return Signal.HOLD, 0.25, f"Chop regime (ADX {adx:.1f} < 20)"
        if close > sma10 and close > sma20 and macd_hist > 0 and macd_hist >= prev_hist and dist_sma50 <= 3.5:
            conf = 0.65 + (0.10 if adx >= 25 else 0.0) + (0.08 if vol_ratio >= 1.0 else 0.0)
            return Signal.BUY, min(conf, 0.95), f"Momentum Long: Expanding MACD & trend breakout (ADX={adx:.1f}, vol={vol_ratio:.1f}x)"
        if allow_short and close < sma10 and close < sma20 and macd_hist < 0 and macd_hist <= prev_hist and dist_sma50 >= -3.5:
            conf = 0.64 + (0.10 if adx >= 25 else 0.0) + (0.08 if vol_ratio >= 1.0 else 0.0)
            return Signal.SELL, min(conf, 0.93), f"Momentum Short: Breakdown below SMA stack (ADX={adx:.1f})"

    elif preset_id == "mean_reversion":
        # Fade stretched moves, wants range / chop (ADX < 30)
        if adx >= 32.0:
            return Signal.HOLD, 0.30, f"Strong trend (ADX {adx:.1f} >= 32) — mean reversion stands aside"
        if (rsi <= 32.0 or pct_b <= 0.06) and dist_sma50 <= -1.8:
            conf = 0.68 + (0.08 if rsi <= 25 else 0.0)
            return Signal.BUY, min(conf, 0.90), f"Mean Reversion Long: Stretched wash (RSI={rsi:.1f}, dist={dist_sma50:.1f} ATR)"
        if allow_short and (rsi >= 68.0 or pct_b >= 0.94) and dist_sma50 >= 1.8:
            conf = 0.66 + (0.08 if rsi >= 75 else 0.0)
            return Signal.SELL, min(conf, 0.88), f"Mean Reversion Short: Stretched blow-off (RSI={rsi:.1f}, dist={dist_sma50:.1f} ATR)"

    elif preset_id == "trend_atr":
        # Pure trend following with rising ADX
        if adx >= 22.0 and close > sma20 and macd_hist > 0:
            conf = 0.65 + (0.10 if htf_bullish else 0.0)
            return Signal.BUY, min(conf, 0.92), f"Trend+ATR Long: Clean trend structure (ADX={adx:.1f}, price > SMA20)"
        if allow_short and adx >= 22.0 and close < sma20 and macd_hist < 0:
            conf = 0.63 + (0.10 if htf_bearish else 0.0)
            return Signal.SELL, min(conf, 0.90), f"Trend+ATR Short: Bearish structure (ADX={adx:.1f}, price < SMA20)"

    elif preset_id == "gold_silver_macro":
        # Strictly exclude GDX, GDXJ, DUST, and UGL from the AI Gold & Silver Macro playbook
        if sym in {"GDX", "GDXJ", "DUST", "UGL"}:
            return Signal.HOLD, 0.0, f"{sym} is strictly excluded from the AI Gold & Silver Macro playbook"

        is_inverse = sym in {"GLL", "GDXD", "ZSL", "JDST"}
        is_leveraged_bull = sym in {"GDXU", "UGL", "AGQ", "NUGT", "JNUG"}
        is_silver = sym in {"SLV", "AGQ", "SIL", "SILJ", "PSLV"}
        is_miner = False

        # Extract macro metrics from row if available
        if macro_score is None and "macro_composite_score" in row and not pd.isna(row["macro_composite_score"]):
            macro_score = float(row["macro_composite_score"])
        if gsr_z is None and "gsr_z" in row and not pd.isna(row["gsr_z"]):
            gsr_z = float(row["gsr_z"])

        has_sma200 = not np.isnan(row["sma200"])
        trend_regime = str(row.get("trend_regime") or ("bullish_above_sma200" if (close >= sma200 if has_sma200 else close >= sma50) else "bearish_below_sma200"))
        is_bull_regime = (trend_regime == "bullish_above_sma200") and (close >= sma200 if has_sma200 else close >= sma50)
        is_bear_regime = (trend_regime == "bearish_below_sma200") and (close < sma200 if has_sma200 else close < sma50)

        yield_trend = str(row.get("yield_trend") or "neutral")
        dollar_trend = str(row.get("dollar_trend") or "neutral")
        dollar_mixed = bool(row.get("dollar_mixed", False) or dollar_trend == "neutral")
        gold_dollar_divergence = bool(row.get("gold_dollar_divergence", False))
        three_conf_state = str(row.get("three_confirmation_state") or "")
        conf_count = int(row.get("confirmations_count", 0)) if "confirmations_count" in row and not pd.isna(row["confirmations_count"]) else 0
        oil_trend = str(row.get("oil_trend") or "neutral")

        if getattr(params, "metals_dollar_index_only", False):
            dollar_score_val = float(row["dollar_score"]) if "dollar_score" in row and not pd.isna(row["dollar_score"]) else (macro_score if macro_score is not None else 0.0)
            d_falling = (dollar_trend in {"falling", "falling_dollar", "bearish"}) or dollar_score_val > 0.15
            d_rising = (dollar_trend in {"rising", "rising_dollar", "bullish"}) or dollar_score_val < -0.15

            if is_inverse:
                if d_rising:
                    return Signal.BUY, 0.85, f"Gold/Silver Macro (Dollar Index Only): Dollar rising ({dollar_trend}) -> BUY inverse ETF {sym}"
                elif d_falling:
                    return Signal.SELL if allow_short else Signal.HOLD, 0.85, f"Gold/Silver Macro (Dollar Index Only): Dollar falling ({dollar_trend}) -> EXIT inverse ETF {sym}"
                else:
                    return Signal.HOLD, 0.40, f"Gold/Silver Macro (Dollar Index Only): Dollar neutral -> HOLD inverse ETF {sym}"

            # Long precious metals (GLD, SLV, IAU, AGQ, etc.)
            if d_falling:
                return Signal.BUY, 0.85, f"Gold/Silver Macro (Dollar Index Only): Dollar falling ({dollar_trend}) -> BUY precious metal {sym}"
            elif d_rising:
                if allow_short:
                    return Signal.SELL, 0.85, f"Gold/Silver Macro (Dollar Index Only): Dollar rising ({dollar_trend}) -> SHORT precious metal {sym}"
                else:
                    return Signal.SELL, 0.85, f"Gold/Silver Macro (Dollar Index Only): Dollar rising ({dollar_trend}) -> EXIT precious metal {sym}"
            else:
                return Signal.HOLD, 0.40, f"Gold/Silver Macro (Dollar Index Only): Dollar neutral -> HOLD {sym}"

        # 1. Inverse ETFs (e.g. GDXD, GLL, ZSL): express bear view by BUYING long
        if is_inverse:
            if gold_dollar_divergence:
                return Signal.HOLD, 0.30, f"Gold/Silver Macro: Inverse ETF {sym} entry blocked by Bullish Decoupling Divergence (Case C)"
            dt = getattr(row, "name", None)
            if dt is not None and hasattr(dt, "hour"):
                try:
                    h = dt.hour
                    if getattr(dt, "tzinfo", None) is not None:
                        dt_utc = dt.tz_convert(timezone.utc) if hasattr(dt, "tz_convert") else dt.astimezone(timezone.utc)
                        h = dt_utc.hour
                    if h >= 19:
                        return Signal.HOLD, 0.35, f"Skip late-session entry on inverse {sym} (hour={h} UTC) to avoid overnight gap"
                except Exception:
                    if getattr(dt, "hour", 0) >= 19:
                        return Signal.HOLD, 0.35, f"Skip late-session entry on inverse {sym} to avoid overnight gap"
            inv_bull = close > sma20 and (close > sma50 or (close > sma200 if has_sma200 else True))
            if inv_bull and 36.0 <= rsi <= 68.0 and dist_sma50 <= 3.2:
                conf = 0.74 + (0.05 if macd_hist > 0 else 0.0) + (0.04 if adx >= 20.0 else 0.0)
                return Signal.BUY, min(conf, 0.92), f"Gold/Silver Macro (Inverse ETF): Bullish trend in {sym} (RSI={rsi:.1f})"
            return Signal.HOLD, 0.35, f"Inverse ETF {sym} awaiting confirmed inverse trend"

        # Reversal & bounce confirmation: do not buy into an uninterrupted falling knife
        bar_open = float(row["open"]) if not np.isnan(row["open"]) else close
        bar_high = float(row["high"]) if not np.isnan(row["high"]) else close
        bar_low = float(row["low"]) if not np.isnan(row["low"]) else close
        prev_close = float(prev_row["close"]) if prev_row is not None and not np.isnan(prev_row["close"]) else bar_open
        prev_rsi = float(prev_row["rsi14"]) if prev_row is not None and not np.isnan(prev_row["rsi14"]) else rsi

        candle_span = max(0.001, bar_high - bar_low)
        lower_tail = max(0.0, min(close, bar_open) - bar_low)
        has_lower_tail = (lower_tail / candle_span) >= 0.28
        bullish_candle = (close >= bar_open) or (close >= prev_close) or has_lower_tail
        rsi_stabilizing = (rsi >= prev_rsi - 1.5) or (rsi >= 38.0)
        held_trend_support = (close >= sma50) or (dist_sma50 >= -0.85)

        # Pullback Entry Rule 2:
        # RSI between 38 and 58, OR price at/near/below 20-day SMA (no breakout chasing)
        pullback_zone = (38.0 <= rsi <= 58.0) or (close <= sma20 * 1.01)
        pullback_ok = pullback_zone and bullish_candle and rsi_stabilizing and held_trend_support
        not_overextended = dist_sma50 <= 3.2

        # Macro Score Filter Rule 2:
        # macro_composite_score must be >= 0.0 (constructive/neutral macro bias). If below -0.5, strict no-buy discount trap.
        macro_score_ok = (macro_score is None) or (macro_score >= 0.0)

        # 2. Leveraged Bull ETFs (GDXU, UGL, AGQ):
        # Rule 2: Requires macro score > +1.5 AND strong trend
        if is_leveraged_bull:
            strong_trend = (adx >= 20.0) and (close > sma50)
            strong_macro = (macro_score is None) or (macro_score > 1.5)
            if is_bull_regime and macro_score_ok and strong_macro and strong_trend and pullback_ok and not_overextended:
                conf = 0.74 + (0.06 if close > sma50 else 0.0) + (0.04 if adx >= 25.0 else 0.0) + (0.03 if close >= bar_open else 0.0)
                thesis = f"Gold/Silver Macro: Leveraged pullback bounce in strong bull regime for {sym} (RSI={rsi:.1f})"
                return Signal.BUY, min(max(conf, 0.70), 0.95), thesis
            return Signal.HOLD, 0.35, f"Leveraged bull vehicle {sym} is unshortable and stands aside (requires macro score > +1.5 & strong trend)"

        # 3. Silver (SLV, SIL, SILJ, PSLV):
        # Rule 2: Stepped up when macro score >= +0.2 or GSR stretched (z >= 0.8)
        if is_silver:
            strong_trend = (adx >= 20.0) and (close > sma50)
            strong_macro = (macro_score is not None and macro_score >= 0.2 and strong_trend)
            silver_gsr_edge = (gsr_z is not None and gsr_z >= 0.8)
            if is_bull_regime and macro_score_ok and (strong_macro or silver_gsr_edge or macro_score is None) and pullback_ok and not_overextended:
                conf = 0.72 + (0.06 if close > sma50 else 0.0) + (0.04 if adx >= 20.0 else 0.0) + (0.03 if close >= bar_open else 0.0)
                if silver_gsr_edge:
                    conf += 0.08
                    thesis = f"Gold/Silver Macro: Silver catch-up pullback entry [GSR stretched z={gsr_z:.1f}] (RSI={rsi:.1f})"
                else:
                    thesis = f"Gold/Silver Macro: Silver pullback bounce in bull regime (RSI={rsi:.1f})"
                return Signal.BUY, min(max(conf, 0.70), 0.95), thesis
            return Signal.HOLD, 0.35, f"Silver {sym} stands aside (requires macro score >= +0.2 or GSR >= 0.8 catch-up)"

        # 4. Standard Bullion (GLD, IAU, PHYS, etc.):
        if is_bull_regime and macro_score_ok and pullback_ok and not_overextended:
            conf = 0.72 + (0.06 if close > sma50 else 0.0) + (0.04 if adx >= 20.0 else 0.0) + (0.03 if close >= bar_open else 0.0)
            macro_str = f", macro={macro_score:+.2f}" if macro_score is not None else ""
            thesis = f"Gold/Silver Macro: Pullback bounce in bull regime (RSI={rsi:.1f}, above SMA{'200' if has_sma200 else '50'}{macro_str})"
            if three_conf_state == "all_three_aligned":
                conf = min(0.95, conf + 0.05)
                thesis += " [All 3 Macro Confirmations Aligned]"
            elif oil_trend == "cooling_inflation":
                conf = min(0.95, conf + 0.03)
                thesis += " [Oil Inflation Cooling]"
            return Signal.BUY, min(max(conf, 0.70), 0.95), thesis

        # 5. Bearish / Short Gate:
        # Rule 3:
        # - Price < 200 SMA and macro score <= -0.5
        # - Rising Treasury yields (yield_trend == "rising_yields")
        # - Strict prohibition: Dollar data Neutral or Mixed, OR Bullish Decoupling Divergence -> NO short trade!
        if allow_short:
            if gold_dollar_divergence:
                return Signal.HOLD, 0.30, "Shorting strictly prohibited: Bullish Decoupling Divergence (Case C: Gold holding bullish structure against rising USD)"
            if dollar_mixed or dollar_trend == "neutral":
                return Signal.HOLD, 0.30, "Shorting strictly prohibited: US Dollar data is mixed or neutral"

            bear_macro_ok = (macro_score is None) or (macro_score <= -0.5)
            yields_rising = (yield_trend == "rising_yields") or ("yield_trend" not in row)
            confirmed_bear_structure = (
                is_bear_regime
                and bear_macro_ok
                and yields_rising
                and close < sma50
                and close < sma20
                and macd_hist < 0
                and adx >= (22.0 if is_miner else 18.0)
                and rsi <= 48.0
                and dist_sma50 >= -3.2
            )
            if confirmed_bear_structure:
                conf = 0.72 + (0.05 if rsi < 40.0 else 0.0) + (0.04 if not np.isnan(row["sma10"]) and close < float(row["sma10"]) else 0.0)
                return Signal.SELL, min(conf, 0.90), f"Gold/Silver Macro: Confirmed short breakdown below SMA{'200' if has_sma200 else '50'} (RSI={rsi:.1f}, ADX={adx:.1f})"

    else:
        # Balanced (default) & Custom: balanced multi-factor alignment
        if adx < 18.0:
            return Signal.HOLD, 0.30, f"Chop tape (ADX {adx:.1f} < 18)"
        long_ok = close > sma20 and 40.0 <= rsi <= 72.0 and dist_sma50 >= -1.0 and dist_sma50 <= 3.2
        short_ok = close < sma20 and 28.0 <= rsi <= 60.0 and dist_sma50 <= 1.0 and dist_sma50 >= -3.2

        if long_ok and macd_hist >= -0.05:
            conf = 0.62 + (0.08 if htf_bullish else 0.0) + (0.06 if adx >= 22 else 0.0) + (0.06 if vol_ratio >= 1.0 else 0.0)
            return Signal.BUY, min(conf, 0.92), f"Balanced Long: Multi-factor trend alignment (ADX={adx:.1f}, RSI={rsi:.1f})"
        if allow_short and short_ok and macd_hist <= 0.05:
            conf = 0.60 + (0.08 if htf_bearish else 0.0) + (0.06 if adx >= 22 else 0.0) + (0.06 if vol_ratio >= 1.0 else 0.0)
            return Signal.SELL, min(conf, 0.90), f"Balanced Short: Multi-factor bear alignment (ADX={adx:.1f}, RSI={rsi:.1f})"

    return Signal.HOLD, 0.40, "No clear playbook alignment"


def run_ai_backtest(
    bars: pd.DataFrame,
    *,
    symbol: str = "",
    params: AiBacktestParams | None = None,
    macro_bars: dict[str, pd.DataFrame] | None = None,
    calendar_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replay the AI Trader engine walk-forward over historical bars."""
    p = params or AiBacktestParams()
    if p.preset == "gold_silver_macro":
        if p.take_profit_r == 2.0:
            p.take_profit_r = 4.0
        if p.trail_after_r == 1.0:
            p.trail_after_r = 2.0
        if p.min_confidence < 0.70:
            p.min_confidence = 0.70

    if bars is None or bars.empty or "close" not in bars.columns:
        raise ValueError("AI backtest requires historical bars with a close column")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("AI backtest requires a DatetimeIndex")
    if p.initial_cash <= 0:
        raise ValueError("initial_cash must be > 0")

    df = bars.sort_index().copy()
    n = len(df)
    warmup = 35 if n < 100 else (50 if n < 300 else 60)
    if n <= warmup + 2:
        raise ValueError(f"AI backtest needs more than {warmup + 2} bars, got {n}")

    frame = compute_ai_indicator_frame(
        df,
        symbol=symbol,
        macro_bars=macro_bars,
        track_dollar_only=bool(getattr(p, "metals_dollar_index_only", False)),
    )
    slip = max(0.0, p.slippage_bps) / 10_000.0
    cash = float(p.initial_cash)
    equity = cash
    position: _OpenPosition | None = None
    trades: list[AiBacktestTrade] = []
    equity_curve: list[dict[str, Any]] = []
    signals_seen = 0
    signals_approved = 0

    # Parse high-impact calendar events
    parsed_events: list[tuple[datetime, str]] = []
    if calendar_events:
        for ev in calendar_events:
            w_str = ev.get("when_utc") or ev.get("date")
            impact = str(ev.get("impact") or "High")
            if not w_str or impact != "High":
                continue
            try:
                dt_obj = datetime.fromisoformat(str(w_str).replace("Z", "+00:00"))
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                title = str(ev.get("title") or "Economic Event")
                parsed_events.append((dt_obj, title))
            except Exception:
                pass

    def _fill(price: float, side: str, direction: str) -> float:
        """Apply slippage against the trade direction."""
        adverse = 1.0 + slip if (side == "long") == (direction == "in") else 1.0 - slip
        return round(price * adverse, 4)

    def _close_position(exit_bar_idx: int, price: float, reason: str) -> None:
        nonlocal position, cash, equity
        assert position is not None
        exit_price = _fill(price, position.side, "out")
        direction = 1.0 if position.side == "long" else -1.0
        pnl = round((exit_price - position.entry_price) * position.qty * direction, 2)
        pnl_pct = round(
            ((exit_price / position.entry_price - 1.0) * direction) * 100.0, 2
        ) if position.entry_price > 0 else 0.0
        r_multiple = (
            round(pnl / (position.stop_distance * position.qty), 2)
            if position.stop_distance > 0 and position.qty > 0
            else 0.0
        )
        cash = round(cash + position.qty * position.entry_price + pnl if position.side == "long" else cash + pnl, 2)
        equity = cash
        trades.append(
            AiBacktestTrade(
                symbol=symbol,
                side=position.side,
                entry_time=position.entry_time,
                entry_price=position.entry_price,
                exit_time=str(frame.index[exit_bar_idx]),
                exit_price=exit_price,
                qty=position.qty,
                pnl=pnl,
                pnl_pct=pnl_pct,
                r_multiple=r_multiple,
                exit_reason=reason,
                entry_reason=position.entry_reason,
                confidence=position.confidence,
                thesis=position.thesis,
            )
        )
        position = None

    for i in range(warmup, n):
        curr_row = frame.iloc[i]
        prev_row = frame.iloc[i - 1] if i > 0 else None
        bar_open = float(curr_row["open"])
        bar_high = float(curr_row["high"])
        bar_low = float(curr_row["low"])
        bar_close = float(curr_row["close"])
        bar_atr = float(curr_row["atr14"]) if not np.isnan(curr_row["atr14"]) else bar_close * 0.015
        stamp = frame.index[i]

        # Check macro events
        event_5m = False
        event_45m = False
        if parsed_events:
            try:
                cur_dt = stamp if isinstance(stamp, datetime) else pd.to_datetime(stamp).to_pydatetime()
                if cur_dt.tzinfo is None:
                    cur_dt = cur_dt.replace(tzinfo=timezone.utc)
                for ev_dt, imp in parsed_events:
                    diff_m = (ev_dt - cur_dt).total_seconds() / 60.0
                    if 0.0 <= diff_m <= 5.0:
                        event_5m = True
                    if 0.0 <= diff_m <= 45.0:
                        event_45m = True
            except Exception:
                pass

        # Rule 5: 5-minute economic event protection
        if event_5m and position is not None and p.preset == "gold_silver_macro" and not getattr(p, "metals_dollar_index_only", False):
            if position.side == "long":
                event_stop = round(bar_close * (1.0 - 0.008), 2)
                if event_stop > position.stop:
                    position.stop = event_stop
                    position.event_protected = True
            elif position.side == "short":
                event_stop = round(bar_close * (1.0 + 0.008), 2)
                if event_stop < position.stop:
                    position.stop = event_stop
                    position.event_protected = True

        # 1. Manage open position exits on this bar
        if position is not None:
            position.peak_price = max(position.peak_price, bar_high)
            position.trough_price = min(position.trough_price, bar_low)

            if position.side == "long":
                # Trailing stop arming & ratcheting (Rule 7: ratchet to breakeven after 2.0R)
                gain_r = (bar_high - position.entry_price) / position.stop_distance if position.stop_distance > 0 else 0.0
                if p.trail_after_r > 0 and gain_r >= p.trail_after_r:
                    position.trail_armed = True
                    trailed_stop = max(position.entry_price, position.peak_price - position.stop_distance)
                    position.stop = max(position.stop, round(trailed_stop, 2))

                # Check Stop Loss hit
                if bar_low <= position.stop:
                    fill_p = position.stop if bar_open >= position.stop else bar_open
                    reason = "trailing_stop" if position.trail_armed else ("event_stop_loss" if position.event_protected else "stop_loss")
                    _close_position(i, fill_p, reason)
                # Check Take Profit target & Scale-out (Rule 7: Scale-out 50% at 4.0R, runner trails)
                elif position.target > 0 and (bar_high >= position.target or (p.preset == "gold_silver_macro" and gain_r >= p.take_profit_r)):
                    if p.preset == "gold_silver_macro" and not position.scaled_out and position.qty >= 2.0:
                        trim_qty = round(position.qty / 2.0, 4)
                        fill_p = position.target if bar_open <= position.target else bar_open
                        exit_p = _fill(fill_p, "long", "out")
                        pnl = round((exit_p - position.entry_price) * trim_qty, 2)
                        pnl_pct = round(((exit_p / position.entry_price) - 1.0) * 100.0, 2)
                        r_mult = round(pnl / (position.stop_distance * trim_qty), 2) if position.stop_distance > 0 else 0.0
                        cash = round(cash + trim_qty * position.entry_price + pnl, 2)
                        trades.append(
                            AiBacktestTrade(
                                symbol=symbol,
                                side="long",
                                entry_time=position.entry_time,
                                entry_price=position.entry_price,
                                exit_time=str(frame.index[i]),
                                exit_price=exit_p,
                                qty=trim_qty,
                                pnl=pnl,
                                pnl_pct=pnl_pct,
                                r_multiple=r_mult,
                                exit_reason="take_profit_scale_out_4.0r",
                                entry_reason=position.entry_reason,
                                confidence=position.confidence,
                                thesis=position.thesis,
                            )
                        )
                        position.qty = round(position.qty - trim_qty, 4)
                        position.scaled_out = True
                        position.target = 0.0  # Let remaining 50% trail with ATR stop
                    else:
                        fill_p = position.target if bar_open <= position.target else bar_open
                        _close_position(i, fill_p, "take_profit")
                # Regime flip, inverse decay protection or macro exit for Gold/Silver
                elif p.preset == "gold_silver_macro":
                    has_200 = not np.isnan(curr_row["sma200"])
                    macro_sc = float(curr_row["macro_composite_score"]) if "macro_composite_score" in curr_row and not pd.isna(curr_row["macro_composite_score"]) else None
                    is_inv = symbol.upper() in {"GLL", "GDXD", "ZSL", "JDST"}
                    bars_held = i - position.entry_bar
                    if is_inv and (float(curr_row["rsi14"]) >= 65.0 or bars_held >= 35):
                        _close_position(i, bar_close, "inverse_decay_protection")
                    elif has_200 and curr_row["close"] < curr_row["sma200"] and curr_row["close"] < curr_row["sma50"]:
                        _close_position(i, bar_close, "regime_flip")
                    elif macro_sc is not None and macro_sc < -1.0:
                        _close_position(i, bar_close, "regime_flip")
                # Strategy trend break exit for general presets (close below SMA20 + MACD flipping negative)
                elif curr_row["close"] < curr_row["sma20"] and curr_row["macd_hist"] < 0 and prev_row is not None and prev_row["macd_hist"] >= 0:
                    _close_position(i, bar_close, "trend_break")

            elif position.side == "short":
                # Rule 4: Mixed Dollar Reversal
                # If US Dollar index or economic data is Mixed/Neutral, immediately cover short and reverse to long!
                dollar_is_mixed = bool(curr_row.get("dollar_mixed", False) or str(curr_row.get("dollar_trend", "")).lower() == "neutral")
                if p.preset == "gold_silver_macro" and dollar_is_mixed:
                    stopped_qty = position.qty
                    _close_position(i, bar_close, "dollar_mixed_reversal")
                    # Immediately reverse to Long position
                    side = "long"
                    entry_p = _fill(bar_close, side, "in")
                    stop_dist = max(0.01, bar_atr * max(0.5, p.atr_stop_mult))
                    risk_budget = equity * (max(0.1, p.risk_pct) / 100.0)
                    target_qty = risk_budget / stop_dist if stop_dist > 0 else stopped_qty
                    if p.qty is not None and p.qty > 0:
                        target_qty = float(p.qty)
                    max_qty = (cash * 0.98) / entry_p if entry_p > 0 else 0.0
                    qty = min(target_qty, max_qty) if max_qty > 0 else target_qty
                    qty = round(max(1.0 if qty >= 1.0 else 0.01, qty), 4)
                    if entry_p > 0 and qty > 0 and qty * entry_p <= cash * 1.05:
                        stop_price = round(entry_p - stop_dist, 2)
                        target_price = round(entry_p + stop_dist * p.take_profit_r, 2) if p.take_profit_r > 0 else 0.0
                        cash = round(cash - qty * entry_p, 2)
                        position = _OpenPosition(
                            side=side,
                            entry_price=entry_p,
                            entry_time=str(stamp),
                            entry_bar=i,
                            qty=qty,
                            stop=stop_price,
                            target=target_price,
                            stop_distance=stop_dist,
                            peak_price=entry_p,
                            trough_price=entry_p,
                            entry_reason=f"[{p.preset}] Dollar Mixed Reversal to Long",
                            confidence=0.85,
                            thesis="US Dollar data is mixed/neutral; closed short and reversed to long to avoid squeeze.",
                        )

                if position is not None and position.side == "short":
                    # Trailing stop arming & ratcheting (Rule 7)
                    gain_r = (position.entry_price - bar_low) / position.stop_distance if position.stop_distance > 0 else 0.0
                    if p.trail_after_r > 0 and gain_r >= p.trail_after_r:
                        position.trail_armed = True
                        trailed_stop = min(position.entry_price, position.trough_price + position.stop_distance)
                        position.stop = min(position.stop, round(trailed_stop, 2))

                    # Check Stop Loss hit
                    if bar_high >= position.stop:
                        fill_p = position.stop if bar_open <= position.stop else bar_open
                        reason = "trailing_stop" if position.trail_armed else ("event_stop_loss" if position.event_protected else "stop_loss")
                        stopped_qty = position.qty
                        _close_position(i, fill_p, reason)

                        # Rule 6: Reversal Buy on Stop
                        # When a short position gets stopped out, immediately submit Reversal Buy (long)
                        # with a new protective stop armed at -0.8% below entry.
                        if p.preset == "gold_silver_macro" and getattr(p, "reversal_buy_on_stop", True):
                            rev_entry_p = _fill(fill_p, "long", "in")
                            rev_stop = round(rev_entry_p * (1.0 - 0.008), 2)  # -0.8% protective stop
                            rev_stop_dist = max(0.01, rev_entry_p - rev_stop)
                            rev_target = round(rev_entry_p + rev_stop_dist * p.take_profit_r, 2) if p.take_profit_r > 0 else 0.0
                            if cash >= rev_entry_p * stopped_qty * 0.95:
                                cash = round(cash - stopped_qty * rev_entry_p, 2)
                                position = _OpenPosition(
                                    side="long",
                                    entry_price=rev_entry_p,
                                    entry_time=str(stamp),
                                    entry_bar=i,
                                    qty=stopped_qty,
                                    stop=rev_stop,
                                    target=rev_target,
                                    stop_distance=rev_stop_dist,
                                    peak_price=rev_entry_p,
                                    trough_price=rev_entry_p,
                                    entry_reason=f"[{p.preset}] Reversal Buy on Stop",
                                    confidence=0.80,
                                    thesis="Short stopped out; executed instant reversal buy with -0.8% protective stop.",
                                )

                    # Check Take Profit target & Scale-out (Rule 7: Scale-out 50% at 4.0R, runner trails)
                    elif position.target > 0 and (bar_low <= position.target or (p.preset == "gold_silver_macro" and gain_r >= p.take_profit_r)):
                        if p.preset == "gold_silver_macro" and not position.scaled_out and position.qty >= 2.0:
                            trim_qty = round(position.qty / 2.0, 4)
                            fill_p = position.target if bar_open >= position.target else bar_open
                            exit_p = _fill(fill_p, "short", "out")
                            pnl = round((position.entry_price - exit_p) * trim_qty, 2)
                            pnl_pct = round(((position.entry_price / exit_p) - 1.0) * 100.0, 2) if exit_p > 0 else 0.0
                            r_mult = round(pnl / (position.stop_distance * trim_qty), 2) if position.stop_distance > 0 else 0.0
                            cash = round(cash + pnl, 2)
                            trades.append(
                                AiBacktestTrade(
                                    symbol=symbol,
                                    side="short",
                                    entry_time=position.entry_time,
                                    entry_price=position.entry_price,
                                    exit_time=str(frame.index[i]),
                                    exit_price=exit_p,
                                    qty=trim_qty,
                                    pnl=pnl,
                                    pnl_pct=pnl_pct,
                                    r_multiple=r_mult,
                                    exit_reason="take_profit_scale_out_4.0r",
                                    entry_reason=position.entry_reason,
                                    confidence=position.confidence,
                                    thesis=position.thesis,
                                )
                            )
                            position.qty = round(position.qty - trim_qty, 4)
                            position.scaled_out = True
                            position.target = 0.0
                        else:
                            fill_p = position.target if bar_open >= position.target else bar_open
                            _close_position(i, fill_p, "take_profit")
                    # Regime flip exit for Gold/Silver
                    elif p.preset == "gold_silver_macro":
                        has_200 = not np.isnan(curr_row["sma200"])
                        macro_sc = float(curr_row["macro_composite_score"]) if "macro_composite_score" in curr_row and not pd.isna(curr_row["macro_composite_score"]) else None
                        if has_200 and curr_row["close"] > curr_row["sma200"]:
                            _close_position(i, bar_close, "regime_flip")
                        elif macro_sc is not None and macro_sc > -0.5:
                            _close_position(i, bar_close, "regime_flip")
                        elif (not has_200 or curr_row["close"] > curr_row["sma50"]) and curr_row["macd_hist"] > 0 and prev_row is not None and prev_row["macd_hist"] > 0:
                            _close_position(i, bar_close, "trend_break")
                    # Strategy trend break exit
                    elif curr_row["close"] > curr_row["sma20"] and curr_row["macd_hist"] > 0 and prev_row is not None and prev_row["macd_hist"] <= 0:
                        _close_position(i, bar_close, "trend_break")

        # 2. Evaluate AI entry signals if flat (or flipping)
        sig, conf, thesis = evaluate_ai_signal(
            curr_row,
            prev_row,
            p,
            allow_short=p.allow_short,
            symbol=symbol,
            event_imminent_45m=event_45m,
        )
        if sig in (Signal.BUY, Signal.SELL):
            signals_seen += 1

        if position is None and sig in (Signal.BUY, Signal.SELL) and conf >= p.min_confidence:
            signals_approved += 1
            side = "long" if sig is Signal.BUY else "short"
            entry_p = _fill(bar_close, side, "in")
            stop_dist = max(0.01, bar_atr * max(0.5, p.atr_stop_mult))

            # Risk-based sizing: risk_pct of current equity / stop distance
            risk_budget = equity * (max(0.1, p.risk_pct) / 100.0)
            if p.preset == "gold_silver_macro" and symbol.upper() in {"SLV", "AGQ", "SIL", "SILJ", "PSLV"}:
                gsr_val = float(curr_row["gsr_z"]) if "gsr_z" in curr_row and not pd.isna(curr_row["gsr_z"]) else 0.0
                if gsr_val >= 0.8:
                    risk_budget = round(risk_budget * 1.25, 2)
            target_qty = risk_budget / stop_dist if stop_dist > 0 else 1.0

            # Override with fixed qty if user specified
            if p.qty is not None and p.qty > 0:
                target_qty = float(p.qty)

            # Cap by cash / equity
            max_qty = (cash * 0.98) / entry_p if entry_p > 0 else 0.0
            qty = min(target_qty, max_qty) if max_qty > 0 else target_qty
            qty = round(max(1.0 if qty >= 1.0 else 0.01, qty), 4)

            if entry_p > 0 and qty > 0 and (side == "short" or qty * entry_p <= cash * 1.05):
                direction = 1.0 if side == "long" else -1.0
                stop_price = round(entry_p - direction * stop_dist, 2)
                target_price = (
                    round(entry_p + direction * stop_dist * p.take_profit_r, 2)
                    if p.take_profit_r > 0
                    else 0.0
                )
                if side == "long":
                    cash = round(cash - qty * entry_p, 2)

                position = _OpenPosition(
                    side=side,
                    entry_price=entry_p,
                    entry_time=str(stamp),
                    entry_bar=i,
                    qty=qty,
                    stop=stop_price,
                    target=target_price,
                    stop_distance=stop_dist,
                    peak_price=entry_p,
                    trough_price=entry_p,
                    entry_reason=f"[{p.preset}] conf={conf:.2f}",
                    confidence=conf,
                    thesis=thesis,
                )

        # 3. Mark to market equity at bar close
        unrealized = 0.0
        if position is not None:
            direction = 1.0 if position.side == "long" else -1.0
            unrealized = (bar_close - position.entry_price) * position.qty * direction
            current_eq = cash + position.qty * bar_close if position.side == "long" else cash + unrealized
        else:
            current_eq = cash

        equity = max(0.0, current_eq)
        equity_curve.append(
            {
                "t": _ts_str(stamp),
                "equity": round(equity, 2),
                "cash": round(cash, 2),
                "price": round(bar_close, 4),
                "position": round(position.qty if position else 0.0, 4),
            }
        )

    # Square off any remaining position at end of backtest window
    if position is not None:
        last_close = float(frame["close"].iloc[-1])
        _close_position(n - 1, last_close, "end_of_data")
        equity_curve.append(
            {
                "t": _ts_str(frame.index[-1]),
                "equity": round(cash, 2),
                "cash": round(cash, 2),
                "price": round(last_close, 4),
                "position": 0.0,
            }
        )

    # Buy & Hold return on evaluation window
    first_p = float(frame["close"].iloc[warmup]) if warmup < n else 0.0
    last_p = float(frame["close"].iloc[-1]) if n > 0 else 0.0
    buy_hold_pct = round(((last_p / first_p) - 1.0) * 100.0, 2) if first_p > 0 else 0.0

    return _score_ai_results(
        trades=trades,
        equity_curve=equity_curve,
        params=p,
        symbol=symbol,
        signals_seen=signals_seen,
        signals_approved=signals_approved,
        start_time=_ts_str(frame.index[warmup]),
        end_time=_ts_str(frame.index[-1]),
        total_bars=n,
        warmup_bars=warmup,
        buy_hold_pct=buy_hold_pct,
    )


def _score_ai_results(
    trades: list[AiBacktestTrade],
    equity_curve: list[dict[str, Any]],
    params: AiBacktestParams,
    symbol: str,
    signals_seen: int,
    signals_approved: int,
    *,
    start_time: str = "",
    end_time: str = "",
    total_bars: int = 0,
    warmup_bars: int = 0,
    buy_hold_pct: float = 0.0,
) -> dict[str, Any]:
    """Assemble final metrics, paired trade list, and comparison summary."""
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    final_equity = equity_curve[-1]["equity"] if equity_curve else params.initial_cash
    r_values = [t.r_multiple for t in trades]
    closed = len(trades)
    win_rate = round(len(wins) / closed, 4) if closed else 0.0
    strat_return_pct = round((final_equity / params.initial_cash - 1.0) * 100.0, 2) if params.initial_cash else 0.0
    alpha_pct = round(strat_return_pct - buy_hold_pct, 2)

    # Paired trade list for desk table
    trade_list: list[dict[str, Any]] = []
    for idx, t in enumerate(trades, start=1):
        entry_side = "buy" if t.side == "long" else "short"
        exit_side = "sell" if t.side == "long" else "cover"
        trade_sym = t.symbol or symbol
        trade_list.append(
            {
                "symbol": trade_sym,
                "side": entry_side,
                "time": t.entry_time,
                "price": t.entry_price,
                "qty": t.qty,
                "reason": t.entry_reason or f"AI {t.side}",
                "pnl": None,
                "pnl_pct": None,
                "group_id": idx,
                "thesis": t.thesis,
            }
        )
        trade_list.append(
            {
                "symbol": trade_sym,
                "side": exit_side,
                "time": t.exit_time,
                "price": t.exit_price,
                "qty": t.qty,
                "reason": t.exit_reason or "exit",
                "pnl": t.pnl,
                "pnl_pct": t.pnl_pct,
                "group_id": idx,
                "r_multiple": t.r_multiple,
            }
        )

    eq_vals = [e["equity"] for e in equity_curve]
    sharpe, sortino = _sharpe_sortino(eq_vals)
    max_dd = _max_drawdown_pct(eq_vals)

    # Annualized return (CAGR) assuming ~252 trading days per year
    eval_bars = max(1, total_bars - warmup_bars)
    years = max(1.0 / 252.0, eval_bars / 252.0)
    if final_equity > 0 and params.initial_cash > 0:
        annualized_return_pct = round(((final_equity / params.initial_cash) ** (1.0 / years) - 1.0) * 100.0, 2)
    else:
        annualized_return_pct = 0.0

    daily_returns = [
        round((eq_vals[k] / eq_vals[k - 1] - 1.0), 5)
        for k in range(1, len(eq_vals))
        if eq_vals[k - 1] > 0
    ]

    preset_meta = get_preset(params.preset)
    preset_label = preset_meta.label if params.preset != "custom" else "Custom"
    label = preset_label if preset_label.startswith("AI ") else f"AI {preset_label}"
    if label.startswith("AI AI "):
        label = label[3:].lstrip()

    return {
        "symbol": symbol,
        "mode": "ai",
        "ai_preset": params.preset,
        "ai_preset_label": preset_label,
        "trades": closed,
        "round_trips": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "win_rate_pct": round(win_rate * 100.0, 2),
        "total_return_pct": strat_return_pct,
        "annualized_return_pct": annualized_return_pct,
        "cagr_pct": annualized_return_pct,
        "buy_hold_return_pct": buy_hold_pct,
        "alpha_pct": alpha_pct,
        "initial_cash": round(float(params.initial_cash), 2),
        "final_equity": round(final_equity, 2),
        "realized_pnl": round(sum(t.pnl for t in trades), 2),
        "open_qty": 0.0,
        "open_entry": None,
        "open_mark": None,
        "unrealized_pnl": 0.0,
        "unrealized_pnl_pct": 0.0,
        "start": start_time,
        "end": end_time,
        "bars": total_bars,
        "warmup_bars": warmup_bars,
        "evaluated_bars": max(0, total_bars - warmup_bars),
        "avg_r": round(sum(r_values) / len(r_values), 3) if r_values else 0.0,
        "total_r": round(sum(r_values), 2),
        "expectancy": round(sum(t.pnl for t in trades) / len(trades), 2) if trades else 0.0,
        "profit_factor": (
            round(gross_win / gross_loss, 2)
            if gross_loss > 0
            else (float("inf") if gross_win > 0 else 0.0)
        ),
        "sharpe": sharpe,
        "sharpe_ratio": sharpe,
        "sortino": sortino,
        "sortino_ratio": sortino,
        "max_drawdown_pct": max_dd,
        "daily_returns": daily_returns,
        "summary_metrics": {
            "total_return_pct": strat_return_pct,
            "win_rate_pct": round(win_rate * 100.0, 2),
            "trades": closed,
            "sharpe": sharpe,
            "max_drawdown_pct": max_dd,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        },
        "signals_seen": signals_seen,
        "signals_approved": signals_approved,
        "trade_list": trade_list,
        "trade_log": [asdict(t) for t in trades],
        "equity_curve": _downsample_curve(equity_curve),
        "params": {
            "ai_preset": params.preset,
            "label": label,
            "min_confidence": params.min_confidence,
            "atr_stop_mult": params.atr_stop_mult,
            "take_profit_r": params.take_profit_r,
            "trail_after_r": params.trail_after_r,
            "risk_pct": params.risk_pct,
            "max_positions": params.max_positions,
            "slippage_bps": params.slippage_bps,
        },
    }


def run_ai_portfolio_backtest(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    params: AiBacktestParams | None = None,
    macro_bars: dict[str, pd.DataFrame] | None = None,
    calendar_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Simulate a shared-cash portfolio backtest across multiple symbols."""
    p = params or AiBacktestParams()
    symbols = [s for s, b in bars_by_symbol.items() if b is not None and not b.empty and "close" in b.columns]
    if not symbols:
        raise ValueError("AI portfolio backtest requires bar data for at least one symbol")

    frames: dict[str, pd.DataFrame] = {}
    warmups: dict[str, int] = {}
    for s in symbols:
        b = bars_by_symbol[s].sort_index()
        frames[s] = compute_ai_indicator_frame(
            b,
            symbol=s,
            macro_bars=macro_bars,
            track_dollar_only=bool(getattr(p, "metals_dollar_index_only", False)),
        )
        warmups[s] = 35 if len(b) < 100 else 50

    # Parse high-impact calendar events
    parsed_events: list[tuple[datetime, str]] = []
    if calendar_events:
        for ev in calendar_events:
            w_str = ev.get("when_utc") or ev.get("date")
            impact = str(ev.get("impact") or "High")
            if not w_str or impact != "High":
                continue
            try:
                dt_obj = datetime.fromisoformat(str(w_str).replace("Z", "+00:00"))
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                title = str(ev.get("title") or "Economic Event")
                parsed_events.append((dt_obj, title))
            except Exception:
                pass

    # Unified timestamp timeline
    all_stamps = sorted({ts for f in frames.values() for ts in f.index})
    if len(all_stamps) < 40:
        raise ValueError("Insufficient overlapping timeline for AI portfolio backtest")

    # Check if gold and silver symbols are in portfolio to compute historical GSR z-scores
    gld_sym = next((s for s in symbols if s.upper() in {"GLD", "IAU"}), None)
    slv_sym = next((s for s in symbols if s.upper() in {"SLV", "AGQ", "SIL"}), None)
    gsr_z_map: dict[Any, float] = {}
    if gld_sym and slv_sym:
        gld_closes = frames[gld_sym]["close"]
        slv_closes = frames[slv_sym]["close"]
        common_idx = gld_closes.index.intersection(slv_closes.index)
        if len(common_idx) >= 15:
            gsr = gld_closes.loc[common_idx] / slv_closes.loc[common_idx]
            rolling_mean = gsr.rolling(window=min(60, len(gsr)), min_periods=10).mean()
            rolling_std = gsr.rolling(window=min(60, len(gsr)), min_periods=10).std()
            for idx in common_idx:
                std_val = rolling_std.loc[idx]
                if pd.notna(std_val) and std_val > 1e-6:
                    gsr_z_map[idx] = round(float((gsr.loc[idx] - rolling_mean.loc[idx]) / std_val), 2)

    slip = max(0.0, p.slippage_bps) / 10_000.0
    cash = float(p.initial_cash)
    positions: dict[str, _OpenPosition | None] = {s: None for s in symbols}
    all_trades: list[AiBacktestTrade] = []
    equity_curve: list[dict[str, Any]] = []

    # Equal-weight hold calculation for the portfolio book & allocated leg cash
    first_prices = {
        s: float(frames[s]["close"].iloc[warmups[s]]) if warmups[s] < len(frames[s]) else float(frames[s]["close"].iloc[0])
        for s in symbols
    }
    hold_alloc = float(p.initial_cash) / max(1, len(symbols))
    hold_shares = {s: hold_alloc / first_prices[s] if first_prices[s] > 0 else 0.0 for s in symbols}
    last_known_close = {
        s: float(frames[s]["close"].iloc[warmups[s]]) if warmups[s] < len(frames[s]) else float(frames[s]["close"].iloc[0])
        for s in symbols
    }

    leg_equity_curves: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
    leg_realized_pnls: dict[str, float] = {s: 0.0 for s in symbols}
    leg_signals_seen: dict[str, int] = {s: 0 for s in symbols}
    leg_signals_approved: dict[str, int] = {s: 0 for s in symbols}

    def _fill(price: float, side: str, direction: str) -> float:
        adverse = 1.0 + slip if (side == "long") == (direction == "in") else 1.0 - slip
        return round(price * adverse, 4)

    def _close_pos(sym: str, exit_p: float, exit_time_str: str, exit_reason: str) -> None:
        nonlocal cash
        pos = positions[sym]
        if pos is None:
            return
        direction = 1.0 if pos.side == "long" else -1.0
        if pos.side == "long":
            pnl = round((exit_p - pos.entry_price) * pos.qty, 2)
            cash = round(cash + pos.qty * pos.entry_price + pnl, 2)
            pnl_pct = round((exit_p / pos.entry_price - 1.0) * 100.0, 2)
        else:
            pnl = round((pos.entry_price - exit_p) * pos.qty, 2)
            cash = round(cash + pnl, 2)
            pnl_pct = round(((pos.entry_price / exit_p - 1.0) if exit_p > 0 else 0.0) * 100.0, 2)
        r_mult = round(pnl / (pos.stop_distance * pos.qty), 2) if pos.stop_distance > 0 else 0.0
        leg_realized_pnls[sym] = round(leg_realized_pnls[sym] + pnl, 2)
        all_trades.append(
            AiBacktestTrade(
                symbol=sym,
                side=pos.side,
                entry_time=pos.entry_time,
                entry_price=pos.entry_price,
                exit_time=exit_time_str,
                exit_price=exit_p,
                qty=pos.qty,
                pnl=pnl,
                pnl_pct=pnl_pct,
                r_multiple=r_mult,
                exit_reason=exit_reason,
                entry_reason=pos.entry_reason,
                confidence=pos.confidence,
                thesis=pos.thesis,
            )
        )
        positions[sym] = None

    for stamp in all_stamps:
        # Update last known close for available symbols at this stamp
        for s in symbols:
            if stamp in frames[s].index:
                last_known_close[s] = float(frames[s].loc[stamp]["close"])

        # Check event proximity (Rule 5 & Risk Filter)
        event_5m = False
        event_45m = False
        if parsed_events:
            try:
                curr_dt = stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
                if hasattr(curr_dt, "tzinfo") and curr_dt.tzinfo is None:
                    curr_dt = curr_dt.replace(tzinfo=timezone.utc)
                for ev_time, _ in parsed_events:
                    diff_m = (ev_time - curr_dt).total_seconds() / 60.0
                    if 0.0 <= diff_m <= 5.0:
                        event_5m = True
                    if 0.0 <= diff_m <= 45.0:
                        event_45m = True
            except Exception:
                pass

        # Rule 5: 5-minute economic event protection
        if event_5m and p.preset == "gold_silver_macro" and not getattr(p, "metals_dollar_index_only", False):
            for s in symbols:
                pos = positions[s]
                if pos is not None and stamp in frames[s].index:
                    b_close = float(frames[s].loc[stamp]["close"])
                    if pos.side == "long":
                        event_stop = round(b_close * (1.0 - 0.008), 2)
                        if event_stop > pos.stop:
                            pos.stop = event_stop
                            pos.event_protected = True
                    elif pos.side == "short":
                        event_stop = round(b_close * (1.0 + 0.008), 2)
                        if event_stop < pos.stop:
                            pos.stop = event_stop
                            pos.event_protected = True

        # 1. Manage exits
        for s in symbols:
            pos = positions[s]
            if pos is None or stamp not in frames[s].index:
                continue
            row = frames[s].loc[stamp]
            bar_open = float(row["open"])
            bar_high = float(row["high"])
            bar_low = float(row["low"])
            bar_close = float(row["close"])

            pos.peak_price = max(pos.peak_price, bar_high)
            pos.trough_price = min(pos.trough_price, bar_low)

            if pos.side == "long":
                # Trailing stop arming & ratcheting (Rule 7: ratchet to breakeven after 2.0R)
                gain_r = (bar_high - pos.entry_price) / pos.stop_distance if pos.stop_distance > 0 else 0.0
                if p.trail_after_r > 0 and gain_r >= p.trail_after_r:
                    pos.trail_armed = True
                    trailed_stop = max(pos.entry_price, pos.peak_price - pos.stop_distance)
                    pos.stop = max(pos.stop, round(trailed_stop, 2))

                # Check Stop Loss hit
                if bar_low <= pos.stop:
                    fill_p = pos.stop if bar_open >= pos.stop else bar_open
                    exit_p = _fill(fill_p, "long", "out")
                    reason = "trailing_stop" if pos.trail_armed else ("event_stop_loss" if pos.event_protected else "stop_loss")
                    _close_pos(s, exit_p, _ts_str(stamp), reason)
                # Check Take Profit target & Scale-out (Rule 7: Scale-out 50% at 4.0R, runner trails)
                elif pos.target > 0 and (bar_high >= pos.target or (p.preset == "gold_silver_macro" and gain_r >= p.take_profit_r)):
                    if p.preset == "gold_silver_macro" and not pos.scaled_out and pos.qty >= 2.0:
                        trim_qty = round(pos.qty / 2.0, 4)
                        fill_p = pos.target if bar_open <= pos.target else bar_open
                        exit_p = _fill(fill_p, "long", "out")
                        pnl = round((exit_p - pos.entry_price) * trim_qty, 2)
                        pnl_pct = round(((exit_p / pos.entry_price) - 1.0) * 100.0, 2)
                        r_mult = round(pnl / (pos.stop_distance * trim_qty), 2) if pos.stop_distance > 0 else 0.0
                        cash = round(cash + trim_qty * pos.entry_price + pnl, 2)
                        leg_realized_pnls[s] = round(leg_realized_pnls[s] + pnl, 2)
                        all_trades.append(
                            AiBacktestTrade(
                                symbol=s,
                                side="long",
                                entry_time=pos.entry_time,
                                entry_price=pos.entry_price,
                                exit_time=_ts_str(stamp),
                                exit_price=exit_p,
                                qty=trim_qty,
                                pnl=pnl,
                                pnl_pct=pnl_pct,
                                r_multiple=r_mult,
                                exit_reason="take_profit_scale_out_4.0r",
                                entry_reason=pos.entry_reason,
                                confidence=pos.confidence,
                                thesis=pos.thesis,
                            )
                        )
                        pos.qty = round(pos.qty - trim_qty, 4)
                        pos.scaled_out = True
                        pos.target = 0.0
                    else:
                        fill_p = pos.target if bar_open <= pos.target else bar_open
                        exit_p = _fill(fill_p, "long", "out")
                        _close_pos(s, exit_p, _ts_str(stamp), "take_profit")
                elif p.preset == "gold_silver_macro":
                    has_200 = not np.isnan(row["sma200"])
                    loc_now = frames[s].index.get_loc(stamp)
                    prev_hist = float(frames[s].iloc[loc_now - 1]["macd_hist"]) if isinstance(loc_now, int) and loc_now > 0 else 0.0
                    macro_sc = float(row["macro_composite_score"]) if "macro_composite_score" in row and not pd.isna(row["macro_composite_score"]) else None
                    is_inv = s.upper() in {"GLL", "GDXD", "ZSL", "JDST"}
                    bars_held = (loc_now - pos.entry_bar) if isinstance(loc_now, int) else 0
                    if is_inv and (float(row["rsi14"]) >= 65.0 or bars_held >= 35):
                        exit_p = _fill(bar_close, "long", "out")
                        _close_pos(s, exit_p, _ts_str(stamp), "inverse_decay_protection")
                    elif has_200 and bar_close < float(row["sma200"]) and bar_close < float(row["sma50"]):
                        exit_p = _fill(bar_close, "long", "out")
                        _close_pos(s, exit_p, _ts_str(stamp), "regime_flip")
                    elif macro_sc is not None and macro_sc < -1.0:
                        exit_p = _fill(bar_close, "long", "out")
                        _close_pos(s, exit_p, _ts_str(stamp), "regime_flip")
                elif float(row["close"]) < float(row["sma20"]) and float(row["macd_hist"]) < 0:
                    loc_now = frames[s].index.get_loc(stamp)
                    if isinstance(loc_now, int) and loc_now > 0 and float(frames[s].iloc[loc_now - 1]["macd_hist"]) >= 0:
                        exit_p = _fill(bar_close, "long", "out")
                        _close_pos(s, exit_p, _ts_str(stamp), "trend_break")

            elif pos.side == "short":
                # Rule 4: Mixed Dollar Reversal
                dollar_is_mixed = bool(row.get("dollar_mixed", False) or str(row.get("dollar_trend", "")).lower() == "neutral")
                if p.preset == "gold_silver_macro" and dollar_is_mixed:
                    stopped_qty = pos.qty
                    exit_p = _fill(bar_close, "short", "out")
                    _close_pos(s, exit_p, _ts_str(stamp), "dollar_mixed_reversal")
                    # Immediately reverse to Long position
                    side = "long"
                    entry_p = _fill(bar_close, side, "in")
                    bar_atr = float(row["atr14"]) if not np.isnan(row["atr14"]) and float(row["atr14"]) > 0 else bar_close * 0.02
                    stop_dist = max(0.01, bar_atr * max(0.5, p.atr_stop_mult))
                    risk_budget = port_equity * (max(0.1, p.risk_pct) / 100.0)
                    target_qty = risk_budget / stop_dist if stop_dist > 0 else stopped_qty
                    if p.qty is not None and p.qty > 0:
                        target_qty = float(p.qty)
                    max_qty = (cash * 0.95) / entry_p if entry_p > 0 else 0.0
                    qty = min(target_qty, max_qty) if max_qty > 0 else target_qty
                    qty = round(max(0.01, qty), 4)
                    if entry_p > 0 and qty > 0 and qty * entry_p <= cash:
                        stop_price = round(entry_p - stop_dist, 2)
                        target_price = round(entry_p + stop_dist * p.take_profit_r, 2) if p.take_profit_r > 0 else 0.0
                        cash = round(cash - qty * entry_p, 2)
                        loc_now = frames[s].index.get_loc(stamp)
                        positions[s] = _OpenPosition(
                            side=side,
                            entry_price=entry_p,
                            entry_time=_ts_str(stamp),
                            entry_bar=loc_now if isinstance(loc_now, int) else 0,
                            qty=qty,
                            stop=stop_price,
                            target=target_price,
                            stop_distance=stop_dist,
                            peak_price=entry_p,
                            trough_price=entry_p,
                            entry_reason=f"[{p.preset}] Dollar Mixed Reversal to Long",
                            confidence=0.85,
                            thesis="US Dollar data is mixed/neutral; closed short and reversed to long to avoid squeeze.",
                        )

                if positions[s] is not None and positions[s].side == "short":
                    pos = positions[s]
                    gain_r = (pos.entry_price - bar_low) / pos.stop_distance if pos.stop_distance > 0 else 0.0
                    if p.trail_after_r > 0 and gain_r >= p.trail_after_r:
                        pos.trail_armed = True
                        trailed_stop = min(pos.entry_price, pos.trough_price + pos.stop_distance)
                        pos.stop = min(pos.stop, round(trailed_stop, 2))

                    if bar_high >= pos.stop:
                        fill_p = pos.stop if bar_open <= pos.stop else bar_open
                        exit_p = _fill(fill_p, "short", "out")
                        reason = "trailing_stop" if pos.trail_armed else ("event_stop_loss" if pos.event_protected else "stop_loss")
                        stopped_qty = pos.qty
                        _close_pos(s, exit_p, _ts_str(stamp), reason)

                        # Rule 6: Reversal Buy on Stop
                        if p.preset == "gold_silver_macro" and getattr(p, "reversal_buy_on_stop", True):
                            rev_entry_p = _fill(fill_p, "long", "in")
                            rev_stop = round(rev_entry_p * (1.0 - 0.008), 2)
                            rev_stop_dist = max(0.01, rev_entry_p - rev_stop)
                            rev_target = round(rev_entry_p + rev_stop_dist * p.take_profit_r, 2) if p.take_profit_r > 0 else 0.0
                            if cash >= rev_entry_p * stopped_qty * 0.95:
                                cash = round(cash - stopped_qty * rev_entry_p, 2)
                                loc_now = frames[s].index.get_loc(stamp)
                                positions[s] = _OpenPosition(
                                    side="long",
                                    entry_price=rev_entry_p,
                                    entry_time=_ts_str(stamp),
                                    entry_bar=loc_now if isinstance(loc_now, int) else 0,
                                    qty=stopped_qty,
                                    stop=rev_stop,
                                    target=rev_target,
                                    stop_distance=rev_stop_dist,
                                    peak_price=rev_entry_p,
                                    trough_price=rev_entry_p,
                                    entry_reason="[gold_silver_macro] Reversal Buy on Short Stop",
                                    confidence=0.80,
                                    thesis="Short stopped out; immediately reversed into protective long position.",
                                )
                    elif pos.target > 0 and (bar_low <= pos.target or (p.preset == "gold_silver_macro" and gain_r >= p.take_profit_r)):
                        if p.preset == "gold_silver_macro" and not pos.scaled_out and pos.qty >= 2.0:
                            trim_qty = round(pos.qty / 2.0, 4)
                            fill_p = pos.target if bar_open >= pos.target else bar_open
                            exit_p = _fill(fill_p, "short", "out")
                            pnl = round((pos.entry_price - exit_p) * trim_qty, 2)
                            pnl_pct = round(((pos.entry_price / exit_p) - 1.0) * 100.0, 2) if exit_p > 0 else 0.0
                            r_mult = round(pnl / (pos.stop_distance * trim_qty), 2) if pos.stop_distance > 0 else 0.0
                            cash = round(cash + pnl, 2)
                            leg_realized_pnls[s] = round(leg_realized_pnls[s] + pnl, 2)
                            all_trades.append(
                                AiBacktestTrade(
                                    symbol=s,
                                    side="short",
                                    entry_time=pos.entry_time,
                                    entry_price=pos.entry_price,
                                    exit_time=_ts_str(stamp),
                                    exit_price=exit_p,
                                    qty=trim_qty,
                                    pnl=pnl,
                                    pnl_pct=pnl_pct,
                                    r_multiple=r_mult,
                                    exit_reason="take_profit_scale_out_4.0r",
                                    entry_reason=pos.entry_reason,
                                    confidence=pos.confidence,
                                    thesis=pos.thesis,
                                )
                            )
                            pos.qty = round(pos.qty - trim_qty, 4)
                            pos.scaled_out = True
                            pos.target = 0.0
                        else:
                            fill_p = pos.target if bar_open >= pos.target else bar_open
                            exit_p = _fill(fill_p, "short", "out")
                            _close_pos(s, exit_p, _ts_str(stamp), "take_profit")
                    elif p.preset == "gold_silver_macro":
                        has_200 = not np.isnan(row["sma200"])
                        loc_now = frames[s].index.get_loc(stamp)
                        prev_hist = float(frames[s].iloc[loc_now - 1]["macd_hist"]) if isinstance(loc_now, int) and loc_now > 0 else 0.0
                        macro_sc = float(row["macro_composite_score"]) if "macro_composite_score" in row and not pd.isna(row["macro_composite_score"]) else None
                        if has_200 and bar_close > float(row["sma200"]):
                            exit_p = _fill(bar_close, "short", "out")
                            _close_pos(s, exit_p, _ts_str(stamp), "regime_flip")
                        elif macro_sc is not None and macro_sc > -0.5:
                            exit_p = _fill(bar_close, "short", "out")
                            _close_pos(s, exit_p, _ts_str(stamp), "regime_flip")
                        elif (not has_200 or bar_close > float(row["sma50"])) and float(row["macd_hist"]) > 0 and prev_hist > 0:
                            exit_p = _fill(bar_close, "short", "out")
                            _close_pos(s, exit_p, _ts_str(stamp), "trend_break")
                    elif float(row["close"]) > float(row["sma20"]) and float(row["macd_hist"]) > 0:
                        loc_now = frames[s].index.get_loc(stamp)
                        if isinstance(loc_now, int) and loc_now > 0 and float(frames[s].iloc[loc_now - 1]["macd_hist"]) <= 0:
                            exit_p = _fill(bar_close, "short", "out")
                            _close_pos(s, exit_p, _ts_str(stamp), "trend_break")

        # Calculate active portfolio equity and per-leg equity using last known prices
        open_pos_count = sum(1 for p_pos in positions.values() if p_pos is not None)
        port_equity = cash
        for s in symbols:
            pos = positions[s]
            curr_px = last_known_close[s]
            unrealized = 0.0
            if pos is not None:
                direction = 1.0 if pos.side == "long" else -1.0
                unrealized = (curr_px - pos.entry_price) * pos.qty * direction
                port_equity += pos.qty * curr_px if pos.side == "long" else unrealized
            leg_eq = round(max(0.0, hold_alloc + leg_realized_pnls[s] + unrealized), 2)
            leg_equity_curves[s].append(
                {
                    "t": _ts_str(stamp),
                    "equity": leg_eq,
                    "cash": round(max(0.0, hold_alloc + leg_realized_pnls[s]), 2),
                    "positions": 1 if pos is not None else 0,
                }
            )

        # 2. Entries if under max_positions
        if open_pos_count < max(1, p.max_positions):
            for s in symbols:
                if positions[s] is not None or stamp not in frames[s].index:
                    continue
                # Opposing leveraged pair check: do not hold GDXU and GDXD simultaneously
                opp_sym = OPPOSING_METALS_PAIRS.get(s.upper())
                if opp_sym and positions.get(opp_sym) is not None:
                    continue
                loc = frames[s].index.get_loc(stamp)
                if isinstance(loc, slice) or not isinstance(loc, int) or loc < warmups[s]:
                    continue
                row = frames[s].iloc[loc]
                prev = frames[s].iloc[loc - 1] if loc > 0 else None
                sig, conf, thesis = evaluate_ai_signal(
                    row,
                    prev,
                    p,
                    allow_short=p.allow_short,
                    symbol=s,
                    gsr_z=gsr_z_map.get(stamp),
                    event_imminent_45m=event_45m,
                )
                leg_signals_seen[s] += 1
                if sig in (Signal.BUY, Signal.SELL) and conf >= p.min_confidence:
                    leg_signals_approved[s] += 1
                    bar_atr = float(row["atr14"]) if not np.isnan(row["atr14"]) and float(row["atr14"]) > 0 else float(row["close"]) * 0.02
                    side = "long" if sig is Signal.BUY else "short"
                    entry_p = _fill(float(row["close"]), side, "in")
                    stop_dist = max(0.01, bar_atr * max(0.5, p.atr_stop_mult))

                    risk_budget = port_equity * (max(0.1, p.risk_pct) / 100.0)
                    if p.preset == "gold_silver_macro" and s.upper() in {"SLV", "AGQ", "SIL", "SILJ", "PSLV"} and (gsr_z_map.get(stamp) or 0.0) >= 0.8:
                        risk_budget = round(risk_budget * 1.25, 2)
                    target_qty = risk_budget / stop_dist if stop_dist > 0 else 1.0
                    if p.max_positions >= 2:
                        max_cash_budget = min(cash * 0.95, port_equity * 0.55)
                    else:
                        max_cash_budget = cash * 0.95
                    max_cash_qty = max_cash_budget / entry_p if entry_p > 0 else 0.0
                    qty = min(target_qty, max_cash_qty) if max_cash_qty > 0 else target_qty
                    qty = round(max(0.01, qty), 4)

                    if qty > 0 and (side == "short" or qty * entry_p <= cash):
                        if side == "long":
                            cash = round(cash - qty * entry_p, 2)
                        direction = 1.0 if side == "long" else -1.0
                        positions[s] = _OpenPosition(
                            side=side,
                            entry_price=entry_p,
                            entry_time=_ts_str(stamp),
                            entry_bar=loc,
                            qty=qty,
                            stop=round(entry_p - direction * stop_dist, 2),
                            target=(
                                round(entry_p + direction * stop_dist * p.take_profit_r, 2)
                                if p.take_profit_r > 0
                                else 0.0
                            ),
                            stop_distance=stop_dist,
                            peak_price=entry_p,
                            trough_price=entry_p,
                            entry_reason=f"[{p.preset}] conf={conf:.2f}",
                            confidence=conf,
                            thesis=thesis,
                        )
                        open_pos_count += 1
                        if open_pos_count >= p.max_positions:
                            break

        equity_curve.append(
            {
                "t": _ts_str(stamp),
                "equity": round(max(0.0, port_equity), 2),
                "cash": round(cash, 2),
                "positions": open_pos_count,
            }
        )

    # Close any open positions at end
    for s in symbols:
        pos = positions[s]
        if pos is not None:
            last_px = float(frames[s]["close"].iloc[-1])
            exit_p = _fill(last_px, pos.side, "out")
            _close_pos(s, exit_p, _ts_str(all_stamps[-1]), "end_of_data")

    # Equal-weight buy & hold return of all symbols in the portfolio
    last_prices = {s: float(frames[s]["close"].iloc[-1]) if len(frames[s]) else 0.0 for s in symbols}
    last_hold_equity = sum(hold_shares[s] * last_prices.get(s, first_prices[s]) for s in symbols)
    book_buy_hold_pct = round((last_hold_equity / p.initial_cash - 1.0) * 100.0, 2) if p.initial_cash > 0 else 0.0

    # Per-leg individual results for detail inspection
    individual_legs: list[dict[str, Any]] = []
    for s in symbols:
        leg_trades = [t for t in all_trades if t.symbol == s]
        s_first_p = first_prices[s]
        s_last_p = last_prices.get(s, s_first_p)
        s_bh = round((s_last_p / s_first_p - 1.0) * 100.0, 2) if s_first_p > 0 else 0.0
        leg_params = replace(p, initial_cash=hold_alloc)
        indiv = _score_ai_results(
            trades=leg_trades,
            equity_curve=leg_equity_curves[s],
            params=leg_params,
            symbol=s,
            signals_seen=leg_signals_seen[s],
            signals_approved=leg_signals_approved[s],
            start_time=_ts_str(all_stamps[0]),
            end_time=_ts_str(all_stamps[-1]),
            total_bars=len(all_stamps),
            warmup_bars=warmups[s],
            buy_hold_pct=s_bh,
        )
        individual_legs.append(indiv)

    # Overall book score
    book = _score_ai_results(
        trades=all_trades,
        equity_curve=equity_curve,
        params=p,
        symbol="+".join(symbols),
        signals_seen=sum(leg_signals_seen.values()),
        signals_approved=sum(leg_signals_approved.values()),
        start_time=_ts_str(all_stamps[0]),
        end_time=_ts_str(all_stamps[-1]),
        total_bars=len(all_stamps),
        buy_hold_pct=book_buy_hold_pct,
    )
    book["results"] = individual_legs
    book["symbols"] = symbols
    book["run_kind"] = "portfolio"
    return book
