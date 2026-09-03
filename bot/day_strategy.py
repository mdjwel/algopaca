"""Day Trading Strategy: VWAP, Opening Range Breakout (ORB), EMA Momentum, and Mean Reversion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime

import numpy as np
import pandas as pd

from bot.analysis import _adx, _atr, _ema, _rsi
from bot.strategy import Signal, StrategyResult

ET_TZ = "America/New_York"
# Regular US equity hours; the intraday session every day rule below is anchored to.
MARKET_OPEN_ET = dtime(9, 30)
MAX_ORB_MINUTES = 60

# --- Signal quality filters -------------------------------------------------
# These exist to cut the two ways an intraday system bleeds: churning in and out
# around a level, and taking trades with no edge behind them. Every threshold is
# expressed in ATR or in the desk's existing indicator conventions so it travels
# across symbols and timeframes.

# Hysteresis around VWAP. Entry needs price clear of VWAP by ENTRY; the exit only
# fires once price is back below it by EXIT. The gap between the two is a dead
# band, so price oscillating on VWAP can no longer trigger enter/exit/enter.
VWAP_ENTRY_BUFFER_ATR = 0.10
VWAP_EXIT_BUFFER_ATR = 0.35

# Trend vs chop. Same ADX convention the desk already uses in bot/analysis.py.
ADX_TREND_MIN = 20.0
ADX_CHOP_MAX = 18.0
# A trend this strong is worth trading even through the midday lull.
ADX_STRONG_TREND = 25.0

# Bars to look back for a pullback to the fast EMA. A continuation entry needs
# price to have come back to value recently, not merely to be above the line.
PULLBACK_LOOKBACK = 3

# Tangled EMAs mean no directional edge; measure their gap in ATR units.
EMA_SEPARATION_MIN_ATR = 0.15

# A breakout without volume behind it is usually retraced.
BREAKOUT_VOLUME_RATIO = 1.15
VOLUME_LOOKBACK = 20

# Never chase. Entering this far past the trigger leaves the ATR stop too wide
# for the reward left in the move, which is how a good signal becomes a bad trade.
# A breakout is measured against a fixed level, so it can be held to a tight
# bound; a trend entry is measured against a lagging EMA that price legitimately
# runs ahead of, so it only screens out parabolic blow-offs.
BREAKOUT_CHASE_ATR = 1.0
TREND_CHASE_ATR = 2.5

# An ATR this thin relative to price is noise, not volatility worth trading.
MIN_ATR_PCT = 0.05

# RSI overbought / oversold guardrails for day trading entries.
RSI_MAX_BUY = 70.0
RSI_MIN_SELL = 30.0

# The lowest-edge stretch of the US session for momentum scalping.
LUNCH_START_ET = dtime(11, 30)
LUNCH_END_ET = dtime(13, 30)


@dataclass(frozen=True)
class DayContext:
    """Everything the intraday rules read, computed once per evaluation."""

    price: float
    prev_price: float
    fast: float
    slow: float
    prev_fast: float
    prev_slow: float
    vwap: float
    vwap_upper: float
    vwap_lower: float
    orh: float | None
    orl: float | None
    orb_ready: bool
    rsi: float
    prev_rsi: float
    atr: float
    adx: float | None
    vol_ratio: float | None
    bar_time: dtime | None
    # Price dipped to the fast EMA within the last few bars and is back above it.
    pulled_back: bool = False
    # Price rallied to the fast EMA within the last few bars and is back below it.
    pulled_back_bearish: bool = False

    @property
    def entry_buffer(self) -> float:
        return VWAP_ENTRY_BUFFER_ATR * self.atr

    @property
    def exit_buffer(self) -> float:
        return VWAP_EXIT_BUFFER_ATR * self.atr

    @property
    def ema_separation_atr(self) -> float:
        return abs(self.fast - self.slow) / self.atr if self.atr > 0 else 0.0

    @property
    def atr_pct(self) -> float:
        return (self.atr / self.price * 100.0) if self.price > 0 else 0.0

    def is_trending(self) -> bool:
        """Unknown ADX passes: never block a trade on a missing indicator."""
        return self.adx is None or self.adx >= ADX_TREND_MIN

    def is_ranging(self) -> bool:
        return self.adx is None or self.adx <= ADX_CHOP_MAX

    def has_breakout_volume(self) -> bool:
        """Unknown volume passes, so a feed without volume still trades."""
        return self.vol_ratio is None or self.vol_ratio >= BREAKOUT_VOLUME_RATIO

    def in_lunch_chop(self) -> bool:
        """Midday lull — but a strong trend is still worth taking through it."""
        if self.bar_time is None:
            return False
        if not (LUNCH_START_ET <= self.bar_time < LUNCH_END_ET):
            return False
        return self.adx is not None and self.adx < ADX_STRONG_TREND


def eastern_index(index: pd.Index) -> pd.Index:
    """Return `index` expressed in US/Eastern, or unchanged when not datetime-like.

    VWAP and the opening range both anchor to the *Eastern* trading date; using
    the raw (UTC) date instead splits an extended-hours session in two.
    """
    if not isinstance(index, pd.DatetimeIndex) or len(index) == 0:
        return index
    try:
        if index.tz is not None:
            return index.tz_convert(ET_TZ)
        return index.tz_localize("UTC").tz_convert(ET_TZ)
    except Exception:
        return index


def orb_cutoff_time(orb_minutes: int) -> dtime:
    """Eastern clock time at which the opening range window closes."""
    minutes = max(1, min(MAX_ORB_MINUTES, int(orb_minutes or 15)))
    total = MARKET_OPEN_ET.hour * 60 + MARKET_OPEN_ET.minute + minutes
    return dtime(total // 60, total % 60)


def compute_intraday_vwap(bars: pd.DataFrame) -> dict[str, float | None]:
    """Compute cumulative intraday VWAP and deviation bands for the latest session."""
    if bars.empty or "close" not in bars.columns or "volume" not in bars.columns:
        return {"vwap": None, "upper": None, "lower": None, "std": None}

    df = bars.copy()
    highs = df["high"].astype(float) if "high" in df.columns else df["close"].astype(float)
    lows = df["low"].astype(float) if "low" in df.columns else df["close"].astype(float)
    closes = df["close"].astype(float)
    volumes = df["volume"].astype(float)

    # Typical Price = (High + Low + Close) / 3
    tp = (highs + lows + closes) / 3.0

    # If datetime index, anchor to the latest Eastern trading day; else use all bars
    if isinstance(df.index, pd.DatetimeIndex) and len(df.index) > 0:
        idx_et = eastern_index(df.index)
        latest_date = idx_et[-1].date()
        mask = idx_et.date == latest_date
        if mask.any():
            tp = tp[mask]
            volumes = volumes[mask]
            closes = closes[mask]

    vol_sum = volumes.sum()
    if vol_sum <= 0:
        price = float(closes.iloc[-1])
        return {"vwap": price, "upper": price, "lower": price, "std": 0.0}

    pv = tp * volumes
    vwap = float(pv.sum() / vol_sum)

    # Standard deviation around VWAP
    variance = float(((tp - vwap) ** 2 * volumes).sum() / vol_sum)
    std = float(np.sqrt(max(0.0, variance)))

    return {
        "vwap": round(vwap, 4),
        "upper": round(vwap + 1.5 * std, 4),
        "lower": round(vwap - 1.5 * std, 4),
        "std": round(std, 4),
    }


def compute_opening_range(
    bars: pd.DataFrame, orb_minutes: int = 15
) -> dict[str, float | None]:
    """Calculate Opening Range High (ORH) and Low (ORL) for regular market hours (9:30 AM ET onwards)."""
    if bars.empty or not isinstance(bars.index, pd.DatetimeIndex):
        closes = bars["close"].astype(float) if "close" in bars.columns else pd.Series(dtype=float)
        if closes.empty:
            return {"orh": None, "orl": None, "is_established": False}
        return {
            "orh": float(closes.max()),
            "orl": float(closes.min()),
            "is_established": True,
        }

    # Convert index to US/Eastern if tz-aware, else treat as UTC/local
    idx_et = eastern_index(bars.index)

    latest_date = idx_et[-1].date()
    today_mask = idx_et.date == latest_date
    today_bars = bars[today_mask]
    today_et = idx_et[today_mask]

    if today_bars.empty:
        return {"orh": None, "orl": None, "is_established": False}

    # Regular hours start at 09:30 ET
    open_time = MARKET_OPEN_ET
    cutoff_time = orb_cutoff_time(orb_minutes)

    orb_mask = [(t.time() >= open_time and t.time() < cutoff_time) for t in today_et]
    orb_bars = today_bars[orb_mask]

    highs = today_bars["high"].astype(float) if "high" in today_bars.columns else today_bars["close"].astype(float)
    lows = today_bars["low"].astype(float) if "low" in today_bars.columns else today_bars["close"].astype(float)

    if not orb_bars.empty:
        orb_highs = orb_bars["high"].astype(float) if "high" in orb_bars.columns else orb_bars["close"].astype(float)
        orb_lows = orb_bars["low"].astype(float) if "low" in orb_bars.columns else orb_bars["close"].astype(float)
        # The range is only final once the clock has cleared the ORB window.
        # Counting bars instead would assume a 5-minute timeframe: it locks the
        # range in after 3 minutes on 1Min bars and never locks it in at all on
        # 15Min or coarser bars.
        return {
            "orh": round(float(orb_highs.max()), 4),
            "orl": round(float(orb_lows.min()), 4),
            "is_established": today_et[-1].time() >= cutoff_time,
        }

    # Fallback to current session high/low when no regular-hours bar has printed
    # inside the opening range window yet (pre-market only, or a late data feed).
    return {
        "orh": round(float(highs.max()), 4),
        "orl": round(float(lows.min()), 4),
        "is_established": False,
    }


class DayTradingStrategy:
    """Quantitative Day Trading strategy engine combining VWAP, ORB, EMA momentum, and mean reversion."""

    def __init__(
        self,
        *,
        sub_mode: str = "vwap_trend",
        ema_fast: int = 9,
        ema_slow: int = 21,
        orb_minutes: int = 15,
        side: str = "long_only",
        quality_filters: bool = True,
    ) -> None:
        self.sub_mode = str(sub_mode or "vwap_trend").strip().lower()
        if self.sub_mode not in {"vwap_trend", "orb", "momentum_scalp", "vwap_fade"}:
            self.sub_mode = "vwap_trend"
        self.ema_fast = max(2, int(ema_fast or 9))
        self.ema_slow = max(self.ema_fast + 1, int(ema_slow or 21))
        self.orb_minutes = max(1, min(MAX_ORB_MINUTES, int(orb_minutes or 15)))
        side_val = str(side or "long_only").strip().lower()
        self.side = side_val if side_val in {"long_only", "short_only", "long_short"} else "long_only"
        # Off reproduces the raw indicator triggers with no regime, volume,
        # extension or hysteresis screening — kept so the two can be measured
        # against each other in a backtest.
        self.quality_filters = bool(quality_filters)

    # ------------------------------------------------------------------ setup

    def bars_needed(self) -> int:
        # ADX(14) needs more than 28 bars before it means anything.
        return max(self.ema_slow + 2, 30)

    def _context(self, bars: pd.DataFrame) -> DayContext:
        closes = bars["close"].astype(float)
        highs = bars["high"].astype(float) if "high" in bars.columns else closes
        lows = bars["low"].astype(float) if "low" in bars.columns else closes
        price = float(closes.iloc[-1])

        fast_series = _ema(closes, self.ema_fast)
        slow_series = _ema(closes, self.ema_slow)

        vwap_data = compute_intraday_vwap(bars)
        orb_data = compute_opening_range(bars, self.orb_minutes)

        atr_series = _atr(highs, lows, closes, 14)
        atr = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
        if not np.isfinite(atr) or atr <= 0:
            # A flat feed still needs a non-zero scale for the ATR-based buffers.
            atr = max(price * 0.002, 1e-6)

        adx: float | None = None
        if len(closes) > 28:
            adx_val = float(_adx(highs, lows, closes, 14).iloc[-1])
            adx = adx_val if np.isfinite(adx_val) else None

        vol_ratio: float | None = None
        if "volume" in bars.columns and len(bars) > VOLUME_LOOKBACK:
            volumes = bars["volume"].astype(float)
            baseline = float(volumes.iloc[-(VOLUME_LOOKBACK + 1) : -1].mean())
            if baseline > 0:
                vol_ratio = float(volumes.iloc[-1]) / baseline

        bar_time: dtime | None = None
        idx_et = eastern_index(bars.index)
        if isinstance(idx_et, pd.DatetimeIndex) and len(idx_et):
            bar_time = idx_et[-1].time()

        curr_fast = float(fast_series.iloc[-1])
        pulled_back = False
        if price > curr_fast and len(lows) > PULLBACK_LOOKBACK:
            recent_lows = lows.iloc[-(PULLBACK_LOOKBACK + 1) : -1]
            recent_fast = fast_series.iloc[-(PULLBACK_LOOKBACK + 1) : -1]
            pulled_back = bool((recent_lows <= recent_fast).any())

        pulled_back_bearish = False
        if price < curr_fast and len(highs) > PULLBACK_LOOKBACK:
            recent_highs = highs.iloc[-(PULLBACK_LOOKBACK + 1) : -1]
            recent_fast = fast_series.iloc[-(PULLBACK_LOOKBACK + 1) : -1]
            pulled_back_bearish = bool((recent_highs >= recent_fast).any())

        return DayContext(
            price=price,
            prev_price=float(closes.iloc[-2]) if len(closes) > 1 else price,
            fast=float(fast_series.iloc[-1]),
            slow=float(slow_series.iloc[-1]),
            prev_fast=float(fast_series.iloc[-2]) if len(fast_series) > 1 else float(fast_series.iloc[-1]),
            prev_slow=float(slow_series.iloc[-2]) if len(slow_series) > 1 else float(slow_series.iloc[-1]),
            vwap=vwap_data.get("vwap") or price,
            vwap_upper=vwap_data.get("upper") or price,
            vwap_lower=vwap_data.get("lower") or price,
            orh=orb_data.get("orh"),
            orl=orb_data.get("orl"),
            orb_ready=bool(orb_data.get("is_established", False)),
            rsi=_rsi(closes, 14) or 50.0,
            prev_rsi=_rsi(closes.iloc[:-1], 14) or (_rsi(closes, 14) or 50.0),
            atr=atr,
            adx=adx,
            vol_ratio=vol_ratio,
            bar_time=bar_time,
            pulled_back=pulled_back,
            pulled_back_bearish=pulled_back_bearish,
        )

    def _tradable(self, ctx: DayContext) -> str | None:
        """Reason this bar is untradable at all, or None when it is fine."""
        if not self.quality_filters:
            return None
        if ctx.atr_pct < MIN_ATR_PCT:
            return f"volatility too thin (ATR {ctx.atr_pct:.2f}% of price)"
        return None

    # ------------------------------------------------------------- evaluation

    def evaluate(self, bars: pd.DataFrame) -> StrategyResult:
        if bars.empty or "close" not in bars.columns:
            return StrategyResult(Signal.HOLD, 0.0, 0.0, 0.0, "no bar data")

        price = float(bars["close"].astype(float).iloc[-1])
        need = self.bars_needed()
        if len(bars) < need:
            return StrategyResult(
                Signal.HOLD,
                price,
                0.0,
                0.0,
                f"need at least {need} intraday bars, got {len(bars)}",
            )

        ctx = self._context(bars)

        if self.sub_mode == "vwap_trend":
            return self._eval_vwap_trend(ctx)
        if self.sub_mode == "orb":
            return self._eval_orb(ctx)
        if self.sub_mode == "momentum_scalp":
            return self._eval_momentum_scalp(ctx)
        if self.sub_mode == "vwap_fade":
            return self._eval_vwap_fade(ctx)
        return StrategyResult(Signal.HOLD, ctx.price, ctx.vwap, ctx.fast, "hold")

    # --------------------------------------------------------------- vwap_trend

    def _eval_vwap_trend(self, ctx: DayContext) -> StrategyResult:
        a, b = ctx.vwap, ctx.fast

        def result(signal: Signal, reason: str) -> StrategyResult:
            return StrategyResult(signal, ctx.price, a, b, reason)

        # --- exits: positions must always be able to get out ----------------
        exit_level_long = ctx.vwap - ctx.exit_buffer if self.quality_filters else ctx.vwap
        broke_vwap = ctx.price < exit_level_long
        lost_trend = ctx.fast < ctx.slow

        should_exit_long = (
            (broke_vwap and (lost_trend or ctx.price < ctx.vwap - 2.0 * ctx.exit_buffer))
            if self.quality_filters
            else (broke_vwap or lost_trend)
        )

        reclaim_level_short = ctx.vwap + ctx.exit_buffer if self.quality_filters else ctx.vwap
        reclaimed_vwap = ctx.price > reclaim_level_short
        recovered_trend = ctx.fast > ctx.slow

        should_exit_short = (
            (reclaimed_vwap and (recovered_trend or ctx.price > ctx.vwap + 2.0 * ctx.exit_buffer))
            if self.quality_filters
            else (reclaimed_vwap or recovered_trend)
        )

        blocked = self._tradable(ctx)

        # --- short-only mode: only short entries and short covers -----------
        if self.side == "short_only":
            if should_exit_short:
                why = "reclaimed VWAP" if reclaimed_vwap else f"EMA{self.ema_fast} crossed above EMA{self.ema_slow}"
                return result(
                    Signal.BUY,
                    f"VWAP trend cover / exit short — {why} (Price ${ctx.price:.2f}, VWAP ${ctx.vwap:.2f}, RSI {ctx.rsi:.1f})",
                )
            if blocked:
                return result(Signal.HOLD, f"no entry — {blocked}")

            below_vwap = ctx.price < ctx.vwap - (ctx.entry_buffer if self.quality_filters else 0.0)
            trend_down = ctx.fast < ctx.slow if self.quality_filters else ctx.fast <= ctx.slow
            rsi_ok_short = (
                (RSI_MIN_SELL <= ctx.rsi <= 50.0)
                if self.quality_filters
                else (ctx.rsi <= 52.0)
            )

            if not (below_vwap and trend_down and rsi_ok_short):
                rsi_hint = f", oversold RSI {ctx.rsi:.1f}" if ctx.rsi < RSI_MIN_SELL else f", RSI {ctx.rsi:.1f}"
                return result(
                    Signal.HOLD,
                    f"inside VWAP zone (Price ${ctx.price:.2f}, VWAP ${ctx.vwap:.2f}{rsi_hint})",
                )

            if self.quality_filters:
                if not ctx.is_trending():
                    return result(Signal.HOLD, f"no entry — chop regime (ADX {ctx.adx:.0f} < {ADX_TREND_MIN:.0f})")
                if ctx.ema_separation_atr < EMA_SEPARATION_MIN_ATR:
                    return result(Signal.HOLD, f"no entry — EMAs tangled ({ctx.ema_separation_atr:.2f} ATR apart)")
                if ctx.price < ctx.fast - TREND_CHASE_ATR * ctx.atr:
                    return result(Signal.HOLD, f"no entry — extended {((ctx.fast - ctx.price) / ctx.atr):.1f} ATR below EMA{self.ema_fast}")

            crossed_down = ctx.prev_fast >= ctx.prev_slow or ctx.prev_price >= ctx.vwap
            is_fresh_short = crossed_down or ctx.pulled_back_bearish or (ctx.prev_price >= ctx.prev_fast and ctx.price < ctx.fast)
            if self.quality_filters and not is_fresh_short:
                return result(Signal.HOLD, "bearish VWAP trend alignment (waiting for fresh cross or pullback rejection)")

            trigger_short = "bearish VWAP breakdown" if crossed_down else ("pullback rejected" if ctx.pulled_back_bearish else "bearish VWAP trend alignment")
            return result(
                Signal.SELL,
                f"{trigger_short} (Price ${ctx.price:.2f} < VWAP ${ctx.vwap:.2f}, EMA{self.ema_fast} < EMA{self.ema_slow}, RSI {ctx.rsi:.1f})",
            )

        # --- long entries (when in long_only or long_short) -----------------
        above_vwap = ctx.price > ctx.vwap + (ctx.entry_buffer if self.quality_filters else 0.0)
        trend_up = ctx.fast > ctx.slow if self.quality_filters else ctx.fast >= ctx.slow
        rsi_ok = (
            (50.0 <= ctx.rsi <= RSI_MAX_BUY)
            if self.quality_filters
            else (ctx.rsi >= 48.0)
        )

        can_long_entry = not blocked and above_vwap and trend_up and rsi_ok
        if can_long_entry and self.quality_filters:
            if not ctx.is_trending() or ctx.ema_separation_atr < EMA_SEPARATION_MIN_ATR or ctx.price > ctx.fast + TREND_CHASE_ATR * ctx.atr:
                can_long_entry = False

        crossed_up = ctx.prev_fast <= ctx.prev_slow or ctx.prev_price <= ctx.vwap
        is_fresh = crossed_up or ctx.pulled_back or (ctx.prev_price <= ctx.prev_fast and ctx.price > ctx.fast)
        if self.quality_filters and not is_fresh:
            can_long_entry = False

        if can_long_entry:
            trigger = "bullish VWAP crossover" if crossed_up else ("pullback resumed" if ctx.pulled_back else "bullish VWAP trend alignment")
            return result(
                Signal.BUY,
                f"{trigger} (Price ${ctx.price:.2f} > VWAP ${ctx.vwap:.2f}, "
                f"EMA{self.ema_fast} > EMA{self.ema_slow}, RSI {ctx.rsi:.1f})",
            )

        # --- short entries (when in long_short) -----------------------------
        if self.side == "long_short":
            below_vwap = ctx.price < ctx.vwap - (ctx.entry_buffer if self.quality_filters else 0.0)
            trend_down = ctx.fast < ctx.slow if self.quality_filters else ctx.fast <= ctx.slow
            rsi_ok_short = (
                (RSI_MIN_SELL <= ctx.rsi <= 50.0)
                if self.quality_filters
                else (ctx.rsi <= 52.0)
            )
            can_short_entry = not blocked and below_vwap and trend_down and rsi_ok_short
            if can_short_entry and self.quality_filters:
                if not ctx.is_trending() or ctx.ema_separation_atr < EMA_SEPARATION_MIN_ATR or ctx.price < ctx.fast - TREND_CHASE_ATR * ctx.atr:
                    can_short_entry = False

            crossed_down = ctx.prev_fast >= ctx.prev_slow or ctx.prev_price >= ctx.vwap
            is_fresh_short = crossed_down or ctx.pulled_back_bearish or (ctx.prev_price >= ctx.prev_fast and ctx.price < ctx.fast)
            if self.quality_filters and not is_fresh_short:
                can_short_entry = False

            if can_short_entry:
                trigger_short = "bearish VWAP breakdown" if crossed_down else ("pullback rejected" if ctx.pulled_back_bearish else "bearish VWAP trend alignment")
                return result(
                    Signal.SELL,
                    f"{trigger_short} (Price ${ctx.price:.2f} < VWAP ${ctx.vwap:.2f}, EMA{self.ema_fast} < EMA{self.ema_slow}, RSI {ctx.rsi:.1f})",
                )

        # --- exits if no new entry triggered --------------------------------
        if should_exit_long:
            why = "lost VWAP" if broke_vwap else f"EMA{self.ema_fast} crossed below EMA{self.ema_slow}"
            return result(
                Signal.SELL,
                f"VWAP trend exit — {why} (Price ${ctx.price:.2f}, VWAP ${ctx.vwap:.2f}, RSI {ctx.rsi:.1f})",
            )
        if self.side == "long_short" and should_exit_short:
            why = "reclaimed VWAP" if reclaimed_vwap else f"EMA{self.ema_fast} crossed above EMA{self.ema_slow}"
            return result(
                Signal.BUY,
                f"VWAP trend cover / exit short — {why} (Price ${ctx.price:.2f}, VWAP ${ctx.vwap:.2f}, RSI {ctx.rsi:.1f})",
            )

        if blocked:
            return result(Signal.HOLD, f"no entry — {blocked}")

        rsi_hint = f", overbought RSI {ctx.rsi:.1f}" if ctx.rsi > RSI_MAX_BUY else (f", oversold RSI {ctx.rsi:.1f}" if ctx.rsi < RSI_MIN_SELL else f", RSI {ctx.rsi:.1f}")
        return result(
            Signal.HOLD,
            f"inside VWAP zone (Price ${ctx.price:.2f}, VWAP ${ctx.vwap:.2f}{rsi_hint})",
        )

    # ---------------------------------------------------------------------- orb

    def _eval_orb(self, ctx: DayContext) -> StrategyResult:
        orh, orl = ctx.orh, ctx.orl

        def result(signal: Signal, reason: str) -> StrategyResult:
            return StrategyResult(
                signal,
                ctx.price,
                orh if orh is not None else ctx.price,
                orl if orl is not None else ctx.price,
                reason,
            )

        if not ctx.orb_ready or orh is None or orl is None:
            return result(
                Signal.HOLD,
                f"building {self.orb_minutes}m opening range "
                f"(High: ${orh or 0:.2f}, Low: ${orl or 0:.2f})",
            )

        blocked = self._tradable(ctx)
        mid = (orh + orl) / 2.0

        # --- 1. Below Opening Range Low (Breakdown / Short Entry or Long Exit)
        if ctx.price < orl:
            if self.side in {"short_only", "long_short"}:
                if not self.quality_filters:
                    if ctx.prev_price >= orl or ctx.prev_fast >= ctx.prev_slow:
                        if ctx.rsi <= 55.0:
                            return result(
                                Signal.SELL,
                                f"ORB {self.orb_minutes}m low breakdown (Price ${ctx.price:.2f} < ORL ${orl:.2f}, RSI {ctx.rsi:.1f})",
                            )
                    return result(Signal.HOLD, f"below ORB range (ORL: ${orl:.2f}, current: ${ctx.price:.2f})")

                if blocked:
                    return result(Signal.HOLD, f"no entry — {blocked}")

                fresh_break_short = ctx.prev_price >= orl
                continuation_short = ctx.pulled_back_bearish and ctx.fast < ctx.slow and ctx.is_trending()
                if not (fresh_break_short or continuation_short):
                    return result(
                        Signal.HOLD,
                        f"breakdown already underway (ORL ${orl:.2f}) — no fresh trigger",
                    )
                if not ctx.has_breakout_volume():
                    return result(
                        Signal.HOLD,
                        f"no entry — breakdown lacks volume ({ctx.vol_ratio:.2f}× average)",
                    )
                if ctx.price < orl - BREAKOUT_CHASE_ATR * ctx.atr:
                    return result(
                        Signal.HOLD,
                        f"no entry — {((orl - ctx.price) / ctx.atr):.1f} ATR past ORL ${orl:.2f}",
                    )
                if ctx.price > ctx.vwap:
                    return result(
                        Signal.HOLD,
                        f"no entry — breakdown above VWAP ${ctx.vwap:.2f} (institutional bid present)",
                    )
                if ctx.rsi > 50.0:
                    return result(Signal.HOLD, f"no entry — bearish momentum too weak (RSI {ctx.rsi:.1f})")
                if ctx.rsi < RSI_MIN_SELL:
                    return result(Signal.HOLD, f"no entry — oversold (RSI {ctx.rsi:.1f} < {RSI_MIN_SELL:.0f})")

                return result(
                    Signal.SELL,
                    f"ORB {self.orb_minutes}m low breakdown "
                    f"(Price ${ctx.price:.2f} < ORL ${orl:.2f}, RSI {ctx.rsi:.1f}"
                    + (f", volume {ctx.vol_ratio:.2f}×" if ctx.vol_ratio is not None else "")
                    + ")",
                )
            return result(Signal.SELL, f"exit long — breakdown below ORL (${orl:.2f})")

        # --- 2. Above Opening Range High (Breakout / Long Entry or Short Exit)
        if ctx.price > orh:
            if self.side == "short_only":
                return result(Signal.BUY, f"exit short — breakdown invalid above ORH (${orh:.2f})")

            if not self.quality_filters:
                if ctx.prev_price <= orh or ctx.prev_fast <= ctx.prev_slow:
                    if ctx.rsi >= 45.0:
                        return result(
                            Signal.BUY,
                            f"ORB {self.orb_minutes}m high breakout "
                            f"(Price ${ctx.price:.2f} > ORH ${orh:.2f}, RSI {ctx.rsi:.1f})",
                        )
                return result(Signal.HOLD, f"above ORB range (ORH: ${orh:.2f}, current: ${ctx.price:.2f})")

            if blocked:
                return result(Signal.HOLD, f"no entry — {blocked}")

            fresh_break = ctx.prev_price <= orh
            continuation = ctx.pulled_back and ctx.fast > ctx.slow and ctx.is_trending()
            if not (fresh_break or continuation):
                return result(
                    Signal.HOLD,
                    f"breakout already underway (ORH ${orh:.2f}) — no fresh trigger",
                )
            if not ctx.has_breakout_volume():
                return result(
                    Signal.HOLD,
                    f"no entry — breakout lacks volume ({ctx.vol_ratio:.2f}× average)",
                )
            if ctx.price > orh + BREAKOUT_CHASE_ATR * ctx.atr:
                return result(
                    Signal.HOLD,
                    f"no entry — {((ctx.price - orh) / ctx.atr):.1f} ATR past ORH ${orh:.2f}",
                )
            if ctx.price < ctx.vwap:
                return result(
                    Signal.HOLD,
                    f"no entry — breakout below VWAP ${ctx.vwap:.2f} (no institutional bid)",
                )
            if ctx.rsi < 50.0:
                return result(Signal.HOLD, f"no entry — momentum too weak (RSI {ctx.rsi:.1f})")
            if ctx.rsi > RSI_MAX_BUY:
                return result(Signal.HOLD, f"no entry — overbought (RSI {ctx.rsi:.1f} > {RSI_MAX_BUY:.0f})")

            return result(
                Signal.BUY,
                f"ORB {self.orb_minutes}m high breakout "
                f"(Price ${ctx.price:.2f} > ORH ${orh:.2f}, RSI {ctx.rsi:.1f}"
                + (f", volume {ctx.vol_ratio:.2f}×" if ctx.vol_ratio is not None else "")
                + ")",
            )

        # --- 3. Inside Range: Exits on failed breakout or breakdown ---------
        if self.quality_filters:
            failed_long = ctx.price < orh - ctx.exit_buffer
            if failed_long and ctx.prev_price >= orh - ctx.exit_buffer:
                return result(
                    Signal.SELL,
                    f"exit long — failed breakout, back inside range (ORH ${orh:.2f})",
                )
            if self.side in {"short_only", "long_short"}:
                failed_short = ctx.price > orl + ctx.exit_buffer
                if failed_short and ctx.prev_price <= orl + ctx.exit_buffer:
                    return result(
                        Signal.BUY,
                        f"exit short — failed breakdown, back inside range (ORL ${orl:.2f})",
                    )

        if ctx.price < mid and ctx.prev_price >= mid:
            return result(
                Signal.SELL,
                f"exit long — fell back inside opening range (Mid: ${mid:.2f})",
            )
        if self.side in {"short_only", "long_short"} and ctx.price > mid and ctx.prev_price <= mid:
            return result(
                Signal.BUY,
                f"exit short — rallied back inside opening range (Mid: ${mid:.2f})",
            )

        return result(
            Signal.HOLD,
            f"inside ORB range (${orl:.2f} – ${orh:.2f}, current: ${ctx.price:.2f})",
        )

    # ----------------------------------------------------------- momentum_scalp

    def _eval_momentum_scalp(self, ctx: DayContext) -> StrategyResult:
        a, b = ctx.fast, ctx.slow

        def result(signal: Signal, reason: str) -> StrategyResult:
            return StrategyResult(signal, ctx.price, a, b, reason)

        crossed_up = ctx.prev_fast <= ctx.prev_slow and ctx.fast > ctx.slow
        crossed_down = ctx.prev_fast >= ctx.prev_slow and ctx.fast < ctx.slow

        # --- short-only mode: only short entries and covers -----------------
        if self.side == "short_only":
            lost_short_momentum = crossed_up or ctx.price > ctx.slow
            if lost_short_momentum:
                return result(
                    Signal.BUY,
                    f"momentum scalp cover / exit short (EMA{self.ema_fast} > EMA{self.ema_slow}, RSI {ctx.rsi:.1f})",
                )

            blocked = self._tradable(ctx)
            if blocked:
                return result(Signal.HOLD, f"no entry — {blocked}")

            if not self.quality_filters:
                if (crossed_down or (ctx.fast < ctx.slow and ctx.price < ctx.fast)) and ctx.rsi <= 48.0:
                    return result(
                        Signal.SELL,
                        f"momentum scalp short (EMA{self.ema_fast} < EMA{self.ema_slow}, RSI {ctx.rsi:.1f})",
                    )
                return result(Signal.HOLD, f"no momentum trigger (EMA{self.ema_fast}: {ctx.fast:.2f}, RSI {ctx.rsi:.1f})")

            if ctx.in_lunch_chop():
                return result(Signal.HOLD, "no entry — midday chop window (11:30–13:30 ET)")
            if ctx.fast >= ctx.slow:
                return result(Signal.HOLD, f"no momentum trigger (EMA{self.ema_fast}: {ctx.fast:.2f} >= EMA{self.ema_slow}: {ctx.slow:.2f})")
            if not ctx.is_trending():
                return result(Signal.HOLD, f"no entry — chop regime (ADX {ctx.adx:.0f} < {ADX_TREND_MIN:.0f})")
            if ctx.ema_separation_atr < EMA_SEPARATION_MIN_ATR:
                return result(Signal.HOLD, f"no entry — EMAs tangled ({ctx.ema_separation_atr:.2f} ATR apart)")
            if ctx.price > ctx.vwap:
                return result(Signal.HOLD, f"no entry — above VWAP ${ctx.vwap:.2f}")
            if ctx.rsi > 45.0:
                return result(Signal.HOLD, f"no entry — bearish momentum too weak (RSI {ctx.rsi:.1f})")
            if ctx.rsi < RSI_MIN_SELL:
                return result(Signal.HOLD, f"no entry — oversold (RSI {ctx.rsi:.1f} < {RSI_MIN_SELL:.0f})")
            if ctx.price < ctx.fast - TREND_CHASE_ATR * ctx.atr:
                return result(Signal.HOLD, f"no entry — extended {((ctx.fast - ctx.price) / ctx.atr):.1f} ATR below EMA{self.ema_fast}")

            resumed_down = ctx.pulled_back_bearish or (ctx.prev_price >= ctx.prev_fast and ctx.price < ctx.fast)
            if not (crossed_down or resumed_down):
                return result(Signal.HOLD, f"no fresh trigger (EMA{self.ema_fast}: {ctx.fast:.2f}, RSI {ctx.rsi:.1f})")

            trigger_short = "EMA cross down" if crossed_down else "pullback rejected"
            return result(
                Signal.SELL,
                f"momentum scalp short — {trigger_short} (EMA{self.ema_fast} < EMA{self.ema_slow}, RSI {ctx.rsi:.1f})",
            )

        # --- exits ----------------------------------------------------------
        if self.quality_filters:
            lost_momentum = crossed_down or ctx.price < ctx.slow
            if lost_momentum and self.side != "long_short":
                return result(
                    Signal.SELL,
                    f"momentum scalp exit (Price ${ctx.price:.2f} < EMA{self.ema_slow} ${ctx.slow:.2f}, RSI {ctx.rsi:.1f})",
                )
        else:
            if (crossed_down or (ctx.fast < ctx.slow and ctx.rsi <= 48.0)) and self.side != "long_short":
                return result(
                    Signal.SELL,
                    f"momentum scalp exit (EMA{self.ema_fast} < EMA{self.ema_slow}, RSI {ctx.rsi:.1f})",
                )

        # --- entry ----------------------------------------------------------
        blocked = self._tradable(ctx)

        # Long entry check
        can_long = False
        if not self.quality_filters:
            if (crossed_up or (ctx.fast > ctx.slow and ctx.price > ctx.fast)) and ctx.rsi >= 52.0:
                can_long = True
        else:
            if not blocked and not ctx.in_lunch_chop() and ctx.fast > ctx.slow and ctx.is_trending():
                if ctx.ema_separation_atr >= EMA_SEPARATION_MIN_ATR and ctx.price >= ctx.vwap:
                    if 55.0 <= ctx.rsi <= RSI_MAX_BUY and ctx.price <= ctx.fast + TREND_CHASE_ATR * ctx.atr:
                        resumed = ctx.pulled_back or (ctx.prev_price <= ctx.prev_fast and ctx.price > ctx.fast)
                        if crossed_up or resumed:
                            can_long = True

        if can_long:
            trigger = "EMA cross" if crossed_up else "pullback resumed"
            return result(
                Signal.BUY,
                f"momentum scalp long — {trigger} (EMA{self.ema_fast} > EMA{self.ema_slow}, RSI {ctx.rsi:.1f})",
            )

        # Short entry check (when in long_short)
        if self.side == "long_short":
            can_short = False
            if not self.quality_filters:
                if (crossed_down or (ctx.fast < ctx.slow and ctx.price < ctx.fast)) and ctx.rsi <= 48.0:
                    can_short = True
            else:
                if not blocked and not ctx.in_lunch_chop() and ctx.fast < ctx.slow and ctx.is_trending():
                    if ctx.ema_separation_atr >= EMA_SEPARATION_MIN_ATR and ctx.price <= ctx.vwap:
                        if RSI_MIN_SELL <= ctx.rsi <= 45.0 and ctx.price >= ctx.fast - TREND_CHASE_ATR * ctx.atr:
                            resumed_down = ctx.pulled_back_bearish or (ctx.prev_price >= ctx.prev_fast and ctx.price < ctx.fast)
                            if crossed_down or resumed_down:
                                can_short = True

            if can_short:
                trigger_short = "EMA cross down" if crossed_down else "pullback rejected"
                return result(
                    Signal.SELL,
                    f"momentum scalp short — {trigger_short} (EMA{self.ema_fast} < EMA{self.ema_slow}, RSI {ctx.rsi:.1f})",
                )

            # In long_short mode, exit long on momentum loss
            lost_momentum = crossed_down or ctx.price < ctx.slow
            if lost_momentum:
                return result(
                    Signal.SELL,
                    f"momentum scalp exit long (Price ${ctx.price:.2f} < EMA{self.ema_slow} ${ctx.slow:.2f}, RSI {ctx.rsi:.1f})",
                )

        if blocked:
            return result(Signal.HOLD, f"no entry — {blocked}")

        return result(
            Signal.HOLD,
            f"no momentum trigger (EMA{self.ema_fast}: {ctx.fast:.2f}, EMA{self.ema_slow}: {ctx.slow:.2f}, RSI {ctx.rsi:.1f})",
        )

    # ---------------------------------------------------------------- vwap_fade

    def _eval_vwap_fade(self, ctx: DayContext) -> StrategyResult:
        def result(signal: Signal, metric_b: float, reason: str) -> StrategyResult:
            return StrategyResult(signal, ctx.price, ctx.vwap, metric_b, reason)

        blocked = self._tradable(ctx)

        # --- short-only mode: only short upper-band fade and cover at midline
        if self.side == "short_only":
            if ctx.price <= ctx.vwap:
                return result(
                    Signal.BUY,
                    ctx.vwap_lower,
                    f"VWAP fade short profit target reached (Price ${ctx.price:.2f} <= VWAP ${ctx.vwap:.2f})",
                )
            if blocked:
                return result(Signal.HOLD, ctx.vwap_upper, f"no entry — {blocked}")
            if not self.quality_filters:
                oversold_short = ctx.price >= ctx.vwap_upper or (ctx.rsi >= 68.0 and ctx.price >= ctx.vwap)
                if oversold_short:
                    return result(
                        Signal.SELL,
                        ctx.vwap_upper,
                        f"VWAP fade short (Price ${ctx.price:.2f} at upper band ${ctx.vwap_upper:.2f}, RSI {ctx.rsi:.1f})",
                    )
                return result(Signal.HOLD, ctx.vwap_upper, f"no fade setup (Price ${ctx.price:.2f}, Upper: ${ctx.vwap_upper:.2f})")

            if not ctx.is_ranging():
                return result(Signal.HOLD, ctx.vwap_upper, f"no fade — market is trending (ADX {ctx.adx:.0f} > {ADX_CHOP_MAX:.0f})")
            if ctx.price < ctx.vwap_upper:
                return result(Signal.HOLD, ctx.vwap_upper, f"no fade setup (Price ${ctx.price:.2f}, Upper: ${ctx.vwap_upper:.2f})")
            if ctx.rsi < 65.0:
                return result(Signal.HOLD, ctx.vwap_upper, f"no fade — not overbought enough (RSI {ctx.rsi:.1f})")
            if not (ctx.rsi < ctx.prev_rsi and ctx.price < ctx.prev_price):
                return result(Signal.HOLD, ctx.vwap_upper, f"no fade — still rising (RSI {ctx.prev_rsi:.1f} → {ctx.rsi:.1f})")

            return result(
                Signal.SELL,
                ctx.vwap_upper,
                f"VWAP fade short — reversal confirmed (Price ${ctx.price:.2f} at upper band ${ctx.vwap_upper:.2f}, RSI {ctx.rsi:.1f})",
            )

        # --- long_only / long_short modes -----------------------------------
        # Exits for existing long fade (profit target at midline)
        if ctx.price >= ctx.vwap:
            if self.side == "long_short":
                # Check if upper band short fade triggers
                overbought_stretch = ctx.price >= ctx.vwap_upper and ctx.rsi >= 65.0 and ctx.is_ranging()
                if overbought_stretch and (not self.quality_filters or (ctx.rsi < ctx.prev_rsi and ctx.price < ctx.prev_price)):
                    return result(
                        Signal.SELL,
                        ctx.vwap_upper,
                        f"VWAP fade short — reversal confirmed (Price ${ctx.price:.2f} at upper band ${ctx.vwap_upper:.2f}, RSI {ctx.rsi:.1f})",
                    )
            return result(
                Signal.SELL,
                ctx.vwap_upper,
                f"VWAP fade profit target reached (Price ${ctx.price:.2f} >= VWAP ${ctx.vwap:.2f})",
            )

        # In long_short mode, if price is below midline, check short cover
        if self.side == "long_short" and ctx.price <= ctx.vwap_lower:
            pass  # May trigger lower band buy bounce below

        # --- entry for lower band buy ---------------------------------------
        if blocked:
            return result(Signal.HOLD, ctx.vwap_lower, f"no entry — {blocked}")

        if not self.quality_filters:
            oversold = ctx.price <= ctx.vwap_lower or (ctx.rsi <= 32.0 and ctx.price <= ctx.vwap)
            if oversold and ctx.rsi >= ctx.prev_rsi - 0.2:
                return result(
                    Signal.BUY,
                    ctx.vwap_lower,
                    f"VWAP fade buy (Price ${ctx.price:.2f} at lower band ${ctx.vwap_lower:.2f}, RSI {ctx.rsi:.1f})",
                )
            return result(
                Signal.HOLD,
                ctx.vwap_lower,
                f"no fade setup (Price ${ctx.price:.2f}, VWAP ${ctx.vwap:.2f}, Lower: ${ctx.vwap_lower:.2f})",
            )

        if not ctx.is_ranging():
            return result(
                Signal.HOLD,
                ctx.vwap_lower,
                f"no fade — market is trending (ADX {ctx.adx:.0f} > {ADX_CHOP_MAX:.0f})",
            )
        if ctx.price > ctx.vwap_lower:
            return result(
                Signal.HOLD,
                ctx.vwap_lower,
                f"no fade setup (Price ${ctx.price:.2f}, VWAP ${ctx.vwap:.2f}, Lower: ${ctx.vwap_lower:.2f})",
            )
        if ctx.rsi > 35.0:
            return result(
                Signal.HOLD, ctx.vwap_lower, f"no fade — not oversold enough (RSI {ctx.rsi:.1f})"
            )
        if not (ctx.rsi > ctx.prev_rsi and ctx.price > ctx.prev_price):
            return result(
                Signal.HOLD,
                ctx.vwap_lower,
                f"no fade — still falling (RSI {ctx.prev_rsi:.1f} → {ctx.rsi:.1f})",
            )

        return result(
            Signal.BUY,
            ctx.vwap_lower,
            f"VWAP fade buy — reversal confirmed "
            f"(Price ${ctx.price:.2f} at lower band ${ctx.vwap_lower:.2f}, RSI {ctx.rsi:.1f})",
        )
