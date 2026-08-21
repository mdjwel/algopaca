"""Technical analysis indicators from OHLCV bars."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    last_gain = float(gain.iloc[-1])
    last_loss = float(loss.iloc[-1])
    if np.isnan(last_gain) or np.isnan(last_loss):
        return None
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    rs = last_gain / last_loss
    return round(100 - (100 / (1 + rs)), 2)


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _macd_series(
    closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """Return MACD line, signal, and histogram as aligned series."""
    macd_line = _ema(closes.astype(float), fast) - _ema(closes.astype(float), slow)
    signal_line = _ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "histogram": hist},
        index=closes.index,
    )


def _macd(
    closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, float | None]:
    if len(closes) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None}
    frame = _macd_series(closes, fast=fast, slow=slow, signal=signal)
    return {
        "macd": _finite(frame["macd"].iloc[-1]),
        "signal": _finite(frame["signal"].iloc[-1]),
        "histogram": _finite(frame["histogram"].iloc[-1]),
    }


def _true_range(highs: pd.Series, lows: pd.Series, closes: pd.Series) -> pd.Series:
    prev_close = closes.astype(float).shift(1)
    high = highs.astype(float)
    low = lows.astype(float)
    ranges = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _atr(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Wilder-smoothed Average True Range series."""
    tr = _true_range(highs, lows, closes)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _adx(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Wilder ADX series (trend strength; typically >20 = trending)."""
    high = highs.astype(float)
    low = lows.astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(
        np.where((up > down) & (up > 0), up, 0.0), index=high.index, dtype=float
    )
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0), down, 0.0), index=high.index, dtype=float
    )
    atr = _atr(high, low, closes, period)
    plus_di = 100.0 * (
        plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, np.nan)
    )
    minus_di = 100.0 * (
        minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, np.nan)
    )
    dx = (
        100.0
        * (plus_di - minus_di).abs()
        / (plus_di + minus_di).replace(0, np.nan)
    )
    return dx.ewm(alpha=1.0 / period, adjust=False).mean()


def _bollinger(
    closes: pd.Series, period: int = 20, num_std: float = 2.0
) -> dict[str, float | None]:
    if len(closes) < period:
        return {"mid": None, "upper": None, "lower": None, "pct_b": None}
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    last = float(closes.iloc[-1])
    u, l, m = float(upper.iloc[-1]), float(lower.iloc[-1]), float(mid.iloc[-1])
    width = u - l
    pct_b = (last - l) / width if width else None
    return {
        "mid": _finite(m),
        "upper": _finite(u),
        "lower": _finite(l),
        "pct_b": _finite(pct_b) if pct_b is not None else None,
    }


def _sma(closes: pd.Series, period: int) -> float | None:
    if len(closes) < period:
        return None
    return _finite(closes.rolling(period).mean().iloc[-1])


def _finite(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(f) or np.isinf(f):
        return None
    return f


def _trend_bias(
    price: float,
    sma10: float | None,
    sma20: float | None,
    sma50: float | None,
) -> str:
    """Classify trend from SMA stack. SMA20 confirms when present."""
    if sma10 is None or sma50 is None:
        return "neutral"
    above_mid = sma20 is None or price > sma20
    below_mid = sma20 is None or price < sma20
    if sma10 > sma50 and above_mid:
        return "bullish"
    if sma10 < sma50 and below_mid:
        return "bearish"
    return "neutral"


def htf_trend(bars: pd.DataFrame) -> dict[str, Any]:
    """Compact higher-timeframe read used to veto counter-trend intraday entries."""
    if bars is None or bars.empty or "close" not in bars.columns:
        return {"ok": False, "bias": "unknown"}
    closes = bars["close"].astype(float)
    price = float(closes.iloc[-1])
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    if sma20 is None or sma50 is None:
        return {"ok": False, "bias": "unknown"}
    if price > sma20 and sma20 > sma50:
        bias = "bullish"
    elif price < sma20 and sma20 < sma50:
        bias = "bearish"
    else:
        bias = "neutral"
    return {
        "ok": True,
        "bias": bias,
        "price": round(price, 4),
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4),
    }


def compute_technicals(bars: pd.DataFrame) -> dict[str, Any]:
    """Return a compact TA snapshot for LLM context."""
    if bars is None or bars.empty or "close" not in bars.columns:
        return {"ok": False, "error": "no bar data"}

    closes = bars["close"].astype(float)
    highs = bars["high"].astype(float) if "high" in bars.columns else closes
    lows = bars["low"].astype(float) if "low" in bars.columns else closes
    volume = (
        bars["volume"].astype(float) if "volume" in bars.columns else pd.Series(dtype=float)
    )

    price = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else price
    change_pct = ((price - prev) / prev * 100) if prev else 0.0

    sma10 = _sma(closes, 10)
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    rsi = _rsi(closes, 14)
    macd = _macd(closes)
    bb = _bollinger(closes, 20)

    trend = _trend_bias(price, sma10, sma20, sma50)

    # Volatility + trend strength: sizing, stop distance, and the chop filter all
    # read these, so they must reach the model (they used to be computed only by
    # the L/S engine).
    atr14 = _finite(_atr(highs, lows, closes, 14).iloc[-1]) if len(closes) > 14 else None
    adx14 = _finite(_adx(highs, lows, closes, 14).iloc[-1]) if len(closes) > 28 else None
    atr_pct = (atr14 / price * 100) if (atr14 and price) else None
    regime = "unknown" if adx14 is None else ("trending" if adx14 >= 20 else "chop")
    dist_sma50_atr = (
        round((price - sma50) / atr14, 2) if (sma50 and atr14 and atr14 > 0) else None
    )

    vol_avg = None
    vol_ratio = None
    if not volume.empty and len(volume) >= 20:
        vol_avg = _finite(volume.tail(20).mean())
        last_vol = _finite(volume.iloc[-1])
        if vol_avg and last_vol is not None and vol_avg > 0:
            vol_ratio = round(last_vol / vol_avg, 2)

    recent = []
    tail = bars.tail(5)
    for ts, row in tail.iterrows():
        recent.append(
            {
                "t": str(ts),
                "o": _finite(row.get("open")),
                "h": _finite(row.get("high")),
                "l": _finite(row.get("low")),
                "c": _finite(row.get("close")),
                "v": _finite(row.get("volume")),
            }
        )

    high_20 = _finite(highs.tail(20).max()) if len(highs) else None
    low_20 = _finite(lows.tail(20).min()) if len(lows) else None

    return {
        "ok": True,
        "price": round(price, 4),
        "change_1bar_pct": round(change_pct, 3),
        "sma": {"10": sma10, "20": sma20, "50": sma50},
        "rsi_14": round(rsi, 2) if rsi is not None else None,
        "macd": macd,
        "bollinger": bb,
        "trend_bias": trend,
        "atr_14": round(atr14, 4) if atr14 is not None else None,
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        "adx_14": round(adx14, 1) if adx14 is not None else None,
        "regime": regime,
        "dist_sma50_atr": dist_sma50_atr,
        "volume_avg_20": vol_avg,
        "volume_ratio": vol_ratio,
        "range_20": {"high": high_20, "low": low_20},
        "bars_used": len(bars),
        "recent_bars": recent,
    }


def daily_bar_stats(bars: pd.DataFrame) -> dict[str, Any]:
    """Where price sits today and against its year, for the Manual Order rail.

    Deliberately separate from :func:`compute_technicals`: that builds an LLM
    context and computes a dozen indicators the ticket never shows. This is the
    handful of numbers a person needs to answer "am I buying the high of the
    day?" — and a ticket refresh runs every fifteen seconds, so it stays cheap.

    Every field is independently optional. A symbol with three days of history
    still gets a day change; it just has no 52-week range, and the caller
    renders a dash rather than a wrong number.
    """
    empty = {
        "ok": False,
        "day_change_pct": None,
        "day_range": None,
        "day_range_pct": None,
        "range_52w": None,
        "pct_from_52w_high": None,
        "atr_pct": None,
        "volume_ratio": None,
        "bars_used": 0,
    }
    if bars is None or bars.empty or "close" not in bars.columns:
        return empty

    closes = bars["close"].astype(float).dropna()
    if closes.empty:
        return empty
    highs = bars["high"].astype(float) if "high" in bars.columns else closes
    lows = bars["low"].astype(float) if "low" in bars.columns else closes

    price = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else None
    day_change = (
        round((price - prev) / prev * 100, 2) if prev and prev > 0 else None
    )

    day_high = _finite(highs.iloc[-1])
    day_low = _finite(lows.iloc[-1])
    day_range = None
    day_range_pct = None
    if day_high is not None and day_low is not None and day_high > day_low:
        day_range = {"high": round(day_high, 4), "low": round(day_low, 4)}
        # 0% = printing the low of the day, 100% = printing the high.
        day_range_pct = round((price - day_low) / (day_high - day_low) * 100, 1)

    range_52w = None
    pct_from_high = None
    # 200 sessions is close enough to a year that the number means something,
    # and short enough that a recent listing still gets one.
    if len(closes) >= 200:
        window_high = _finite(highs.tail(252).max())
        window_low = _finite(lows.tail(252).min())
        if window_high and window_low and window_high > window_low:
            range_52w = {"high": round(window_high, 4), "low": round(window_low, 4)}
            pct_from_high = round((price - window_high) / window_high * 100, 2)

    atr14 = _finite(_atr(highs, lows, closes, 14).iloc[-1]) if len(closes) > 14 else None
    atr_pct = round(atr14 / price * 100, 2) if (atr14 and price > 0) else None

    volume_ratio = None
    if "volume" in bars.columns:
        volume = bars["volume"].astype(float)
        if len(volume) >= 20:
            avg = _finite(volume.tail(20).mean())
            last = _finite(volume.iloc[-1])
            if avg and last is not None and avg > 0:
                volume_ratio = round(last / avg, 2)

    return {
        "ok": True,
        "price": round(price, 4),
        "day_change_pct": day_change,
        "day_range": day_range,
        "day_range_pct": day_range_pct,
        "range_52w": range_52w,
        "pct_from_52w_high": pct_from_high,
        "atr_pct": atr_pct,
        "volume_ratio": volume_ratio,
        "bars_used": int(len(closes)),
    }
