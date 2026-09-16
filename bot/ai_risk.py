"""Pre-trade gates and open-position management for the AI desk.

Everything here is a pure function over plain dicts so the rules can be tested
without an Alpaca connection. The trader wires them to live orders.

The AI desk used to have exactly one hard risk rule (a fixed-percent stop that
never moved). These rules add the missing half: how big a position may be, how
many may be open at once, when a stop moves up, and when the desk must sit out.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
# Legacy flat file — treated as the paper bucket when the mode-scoped files
# are absent so existing trail/TP scratch state keeps working after upgrade.
STATE_PATH = ROOT / ".ai_risk_state.json"
PAPER_STATE_PATH = ROOT / ".ai_risk_state.paper.json"
LIVE_STATE_PATH = ROOT / ".ai_risk_state.live.json"


def state_path_for(*, paper: bool = True) -> Path:
    return PAPER_STATE_PATH if paper else LIVE_STATE_PATH


# --- tiny JSON store: only what cannot be derived from broker state -----------


def _read_state(*, paper: bool = True) -> dict[str, Any]:
    path = state_path_for(paper=paper)
    legacy = STATE_PATH
    if not path.exists() and paper and legacy.exists():
        path = legacy
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(data: dict[str, Any], *, paper: bool = True) -> None:
    path = state_path_for(paper=paper)
    try:
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - disk issues should not halt trading
        logger.warning("could not persist AI risk state: %s", exc)


def load_trade_state(
    symbol: str, entry: float | None, *, paper: bool = True
) -> dict[str, Any]:
    """Per-symbol scratch state, reset whenever the entry price changes."""
    row = _read_state(paper=paper).get(symbol.upper()) or {}
    stored_entry = row.get("entry")
    if entry is None or stored_entry is None or abs(float(stored_entry) - float(entry)) > 0.01:
        return {"entry": entry, "scaled_out": False, "peak_r": 0.0}
    return {
        "entry": stored_entry,
        "scaled_out": bool(row.get("scaled_out")),
        "peak_r": float(row.get("peak_r") or 0.0),
    }


def save_trade_state(
    symbol: str, state: dict[str, Any], *, paper: bool = True
) -> None:
    data = _read_state(paper=paper)
    data[symbol.upper()] = state
    _write_state(data, paper=paper)


def clear_trade_state(symbol: str, *, paper: bool = True) -> None:
    data = _read_state(paper=paper)
    if data.pop(symbol.upper(), None) is not None:
        _write_state(data, paper=paper)


@dataclass(frozen=True)
class Gate:
    """Outcome of a rule check. `allowed=False` blocks the action."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.allowed


ALLOW = Gate(True)


