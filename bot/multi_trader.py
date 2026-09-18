"""Multi Auto-Trade Engine and Ticker Runner Management.

Allows traders to run concurrent, independent auto-trade loops for individual
or multiple tickers, each under distinct strategies (SMA, Buy The Dip, AI, Day
Trading, Long/Short Trend, or Custom Engines).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from bot import custom_engine_store
from bot.client import AlpacaService
from bot.config import Config, resolve_day_timeframe, resolve_size_mode
from bot.trader import TradingBot
from bot.ai_trader import AiTradingBot
from bot.day_trader import DayTradingBot
from bot.ls_trader import LsTradingBot

logger = logging.getLogger(__name__)

STRATEGY_NAMES = {
    "sma": "SMA Crossover",
    "dip": "Buy The Dip",
    "ai": "AI Momentum",
    "day": "Day Trading",
    "ls": "Long/Short Trend",
}

# A pair trade needs a long and a short leg, so it cannot be expressed as one
# ticker's isolated runner — the modal hides those engines and this guards the API.
UNSUPPORTED_MODES = {"pair"}

# Predefined curated ticker baskets
DEFAULT_BASKETS: list[dict[str, Any]] = [
    {
        "id": "mega_tech",
        "name": "Mega-Cap Tech",
        "description": "Leading US technology giants (AAPL, MSFT, NVDA, GOOGL, AMZN, META)",
        "symbols": ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META"],
        "default_strategy": "ai",
    },
    {
        "id": "semis",
        "name": "Semiconductors",
        "description": "High-beta chipmakers and semiconductor equipment (NVDA, AMD, AVGO, TSM, QCOM)",
        "symbols": ["NVDA", "AMD", "AVGO", "TSM", "QCOM"],
        "default_strategy": "sma",
    },
    {
        "id": "etf_core",
        "name": "Index Core ETFs",
        "description": "Broad market liquidity and sector benchmark ETFs (SPY, QQQ, IWM, DIA)",
        "symbols": ["SPY", "QQQ", "IWM", "DIA"],
        "default_strategy": "sma",
    },
    {
        "id": "dividend_staples",
        "name": "Dividend Staples",
        "description": "Defensive dividend-paying consumer staples and healthcare (KO, PEP, JNJ, PG, WMT)",
        "symbols": ["KO", "PEP", "JNJ", "PG", "WMT"],
        "default_strategy": "dip",
    },
]


# Config fields a runner is never allowed to set from user settings: the symbol
# and mode are owned by the runner itself, and credentials come from the desk.
PROTECTED_CONFIG_FIELDS = {
    "api_key",
    "secret_key",
    "paper",
    "symbol",
    "symbols",
    "strategy_mode",
    "trading_mode",
    "alpaca_live_api_key",
    "alpaca_live_secret_key",
    "alpaca_paper_api_key",
    "alpaca_paper_secret_key",
    "openai_api_key",
    "gemini_api_key",
    "anthropic_api_key",
    "xai_api_key",
}


def _coerce_like(current: Any, value: Any) -> Any:
    """Coerce an incoming setting to the type the Config field already holds."""
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if isinstance(current, int):
        return int(float(value))
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        return str(value)
    return value


def generate_auto_trade_name(
    symbols: list[str] | str,
    strategy_mode: str = "",
    settings: dict[str, Any] | None = None,
    engine_name: str | None = None,
    timeframe: str | None = None,
) -> str:
    """Generate a descriptive, human-readable auto-trade runner name from configured settings.

    Examples:
        - AAPL · SMA 10/30 (15Min)
        - NVDA · Dip 2% (15Min)
        - SPY · Day VWAP (1Min)
        - AAPL · AI Gemini (15Min)
        - QQQ · Long/Short (15Min)
        - AAPL, MSFT, NVDA · SMA 10/30 (15Min)
        - AAPL · Tech Alpha (15Min) (Custom engine)
    """
    clean_syms: list[str] = []
    if isinstance(symbols, str):
        clean_syms = [s.strip().upper() for s in re.split(r"[,;\s]+", symbols) if s.strip()]
    elif isinstance(symbols, (list, tuple, set)):
        for item in symbols:
            if isinstance(item, str):
                clean_syms.extend(s.strip().upper() for s in re.split(r"[,;\s]+", item) if s.strip())
            elif item:
                clean_syms.append(str(item).strip().upper())
    clean_syms = list(dict.fromkeys(clean_syms)) if clean_syms else ["AAPL"]

    if len(clean_syms) == 1:
        sym_str = clean_syms[0]
    elif len(clean_syms) <= 3:
        sym_str = ", ".join(clean_syms)
    else:
        sym_str = f"{', '.join(clean_syms[:2])} +{len(clean_syms) - 2}"

    st = dict(settings or {})
    mode = str(strategy_mode or st.get("strategy_mode") or "").lower().strip()
    tf = str(timeframe or st.get("bar_timeframe") or "15Min").strip()

    is_custom = bool(st.get("custom_engine_id")) or (
        bool(engine_name)
        and engine_name not in STRATEGY_NAMES.values()
        and engine_name.lower() not in STRATEGY_NAMES
    )

    if is_custom and engine_name:
        strat_part = engine_name.strip()
    elif mode == "sma":
        fast = st.get("fast_period", 10)
        slow = st.get("slow_period", 30)
        strat_part = f"SMA {fast}/{slow}"
    elif mode == "dip":
        dip_pct = st.get("dip_percentage", 2.0)
        try:
            dip_val = float(dip_pct)
            strat_part = f"Dip {dip_val:g}%"
        except (ValueError, TypeError):
            strat_part = f"Dip {dip_pct}%"
    elif mode == "day":
        day_type = str(st.get("day_strategy_type") or "vwap").lower().strip()
        day_labels = {
            "vwap": "VWAP",
            "orb": "ORB",
            "ema_cross": "EMA Cross",
            "mean_reversion": "Mean Reversion",
        }
        label = day_labels.get(day_type, day_type.upper())
        strat_part = f"Day {label}"
    elif mode == "ai":
        provider = str(st.get("ai_provider") or "AI").strip().capitalize()
        if provider.lower() in ("xai", "grok"):
            provider = "Grok"
        strat_part = f"AI {provider}"
    elif mode == "ls":
        strat_part = "Long/Short"
    else:
        strat_part = engine_name or STRATEGY_NAMES.get(mode, mode.upper() or "Standard")

    return f"{sym_str} · {strat_part} ({tf})"


class TickerRunner:
    """An isolated background auto-trade loop dedicated to one or more tickers."""

    def __init__(
        self,
        symbol: str | list[str] | None = None,
        strategy_mode: str = "",
        app_state: Any = None,
        custom_engine_id: str | None = None,
        engine_name: str | None = None,
        settings: dict[str, Any] | None = None,
        symbols: list[str] | str | None = None,
        name: str | None = None,
    ) -> None:
        raw_symbols = symbols if symbols is not None else symbol
        if raw_symbols is None and settings:
            raw_symbols = settings.get("symbols") or settings.get("symbol")

        clean_symbols: list[str] = []
        if isinstance(raw_symbols, str):
            clean_symbols = [s.strip().upper() for s in re.split(r"[,;\s]+", raw_symbols) if s.strip()]
        elif isinstance(raw_symbols, (list, tuple, set)):
            for item in raw_symbols:
                if isinstance(item, str):
                    clean_symbols.extend(s.strip().upper() for s in re.split(r"[,;\s]+", item) if s.strip())
                elif item:
                    clean_symbols.append(str(item).strip().upper())
        self.symbols: list[str] = list(dict.fromkeys(clean_symbols)) if clean_symbols else ["AAPL"]
        self.symbol: str = self.symbols[0]

        sym_slug = "_".join(self.symbols[:3])
        self.id = f"runner_{sym_slug}_{uuid.uuid4().hex[:8]}"
        self.app_state = app_state
        self.custom_engine_id = custom_engine_id or None
        self.settings = dict(settings or {})
        self.settings.pop("symbol", None)
        self.settings.pop("symbols", None)
        self._initial_name = name

        requested_mode = str(strategy_mode or "").lower().strip()
        engine: dict[str, Any] | None = None
        if self.custom_engine_id:
            missing_engine_id = self.custom_engine_id
            engine = custom_engine_store.get_custom_engine(
                self.custom_engine_id,
                user_id=str(getattr(app_state, "user_id", "") or ""),
            )
            if engine is None:
                self.custom_engine_id = None
                logger.warning(
                    "Custom engine %s not found for %s; falling back to the requested mode.",
                    missing_engine_id,
                    self.symbols,
                )

        if engine:
            # A custom engine carries its own base strategy and saved choices —
            # the same resolution the Auto Trade page applies when an engine is
            # loaded. An explicitly requested standard mode still wins.
            choices = dict(engine.get("choices") or {})
            base_mode = str(
                engine.get("base_engine") or choices.get("strategy_mode") or ""
            ).lower().strip()
            self.strategy_mode = requested_mode or base_mode or "sma"
            for key, value in choices.items():
                if key not in PROTECTED_CONFIG_FIELDS:
                    self.settings.setdefault(key, value)
            if engine.get("instructions"):
                self.settings.setdefault("ai_instructions", engine["instructions"])
            self.engine_name = engine_name or engine.get("name") or STRATEGY_NAMES.get(
                self.strategy_mode, self.strategy_mode.upper()
            )
        else:
            self.strategy_mode = requested_mode or "sma"
            self.engine_name = engine_name or STRATEGY_NAMES.get(
                self.strategy_mode, self.strategy_mode.upper()
            )

        if self.strategy_mode in UNSUPPORTED_MODES:
            raise ValueError(
                f"'{self.strategy_mode}' trades two legs at once and cannot run as a "
                f"standard auto-trade runner."
            )
        if self.strategy_mode not in STRATEGY_NAMES:
            raise ValueError(
                f"Unsupported strategy mode '{self.strategy_mode}'. "
                f"Choose one of: {', '.join(sorted(STRATEGY_NAMES))}."
            )

        self.poll_seconds = max(5, int(self.settings.get("poll_seconds") or 30))
        base_tf = (
            getattr(getattr(app_state, "settings", None), "bar_timeframe", None)
            or getattr(getattr(app_state, "config", None), "bar_timeframe", None)
            or "15Min"
        ) if app_state else "15Min"
        self.timeframe = str(self.settings.get("bar_timeframe") or base_tf)
        if self.strategy_mode == "day":
            self.timeframe = resolve_day_timeframe(self.timeframe)

        self.status = "idle"  # idle | running | stopping | stopped | error
        self.desired_running: bool = False
        self._last_restart_attempt: float = 0.0
        self._restart_backoff: float = 5.0
        self.created_at = time.time()
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.last_run_at: float | None = None
        self.last_signal: str | None = None
        self.last_price: float | None = None
        self.last_reason: str | None = None
        self.trades_count = int(self.settings.get("trades_count") or 0)
        self.cycles_count = int(self.settings.get("cycles_count") or 0)
        self.error: str | None = None
        self.realized_pl: float = float(self.settings.get("realized_pl") or 0.0)
        self.unrealized_pl: float = 0.0
        self.unrealized_pct: float | None = None
        self.total_pl: float = 0.0
        self.position_qty: float = 0.0
        self.position_side: str = "flat"
        self.position_cost_basis: float = 0.0
        self.position_avg_entry: float | None = None
        self.position_current_price: float | None = None

        # Per-ticker breakdown state
        self.symbols_data: dict[str, dict[str, Any]] = {
            s: {
                "symbol": s,
                "price": None,
                "last_price": None,
                "signal": "HOLD",
                "last_signal": "HOLD",
                "reason": None,
                "last_reason": None,
                "error": None,
                "position_qty": 0.0,
                "position_side": "flat",
                "cost_basis": 0.0,
                "avg_entry": None,
                "current_price": None,
                "unrealized_pl": 0.0,
                "unrealized_pct": None,
            }
            for s in self.symbols
        }

        # Performance & Win Rate Analytics
        self.trades_won: int = int(self.settings.get("trades_won") or 0)
        self.trades_lost: int = int(self.settings.get("trades_lost") or 0)
        self.gross_profit: float = float(self.settings.get("gross_profit") or 0.0)
        self.gross_loss: float = float(self.settings.get("gross_loss") or 0.0)

        # Risk Controls & Session Filter
        raw_max_loss = self.settings.get("max_loss_limit")
        self.max_loss_limit: float | None = (
            float(raw_max_loss)
            if raw_max_loss is not None and str(raw_max_loss).strip() != ""
            else None
        )
        raw_notional_cap = self.settings.get("max_notional_cap")
        self.max_notional_cap: float | None = (
            float(raw_notional_cap)
            if raw_notional_cap is not None and str(raw_notional_cap).strip() != ""
            else None
        )
        sess = str(self.settings.get("session_hours") or "regular").lower().strip()
        self.session_hours: str = sess if sess in ("regular", "all") else "regular"

        self.circuit_breaker_triggered: bool = False
        self.circuit_breaker_reason: str | None = None

        custom_name = str(self._initial_name or self.settings.get("name") or "").strip()
        if custom_name:
            self.name: str = custom_name
        else:
            self.name = generate_auto_trade_name(
                symbols=self.symbols,
                strategy_mode=self.strategy_mode,
                settings=self.settings,
                engine_name=self.engine_name,
                timeframe=self.timeframe,
            )
        self.settings["name"] = self.name

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def rename(self, new_name: str) -> str:
        """Rename the auto-trade runner. If new_name is empty, regenerate default name."""
        with self._lock:
            cleaned = str(new_name or "").strip()
            if cleaned:
                self.name = cleaned
            else:
                self.name = generate_auto_trade_name(
                    symbols=self.symbols,
                    strategy_mode=self.strategy_mode,
                    settings=self.settings,
                    engine_name=self.engine_name,
                    timeframe=self.timeframe,
                )
            self.settings["name"] = self.name
            return self.name

    @property
    def is_running(self) -> bool:
        with self._lock:
            return (
                self.status in ("running", "stopping")
                and self._thread is not None
                and self._thread.is_alive()
            )

    def start(self, from_watchdog: bool = False) -> None:
        """Start the background runner thread."""
        with self._lock:
            if self.is_running:
                return
            if not from_watchdog:
                self.desired_running = True
            self._stop_event.clear()
            self.status = "running"
            self.started_at = time.time()
            self.stopped_at = None
            self.error = None
            syms_tag = "-".join(self.symbols[:3])
            self._thread = threading.Thread(
                target=self._worker_loop,
                name=f"TickerRunner-{syms_tag}",
                daemon=True,
            )
            self._thread.start()
            logger.info(
                "%s multi-auto-trade runner for %s [%s]",
                "Restarted (watchdog)" if from_watchdog else "Started",
                ", ".join(self.symbols),
                self.engine_name,
            )

    def stop(self, wait: bool = True, timeout: float = 3.0) -> None:
        """Signal the runner to stop and optionally wait for termination."""
        self.desired_running = False
        self._stop_event.set()
        with self._lock:
            if self.status == "running":
                self.status = "stopping"
        if wait and self._thread and self._thread.is_alive():
            if threading.current_thread() != self._thread:
                self._thread.join(timeout=timeout)
            with self._lock:
                if not self._thread.is_alive():
                    self.status = "stopped"
                    self.stopped_at = time.time()
        logger.info("Signaled stop for multi-auto-trade runner %s", ", ".join(self.symbols))

    def _build_config(self) -> Config:
        """Construct an isolated Config instance for this runner."""
        base_cfg = self.app_state._base_config()
        overrides: dict[str, Any] = {
            "symbol": self.symbol,
            "symbols": tuple(self.symbols),
            "strategy_mode": self.strategy_mode,
            "poll_seconds": self.poll_seconds,
        }

        # Apply timeframe
        overrides["bar_timeframe"] = self.timeframe

        # Legacy alias kept for callers that still send the short key name.
        if (
            self.settings.get("metals_reversal_buy_on_stop") is None
            and self.settings.get("reversal_buy_on_stop") is not None
        ):
            overrides["metals_reversal_buy_on_stop"] = bool(
                self.settings["reversal_buy_on_stop"]
            )
        if (
            self.settings.get("metals_dollar_index_only") is None
            and self.settings.get("dollar_index_only") is not None
        ):
            overrides["metals_dollar_index_only"] = bool(
                self.settings["dollar_index_only"]
            )

        # Everything else that names a real Config field is applied as an
        # override, coerced to the type that field already holds. A whitelist
        # here would silently drop a custom engine's saved choices — the tuning
        # is the whole reason the engine was picked.
        for key, value in self.settings.items():
            if key in overrides or key in PROTECTED_CONFIG_FIELDS or value is None:
                continue
            if not hasattr(base_cfg, key):
                continue
            try:
                overrides[key] = _coerce_like(getattr(base_cfg, key), value)
            except (TypeError, ValueError):
                logger.debug(
                    "Ignoring non-coercible runner setting %s=%r for %s",
                    key,
                    value,
                    self.symbol,
                )

        # Ensure size_mode matches strategy mode constraints
        overrides["size_mode"] = resolve_size_mode(
            self.settings.get("size_mode") or getattr(base_cfg, "size_mode", "qty"),
            self.strategy_mode,
        )

        # Capital Allocation Cap
        if self.max_notional_cap is not None and self.max_notional_cap > 0:
            notional_val = overrides.get("trade_notional") or getattr(base_cfg, "trade_notional", None)
            if notional_val is not None:
                try:
                    overrides["trade_notional"] = min(float(notional_val), float(self.max_notional_cap))
                except (ValueError, TypeError):
                    pass

        return replace(base_cfg, **overrides)

    def _build_bot(self, cfg: Config) -> Any:
        """Create the appropriate bot instance for the strategy mode."""
        service = AlpacaService(cfg, synthetic_order_handler=self.app_state)
        approval_handler = getattr(self.app_state, "create_pending_approval", None)

        mode = cfg.strategy_mode
        if mode == "ai":
            return AiTradingBot(cfg, service=service, approval_handler=approval_handler)
        if mode == "day":
            return DayTradingBot(cfg, service=service, approval_handler=approval_handler)
        if mode == "ls":
            return LsTradingBot(cfg, service=service, approval_handler=approval_handler)
        return TradingBot(cfg, service=service, approval_handler=approval_handler)

    def _run_cycle(self) -> None:
        """Execute a single strategy cycle for all symbols in this runner."""
        self.app_state._require_live_execution()

        # Session Filter: enforce regular trading hours if configured
        if self.session_hours == "regular":
            try:
                base_cfg = self.app_state._base_config() if self.app_state else self._build_config()
                probe_service = AlpacaService(base_cfg)
                sess_info = probe_service.market_session()
                is_rth = bool(sess_info.get("session") == "regular" or sess_info.get("is_open"))
                curr_sess = str(sess_info.get("session") or "closed")
            except Exception as exc:
                logger.debug("Market session probe exception for %s: %s", self.symbols, exc)
                is_rth = True
                curr_sess = "unknown"

            if not is_rth:
                with self._lock:
                    self.cycles_count += 1
                    self.last_run_at = time.time()
                    self.last_signal = "HOLD"
                    self.last_reason = f"Session filter: market is {curr_sess} (regular hours only)"
                for sym in self.symbols:
                    try:
                        pos_detail = probe_service.get_position_detail(sym)
                        qty = float(pos_detail.get("qty") or 0.0)
                        side = pos_detail.get("side") or ("flat" if qty == 0 else "long")
                        unrealized = float(pos_detail.get("unrealized_pl") or 0.0)
                        with self._lock:
                            if sym not in self.symbols_data:
                                self.symbols_data[sym] = {"symbol": sym}
                            px = pos_detail.get("current_price")
                            self.symbols_data[sym].update({
                                "symbol": sym,
                                "price": px,
                                "current_price": px,
                                "last_price": px,
                                "avg_entry": pos_detail.get("avg_entry"),
                                "position_qty": qty,
                                "position_side": side,
                                "unrealized_pl": unrealized,
                                "unrealized_pct": pos_detail.get("unrealized_pct"),
                                "cost_basis": abs(float(pos_detail.get("market_value") or 0.0)),
                                "signal": "HOLD",
                                "last_signal": "HOLD",
                                "reason": self.last_reason,
                                "last_reason": self.last_reason,
                            })
                    except Exception as exc:
                        logger.debug("Failed updating off-session position for %s: %s", sym, exc)

                with self._lock:
                    self.unrealized_pl = round(sum(float(sd.get("unrealized_pl") or 0.0) for sd in self.symbols_data.values()), 2)
                    self.total_pl = round(self.realized_pl + self.unrealized_pl, 2)
                    primary_data = self.symbols_data.get(self.symbol, {})
                    self.position_qty = float(primary_data.get("position_qty") or 0.0)
                    self.position_side = str(primary_data.get("position_side") or "flat")
                    self.position_avg_entry = primary_data.get("avg_entry")
                    self.position_current_price = primary_data.get("current_price")
                    self.position_cost_basis = float(primary_data.get("cost_basis") or 0.0)
                    self.unrealized_pct = primary_data.get("unrealized_pct")
                return

        cfg = self._build_config()
        bot = self._build_bot(cfg)

        bundle = bot.run_once(should_stop=self._stop_event.is_set)
        primary = (bundle or {}).get("primary") or {}
        results = (bundle or {}).get("results") or []

        with self._lock:
            self.cycles_count += 1
            self.last_run_at = time.time()
            self.last_signal = primary.get("signal")
            primary_reason = primary.get("reason")
            if primary_reason and primary_reason != "no symbols":
                self.last_reason = primary_reason
            elif not self.last_reason or self.last_reason == "no symbols":
                valid_reasons = [r.get("reason") for r in results if r.get("reason") and r.get("reason") != "no symbols"]
                if valid_reasons:
                    self.last_reason = valid_reasons[0]
                elif not self.last_reason:
                    self.last_reason = "Cycle completed" if self.is_running else "Stopped"
            self.error = None

            # Check if trade occurred
            if primary.get("order_id"):
                self.trades_count += 1
            else:
                for res in results:
                    if res.get("order_id") or any(
                        act.get("order_id") for act in res.get("actions", [])
                    ):
                        self.trades_count += 1
                        break

            # Update per-symbol signals & reasons from results
            for res in results:
                sym = str(res.get("symbol") or "").upper()
                if sym:
                    if sym not in self.symbols_data:
                        self.symbols_data[sym] = {"symbol": sym}
                    px = res.get("price")
                    sig = res.get("signal") or "HOLD"
                    reason = res.get("reason")
                    self.symbols_data[sym].update({
                        "symbol": sym,
                        "signal": sig,
                        "last_signal": sig,
                        "price": px,
                        "current_price": px,
                        "last_price": px,
                        "reason": reason,
                        "last_reason": reason,
                        "error": res.get("error"),
                    })

            # Accumulate any realized P/L reported in results & track win/loss analytics
            for res in results:
                pnl_val = res.get("pnl") or res.get("realized_pl")
                if pnl_val is not None:
                    try:
                        pnl_f = float(pnl_val)
                        self.realized_pl = round(self.realized_pl + pnl_f, 2)
                        if pnl_f > 0.001:
                            self.trades_won += 1
                            self.gross_profit = round(self.gross_profit + pnl_f, 2)
                        elif pnl_f < -0.001:
                            self.trades_lost += 1
                            self.gross_loss = round(self.gross_loss + abs(pnl_f), 2)
                    except (ValueError, TypeError):
                        pass

        # Update current position details for all symbols in this runner
        for sym in self.symbols:
            try:
                pos_detail = bot.service.get_position_detail(sym)
                qty = float(pos_detail.get("qty") or 0.0)
                side = pos_detail.get("side") or ("flat" if qty == 0 else "long")
                unrealized = float(pos_detail.get("unrealized_pl") or 0.0)
                with self._lock:
                    if sym not in self.symbols_data:
                        self.symbols_data[sym] = {"symbol": sym}
                    cur_px = pos_detail.get("current_price") or self.symbols_data[sym].get("price") or self.symbols_data[sym].get("last_price")
                    self.symbols_data[sym].update({
                        "position_qty": qty,
                        "position_side": side,
                        "avg_entry": pos_detail.get("avg_entry"),
                        "current_price": cur_px,
                        "price": cur_px,
                        "last_price": cur_px,
                        "cost_basis": abs(float(pos_detail.get("market_value") or 0.0)),
                        "unrealized_pl": unrealized,
                        "unrealized_pct": pos_detail.get("unrealized_pct"),
                    })
            except Exception as exc:
                logger.debug("Failed updating position detail in runner for %s: %s", sym, exc)

        with self._lock:
            self.unrealized_pl = round(sum(float(sd.get("unrealized_pl") or 0.0) for sd in self.symbols_data.values()), 2)
            self.total_pl = round(self.realized_pl + self.unrealized_pl, 2)
            # Synchronize primary symbol fields
            primary_data = self.symbols_data.get(self.symbol, {})
            self.position_qty = float(primary_data.get("position_qty") or 0.0)
            self.position_side = str(primary_data.get("position_side") or "flat")
            self.position_avg_entry = primary_data.get("avg_entry")
            self.position_current_price = primary_data.get("current_price")
            self.position_cost_basis = float(primary_data.get("cost_basis") or 0.0)
            self.unrealized_pct = primary_data.get("unrealized_pct")
            if not self.last_price and primary_data.get("current_price"):
                self.last_price = primary_data.get("current_price")
            if not self.last_signal:
                self.last_signal = primary_data.get("signal") or "HOLD"

        # Record trade history into user's AppState if applicable
        try:
            history_items = self.app_state._expand_leg_history(
                results, self.engine_name or self.strategy_mode
            )
            if history_items:
                self.app_state._record_trade_history(history_items)
        except Exception as exc:
            logger.debug("Failed recording trade history in runner: %s", exc)

        # Auto Circuit Breaker: check if cumulative total loss or open unrealized loss exceeds limit
        if self.max_loss_limit is not None and self.max_loss_limit > 0:
            limit_val = abs(self.max_loss_limit)
            should_trip = False
            trip_reason = ""
            with self._lock:
                if self.total_pl <= -limit_val:
                    should_trip = True
                    trip_reason = f"Circuit breaker: total loss (${abs(self.total_pl):.2f}) reached limit (-${limit_val:.2f})"
                elif self.unrealized_pl <= -limit_val and any(abs(float(sd.get("position_qty") or 0.0)) > 1e-6 for sd in self.symbols_data.values()):
                    should_trip = True
                    trip_reason = f"Circuit breaker: unrealized loss (${abs(self.unrealized_pl):.2f}) reached limit (-${limit_val:.2f})"

            if should_trip:
                logger.warning("Circuit breaker triggered for %s: %s", self.symbols, trip_reason)
                with self._lock:
                    self.circuit_breaker_triggered = True
                    self.circuit_breaker_reason = trip_reason
                    self.error = trip_reason
                    self.last_reason = trip_reason
                    self.desired_running = False

                # Auto-close open positions for all symbols if any
                if self.app_state and hasattr(self.app_state, "close_single_position"):
                    for sym in self.symbols:
                        try:
                            service = AlpacaService(self.app_state._base_config())
                            pos = service.get_position_detail(sym)
                            pos_qty = abs(float(pos.get("qty") or 0.0)) if pos else 0.0
                            sym_qty = abs(float(self.symbols_data.get(sym, {}).get("position_qty") or 0.0))
                            qty = pos_qty if pos_qty > 0 else sym_qty
                            if qty > 0:
                                unrealized_before_close = (
                                    float(pos.get("unrealized_pl") or 0.0)
                                    if (pos and pos_qty > 0)
                                    else float(self.symbols_data.get(sym, {}).get("unrealized_pl") or 0.0)
                                )
                                self.app_state.close_single_position(
                                    sym, cancel_orders=True, bypass_autotrade_check=True
                                )
                                with self._lock:
                                    self.realized_pl = round(
                                        self.realized_pl + unrealized_before_close, 2
                                    )
                                    if unrealized_before_close > 0.001:
                                        self.trades_won += 1
                                        self.gross_profit = round(self.gross_profit + unrealized_before_close, 2)
                                    elif unrealized_before_close < -0.001:
                                        self.trades_lost += 1
                                        self.gross_loss = round(self.gross_loss + abs(unrealized_before_close), 2)
                                    if sym in self.symbols_data:
                                        self.symbols_data[sym]["unrealized_pl"] = 0.0
                                        self.symbols_data[sym]["unrealized_pct"] = 0.0
                                        self.symbols_data[sym]["position_qty"] = 0.0
                                        self.symbols_data[sym]["position_side"] = "flat"
                        except Exception as exc:
                            logger.warning("Circuit breaker failed closing position for %s: %s", sym, exc)

                with self._lock:
                    self.unrealized_pl = 0.0
                    self.unrealized_pct = 0.0
                    self.total_pl = self.realized_pl

                self.stop(wait=False)
                if hasattr(self.app_state, "_save_auto_trade_state"):
                    try:
                        self.app_state._save_auto_trade_state()
                    except Exception as exc:
                        logger.warning("Failed saving auto-trade state after circuit breaker: %s", exc)
                return

    def _worker_loop(self) -> None:
        """Background execution loop."""
        try:
            while not self._stop_event.is_set():
                cycle_start = time.time()
                try:
                    self._run_cycle()
                except Exception as exc:
                    logger.exception("Error in TickerRunner %s cycle: %s", ", ".join(self.symbols), exc)
                    with self._lock:
                        self.error = str(exc)
                        self.last_reason = f"Error: {exc}"

                if self._stop_event.is_set():
                    break

                # Sliced interruptible sleep
                poll_dur = max(5, int(self.poll_seconds))
                elapsed = time.time() - cycle_start
                sleep_rem = max(0.5, poll_dur - elapsed)
                step = 0.5
                while sleep_rem > 0 and not self._stop_event.is_set():
                    time.sleep(min(step, sleep_rem))
                    sleep_rem -= step

        finally:
            with self._lock:
                self.status = "stopped"
                self.stopped_at = time.time()
            logger.info("TickerRunner loop exited for %s", ", ".join(self.symbols))

    def snapshot(self) -> dict[str, Any]:
        """Return an immutable dictionary representing the runner's current telemetry."""
        with self._lock:
            running = self.is_running
            uptime = (
                round(time.time() - self.started_at, 1)
                if running and self.started_at
                else None
            )

            trade_qty = self.settings.get("trade_qty", 1)
            trade_notional = self.settings.get("trade_notional")
            size_mode = str(self.settings.get("size_mode") or "qty").lower()
            if size_mode == "notional" and trade_notional is not None:
                size_display = f"${float(trade_notional):.2f}"
            elif size_mode == "ai":
                size_display = "AI"
            else:
                size_display = f"{float(trade_qty or 1):g} sh"

            symbols_snapshot = {k: dict(v) for k, v in self.symbols_data.items()}
            unrealized_pl = self.unrealized_pl
            unrealized_pct = self.unrealized_pct
            pos_qty = self.position_qty
            pos_side = self.position_side
            pos_avg_entry = self.position_avg_entry
            cost_basis = self.position_cost_basis

            if len(self.symbols) > 1:
                total_desk_unrealized = 0.0
                total_desk_cb = 0.0
                has_any_desk = False
                if self.app_state:
                    desk_entries = getattr(self.app_state, "_desk_avg_entries", {})
                    for sym, sd in symbols_snapshot.items():
                        cached_desk = desk_entries.get(sym)
                        if cached_desk:
                            d_qty = float(cached_desk.get("qty") or 0.0)
                            if abs(d_qty) > 1e-6:
                                has_any_desk = True
                                sd["avg_entry"] = cached_desk.get("avg_entry_price") or sd.get("avg_entry")
                                cb = float(cached_desk.get("cost_basis") or 0.0)
                                if cb > 0:
                                    sd["cost_basis"] = cb
                                    u_pl = float(cached_desk.get("unrealized_pl") or sd.get("unrealized_pl") or 0.0)
                                    sd["unrealized_pl"] = u_pl
                                    sd["unrealized_pct"] = round((u_pl / cb) * 100.0, 3)
                                    total_desk_unrealized += u_pl
                                    total_desk_cb += cb
                if has_any_desk:
                    unrealized_pl = round(total_desk_unrealized, 2)
                    cost_basis = round(total_desk_cb, 2)
                    unrealized_pct = round((unrealized_pl / cost_basis) * 100.0, 3) if cost_basis > 0 else None
            else:
                if abs(pos_qty) > 1e-6 and self.app_state:
                    cached_desk = getattr(self.app_state, "_desk_avg_entries", {}).get(self.symbol)
                    if cached_desk:
                        desk_qty = float(cached_desk.get("qty") or 0.0)
                        if abs(desk_qty) > 1e-6 and (desk_qty > 0) == (pos_qty > 0):
                            pos_avg_entry = cached_desk.get("avg_entry_price") or pos_avg_entry
                            cb = float(cached_desk.get("cost_basis") or 0.0)
                            if cb > 0:
                                cost_basis = cb
                                unrealized_pl = float(cached_desk.get("unrealized_pl") or unrealized_pl)
                                unrealized_pct = round((unrealized_pl / cb) * 100.0, 3)
                elif abs(pos_qty) <= 1e-6:
                    pos_qty = 0.0
                    pos_side = "flat"
                    pos_avg_entry = None
                    cost_basis = 0.0
                    unrealized_pl = 0.0
                    unrealized_pct = None

            total_pl = round(self.realized_pl + unrealized_pl, 2)
            total_closed = self.trades_won + self.trades_lost
            win_rate = (
                round((self.trades_won / total_closed) * 100.0, 1)
                if total_closed > 0
                else None
            )
            profit_factor = (
                round(self.gross_profit / self.gross_loss, 2)
                if self.gross_loss > 0
                else (round(self.gross_profit, 2) if self.gross_profit > 0 else None)
            )

            return {
                "id": self.id,
                "name": self.name,
                "symbol": self.symbol,
                "symbols": list(self.symbols),
                "symbols_count": len(self.symbols),
                "symbols_data": symbols_snapshot,
                "strategy_mode": self.strategy_mode,
                "engine_name": self.engine_name,
                "custom_engine_id": self.custom_engine_id,
                "status": self.status,
                "is_running": running,
                "desired_running": self.desired_running,
                "timeframe": self.timeframe,
                "poll_seconds": self.poll_seconds,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "uptime_seconds": uptime,
                "last_run_at": self.last_run_at,
                "last_signal": self.last_signal,
                "last_price": self.last_price,
                "last_reason": self.last_reason,
                "cycles_count": self.cycles_count,
                "trades_count": self.trades_count,
                "trades_won": self.trades_won,
                "trades_lost": self.trades_lost,
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "gross_profit": round(self.gross_profit, 2),
                "gross_loss": round(self.gross_loss, 2),
                "max_loss_limit": self.max_loss_limit,
                "max_notional_cap": self.max_notional_cap,
                "session_hours": self.session_hours,
                "circuit_breaker_triggered": self.circuit_breaker_triggered,
                "circuit_breaker_reason": self.circuit_breaker_reason,
                "realized_pl": round(self.realized_pl, 2),
                "unrealized_pl": round(unrealized_pl, 2),
                "total_pl": total_pl,
                "unrealized_pct": round(unrealized_pct, 2) if unrealized_pct is not None else None,
                "position_qty": pos_qty,
                "position_side": pos_side,
                "position_avg_entry": pos_avg_entry,
                "position_cost_basis": round(cost_basis, 2),
                "error": self.error,
                "size_mode": size_mode,
                "size_display": size_display,
                "trade_notional": float(trade_notional) if trade_notional is not None else None,
                "trade_qty": float(trade_qty) if trade_qty is not None else None,
                "settings": {
                    k: v
                    for k, v in self.settings.items()
                    if k not in PROTECTED_CONFIG_FIELDS
                    and not str(k).endswith("_key")
                    and not str(k).endswith("_secret")
                    and "token" not in str(k).lower()
                    and "password" not in str(k).lower()
                },
            }


