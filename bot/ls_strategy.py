"""Regime Dual Momentum — directional long/short signals on a single symbol.

Trend filter: EMA fast vs EMA slow with ADX strength gate.
Momentum trigger: MACD histogram zero-line cross in the regime direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from bot.analysis import _adx, _atr, _ema, _macd_series


class LSSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True)
class LSSignal:
    side: LSSide
    price: float
    atr: float
    adx: float
    ema_fast: float
    ema_slow: float
    macd_hist: float
    reason: str


class LongShortRegimeStrategy:
    """Per-bar long / short / flat using EMA regime + ADX + MACD hist cross."""

    def __init__(
        self,
        *,
        ema_fast: int = 21,
        ema_slow: int = 55,
        adx_period: int = 14,
        adx_min: float = 20.0,
        atr_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
    ) -> None:
        if ema_fast >= ema_slow:
            raise ValueError("ema_fast must be < ema_slow")
        if adx_min < 0:
            raise ValueError("adx_min must be >= 0")
        self.ema_fast = int(ema_fast)
        self.ema_slow = int(ema_slow)
        self.adx_period = int(adx_period)
        self.adx_min = float(adx_min)
        self.atr_period = int(atr_period)
        self.macd_fast = int(macd_fast)
        self.macd_slow = int(macd_slow)
        self.macd_signal = int(macd_signal)

    @property
    def bars_needed(self) -> int:
        return max(
            self.ema_slow,
            self.adx_period * 3,
            self.atr_period * 2,
            self.macd_slow + self.macd_signal,
        ) + 5

    def indicator_frame(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Compute indicator columns aligned to `bars` index."""
        if bars is None or bars.empty or "close" not in bars.columns:
            raise ValueError("Need OHLCV bars with a close column")
        df = bars.sort_index().copy()
        closes = df["close"].astype(float)
        highs = df["high"].astype(float) if "high" in df.columns else closes
        lows = df["low"].astype(float) if "low" in df.columns else closes

        out = pd.DataFrame(index=df.index)
        out["close"] = closes
        out["high"] = highs
        out["low"] = lows
        out["open"] = df["open"].astype(float) if "open" in df.columns else closes
        out["ema_fast"] = _ema(closes, self.ema_fast)
        out["ema_slow"] = _ema(closes, self.ema_slow)
        out["atr"] = _atr(highs, lows, closes, self.atr_period)
        out["adx"] = _adx(highs, lows, closes, self.adx_period)
        macd = _macd_series(
            closes,
            fast=self.macd_fast,
            slow=self.macd_slow,
            signal=self.macd_signal,
        )
        out["macd_hist"] = macd["histogram"]
        return out

    def signal_series(self, bars: pd.DataFrame) -> pd.Series:
        """Return LSSide value per bar (flat during warmup / no trigger)."""
        frame = self.indicator_frame(bars)
        sides: list[str] = []
        for i in range(len(frame)):
            if i < self.bars_needed:
                sides.append(LSSide.FLAT.value)
                continue
            row = frame.iloc[i]
            prev = frame.iloc[i - 1]
            sig = self._signal_from_rows(row, prev)
            sides.append(sig.side.value)
        return pd.Series(sides, index=frame.index, name="side")

    def evaluate(self, bars: pd.DataFrame) -> LSSignal:
        """Evaluate the latest bar (needs history for indicators)."""
        if bars is None or bars.empty:
            return LSSignal(
                LSSide.FLAT, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "no bar data"
            )
        frame = self.indicator_frame(bars)
        if len(frame) < self.bars_needed + 1:
            price = float(frame["close"].iloc[-1]) if len(frame) else 0.0
            return LSSignal(
                LSSide.FLAT,
                price,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                f"need at least {self.bars_needed + 1} bars, got {len(frame)}",
            )
        row = frame.iloc[-1]
        prev = frame.iloc[-2]
        return self._signal_from_rows(row, prev)

    def _signal_from_rows(self, row: pd.Series, prev: pd.Series) -> LSSignal:
        price = float(row["close"])
        atr = float(row["atr"]) if pd.notna(row["atr"]) else 0.0
        adx = float(row["adx"]) if pd.notna(row["adx"]) else 0.0
        ema_f = float(row["ema_fast"]) if pd.notna(row["ema_fast"]) else 0.0
        ema_s = float(row["ema_slow"]) if pd.notna(row["ema_slow"]) else 0.0
        hist = float(row["macd_hist"]) if pd.notna(row["macd_hist"]) else 0.0
        prev_hist = float(prev["macd_hist"]) if pd.notna(prev["macd_hist"]) else 0.0

        if any(
            pd.isna(row[c])
            for c in ("ema_fast", "ema_slow", "atr", "adx", "macd_hist")
        ):
            return LSSignal(
                LSSide.FLAT, price, atr, adx, ema_f, ema_s, hist, "indicators warming"
            )

        if adx < self.adx_min:
            return LSSignal(
                LSSide.FLAT,
                price,
                atr,
                adx,
                ema_f,
                ema_s,
                hist,
                f"ADX {adx:.1f} < {self.adx_min:.0f} (no trend)",
            )

        bull = ema_f > ema_s
        bear = ema_f < ema_s
        cross_up = prev_hist <= 0.0 and hist > 0.0
        cross_down = prev_hist >= 0.0 and hist < 0.0

        if bull and cross_up:
            return LSSignal(
                LSSide.LONG,
                price,
                atr,
                adx,
                ema_f,
                ema_s,
                hist,
                f"bull regime (EMA{self.ema_fast}>{self.ema_slow}, ADX {adx:.1f}) "
                f"+ MACD hist cross up",
            )
        if bear and cross_down:
            return LSSignal(
                LSSide.SHORT,
                price,
                atr,
                adx,
                ema_f,
                ema_s,
                hist,
                f"bear regime (EMA{self.ema_fast}<{self.ema_slow}, ADX {adx:.1f}) "
                f"+ MACD hist cross down",
            )
        return LSSignal(
            LSSide.FLAT,
            price,
            atr,
            adx,
            ema_f,
            ema_s,
            hist,
            "no entry trigger",
        )