def spread_bps(mark: dict[str, Any] | None) -> float | None:
    """Round-trip cost proxy in basis points, or None when no quote is available."""
    mark = mark or {}
    try:
        bid = float(mark.get("bid") or 0)
        ask = float(mark.get("ask") or 0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return round((ask - bid) / mid * 10_000, 1)


def r_multiple(
    *, side: str, entry: float | None, price: float | None, stop_distance: float | None
) -> float | None:
    """Open profit measured in units of initial risk. Negative = underwater."""
    try:
        entry_f = float(entry or 0)
        price_f = float(price or 0)
        dist = float(stop_distance or 0)
    except (TypeError, ValueError):
        return None
    if entry_f <= 0 or price_f <= 0 or dist <= 0:
        return None
    move = price_f - entry_f if side == "long" else entry_f - price_f
    return round(move / dist, 2)


def entry_gates(
    config: Any,
    context: dict[str, Any],
    *,
    open_positions: int,
    day_pl_pct: float | None,
    action: str | None = None,
) -> Gate:
    """Portfolio- and cost-level checks applied before any new position opens.

    Exits are never gated — these only block *opening* risk.
    """
    limit = float(getattr(config, "ai_daily_loss_limit_pct", 0) or 0)
    if limit > 0 and day_pl_pct is not None and day_pl_pct <= -limit:
        return Gate(
            False,
            f"Daily loss limit hit ({day_pl_pct:.2f}% <= -{limit:.2f}%) — no new risk today.",
        )

    max_pos = int(getattr(config, "ai_max_positions", 0) or 0)
    if max_pos > 0 and open_positions >= max_pos:
        return Gate(
            False,
            f"Max concurrent positions reached ({open_positions}/{max_pos}).",
        )

    max_spread = float(getattr(config, "ai_max_spread_bps", 0) or 0)
    spread = spread_bps(context.get("mark"))
    if max_spread > 0 and spread is not None and spread > max_spread:
        return Gate(
            False,
            f"Spread {spread:.1f}bps above {max_spread:.0f}bps — edge is thinner than the cost.",
        )

    cooldown = int(getattr(config, "ai_cooldown_minutes", 0) or 0)
    activity = context.get("activity") or {}
    stop_age = activity.get("stop_out_age_min")
    if cooldown > 0 and stop_age is not None and float(stop_age) < cooldown:
        return Gate(
            False,
            f"Stopped out {float(stop_age):.0f}m ago — cooling down for {cooldown}m.",
        )

    preset_id = getattr(config, "ai_preset", "")
    if preset_id == "gold_silver_macro":
        symbol = str(context.get("symbol") or "").upper().strip()
        metals_intel = context.get("precious_metals_intel") or {}
        technicals = context.get("technicals") or {}
        try:
            val_macro = metals_intel.get("macro_composite_score")
            macro_score = float(val_macro) if val_macro is not None else 0.0
        except (ValueError, TypeError):
            macro_score = 0.0

        try:
            val_gsr = metals_intel.get("gsr_z_score")
            gsr_z = float(val_gsr) if val_gsr is not None else 0.0
        except (ValueError, TypeError):
            gsr_z = 0.0

        # Strictly exclude miner & leveraged ETFs from AI Gold & Silver Macro
        if symbol in {"GDX", "GDXJ", "DUST", "UGL", "GDXU", "GDXD"}:
            return Gate(
                False,
                f"{symbol} is strictly excluded from AI Gold & Silver Macro playbook to avoid leverage decay and equity drag.",
            )

        # 1. Late-session entry filter on inverse ETFs (hour >= 19 UTC / 3 PM ET)
        if symbol in {"GLL", "ZSL", "JDST"}:
            now_utc = datetime.now(timezone.utc)
            if now_utc.hour >= 19:
                return Gate(
                    False,
                    f"Late-session entry on inverse ETF {symbol} blocked ({now_utc.strftime('%H:%M')} UTC >= 19:00 UTC) to avoid overnight gap risk.",
                )

        # Determine target side / action (default to long if opening new position and action not specified or buy)
        act = str(action or context.get("action") or "buy").lower().strip()
        is_short = act in {"sell", "short", "sell_short"}

        # 2. Dollar Mixed / Neutral Short Gate:
        if is_short or symbol in {"GLL", "ZSL", "JDST"}:
            dollar_mixed = bool(metals_intel.get("dollar_mixed") or metals_intel.get("dollar_trend") == "neutral")
            if dollar_mixed:
                return Gate(
                    False,
                    f"Short / inverse entry on {symbol} strictly prohibited: US Dollar Index data is mixed or neutral.",
                )

        # 3. GLD / bullion macro & regime gate (macro_composite_score >= 0.0, trend_regime is bullish)
        if symbol in {"GLD", "IAU", "BAR", "OUNZ", "PHYS"}:
            if not is_short:
                if macro_score < 0.0:
                    return Gate(
                        False,
                        f"GLD macro score ({macro_score:+.2f} < 0.00) is negative — waiting for constructive macro backdrop.",
                    )
                trend_regime = str(metals_intel.get("trend_regime") or "")
                price = float(technicals.get("price") or (context.get("mark") or {}).get("price") or 0.0)
                smas = technicals.get("sma") or {}
                sma200 = float(smas.get("200") or 0.0)
                sma50 = float(smas.get("50") or 0.0)
                if sma200 > 0 and price > 0 and price < sma200:
                    return Gate(
                        False,
                        f"GLD price (${price:.2f}) is below 200 SMA (${sma200:.2f}) — long entries strictly prohibited in bear regime.",
                    )
                if trend_regime == "bearish_below_sma200":
                    return Gate(
                        False,
                        f"GLD trend regime is '{trend_regime}' — long entries strictly prohibited in bear regime.",
                    )

        # 4. SLV / silver catch-up & regime gate (macro >= 0.2 or gsr_z >= 0.8)
        if symbol in {"SLV", "AGQ", "SIL", "SILJ", "PSLV"}:
            if not is_short:
                if macro_score < 0.2 and gsr_z < 0.8:
                    return Gate(
                        False,
                        f"SLV macro score ({macro_score:+.2f} < 0.20) and GSR z ({gsr_z:+.2f} < 0.80) are unsupportive for silver catch-up.",
                    )
                trend_regime = str(metals_intel.get("trend_regime") or "")
                price = float(technicals.get("price") or (context.get("mark") or {}).get("price") or 0.0)
                smas = technicals.get("sma") or {}
                sma200 = float(smas.get("200") or 0.0)
                if sma200 > 0 and price > 0 and price < sma200:
                    return Gate(
                        False,
                        f"SLV price (${price:.2f}) is below 200 SMA (${sma200:.2f}) — long entries strictly prohibited in bear regime.",
                    )
                if trend_regime == "bearish_below_sma200":
                    return Gate(
                        False,
                        f"SLV trend regime is '{trend_regime}' — long entries strictly prohibited in bear regime.",
                    )

        # 5. RSI Pullback Zone Gate for Long Bullion (prevent chasing overextended breakouts):
        if symbol in {"GLD", "SLV", "IAU", "BAR", "PHYS"} and not is_short:
            rsi = technicals.get("rsi_14")
            adx = technicals.get("adx_14")
            if rsi is not None and float(rsi) > 0:
                rsi_f = float(rsi)
                adx_f = float(adx or 20.0)
                strong_bull = (adx_f >= 25.0) or (macro_score >= 1.5)
                pullback_rsi_max = 65.0 if strong_bull else 58.0
                price = float(technicals.get("price") or (context.get("mark") or {}).get("price") or 0.0)
                smas = technicals.get("sma") or {}
                sma20 = float(smas.get("20") or 0.0)
                pullback_sma_thresh = (sma20 * 1.015) if strong_bull else (sma20 * 1.01) if sma20 > 0 else price
                in_pullback = (38.0 <= rsi_f <= pullback_rsi_max) or (sma20 > 0 and price <= pullback_sma_thresh)
                if not in_pullback and rsi_f > pullback_rsi_max and (sma20 > 0 and price > pullback_sma_thresh):
                    return Gate(
                        False,
                        f"{symbol} RSI ({rsi_f:.1f}) is extended above pullback zone (max {pullback_rsi_max:.1f}) — breakout chase prohibited.",
                    )

        # 6. Overextended Move Gate (dist_sma50_atr > 3.2 for long or < -3.2 for short):
        dist_sma50 = technicals.get("dist_sma50_atr")
        if dist_sma50 is None:
            dist_sma50 = technicals.get("dist_sma50")
        if dist_sma50 is not None:
            dist_f = float(dist_sma50)
            if not is_short and dist_f > 3.2:
                return Gate(
                    False,
                    f"{symbol} dist_sma50_atr ({dist_f:+.2f}) is overextended > +3.20 ATR — long entry prohibited.",
                )
            if is_short and dist_f < -3.2:
                return Gate(
                    False,
                    f"{symbol} dist_sma50_atr ({dist_f:+.2f}) is overextended < -3.20 ATR — short entry prohibited.",
                )

    return ALLOW


def reversal_gate(config: Any, context: dict[str, Any], confidence: float) -> Gate:
    """Guards that only apply when flipping or closing an existing position.

    Stops churn: a fresh position may not be reversed on a marginal signal, and
    a reversal needs more conviction than an open did.
    """
    intel = context.get("precious_metals_intel") or {}
    pos = context.get("position") or {}
    pos_qty = float(pos.get("qty") or 0.0)
    # Allow prompt reversal of short on precious metals when dollar index / macro data is mixed
    if intel.get("dollar_mixed") and pos_qty < 0:
        return ALLOW
    activity = context.get("activity") or {}
    min_hold = int(getattr(config, "ai_min_hold_minutes", 0) or 0)
    age = activity.get("last_fill_age_min")
    if min_hold > 0 and age is not None and float(age) < min_hold:
        return Gate(
            False,
            f"Position is {float(age):.0f}m old — min hold is {min_hold}m (stop still protects).",
        )

    bump = float(getattr(config, "ai_reversal_conf_bump", 0) or 0)
    needed = float(getattr(config, "ai_min_confidence", 0) or 0) + bump
    if bump > 0 and confidence < needed:
        return Gate(
            False,
            f"Reversal needs confidence >= {needed:.2f} (got {confidence:.2f}).",
        )
    return ALLOW


def desired_stop(
    config: Any,
    *,
    side: str,
    entry: float | None,
    price: float | None,
    stop_distance: float | None,
    current_stop: float | None = None,
) -> dict[str, Any] | None:
    """Where the protective stop should sit right now.

    Below `ai_trail_after_r` the initial stop stands. At or above it the stop
    jumps to breakeven and then trails one stop-distance behind price. A stop
    never moves against the position, so this only ever locks in more.
    """
    try:
        entry_f = float(entry or 0)
        price_f = float(price or 0)
        dist = float(stop_distance or 0)
    except (TypeError, ValueError):
        return None
    if entry_f <= 0 or price_f <= 0 or dist <= 0 or side not in {"long", "short"}:
        return None

    is_long = side == "long"
    initial = entry_f - dist if is_long else entry_f + dist
    target = initial
    stage = "initial"

    trail_after = float(getattr(config, "ai_trail_after_r", 0) or 0)
    r = r_multiple(side=side, entry=entry_f, price=price_f, stop_distance=dist)
    if trail_after > 0 and r is not None and r >= trail_after:
        trailed = price_f - dist if is_long else price_f + dist
        # Breakeven acts as a floor: once the trade has paid for its own risk it
        # is not allowed to become a loser again.
        target = max(entry_f, trailed) if is_long else min(entry_f, trailed)
        stage = "trailing" if (
            (is_long and trailed > entry_f) or (not is_long and trailed < entry_f)
        ) else "breakeven"

    if current_stop is not None:
        try:
            existing = float(current_stop)
        except (TypeError, ValueError):
            existing = 0.0
        if existing > 0:
            target = max(target, existing) if is_long else min(target, existing)

    target = round(target, 2)
    # Alpaca rejects a stop on the wrong side of the market.
    if is_long and target >= price_f:
        target = round(price_f - 0.01, 2)
    if not is_long and target <= price_f:
        target = round(price_f + 0.01, 2)
    if target <= 0:
        return None
    return {"stop_price": target, "stage": stage, "r": r}


def should_scale_out(
    config: Any,
    *,
    r: float | None,
    already_scaled: bool,
    target_r: float | None = None,
) -> bool:
    """True when the position has earned its take-profit trim and has not taken it.

    ``target_r`` overrides the desk-wide ``ai_take_profit_r`` for engines that carry
    their own profit target (Day Trading's ``day_profit_target_r``).
    """
    if target_r is not None:
        target = float(target_r or 0)
    else:
        target = float(getattr(config, "ai_take_profit_r", 0) or 0)
    if target <= 0 or already_scaled or r is None:
        return False
    return float(r) >= target


def should_scale_out_tier(
    config: Any,
    *,
    r: float | None,
    scale_tier: int,
    adx: float | None = None,
    macro_score: float | None = None,
) -> tuple[bool, int, float, str]:
    """Evaluate multi-tier profit scaling for precious metals and legacy presets.

    Returns:
        (should_scale, next_tier, trim_fraction, reason)
    """
    if r is None or float(r) <= 0:
        return False, scale_tier, 0.0, ""

    r_f = float(r)
    preset_id = getattr(config, "ai_preset", "")

    if preset_id != "gold_silver_macro":
        target_r = float(getattr(config, "ai_take_profit_r", 2.0) or 2.0)
        if scale_tier == 0 and r_f >= target_r:
            return True, 1, 0.5, f"Take-profit reached @ +{r_f:.1f}R"
        return False, scale_tier, 0.0, ""

    # Precious Metals (gold_silver_macro) 3-Tier Scaling:
    base_tp_r = float(getattr(config, "ai_take_profit_r", 3.2) or 3.2)
    is_strong = (adx is not None and float(adx) >= 28.0) or (macro_score is not None and float(macro_score) >= 1.5)
    tier2_target_r = 4.0 if is_strong else base_tp_r

    # Tier 1: at 2.0R, scale out 25% of initial, lock stop at breakeven
    if scale_tier == 0 and r_f >= 2.0:
        return True, 1, 0.25, "Tier 1 (+2.0R): Lock 25% profit and ratchet stop to breakeven"

    # Tier 2: at 3.2R (or 4.0R if strong trend), scale out another 25% of initial (~33% of remaining)
    if scale_tier == 1 and r_f >= tier2_target_r:
        tag = f"ADX={float(adx):.1f} expanded " if is_strong and adx else ""
        return True, 2, 0.3333, f"Tier 2 (+{tier2_target_r:.1f}R): Scale out 25% at {tag}take-profit target"

    return False, scale_tier, 0.0, ""


def confidence_scaled_qty(
    config: Any, qty: float, confidence: float, *, floor: float = 0.35
) -> float:
    """Shrink size toward `floor` as confidence approaches the minimum threshold.

    Self-reported LLM confidence is poorly calibrated, so it is a better size
    dial than an on/off switch.
    """
    min_conf = float(getattr(config, "ai_min_confidence", 0) or 0)
    if qty <= 0:
        return 0.0
    headroom = 1.0 - min_conf
    if headroom <= 0:
        return float(qty)
    scale = (float(confidence) - min_conf) / headroom
    scale = max(floor, min(1.0, scale))
    scaled = float(qty) * scale
    # Shorts and extended-hours orders need whole shares, so scaling a one-share
    # trade down to a fraction would silently cancel it rather than de-risk it.
    if qty >= 1 and scaled < 1:
        return 1.0
    return scaled
