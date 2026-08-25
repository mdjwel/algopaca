"""Load configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

from bot.ai_models import DEFAULT_GEMINI_MODEL, DEFAULT_OPENAI_MODEL
from bot.ai_presets import (
    DEFAULT_PRESET_ID,
    instructions_for,
    resolve_preset_id,
    risk_profile_for,
)
from bot.env_store import mask_secret
from bot.dip_presets import (
    DEFAULT_PRESET_ID as DEFAULT_DIP_PRESET_ID,
    get_preset as get_dip_preset,
    match_preset_id as match_dip_preset_id,
    resolve_preset_id as resolve_dip_preset_id,
)
from bot.pair_presets import (
    DEFAULT_PRESET_ID as DEFAULT_PAIR_PRESET_ID,
    get_preset as get_pair_preset,
    match_preset_id as match_pair_preset_id,
    normalize_weak_side,
    resolve_preset_id as resolve_pair_preset_id,
)
from bot.sma_presets import (
    DEFAULT_PRESET_ID as DEFAULT_SMA_PRESET_ID,
    get_preset as get_sma_preset,
    match_preset_id,
    resolve_preset_id as resolve_sma_preset_id,
)
from bot.options_chain import normalize_options_style
from dotenv import load_dotenv

load_dotenv()


def _e(name: str, default: str = "") -> str:
    """Read a config key from the process env."""
    value = os.getenv(name, default)
    if value is None:
        return default
    return value


def env_flag(name: str, default: bool = False) -> bool:
    raw = _e(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def paper_mode_from_env() -> bool:
    """True when the desk is pointed at Alpaca paper endpoints (default)."""
    return env_flag("ALPACA_PAPER", True)


def live_allowed_from_env() -> bool:
    """Kill-switch: Live credentials/mode are ignored unless this is true."""
    return env_flag("ALPACA_ALLOW_LIVE", False)


def resolve_alpaca_credentials(
    *, paper: bool | None = None
) -> tuple[str, str, bool]:
    """Return ``(api_key, secret_key, paper)`` for the selected environment.

    Paper keys stay on the legacy ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``
    pair (optional ``ALPACA_PAPER_*`` aliases). Live keys use the dedicated
    ``ALPACA_LIVE_*`` slots so paper credentials are never promoted silently.
    """
    use_paper = paper_mode_from_env() if paper is None else bool(paper)
    if use_paper:
        api_key = (
            (_e("ALPACA_PAPER_API_KEY", "") or "").strip()
            or (_e("ALPACA_API_KEY", "") or "").strip()
        )
        secret_key = (
            (_e("ALPACA_PAPER_SECRET_KEY", "") or "").strip()
            or (_e("ALPACA_SECRET_KEY", "") or "").strip()
        )
        return api_key, secret_key, True
    api_key = (_e("ALPACA_LIVE_API_KEY", "") or "").strip()
    secret_key = (_e("ALPACA_LIVE_SECRET_KEY", "") or "").strip()
    return api_key, secret_key, False


def alpaca_slot_status(*, paper: bool) -> dict[str, Any]:
    """Masked presence info for one credential slot (paper or live)."""
    api_key, secret_key, _ = resolve_alpaca_credentials(paper=paper)
    return {
        "set": bool(api_key and secret_key),
        "api_key_set": bool(api_key),
        "secret_set": bool(secret_key),
        "api_key_hint": mask_secret(api_key) if api_key else "",
        "secret_hint": mask_secret(secret_key) if secret_key else "",
    }


def _parse_symbols(raw: str) -> tuple[str, ...]:
    parts = [p.strip().upper() for p in raw.replace(";", ",").split(",")]
    symbols = tuple(dict.fromkeys(p for p in parts if p))
    return symbols or ("AAPL",)


def normalize_size_mode(raw: str | None) -> str:
    mode = str(raw or "qty").strip().lower()
    if mode in {"notional", "dollar", "dollars", "usd", "$"}:
        return "notional"
    if mode in {"ai", "model", "llm", "auto"}:
        return "ai"
    return "qty"


def resolve_size_mode(raw: str | None, strategy_mode: str) -> str:
    """AI sizing is only valid in AI trader mode; otherwise fall back to shares."""
    mode = normalize_size_mode(raw)
    if mode == "ai" and str(strategy_mode or "").strip().lower() != "ai":
        return "qty"
    return mode


DEFAULT_LANG = "en"

# Desk languages, shared by the UI and the AI prompt. Keep in sync with
# web/static/lang/*.json and SUPPORTED_LANGS in web/static/i18n.js.
LANGUAGES: dict[str, str] = {
    "en": "English",
    "bn": "Bangla",
    "es": "Spanish",
    "fr": "French",
    "hi": "Hindi",
}


def normalize_lang(raw: str | None) -> str:
    """Fall back to English for any code the desk has no translations for."""
    code = str(raw or "").strip().lower()
    return code if code in LANGUAGES else DEFAULT_LANG


def language_name(raw: str | None) -> str:
    """Human-readable language name for the AI prompt."""
    return LANGUAGES[normalize_lang(raw)]


@dataclass(frozen=True)
class Config:
    api_key: str
    secret_key: str
    paper: bool
    symbol: str
    symbols: tuple[str, ...]
    fast_sma: int
    slow_sma: int
    sma_preset: str
    dip_preset: str
    dip_rsi_buy: float
    dip_rsi_sell: float
    dip_skip_bearish: bool
    trade_qty: float
    size_mode: str  # qty | notional | ai
    trade_notional: float  # dollar size when size_mode=notional
    bar_timeframe: str
    poll_seconds: int
    strategy_mode: str  # sma | dip | ai | pair | ls
    pair_preset: str
    pair_sma_period: int
    pair_lookback: int
    pair_impulse_pct: float
    pair_weak_side: str  # LONG | CASH
    pair_long_symbol: str
    pair_short_symbol: str
    ls_ema_fast: int
    ls_ema_slow: int
    ls_adx_min: float
    ls_atr_stop_mult: float
    ls_risk_pct: float
    ls_rr: float
    ls_time_stop_bars: int
    ai_provider: str  # openai | gemini
    openai_api_key: str
    gemini_api_key: str
    openai_model: str
    gemini_model: str
    ai_preset: str
    ai_instructions: str
    ai_min_confidence: float
    stop_loss_pct: float  # 0 = off; percent below entry for protective sell
    # --- Risk engine (shared by AI / SMA / dip / pair; all 0 = off) ---
    ai_risk_pct: float = 0.5  # equity % risked per trade; 0 = fixed qty/notional
    ai_atr_stop_mult: float = 1.8  # stop distance in ATR14; 0 = fall back to stop_loss_pct
    ai_take_profit_r: float = 2.0  # scale out half at N×R; 0 = off
    ai_trail_after_r: float = 1.0  # move stop to breakeven then trail past N×R; 0 = off
    ai_max_positions: int = 3  # concurrent open AI positions; 0 = unlimited
    ai_daily_loss_limit_pct: float = 3.0  # halt new entries below -N% day P&L; 0 = off
    ai_min_hold_minutes: int = 15  # no model-driven reverse before this; 0 = off
    ai_cooldown_minutes: int = 60  # no re-entry after a stop-out; 0 = off
    ai_max_spread_bps: float = 25.0  # skip entries above this bid/ask spread; 0 = off
    ai_reversal_conf_bump: float = 0.15  # extra confidence needed to flip a position
    # 0 = stop-market (fill at whatever prints). >0 = stop-limit cushion % past
    # the trigger so the exit refuses a worse print (long: below stop; short: above).
    stop_limit_offset_pct: float = 0.0
    lang: str = DEFAULT_LANG  # desk language; the AI writes thesis / risks in it
    # Options overlay — every strategy cycle maps its equity view onto Alpaca options.
    options_enabled: bool = True
    options_style: str = "vertical"  # vertical | long_option | hedge
    options_dte_min: int = 21
    options_dte_max: int = 45
    options_otm_pct: float = 5.0
    options_max_contracts: int = 1
    options_max_premium_pct: float = 1.0

    def primary_symbols(self) -> tuple[str, ...]:
        """Symbols to evaluate this cycle.

        Primary `symbol` first (signal wall / quote), then the rest of the
        watchlist — SMA, dip, and AI modes.
        """
        head = (self.symbol or "").upper().strip()
        rest = tuple(s for s in self.symbols if s and s != head)
        if head:
            return (head, *rest)
        return rest or ("AAPL",)

    def order_qty_for_price(self, price: float) -> float:
        """Shares to trade at `price` (converts dollar notional when configured)."""
        mode = normalize_size_mode(self.size_mode)
        if mode == "notional":
            mark = float(price or 0)
            if mark <= 0:
                raise ValueError("Need a positive mark price to size by dollar amount")
            notional = float(self.trade_notional or 0)
            if notional <= 0:
                raise ValueError("Dollar amount must be greater than 0")
            return notional / mark
        qty = float(self.trade_qty or 0)
        if qty <= 0:
            raise ValueError("Trade qty must be greater than 0")
        return qty

    def ai_stop_distance(self, price: float, atr: float | None) -> float:
        """Per-share risk in dollars: ATR-scaled when configured, else flat percent.

        The risk engine owns stops via ``ai_atr_stop_mult`` for AI / SMA / dip /
        pair. Flat ``stop_loss_pct`` is only a fallback when the ATR multiple is 0.
        """
        price = float(price or 0)
        if price <= 0:
            return 0.0
        if self.ai_atr_stop_mult > 0 and atr and float(atr) > 0:
            return float(atr) * float(self.ai_atr_stop_mult)
        return price * (float(self.stop_loss_pct or 0) / 100.0)

    def ai_qty_for_risk(
        self, price: float, stop_distance: float, equity: float
    ) -> float | None:
        """Shares so that a stop-out costs `ai_risk_pct` of equity. None = risk sizing off."""
        if self.ai_risk_pct <= 0:
            return None
        price = float(price or 0)
        stop_distance = float(stop_distance or 0)
        equity = float(equity or 0)
        if price <= 0 or stop_distance <= 0 or equity <= 0:
            return None
        qty = (equity * (self.ai_risk_pct / 100.0)) / stop_distance
        # Never let one position exceed the account, regardless of how tight the stop is.
        max_affordable = equity / price
        return max(0.0, min(qty, max_affordable))

    def size_summary(self) -> str:
        """Short label for logs / CLI banners."""
        mode = normalize_size_mode(self.size_mode)
        if mode == "ai":
            return "AI qty"
        if mode == "notional":
            return f"${float(self.trade_notional):.2f}"
        return f"{float(self.trade_qty):g} sh"

    def override(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        symbol: str | None = None,
        symbols: tuple[str, ...] | list[str] | str | None = None,
        fast_sma: int | None = None,
        slow_sma: int | None = None,
        sma_preset: str | None = None,
        dip_preset: str | None = None,
        dip_rsi_buy: float | None = None,
        dip_rsi_sell: float | None = None,
        dip_skip_bearish: bool | None = None,
        trade_qty: float | None = None,
        size_mode: str | None = None,
        trade_notional: float | None = None,
        bar_timeframe: str | None = None,
        poll_seconds: int | None = None,
        paper: bool | None = None,
        strategy_mode: str | None = None,
        pair_preset: str | None = None,
        pair_sma_period: int | None = None,
        pair_lookback: int | None = None,
        pair_impulse_pct: float | None = None,
        pair_weak_side: str | None = None,
        pair_long_symbol: str | None = None,
        pair_short_symbol: str | None = None,
        ls_ema_fast: int | None = None,
        ls_ema_slow: int | None = None,
        ls_adx_min: float | None = None,
        ls_atr_stop_mult: float | None = None,
        ls_risk_pct: float | None = None,
        ls_rr: float | None = None,
        ls_time_stop_bars: int | None = None,
        ai_provider: str | None = None,
        ai_preset: str | None = None,
        ai_instructions: str | None = None,
        ai_min_confidence: float | None = None,
        stop_loss_pct: float | None = None,
        ai_risk_pct: float | None = None,
        ai_atr_stop_mult: float | None = None,
        ai_take_profit_r: float | None = None,
        ai_trail_after_r: float | None = None,
        ai_max_positions: int | None = None,
        ai_daily_loss_limit_pct: float | None = None,
        ai_min_hold_minutes: int | None = None,
        ai_cooldown_minutes: int | None = None,
        ai_max_spread_bps: float | None = None,
        ai_reversal_conf_bump: float | None = None,
        stop_limit_offset_pct: float | None = None,
        openai_model: str | None = None,
        gemini_model: str | None = None,
        openai_api_key: str | None = None,
        gemini_api_key: str | None = None,
        lang: str | None = None,
        options_enabled: bool | None = None,
        options_style: str | None = None,
        options_dte_min: int | None = None,
        options_dte_max: int | None = None,
        options_otm_pct: float | None = None,
        options_max_contracts: int | None = None,
        options_max_premium_pct: float | None = None,
    ) -> Config:
        sma_preset_id = resolve_sma_preset_id(
            self.sma_preset if sma_preset is None else sma_preset
        )
        sma_def = get_sma_preset(sma_preset_id)
        if sma_preset is not None and sma_preset_id != "custom":
            # CLI / UI selected a named preset — fill windows unless both given.
            fast = sma_def.fast_sma if fast_sma is None else fast_sma
            slow = sma_def.slow_sma if slow_sma is None else slow_sma
        else:
            fast = self.fast_sma if fast_sma is None else fast_sma
            slow = self.slow_sma if slow_sma is None else slow_sma
            if sma_preset is None and (fast_sma is not None or slow_sma is not None):
                sma_preset_id = match_preset_id(fast, slow)
            elif sma_preset_id == "custom" and sma_preset is None:
                sma_preset_id = match_preset_id(fast, slow)
        if fast >= slow:
            raise ValueError("Fast SMA must be smaller than Slow SMA")

        dip_preset_id = resolve_dip_preset_id(
            self.dip_preset if dip_preset is None else dip_preset
        )
        dip_def = get_dip_preset(dip_preset_id)
        if dip_preset is not None and dip_preset_id != "custom":
            rsi_buy = dip_def.rsi_buy if dip_rsi_buy is None else float(dip_rsi_buy)
            rsi_sell = dip_def.rsi_sell if dip_rsi_sell is None else float(dip_rsi_sell)
            skip_bearish = (
                dip_def.skip_bearish
                if dip_skip_bearish is None
                else bool(dip_skip_bearish)
            )
            # Named id only sticks when thresholds still match the preset.
            if (
                dip_rsi_buy is not None
                or dip_rsi_sell is not None
                or dip_skip_bearish is not None
            ):
                dip_preset_id = match_dip_preset_id(rsi_buy, rsi_sell, skip_bearish)
        else:
            rsi_buy = (
                self.dip_rsi_buy if dip_rsi_buy is None else float(dip_rsi_buy)
            )
            rsi_sell = (
                self.dip_rsi_sell if dip_rsi_sell is None else float(dip_rsi_sell)
            )
            skip_bearish = (
                self.dip_skip_bearish
                if dip_skip_bearish is None
                else bool(dip_skip_bearish)
            )
            if dip_preset is None and (
                dip_rsi_buy is not None
                or dip_rsi_sell is not None
                or dip_skip_bearish is not None
            ):
                dip_preset_id = match_dip_preset_id(rsi_buy, rsi_sell, skip_bearish)
            elif dip_preset_id == "custom" and dip_preset is None:
                dip_preset_id = match_dip_preset_id(rsi_buy, rsi_sell, skip_bearish)
        if not (0 < rsi_buy < rsi_sell < 100):
            raise ValueError("Dip RSI buy must be less than RSI sell (0–100)")

        mode = (strategy_mode or self.strategy_mode).strip().lower()
        if mode not in {"sma", "dip", "ai", "pair", "ls"}:
            raise ValueError(
                "strategy_mode must be 'sma', 'dip', 'ai', 'pair', or 'ls'"
            )

        ls_fast = int(self.ls_ema_fast if ls_ema_fast is None else ls_ema_fast)
        ls_slow = int(self.ls_ema_slow if ls_ema_slow is None else ls_ema_slow)
        if ls_fast >= ls_slow:
            raise ValueError("ls_ema_fast must be < ls_ema_slow")
        ls_adx = float(self.ls_adx_min if ls_adx_min is None else ls_adx_min)
        ls_atr_m = float(
            self.ls_atr_stop_mult if ls_atr_stop_mult is None else ls_atr_stop_mult
        )
        ls_risk = float(self.ls_risk_pct if ls_risk_pct is None else ls_risk_pct)
        ls_rr_v = float(self.ls_rr if ls_rr is None else ls_rr)
        ls_time = int(
            self.ls_time_stop_bars if ls_time_stop_bars is None else ls_time_stop_bars
        )
        if ls_adx < 0 or ls_atr_m <= 0 or ls_risk <= 0 or ls_rr_v <= 0 or ls_time < 1:
            raise ValueError("Invalid LS risk / EMA parameters")

        if mode == "ls":
            bar_timeframe = "1Day"

        pair_preset_id = resolve_pair_preset_id(
            self.pair_preset if pair_preset is None else pair_preset
        )
        pair_def = get_pair_preset(pair_preset_id)
        if pair_preset is not None and pair_preset_id != "custom":
            p_sma = pair_def.sma_period if pair_sma_period is None else int(pair_sma_period)
            p_lb = pair_def.lookback if pair_lookback is None else int(pair_lookback)
            p_imp = (
                pair_def.impulse_pct
                if pair_impulse_pct is None
                else float(pair_impulse_pct)
            )
            p_weak = (
                pair_def.weak_side
                if pair_weak_side is None
                else str(pair_weak_side).strip().upper()
            )
            p_long = (
                pair_def.long_symbol
                if pair_long_symbol is None
                else str(pair_long_symbol).strip().upper()
            )
            p_short = (
                pair_def.short_symbol
                if pair_short_symbol is None
                else str(pair_short_symbol).strip().upper()
            )
            if (
                pair_sma_period is not None
                or pair_lookback is not None
                or pair_impulse_pct is not None
                or pair_weak_side is not None
                or pair_long_symbol is not None
                or pair_short_symbol is not None
            ):
                pair_preset_id = match_pair_preset_id(
                    p_sma, p_lb, p_imp, p_weak, long_symbol=p_long, short_symbol=p_short
                )
        else:
            p_sma = (
                self.pair_sma_period
                if pair_sma_period is None
                else int(pair_sma_period)
            )
            p_lb = (
                self.pair_lookback if pair_lookback is None else int(pair_lookback)
            )
            p_imp = (
                self.pair_impulse_pct
                if pair_impulse_pct is None
                else float(pair_impulse_pct)
            )
            p_weak = (
                self.pair_weak_side
                if pair_weak_side is None
                else str(pair_weak_side).strip().upper()
            )
            p_long = (
                self.pair_long_symbol
                if pair_long_symbol is None
                else str(pair_long_symbol).strip().upper()
            )
            p_short = (
                self.pair_short_symbol
                if pair_short_symbol is None
                else str(pair_short_symbol).strip().upper()
            )
            if pair_preset is None and (
                pair_sma_period is not None
                or pair_lookback is not None
                or pair_impulse_pct is not None
                or pair_weak_side is not None
            ):
                pair_preset_id = match_pair_preset_id(
                    p_sma, p_lb, p_imp, p_weak, long_symbol=p_long, short_symbol=p_short
                )
            elif pair_preset_id == "custom" and pair_preset is None:
                pair_preset_id = match_pair_preset_id(
                    p_sma, p_lb, p_imp, p_weak, long_symbol=p_long, short_symbol=p_short
                )
        if p_sma < 2 or p_lb < 1 or not (0 < p_imp < 100):
            raise ValueError("Invalid pair strategy parameters")
        p_weak = normalize_weak_side(p_weak)
        p_long = str(p_long or "").strip().upper()
        p_short = str(p_short or "").strip().upper()

        provider = (ai_provider or self.ai_provider).strip().lower()
        if provider not in {"openai", "gemini"}:
            raise ValueError("ai_provider must be 'openai' or 'gemini'")

        if isinstance(symbols, str):
            parsed_symbols = _parse_symbols(symbols)
        elif symbols is not None:
            parsed_symbols = _parse_symbols(",".join(symbols))
        else:
            parsed_symbols = self.symbols

        primary = (symbol or self.symbol).upper().strip()
        if symbols is None and symbol is not None:
            # Single-symbol form field updates the watchlist head.
            parsed_symbols = (primary, *tuple(s for s in parsed_symbols if s != primary))

        if mode == "pair":
            # Legs come from the watchlist (long, short) unless explicitly set.
            if (not p_long or not p_short) and len(parsed_symbols) >= 2:
                p_long, p_short = parsed_symbols[0], parsed_symbols[1]
            if not p_long or not p_short or p_long == p_short:
                raise ValueError(
                    "Long & Short Pair needs two different symbols "
                    "(long first, short second)"
                )
            primary = p_long
            parsed_symbols = (p_long, p_short)
        conf = (
            self.ai_min_confidence
            if ai_min_confidence is None
            else float(ai_min_confidence)
        )
        conf = max(0.0, min(1.0, conf))

        stop_pct = (
            self.stop_loss_pct if stop_loss_pct is None else float(stop_loss_pct)
        )
        stop_pct = max(0.0, min(50.0, stop_pct))

        preset_risk: dict[str, Any] = {}
        if ai_preset is not None:
            # Picking a named preset also adopts its stop/target geometry, the way
            # choosing an SMA preset fills its windows. Explicit values still win.
            preset_risk = risk_profile_for(ai_preset)

        def _num(current: float, incoming: Any, lo: float, hi: float, key: str = "") -> float:
            if incoming is None and key and key in preset_risk:
                incoming = preset_risk[key]
            value = current if incoming is None else float(incoming)
            return max(lo, min(hi, value))

        risk_pct = _num(self.ai_risk_pct, ai_risk_pct, 0.0, 10.0, "ai_risk_pct")
        atr_mult = _num(
            self.ai_atr_stop_mult, ai_atr_stop_mult, 0.0, 10.0, "ai_atr_stop_mult"
        )
        tp_r = _num(
            self.ai_take_profit_r, ai_take_profit_r, 0.0, 20.0, "ai_take_profit_r"
        )
        trail_r = _num(
            self.ai_trail_after_r, ai_trail_after_r, 0.0, 20.0, "ai_trail_after_r"
        )
        max_pos = int(
            _num(self.ai_max_positions, ai_max_positions, 0, 50, "ai_max_positions")
        )
        daily_loss = _num(
            self.ai_daily_loss_limit_pct, ai_daily_loss_limit_pct, 0.0, 100.0
        )
        min_hold = int(_num(self.ai_min_hold_minutes, ai_min_hold_minutes, 0, 1440))
        cooldown = int(_num(self.ai_cooldown_minutes, ai_cooldown_minutes, 0, 1440))
        max_spread = _num(self.ai_max_spread_bps, ai_max_spread_bps, 0.0, 1000.0)
        conf_bump = _num(self.ai_reversal_conf_bump, ai_reversal_conf_bump, 0.0, 1.0)
        stop_limit_offset = _num(
            self.stop_limit_offset_pct, stop_limit_offset_pct, 0.0, 50.0
        )
        dte_lo = int(_num(self.options_dte_min, options_dte_min, 1, 180))
        dte_hi = int(_num(self.options_dte_max, options_dte_max, 1, 365))
        if dte_lo > dte_hi:
            dte_lo, dte_hi = dte_hi, dte_lo

        preset = resolve_preset_id(
            self.ai_preset if ai_preset is None else ai_preset
        )
        raw_instructions = (
            self.ai_instructions if ai_instructions is None else ai_instructions
        )
        resolved_instructions = instructions_for(preset, raw_instructions)

        return replace(
            self,
            api_key=self.api_key if api_key is None else api_key.strip(),
            secret_key=self.secret_key if secret_key is None else secret_key.strip(),
            symbol=primary,
            symbols=parsed_symbols or (primary,),
            fast_sma=fast,
            slow_sma=slow,
            sma_preset=sma_preset_id,
            dip_preset=dip_preset_id,
            dip_rsi_buy=rsi_buy,
            dip_rsi_sell=rsi_sell,
            dip_skip_bearish=skip_bearish,
            trade_qty=self.trade_qty if trade_qty is None else trade_qty,
            size_mode=resolve_size_mode(
                self.size_mode if size_mode is None else size_mode, mode
            ),
            trade_notional=(
                self.trade_notional if trade_notional is None else float(trade_notional)
            ),
            bar_timeframe=bar_timeframe or self.bar_timeframe,
            poll_seconds=max(
                10,
                self.poll_seconds if poll_seconds is None else poll_seconds,
            ),
            paper=self.paper if paper is None else paper,
            strategy_mode=mode,
            pair_preset=pair_preset_id,
            pair_sma_period=p_sma,
            pair_lookback=p_lb,
            pair_impulse_pct=p_imp,
            pair_weak_side=p_weak,
            pair_long_symbol=p_long,
            pair_short_symbol=p_short,
            ls_ema_fast=ls_fast,
            ls_ema_slow=ls_slow,
            ls_adx_min=ls_adx,
            ls_atr_stop_mult=ls_atr_m,
            ls_risk_pct=ls_risk,
            ls_rr=ls_rr_v,
            ls_time_stop_bars=ls_time,
            ai_provider=provider,
            ai_preset=preset,
            ai_instructions=resolved_instructions,
            ai_min_confidence=conf,
            stop_loss_pct=stop_pct,
            ai_risk_pct=risk_pct,
            ai_atr_stop_mult=atr_mult,
            ai_take_profit_r=tp_r,
            ai_trail_after_r=trail_r,
            ai_max_positions=max_pos,
            ai_daily_loss_limit_pct=daily_loss,
            ai_min_hold_minutes=min_hold,
            ai_cooldown_minutes=cooldown,
            ai_max_spread_bps=max_spread,
            ai_reversal_conf_bump=conf_bump,
            stop_limit_offset_pct=stop_limit_offset,
            openai_model=openai_model or self.openai_model,
            gemini_model=gemini_model or self.gemini_model,
            openai_api_key=(
                self.openai_api_key if openai_api_key is None else openai_api_key.strip()
            ),
            gemini_api_key=(
                self.gemini_api_key if gemini_api_key is None else gemini_api_key.strip()
            ),
            lang=normalize_lang(self.lang if lang is None else lang),
            options_enabled=(
                self.options_enabled if options_enabled is None else bool(options_enabled)
            ),
            options_style=normalize_options_style(
                self.options_style if options_style is None else options_style
            ),
            options_dte_min=dte_lo,
            options_dte_max=dte_hi,
            options_otm_pct=_num(self.options_otm_pct, options_otm_pct, 0.5, 25.0),
            options_max_contracts=int(
                _num(self.options_max_contracts, options_max_contracts, 1, 20)
            ),
            options_max_premium_pct=_num(
                self.options_max_premium_pct, options_max_premium_pct, 0.0, 10.0
            ),
        )

    @classmethod
    def default(
        cls,
        *,
        api_key: str = "",
        secret_key: str = "",
        paper: bool = True,
        symbol: str = "AAPL",
        symbols: tuple[str, ...] = ("AAPL",),
        fast_sma: int = 10,
        slow_sma: int = 30,
        sma_preset: str = "classic",
        dip_preset: str = "deep",
        dip_rsi_buy: float = 30.0,
        dip_rsi_sell: float = 60.0,
        dip_skip_bearish: bool = True,
        trade_qty: float = 1.0,
        size_mode: str = "qty",
        trade_notional: float = 100.0,
        bar_timeframe: str = "15Min",
        poll_seconds: int = 20,
        strategy_mode: str = "sma",
        pair_preset: str = "research_max",
        pair_sma_period: int = 50,
        pair_lookback: int = 7,
        pair_impulse_pct: float = 5.0,
        pair_weak_side: str = "LONG",
        pair_long_symbol: str = "",
        pair_short_symbol: str = "",
        ls_ema_fast: int = 21,
        ls_ema_slow: int = 55,
        ls_adx_min: float = 20.0,
        ls_atr_stop_mult: float = 1.5,
        ls_risk_pct: float = 1.0,
        ls_rr: float = 2.0,
        ls_time_stop_bars: int = 15,
        ai_provider: str = "openai",
        openai_api_key: str = "",
        gemini_api_key: str = "",
        openai_model: str = DEFAULT_OPENAI_MODEL,
        gemini_model: str = DEFAULT_GEMINI_MODEL,
        ai_preset: str = "balanced",
        ai_instructions: str = "",
        ai_min_confidence: float = 0.55,
        stop_loss_pct: float = 0.0,
        ai_risk_pct: float = 0.5,
        ai_atr_stop_mult: float = 1.8,
        ai_take_profit_r: float = 2.0,
        ai_trail_after_r: float = 1.0,
        ai_max_positions: int = 3,
        ai_daily_loss_limit_pct: float = 3.0,
        ai_min_hold_minutes: int = 15,
        ai_cooldown_minutes: int = 60,
        ai_max_spread_bps: float = 25.0,
        ai_reversal_conf_bump: float = 0.15,
        stop_limit_offset_pct: float = 0.0,
        lang: str = DEFAULT_LANG,
        options_enabled: bool = True,
        options_style: str = "vertical",
        options_dte_min: int = 21,
        options_dte_max: int = 45,
        options_otm_pct: float = 5.0,
        options_max_contracts: int = 1,
        options_max_premium_pct: float = 1.0,
    ) -> Config:
        return cls(
            api_key=(api_key or "").strip(),
            secret_key=(secret_key or "").strip(),
            paper=bool(paper),
            symbol=(symbol or "AAPL").strip().upper(),
            symbols=symbols or (symbol or "AAPL",),
            fast_sma=fast_sma,
            slow_sma=slow_sma,
            sma_preset=sma_preset,
            dip_preset=dip_preset,
            dip_rsi_buy=dip_rsi_buy,
            dip_rsi_sell=dip_rsi_sell,
            dip_skip_bearish=dip_skip_bearish,
            trade_qty=trade_qty,
            size_mode=size_mode,
            trade_notional=trade_notional,
            bar_timeframe=bar_timeframe,
            poll_seconds=poll_seconds,
            strategy_mode=strategy_mode,
            pair_preset=pair_preset,
            pair_sma_period=pair_sma_period,
            pair_lookback=pair_lookback,
            pair_impulse_pct=pair_impulse_pct,
            pair_weak_side=pair_weak_side,
            pair_long_symbol=pair_long_symbol,
            pair_short_symbol=pair_short_symbol,
            ls_ema_fast=ls_ema_fast,
            ls_ema_slow=ls_ema_slow,
            ls_adx_min=ls_adx_min,
            ls_atr_stop_mult=ls_atr_stop_mult,
            ls_risk_pct=ls_risk_pct,
            ls_rr=ls_rr,
            ls_time_stop_bars=ls_time_stop_bars,
            ai_provider=ai_provider,
            openai_api_key=(openai_api_key or "").strip(),
            gemini_api_key=(gemini_api_key or "").strip(),
            openai_model=openai_model,
            gemini_model=gemini_model,
            ai_preset=ai_preset,
            ai_instructions=ai_instructions,
            ai_min_confidence=ai_min_confidence,
            stop_loss_pct=stop_loss_pct,
            ai_risk_pct=ai_risk_pct,
            ai_atr_stop_mult=ai_atr_stop_mult,
            ai_take_profit_r=ai_take_profit_r,
            ai_trail_after_r=ai_trail_after_r,
            ai_max_positions=ai_max_positions,
            ai_daily_loss_limit_pct=ai_daily_loss_limit_pct,
            ai_min_hold_minutes=ai_min_hold_minutes,
            ai_cooldown_minutes=ai_cooldown_minutes,
            ai_max_spread_bps=ai_max_spread_bps,
            ai_reversal_conf_bump=ai_reversal_conf_bump,
            stop_limit_offset_pct=stop_limit_offset_pct,
            lang=lang,
            options_enabled=options_enabled,
            options_style=options_style,
            options_dte_min=options_dte_min,
            options_dte_max=options_dte_max,
            options_otm_pct=options_otm_pct,
            options_max_contracts=options_max_contracts,
            options_max_premium_pct=options_max_premium_pct,
        )

    @classmethod
    def from_env(cls) -> Config:
        paper = paper_mode_from_env()
        if not paper and not live_allowed_from_env():
            raise ValueError(
                "Live trading is blocked. Set ALPACA_ALLOW_LIVE=true in .env and "
                "save distinct ALPACA_LIVE_API_KEY / ALPACA_LIVE_SECRET_KEY."
            )
        api_key, secret_key, paper = resolve_alpaca_credentials(paper=paper)
        if not api_key or not secret_key:
            if paper:
                raise ValueError(
                    "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env\n"
                    "Paste paper keys from the Alpaca paper dashboard.\n"
                    "Secret is shown only once when you generate/regenerate keys."
                )
            raise ValueError(
                "ALPACA_LIVE_API_KEY and ALPACA_LIVE_SECRET_KEY must be set in .env\n"
                "Paste live keys from the Alpaca live dashboard. "
                "Do not reuse paper keys for live trading."
            )

        sma_preset_env = _e("SMA_PRESET", "").strip()
        fast_env = _e("FAST_SMA", "").strip()
        slow_env = _e("SLOW_SMA", "").strip()
        if sma_preset_env:
            sma_preset = resolve_sma_preset_id(sma_preset_env)
            sma_def = get_sma_preset(sma_preset)
            if sma_preset != "custom" and not fast_env and not slow_env:
                fast, slow = sma_def.fast_sma, sma_def.slow_sma
            else:
                fast = int(fast_env or sma_def.fast_sma)
                slow = int(slow_env or sma_def.slow_sma)
                if sma_preset != "custom":
                    # Keep the named id only when windows still match.
                    if match_preset_id(fast, slow) != sma_preset:
                        sma_preset = match_preset_id(fast, slow)
        else:
            fast = int(fast_env or "10")
            slow = int(slow_env or "30")
            sma_preset = match_preset_id(fast, slow)
        if fast >= slow:
            raise ValueError("FAST_SMA must be smaller than SLOW_SMA")

        symbol = _e("SYMBOL", "AAPL").upper().strip()
        symbols_raw = _e("SYMBOLS", "").strip()
        symbols = _parse_symbols(symbols_raw) if symbols_raw else (symbol,)

        mode = _e("STRATEGY_MODE", "sma").strip().lower()
        if mode not in {"sma", "dip", "ai", "pair", "ls"}:
            mode = "sma"

        ls_fast = int(_e("LS_EMA_FAST", "21") or 21)
        ls_slow = int(_e("LS_EMA_SLOW", "55") or 55)
        if ls_fast >= ls_slow:
            ls_fast, ls_slow = 21, 55
        ls_adx = float(_e("LS_ADX_MIN", "20") or 20)
        ls_atr_m = float(_e("LS_ATR_STOP_MULT", "1.5") or 1.5)
        ls_risk = float(_e("LS_RISK_PCT", "1.0") or 1.0)
        ls_rr_v = float(_e("LS_RR", "2.0") or 2.0)
        ls_time = int(_e("LS_TIME_STOP_BARS", "15") or 15)
        provider = _e("AI_PROVIDER", "openai").strip().lower()
        if provider not in {"openai", "gemini"}:
            provider = "openai"

        preset = resolve_preset_id(_e("AI_PRESET", DEFAULT_PRESET_ID))
        raw_instructions = _e("AI_INSTRUCTIONS", "").strip()
        instructions = instructions_for(preset, raw_instructions)

        conf = float(_e("AI_MIN_CONFIDENCE", "0.55"))
        conf = max(0.0, min(1.0, conf))

        stop_pct = float(_e("STOP_LOSS_PCT", "0") or 0)
        stop_pct = max(0.0, min(50.0, stop_pct))

        dip_preset_env = _e("DIP_PRESET", "").strip()
        rsi_buy_env = _e("DIP_RSI_BUY", "").strip()
        rsi_sell_env = _e("DIP_RSI_SELL", "").strip()
        skip_env = _e("DIP_SKIP_BEARISH", "").strip().lower()
        if dip_preset_env:
            dip_preset = resolve_dip_preset_id(dip_preset_env)
            dip_def = get_dip_preset(dip_preset)
            # Named preset wholesale only when no per-field overrides are set.
            if (
                dip_preset != "custom"
                and not rsi_buy_env
                and not rsi_sell_env
                and not skip_env
            ):
                rsi_buy, rsi_sell = dip_def.rsi_buy, dip_def.rsi_sell
                skip_bearish = dip_def.skip_bearish
            else:
                rsi_buy = float(rsi_buy_env or dip_def.rsi_buy)
                rsi_sell = float(rsi_sell_env or dip_def.rsi_sell)
                if skip_env in {"0", "false", "no"}:
                    skip_bearish = False
                elif skip_env in {"1", "true", "yes"}:
                    skip_bearish = True
                else:
                    skip_bearish = dip_def.skip_bearish
                if dip_preset != "custom":
                    matched = match_dip_preset_id(rsi_buy, rsi_sell, skip_bearish)
                    if matched != dip_preset:
                        dip_preset = matched
        else:
            rsi_buy = float(rsi_buy_env or "30")
            rsi_sell = float(rsi_sell_env or "60")
            if skip_env in {"0", "false", "no"}:
                skip_bearish = False
            elif skip_env in {"1", "true", "yes"}:
                skip_bearish = True
            else:
                skip_bearish = True
            dip_preset = match_dip_preset_id(rsi_buy, rsi_sell, skip_bearish)
        if not (0 < rsi_buy < rsi_sell < 100):
            raise ValueError("DIP_RSI_BUY must be less than DIP_RSI_SELL (0–100)")

        pair_preset_env = _e("PAIR_PRESET", "").strip()
        pair_sma_env = _e("PAIR_SMA_PERIOD", "").strip()
        pair_lb_env = _e("PAIR_LOOKBACK", "").strip()
        pair_imp_env = _e("PAIR_IMPULSE_PCT", "").strip()
        pair_weak_env = _e("PAIR_WEAK_SIDE", "").strip().upper()
        pair_long_env = _e("PAIR_LONG_SYMBOL", "").strip().upper()
        pair_short_env = _e("PAIR_SHORT_SYMBOL", "").strip().upper()
        if pair_preset_env:
            pair_preset = resolve_pair_preset_id(pair_preset_env)
            pair_def = get_pair_preset(pair_preset)
            if (
                pair_preset != "custom"
                and not pair_sma_env
                and not pair_lb_env
                and not pair_imp_env
                and not pair_weak_env
            ):
                p_sma, p_lb = pair_def.sma_period, pair_def.lookback
                p_imp, p_weak = pair_def.impulse_pct, pair_def.weak_side
                p_long, p_short = pair_def.long_symbol, pair_def.short_symbol
            else:
                p_sma = int(pair_sma_env or pair_def.sma_period)
                p_lb = int(pair_lb_env or pair_def.lookback)
                p_imp = float(pair_imp_env or pair_def.impulse_pct)
                p_weak = pair_weak_env or pair_def.weak_side
                p_long = pair_long_env or pair_def.long_symbol
                p_short = pair_short_env or pair_def.short_symbol
                if pair_preset != "custom":
                    matched = match_pair_preset_id(
                        p_sma,
                        p_lb,
                        p_imp,
                        p_weak,
                        long_symbol=p_long,
                        short_symbol=p_short,
                    )
                    if matched != pair_preset:
                        pair_preset = matched
        else:
            pair_def = get_pair_preset(DEFAULT_PAIR_PRESET_ID)
            p_sma = int(pair_sma_env or pair_def.sma_period)
            p_lb = int(pair_lb_env or pair_def.lookback)
            p_imp = float(pair_imp_env or pair_def.impulse_pct)
            p_weak = pair_weak_env or pair_def.weak_side
            p_long = pair_long_env or pair_def.long_symbol
            p_short = pair_short_env or pair_def.short_symbol
            pair_preset = match_pair_preset_id(
                p_sma, p_lb, p_imp, p_weak, long_symbol=p_long, short_symbol=p_short
            )
        if p_sma < 2 or p_lb < 1 or not (0 < p_imp < 100):
            raise ValueError("Invalid pair strategy parameters")
        p_weak = normalize_weak_side(p_weak)
        p_long = str(p_long or "").strip().upper()
        p_short = str(p_short or "").strip().upper()
        # Prefer watchlist legs when pair symbols are unset.
        if (not p_long or not p_short) and len(symbols) >= 2:
            p_long, p_short = symbols[0], symbols[1]

        bar_tf = _e("BAR_TIMEFRAME", "15Min")
        if mode in {"pair", "ls"}:
            bar_tf = "1Day"

        options_dte_min = max(1, min(180, int(_e("OPTIONS_DTE_MIN", "21") or 21)))
        options_dte_max = max(1, min(365, int(_e("OPTIONS_DTE_MAX", "45") or 45)))
        if options_dte_min > options_dte_max:
            options_dte_min, options_dte_max = options_dte_max, options_dte_min

        return cls(
            api_key=api_key,
            secret_key=secret_key,
            paper=paper,
            symbol=symbol,
            symbols=symbols,
            fast_sma=fast,
            slow_sma=slow,
            sma_preset=sma_preset or DEFAULT_SMA_PRESET_ID,
            dip_preset=dip_preset or DEFAULT_DIP_PRESET_ID,
            dip_rsi_buy=rsi_buy,
            dip_rsi_sell=rsi_sell,
            dip_skip_bearish=skip_bearish,
            trade_qty=float(_e("TRADE_QTY", "1")),
            size_mode=resolve_size_mode(_e("SIZE_MODE", "qty"), mode),
            trade_notional=float(_e("TRADE_NOTIONAL", "100")),
            bar_timeframe=bar_tf,
            poll_seconds=max(10, int(_e("POLL_SECONDS", "20"))),
            strategy_mode=mode,
            pair_preset=pair_preset or DEFAULT_PAIR_PRESET_ID,
            pair_sma_period=p_sma,
            pair_lookback=p_lb,
            pair_impulse_pct=p_imp,
            pair_weak_side=p_weak,
            pair_long_symbol=p_long,
            pair_short_symbol=p_short,
            ls_ema_fast=ls_fast,
            ls_ema_slow=ls_slow,
            ls_adx_min=ls_adx,
            ls_atr_stop_mult=ls_atr_m,
            ls_risk_pct=ls_risk,
            ls_rr=ls_rr_v,
            ls_time_stop_bars=ls_time,
            ai_provider=provider,
            openai_api_key=_e("OPENAI_API_KEY", "").strip(),
            gemini_api_key=_e("GEMINI_API_KEY", "").strip(),
            openai_model=_e("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip()
            or DEFAULT_OPENAI_MODEL,
            gemini_model=_e("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
            or DEFAULT_GEMINI_MODEL,
            ai_preset=preset,
            ai_instructions=instructions,
            ai_min_confidence=conf,
            stop_loss_pct=stop_pct,
            ai_risk_pct=max(0.0, min(10.0, float(_e("AI_RISK_PCT", "0.5")))),
            ai_atr_stop_mult=max(
                0.0, min(10.0, float(_e("AI_ATR_STOP_MULT", "1.8")))
            ),
            ai_take_profit_r=max(
                0.0, min(20.0, float(_e("AI_TAKE_PROFIT_R", "2.0")))
            ),
            ai_trail_after_r=max(
                0.0, min(20.0, float(_e("AI_TRAIL_AFTER_R", "1.0")))
            ),
            ai_max_positions=max(0, min(50, int(_e("AI_MAX_POSITIONS", "3")))),
            ai_daily_loss_limit_pct=max(
                0.0, min(100.0, float(_e("AI_DAILY_LOSS_LIMIT_PCT", "3.0")))
            ),
            ai_min_hold_minutes=max(
                0, min(1440, int(_e("AI_MIN_HOLD_MINUTES", "15")))
            ),
            ai_cooldown_minutes=max(
                0, min(1440, int(_e("AI_COOLDOWN_MINUTES", "60")))
            ),
            stop_limit_offset_pct=max(
                0.0, min(50.0, float(_e("STOP_LIMIT_OFFSET_PCT", "0")))
            ),
            ai_max_spread_bps=max(
                0.0, min(1000.0, float(_e("AI_MAX_SPREAD_BPS", "25")))
            ),
            ai_reversal_conf_bump=max(
                0.0, min(1.0, float(_e("AI_REVERSAL_CONF_BUMP", "0.15")))
            ),
            lang=normalize_lang(_e("LANG_CODE", DEFAULT_LANG)),
            options_enabled=env_flag("OPTIONS_ENABLED", True),
            options_style=normalize_options_style(_e("OPTIONS_STYLE", "vertical")),
            options_dte_min=options_dte_min,
            options_dte_max=options_dte_max,
            options_otm_pct=max(
                0.5, min(25.0, float(_e("OPTIONS_OTM_PCT", "5.0") or 5.0))
            ),
            options_max_contracts=max(
                1, min(20, int(_e("OPTIONS_MAX_CONTRACTS", "1") or 1))
            ),
            options_max_premium_pct=max(
                0.0, min(10.0, float(_e("OPTIONS_MAX_PREMIUM_PCT", "1.0") or 1.0))
            ),
        )
