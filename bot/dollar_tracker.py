"""Real-time US Dollar Index (UUP / DXY proxy) tracking and macro analytics.

Provides dedicated real-time mark tracking, intraday percentage change computation,
z-score valuation scoring, trend classification, and actionable signal/reversal generation
for algorithmic trading across precious metals and macro strategies.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from bot.client import AlpacaService

logger = logging.getLogger(__name__)

# Default dollar proxy ticker. Invesco DB US Dollar Index Bullish Fund tracks
# the Deutsche Bank Short US Dollar Index Futures Index — closely tracking ICE DXY.
DEFAULT_DOLLAR_SYMBOL: str = "UUP"

# Factor window parameters matching empirical forward-return studies.
DOLLAR_Z_WINDOW: int = 60
DOLLAR_Z_SCALE: float = 1.5
DOLLAR_MACRO_BAR_LIMIT: int = 100
SCORE_SCALE: float = 3.0


def clip_unit(value: float) -> float:
    """Squash a factor score into [-1.0, +1.0]."""
    try:
        val = float(value)
        return max(-1.0, min(1.0, val))
    except (TypeError, ValueError):
        return 0.0


def zscore(series: pd.Series | None, window: int) -> float | None:
    """Latest z-score over `window`, shrinking window when history is thin."""
    if series is None or len(series) < 3:
        return None
    win = min(int(window), len(series))
    if win < 3:
        return None
    try:
        mean = float(series.rolling(win).mean().iloc[-1])
        std = float(series.rolling(win).std().iloc[-1])
        if not std or std <= 1e-9 or pd.isna(std) or pd.isna(mean):
            return None
        return float((float(series.iloc[-1]) - mean) / std)
    except Exception as exc:
        logger.debug("Dollar tracker zscore calculation failed: %s", exc)
        return None


def score_dollar(uup_z: float | None) -> float | None:
    """Valuation score for the US Dollar.

    A weaker/cheap dollar lifts commodities and metals (sign is inverted).
    Returns value in [-1.0, +1.0] where:
      > 0: dollar is cheap / falling (bullish for metals & commodities)
      < 0: dollar is expensive / rising (bearish for metals & commodities)
    """
    if uup_z is None:
        return None
    return clip_unit(-uup_z / DOLLAR_Z_SCALE)


def blend_realtime_dollar_movement(
    base_score: float | None,
    change_pct: float,
) -> float:
    """Blend real-time intraday dollar percentage change into the dollar score.

    Ensures sub-second responsiveness to intraday dollar fluctuations
    when real-time tracking is active.
    """
    if base_score is None:
        return clip_unit(-change_pct / 0.5)

    score = base_score
    if change_pct <= -0.10:
        score = max(score, 0.45)
    elif change_pct >= 0.10:
        score = min(score, -0.45)
    return clip_unit(score)


def classify_dollar_trend(dollar_score: float | None) -> tuple[str, bool]:
    """Classify the dollar trend into human-readable label and mixed flag.

    Returns:
        (trend_label, is_mixed)
        trend_label in {"falling", "rising", "neutral", "unknown"}
    """
    if dollar_score is None:
        return "unknown", True
    if dollar_score > 0.25:
        return "falling", False
    if dollar_score < -0.25:
        return "rising", False
    return "neutral", True


def evaluate_dollar_action(
    dollar_score: float | None,
    trend: str,
    is_mixed: bool,
    *,
    track_dollar_only: bool = False,
) -> tuple[str, str, bool, str | None]:
    """Determine dollar signal, action, and reversal state for trading execution.

    Returns:
        (dollar_signal, dollar_action, is_reversal, reversal_reason)
        - dollar_signal: 'bullish' | 'bearish' | 'neutral' (relative to gold/commodities)
        - dollar_action: 'buy' | 'sell' | 'hold'
        - is_reversal: bool (True if existing short positions should immediately reverse to long)
        - reversal_reason: str | None
    """
    eff_score = dollar_score if dollar_score is not None else 0.0

    if eff_score > 0.15:
        signal = "bullish"
        action = "buy"
    elif eff_score < -0.15:
        signal = "bearish"
        action = "sell"
    else:
        signal = "neutral"
        action = "hold"

    if track_dollar_only:
        # In real-time dollar-only mode, if dollar is falling or mixed, reverse any short position immediately
        is_reversal = bool(trend == "falling" or eff_score > 0.15 or is_mixed)
        reversal_reason = (
            "Real-time US Dollar Index is falling / weakening; close short and reverse to long immediately"
            if is_reversal
            else None
        )
    else:
        is_reversal = bool(is_mixed)
        reversal_reason = (
            "US Dollar Index momentum / economic data is mixed (neutral); close short and reverse to long"
            if is_reversal
            else None
        )

    return signal, action, is_reversal, reversal_reason


def compute_dollar_series(
    uup_closes: pd.Series,
    window: int = DOLLAR_Z_WINDOW,
) -> pd.Series:
    """Compute rolling dollar scores over a close price series for backtesting."""
    if uup_closes is None or len(uup_closes) < 10:
        return pd.Series(dtype=float)

    win = min(int(window), max(10, len(uup_closes)))
    roll_mean = uup_closes.rolling(win).mean()
    roll_std = uup_closes.rolling(win).std().replace(0, float("nan"))
    uup_z = (uup_closes - roll_mean) / roll_std
    return (-uup_z / DOLLAR_Z_SCALE).clip(-1.0, 1.0).fillna(0.0)


def fetch_live_dollar_snapshot(
    service: AlpacaService,
    symbol: str = DEFAULT_DOLLAR_SYMBOL,
    *,
    track_dollar_only: bool = False,
    limit: int = DOLLAR_MACRO_BAR_LIMIT,
) -> dict[str, Any]:
    """Fetch complete real-time dollar state including live mark, change %, z-score,
    trend, and execution signals.

    This function never raises — it gracefully falls back so macro calculations
    can continue even during feed blips.
    """
    sym = symbol.upper().strip()
    live_price = 0.0
    mark_payload: dict[str, Any] = {}

    try:
        mark = service.get_mark_price(sym)
        if isinstance(mark, dict):
            mark_payload = mark
            if isinstance(mark.get("price"), (int, float)):
                live_price = float(mark["price"])
    except Exception as exc:
        logger.debug("Failed to fetch %s live mark: %s", sym, exc)

    # Daily bars for rolling z-score and previous close comparison
    uup_close: pd.Series | None = None
    try:
        bars = service.get_bars(sym, limit=limit, timeframe="1Day")
        if bars is not None and isinstance(bars, pd.DataFrame) and not bars.empty and "close" in bars.columns:
            closes = bars["close"].astype(float).dropna()
            if len(closes) >= 2:
                uup_close = closes
    except Exception as exc:
        logger.debug("Failed to fetch %s bars: %s", sym, exc)

    # Intraday percentage change from previous day's close
    change_pct = 0.0
    prev_close: float | None = None
    if uup_close is not None and len(uup_close) >= 2:
        prev_close = float(uup_close.iloc[-2])
        if prev_close > 0 and live_price > 0:
            change_pct = round(((live_price / prev_close) - 1.0) * 100.0, 3)

    dollar_z = zscore(uup_close, DOLLAR_Z_WINDOW)
    base_score = score_dollar(dollar_z)

    if track_dollar_only and change_pct != 0.0 and abs(change_pct) <= 20.0:
        dollar_score = blend_realtime_dollar_movement(base_score, change_pct)
    else:
        dollar_score = base_score

    trend, is_mixed = classify_dollar_trend(dollar_score)
    signal, action, is_reversal, reversal_reason = evaluate_dollar_action(
        dollar_score, trend, is_mixed, track_dollar_only=track_dollar_only
    )

    macro_composite = (
        round(clip_unit(dollar_score if dollar_score is not None else 0.0) * SCORE_SCALE, 2)
        if track_dollar_only
        else None
    )

    return {
        "symbol": sym,
        "price": live_price,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "dollar_z": round(dollar_z, 4) if dollar_z is not None else None,
        "dollar_score": round(dollar_score, 4) if dollar_score is not None else None,
        "dollar_trend": trend,
        "dollar_mixed": is_mixed,
        "dollar_signal": signal,
        "dollar_action": action,
        "macro_composite_score": macro_composite,
        "short_reversal_to_long": is_reversal,
        "short_reversal_reason": reversal_reason,
        "asof": mark_payload.get("asof"),
        "source": mark_payload.get("source"),
        "bid": mark_payload.get("bid"),
        "ask": mark_payload.get("ask"),
        "track_dollar_only": track_dollar_only,
    }
