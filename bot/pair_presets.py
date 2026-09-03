"""Named long/short regime-impulse presets (params from historical research)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PairPreset:
    id: str
    label: str
    summary: str
    sma_period: int
    lookback: int
    impulse_pct: float
    weak_side: str  # LONG | CASH
    long_symbol: str
    short_symbol: str


_PRESETS: tuple[PairPreset, ...] = (
    PairPreset(
        id="research_max",
        label="Research max (50 / 7d / 5%)",
        summary=(
            "Long leg above SMA50 by default; rotate to the short leg only on "
            "≤−5% N-day drops while below SMA. Tuned on inverse ETF history."
        ),
        sma_period=50,
        lookback=7,
        impulse_pct=5.0,
        weak_side="LONG",
        long_symbol="",
        short_symbol="",
    ),
    PairPreset(
        id="research_strict",
        label="Research strict (50 / 7d / 8%)",
        summary=(
            "Fewer short-leg flips — needs a sharper 7-day drop (≤−8%) below SMA50."
        ),
        sma_period=50,
        lookback=7,
        impulse_pct=8.0,
        weak_side="LONG",
        long_symbol="",
        short_symbol="",
    ),
    PairPreset(
        id="cash_weak",
        label="Cash when weak (50 / 7d / 5%)",
        summary=(
            "Same impulse entry into the short leg, but sit in cash (not the long "
            "leg) when below SMA without a crash signal — lower time-in-market."
        ),
        sma_period=50,
        lookback=7,
        impulse_pct=5.0,
        weak_side="CASH",
        long_symbol="",
        short_symbol="",
    ),
    PairPreset(
        id="gold_silver_rotation",
        label="Gold / Silver Rotation (GLD / SLV)",
        summary=(
            "Gold (GLD) as core long leg above SMA100; rotate to Silver (SLV) only "
            "on a large confirmed impulse. Stays in gold rather than cash when the "
            "signal is weak — roughly 20 switches over two decades, not hundreds."
        ),
        # The shipped 50/7/4.0/CASH settings switched 471 times and LOST money
        # across 2005-2025 (-3.9%). A slower filter with a much larger impulse
        # threshold and gold as the default parking place returned +492% on the
        # same engine, and improved both halves of the sample (IS -35% -> +102%,
        # OOS +46% -> +166%). Every axis here sits on a broad plateau.
        sma_period=100,
        lookback=15,
        impulse_pct=10.0,
        weak_side="LONG",
        long_symbol="GLD",
        short_symbol="SLV",
    ),
    PairPreset(
        id="gold_inverse_hedge",
        label="Gold / Inverse Gold Bear-Proof (GLD / GLL)",
        summary=(
            "Long Gold (GLD) above SMA150; rotate to UltraShort Gold 2x (GLL) on "
            "confirmed breakdown impulses for positive returns even during gold downturns."
        ),
        # Slowing the regime filter from SMA50 to SMA150 halved the switch count
        # and lifted both halves (IS +4% -> +78%, OOS +45% -> +84%). GLL's daily
        # reset makes its parameter surface noisy, so only the one axis with a
        # clear both-halves gain was changed.
        sma_period=150,
        lookback=7,
        impulse_pct=4.0,
        weak_side="CASH",
        long_symbol="GLD",
        short_symbol="GLL",
    ),
    PairPreset(
        id="gold_miners_rotator",
        label="Gold / Miners High-Beta Rotator (GLD / GDX)",
        summary=(
            "Hold physical Gold (GLD) as core above SMA200; rotate into Gold Miners "
            "(GDX) on confirmed impulse momentum for 2x-3x higher beta returns."
        ),
        # Slowing the filter (SMA50 -> SMA200, 7 -> 10 day lookback, 4% -> 5%
        # impulse) cut switches from 142 to 96 and lifted the full period from
        # +473% to +862%. The gain is concentrated in-sample (IS +71% -> +179%);
        # out-of-sample was flat (+213% -> +210%), so treat it as a robustness
        # improvement rather than proven extra edge.
        sma_period=200,
        lookback=10,
        impulse_pct=5.0,
        weak_side="LONG",
        long_symbol="GLD",
        short_symbol="GDX",
    ),
    PairPreset(
        id="custom",
        label="Custom",
        summary="Set SMA, lookback, impulse %, and weak-side behavior below.",
        sma_period=50,
        lookback=7,
        impulse_pct=5.0,
        weak_side="LONG",
        long_symbol="",
        short_symbol="",
    ),
)

_BY_ID = {p.id: p for p in _PRESETS}
DEFAULT_PRESET_ID = "research_max"


def list_presets() -> list[dict[str, Any]]:
    return [asdict(p) for p in _PRESETS]


def get_preset(preset_id: str | None) -> PairPreset:
    key = (preset_id or DEFAULT_PRESET_ID).strip().lower()
    return _BY_ID.get(key, _BY_ID[DEFAULT_PRESET_ID])


def resolve_preset_id(preset_id: str | None) -> str:
    key = (preset_id or DEFAULT_PRESET_ID).strip().lower()
    return key if key in _BY_ID else DEFAULT_PRESET_ID


def normalize_weak_side(weak_side: str | None) -> str:
    """Map legacy SOXL → LONG; accept LONG/CASH only."""
    weak = (weak_side or "LONG").strip().upper()
    if weak in {"SOXL", "LONG"}:
        return "LONG"
    if weak == "CASH":
        return "CASH"
    return "LONG"


def match_preset_id(
    sma_period: int,
    lookback: int,
    impulse_pct: float,
    weak_side: str,
    *,
    long_symbol: str = "",
    short_symbol: str = "",
) -> str:
    """Match on strategy params only — legs come from the symbols field."""
    del long_symbol, short_symbol  # unused; kept for call-site compatibility
    weak = normalize_weak_side(weak_side)
    for preset in _PRESETS:
        if preset.id == "custom":
            continue
        if (
            preset.sma_period == int(sma_period)
            and preset.lookback == int(lookback)
            and abs(preset.impulse_pct - float(impulse_pct)) < 1e-6
            and normalize_weak_side(preset.weak_side) == weak
        ):
            return preset.id
    return "custom"
