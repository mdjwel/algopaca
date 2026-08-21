"""Trading strategies: SMA crossover and buy-the-dip."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from bot.analysis import _bollinger, _rsi, _sma, _trend_bias


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class StrategyResult:
    signal: Signal
    price: float
    fast_sma: float
    slow_sma: float
    reason: str


class SmaCrossoverStrategy:
    """Buy when fast SMA crosses above slow SMA; sell on cross below."""

    def __init__(self, fast: int, slow: int) -> None:
        if fast >= slow:
            raise ValueError("fast period must be < slow period")
        self.fast = fast
        self.slow = slow

    def evaluate(self, bars: pd.DataFrame) -> StrategyResult:
        if bars.empty or "close" not in bars.columns:
            return StrategyResult(Signal.HOLD, 0.0, 0.0, 0.0, "no bar data")

        if len(bars) < self.slow + 2:
            return StrategyResult(
                Signal.HOLD,
                float(bars["close"].iloc[-1]),
                0.0,
                0.0,
                f"need at least {self.slow + 2} bars, got {len(bars)}",
            )

        closes = bars["close"].astype(float)
        fast = closes.rolling(self.fast).mean()
        slow = closes.rolling(self.slow).mean()

        prev_fast, curr_fast = float(fast.iloc[-2]), float(fast.iloc[-1])
        prev_slow, curr_slow = float(slow.iloc[-2]), float(slow.iloc[-1])
        price = float(closes.iloc[-1])

        crossed_up = prev_fast <= prev_slow and curr_fast > curr_slow
        crossed_down = prev_fast >= prev_slow and curr_fast < curr_slow

        if crossed_up:
            return StrategyResult(
                Signal.BUY,
                price,
                curr_fast,
                curr_slow,
                f"bullish crossover (SMA{self.fast} > SMA{self.slow})",
            )
        if crossed_down:
            return StrategyResult(
                Signal.SELL,
                price,
                curr_fast,
                curr_slow,
                f"bearish crossover (SMA{self.fast} < SMA{self.slow})",
            )
        return StrategyResult(
            Signal.HOLD,
            price,
            curr_fast,
            curr_slow,
            "no crossover",
        )


class BuyTheDipStrategy:
    """Buy oversold / lower-band washes; sell into RSI recovery or upper band."""

    def __init__(
        self,
        rsi_buy: float = 30.0,
        rsi_sell: float = 60.0,
        *,
        skip_bearish: bool = True,
        use_lower_band: bool = True,
        rsi_period: int = 14,
        bb_period: int = 20,
    ) -> None:
        if not (0 < rsi_buy < rsi_sell < 100):
            raise ValueError("need 0 < rsi_buy < rsi_sell < 100")
        self.rsi_buy = float(rsi_buy)
        self.rsi_sell = float(rsi_sell)
        self.skip_bearish = bool(skip_bearish)
        self.use_lower_band = bool(use_lower_band)
        self.rsi_period = int(rsi_period)
        self.bb_period = int(bb_period)

    def evaluate(self, bars: pd.DataFrame) -> StrategyResult:
        if bars.empty or "close" not in bars.columns:
            return StrategyResult(Signal.HOLD, 0.0, 0.0, 0.0, "no bar data")

        need = max(self.rsi_period + 2, self.bb_period, 50) + 1
        closes = bars["close"].astype(float)
        price = float(closes.iloc[-1])
        if len(bars) < need:
            return StrategyResult(
                Signal.HOLD,
                price,
                0.0,
                0.0,
                f"need at least {need} bars, got {len(bars)}",
            )

        rsi = _rsi(closes, self.rsi_period)
        prev_rsi = _rsi(closes.iloc[:-1], self.rsi_period)
        bb = _bollinger(closes, self.bb_period)
        pct_b = bb.get("pct_b")
        sma10 = _sma(closes, 10)
        sma20 = _sma(closes, 20)
        sma50 = _sma(closes, 50)
        trend = _trend_bias(price, sma10, sma20, sma50)

        # Wall metrics: fast=RSI, slow=BB %b scaled 0–100.
        rsi_v = float(rsi) if rsi is not None else 0.0
        pct_display = float(pct_b) * 100.0 if pct_b is not None else 0.0

        if rsi is None or pct_b is None:
            return StrategyResult(
                Signal.HOLD,
                price,
                rsi_v,
                pct_display,
                "indicators unavailable",
            )

        oversold = rsi <= self.rsi_buy
        at_lower_band = self.use_lower_band and pct_b <= 0.05
        deep = oversold or at_lower_band
        # Prefer a wash that is stabilizing (RSI not still collapsing).
        stabilizing = prev_rsi is None or rsi >= prev_rsi - 0.5

        # Exit only on clear recovery / stretch — mid-band alone is not a sell.
        recovered = rsi >= self.rsi_sell or pct_b >= 0.95

        if deep and stabilizing:
            if self.skip_bearish and trend == "bearish":
                return StrategyResult(
                    Signal.HOLD,
                    price,
                    rsi_v,
                    pct_display,
                    (
                        f"dip ignored — bearish trend "
                        f"(RSI {rsi_v:.1f}, %b {pct_display:.0f})"
                    ),
                )
            why = []
            if oversold:
                why.append(f"RSI {rsi_v:.1f}≤{self.rsi_buy:.0f}")
            if at_lower_band:
                why.append(f"lower BB (%b {pct_display:.0f})")
            return StrategyResult(
                Signal.BUY,
                price,
                rsi_v,
                pct_display,
                f"buy the dip ({', '.join(why)}; trend={trend})",
            )

        if recovered:
            why = (
                f"RSI {rsi_v:.1f}≥{self.rsi_sell:.0f}"
                if rsi >= self.rsi_sell
                else f"%b {pct_display:.0f} at upper band"
            )
            return StrategyResult(
                Signal.SELL,
                price,
                rsi_v,
                pct_display,
                f"dip recovery exit ({why})",
            )

        return StrategyResult(
            Signal.HOLD,
            price,
            rsi_v,
            pct_display,
            f"no dip (RSI {rsi_v:.1f}, %b {pct_display:.0f}, trend={trend})",
        )
