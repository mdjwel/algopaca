"""Real-time market and macro intelligence for Gold and Silver trading.

The composite score here is calibrated, not hand-waved. Every factor was
measured against forward GLD returns over 2007-2025 (Spearman IC, split into
2007-2016 and 2017-2025 halves), and the weights follow that measurement:

    factor    IC vs fwd 20d GLD    weight    note
    rates     +0.086 (both halves) 0.40      falling yields is the real driver
    gsr       +0.108 (both halves) 0.35      high GSR = risk-off bid for metals
    trend     +0.011               0.15      regime participation, not alpha
    dollar    +0.011               0.10      far weaker than folklore claims
    miners    -0.010               0.00      no measurable edge — context only

The previous scoring gave the dollar, yields and miners one equal vote each on
asymmetric thresholds (OR for bullish, AND for bearish). That produced a score
whose buckets were *non-monotonic* in forward returns — it carried no usable
information (IC -0.0007). This version is monotonic: mean forward 20d GLD
return climbs -0.29% -> -0.18% -> +0.53% -> +1.14% -> +1.09% across the five
score buckets, with a +0.110 IC that holds in both halves of the sample.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np
import pandas as pd

from bot.client import AlpacaService
from bot.dollar_tracker import (
    DEFAULT_DOLLAR_SYMBOL,
    DOLLAR_MACRO_BAR_LIMIT,
    DOLLAR_Z_SCALE,
    DOLLAR_Z_WINDOW,
    blend_realtime_dollar_movement,
    classify_dollar_trend,
    compute_dollar_series,
    evaluate_dollar_action,
    fetch_live_dollar_snapshot,
    score_dollar,
)
from bot.econ_calendar import fetch_economic_calendar
from bot.macro_releases import get_active_macro_catalyst

logger = logging.getLogger(__name__)

# Core tradeable precious metals & miners symbols on Alpaca
GOLD_SYMBOLS = {"GLD", "IAU", "UGL", "GLL", "PHYS"}
SILVER_SYMBOLS = {"SLV", "AGQ", "ZSL", "SIL", "SILJ", "PSLV"}
MINER_SYMBOLS = {"GDX", "GDXJ", "GDXD", "GDXU"}
PRECIOUS_METALS = GOLD_SYMBOLS | SILVER_SYMBOLS | MINER_SYMBOLS

# --- Calibrated factor windows and scales (see module docstring) -------------
# The Gold/Silver ratio z-score is far more informative on a 1-year window than
# on the 20-day window used previously (IC +0.108 vs +0.056).
GSR_Z_WINDOW = 250
GSR_Z_FALLBACK_WINDOW = 20
GSR_Z_SCALE = 1.5

# Yield proxy: TLT momentum. 20d and 60d blended — 60d carries the stronger
# signal, 20d keeps the score responsive. Scales are the ~1 sigma move on each.
RATES_FAST_PERIODS = 20
RATES_SLOW_PERIODS = 60
RATES_FAST_SCALE = 0.03
RATES_SLOW_SCALE = 0.06

# Dollar proxy parameters (DOLLAR_Z_WINDOW, DOLLAR_Z_SCALE) are imported from bot.dollar_tracker

# Regime participation filter.
TREND_WINDOW = 200

# Miners ratio is reported for context; it carries no score weight.
MINERS_Z_WINDOW = 60
MINERS_Z_SCALE = 1.5

# Crude oil proxy parameters (USO) - inflation expectation and yield easing tell
OIL_FAST_PERIODS = 20
OIL_SLOW_PERIODS = 60
OIL_FAST_SCALE = 0.08
OIL_SLOW_SCALE = 0.15

FACTOR_WEIGHTS: dict[str, float] = {
    "rates": 0.40,
    "gsr": 0.35,
    "trend": 0.15,
    "dollar": 0.10,
}

# Score is reported on the familiar -3..+3 scale.
SCORE_SCALE = 3.0

# Bucket edges lifted straight from the forward-return study.
_STRONG_BULL = 1.5
_MODERATE_BULL = 0.5
_MODERATE_BEAR = -0.5
_STRONG_BEAR = -1.5

# Bars to request per symbol — enough for the longest window each factor needs.
_METAL_BAR_LIMIT = GSR_Z_WINDOW + 30
_MACRO_BAR_LIMIT = RATES_SLOW_PERIODS + 40

# High-impact macro keywords specifically driving gold & silver volatility
_METALS_MACRO_KEYWORDS = (
    "fomc",
    "fed",
    "interest rate",
    "cpi",
    "inflation",
    "ppi",
    "pce",
    "non-farm",
    "employment",
    "unemployment",
    "powell",
    "yield",
    "treasury",
    "gdp",
)


def is_precious_metal(symbol: str | None) -> bool:
    """Return True if the symbol is a recognized gold, silver, or miners ticker."""
    if not symbol:
        return False
    return symbol.upper().strip() in PRECIOUS_METALS


def get_metal_category(symbol: str | None) -> str:
    """Classify the metal ticker into 'gold', 'silver', 'miners', or 'other'."""
    s = str(symbol or "").upper().strip()
    if s in GOLD_SYMBOLS:
        return "gold"
    if s in SILVER_SYMBOLS:
        return "silver"
    if s in MINER_SYMBOLS:
        return "miners"
    return "other"


def calculate_gsr(price_gold: float, price_silver: float) -> float | None:
    """Calculate the Gold/Silver Ratio (GSR = Price Gold / Price Silver)."""
    if price_gold <= 0 or price_silver <= 0:
        return None
    return round(price_gold / price_silver, 4)


def clip_unit(value: float) -> float:
    """Squash a factor into [-1, +1] so no single input can dominate the score."""
    return max(-1.0, min(1.0, float(value)))


def _closes(bars: pd.DataFrame | None) -> pd.Series | None:
    """Float close series from a bar frame, or None when unusable."""
    if bars is None or not isinstance(bars, pd.DataFrame) or bars.empty:
        return None
    if "close" not in bars.columns:
        return None
    closes = bars["close"].astype(float).dropna()
    return closes if len(closes) >= 2 else None


def zscore(series: pd.Series | None, window: int) -> float | None:
    """Latest z-score over `window`, shrinking the window to the data available."""
    if series is None or len(series) < 3:
        return None
    if not isinstance(series, pd.Series):
        series = pd.Series(series)
    win = min(int(window), len(series))
    if win < 3:
        return None
    mean = float(series.rolling(win).mean().iloc[-1])
    std = float(series.rolling(win).std().iloc[-1])
    if not std or std <= 1e-9 or pd.isna(std) or pd.isna(mean):
        return None
    return float((float(series.iloc[-1]) - mean) / std)


def momentum(series: pd.Series | Sequence[float] | None, periods: int) -> float | None:
    """Fractional return over `periods` bars, shortened when history is thin."""
    if series is None or len(series) < 2:
        return None
    if not isinstance(series, pd.Series):
        series = pd.Series(series)
    step = min(int(periods), len(series) - 1)
    if step < 1:
        return None
    past = float(series.iloc[-1 - step])
    if past <= 0 or pd.isna(past):
        return None
    curr = float(series.iloc[-1])
    if pd.isna(curr):
        return None
    return float(curr / past - 1.0)


def score_rates(tlt_closes: pd.Series | None) -> float | None:
    """Rising TLT means falling yields, the strongest measured gold tailwind."""
    fast = momentum(tlt_closes, RATES_FAST_PERIODS)
    slow = momentum(tlt_closes, RATES_SLOW_PERIODS)
    parts = []
    if fast is not None:
        parts.append(clip_unit(fast / RATES_FAST_SCALE))
    if slow is not None:
        parts.append(clip_unit(slow / RATES_SLOW_SCALE))
    if not parts:
        return None
    return clip_unit(sum(parts) / len(parts))


def score_gsr(gsr_z: float | None) -> float | None:
    """A stretched Gold/Silver ratio marks a risk-off bid across the complex."""
    if gsr_z is None:
        return None
    return clip_unit(gsr_z / GSR_Z_SCALE)


# score_dollar is imported from bot.dollar_tracker


def score_trend(gld_closes: pd.Series | None) -> float | None:
    """Participation gate: is bullion above its long-term moving average."""
    if gld_closes is None or len(gld_closes) < 20:
        return None
    win = min(TREND_WINDOW, len(gld_closes))
    sma = float(gld_closes.rolling(win).mean().iloc[-1])
    if pd.isna(sma) or sma <= 0:
        return None
    return 1.0 if float(gld_closes.iloc[-1]) > sma else -1.0


def combine_macro_score(components: dict[str, float | None]) -> float:
    """Weighted blend of the available factors, rescaled to -3..+3.

    Weights are renormalized over whatever data actually arrived, so a failed
    TLT fetch shrinks the score's confidence rather than dragging it to zero.
    """
    total_weight = 0.0
    weighted = 0.0
    for name, weight in FACTOR_WEIGHTS.items():
        value = components.get(name)
        if value is None:
            continue
        weighted += weight * clip_unit(value)
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    return round((weighted / total_weight) * SCORE_SCALE, 2)


def classify_bias(score: float) -> str:
    """Map the composite score onto the labels the AI playbooks reference."""
    if score >= _STRONG_BULL:
        return "strong_bullish_tailwind"
    if score >= _MODERATE_BULL:
        return "moderate_bullish"
    if score <= _STRONG_BEAR:
        return "strong_bearish_headwind"
    if score <= _MODERATE_BEAR:
        return "moderate_bearish"
    return "neutral"


def score_oil(uso_closes: pd.Series | Sequence[float] | None) -> tuple[float | None, str]:
    """Valuation / momentum score for crude oil (USO proxy).

    Falling crude oil cools inflation expectations and eases Treasury yield pressure,
    acting as a supportive tailwind for precious metals.
    Returns:
        (oil_score, oil_trend)
        oil_score: in [-1.0, 1.0], positive when oil is falling (inflation cooling = supportive for gold)
        oil_trend: 'cooling_inflation', 'heating_inflation', 'neutral', or 'unknown'
    """
    if uso_closes is None or len(uso_closes) < 2:
        return None, "unknown"
    fast = momentum(uso_closes, OIL_FAST_PERIODS)
    slow = momentum(uso_closes, OIL_SLOW_PERIODS)
    parts = []
    if fast is not None:
        parts.append(clip_unit(-fast / OIL_FAST_SCALE))  # inverted: oil drop is positive for gold
    if slow is not None:
        parts.append(clip_unit(-slow / OIL_SLOW_SCALE))
    if not parts:
        return None, "unknown"
    score = clip_unit(sum(parts) / len(parts))
    if score > 0.25:
        trend = "cooling_inflation"
    elif score < -0.25:
        trend = "heating_inflation"
    else:
        trend = "neutral"
    return score, trend


def detect_metals_divergence(
    dollar_score: float | None,
    dollar_trend: str,
    gld_closes: pd.Series | None = None,
    trend_regime: str = "unknown",
) -> dict[str, Any]:
    """Detect divergence between the US Dollar and Gold bullion.

    Case C (Warning from Sept 17/18 macro): DXY rising, but Gold is still
    rising or holding steady above its long-term trend.  A 200-day regime by
    itself is not enough: a stale bull regime can coexist with a fresh gold
    breakdown, where suppressing every short would be incorrect.
    This signifies sovereign central-bank demand, fiscal deficit hedging, or geopolitical bids
    where gold decouples from standard dollar inverse correlation.
    Shorting gold during a bullish decoupling is prohibited.
    """
    dollar_strong = (dollar_trend == "rising") or (dollar_score is not None and dollar_score < -0.25)
    gold_strong = False
    if gld_closes is not None and len(gld_closes) >= 5:
        recent_mom = momentum(gld_closes, min(5, len(gld_closes) - 1))
        # "Holding" permits a flat five-bar move but never a fresh decline.
        # Requiring both the long-term regime and near-term price behaviour
        # keeps this protective rule from masking an actual bearish break.
        gold_strong = bool(
            trend_regime == "bullish_above_sma200"
            and recent_mom is not None
            and recent_mom >= 0.0
        )
    # With no recent gold series, there is no observable decoupling.  Do not
    # infer one from the long-term regime alone.

    divergence_active = bool(dollar_strong and gold_strong)
    return {
        "divergence_active": divergence_active,
        "divergence_type": "bullish_decoupling" if divergence_active else "none",
        "prohibit_shorts": divergence_active,
        "note": (
            "DXY rising while Gold is holding bullish structure (Case C Divergence: sovereign/central-bank demand). "
            "Shorting gold is strictly prohibited during bullish decoupling."
            if divergence_active
            else "Standard macro cross-asset correlation intact."
        ),
    }


def compute_three_confirmation_matrix(
    dollar_score: float | None,
    dollar_trend: str,
    rates_score: float | None,
    yield_trend: str,
    trend_regime: str,
    divergence_active: bool = False,
) -> dict[str, Any]:
    """3-Confirmation Rule for high-probability precious metals trading:

    1. Dollar Structure: Bearish or falling (dollar_trend == 'falling' or dollar_score > 0.15)
    2. Rates / Yields Structure: Falling yields / rising TLT (yield_trend == 'falling_yields' or rates_score > 0.15)
    3. Gold Bullion Structure: Bullish regime (trend_regime == 'bullish_above_sma200')
    """
    conf_dollar = (dollar_trend == "falling") or (dollar_score is not None and dollar_score > 0.15)
    conf_rates = (yield_trend == "falling_yields") or (rates_score is not None and rates_score > 0.15)
    conf_gold = (trend_regime == "bullish_above_sma200")

    count = (1 if conf_dollar else 0) + (1 if conf_rates else 0) + (1 if conf_gold else 0)

    if divergence_active:
        alignment_state = "divergent_warning"
        summary = "DXY and Gold are diverging (Case C); hold longs, prohibit shorts, wait for clear resolution."
    elif count == 3:
        alignment_state = "all_three_aligned"
        summary = "All 3 macro confirmations aligned (DXY down, Yields down, Gold bullish). Prime long setup."
    elif count == 2:
        alignment_state = "moderate_aligned"
        summary = "2 of 3 macro confirmations aligned. Standard pullback setup."
    elif count == 1:
        alignment_state = "partial_unconfirmed"
        summary = "Only 1 macro confirmation aligned. High chop risk; selective entries only."
    else:
        alignment_state = "bearish_aligned" if (dollar_trend == "rising" and yield_trend == "rising_yields") else "unconfirmed"
        summary = "Macro headwinds dominate (rising yields & dollar). Bullish long setups stand aside."

    return {
        "conf_dollar": bool(conf_dollar),
        "conf_rates": bool(conf_rates),
        "conf_gold": bool(conf_gold),
        "confirmations_count": int(count),
        "alignment_state": alignment_state,
        "summary": summary,
    }


def compute_historical_macro_series(
    bars: pd.DataFrame,
    symbol: str = "GLD",
    macro_bars: dict[str, pd.DataFrame] | None = None,
    track_dollar_only: bool = False,
) -> pd.DataFrame:
    """Compute rolling historical macro factors and composite score for each bar.

    Weights calibrated against GLD returns (IC measurements):
        - Rates / Yields (TLT momentum): 40% weight
        - Gold-to-Silver Ratio (GSR z-score): 35% weight
        - 200-day Trend (GLD vs SMA200): 15% weight
        - US Dollar Index (UUP z-score): 10% weight

    Args:
        bars: The primary asset's OHLCV DataFrame (must have DatetimeIndex and 'close').
        symbol: The primary asset symbol (e.g. 'GLD', 'SLV', 'UGL').
        macro_bars: Optional dictionary of companion DataFrames:
            'TLT' (rates / yields), 'UUP' (dollar), 'GLD' (gold benchmark), 'SLV' (silver benchmark).

    Returns:
        pd.DataFrame indexed by bars.index with columns:
            - macro_composite_score (float, -3.0 to +3.0)
            - metals_macro_bias (str)
            - trend_regime (str: 'bullish_above_sma200' or 'bearish_below_sma200')
            - yield_trend (str: 'falling_yields', 'rising_yields', or 'neutral')
            - dollar_trend (str: 'falling', 'rising', or 'neutral')
            - dollar_mixed (bool)
            - gsr_z (float or NaN)
            - rates_score (float or NaN)
            - dollar_score (float or NaN)
            - gsr_score (float or NaN)
            - trend_score (float or NaN)
    """
    if bars is None or bars.empty or "close" not in bars.columns:
        return pd.DataFrame()

    df_index = bars.index
    mb = macro_bars or {}
    sym = (symbol or "GLD").upper().strip()

    # 1. Gold series for 200-day trend
    gld_df = mb.get("GLD")
    if gld_df is not None and not gld_df.empty and "close" in gld_df.columns:
        gld_closes = gld_df["close"].astype(float).reindex(df_index, method="ffill")
    elif sym in GOLD_SYMBOLS or "GLD" in sym:
        gld_closes = bars["close"].astype(float)
    else:
        gld_closes = bars["close"].astype(float)

    # 200-day trend score
    win_trend = min(TREND_WINDOW, max(10, len(gld_closes)))
    sma200 = gld_closes.rolling(win_trend, min_periods=min(15, win_trend)).mean()
    trend_score = pd.Series(np.where(gld_closes >= sma200, 1.0, -1.0), index=df_index)
    trend_regime = pd.Series(
        np.where(trend_score > 0, "bullish_above_sma200", "bearish_below_sma200"),
        index=df_index,
    )

    # 2. Rates score from TLT (rising TLT = falling yields = bullish for gold)
    tlt_df = mb.get("TLT")
    if tlt_df is not None and not tlt_df.empty and "close" in tlt_df.columns:
        tlt_closes = tlt_df["close"].astype(float).reindex(df_index, method="ffill")
        fast_step = min(RATES_FAST_PERIODS, max(2, len(tlt_closes) - 1))
        slow_step = min(RATES_SLOW_PERIODS, max(5, len(tlt_closes) - 1))
        fast_mom = (tlt_closes / tlt_closes.shift(fast_step) - 1.0) / RATES_FAST_SCALE
        slow_mom = (tlt_closes / tlt_closes.shift(slow_step) - 1.0) / RATES_SLOW_SCALE
        fast_clipped = fast_mom.clip(-1.0, 1.0)
        slow_clipped = slow_mom.clip(-1.0, 1.0)
        parts_sum = fast_clipped.fillna(0.0) + slow_clipped.fillna(0.0)
        parts_cnt = (fast_clipped.notna().astype(float) + slow_clipped.notna().astype(float)).replace(0, np.nan)
        rates_score = (parts_sum / parts_cnt).clip(-1.0, 1.0)
    else:
        rates_score = pd.Series(np.nan, index=df_index)

    yield_trend = pd.Series("neutral", index=df_index)
    yield_trend[rates_score > 0.25] = "falling_yields"
    yield_trend[rates_score < -0.25] = "rising_yields"

    # 3. Gold / Silver ratio z-score & score
    slv_df = mb.get("SLV")
    if slv_df is not None and not slv_df.empty and "close" in slv_df.columns:
        slv_closes = slv_df["close"].astype(float).reindex(df_index, method="ffill")
    elif sym in SILVER_SYMBOLS:
        slv_closes = bars["close"].astype(float)
    else:
        slv_closes = None

    if slv_closes is not None and gld_closes is not None:
        raw_gsr = (gld_closes / slv_closes.replace(0, np.nan)).dropna()
        gsr = raw_gsr.reindex(df_index, method="ffill")
        win_gsr = min(GSR_Z_WINDOW, max(15, len(gsr)))
        gsr_mean = gsr.rolling(win_gsr, min_periods=min(10, win_gsr)).mean()
        gsr_std = gsr.rolling(win_gsr, min_periods=min(10, win_gsr)).std().replace(0, np.nan)
        gsr_z = (gsr - gsr_mean) / gsr_std
        gsr_score = (gsr_z / GSR_Z_SCALE).clip(-1.0, 1.0)
    else:
        gsr_z = pd.Series(np.nan, index=df_index)
        gsr_score = pd.Series(np.nan, index=df_index)

    # 4. Dollar score from UUP
    uup_df = mb.get("UUP")
    if uup_df is not None and not uup_df.empty and "close" in uup_df.columns:
        uup_closes = uup_df["close"].astype(float).reindex(df_index, method="ffill")
        win_uup = min(DOLLAR_Z_WINDOW, max(10, len(uup_closes)))
        uup_mean = uup_closes.rolling(win_uup, min_periods=min(8, win_uup)).mean()
        uup_std = uup_closes.rolling(win_uup, min_periods=min(8, win_uup)).std().replace(0, np.nan)
        uup_z = (uup_closes - uup_mean) / uup_std
        dollar_score = (-uup_z / DOLLAR_Z_SCALE).clip(-1.0, 1.0)
    else:
        dollar_score = pd.Series(np.nan, index=df_index)

    dollar_trend = pd.Series("neutral", index=df_index)
    dollar_trend[dollar_score > 0.25] = "falling"
    dollar_trend[dollar_score < -0.25] = "rising"
    dollar_mixed = (dollar_trend == "neutral") | (dollar_score.abs() <= 0.25) | dollar_score.isna()

    if track_dollar_only:
        macro_score = np.round(dollar_score.fillna(0.0).clip(-1.0, 1.0) * SCORE_SCALE, 2)
        metals_bias = [classify_bias(float(s)) for s in macro_score]
    else:
        # 5. Composite macro score combining all available factors
        # Weights: rates: 0.40, gsr: 0.35, trend: 0.15, dollar: 0.10
        w_rates = FACTOR_WEIGHTS["rates"]
        w_gsr = FACTOR_WEIGHTS["gsr"]
        w_trend = FACTOR_WEIGHTS["trend"]
        w_dollar = FACTOR_WEIGHTS["dollar"]

        has_rates = rates_score.notna()
        has_gsr = gsr_score.notna()
        has_trend = trend_score.notna()
        has_dollar = dollar_score.notna()

        total_w = (
            has_rates.astype(float) * w_rates
            + has_gsr.astype(float) * w_gsr
            + has_trend.astype(float) * w_trend
            + has_dollar.astype(float) * w_dollar
        ).replace(0, np.nan)

        weighted_val = (
            rates_score.fillna(0.0) * w_rates
            + gsr_score.fillna(0.0) * w_gsr
            + trend_score.fillna(0.0) * w_trend
            + dollar_score.fillna(0.0) * w_dollar
        )
        macro_score = np.round((weighted_val / total_w).fillna(0.0) * SCORE_SCALE, 2)
        metals_bias = [classify_bias(float(s)) for s in macro_score]

    # 5. Oil score from USO
    uso_df = mb.get("USO")
    if uso_df is not None and not uso_df.empty and "close" in uso_df.columns:
        uso_closes = uso_df["close"].astype(float).reindex(df_index, method="ffill")
        fast_step_oil = min(OIL_FAST_PERIODS, max(2, len(uso_closes) - 1))
        slow_step_oil = min(OIL_SLOW_PERIODS, max(5, len(uso_closes) - 1))
        fast_mom_oil = (uso_closes / uso_closes.shift(fast_step_oil) - 1.0) / OIL_FAST_SCALE
        slow_mom_oil = (uso_closes / uso_closes.shift(slow_step_oil) - 1.0) / OIL_SLOW_SCALE
        fast_clip_oil = (-fast_mom_oil).clip(-1.0, 1.0)
        slow_clip_oil = (-slow_mom_oil).clip(-1.0, 1.0)
        oil_sum = fast_clip_oil.fillna(0.0) + slow_clip_oil.fillna(0.0)
        oil_cnt = (fast_clip_oil.notna().astype(float) + slow_clip_oil.notna().astype(float)).replace(0, np.nan)
        oil_score = (oil_sum / oil_cnt).clip(-1.0, 1.0)
    else:
        oil_score = pd.Series(np.nan, index=df_index)

    oil_trend = pd.Series("neutral", index=df_index)
    oil_trend[oil_score > 0.25] = "cooling_inflation"
    oil_trend[oil_score < -0.25] = "heating_inflation"

    # 6. Divergence and 3-Confirmation Matrix.  As with the real-time
    # detector, a bullish 200-day regime alone is not a DXY/Gold divergence;
    # bullion must also be holding or rising over the recent five bars.
    dollar_strong = (dollar_trend == "rising") | (dollar_score < -0.25)
    gold_momentum_5 = gld_closes / gld_closes.shift(5) - 1.0
    gold_strong = (trend_regime == "bullish_above_sma200") & (gold_momentum_5 >= 0.0)
    gold_dollar_divergence = dollar_strong & gold_strong

    conf_d = (dollar_trend == "falling") | (dollar_score > 0.15)
    conf_r = (yield_trend == "falling_yields") | (rates_score > 0.15)
    conf_g = (trend_regime == "bullish_above_sma200")
    confirmations_count = conf_d.astype(int) + conf_r.astype(int) + conf_g.astype(int)

    three_confirmation_state = pd.Series("unconfirmed", index=df_index)
    three_confirmation_state[confirmations_count == 1] = "partial_unconfirmed"
    three_confirmation_state[confirmations_count == 2] = "moderate_aligned"
    three_confirmation_state[confirmations_count == 3] = "all_three_aligned"
    three_confirmation_state[gold_dollar_divergence] = "divergent_warning"

    return pd.DataFrame(
        {
            "macro_composite_score": macro_score,
            "metals_macro_bias": metals_bias,
            "trend_regime": trend_regime,
            "yield_trend": yield_trend,
            "dollar_trend": dollar_trend,
            "dollar_mixed": dollar_mixed,
            "gsr_z": gsr_z.round(2),
            "rates_score": rates_score.round(3),
            "dollar_score": dollar_score.round(3),
            "gsr_score": gsr_score.round(3),
            "trend_score": trend_score.round(3),
            "oil_score": oil_score.round(3),
            "oil_trend": oil_trend,
            "gold_dollar_divergence": gold_dollar_divergence,
            "confirmations_count": confirmations_count,
            "three_confirmation_state": three_confirmation_state,
        },
        index=df_index,
    )


def _fetch_closes(
    service: AlpacaService, symbol: str, limit: int
) -> tuple[pd.DataFrame | None, pd.Series | None]:
    """Bars plus their close series; never raises, so one bad symbol can't
    take down the whole context build."""
    try:
        bars = service.get_bars(symbol, limit=limit, timeframe="1Day")
    except Exception as exc:
        logger.debug("bars unavailable for %s: %s", symbol, exc)
        return None, None
    return bars, _closes(bars)


def fetch_metals_macro_context(
    service: AlpacaService,
    symbol: str,
    calendar: list[dict[str, Any]] | None = None,
    track_dollar_only: bool = False,
) -> dict[str, Any]:
    """Build real-time precious metals analytics including GSR and macro catalyst checks."""
    sym = symbol.upper().strip()
    gold_sym = "GLD"
    silver_sym = "SLV"

    # 1. Fetch live mark prices for GLD, SLV, and UUP (Dollar Index proxy)
    gld_price = 0.0
    slv_price = 0.0
    uup_price = 0.0
    try:
        gld_mark = service.get_mark_price(gold_sym)
        if isinstance(gld_mark.get("price"), (int, float)):
            gld_price = float(gld_mark["price"])
    except Exception as exc:
        logger.debug("Failed to fetch GLD mark: %s", exc)

    try:
        slv_mark = service.get_mark_price(silver_sym)
        if isinstance(slv_mark.get("price"), (int, float)):
            slv_price = float(slv_mark["price"])
    except Exception as exc:
        logger.debug("Failed to fetch SLV mark: %s", exc)

    dollar_snap = fetch_live_dollar_snapshot(
        service, DEFAULT_DOLLAR_SYMBOL, track_dollar_only=track_dollar_only, limit=_MACRO_BAR_LIMIT
    )
    uup_price = dollar_snap["price"]
    uup_change_pct = dollar_snap["change_pct"]

    # 2. Compute live GSR
    live_gsr = calculate_gsr(gld_price, slv_price)

    # 3. Daily history for the factor windows
    gld_bars, gld_close = _fetch_closes(service, gold_sym, _METAL_BAR_LIMIT)
    _, slv_close = _fetch_closes(service, silver_sym, _METAL_BAR_LIMIT)
    _, tlt_close = _fetch_closes(service, "TLT", _MACRO_BAR_LIMIT)
    _, gdx_close = _fetch_closes(service, "GDX", _MACRO_BAR_LIMIT)

    # Crude oil (USO proxy) for inflation expectations and yield easing tell
    uso_price = 0.0
    try:
        uso_mark = service.get_mark_price("USO")
        if isinstance(uso_mark.get("price"), (int, float)):
            uso_price = float(uso_mark["price"])
    except Exception as exc:
        logger.debug("Failed to fetch USO mark: %s", exc)

    _, uso_close = _fetch_closes(service, "USO", _MACRO_BAR_LIMIT)
    oil_score, oil_trend = score_oil(uso_close)

    # 4. Gold/Silver ratio history and relative valuation
    gsr_sma: float | None = None
    gsr_std: float | None = None
    gsr_z_score: float | None = None
    gsr_z_window: int | None = None
    relative_valuation = "neutral_range"
    valuation_note = "Gold and silver are trading in standard equilibrium."

    if gld_close is not None and slv_close is not None:
        try:
            common_idx = gld_close.index.intersection(slv_close.index)
            if len(common_idx) >= 15:
                hist_gsr = gld_close.loc[common_idx] / slv_close.loc[common_idx]
                # Prefer the 1-year window; fall back to 20d when history is thin.
                window = (
                    GSR_Z_WINDOW
                    if len(common_idx) >= GSR_Z_WINDOW
                    else min(GSR_Z_FALLBACK_WINDOW, len(common_idx))
                )
                gsr_z_window = min(window, len(common_idx))
                mean = float(hist_gsr.rolling(gsr_z_window).mean().iloc[-1])
                std = float(hist_gsr.rolling(gsr_z_window).std().iloc[-1])
                reference = live_gsr if live_gsr is not None else float(hist_gsr.iloc[-1])
                if std > 1e-6 and reference is not None:
                    gsr_sma = round(mean, 2)
                    gsr_std = round(std, 2)
                    gsr_z_score = round(float((reference - mean) / std), 2)

                    if gsr_z_score >= 1.2:
                        relative_valuation = "silver_undervalued"
                        valuation_note = (
                            f"Gold/Silver ratio ({reference:.2f}) is +{gsr_z_score:.1f}σ above its "
                            f"{gsr_z_window}d mean ({mean:.2f}). Silver is historically cheap against "
                            "gold, and a stretched ratio has historically marked a risk-off bid that "
                            "lifted the whole complex — the strongest single bullish factor measured."
                        )
                    elif gsr_z_score <= -1.2:
                        relative_valuation = "gold_undervalued"
                        valuation_note = (
                            f"Gold/Silver ratio ({reference:.2f}) is {gsr_z_score:.1f}σ below its "
                            f"{gsr_z_window}d mean ({mean:.2f}). Gold is historically cheap against "
                            "silver, but a compressed ratio is a risk-on tell and has historically "
                            "preceded below-average bullion returns."
                        )
        except Exception as exc:
            logger.debug("Failed computing historical GSR: %s", exc)

    # 5. Factor scores
    rates_score = score_rates(tlt_close)
    gsr_score = score_gsr(gsr_z_score)
    dollar_z = dollar_snap["dollar_z"]
    dollar_score = dollar_snap["dollar_score"]
    trend_score = score_trend(gld_close)

    # 6. Miners ratio — reported as context only. Measured IC was -0.01, so it
    # carries no weight in the score despite the folklore about miners leading.
    gdx_gld_ratio: float | None = None
    miners_signal = "unknown"
    miners_z: float | None = None
    if gdx_close is not None and gld_close is not None:
        try:
            common_m = gdx_close.index.intersection(gld_close.index)
            if len(common_m) >= 10:
                ratio_series = gdx_close.loc[common_m] / gld_close.loc[common_m]
                gdx_gld_ratio = round(float(ratio_series.iloc[-1]), 4)
                miners_z = zscore(ratio_series, MINERS_Z_WINDOW)
                if miners_z is None:
                    miners_signal = "neutral"
                elif miners_z > 0.5:
                    miners_signal = "miners_outperforming"
                elif miners_z < -0.5:
                    miners_signal = "miners_lagging"
                else:
                    miners_signal = "neutral"
        except Exception as exc:
            logger.debug("Failed computing Miners ratio metrics: %s", exc)

    if track_dollar_only:
        # Driven 100% by the US Dollar Index. Rates, GSR, and Trend are bypassed.
        macro_composite_score = round(clip_unit(dollar_score if dollar_score is not None else 0.0) * SCORE_SCALE, 2)
        metals_macro_bias = classify_bias(macro_composite_score)
    else:
        macro_composite_score = combine_macro_score(
            {
                "rates": rates_score,
                "gsr": gsr_score,
                "trend": trend_score,
                "dollar": dollar_score,
            }
        )
        metals_macro_bias = classify_bias(macro_composite_score)

    # Human-readable trend labels kept for the prompt and the UI.
    dollar_trend = dollar_snap["dollar_trend"]
    dollar_mixed = dollar_snap["dollar_mixed"]

    if rates_score is None:
        yield_trend = "unknown"
    elif rates_score > 0.25:
        yield_trend = "falling_yields"
    elif rates_score < -0.25:
        yield_trend = "rising_yields"
    else:
        yield_trend = "neutral"

    trend_regime = (
        "unknown" if trend_score is None
        else ("bullish_above_sma200" if trend_score > 0 else "bearish_below_sma200")
    )

    # 7. Filter Macro Events relevant to Precious Metals (Bypassed in Track Dollar Only mode)
    relevant_events: list[dict[str, Any]] = []
    imminent_risk = False
    events_5m_imminent: list[dict[str, Any]] = []
    active_catalyst: dict[str, Any] | None = None

    if not track_dollar_only:
        cal = (
            calendar
            if calendar is not None
            else fetch_economic_calendar(hours_ahead=48, hours_behind=8)
        )
        now_utc = datetime.now(timezone.utc)

        for ev in cal:
            title_lower = str(ev.get("title") or "").lower()
            impact = str(ev.get("impact") or "Low")
            if any(kw in title_lower for kw in _METALS_MACRO_KEYWORDS) or impact == "High":
                event_copy = dict(ev)
                when_utc_str = ev.get("when_utc")
                if when_utc_str:
                    try:
                        when_dt = datetime.fromisoformat(str(when_utc_str).replace("Z", "+00:00"))
                        if when_dt.tzinfo is None:
                            when_dt = when_dt.replace(tzinfo=timezone.utc)
                        minutes_diff = (when_dt - now_utc).total_seconds() / 60.0
                        event_copy["minutes_away"] = round(minutes_diff, 1)
                        # Only unreleased events in the near future constitute imminent surprise risk
                        is_unreleased = (
                            not ev.get("released")
                            and not str(ev.get("actual") or "").strip()
                            and not ev.get("rate_already_released")
                        )
                        if is_unreleased and 0.0 < minutes_diff <= 45.0 and impact == "High":
                            imminent_risk = True
                        if is_unreleased and 0.0 < minutes_diff <= 5.0:
                            events_5m_imminent.append(event_copy)
                    except Exception:
                        pass
                relevant_events.append(event_copy)

        # Check for active recently released macro catalyst (e.g. FOMC rate decision today)
        active_catalyst = get_active_macro_catalyst(relevant_events, now_utc=now_utc)
        if active_catalyst:
            action = str(active_catalyst.get("action") or "").lower()
            if action == "hike":
                # A policy hike is not automatically a bearish market move.
                # Classify its *observed* cross-asset reaction, rather than
                # manufacturing a rising-yield / stronger-dollar signal when
                # either data feed is missing or the reaction is mixed.
                is_rebound = (
                    (rates_score is not None and rates_score > 0.10)
                    or (rates_score is not None and rates_score > 0.0 and dollar_score is not None and dollar_score > 0.10)
                )
                is_hawkish_follow_through = (
                    rates_score is not None
                    and rates_score < -0.10
                    and dollar_score is not None
                    and dollar_score < -0.10
                )
                if is_rebound:
                    active_catalyst["absorbed_rebound"] = True
                    active_catalyst["market_reaction"] = "hike_absorbed_yields_falling"
                    if rates_score is not None and rates_score > 0:
                        yield_trend = "falling_yields"
                elif is_hawkish_follow_through:
                    active_catalyst["absorbed_rebound"] = False
                    active_catalyst["market_reaction"] = "hawkish_follow_through"
                    yield_trend = "rising_yields"
                    dollar_trend = "rising"
                    dollar_mixed = False
                else:
                    active_catalyst["absorbed_rebound"] = False
                    active_catalyst["market_reaction"] = "awaiting_cross_asset_confirmation"
            elif action == "cut":
                rates_score = max(rates_score if rates_score is not None else 0.6, 0.6)
                yield_trend = "falling_yields"
                dollar_trend = "falling"
                dollar_score = max(dollar_score if dollar_score is not None else 0.5, 0.5)
                dollar_mixed = False

            # Recombine macro score incorporating real-time catalyst impact
            macro_composite_score = combine_macro_score(
                {
                    "rates": rates_score,
                    "gsr": gsr_score,
                    "trend": trend_score,
                    "dollar": dollar_score,
                }
            )
            metals_macro_bias = classify_bias(macro_composite_score)

    # 8. Divergence Detection & 3-Confirmation Matrix
    divergence = detect_metals_divergence(
        dollar_score=dollar_score,
        dollar_trend=dollar_trend,
        gld_closes=gld_close,
        trend_regime=trend_regime,
    )
    gold_dollar_divergence = bool(divergence["divergence_active"])

    three_conf = compute_three_confirmation_matrix(
        dollar_score=dollar_score,
        dollar_trend=dollar_trend,
        rates_score=rates_score,
        yield_trend=yield_trend,
        trend_regime=trend_regime,
        divergence_active=gold_dollar_divergence,
    )

    if track_dollar_only:
        macro_risk_level = "normal"
        factor_weights = {"dollar": 1.0}
        factor_scores = {
            "rates": None,
            "gsr": None,
            "trend": None,
            "dollar": None if dollar_score is None else round(dollar_score, 3),
            "oil": None,
            "miners_unweighted": None,
        }
    else:
        factor_weights = dict(FACTOR_WEIGHTS)
        factor_scores = {
            "rates": None if rates_score is None else round(rates_score, 3),
            "gsr": None if gsr_score is None else round(gsr_score, 3),
            "trend": None if trend_score is None else round(trend_score, 3),
            "dollar": None if dollar_score is None else round(dollar_score, 3),
            "oil": None if oil_score is None else round(oil_score, 3),
            "miners_unweighted": None if miners_z is None else round(clip_unit(miners_z / MINERS_Z_SCALE), 3),
        }
        if imminent_risk:
            macro_risk_level = "imminent_release"
        elif active_catalyst:
            macro_risk_level = "post_release_catalyst"
        elif relevant_events:
            macro_risk_level = "elevated"
        else:
            macro_risk_level = "normal"

    if track_dollar_only:
        is_reversal = bool(dollar_snap["short_reversal_to_long"])
        reversal_reason = dollar_snap["short_reversal_reason"]
    else:
        is_reversal = bool(
            (dollar_mixed or gold_dollar_divergence)
            and not (active_catalyst and active_catalyst.get("action") == "hike" and not active_catalyst.get("absorbed_rebound"))
        )
        reversal_reason = (
            "DXY-Gold bullish divergence active (central-bank demand decoupling); close short immediately"
            if gold_dollar_divergence
            else (
                "US Dollar Index momentum / economic data is mixed (neutral); close short and reverse to long"
                if is_reversal
                else None
            )
        )

    dollar_sig = dollar_snap["dollar_signal"]
    dollar_act = dollar_snap["dollar_action"]

    return {
        "is_precious_metal": True,
        "symbol": sym,
        "category": get_metal_category(sym),
        "primary_gold_ticker": gold_sym,
        "primary_silver_ticker": silver_sym,
        "price_gld": gld_price,
        "price_slv": slv_price,
        "gsr_live": live_gsr,
        "gsr_sma20": gsr_sma,
        "gsr_std20": gsr_std,
        "gsr_z_score": gsr_z_score,
        "gsr_z_window": gsr_z_window,
        "relative_valuation": relative_valuation,
        "valuation_note": valuation_note,
        "dollar_trend": dollar_trend,
        "dollar_mixed": dollar_mixed,
        "dollar_economic_data_status": "mixed" if dollar_mixed else dollar_trend,
        "short_reversal_to_long": is_reversal,
        "short_reversal_reason": reversal_reason,
        "yield_trend": yield_trend,
        "trend_regime": trend_regime,
        "miners_signal": miners_signal,
        "gdx_gld_ratio": gdx_gld_ratio,
        "factor_scores": factor_scores,
        "factor_weights": factor_weights,
        "macro_composite_score": macro_composite_score,
        "metals_macro_bias": metals_macro_bias,
        "macro_risk_level": macro_risk_level,
        "active_catalyst": active_catalyst,
        "relevant_macro_events": relevant_events[:5],
        "events_5m_imminent": events_5m_imminent,
        "track_dollar_only": track_dollar_only,
        "dollar_only_mode": track_dollar_only,
        "dollar_live_price": uup_price,
        "dollar_change_pct": uup_change_pct,
        "dollar_signal": dollar_sig,
        "dollar_action": dollar_act,
        "oil_score": oil_score,
        "oil_trend": oil_trend,
        "oil_live_price": uso_price,
        "gold_dollar_divergence": gold_dollar_divergence,
        "divergence_details": divergence,
        "three_confirmation": three_conf,
        "three_confirmation_state": three_conf["alignment_state"],
        "confirmations_count": three_conf["confirmations_count"],
    }


def check_imminent_economic_events(
    calendar: list[dict[str, Any]] | None = None,
    window_minutes: float = 5.0,
) -> list[dict[str, Any]]:
    """Return high-impact economic events occurring within `window_minutes` from now."""
    cal = (
        calendar
        if calendar is not None
        else fetch_economic_calendar(hours_ahead=12, hours_behind=2)
    )
    imminent: list[dict[str, Any]] = []
    now_utc = datetime.now(timezone.utc)
    for ev in cal:
        impact = str(ev.get("impact") or "Low")
        title_lower = str(ev.get("title") or "").lower()
        is_relevant = (impact == "High") or any(kw in title_lower for kw in _METALS_MACRO_KEYWORDS)
        if not is_relevant:
            continue
        when_utc_str = ev.get("when_utc")
        if not when_utc_str:
            continue
        # Skip events that are already released or whose primary rate decision is already out
        if (ev.get("released") and str(ev.get("actual") or "").strip()) or ev.get("rate_already_released"):
            continue
        try:
            when_dt = datetime.fromisoformat(str(when_utc_str).replace("Z", "+00:00"))
            if when_dt.tzinfo is None:
                when_dt = when_dt.replace(tzinfo=timezone.utc)
            minutes_diff = (when_dt - now_utc).total_seconds() / 60.0
            if 0.0 < minutes_diff <= float(window_minutes):
                ev_copy = dict(ev)
                ev_copy["minutes_away"] = round(minutes_diff, 1)
                imminent.append(ev_copy)
        except Exception:
            continue
    imminent.sort(key=lambda x: x.get("minutes_away", 999))
    return imminent


def calculate_event_stop_limit_prices(
    current_price: float,
    side: str = "long",
    stop_buffer_pct: float = 0.8,
    limit_offset_pct: float = 0.5,
) -> tuple[float, float]:
    """Calculate tight protective stop and limit prices based on current price.

    Args:
        current_price: Current mark/live price of the asset.
        side: 'long' or 'short'.
        stop_buffer_pct: Percent away from current price to set the stop (default 0.8%).
        limit_offset_pct: Percent beyond the stop price to set the limit order (default 0.5%).

    Returns:
        (stop_price, limit_price) rounded to 2 decimal places.
    """
    price = float(current_price)
    if price <= 0:
        return 0.0, 0.0

    side_str = str(side).strip().lower()
    is_short = side_str in {"short", "sell_short"}
    if is_short:
        # For short position, stop price is ABOVE current price to cut losses on upward spikes
        stop = price * (1.0 + stop_buffer_pct / 100.0)
        limit = stop * (1.0 + limit_offset_pct / 100.0)
    else:
        # For long position, stop price is BELOW current price to cut losses on sudden drops
        stop = price * (1.0 - stop_buffer_pct / 100.0)
        limit = stop * (1.0 - limit_offset_pct / 100.0)

    return round(stop, 2), round(limit, 2)


def protect_metals_position_before_event(
    service: Any,
    symbol: str,
    event: dict[str, Any],
    *,
    stop_buffer_pct: float = 0.8,
    limit_offset_pct: float = 0.5,
    synthetic_handler: Any = None,
    reversal_buy: bool = True,
    track_dollar_only: bool = False,
) -> dict[str, Any] | None:
    """Set or tighten a protective Stop-Limit order 5 minutes before an economic event.

    Args:
        service: AlpacaService instance.
        symbol: Precious metal ticker (e.g. GLD, SLV, IAU, UGL, GDX).
        event: Economic calendar event dictionary.
        stop_buffer_pct: Distance in % below/above current price.
        limit_offset_pct: Distance in % beyond stop price for limit.
        synthetic_handler: Optional synthetic order handler (web_state) for 24h extended-hours execution.
        reversal_buy: If True and position is short, automatically reverse into a buy position when stop-loss is hit.
        track_dollar_only: If True, news and economic events are bypassed; returns None.

    Returns:
        Dict with protection details or None if no action taken.
    """
    if track_dollar_only:
        return None

    sym = str(symbol).upper().strip()
    if not is_precious_metal(sym):
        return None

    try:
        pos_qty = float(service.get_position_qty(sym) or 0.0)
    except Exception as exc:
        logger.warning("Could not read position qty for %s: %s", sym, exc)
        return None

    if pos_qty == 0.0:
        return None

    is_short = pos_qty < 0
    side = "short" if is_short else "long"

    try:
        mark = service.get_mark_price(sym)
        current_price = float(mark.get("price") or 0.0)
    except Exception as exc:
        logger.warning("Could not read mark price for %s: %s", sym, exc)
        return None

    if current_price <= 0.0:
        return None

    target_stop, target_limit = calculate_event_stop_limit_prices(
        current_price=current_price,
        side=side,
        stop_buffer_pct=stop_buffer_pct,
        limit_offset_pct=limit_offset_pct,
    )

    if target_stop <= 0 or target_limit <= 0:
        return None

    # Check current resting stop on Alpaca
    try:
        current_stop = service.current_stop_price(sym)
    except Exception:
        current_stop = None

    should_update = False

    if current_stop is None or current_stop <= 0:
        should_update = True
    elif not is_short:
        # Long position: tighten stop upward toward current price.
        # If existing stop is looser (below target_stop), tighten it up to target_stop!
        # If existing stop is already higher than target_stop (locked in profit), keep the higher stop.
        if current_stop < target_stop:
            should_update = True
    else:
        # Short position: tighten stop downward toward current price.
        # If existing stop is looser (above target_stop), tighten it down to target_stop!
        if current_stop > target_stop:
            should_update = True

    event_title = str(event.get("title") or "Economic Event")
    minutes_away = event.get("minutes_away", 5.0)

    armed_info: dict[str, Any] = {
        "symbol": sym,
        "side": side,
        "qty": abs(pos_qty),
        "current_price": current_price,
        "stop_price": target_stop,
        "limit_price": target_limit,
        "event_title": event_title,
        "minutes_away": minutes_away,
        "tightened_from": current_stop,
        "action_taken": "updated" if should_update else "maintained_existing_tighter",
        "reversal_buy": bool(reversal_buy and is_short),
    }
    if reversal_buy and is_short:
        armed_info["reversal_qty"] = abs(pos_qty)
        armed_info["reversal_event_title"] = event_title

    if should_update:
        # 1. Attempt native Alpaca stop replacement (works if market is in regular hours)
        try:
            replace_res = service.replace_stop_loss(sym, target_stop)
            if replace_res:
                armed_info["alpaca_order_id"] = replace_res.get("id")
        except Exception as exc:
            logger.debug("Native stop replacement for %s skipped or failed: %s", sym, exc)

    # 2. Always register / sync with synthetic 24h stop-limit watcher!
    # This is CRUCIAL because 8:30 AM ET releases (CPI, NFP) occur in pre-market
    # where Alpaca rejects native stop orders.
    handler = synthetic_handler
    if handler is None and hasattr(service, "_get_synthetic_handler"):
        handler = service._get_synthetic_handler()
    if handler and hasattr(handler, "sync_strategy_stop"):
        try:
            synth = handler.sync_strategy_stop(
                symbol=sym,
                side="buy" if is_short else "sell",
                qty=abs(pos_qty),
                stop_price=target_stop,
                limit_price=target_limit,
                source=f"event_5m_{event_title[:20]}",
                reversal_buy=bool(reversal_buy and is_short),
                reversal_qty=abs(pos_qty) if (reversal_buy and is_short) else None,
                reversal_event_title=event_title if (reversal_buy and is_short) else None,
            )
            if synth:
                armed_info["synthetic_order_id"] = synth.get("id")
        except Exception as exc:
            logger.warning("Failed to sync synthetic event stop for %s: %s", sym, exc)

    logger.info(
        "Event Protection armed for %s (%s) 5m before '%s': Stop @ $%.2f, Limit @ $%.2f (mark $%.2f)",
        sym,
        side,
        event_title,
        target_stop,
        target_limit,
        current_price,
    )
    return armed_info
