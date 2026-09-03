"""Named buy-the-dip strategy presets for AlgoPaca."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DipPreset:
    id: str
    label: str
    summary: str
    rsi_buy: float
    rsi_sell: float
    # Skip buys when the SMA stack is bearish (avoids catching knives).
    skip_bearish: bool = True
    # When False, entries require RSI ≤ rsi_buy (no lower-BB-only buys).
    use_lower_band: bool = True


_PRESETS: tuple[DipPreset, ...] = (
    DipPreset(
        id="deep",
        label="Deep dip",
        summary="Buy RSI ≤30 or lower BB; exit RSI ≥60 or upper band.",
        rsi_buy=30.0,
        rsi_sell=60.0,
        skip_bearish=True,
        use_lower_band=True,
    ),
    DipPreset(
        id="mild",
        label="Mild pullback",
        summary="Shallower dips — RSI ≤35 or lower BB; exit RSI ≥55 or upper band.",
        rsi_buy=35.0,
        rsi_sell=55.0,
        skip_bearish=True,
        use_lower_band=True,
    ),
    DipPreset(
        id="washout",
        label="Washout",
        summary="Capitulation only — RSI ≤25 (no BB-only entries); exit RSI ≥65 or upper band. Allows bearish trend.",
        rsi_buy=25.0,
        rsi_sell=65.0,
        skip_bearish=False,
        use_lower_band=False,
    ),
    DipPreset(
        id="gold_dip",
        label="Gold Bullion Dip Hunter",
        summary="Buy secular gold bull pullbacks at RSI ≤45; hold until genuinely overbought at RSI ≥80. Filters out bearish downtrends.",
        rsi_buy=45.0,
        rsi_sell=80.0,
        skip_bearish=True,
        use_lower_band=True,
    ),
    DipPreset(
        id="custom",
        label="Custom",
        summary="Set your own RSI buy / sell thresholds below (also buys lower BB).",
        rsi_buy=30.0,
        rsi_sell=60.0,
        skip_bearish=True,
        use_lower_band=True,
    ),
)

_BY_ID = {p.id: p for p in _PRESETS}
DEFAULT_PRESET_ID = "deep"


def list_presets() -> list[dict[str, Any]]:
    return [asdict(p) for p in _PRESETS]


def get_preset(preset_id: str | None) -> DipPreset:
    key = (preset_id or DEFAULT_PRESET_ID).strip().lower()
    return _BY_ID.get(key, _BY_ID[DEFAULT_PRESET_ID])


def resolve_preset_id(preset_id: str | None) -> str:
    key = (preset_id or DEFAULT_PRESET_ID).strip().lower()
    return key if key in _BY_ID else DEFAULT_PRESET_ID


def match_preset_id(rsi_buy: float, rsi_sell: float, skip_bearish: bool) -> str:
    for preset in _PRESETS:
        if preset.id == "custom":
            continue
        if (
            abs(preset.rsi_buy - rsi_buy) < 0.05
            and abs(preset.rsi_sell - rsi_sell) < 0.05
            and preset.skip_bearish == skip_bearish
        ):
            return preset.id
    return "custom"
