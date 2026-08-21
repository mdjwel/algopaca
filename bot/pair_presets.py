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