class MultiTradeManager:
    def __init__(self, app_state: Any) -> None:
        self.app_state = app_state
        self._runners: dict[str, TickerRunner] = {}  # Keyed by runner.id
        self._history: list[dict[str, Any]] = []  # Snapshots of previous runners
        self._lock = threading.RLock()

    def start_runner(
        self,
        symbol: str | list[str] | None = None,
        strategy_mode: str = "",
        custom_engine_id: str | None = None,
        engine_name: str | None = None,
        settings: dict[str, Any] | None = None,
        replace_existing: bool = True,
        symbols: list[str] | str | None = None,
        name: str | None = None,
    ) -> TickerRunner:
        """Start auto-trade for one or more symbols under a specified strategy."""
        raw_symbols = symbols if symbols is not None else symbol
        clean_symbols: list[str] = []
        if isinstance(raw_symbols, str):
            clean_symbols = [s.strip().upper() for s in re.split(r"[,;\s]+", raw_symbols) if s.strip()]
        elif isinstance(raw_symbols, (list, tuple, set)):
            for item in raw_symbols:
                if isinstance(item, str):
                    clean_symbols.extend(s.strip().upper() for s in re.split(r"[,;\s]+", item) if s.strip())
                elif item:
                    clean_symbols.append(str(item).strip().upper())
        elif settings and (settings.get("symbols") or settings.get("symbol")):
            raw = settings.get("symbols") or settings.get("symbol")
            if isinstance(raw, str):
                clean_symbols = [s.strip().upper() for s in re.split(r"[,;\s]+", raw) if s.strip()]
            else:
                clean_symbols = [str(s).strip().upper() for s in raw if str(s).strip()]

        clean_symbols = list(dict.fromkeys(clean_symbols))
        if not clean_symbols:
            raise ValueError("At least one valid ticker symbol is required.")

        if hasattr(self.app_state, "_require_live_execution"):
            self.app_state._require_live_execution()

        if hasattr(self.app_state, "_is_symbol_in_loop"):
            for s in clean_symbols:
                if self.app_state._is_symbol_in_loop(s):
                    raise ValueError(
                        f"Symbol '{s}' is already actively traded by the running main Auto Trade loop. Stop the main loop first."
                    )

        existing_to_stop: list[TickerRunner] = []
        with self._lock:
            for s in clean_symbols:
                for r in self._runners.values():
                    if (r.is_running or r.desired_running) and s in r.symbols:
                        if not replace_existing:
                            raise ValueError(
                                f"Auto-trade is already active for {s} ({r.engine_name}). Stop it first."
                            )
                        # A runner can now own a basket. Replacing it with a
                        # request for only one of its symbols would silently
                        # stop the other symbols in that basket, which is not
                        # a safe interpretation of "start AAPL". Expanding a
                        # one-symbol runner into a basket remains supported.
                        displaced_symbols = set(r.symbols) - set(clean_symbols)
                        if displaced_symbols:
                            displaced = ", ".join(sorted(displaced_symbols))
                            raise ValueError(
                                f"{s} is part of the active Auto-Trade basket "
                                f"({', '.join(r.symbols)}). This request would also stop "
                                f"{displaced}; stop or reconfigure that basket first."
                            )
                        if r not in existing_to_stop:
                            existing_to_stop.append(r)

        for ex in existing_to_stop:
            ex.stop(wait=True, timeout=3.0)
            with self._lock:
                snap = ex.snapshot()
                self._history = [h for h in self._history if h.get("id") != snap.get("id")]
                self._history.append(snap)
                if len(self._history) > 50:
                    self._history = self._history[-50:]

        runner = TickerRunner(
            symbols=clean_symbols,
            strategy_mode=strategy_mode,
            app_state=self.app_state,
            custom_engine_id=custom_engine_id,
            engine_name=engine_name,
            settings=settings,
            name=name,
        )
        with self._lock:
            self._runners[runner.id] = runner
        runner.start()
        if hasattr(self.app_state, "_save_auto_trade_state"):
            try:
                self.app_state._save_auto_trade_state()
            except Exception:
                pass
        return runner

    def start_batch(
        self,
        symbols: list[str] | str,
        strategy_mode: str = "",
        custom_engine_id: str | None = None,
        engine_name: str | None = None,
        settings: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> list[TickerRunner]:
        """Start auto-trade for multiple symbols under a specified strategy as ONE integrated runner."""
        runner = self.start_runner(
            symbols=symbols,
            strategy_mode=strategy_mode,
            custom_engine_id=custom_engine_id,
            engine_name=engine_name,
            settings=settings,
            replace_existing=True,
            name=name,
        )
        return [runner]

    def stop_runner(
        self,
        symbol_or_id: str,
        wait: bool = True,
        close_position: bool = False,
    ) -> bool:
        """Stop a runner by symbol or runner ID, optionally closing open positions for its symbols."""
        target = str(symbol_or_id or "").strip()
        target_upper = target.upper()
        runner_to_stop: TickerRunner | None = None
        with self._lock:
            if target in self._runners:
                runner_to_stop = self._runners[target]
            else:
                for r in self._runners.values():
                    if r.id == target or target_upper in r.symbols:
                        runner_to_stop = r
                        break

        if runner_to_stop:
            runner_to_stop.stop(wait=wait)

            if close_position and self.app_state and hasattr(self.app_state, "close_single_position"):
                for sym in runner_to_stop.symbols:
                    try:
                        pos = None
                        try:
                            service = AlpacaService(self.app_state._base_config())
                            pos = service.get_position_detail(sym)
                        except Exception:
                            pos = None
                        pos_qty = abs(float(pos.get("qty") or 0.0)) if pos else 0.0
                        sym_qty = abs(float(runner_to_stop.symbols_data.get(sym, {}).get("position_qty") or 0.0))
                        runner_qty = abs(float(runner_to_stop.position_qty or 0.0)) if sym == runner_to_stop.symbol else 0.0
                        qty = pos_qty if pos_qty > 0 else (sym_qty if sym_qty > 0 else runner_qty)
                        if qty > 0:
                            unrealized_before_close = (
                                float(pos.get("unrealized_pl") or 0.0)
                                if (pos and pos_qty > 0)
                                else float(runner_to_stop.symbols_data.get(sym, {}).get("unrealized_pl") or 0.0)
                            )
                            self.app_state.close_single_position(
                                sym, cancel_orders=True, bypass_autotrade_check=True
                            )
                            with runner_to_stop._lock:
                                runner_to_stop.realized_pl = round(
                                    runner_to_stop.realized_pl + unrealized_before_close, 2
                                )
                                if sym in runner_to_stop.symbols_data:
                                    runner_to_stop.symbols_data[sym]["unrealized_pl"] = 0.0
                                    runner_to_stop.symbols_data[sym]["unrealized_pct"] = 0.0
                                    runner_to_stop.symbols_data[sym]["position_qty"] = 0.0
                                    runner_to_stop.symbols_data[sym]["position_side"] = "flat"
                    except Exception as exc:
                        logger.warning("Failed closing position for %s on runner stop: %s", sym, exc)

                with runner_to_stop._lock:
                    runner_to_stop.unrealized_pl = 0.0
                    runner_to_stop.unrealized_pct = 0.0
                    runner_to_stop.total_pl = runner_to_stop.realized_pl
                    runner_to_stop.position_qty = 0.0
                    runner_to_stop.position_side = "flat"

            if hasattr(self.app_state, "_save_auto_trade_state"):
                try:
                    self.app_state._save_auto_trade_state()
                except Exception as exc:
                    logger.warning("Failed saving auto-trade state after runner stop: %s", exc)

            return True

        return False

    def stop_all(
        self,
        wait: bool = True,
        timeout: float = 3.0,
        close_positions: bool = False,
    ) -> int:
        """Stop all active auto-trade runners concurrently, optionally closing their open positions."""
        with self._lock:
            runners_to_stop = [r for r in self._runners.values() if r.is_running or r.desired_running]

        if not runners_to_stop:
            return 0

        # Signal stop to all runners in parallel first
        for runner in runners_to_stop:
            runner.stop(wait=False)

        # Wait for termination up to deadline
        if wait:
            deadline = time.time() + timeout
            for runner in runners_to_stop:
                rem = max(0.05, deadline - time.time())
                runner.stop(wait=True, timeout=rem)

        if close_positions and self.app_state and hasattr(self.app_state, "close_single_position"):
            for runner in runners_to_stop:
                for sym in runner.symbols:
                    try:
                        pos = None
                        try:
                            service = AlpacaService(self.app_state._base_config())
                            pos = service.get_position_detail(sym)
                        except Exception:
                            pos = None
                        pos_qty = abs(float(pos.get("qty") or 0.0)) if pos else 0.0
                        sym_qty = abs(float(runner.symbols_data.get(sym, {}).get("position_qty") or 0.0))
                        runner_qty = abs(float(runner.position_qty or 0.0)) if sym == runner.symbol else 0.0
                        qty = pos_qty if pos_qty > 0 else (sym_qty if sym_qty > 0 else runner_qty)
                        if qty > 0:
                            unrealized_before_close = (
                                float(pos.get("unrealized_pl") or 0.0)
                                if (pos and pos_qty > 0)
                                else float(runner.symbols_data.get(sym, {}).get("unrealized_pl") or 0.0)
                            )
                            self.app_state.close_single_position(
                                sym, cancel_orders=True, bypass_autotrade_check=True
                            )
                            with runner._lock:
                                runner.realized_pl = round(
                                    runner.realized_pl + unrealized_before_close, 2
                                )
                                if sym in runner.symbols_data:
                                    runner.symbols_data[sym]["unrealized_pl"] = 0.0
                                    runner.symbols_data[sym]["unrealized_pct"] = 0.0
                                    runner.symbols_data[sym]["position_qty"] = 0.0
                                    runner.symbols_data[sym]["position_side"] = "flat"
                    except Exception as exc:
                        logger.warning("Failed closing position for %s during stop_all: %s", sym, exc)
                with runner._lock:
                    runner.unrealized_pl = 0.0
                    runner.unrealized_pct = 0.0
                    runner.total_pl = runner.realized_pl
                    runner.position_qty = 0.0
                    runner.position_side = "flat"

        if hasattr(self.app_state, "_save_auto_trade_state"):
            try:
                self.app_state._save_auto_trade_state()
            except Exception:
                pass

        return len(runners_to_stop)

    def get_runner(self, symbol_or_id: str) -> TickerRunner | None:
        target = str(symbol_or_id or "").strip()
        target_upper = target.upper()
        with self._lock:
            if target in self._runners:
                return self._runners[target]
            for r in self._runners.values():
                if r.id == target or target_upper in r.symbols:
                    return r
        return None

    def is_running(self, symbol: str) -> bool:
        """Return True if an auto-trade runner is currently active for the given symbol."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return False
        with self._lock:
            for r in self._runners.values():
                if r.is_running and sym in r.symbols:
                    return True
        runner = self.get_runner(symbol)
        return bool(runner and getattr(runner, "is_running", False))

    def get_runner_summary(self, symbol: str) -> dict[str, Any] | None:
        sym = str(symbol or "").strip().upper()
        with self._lock:
            for r in self._runners.values():
                if sym in r.symbols:
                    return r.snapshot()
        return None

    def list_runners(self, active_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            runners = list(self._runners.values())
            history = list(self._history)

        result: list[dict[str, Any]] = []
        seen_ids = set()
        for r in runners:
            if active_only and not r.is_running:
                continue
            snap = r.snapshot()
            seen_ids.add(snap["id"])
            result.append(snap)

        if not active_only:
            for item in reversed(history):
                if item.get("id") not in seen_ids:
                    seen_ids.add(item.get("id"))
                    result.append(dict(item))

        # Sort running first, then recently started or stopped
        result.sort(
            key=lambda x: (
                1 if x.get("is_running") else 0,
                x.get("started_at") or x.get("created_at") or 0,
            ),
            reverse=True,
        )
        return result

    def remove_runner(self, symbol_or_id: str) -> bool:
        """Remove a runner from active map or history."""
        target = str(symbol_or_id or "").strip()
        target_upper = target.upper()
        removed = False
        with self._lock:
            found_id = None
            if target in self._runners:
                found_id = target
            else:
                for r_id, r in self._runners.items():
                    if r.id == target or target_upper in r.symbols:
                        found_id = r_id
                        break
            if found_id and found_id in self._runners:
                r = self._runners[found_id]
                if r.is_running:
                    r.stop(wait=True)
                del self._runners[found_id]
                removed = True

            # History cleanup: if target matches a runner ID or any symbol
            matched_history_by_id = any(h.get("id") == target for h in self._history)
            orig_len = len(self._history)
            if matched_history_by_id:
                self._history = [h for h in self._history if h.get("id") != target]
            else:
                self._history = [
                    h for h in self._history
                    if h.get("id") != target
                    and target_upper not in (h.get("symbols") or [h.get("symbol")])
                ]
            if len(self._history) < orig_len:
                removed = True

        if removed and hasattr(self.app_state, "_save_auto_trade_state"):
            try:
                self.app_state._save_auto_trade_state()
            except Exception:
                pass
        return removed

    def clear_stopped_runners(self) -> int:
        """Remove all stopped runners from active map and clear history."""
        cleared_count = 0
        with self._lock:
            stopped_ids = [
                r_id for r_id, r in self._runners.items()
                if not (getattr(r, "desired_running", False) or getattr(r, "is_running", False))
            ]
            for r_id in stopped_ids:
                del self._runners[r_id]
                cleared_count += 1

            cleared_count += len(self._history)
            self._history.clear()

        if cleared_count > 0 and hasattr(self.app_state, "_save_auto_trade_state"):
            try:
                self.app_state._save_auto_trade_state()
            except Exception:
                pass
        return cleared_count

    def rename_runner(self, symbol_or_id: str, new_name: str) -> bool:
        """Rename an active or stopped runner by symbol or ID and persist state."""
        target = str(symbol_or_id or "").strip()
        target_upper = target.upper()
        renamed = False
        with self._lock:
            # Check in active/known runners
            runner_to_rename: TickerRunner | None = None
            if target in self._runners:
                runner_to_rename = self._runners[target]
            else:
                for r in self._runners.values():
                    if r.id == target or target_upper in r.symbols:
                        runner_to_rename = r
                        break
            if runner_to_rename:
                runner_to_rename.rename(new_name)
                renamed = True

            # Also update any matching entry in _history
            for h in self._history:
                if (
                    h.get("id") == target
                    or (isinstance(h.get("symbols"), list) and target_upper in h.get("symbols"))
                    or h.get("symbol") == target_upper
                ):
                    cleaned = str(new_name or "").strip()
                    if cleaned:
                        h["name"] = cleaned
                    else:
                        h["name"] = generate_auto_trade_name(
                            symbols=h.get("symbols") or [h.get("symbol", "AAPL")],
                            strategy_mode=h.get("strategy_mode", ""),
                            settings=h.get("settings"),
                            engine_name=h.get("engine_name"),
                            timeframe=h.get("timeframe"),
                        )
                    renamed = True

        if renamed and hasattr(self.app_state, "_save_auto_trade_state"):
            try:
                self.app_state._save_auto_trade_state()
            except Exception:
                pass
        return renamed

    def restart_runner(self, symbol_or_id: str) -> TickerRunner:
        """Restart a stopped runner with its previous settings."""
        target = str(symbol_or_id or "").strip()
        target_upper = target.upper()
        cfg: dict[str, Any] | None = None
        with self._lock:
            # 1. Check active runners by exact ID first
            for r_id, r in self._runners.items():
                if r.id == target or r_id == target:
                    r_settings = dict(r.settings or {})
                    r_settings.update({
                        "realized_pl": r.realized_pl,
                        "trades_count": r.trades_count,
                        "cycles_count": r.cycles_count,
                        "trades_won": r.trades_won,
                        "trades_lost": r.trades_lost,
                        "gross_profit": r.gross_profit,
                        "gross_loss": r.gross_loss,
                    })
                    cfg = {
                        "name": r.name,
                        "symbols": list(r.symbols),
                        "strategy_mode": r.strategy_mode,
                        "custom_engine_id": r.custom_engine_id,
                        "engine_name": r.engine_name,
                        "settings": r_settings,
                    }
                    break

            # 2. Check history by exact ID
            if not cfg:
                for h in reversed(self._history):
                    if h.get("id") == target:
                        h_settings = dict(h.get("settings") or {})
                        for metric_key in ("realized_pl", "trades_count", "cycles_count", "trades_won", "trades_lost", "gross_profit", "gross_loss"):
                            if metric_key in h and metric_key not in h_settings:
                                h_settings[metric_key] = h[metric_key]
                        cfg = {
                            "name": h.get("name"),
                            "symbols": h.get("symbols") or [h.get("symbol")],
                            "strategy_mode": h.get("strategy_mode", ""),
                            "custom_engine_id": h.get("custom_engine_id"),
                            "engine_name": h.get("engine_name"),
                            "settings": h_settings,
                        }
                        break

            # 3. Check active runners by symbol
            if not cfg:
                for r in self._runners.values():
                    if target_upper in r.symbols:
                        r_settings = dict(r.settings or {})
                        r_settings.update({
                            "realized_pl": r.realized_pl,
                            "trades_count": r.trades_count,
                            "cycles_count": r.cycles_count,
                            "trades_won": r.trades_won,
                            "trades_lost": r.trades_lost,
                            "gross_profit": r.gross_profit,
                            "gross_loss": r.gross_loss,
                        })
                        cfg = {
                            "name": r.name,
                            "symbols": list(r.symbols),
                            "strategy_mode": r.strategy_mode,
                            "custom_engine_id": r.custom_engine_id,
                            "engine_name": r.engine_name,
                            "settings": r_settings,
                        }
                        break

            # 4. Check history by symbol
            if not cfg:
                for h in reversed(self._history):
                    h_syms = [str(s).upper() for s in (h.get("symbols") or [h.get("symbol")]) if s]
                    if target_upper in h_syms:
                        h_settings = dict(h.get("settings") or {})
                        for metric_key in ("realized_pl", "trades_count", "cycles_count", "trades_won", "trades_lost", "gross_profit", "gross_loss"):
                            if metric_key in h and metric_key not in h_settings:
                                h_settings[metric_key] = h[metric_key]
                        cfg = {
                            "name": h.get("name"),
                            "symbols": h.get("symbols") or [h.get("symbol")],
                            "strategy_mode": h.get("strategy_mode", ""),
                            "custom_engine_id": h.get("custom_engine_id"),
                            "engine_name": h.get("engine_name"),
                            "settings": h_settings,
                        }
                        break

        if not cfg or not cfg.get("symbols"):
            raise ValueError(f"No auto-trade runner found for '{symbol_or_id}' to restart.")

        return self.start_runner(
            symbols=cfg["symbols"],
            name=cfg.get("name"),
            strategy_mode=cfg.get("strategy_mode", ""),
            custom_engine_id=cfg.get("custom_engine_id"),
            engine_name=cfg.get("engine_name"),
            settings=cfg.get("settings"),
            replace_existing=True,
        )

    def list_active(self) -> list[dict[str, Any]]:
        return self.list_runners(active_only=True)

    @property
    def runners(self) -> dict[str, TickerRunner]:
        """Return a copy of the current runners mapping."""
        with self._lock:
            return dict(self._runners)

    def active_symbols_map(self) -> dict[str, dict[str, Any]]:
        """Return a mapping of symbol -> runner snapshot for all active runners."""
        with self._lock:
            result = {}
            for runner in self._runners.values():
                if runner.is_running:
                    snap = runner.snapshot()
                    for sym in runner.symbols:
                        result[sym] = snap
            return result

    def get_history_snapshots(self) -> list[dict[str, Any]]:
        """Return snapshots of previous/stopped runners for disk persistence."""
        with self._lock:
            stopped_snaps = [
                r.snapshot() for r in self._runners.values()
                if not (r.desired_running or r.is_running)
            ]
            seen_ids = {s.get("id") for s in stopped_snaps}
            combined = list(stopped_snaps)
            for h in self._history:
                if h.get("id") not in seen_ids:
                    seen_ids.add(h.get("id"))
                    combined.append(dict(h))
            return combined

    def restore_history(self, history: list[dict[str, Any]]) -> None:
        """Restore previous runner history snapshots from saved state."""
        if not isinstance(history, list):
            return
        with self._lock:
            seen_ids = {h.get("id") for h in self._history if h.get("id")}
            for item in history:
                if isinstance(item, dict) and item.get("id") and item.get("id") not in seen_ids:
                    item_copy = dict(item)
                    if not item_copy.get("name"):
                        item_copy["name"] = generate_auto_trade_name(
                            symbols=item_copy.get("symbols") or [item_copy.get("symbol", "AAPL")],
                            strategy_mode=item_copy.get("strategy_mode", ""),
                            settings=item_copy.get("settings"),
                            engine_name=item_copy.get("engine_name"),
                            timeframe=item_copy.get("timeframe"),
                        )
                    self._history.append(item_copy)
                    seen_ids.add(item["id"])
            if len(self._history) > 50:
                self._history = self._history[-50:]

    def get_active_runners_config(self) -> list[dict[str, Any]]:
        """Return configurations of runners that are actively running or desired to run."""
        configs = []
        with self._lock:
            for r in self._runners.values():
                if r.desired_running or r.is_running:
                    safe_settings = {
                        k: v
                        for k, v in r.settings.items()
                        if not str(k).endswith("_key") and not str(k).endswith("_secret")
                    }
                    safe_settings.update({
                        "realized_pl": r.realized_pl,
                        "trades_count": r.trades_count,
                        "cycles_count": r.cycles_count,
                        "trades_won": r.trades_won,
                        "trades_lost": r.trades_lost,
                        "gross_profit": r.gross_profit,
                        "gross_loss": r.gross_loss,
                    })
                    configs.append({
                        "id": r.id,
                        "name": r.name,
                        "symbols": list(r.symbols),
                        "symbol": r.symbol,
                        "strategy_mode": r.strategy_mode,
                        "custom_engine_id": r.custom_engine_id,
                        "engine_name": r.engine_name,
                        "settings": safe_settings,
                    })
        return configs

    def restore_from_config(self, configs: list[dict[str, Any]]) -> list[TickerRunner]:
        """Restore and start runners from saved configurations."""
        restored = []
        for cfg in configs:
            syms = cfg.get("symbols") or cfg.get("symbol")
            if not syms:
                continue
            try:
                runner = self.start_runner(
                    symbols=syms,
                    name=cfg.get("name"),
                    strategy_mode=cfg.get("strategy_mode", ""),
                    custom_engine_id=cfg.get("custom_engine_id"),
                    engine_name=cfg.get("engine_name"),
                    settings=cfg.get("settings"),
                    replace_existing=True,
                )
                restored.append(runner)
            except Exception as exc:
                logger.warning("Failed restoring runner for %s: %s", syms, exc)
        return restored

    def get_baskets(self) -> list[dict[str, Any]]:
        """Return predefined curated ticker baskets."""
        return [dict(b) for b in DEFAULT_BASKETS]

    def get_basket(self, basket_id: str) -> dict[str, Any] | None:
        """Lookup a predefined basket by ID."""
        target = str(basket_id or "").strip().lower()
        for b in DEFAULT_BASKETS:
            if b.get("id") == target:
                return dict(b)
        return None

    def check_and_recover_runners(self) -> list[str]:
        """Watchdog check: restart any runners whose desired state is ON but are stopped unexpectedly."""
        recovered = []
        now = time.time()
        with self._lock:
            candidates = [
                r for r in self._runners.values()
                if r.desired_running and not r.is_running and not getattr(r, "circuit_breaker_triggered", False)
            ]

        for r in candidates:
            if now - r._last_restart_attempt < r._restart_backoff:
                continue
            r._last_restart_attempt = now
            try:
                logger.warning(
                    "Multi-trade watchdog: runner for %s was unexpectedly stopped. Auto-restarting...",
                    ", ".join(r.symbols),
                )
                r.start(from_watchdog=True)
                r._restart_backoff = 5.0
                recovered.append(", ".join(r.symbols))
            except Exception as exc:
                logger.error("Multi-trade watchdog restart failed for %s: %s", ", ".join(r.symbols), exc)
                r._restart_backoff = min(30.0, r._restart_backoff * 1.5)

        return recovered
