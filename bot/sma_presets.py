"""Named SMA crossover window presets for AlgoPaca."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SmaPreset:
    id: str
    label: str
    summary: str
    fast_sma: int
    slow_sma: int


_PRESETS: tuple[SmaPreset, ...] = (
    SmaPreset(
        id="classic",
        label="Classic 10/30",
        summary="Default crossover — balanced signal frequency.",
        fast_sma=10,
        slow_sma=30,
    ),
    SmaPreset(
        id="short_term",
        label="Short-term 5/20",
        summary="Faster crosses — more signals, more noise.",
        fast_sma=5,
        slow_sma=20,
    ),
    SmaPreset(
        id="fibonacci",
        label="Fibonacci 8/21",
        summary="Fib-friendly windows popular with swing traders.",
        fast_sma=8,
        slow_sma=21,
    ),
    SmaPreset(
        id="swing",
        label="Swing 20/50",
        summary="Medium-term trend filter — fewer, cleaner crosses.",
        fast_sma=20,
        slow_sma=50,
    ),
    SmaPreset(
        id="golden_cross",
        label="Golden cross 50/200",
        summary="Classic long-term golden/death cross on daily bars.",
        fast_sma=50,
        slow_sma=200,
    ),
    SmaPreset(
        id="custom",
        label="Custom",
        summary="Set your own fast / slow windows below.",
        fast_sma=10,
        slow_sma=30,
    ),
)

_BY_ID = {p.id: p for p in _PRESETS}
DEFAULT_PRESET_ID = "classic"


def list_presets() -> list[dict[str, Any]]:
    return [asdict(p) for p in _PRESETS]


def get_preset(preset_id: str | None) -> SmaPreset:
    key = (preset_id or DEFAULT_PRESET_ID).strip().lower()
    return _BY_ID.get(key, _BY_ID[DEFAULT_PRESET_ID])


def resolve_preset_id(preset_id: str | None) -> str:
    key = (preset_id or DEFAULT_PRESET_ID).strip().lower()
    return key if key in _BY_ID else DEFAULT_PRESET_ID


def match_preset_id(fast: int, slow: int) -> str:
    """Return a named preset id when windows match exactly, else custom."""
    for preset in _PRESETS:
        if preset.id == "custom":
            continue
        if preset.fast_sma == fast and preset.slow_sma == slow:
            return preset.id
    return "custom"


def windows_for(
    preset_id: str | None,
    *,
    fast: int | None = None,
    slow: int | None = None,
) -> tuple[int, int]:
    """Resolve SMA windows for a preset.

    Named presets supply their windows unless the caller already set both
    (UI always writes windows when a preset is chosen). Custom keeps overrides.
    """
    preset = get_preset(preset_id)
    if preset.id == "custom":
        f = 10 if fast is None else int(fast)
        s = 30 if slow is None else int(slow)
        return f, s
    if fast is not None and slow is not None:
        return int(fast), int(slow)
    return preset.fast_sma, preset.slow_sma
