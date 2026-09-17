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
            self._thread = threading.Thread(
                target=self._worker_loop,
                name=f"TickerRunner-{self.symbol}",
                daemon=True,
            )
            self._thread.start()
            logger.info(
                "%s multi-auto-trade runner for %s [%s]",
                "Restarted (watchdog)" if from_watchdog else "Started",
                self.symbol,
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
            if self.started_at:
                if running or self.status in ("running", "stopping"):
                    uptime = max(0, int(time.time() - self.started_at))
                elif self.stopped_at:
                    uptime = max(0, int(self.stopped_at - self.started_at))
                else:
                    uptime = max(0, int(time.time() - self.started_at))

            size_mode = str(self.settings.get("size_mode") or "qty").lower()
            trade_notional = self.settings.get("trade_notional")
            trade_qty = self.settings.get("trade_qty", 1)
            if size_mode == "notional" and trade_notional is not None:
                size_display = f"${float(trade_notional):.2f}"
            elif size_mode == "ai":
                size_display = "AI"
            else:
                size_display = f"{float(trade_qty or 1):g} sh"

            return {
                "id": self.id,
                "symbol": self.symbol,
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
    """Manages all ticker-specific auto-trade runners for a user's AppState."""

    def __init__(self, app_state: Any) -> None:
        self.app_state = app_state
        self._runners: dict[str, TickerRunner] = {}  # Keyed by UPPERCASE symbol
        self._history: list[dict[str, Any]] = []  # Snapshots of previous runners
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

        if hasattr(self.app_state, "_require_live_execution"):
            self.app_state._require_live_execution()

        if hasattr(self.app_state, "_is_symbol_in_loop") and self.app_state._is_symbol_in_loop(sym):
            raise ValueError(
                f"Symbol '{sym}' is already actively traded by the running main Auto Trade loop. Stop the main loop first."
            )

        existing: TickerRunner | None = None
        with self._lock:
            cand = self._runners.get(sym)
            if cand:
                if cand.is_running:
                    if not replace_existing:
                        raise ValueError(
                            f"Auto-trade is already active for {sym} ({cand.engine_name}). Stop it first."
                        )
                existing = cand

        if existing:
            if existing.is_running:
                existing.stop(wait=True, timeout=3.0)
            with self._lock:
                snap = existing.snapshot()
                self._history = [h for h in self._history if h.get("id") != snap.get("id")]
                self._history.append(snap)
                if len(self._history) > 50:
                    self._history = self._history[-50:]

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
        if hasattr(self.app_state, "_save_auto_trade_state"):
            try:
                self.app_state._save_auto_trade_state()
            except Exception:
                pass
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
        clean_symbols = [s.strip().upper() for s in symbols if s and str(s).strip()]
        if not clean_symbols:
            raise ValueError("At least one valid ticker symbol is required.")
        started: list[TickerRunner] = []
        for s in clean_symbols:
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
            if hasattr(self.app_state, "_save_auto_trade_state"):
                try:
                    self.app_state._save_auto_trade_state()
                except Exception:
                    pass
            return True

        return False

    def stop_all(self, wait: bool = True, timeout: float = 3.0) -> int:
        """Stop all active auto-trade runners concurrently."""
        with self._lock:
            runners_to_stop = [r for r in self._runners.values() if r.is_running or r.desired_running]

        for runner in runners_to_stop:
            runner.desired_running = False
            runner._stop_event.set()
            with runner._lock:
                if runner.status == "running":
                    runner.status = "stopping"

        if wait:
            deadline = time.time() + timeout
            for runner in runners_to_stop:
                rem = max(0.05, deadline - time.time())
                runner.stop(wait=True, timeout=rem)

        if hasattr(self.app_state, "_save_auto_trade_state"):
            try:
                self.app_state._save_auto_trade_state()
            except Exception:
                pass

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
        target = symbol_or_id.strip()
        target_upper = target.upper()
        removed = False
        with self._lock:
            # Check by ID in active runners first
            matched_sym = None
            for sym, r in self._runners.items():
                if r.id == target:
                    matched_sym = sym
                    break
            if matched_sym:
                r = self._runners[matched_sym]
                if r.is_running:
                    r.stop(wait=True)
                del self._runners[matched_sym]
                removed = True
            elif target_upper in self._runners:
                r = self._runners[target_upper]
                if r.is_running:
                    r.stop(wait=True)
                del self._runners[target_upper]
                removed = True

            # History cleanup: if target matches a runner ID, remove only that entry
            matched_history_by_id = any(h.get("id") == target for h in self._history)
            orig_len = len(self._history)
            if matched_history_by_id:
                self._history = [h for h in self._history if h.get("id") != target]
            else:
                self._history = [
                    h for h in self._history
                    if h.get("symbol") != target_upper and h.get("id") != target
                ]
            if len(self._history) < orig_len:
                removed = True

        if removed and hasattr(self.app_state, "_save_auto_trade_state"):
            try:
                self.app_state._save_auto_trade_state()
            except Exception:
                pass
        return removed

    def restart_runner(self, symbol_or_id: str) -> TickerRunner:
        """Restart a stopped runner with its previous settings."""
        target = symbol_or_id.strip()
        target_upper = target.upper()
        cfg: dict[str, Any] | None = None
        with self._lock:
            # 1. Check active runners by exact ID first
            for sym, r in self._runners.items():
                if r.id == target:
                    cfg = {
                        "symbol": r.symbol,
                        "strategy_mode": r.strategy_mode,
                        "custom_engine_id": r.custom_engine_id,
                        "engine_name": r.engine_name,
                        "settings": dict(r.settings or {}),
                    }
                    break

            # 2. Check history by exact ID
            if not cfg:
                for h in reversed(self._history):
                    if h.get("id") == target:
                        cfg = {
                            "symbol": h.get("symbol"),
                            "strategy_mode": h.get("strategy_mode", ""),
                            "custom_engine_id": h.get("custom_engine_id"),
                            "engine_name": h.get("engine_name"),
                            "settings": dict(h.get("settings") or {}),
                        }
                        break

            # 3. Check active runners by symbol
            if not cfg and target_upper in self._runners:
                r = self._runners[target_upper]
                cfg = {
                    "symbol": r.symbol,
                    "strategy_mode": r.strategy_mode,
                    "custom_engine_id": r.custom_engine_id,
                    "engine_name": r.engine_name,
                    "settings": dict(r.settings or {}),
                }

            # 4. Check history by symbol
            if not cfg:
                for h in reversed(self._history):
                    if h.get("symbol") == target_upper:
                        cfg = {
                            "symbol": h.get("symbol"),
                            "strategy_mode": h.get("strategy_mode", ""),
                            "custom_engine_id": h.get("custom_engine_id"),
                            "engine_name": h.get("engine_name"),
                            "settings": dict(h.get("settings") or {}),
                        }
                        break

        if not cfg or not cfg.get("symbol"):
            raise ValueError(f"No auto-trade runner found for '{symbol_or_id}' to restart.")

        return self.start_runner(
            symbol=cfg["symbol"],
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
            return {
                sym: runner.snapshot()
                for sym, runner in self._runners.items()
                if runner.is_running
            }

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
                    self._history.append(dict(item))
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
                    configs.append({
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
            sym = cfg.get("symbol")
            if not sym:
                continue
            try:
                runner = self.start_runner(
                    symbol=sym,
                    strategy_mode=cfg.get("strategy_mode", ""),
                    custom_engine_id=cfg.get("custom_engine_id"),
                    engine_name=cfg.get("engine_name"),
                    settings=cfg.get("settings"),
                    replace_existing=True,
                )
                restored.append(runner)
            except Exception as exc:
                logger.warning("Failed restoring runner for %s: %s", sym, exc)
        return restored

    def check_and_recover_runners(self) -> list[str]:
        """Watchdog check: restart any runners whose desired state is ON but are stopped unexpectedly."""
        recovered = []
        now = time.time()
        with self._lock:
            candidates = [
                r for r in self._runners.values()
                if r.desired_running and not r.is_running
            ]

        for r in candidates:
            if now - r._last_restart_attempt < r._restart_backoff:
                continue
            r._last_restart_attempt = now
            try:
                logger.warning(
                    "Multi-trade watchdog: runner for %s was unexpectedly stopped. Auto-restarting...",
                    r.symbol,
                )
                r.start(from_watchdog=True)
                r._restart_backoff = 5.0
                recovered.append(r.symbol)
            except Exception as exc:
                logger.error("Multi-trade watchdog restart failed for %s: %s", r.symbol, exc)
                r._restart_backoff = min(30.0, r._restart_backoff * 1.5)

        return recovered
