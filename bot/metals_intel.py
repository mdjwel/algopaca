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
from typing import Any

import pandas as pd

from bot.client import AlpacaService
from bot.econ_calendar import fetch_economic_calendar

logger = logging.getLogger(__name__)

# Core tradeable precious metals & miners symbols on Alpaca
GOLD_SYMBOLS = {"GLD", "IAU", "UGL", "GLL", "PHYS"}
SILVER_SYMBOLS = {"SLV", "AGQ", "ZSL", "SIL", "SILJ", "PSLV"}
MINER_SYMBOLS = {"GDX", "GDXJ"}
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

# Dollar proxy: a level z-score beats the momentum test the old code used.
DOLLAR_Z_WINDOW = 60
DOLLAR_Z_SCALE = 1.5

# Regime participation filter.
TREND_WINDOW = 200

# Miners ratio is reported for context; it carries no score weight.
MINERS_Z_WINDOW = 60
MINERS_Z_SCALE = 1.5

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
    win = min(int(window), len(series))
    if win < 3:
        return None
    mean = float(series.rolling(win).mean().iloc[-1])
    std = float(series.rolling(win).std().iloc[-1])
    if not std or std <= 1e-9 or pd.isna(std) or pd.isna(mean):
        return None
    return float((float(series.iloc[-1]) - mean) / std)


def momentum(series: pd.Series | None, periods: int) -> float | None:
    """Fractional return over `periods` bars, shortened when history is thin."""
    if series is None or len(series) < 2:
        return None
    step = min(int(periods), len(series) - 1)
    if step < 1:
        return None
    past = float(series.iloc[-1 - step])
    if past <= 0:
        return None
    return float(series.iloc[-1]) / past - 1.0


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


def score_dollar(uup_z: float | None) -> float | None:
    """A cheap dollar helps gold — weakly. Sign is inverted, weight is small."""
    if uup_z is None:
        return None
    return clip_unit(-uup_z / DOLLAR_Z_SCALE)


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
) -> dict[str, Any]:
    """Build real-time precious metals analytics including GSR and macro catalyst checks."""
    sym = symbol.upper().strip()
    gold_sym = "GLD"
    silver_sym = "SLV"

    # 1. Fetch live mark prices for GLD and SLV
    gld_price = 0.0
    slv_price = 0.0
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

    # 2. Compute live GSR
    live_gsr = calculate_gsr(gld_price, slv_price)

    # 3. Daily history for the factor windows
    gld_bars, gld_close = _fetch_closes(service, gold_sym, _METAL_BAR_LIMIT)
    _, slv_close = _fetch_closes(service, silver_sym, _METAL_BAR_LIMIT)
    _, uup_close = _fetch_closes(service, "UUP", _MACRO_BAR_LIMIT)
    _, tlt_close = _fetch_closes(service, "TLT", _MACRO_BAR_LIMIT)
    _, gdx_close = _fetch_closes(service, "GDX", _MACRO_BAR_LIMIT)

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
    dollar_z = zscore(uup_close, DOLLAR_Z_WINDOW)
    dollar_score = score_dollar(dollar_z)
    trend_score = score_trend(gld_close)

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
    if dollar_score is None:
        dollar_trend = "unknown"
    elif dollar_score > 0.25:
        dollar_trend = "falling"
    elif dollar_score < -0.25:
        dollar_trend = "rising"
    else:
        dollar_trend = "neutral"

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

    # 7. Filter Macro Events relevant to Precious Metals
    cal = (
        calendar
        if calendar is not None
        else fetch_economic_calendar(hours_ahead=48, hours_behind=8)
    )
    relevant_events: list[dict[str, Any]] = []
    imminent_risk = False
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
                    if 0 <= minutes_diff <= 45 and impact == "High":
                        imminent_risk = True
                except Exception:
                    pass
            relevant_events.append(event_copy)

    macro_risk_level = "imminent_release" if imminent_risk else ("elevated" if relevant_events else "normal")

    factor_scores = {
        "rates": None if rates_score is None else round(rates_score, 3),
        "gsr": None if gsr_score is None else round(gsr_score, 3),
        "trend": None if trend_score is None else round(trend_score, 3),
        "dollar": None if dollar_score is None else round(dollar_score, 3),
        "miners_unweighted": None if miners_z is None else round(clip_unit(miners_z / MINERS_Z_SCALE), 3),
    }

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
        "yield_trend": yield_trend,
        "trend_regime": trend_regime,
        "miners_signal": miners_signal,
        "gdx_gld_ratio": gdx_gld_ratio,
        "factor_scores": factor_scores,
        "factor_weights": dict(FACTOR_WEIGHTS),
        "macro_composite_score": macro_composite_score,
        "metals_macro_bias": metals_macro_bias,
        "macro_risk_level": macro_risk_level,
        "relevant_macro_events": relevant_events[:5],
    }
