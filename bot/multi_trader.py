"""Multi Auto-Trade Engine and Ticker Runner Management.

Allows traders to run concurrent, independent auto-trade loops for individual
or multiple tickers, each under distinct strategies (SMA, Buy The Dip, AI, Day
Trading, Long/Short Trend, or Custom Engines).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from bot import custom_engine_store
from bot.client import AlpacaService
from bot.config import Config, resolve_day_timeframe
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

# Config fields a runner is never allowed to set from user settings: the symbol
# and mode are owned by the runner itself, and credentials come from the desk.
PROTECTED_CONFIG_FIELDS = {
    "api_key",
    "secret_key",
    "paper",
    "symbol",
    "symbols",
    "strategy_mode",
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


class TickerRunner:
    """An isolated background auto-trade loop dedicated to a single ticker."""

    def __init__(
        self,
        symbol: str,
        strategy_mode: str = "",
        app_state: Any = None,
        custom_engine_id: str | None = None,
        engine_name: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.id = f"runner_{symbol.upper()}_{uuid.uuid4().hex[:8]}"
        self.symbol = symbol.strip().upper()
        self.app_state = app_state
        self.custom_engine_id = custom_engine_id or None
        self.settings = dict(settings or {})

        requested_mode = str(strategy_mode or "").lower().strip()
        engine: dict[str, Any] | None = None
        if self.custom_engine_id:
            engine = custom_engine_store.get_custom_engine(
                self.custom_engine_id,
                user_id=str(getattr(app_state, "user_id", "") or ""),
            )
            if engine is None:
                logger.warning(
                    "Custom engine %s not found for %s; falling back to the requested mode.",
                    self.custom_engine_id,
                    self.symbol,
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
                f"single-ticker auto-trade runner."
            )
        if self.strategy_mode not in STRATEGY_NAMES:
            raise ValueError(
                f"Unsupported strategy mode '{self.strategy_mode}'. "
                f"Choose one of: {', '.join(sorted(STRATEGY_NAMES))}."
            )

        self.poll_seconds = max(5, int(self.settings.get("poll_seconds") or 30))
        self.timeframe = str(self.settings.get("bar_timeframe") or "15Min")
        if self.strategy_mode == "day":
            self.timeframe = resolve_day_timeframe(self.timeframe)

        self.status = "idle"  # idle | running | stopping | stopped | error
        self.created_at = time.time()
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.last_run_at: float | None = None
        self.last_signal: str | None = None
        self.last_price: float | None = None
        self.last_reason: str | None = None
        self.trades_count = 0
        self.cycles_count = 0
        self.error: str | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return (
                self.status in ("running", "stopping")
                and self._thread is not None
                and self._thread.is_alive()
            )

    def start(self) -> None:
        """Start the background runner thread."""
        with self._lock:
            if self.is_running:
                return
            self._stop_event.clear()
            self.status = "running"
            self.started_at = time.time()
            self.stopped_at = None
            self.error = None
            self._thread = threading.Thread(
                target=self._worker_loop,
                name=f"TickerRunner-{self.symbol}",
                daemon=True,
            )
            self._thread.start()
            logger.info(
                "Started multi-auto-trade runner for %s [%s]",
                self.symbol,
                self.engine_name,
            )

    def stop(self, wait: bool = True, timeout: float = 3.0) -> None:
        """Signal the runner to stop and optionally wait for termination."""
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
        logger.info("Signaled stop for multi-auto-trade runner %s", self.symbol)

    def _build_config(self) -> Config:
        """Construct an isolated Config instance for this runner."""
        base_cfg = self.app_state._base_config()
        overrides: dict[str, Any] = {
            "symbol": self.symbol,
            "symbols": (self.symbol,),
            "strategy_mode": self.strategy_mode,
            "poll_seconds": self.poll_seconds,
        }

        # Apply timeframe
        if self.strategy_mode == "day":
            overrides["bar_timeframe"] = resolve_day_timeframe(
                self.settings.get("bar_timeframe") or base_cfg.bar_timeframe
            )
        elif "bar_timeframe" in self.settings:
            overrides["bar_timeframe"] = str(self.settings["bar_timeframe"])

        # Legacy alias kept for callers that still send the short key name.
        if (
            self.settings.get("metals_reversal_buy_on_stop") is None
            and self.settings.get("reversal_buy_on_stop") is not None
        ):
            overrides["metals_reversal_buy_on_stop"] = bool(
                self.settings["reversal_buy_on_stop"]
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
        """Execute a single strategy cycle for this symbol."""
        self.app_state._require_live_execution()
        cfg = self._build_config()
        bot = self._build_bot(cfg)

        bundle = bot.run_once(should_stop=self._stop_event.is_set)
        primary = (bundle or {}).get("primary") or {}
        results = (bundle or {}).get("results") or []

        with self._lock:
            self.cycles_count += 1
            self.last_run_at = time.time()
            self.last_signal = primary.get("signal")
            self.last_price = primary.get("price")
            self.last_reason = primary.get("reason")
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

        # Record trade history into user's AppState if applicable
        try:
            history_items = self.app_state._expand_leg_history(
                results, self.strategy_mode
            )
            if history_items:
                self.app_state._record_trade_history(history_items)
        except Exception as exc:
            logger.debug("Failed recording trade history in runner: %s", exc)

    def _worker_loop(self) -> None:
        """Background execution loop."""
        try:
            while not self._stop_event.is_set():
                cycle_start = time.time()
                try:
                    self._run_cycle()
                except Exception as exc:
                    logger.exception("Error in TickerRunner %s cycle: %s", self.symbol, exc)
                    with self._lock:
                        self.error = str(exc)
                        self.last_reason = f"Error: {exc}"

                if self._stop_event.is_set():
                    break

                # Sliced interruptible sleep
                poll_target = max(5, self.poll_seconds)
                elapsed = time.time() - cycle_start
                remaining = max(1.0, poll_target - elapsed)

                if self._stop_event.wait(timeout=remaining):
                    break

        finally:
            with self._lock:
                self.status = "stopped"
                self.stopped_at = time.time()
            logger.info("TickerRunner loop terminated for %s", self.symbol)

    def snapshot(self) -> dict[str, Any]:
        """Serializable dictionary representation of the runner."""
        with self._lock:
            running = (
                self.status in ("running", "stopping")
                and self._thread is not None
                and self._thread.is_alive()
            )
            uptime = None
            if self.started_at and self.status == "running":
                uptime = max(0, int(time.time() - self.started_at))
            elif self.started_at and self.stopped_at:
                uptime = max(0, int(self.stopped_at - self.started_at))

            return {
                "id": self.id,
                "symbol": self.symbol,
                "strategy_mode": self.strategy_mode,
                "engine_name": self.engine_name,
                "custom_engine_id": self.custom_engine_id,
                "status": self.status,
                "is_running": running,
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
                "error": self.error,
                "settings": {
                    k: v
                    for k, v in self.settings.items()
                    if not k.endswith("_key") and not k.endswith("_secret")
                },
            }


class MultiTradeManager:
    """Manages all ticker-specific auto-trade runners for a user's AppState."""

    def __init__(self, app_state: Any) -> None:
        self.app_state = app_state
        self._runners: dict[str, TickerRunner] = {}  # Keyed by UPPERCASE symbol
        self._lock = threading.RLock()

    def start_runner(
        self,
        symbol: str,
        strategy_mode: str = "",
        custom_engine_id: str | None = None,
        engine_name: str | None = None,
        settings: dict[str, Any] | None = None,
        replace_existing: bool = True,
    ) -> TickerRunner:
        """Start auto-trade for a specific symbol."""
        sym = symbol.strip().upper()
        if not sym:
            raise ValueError("Symbol cannot be empty.")

        existing: TickerRunner | None = None
        with self._lock:
            cand = self._runners.get(sym)
            if cand and cand.is_running:
                if not replace_existing:
                    raise ValueError(
                        f"Auto-trade is already active for {sym} ({cand.engine_name}). Stop it first."
                    )
                existing = cand

        if existing:
            existing.stop(wait=True, timeout=3.0)

        runner = TickerRunner(
            symbol=sym,
            strategy_mode=strategy_mode,
            app_state=self.app_state,
            custom_engine_id=custom_engine_id,
            engine_name=engine_name,
            settings=settings,
        )
        with self._lock:
            self._runners[sym] = runner
        runner.start()
        return runner

    def start_batch(
        self,
        symbols: list[str],
        strategy_mode: str = "",
        custom_engine_id: str | None = None,
        engine_name: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> list[TickerRunner]:
        """Start auto-trade for multiple symbols under a specified strategy."""
        started: list[TickerRunner] = []
        for sym in symbols:
            s = sym.strip().upper()
            if not s:
                continue
            runner = self.start_runner(
                symbol=s,
                strategy_mode=strategy_mode,
                custom_engine_id=custom_engine_id,
                engine_name=engine_name,
                settings=settings,
                replace_existing=True,
            )
            started.append(runner)
        return started

    def stop_runner(self, symbol_or_id: str, wait: bool = True) -> bool:
        """Stop a runner by symbol or runner ID."""
        target = symbol_or_id.strip().upper()
        runner_to_stop: TickerRunner | None = None
        with self._lock:
            # First check if matched by symbol
            if target in self._runners:
                runner_to_stop = self._runners[target]
            else:
                # Check if matched by ID
                for sym, runner in self._runners.items():
                    if runner.id == symbol_or_id:
                        runner_to_stop = runner
                        break

        if runner_to_stop:
            runner_to_stop.stop(wait=wait)
            return True

        return False

    def stop_all(self, wait: bool = True) -> int:
        """Stop all active auto-trade runners."""
        with self._lock:
            runners_to_stop = [r for r in self._runners.values() if r.is_running]

        for runner in runners_to_stop:
            runner.stop(wait=wait)

        return len(runners_to_stop)

    def get_runner(self, symbol: str) -> TickerRunner | None:
        sym = symbol.strip().upper()
        with self._lock:
            return self._runners.get(sym)

    def is_running(self, symbol: str) -> bool:
        """Return True if an auto-trade runner is currently active for the given symbol."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return False
        runner = self.get_runner(sym)
        return bool(runner and runner.is_running)

    def get_runner_summary(self, symbol: str) -> dict[str, Any] | None:
        sym = symbol.strip().upper()
        with self._lock:
            runner = self._runners.get(sym)
            if runner:
                return runner.snapshot()
        return None

    def list_runners(self, active_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            runners = list(self._runners.values())

        result: list[dict[str, Any]] = []
        for r in runners:
            if active_only and not r.is_running:
                continue
            result.append(r.snapshot())

        # Sort running first, then recently started
        result.sort(
            key=lambda x: (
                1 if x.get("is_running") else 0,
                x.get("started_at") or 0,
            ),
            reverse=True,
        )
        return result

    def list_active(self) -> list[dict[str, Any]]:
        return self.list_runners(active_only=True)

    def active_symbols_map(self) -> dict[str, dict[str, Any]]:
        """Return a mapping of symbol -> runner snapshot for all active runners."""
        with self._lock:
            return {
                sym: runner.snapshot()
                for sym, runner in self._runners.items()
                if runner.is_running
            }
