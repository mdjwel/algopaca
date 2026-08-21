"""Long/short regime-impulse pair strategy.

Researched on inverse leveraged ETFs (split-adjusted daily bars, ~2022–2026):
hold the long leg by default; rotate into the short/inverse leg only on confirmed
bear impulses (price below SMA and a sharp N-day drop). Works for any two
symbols the user supplies (e.g. SOXL/SOXS, TQQQ/SQQQ).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from bot.pair_presets import normalize_weak_side


class PairTarget(str, Enum):
    LONG = "long"
    SHORT = "short"
    CASH = "cash"


@dataclass(frozen=True)
class PairStrategyResult:
    target: PairTarget
    long_symbol: str
    short_symbol: str
    symbol: str | None  # concrete ticker to hold, or None for cash
    price: float
    sma: float
    lookback_return_pct: float
    reason: str


def adjust_inverse_splits(
    long_closes: pd.Series,
    short_closes: pd.Series,
    *,
    jump_threshold: float = 1.5,
    other_max: float = 0.40,
) -> tuple[pd.Series, list[dict]]:
    """Backward-adjust reverse splits on the short leg via pair mismatch.

    Leveraged inverse ETFs sometimes reverse-split with huge positive gaps while
    the long leg does not crash proportionally. Unadjusted gaps poison short-leg
    holding returns.
    """
    long_px = long_closes.astype(float).sort_index()
    short_px = short_closes.astype(float).sort_index()
    idx = long_px.index.intersection(short_px.index)
    long_px = long_px.loc[idx]
    short_px = short_px.loc[idx]
    rl = long_px.pct_change()
    rs = short_px.pct_change()

    factors: list[tuple[pd.Timestamp, int]] = []
    nice = (2, 3, 4, 5, 8, 10, 15, 20, 25)
    for ts in short_px.index[1:]:
        a, b = rl.loc[ts], rs.loc[ts]
        if pd.isna(a) or pd.isna(b):
            continue
        if abs(float(b)) > jump_threshold and abs(float(a)) < other_max:
            ratio = 1.0 + float(b)
            factor = max(2, int(round(ratio)))
            for cand in nice:
                if abs(ratio - cand) / cand < 0.15:
                    factor = cand
                    break
            factors.append((ts, factor))

    adj = short_px.copy()
    for ts, factor in sorted(factors, reverse=True):
        mask = adj.index < ts
        adj.loc[mask] = adj.loc[mask] * float(factor)

    meta = [
        {"time": ts.isoformat() if hasattr(ts, "isoformat") else str(ts), "factor": f}
        for ts, f in factors
    ]
    return adj, meta


def parse_pair_symbols(
    symbols: str | tuple[str, ...] | list[str] | None = None,
    *,
    long_symbol: str | None = None,
    short_symbol: str | None = None,
) -> tuple[str, str]:
    """Resolve long/short legs from an explicit pair or a two-symbol list."""
    long_s = str(long_symbol or "").strip().upper()
    short_s = str(short_symbol or "").strip().upper()
    if long_s and short_s and long_s != short_s:
        return long_s, short_s

    parts: list[str] = []
    if isinstance(symbols, (tuple, list)):
        parts = [str(p).strip().upper() for p in symbols if str(p).strip()]
    elif symbols:
        raw = str(symbols).replace(";", ",").replace("\n", ",")
        parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    # Dedupe while preserving order.
    parts = list(dict.fromkeys(parts))
    if len(parts) >= 2 and parts[0] != parts[1]:
        return parts[0], parts[1]
    raise ValueError(
        "Long & Short Pair needs exactly two different symbols "
        "(long leg first, short leg second)"
    )


class SoxRegimeImpulseStrategy:
    """Long-leg default; short leg only on SMA-below + N-day crash impulse."""

    def __init__(
        self,
        *,
        sma_period: int = 50,
        lookback: int = 7,
        impulse_pct: float = 5.0,
        weak_side: str = "LONG",
        long_symbol: str,
        short_symbol: str,
    ) -> None:
        if sma_period < 2:
            raise ValueError("sma_period must be >= 2")
        if lookback < 1:
            raise ValueError("lookback must be >= 1")
        if impulse_pct <= 0 or impulse_pct >= 100:
            raise ValueError("impulse_pct must be in (0, 100)")
        long_s = str(long_symbol or "").strip().upper()
        short_s = str(short_symbol or "").strip().upper()
        if not long_s or not short_s:
            raise ValueError("long_symbol and short_symbol are required")
        if long_s == short_s:
            raise ValueError("long_symbol and short_symbol must differ")
        weak_raw = str(weak_side or "LONG").strip().upper()
        if weak_raw not in {"SOXL", "CASH", "LONG", long_s}:
            raise ValueError("weak_side must be LONG or CASH")
        self.sma_period = int(sma_period)
        self.lookback = int(lookback)
        self.impulse_pct = float(impulse_pct)
        self.weak_side = normalize_weak_side(weak_side)
        self.long_symbol = long_s
        self.short_symbol = short_s

    @property
    def bars_needed(self) -> int:
        return max(self.sma_period, self.lookback) + 2

    def evaluate(self, long_bars: pd.DataFrame) -> PairStrategyResult:
        """Decide target from the long-leg OHLCV series (needs `close`)."""
        long_s = self.long_symbol
        short_s = self.short_symbol
        if long_bars is None or long_bars.empty or "close" not in long_bars.columns:
            return PairStrategyResult(
                PairTarget.CASH,
                long_s,
                short_s,
                None,
                0.0,
                0.0,
                0.0,
                "no bar data",
            )

        closes = long_bars["close"].astype(float).sort_index()
        price = float(closes.iloc[-1])
        need = self.bars_needed
        if len(closes) < need:
            return PairStrategyResult(
                PairTarget.CASH,
                long_s,
                short_s,
                None,
                price,
                0.0,
                0.0,
                f"need at least {need} bars, got {len(closes)}",
            )

        sma = float(closes.rolling(self.sma_period).mean().iloc[-1])
        prev = float(closes.iloc[-(self.lookback + 1)])
        look_ret = (price / prev - 1.0) * 100.0 if prev else 0.0
        thr = -abs(self.impulse_pct)

        if not np.isfinite(sma) or sma <= 0:
            return PairStrategyResult(
                PairTarget.CASH,
                long_s,
                short_s,
                None,
                price,
                0.0,
                look_ret,
                "SMA unavailable",
            )

        if price >= sma:
            return PairStrategyResult(
                PairTarget.LONG,
                long_s,
                short_s,
                long_s,
                price,
                sma,
                look_ret,
                (
                    f"bull regime ({long_s} ≥ SMA{self.sma_period}; "
                    f"{self.lookback}d {look_ret:+.1f}%)"
                ),
            )

        if look_ret <= thr:
            return PairStrategyResult(
                PairTarget.SHORT,
                long_s,
                short_s,
                short_s,
                price,
                sma,
                look_ret,
                (
                    f"bear impulse → {short_s} "
                    f"({long_s} < SMA{self.sma_period}, "
                    f"{self.lookback}d {look_ret:+.1f}% ≤ {thr:.1f}%)"
                ),
            )

        if self.weak_side == "CASH":
            return PairStrategyResult(
                PairTarget.CASH,
                long_s,
                short_s,
                None,
                price,
                sma,
                look_ret,
                (
                    f"below SMA{self.sma_period} without impulse "
                    f"({self.lookback}d {look_ret:+.1f}%) → cash"
                ),
            )

        return PairStrategyResult(
            PairTarget.LONG,
            long_s,
            short_s,
            long_s,
            price,
            sma,
            look_ret,
            (
                f"below SMA{self.sma_period} but no impulse "
                f"({self.lookback}d {look_ret:+.1f}%) → stay {long_s}"
            ),
        )

    def signal_series(self, long_closes: pd.Series) -> pd.Series:
        """Vectorized targets for backtests: LONG/SHORT/CASH labels."""
        closes = long_closes.astype(float).sort_index()
        sma = closes.rolling(self.sma_period).mean()
        look = closes.pct_change(self.lookback) * 100.0
        thr = -abs(self.impulse_pct)
        out: list[str] = []
        for ts in closes.index:
            s = sma.loc[ts]
            r = look.loc[ts]
            p = closes.loc[ts]
            if pd.isna(s) or pd.isna(r):
                out.append(PairTarget.CASH.value)
            elif float(p) >= float(s):
                out.append(PairTarget.LONG.value)
            elif float(r) <= thr:
                out.append(PairTarget.SHORT.value)
            elif self.weak_side == "CASH":
                out.append(PairTarget.CASH.value)
            else:
                out.append(PairTarget.LONG.value)
        return pd.Series(out, index=closes.index, dtype=str)
