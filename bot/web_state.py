"""Shared runtime state for the web trading app."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.trading.enums import OrderSide

logger = logging.getLogger(__name__)

from bot.ai_models import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENAI_MODEL,
    catalog_payload,
)
from bot.ai_presets import (
    DEFAULT_PRESET_ID,
    get_preset,
    instructions_for,
    list_presets,
    resolve_preset_id,
    risk_profile_for,
)
from bot.ai_trader import AiTradingBot
from bot.alpaca_errors import (
    broker_error_kind,
    describe_plan_read_error,
    humanize_alpaca_error,
)
from bot.backtest import (
    build_strategy,
    parse_backtest_symbols,
    run_backtest,
    run_portfolio_backtest,
    summary_from_result,
)
from bot import backtest_store, dip_hunt_store, followon_store, reinvest_store
from bot.dip_hunt import (
    ACTIVE_STATUSES as _DIP_HUNT_ACTIVE,
    CANCELLABLE_STATUSES as _DIP_HUNT_CANCELLABLE,
    DIP_PCT_DEFAULT,
    DIP_PCT_MAX,
    WAIT_MINUTES_DEFAULT,
    WAIT_MINUTES_MAX,
    hunt_action,
    target_buy_price,
)
from bot.analysis import daily_bar_stats
from bot.client import (
    ATTACHABLE_ENTRY_TYPES as _ATTACHABLE_ENTRY_TYPES,
    MANUAL_ORDER_TYPES,
    MANUAL_TIME_IN_FORCE,
    AlpacaService,
    limit_price_for_stop,
    normalize_stock_order_price,
    normalize_stop_exit_limit,
    whole_qty_for_attached_stop,
)
from bot.config import (
    Config,
    DEFAULT_LANG,
    alpaca_slot_status,
    live_allowed_from_env,
    normalize_lang,
    paper_mode_from_env,
    resolve_size_mode,
)
from bot.desk_risk import atr_from_bars, risk_qty_for, stop_distance_for
from bot.earnings import fetch_earnings
from bot.manual_guards import manual_entry_breaches, portfolio_heat
from bot.env_store import mask_secret, remove_env_keys, upsert_env_values
from bot.history_insights import (
    build_facts,
    desk_events_from_result,
    entry_context_from_result,
    narrate,
    parse_query,
)
from bot import lessons_store
from bot.dip_presets import (
    DEFAULT_PRESET_ID as DEFAULT_DIP_PRESET_ID,
    get_preset as get_dip_preset,
    list_presets as list_dip_presets,
    match_preset_id as match_dip_preset_id,
    resolve_preset_id as resolve_dip_preset_id,
)
from bot.ls_backtest import LSRiskParams, run_ls_backtest, run_ls_portfolio_backtest
from bot.ls_strategy import LongShortRegimeStrategy
from bot.ls_trader import LsTradingBot
from bot.pair_backtest import build_pair_strategy, run_pair_backtest
from bot.pair_presets import (
    DEFAULT_PRESET_ID as DEFAULT_PAIR_PRESET_ID,
    get_preset as get_pair_preset,
    list_presets as list_pair_presets,
    match_preset_id as match_pair_preset_id,
    normalize_weak_side,
    resolve_preset_id as resolve_pair_preset_id,
)
from bot.pair_strategy import parse_pair_symbols
from bot.pair_trader import PairTradingBot
from bot.settings_store import SETTINGS_PATH, load_settings, save_settings
from bot.sma_presets import (
    DEFAULT_PRESET_ID as DEFAULT_SMA_PRESET_ID,
    get_preset as get_sma_preset,
    list_presets as list_sma_presets,
    match_preset_id,
    resolve_preset_id as resolve_sma_preset_id,
)
from bot.strategy import StrategyResult
from bot.trader import TradingBot


@dataclass
class RunSettings:
    symbol: str = "AAPL"
    symbols: str = "AAPL"
    fast_sma: int = 10
    slow_sma: int = 30
    sma_preset: str = DEFAULT_SMA_PRESET_ID
    dip_preset: str = DEFAULT_DIP_PRESET_ID
    dip_rsi_buy: float = 30.0
    dip_rsi_sell: float = 60.0
    dip_skip_bearish: bool = True
    trade_qty: float = 1.0
    size_mode: str = "qty"  # qty | notional | ai (ai only in AI strategy mode)
    trade_notional: float = 100.0
    bar_timeframe: str = "15Min"
    poll_seconds: int = 20
    strategy_mode: str = "sma"
    pair_preset: str = DEFAULT_PAIR_PRESET_ID
    pair_sma_period: int = 50
    pair_lookback: int = 7
    pair_impulse_pct: float = 5.0
    pair_weak_side: str = "LONG"
    pair_long_symbol: str = ""
    pair_short_symbol: str = ""
    ls_ema_fast: int = 21
    ls_ema_slow: int = 55
    ls_adx_min: float = 20.0
    ls_atr_stop_mult: float = 1.5
    ls_risk_pct: float = 1.0
    ls_rr: float = 2.0
    ls_time_stop_bars: int = 15
    ai_provider: str = "openai"
    ai_preset: str = DEFAULT_PRESET_ID
    ai_instructions: str = ""
    ai_min_confidence: float = 0.55
    openai_model: str = DEFAULT_OPENAI_MODEL
    gemini_model: str = DEFAULT_GEMINI_MODEL
    stop_loss_pct: float = 0.0  # 0 = off
    # AI risk engine — see bot/ai_risk.py. Named presets set their own geometry.
    ai_risk_pct: float = 0.5
    ai_atr_stop_mult: float = 1.8
    ai_take_profit_r: float = 2.0
    ai_trail_after_r: float = 1.0
    ai_max_positions: int = 3
    ai_daily_loss_limit_pct: float = 3.0
    ai_min_hold_minutes: int = 15
    ai_cooldown_minutes: int = 60
    ai_max_spread_bps: float = 25.0
    # 0 = stop-market after trigger; >0 = stop-limit cushion % past the stop.
    stop_limit_offset_pct: float = 0.0
    # Desk language. Drives the UI strings and the language the AI writes
    # its thesis / risks in — see bot.config.LANGUAGES.
    lang: str = DEFAULT_LANG


ALLOWED_TIMEFRAMES = ("1Min", "5Min", "15Min", "1Hour", "1Day")

# UI range → Alpaca portfolio history period + lookback for closed orders.
# YTD uses start=Jan 1 (not period="all") so Account P&L matches the chip.
_HISTORY_RANGES: dict[str, dict[str, Any]] = {
    "day": {"period": "1D", "days": 1},
    "week": {"period": "1W", "days": 7},
    "month": {"period": "1M", "days": 31},
    "quarter": {"period": "3M", "days": 93},
    "ytd": {"period": None, "days": None},  # Jan 1 → now via start=
    "all": {"period": "all", "days": None},
    "custom": {"period": None, "days": None},  # explicit start/end
}
_ET = ZoneInfo("America/New_York")
# Positions reconstruction still reads closed orders; 500 is Alpaca's per-page max.
_FIFO_WINDOW_LIMIT = 500
# Walk enough pages that an active symbol's year-long fill history usually fits
# before we fall back to a carried average-entry lot.
_FIFO_WINDOW_MAX_PAGES = 10
# Account activities are the actual execution ledger (including partial fills).
# Alpaca caps each activities page at 100; page tokens let History walk all of it.
_FILL_PAGE_SIZE = 100
# Reconstructing the opening inventory of a range that ended in the past means
# reading the fills since. Bounded: past this, History says so instead of
# seeding FIFO from a walk it could not finish.
_TRAILING_FILL_PAGES = 20
# ATR14 only changes when a bar closes, so the Manual Order preview can reuse it
# across the debounced refreshes a symbol edit fires.
_MANUAL_ATR_TTL = 60.0
# Manual tickets size off daily volatility whatever the loop is scanning — see
# `AppState._manual_atr`.
_MANUAL_ATR_TIMEFRAME = "1Day"
# Enough daily bars for a 52-week range; ATR14 needs a fraction of it, and one
# fetch feeding both keeps a ticket refresh to a single bar request.
_MANUAL_BAR_LIMIT = 260
# Tradability flags change about as often as a listing does.
_MANUAL_ASSET_TTL = 900.0
# Earnings scraping is slow and rate-limited upstream; the ticket only needs to
# know whether a print is near, which does not change minute to minute.
_MANUAL_EARNINGS_TTL = 1800.0
# Book-wide risk is read on every ticket refresh, so it gets a short cache
# rather than two extra broker calls per keystroke.
_MANUAL_HEAT_TTL = 20.0

# A ticket resubmitted inside this window with identical terms is treated as a
# double-click or a retry, not as a second order the user actually wants.
_MANUAL_DUPLICATE_WINDOW = 30.0

# What a manual ticket can do. Alpaca has two sides; a desk that can short has
# four actions, and collapsing them would make "sell" mean either "take profit"
# or "open a short" depending on invisible state. See `place_manual_order`.
_MANUAL_ACTIONS = frozenset({"buy", "sell", "short", "cover"})
_MANUAL_EXIT_ACTIONS = frozenset({"sell", "cover"})

# Re-investment: how often the watcher asks the broker whether the sell filled.
# A sell that is going to fill fills within seconds; one that rests can wait.
_REINVEST_POLL_SECONDS = 5.0
# expire_minutes is the fill window after the sell fills, not a sell-watch timeout.
_REINVEST_DEFAULT_MINUTES = 120
_REINVEST_MAX_MINUTES = 1440
# Transient broker/network errors are survivable; a wall of them is not.
_REINVEST_MAX_ERRORS = 12
_REINVEST_LIVE_STATUSES = frozenset({"waiting", "placing", "awaiting_fill"})
_FOLLOWON_LIVE_STATUSES = frozenset({"waiting", "placing"})

# Next ticket after a close: same poll cadence as a buy-back.
# expire_minutes is the send window after the close fills, not a fill timeout.
_FOLLOWON_POLL_SECONDS = 5.0
_FOLLOWON_DEFAULT_MINUTES = 120
_FOLLOWON_MAX_MINUTES = 1440
_FOLLOWON_MAX_ERRORS = 12
_FOLLOWON_FLAT_MAX_CHECKS = 6

# Dip hunt: same poll cadence as re-investment — a stop-out fills in seconds,
# a parked limit may rest for hours.
_DIP_HUNT_POLL_SECONDS = 5.0
_DIP_HUNT_MAX_ERRORS = 12


def _blotter_window_bound(
    value: str, *, field: str, end_of_day: bool = False
) -> datetime | None:
    """A `YYYY-MM-DD` blotter bound as an aware UTC datetime, or None.

    ``until`` is inclusive to the operator, who picks a day and expects that
    whole day back, so it resolves to the day's last instant rather than its
    first.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        day = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{field} must be a YYYY-MM-DD date") from exc
    if end_of_day:
        day = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day.replace(tzinfo=timezone.utc)


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.settings = RunSettings()
        self.loop_running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.loop_stopping = False
        self._poll_seq: int = 0
        self._active_poll: int | None = None
        self.result_history: deque[dict[str, Any]] = deque(maxlen=100)
        self._history_seq: int = 0
        # Cycle outcomes that never become a fill — risk-gate blocks, market
        # closed, open-order skips, stop moves, scale-outs. History's execution
        # audit reads these; without them those events exist only in the log.
        self.desk_events: deque[dict[str, Any]] = deque(maxlen=500)
        # Narrations keyed by range + filters + data state, so re-opening the
        # review on unchanged data never bills a second model call.
        self._insight_cache: dict[str, dict[str, Any]] = {}
        self.loop_sessions: deque[dict[str, Any]] = deque(maxlen=30)
        self._loop_session_seq: int = 0
        self._active_loop_session: dict[str, Any] | None = None
        self.loop_started_at: float | None = None
        self.loop_last_duration_seconds: float | None = None
        self.account: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        # Per-symbol results from the latest cycle (SMA or AI watchlist).
        self.last_ai_results: list[dict[str, Any]] | None = None
        self.quote: dict[str, Any] | None = None
        self.last_position: float = 0.0
        self.error: str | None = None
        self._quote_fetched_at: float = 0.0
        self._quote_symbol: str | None = None
        # symbol -> (expires_monotonic, atr14, day stats). Manual Order refreshes
        # on every symbol edit; daily bars only move once a bar, so a short TTL
        # keeps the ticket's size preview honest without a fetch per keystroke.
        self._manual_atr_cache: dict[
            str, tuple[float, float | None, dict[str, Any]]
        ] = {}
        # Tradability flags and earnings proximity, on much longer TTLs — see
        # `_MANUAL_ASSET_TTL` / `_MANUAL_EARNINGS_TTL`.
        self._manual_asset_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._manual_earnings_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # Book-wide open risk, shared by every symbol rather than per-symbol.
        self._manual_heat_cache: tuple[float, dict[str, Any]] | None = None
        # Fingerprint of the last submitted ticket, so a double-click or a retry
        # after a slow response cannot quietly place the same order twice.
        self._last_manual_ticket: tuple[float, str, dict[str, Any]] | None = None
        # Re-investment plans: a sell ticket that has asked the desk to buy the
        # shares back once it fills. Keyed by plan id, newest first in the UI.
        # Mirrored to disk (see `bot.reinvest_store`) so a restart resumes the
        # promise instead of silently dropping it.
        self.reinvest_plans: dict[str, dict[str, Any]] = {}
        self._reinvest_seq: int = 0
        self._reinvest_thread: threading.Thread | None = None
        self._reinvest_stop = threading.Event()
        self.followon_plans: dict[str, dict[str, Any]] = {}
        self._followon_seq: int = 0
        self._followon_thread: threading.Thread | None = None
        self._followon_stop = threading.Event()
        # Order ids being cancel-and-resubmitted (accepted → new ticket).
        # Watchers must not drop the desk plan while the close is mid-rewrite.
        self._rewriting_order_ids: set[str] = set()
        # Dip-hunt plans: a buy ticket that asked the desk to re-enter cheaper
        # after its stop fills. Same persistence story as re-investment.
        self.dip_hunt_plans: dict[str, dict[str, Any]] = {}
        self._dip_hunt_seq: int = 0
        self._dip_hunt_thread: threading.Thread | None = None
        self._dip_hunt_stop = threading.Event()
        self.ai_ready: dict[str, bool] = {"openai": False, "gemini": False}
        # UI / session overrides — never dumped raw into status settings.
        self._openai_api_key: str | None = None
        self._gemini_api_key: str | None = None
        self._openai_key_source: str = "none"  # none | env | ui
        self._gemini_key_source: str = "none"
        # True while the desk is in Live mode (set on switch / startup).
        self._live_session_authorized: bool = not paper_mode_from_env()

    def _bound_worker(self, fn):
        return fn

    def _begin_poll(self) -> int:
        with self.lock:
            self._poll_seq += 1
            self._active_poll = self._poll_seq
            return self._active_poll

    def _end_poll(self) -> None:
        with self.lock:
            self._active_poll = None

    def clear_history(self) -> None:
        with self.lock:
            self.result_history.clear()
            self.desk_events.clear()
            self._insight_cache.clear()
            self._history_seq = 0
            self.loop_sessions.clear()
            self._loop_session_seq = 0
            # Keep the in-flight session object so live polls still attach,
            # but drop its past results if the user cleared while looping.
            if self._active_loop_session is not None:
                self._active_loop_session["results"] = []
                self._active_loop_session["poll_count"] = 0
                self._active_loop_session["error_count"] = 0
                self.loop_sessions.appendleft(self._active_loop_session)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            key_status = self._key_status_locked()
            started = self.loop_started_at
            elapsed = None
            if self.loop_running and started is not None:
                elapsed = max(0.0, time.time() - started)
            return {
                "loop_running": self.loop_running,
                "loop_stopping": self.loop_stopping,
                "loop_started_at": (
                    datetime.fromtimestamp(started).isoformat(timespec="seconds")
                    if started is not None
                    else None
                ),
                "loop_started_at_ms": (
                    int(started * 1000) if started is not None else None
                ),
                "loop_elapsed_seconds": elapsed,
                "loop_last_duration_seconds": self.loop_last_duration_seconds,
                "settings": asdict(self.settings),
                "account": self.account,
                "last_result": self.last_result,
                "last_ai_results": self.last_ai_results,
                "result_history": list(self.result_history),
                "loop_history": list(self.loop_sessions),
                "quote": self.quote,
                "last_position": self.last_position,
                "error": self.error,
                "ai_ready": {
                    "openai": key_status["openai"]["set"],
                    "gemini": key_status["gemini"]["set"],
                },
                "ai_key_status": key_status,
                "alpaca_key_status": self._alpaca_key_status_locked(),
                "trading_mode": self._trading_mode_status_locked(),
                "ai_presets": list_presets(),
                "sma_presets": list_sma_presets(),
                "dip_presets": list_dip_presets(),
                "pair_presets": list_pair_presets(),
                "ai_models": catalog_payload(),
            }

    def loop_state(self) -> dict[str, Any]:
        """Tiny loop-only view — polled while Stop is in flight.

        A full snapshot rebuilds presets, model catalogs and history, which is
        far too much payload for a sub-second poll.
        """
        with self.lock:
            started = self.loop_started_at
            elapsed = None
            if self.loop_running and started is not None:
                elapsed = max(0.0, time.time() - started)
            return {
                "loop_running": self.loop_running,
                "loop_stopping": self.loop_stopping,
                "loop_elapsed_seconds": elapsed,
                "loop_last_duration_seconds": self.loop_last_duration_seconds,
                "error": self.error,
            }

    def _key_status_locked(self) -> dict[str, Any]:
        try:
            env = Config.from_env()
            env_openai = env.openai_api_key
            env_gemini = env.gemini_api_key
        except Exception:
            env_openai = ""
            env_gemini = ""
        openai = self._openai_api_key or env_openai
        gemini = self._gemini_api_key or env_gemini
        # Prefer env when the key is present on disk / in process env.
        if env_openai:
            openai_source = "env"
        elif self._openai_api_key:
            openai_source = "ui"
        else:
            openai_source = "none"
        if env_gemini:
            gemini_source = "env"
        elif self._gemini_api_key:
            gemini_source = "ui"
        else:
            gemini_source = "none"
        self._openai_key_source = openai_source
        self._gemini_key_source = gemini_source
        self.ai_ready = {"openai": bool(openai), "gemini": bool(gemini)}
        return {
            "openai": {
                "set": bool(openai),
                "source": openai_source,
                "hint": mask_secret(openai) if openai else "",
            },
            "gemini": {
                "set": bool(gemini),
                "source": gemini_source,
                "hint": mask_secret(gemini) if gemini else "",
            },
        }

    def _alpaca_key_status_locked(self) -> dict[str, Any]:
        paper_slot = alpaca_slot_status(paper=True)
        live_slot = alpaca_slot_status(paper=False)
        paper = paper_mode_from_env()
        active = paper_slot if paper else live_slot
        return {
            "set": bool(active.get("set")),
            "api_key_set": bool(active.get("api_key_set")),
            "secret_set": bool(active.get("secret_set")),
            "api_key_hint": active.get("api_key_hint") or "",
            "secret_hint": active.get("secret_hint") or "",
            "paper": paper,
            "trading_mode": "paper" if paper else "live",
            "live_allowed": live_allowed_from_env(),
            "live_authorized": bool(self._live_session_authorized) and not paper,
            "paper_keys": paper_slot,
            "live_keys": live_slot,
        }

    def _trading_mode_status_locked(self) -> dict[str, Any]:
        paper = paper_mode_from_env()
        return {
            "mode": "paper" if paper else "live",
            "paper": paper,
            "live_allowed": live_allowed_from_env(),
            "live_authorized": bool(self._live_session_authorized) and not paper,
        }

    def apply_alpaca_keys(
        self,
        *,
        alpaca_api_key: str | None = None,
        alpaca_secret_key: str | None = None,
        paper: bool | None = None,
        environment: str | None = None,
        save_to_env: bool = True,
    ) -> dict[str, Any]:
        """Save Alpaca credentials for the paper or live slot.

        ``environment`` selects which key pair to write (``paper`` / ``live``).
        Empty strings keep existing values for that slot. Saving live keys does
        not switch the active mode — use ``set_trading_mode`` for that.
        """
        if environment is not None:
            env_name = str(environment).strip().lower()
            if env_name not in {"paper", "live"}:
                raise ValueError("environment must be 'paper' or 'live'")
            target_paper = env_name == "paper"
        elif paper is not None:
            target_paper = bool(paper)
        else:
            target_paper = paper_mode_from_env()

        with self.lock:
            current = (
                dict(self._alpaca_key_status_locked().get("paper_keys") or {})
                if target_paper
                else dict(self._alpaca_key_status_locked().get("live_keys") or {})
            )

        key_in = (alpaca_api_key or "").strip() if alpaca_api_key is not None else ""
        secret_in = (
            (alpaca_secret_key or "").strip() if alpaca_secret_key is not None else ""
        )

        will_have_key = bool(key_in) or bool(current.get("api_key_set"))
        will_have_secret = bool(secret_in) or bool(current.get("secret_set"))

        label = "paper" if target_paper else "live"
        if not key_in and not secret_in:
            if not current.get("set"):
                raise ValueError(f"Paste Alpaca {label} API key and secret to save.")
        elif not (will_have_key and will_have_secret):
            missing: list[str] = []
            if not will_have_key:
                missing.append("API key")
            if not will_have_secret:
                missing.append("secret key")
            raise ValueError(
                f"Paste both Alpaca {label} API key and secret to connect "
                f"({' and '.join(missing)} missing)."
            )

        updates: dict[str, str] = {}
        if target_paper:
            if key_in:
                updates["ALPACA_API_KEY"] = key_in
            if secret_in:
                updates["ALPACA_SECRET_KEY"] = secret_in
        else:
            if key_in:
                updates["ALPACA_LIVE_API_KEY"] = key_in
            if secret_in:
                updates["ALPACA_LIVE_SECRET_KEY"] = secret_in

        if not save_to_env:
            raise ValueError("Alpaca keys must be saved to .env to connect the desk.")
        if updates:
            upsert_env_values(updates)

        # Changing live credentials invalidates any prior Live session grant.
        if not target_paper and updates:
            with self.lock:
                self._live_session_authorized = False

        with self.lock:
            status = self._alpaca_key_status_locked()

        # Only verify the account when the saved slot matches the active mode.
        if target_paper == paper_mode_from_env():
            try:
                account = self.refresh_account()
                status = {
                    **status,
                    "account": {
                        "id": account.get("id"),
                        "status": account.get("status"),
                        "equity": account.get("equity"),
                        "paper": account.get("paper"),
                        "trading_mode": account.get("trading_mode"),
                    },
                }
            except Exception as exc:
                with self.lock:
                    self.account = None
                    self.error = None
                status = {**status, "account_error": str(exc)}
        return status

    def clear_alpaca_keys(self, *, environment: str | None = None) -> dict[str, Any]:
        """Remove Alpaca credentials for one slot or both."""
        env_name = (environment or "all").strip().lower()
        if env_name not in {"paper", "live", "all"}:
            raise ValueError("environment must be 'paper', 'live', or 'all'")
        keys: list[str] = []
        if env_name in {"paper", "all"}:
            keys.extend(
                [
                    "ALPACA_API_KEY",
                    "ALPACA_SECRET_KEY",
                    "ALPACA_PAPER_API_KEY",
                    "ALPACA_PAPER_SECRET_KEY",
                ]
            )
        if env_name in {"live", "all"}:
            keys.extend(["ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY"])
            with self.lock:
                self._live_session_authorized = False
        remove_env_keys(keys)
        # Wiping the live slot while Live is the active mode would leave the desk
        # pointed at an environment it can no longer authenticate — every later
        # Config.from_env() would raise. Fall back to paper and re-arm the
        # kill-switch, which is also the safe default after wiping everything.
        if env_name == "all" or (env_name == "live" and not paper_mode_from_env()):
            self._reset_runtime_for_mode_switch()
            upsert_env_values({"ALPACA_PAPER": "true", "ALPACA_ALLOW_LIVE": "false"})
        with self.lock:
            self.account = None
            self.error = None
            status = self._alpaca_key_status_locked()
        return status

    def set_trading_mode(
        self,
        mode: str,
        *,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        """Switch Paper/Live environment with safety resets.

        ``confirm`` is accepted for API compatibility and ignored.
        """
        target = str(mode or "").strip().lower()
        if target not in {"paper", "live"}:
            raise ValueError("mode must be 'paper' or 'live'")
        want_paper = target == "paper"
        current_paper = paper_mode_from_env()
        current_allow_live = live_allowed_from_env()

        if not want_paper:
            live_keys = alpaca_slot_status(paper=False)
            if not live_keys.get("set"):
                raise ValueError(
                    "Save distinct ALPACA_LIVE_API_KEY and ALPACA_LIVE_SECRET_KEY "
                    "before switching to Live."
                )

        updates: dict[str, str] = {
            "ALPACA_PAPER": "true" if want_paper else "false",
        }
        if not want_paper:
            updates["ALPACA_ALLOW_LIVE"] = "true"

        # Stop automation and clear mode-sensitive runtime *before* the env
        # flips — never leave a paper loop firing live orders, and let the
        # re-investment ledger settle into the file for the account it belongs
        # to rather than the one the desk is about to point at.
        self._reset_runtime_for_mode_switch()

        upsert_env_values(updates)

        with self.lock:
            self._live_session_authorized = not want_paper

        account: dict[str, Any] | None = None
        account_error: str | None = None
        try:
            account = self.refresh_account()
        except Exception as exc:
            account_error = str(exc)
            with self.lock:
                self.account = None
                self.error = None
                self._live_session_authorized = False
            if want_paper:
                # A failed *paper* check is never a reason to keep the desk on
                # Live — retreating to the safe environment is the whole point
                # of the switch, so it stands even when the keys are bad.
                raise ValueError(
                    "Paper account check failed — the desk is on Paper with "
                    f"unverified keys. {account_error}"
                ) from exc
            # A Live switch that cannot reach the account rolls all the way
            # back, kill-switch included, so a half-applied Live never lingers.
            revert = {"ALPACA_PAPER": "true" if current_paper else "false"}
            if not current_allow_live:
                revert["ALPACA_ALLOW_LIVE"] = "false"
            upsert_env_values(revert)
            raise ValueError(
                f"Live account check failed — mode unchanged. {account_error}"
            ) from exc

        with self.lock:
            status = self._alpaca_key_status_locked()
            mode_status = self._trading_mode_status_locked()
        status = {
            **status,
            "account": {
                "id": account.get("id"),
                "status": account.get("status"),
                "equity": account.get("equity"),
                "paper": account.get("paper"),
                "trading_mode": account.get("trading_mode"),
            },
        }
        return {
            "ok": True,
            "trading_mode": mode_status,
            "alpaca_key_status": status,
            "settings": asdict(self.settings),
        }

    def _reset_runtime_for_mode_switch(self) -> None:
        """Stop loops/reinvest watchers and clear caches when the account changes."""
        try:
            self.stop_loop()
        except Exception:
            pass
        # Wait briefly for the worker to notice the stop flag.
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

        # An armed buy-back is a real order waiting on a fill. Left running it
        # would settle against whichever account the desk now points at, so the
        # watcher is stopped and every waiting plan is closed out first.
        self._reinvest_stop.set()
        reinvest_thread = self._reinvest_thread
        if reinvest_thread is not None and reinvest_thread.is_alive():
            reinvest_thread.join(timeout=2.0)

        self._followon_stop.set()
        followon_thread = self._followon_thread
        if followon_thread is not None and followon_thread.is_alive():
            followon_thread.join(timeout=2.0)

        self._dip_hunt_stop.set()
        dip_thread = self._dip_hunt_thread
        if dip_thread is not None and dip_thread.is_alive():
            dip_thread.join(timeout=2.0)

        # A dip buy may already be resting at the broker. Cancel it while the
        # environment still points at the old account; after the switch there
        # is no safe way to reach that order. A failed cancellation is reported
        # as interrupted, never falsely presented as cancelled.
        with self.lock:
            parked_hunts = [
                dict(plan)
                for plan in self.dip_hunt_plans.values()
                if str(plan.get("status") or "") == "awaiting_fill"
            ]
            parked_buybacks = [
                dict(plan)
                for plan in self.reinvest_plans.values()
                if str(plan.get("status") or "") == "awaiting_fill"
            ]
        dip_cancel_errors: dict[str, str] = {}
        dip_service: AlpacaService | None = None
        if parked_hunts:
            try:
                dip_service = AlpacaService(self._base_config())
            except Exception as exc:
                for plan in parked_hunts:
                    dip_cancel_errors[str(plan.get("id") or "")] = str(exc)
        if dip_service is not None:
            for plan in parked_hunts:
                try:
                    self._cancel_parked_dip_buy(plan, service=dip_service)
                except Exception as exc:
                    dip_cancel_errors[str(plan.get("id") or "")] = str(exc)

        buyback_cancel_errors: dict[str, str] = {}
        buyback_service: AlpacaService | None = None
        if parked_buybacks:
            try:
                buyback_service = dip_service or AlpacaService(self._base_config())
            except Exception as exc:
                for plan in parked_buybacks:
                    buyback_cancel_errors[str(plan.get("id") or "")] = str(exc)
        if buyback_service is not None:
            for plan in parked_buybacks:
                try:
                    self._cancel_resting_reinvest_buy(plan, service=buyback_service)
                except Exception as exc:
                    buyback_cancel_errors[str(plan.get("id") or "")] = str(exc)

        with self.lock:
            for plan in list(self.reinvest_plans.values()):
                status = str(plan.get("status") or "")
                cancel_error = buyback_cancel_errors.get(str(plan.get("id") or ""))
                if cancel_error:
                    plan["status"] = "interrupted"
                    plan["message"] = (
                        "Trading environment changed, but the buy-back "
                        f"could not be cancelled — check the old account: {cancel_error}"
                    )
                    plan["settled_at_iso"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                elif status in {"waiting", "awaiting_fill"}:
                    plan["status"] = "cancelled"
                    plan["message"] = "Cancelled because the trading environment changed."
                    plan["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
                elif status == "placing":
                    plan["status"] = "interrupted"
                    plan["message"] = (
                        "Trading environment changed while the buy-back was being "
                        "sent — check the old account."
                    )
                    plan["settled_at_iso"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
            for plan in list(self.followon_plans.values()):
                if str(plan.get("status") or "") == "waiting":
                    plan["status"] = "cancelled"
                    plan["message"] = "Cancelled because the trading environment changed."
                    plan["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
            for plan_id, plan in list(self.dip_hunt_plans.items()):
                status = str(plan.get("status") or "")
                cancel_error = dip_cancel_errors.get(plan_id)
                if cancel_error:
                    plan["status"] = "interrupted"
                    plan["message"] = (
                        "Trading environment changed, but the parked dip buy "
                        f"could not be cancelled — check the old account: {cancel_error}"
                    )
                    plan["settled_at_iso"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                elif status in _DIP_HUNT_CANCELLABLE:
                    plan["status"] = "cancelled"
                    plan["message"] = "Cancelled because the trading environment changed."
                    plan["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
                elif status == "placing":
                    plan["status"] = "interrupted"
                    plan["message"] = (
                        "Trading environment changed while the dip buy was being "
                        "sent — check the old account."
                    )
                    plan["settled_at_iso"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
            self._manual_asset_cache.clear()
            self._manual_earnings_cache.clear()
            self._manual_heat_cache = None
            self._last_manual_ticket = None
            self.account = None
            self.quote = None
            self._quote_fetched_at = 0.0
            self._quote_symbol = None
            self._manual_atr_cache.clear()
            self.last_result = None
            self.last_ai_results = None
            self.error = None
        # Runs while the env still names the *old* account, so the cancelled
        # plans land in that account's ledger — see `set_trading_mode`. They
        # stay in memory too: a buy-back the desk disarmed on the user's behalf
        # is something they need to see, not something that should vanish.
        self._persist_reinvest_plans()
        self._persist_followon_plans()
        self._persist_dip_hunt_plans()

    def _require_live_execution(self) -> None:
        """Block real Live orders unless ALPACA_ALLOW_LIVE is set."""
        if paper_mode_from_env():
            return
        if not live_allowed_from_env():
            raise ValueError(
                "Live trading is blocked. Set ALPACA_ALLOW_LIVE=true on Configuration."
            )

    def authorize_live_session(self, confirm: str | None = None) -> dict[str, Any]:
        """Mark Live as authorized for this process (no typed phrase required).

        ``confirm`` is accepted for API compatibility and ignored.
        """
        if paper_mode_from_env():
            raise ValueError("Desk is in Paper mode — switch to Live first.")
        if not live_allowed_from_env():
            upsert_env_values({"ALPACA_ALLOW_LIVE": "true"})
        live_keys = alpaca_slot_status(paper=False)
        if not live_keys.get("set"):
            raise ValueError("Save live API credentials before authorizing Live.")
        account = self.refresh_account()
        with self.lock:
            self._live_session_authorized = True
            mode_status = self._trading_mode_status_locked()
        return {
            "ok": True,
            "trading_mode": mode_status,
            "account": {
                "id": account.get("id"),
                "status": account.get("status"),
                "equity": account.get("equity"),
                "paper": account.get("paper"),
                "trading_mode": account.get("trading_mode"),
            },
        }

    def apply_api_keys(
        self,
        *,
        openai_api_key: str | None = None,
        gemini_api_key: str | None = None,
        save_to_env: bool = False,
    ) -> dict[str, Any]:
        """Update session keys. Empty strings are ignored (keep existing).

        When save_to_env is True, keys are written to `.env` and kept in memory
        as a backup so status stays correct after reload quirks.
        """
        updates: dict[str, str] = {}
        with self.lock:
            if openai_api_key is not None:
                cleaned = openai_api_key.strip()
                if cleaned:
                    self._openai_api_key = cleaned
                    updates["OPENAI_API_KEY"] = cleaned
            if gemini_api_key is not None:
                cleaned = gemini_api_key.strip()
                if cleaned:
                    self._gemini_api_key = cleaned
                    updates["GEMINI_API_KEY"] = cleaned
            status = self._key_status_locked()

        if save_to_env and updates:
            upsert_env_values(updates)
            with self.lock:
                # Keep memory copies; prefer reporting source=env when .env has them.
                status = self._key_status_locked()
        return status

    def clear_api_keys(
        self,
        *,
        openai: bool = False,
        gemini: bool = False,
        clear_env: bool = True,
    ) -> dict[str, Any]:
        """Clear session AI keys and optionally remove them from `.env`."""
        if not openai and not gemini:
            raise ValueError("Choose OpenAI and/or Gemini to clear.")
        to_remove: list[str] = []
        with self.lock:
            if openai:
                self._openai_api_key = None
                to_remove.append("OPENAI_API_KEY")
            if gemini:
                self._gemini_api_key = None
                to_remove.append("GEMINI_API_KEY")
        if clear_env and to_remove:
            remove_env_keys(to_remove)
        with self.lock:
            return self._key_status_locked()

    def sync_key_sources_from_env(self) -> None:
        """Refresh ready flags after process start / .env edits."""
        with self.lock:
            try:
                env = Config.from_env()
            except Exception:
                return
            # Hydrate memory from .env so UI status survives process logic gaps.
            if env.openai_api_key and not self._openai_api_key:
                self._openai_api_key = env.openai_api_key
            if env.gemini_api_key and not self._gemini_api_key:
                self._gemini_api_key = env.gemini_api_key
            self._key_status_locked()

    def _persist_settings_locked(self) -> None:
        payload = asdict(self.settings)
        save_settings(payload)
        # Mirror strategy flags into .env so CLI and web stay aligned.
        upsert_env_values(
            {
                "SYMBOL": payload["symbol"],
                "SYMBOLS": payload["symbols"],
                "FAST_SMA": str(payload["fast_sma"]),
                "SLOW_SMA": str(payload["slow_sma"]),
                "SMA_PRESET": payload["sma_preset"],
                "DIP_PRESET": payload["dip_preset"],
                "DIP_RSI_BUY": str(payload["dip_rsi_buy"]),
                "DIP_RSI_SELL": str(payload["dip_rsi_sell"]),
                "DIP_SKIP_BEARISH": "true" if payload["dip_skip_bearish"] else "false",
                "TRADE_QTY": str(payload["trade_qty"]),
                "SIZE_MODE": str(payload.get("size_mode") or "qty"),
                "TRADE_NOTIONAL": str(payload.get("trade_notional") or 100),
                "BAR_TIMEFRAME": payload["bar_timeframe"],
                "POLL_SECONDS": str(payload["poll_seconds"]),
                "STRATEGY_MODE": payload["strategy_mode"],
                "PAIR_PRESET": payload.get("pair_preset", "research_max"),
                "PAIR_SMA_PERIOD": str(payload.get("pair_sma_period", 50)),
                "PAIR_LOOKBACK": str(payload.get("pair_lookback", 7)),
                "PAIR_IMPULSE_PCT": str(payload.get("pair_impulse_pct", 5)),
                "PAIR_WEAK_SIDE": payload.get("pair_weak_side", "LONG"),
                "PAIR_LONG_SYMBOL": payload.get("pair_long_symbol", "") or "",
                "PAIR_SHORT_SYMBOL": payload.get("pair_short_symbol", "") or "",
                "LS_EMA_FAST": str(payload.get("ls_ema_fast", 21)),
                "LS_EMA_SLOW": str(payload.get("ls_ema_slow", 55)),
                "LS_ADX_MIN": str(payload.get("ls_adx_min", 20)),
                "LS_ATR_STOP_MULT": str(payload.get("ls_atr_stop_mult", 1.5)),
                "LS_RISK_PCT": str(payload.get("ls_risk_pct", 1.0)),
                "LS_RR": str(payload.get("ls_rr", 2.0)),
                "LS_TIME_STOP_BARS": str(payload.get("ls_time_stop_bars", 15)),
                "AI_PROVIDER": payload["ai_provider"],
                "AI_PRESET": payload["ai_preset"],
                "AI_MIN_CONFIDENCE": str(payload["ai_min_confidence"]),
                "AI_INSTRUCTIONS": payload["ai_instructions"],
                "OPENAI_MODEL": payload["openai_model"],
                "GEMINI_MODEL": payload["gemini_model"],
                "STOP_LOSS_PCT": str(payload["stop_loss_pct"]),
                "STOP_LIMIT_OFFSET_PCT": str(payload.get("stop_limit_offset_pct", 0.0)),
                "AI_RISK_PCT": str(payload.get("ai_risk_pct", 0.5)),
                "AI_ATR_STOP_MULT": str(payload.get("ai_atr_stop_mult", 1.8)),
                "AI_TAKE_PROFIT_R": str(payload.get("ai_take_profit_r", 2.0)),
                "AI_TRAIL_AFTER_R": str(payload.get("ai_trail_after_r", 1.0)),
                "AI_MAX_POSITIONS": str(payload.get("ai_max_positions", 3)),
                "AI_DAILY_LOSS_LIMIT_PCT": str(
                    payload.get("ai_daily_loss_limit_pct", 3.0)
                ),
                "AI_MIN_HOLD_MINUTES": str(payload.get("ai_min_hold_minutes", 15)),
                "AI_COOLDOWN_MINUTES": str(payload.get("ai_cooldown_minutes", 60)),
                "AI_MAX_SPREAD_BPS": str(payload.get("ai_max_spread_bps", 25.0)),
                # Not "LANG" — that is a standard POSIX shell variable.
                "LANG_CODE": str(payload.get("lang") or DEFAULT_LANG),
            }
        )

    def update_settings(self, data: dict[str, Any]) -> RunSettings:
        with self.lock:
            timeframe = str(
                data.get("bar_timeframe", self.settings.bar_timeframe)
            ).strip()
            if timeframe not in ALLOWED_TIMEFRAMES:
                raise ValueError(
                    f"Bar timeframe must be one of: {', '.join(ALLOWED_TIMEFRAMES)}"
                )
            mode = str(data.get("strategy_mode", self.settings.strategy_mode)).lower()
            if mode not in {"sma", "dip", "ai", "pair", "ls"}:
                raise ValueError("strategy_mode must be sma, dip, ai, pair, or ls")
            provider = str(data.get("ai_provider", self.settings.ai_provider)).lower()
            if provider not in {"openai", "gemini"}:
                raise ValueError("ai_provider must be openai or gemini")

            pair_preset = resolve_pair_preset_id(
                str(data.get("pair_preset", self.settings.pair_preset))
            )
            pair_def = get_pair_preset(pair_preset)
            if pair_preset != "custom" and "pair_preset" in data:
                pair_sma = pair_def.sma_period
                pair_lb = pair_def.lookback
                pair_imp = pair_def.impulse_pct
                pair_weak = pair_def.weak_side
            else:
                pair_sma = int(
                    data.get("pair_sma_period", self.settings.pair_sma_period)
                )
                pair_lb = int(data.get("pair_lookback", self.settings.pair_lookback))
                pair_imp = float(
                    data.get("pair_impulse_pct", self.settings.pair_impulse_pct)
                )
                pair_weak = str(
                    data.get("pair_weak_side", self.settings.pair_weak_side)
                ).strip().upper() or "LONG"
                pair_preset = match_pair_preset_id(
                    pair_sma,
                    pair_lb,
                    pair_imp,
                    pair_weak,
                )
            pair_weak = normalize_weak_side(pair_weak)

            ls_ema_fast = int(data.get("ls_ema_fast", self.settings.ls_ema_fast))
            ls_ema_slow = int(data.get("ls_ema_slow", self.settings.ls_ema_slow))
            ls_adx_min = float(data.get("ls_adx_min", self.settings.ls_adx_min))
            ls_atr_stop_mult = float(
                data.get("ls_atr_stop_mult", self.settings.ls_atr_stop_mult)
            )
            ls_risk_pct = float(data.get("ls_risk_pct", self.settings.ls_risk_pct))
            ls_rr = float(data.get("ls_rr", self.settings.ls_rr))
            ls_time_stop_bars = int(
                data.get("ls_time_stop_bars", self.settings.ls_time_stop_bars)
            )
            if ls_ema_fast >= ls_ema_slow:
                raise ValueError("ls_ema_fast must be < ls_ema_slow")

            symbols_raw = str(
                data.get("symbols", data.get("symbol", self.settings.symbols))
            ).strip()
            symbol = str(data.get("symbol", self.settings.symbol)).upper().strip()
            if mode != "pair" and not symbols_raw:
                symbols_raw = symbol

            # Explicit pair legs from payload (optional); else derive from symbols.
            pair_long = str(
                data.get("pair_long_symbol", self.settings.pair_long_symbol) or ""
            ).strip().upper()
            pair_short = str(
                data.get("pair_short_symbol", self.settings.pair_short_symbol) or ""
            ).strip().upper()
            if mode == "pair":
                timeframe = "1Day"
                pair_long, pair_short = parse_pair_symbols(
                    symbols_raw or f"{pair_long},{pair_short}",
                    long_symbol=pair_long,
                    short_symbol=pair_short,
                )
                symbol = pair_long
                symbols_raw = f"{pair_long},{pair_short}"
            if mode == "ls":
                timeframe = "1Day"
            preset = resolve_preset_id(
                str(data.get("ai_preset", self.settings.ai_preset))
            )
            preset_def = get_preset(preset)
            raw_instructions = str(
                data.get("ai_instructions", self.settings.ai_instructions)
            )
            # Selecting a named preset without instructions → fill playbook.
            if "ai_preset" in data and preset != "custom" and not raw_instructions.strip():
                raw_instructions = preset_def.instructions
            instructions = instructions_for(preset, raw_instructions)

            # SMA preset + windows. UI applies windows when a preset is chosen;
            # editing windows flips the select to custom before save.
            sma_preset = resolve_sma_preset_id(
                str(data.get("sma_preset", self.settings.sma_preset))
            )
            sma_def = get_sma_preset(sma_preset)
            if "fast_sma" in data or "slow_sma" in data:
                fast = int(data.get("fast_sma", self.settings.fast_sma))
                slow = int(data.get("slow_sma", self.settings.slow_sma))
            elif sma_preset != "custom":
                fast, slow = sma_def.fast_sma, sma_def.slow_sma
            else:
                fast, slow = self.settings.fast_sma, self.settings.slow_sma
            if sma_preset != "custom" and match_preset_id(fast, slow) != sma_preset:
                # Named preset without matching windows → fill from the preset.
                fast, slow = sma_def.fast_sma, sma_def.slow_sma
            elif sma_preset == "custom":
                pass
            elif "sma_preset" not in data:
                sma_preset = match_preset_id(fast, slow)

            # Dip preset + RSI thresholds.
            dip_preset = resolve_dip_preset_id(
                str(data.get("dip_preset", self.settings.dip_preset))
            )
            dip_def = get_dip_preset(dip_preset)
            if "dip_rsi_buy" in data or "dip_rsi_sell" in data:
                rsi_buy = float(data.get("dip_rsi_buy", self.settings.dip_rsi_buy))
                rsi_sell = float(data.get("dip_rsi_sell", self.settings.dip_rsi_sell))
            elif dip_preset != "custom":
                rsi_buy, rsi_sell = dip_def.rsi_buy, dip_def.rsi_sell
            else:
                rsi_buy, rsi_sell = self.settings.dip_rsi_buy, self.settings.dip_rsi_sell
            if "dip_skip_bearish" in data:
                skip_bearish = bool(data.get("dip_skip_bearish"))
            elif dip_preset != "custom":
                skip_bearish = dip_def.skip_bearish
            else:
                skip_bearish = self.settings.dip_skip_bearish
            if dip_preset != "custom" and match_dip_preset_id(
                rsi_buy, rsi_sell, skip_bearish
            ) != dip_preset:
                rsi_buy, rsi_sell = dip_def.rsi_buy, dip_def.rsi_sell
                skip_bearish = dip_def.skip_bearish
            elif dip_preset == "custom":
                pass
            elif "dip_preset" not in data:
                dip_preset = match_dip_preset_id(rsi_buy, rsi_sell, skip_bearish)
            if not (0 < rsi_buy < rsi_sell < 100):
                raise ValueError("Dip RSI buy must be less than RSI sell (0–100)")

            # Optional fields: omit / null keeps the current value (avoids UI
            # clients that omit model/stop wiping .env-configured values).
            if data.get("ai_min_confidence") is None:
                conf = self.settings.ai_min_confidence
                # If preset changed and confidence was omitted, use the preset default.
                if preset != self.settings.ai_preset and preset != "custom":
                    conf = preset_def.min_confidence
            else:
                conf = max(0.0, min(1.0, float(data["ai_min_confidence"])))

            if data.get("stop_loss_pct") is None:
                stop_pct = self.settings.stop_loss_pct
            else:
                stop_pct = max(0.0, min(50.0, float(data["stop_loss_pct"])))

            if data.get("stop_limit_offset_pct") is None:
                stop_limit_offset = float(self.settings.stop_limit_offset_pct or 0)
            else:
                stop_limit_offset = max(
                    0.0, min(50.0, float(data["stop_limit_offset_pct"]))
                )

            # AI risk knobs. Omitted keeps the current value, except right after a
            # preset switch — then the preset's own geometry wins, so a trend book
            # does not inherit a mean-reversion target.
            preset_risk = (
                risk_profile_for(preset)
                if (preset != self.settings.ai_preset and preset != "custom")
                else {}
            )

            def _risk(key: str, lo: float, hi: float) -> float:
                if data.get(key) is not None:
                    return max(lo, min(hi, float(data[key])))
                if key in preset_risk:
                    return max(lo, min(hi, float(preset_risk[key])))
                return float(getattr(self.settings, key))

            ai_risk_pct = _risk("ai_risk_pct", 0.0, 10.0)
            ai_atr_stop_mult = _risk("ai_atr_stop_mult", 0.0, 10.0)
            ai_take_profit_r = _risk("ai_take_profit_r", 0.0, 20.0)
            ai_trail_after_r = _risk("ai_trail_after_r", 0.0, 20.0)
            ai_max_positions = int(_risk("ai_max_positions", 0, 50))
            ai_daily_loss_limit_pct = _risk("ai_daily_loss_limit_pct", 0.0, 100.0)
            ai_min_hold_minutes = int(_risk("ai_min_hold_minutes", 0, 1440))
            ai_cooldown_minutes = int(_risk("ai_cooldown_minutes", 0, 1440))
            ai_max_spread_bps = _risk("ai_max_spread_bps", 0.0, 1000.0)

            # Omit / null keeps the current language (same rule as the AI knobs).
            lang = normalize_lang(
                self.settings.lang if data.get("lang") is None else data["lang"]
            )

            size_mode = resolve_size_mode(
                data.get("size_mode", self.settings.size_mode), mode
            )
            trade_qty = float(data.get("trade_qty", self.settings.trade_qty))
            trade_notional = float(
                data.get("trade_notional", self.settings.trade_notional)
            )
            if size_mode == "qty" and trade_qty <= 0:
                raise ValueError("Shares / qty must be greater than 0")
            if size_mode == "notional" and trade_notional <= 0:
                raise ValueError("Dollar amount must be greater than 0")

            openai_model = str(
                data.get("openai_model") or self.settings.openai_model
            ).strip() or self.settings.openai_model
            gemini_model = str(
                data.get("gemini_model") or self.settings.gemini_model
            ).strip() or self.settings.gemini_model

            self.settings = RunSettings(
                symbol=symbol,
                symbols=symbols_raw.upper(),
                fast_sma=fast,
                slow_sma=slow,
                sma_preset=sma_preset,
                dip_preset=dip_preset,
                dip_rsi_buy=rsi_buy,
                dip_rsi_sell=rsi_sell,
                dip_skip_bearish=skip_bearish,
                trade_qty=trade_qty,
                size_mode=size_mode,
                trade_notional=trade_notional,
                bar_timeframe=timeframe,
                poll_seconds=max(
                    10, int(data.get("poll_seconds", self.settings.poll_seconds))
                ),
                strategy_mode=mode,
                pair_preset=pair_preset,
                pair_sma_period=pair_sma,
                pair_lookback=pair_lb,
                pair_impulse_pct=pair_imp,
                pair_weak_side=pair_weak,
                pair_long_symbol=pair_long,
                pair_short_symbol=pair_short,
                ls_ema_fast=ls_ema_fast,
                ls_ema_slow=ls_ema_slow,
                ls_adx_min=ls_adx_min,
                ls_atr_stop_mult=ls_atr_stop_mult,
                ls_risk_pct=ls_risk_pct,
                ls_rr=ls_rr,
                ls_time_stop_bars=ls_time_stop_bars,
                ai_provider=provider,
                ai_preset=preset,
                ai_instructions=instructions,
                ai_min_confidence=conf,
                openai_model=openai_model,
                gemini_model=gemini_model,
                stop_loss_pct=stop_pct,
                ai_risk_pct=ai_risk_pct,
                ai_atr_stop_mult=ai_atr_stop_mult,
                ai_take_profit_r=ai_take_profit_r,
                ai_trail_after_r=ai_trail_after_r,
                ai_max_positions=ai_max_positions,
                ai_daily_loss_limit_pct=ai_daily_loss_limit_pct,
                ai_min_hold_minutes=ai_min_hold_minutes,
                ai_cooldown_minutes=ai_cooldown_minutes,
                ai_max_spread_bps=ai_max_spread_bps,
                stop_limit_offset_pct=stop_limit_offset,
                lang=lang,
            )
            if self.settings.fast_sma >= self.settings.slow_sma:
                raise ValueError("Fast SMA must be smaller than Slow SMA")
            if (
                self.quote is not None
                and self._quote_symbol is not None
                and self._quote_symbol != self.settings.symbol
            ):
                self.quote = None
                self._quote_fetched_at = 0.0
                self._quote_symbol = None
            self._persist_settings_locked()

        # Keys can arrive with settings payloads from the web form.
        self.apply_api_keys(
            openai_api_key=data.get("openai_api_key"),
            gemini_api_key=data.get("gemini_api_key"),
            save_to_env=bool(data.get("save_keys_to_env", False)),
        )
        with self.lock:
            return self.settings

    def bootstrap_settings(self) -> RunSettings:
        """Load last UI settings (JSON), falling back to .env defaults."""
        persisted = load_settings()
        if not SETTINGS_PATH.exists():
            try:
                env = Config.from_env()
                persisted = {
                    "symbol": env.symbol,
                    "symbols": ",".join(env.symbols),
                    "fast_sma": env.fast_sma,
                    "slow_sma": env.slow_sma,
                    "sma_preset": env.sma_preset,
                    "dip_preset": env.dip_preset,
                    "dip_rsi_buy": env.dip_rsi_buy,
                    "dip_rsi_sell": env.dip_rsi_sell,
                    "dip_skip_bearish": env.dip_skip_bearish,
                    "trade_qty": env.trade_qty,
                    "size_mode": env.size_mode,
                    "trade_notional": env.trade_notional,
                    "bar_timeframe": env.bar_timeframe,
                    "poll_seconds": env.poll_seconds,
                    "strategy_mode": env.strategy_mode,
                    "pair_preset": env.pair_preset,
                    "pair_sma_period": env.pair_sma_period,
                    "pair_lookback": env.pair_lookback,
                    "pair_impulse_pct": env.pair_impulse_pct,
                    "pair_weak_side": env.pair_weak_side,
                    "pair_long_symbol": env.pair_long_symbol,
                    "pair_short_symbol": env.pair_short_symbol,
                    "ls_ema_fast": env.ls_ema_fast,
                    "ls_ema_slow": env.ls_ema_slow,
                    "ls_adx_min": env.ls_adx_min,
                    "ls_atr_stop_mult": env.ls_atr_stop_mult,
                    "ls_risk_pct": env.ls_risk_pct,
                    "ls_rr": env.ls_rr,
                    "ls_time_stop_bars": env.ls_time_stop_bars,
                    "ai_provider": env.ai_provider,
                    "ai_preset": env.ai_preset,
                    "ai_instructions": env.ai_instructions,
                    "ai_min_confidence": env.ai_min_confidence,
                    "openai_model": env.openai_model,
                    "gemini_model": env.gemini_model,
                    "stop_loss_pct": env.stop_loss_pct,
                    "ai_risk_pct": env.ai_risk_pct,
                    "ai_atr_stop_mult": env.ai_atr_stop_mult,
                    "ai_take_profit_r": env.ai_take_profit_r,
                    "ai_trail_after_r": env.ai_trail_after_r,
                    "ai_max_positions": env.ai_max_positions,
                    "ai_daily_loss_limit_pct": env.ai_daily_loss_limit_pct,
                    "ai_min_hold_minutes": env.ai_min_hold_minutes,
                    "ai_cooldown_minutes": env.ai_cooldown_minutes,
                    "ai_max_spread_bps": env.ai_max_spread_bps,
                    "stop_limit_offset_pct": env.stop_limit_offset_pct,
                    "lang": env.lang,
                }
            except Exception:
                pass
        try:
            return self.update_settings(persisted)
        except ValueError:
            # Older desk files could store pair mode without two legs.
            if str(persisted.get("strategy_mode", "")).lower() == "pair":
                persisted = {**persisted, "strategy_mode": "sma"}
                return self.update_settings(persisted)
            raise

    def _base_config(self) -> Config:
        base = Config.from_env()
        # Live is allowed when ALPACA_ALLOW_LIVE is set and mode is live; real
        # orders still need session authorization via _require_live_execution.
        with self.lock:
            s = self.settings
            openai_key = self._openai_api_key or base.openai_api_key
            gemini_key = self._gemini_api_key or base.gemini_api_key
            self.ai_ready = {
                "openai": bool(openai_key),
                "gemini": bool(gemini_key),
            }
        return base.override(
            symbol=s.symbol,
            symbols=s.symbols,
            fast_sma=s.fast_sma,
            slow_sma=s.slow_sma,
            sma_preset=s.sma_preset,
            dip_preset=s.dip_preset,
            dip_rsi_buy=s.dip_rsi_buy,
            dip_rsi_sell=s.dip_rsi_sell,
            dip_skip_bearish=s.dip_skip_bearish,
            trade_qty=s.trade_qty,
            size_mode=s.size_mode,
            trade_notional=s.trade_notional,
            bar_timeframe=s.bar_timeframe,
            poll_seconds=s.poll_seconds,
            strategy_mode=s.strategy_mode,
            pair_preset=s.pair_preset,
            pair_sma_period=s.pair_sma_period,
            pair_lookback=s.pair_lookback,
            pair_impulse_pct=s.pair_impulse_pct,
            pair_weak_side=s.pair_weak_side,
            pair_long_symbol=s.pair_long_symbol,
            pair_short_symbol=s.pair_short_symbol,
            ls_ema_fast=s.ls_ema_fast,
            ls_ema_slow=s.ls_ema_slow,
            ls_adx_min=s.ls_adx_min,
            ls_atr_stop_mult=s.ls_atr_stop_mult,
            ls_risk_pct=s.ls_risk_pct,
            ls_rr=s.ls_rr,
            ls_time_stop_bars=s.ls_time_stop_bars,
            ai_provider=s.ai_provider,
            ai_preset=s.ai_preset,
            ai_instructions=s.ai_instructions,
            ai_min_confidence=s.ai_min_confidence,
            stop_loss_pct=s.stop_loss_pct,
            ai_risk_pct=s.ai_risk_pct,
            ai_atr_stop_mult=s.ai_atr_stop_mult,
            ai_take_profit_r=s.ai_take_profit_r,
            ai_trail_after_r=s.ai_trail_after_r,
            ai_max_positions=s.ai_max_positions,
            ai_daily_loss_limit_pct=s.ai_daily_loss_limit_pct,
            ai_min_hold_minutes=s.ai_min_hold_minutes,
            ai_cooldown_minutes=s.ai_cooldown_minutes,
            ai_max_spread_bps=s.ai_max_spread_bps,
            stop_limit_offset_pct=s.stop_limit_offset_pct,
            openai_model=s.openai_model,
            gemini_model=s.gemini_model,
            openai_api_key=openai_key,
            gemini_api_key=gemini_key,
            lang=s.lang,
        )

    def _build_algo_bot(self) -> TradingBot:
        """SMA or buy-the-dip bot (TradingBot branches on strategy_mode)."""
        return TradingBot(self._base_config())

    def _build_pair_bot(self) -> PairTradingBot:
        return PairTradingBot(self._base_config())

    def _build_ls_bot(self) -> LsTradingBot:
        return LsTradingBot(self._base_config())

    def _build_ai_bot(self) -> AiTradingBot:
        config = self._base_config()
        if config.ai_provider == "openai" and not config.openai_api_key:
            raise ValueError("OpenAI API key missing — paste it on Configuration")
        if config.ai_provider == "gemini" and not config.gemini_api_key:
            raise ValueError("Gemini API key missing — paste it on Configuration")
        return AiTradingBot(config)

    def _trim_backtest_bars(
        self, bars: pd.DataFrame, *, days_i: int, end: datetime
    ) -> pd.DataFrame:
        """Trim to the requested evaluation window but keep warmup rows."""
        cutoff = end - timedelta(days=days_i)
        if bars.index.tz is None:
            cutoff = cutoff.replace(tzinfo=None)
        else:
            cutoff = cutoff.astimezone(bars.index.tz)
        pre = bars[bars.index < cutoff]
        window = bars[bars.index >= cutoff]
        if window.empty:
            keep = min(len(bars), max(days_i, 40))
            return bars.tail(keep + 80)
        warmup_keep = pre.tail(150)
        return pd.concat([warmup_keep, window]) if not warmup_keep.empty else window

    def _run_pair_backtest(
        self,
        *,
        days: int,
        bar_timeframe: str,
        initial_cash: float,
        pair_preset: str | None,
        pair_sma_period: int | None,
        pair_lookback: int | None,
        pair_impulse_pct: float | None,
        pair_weak_side: str | None,
        pair_long_symbol: str | None,
        pair_short_symbol: str | None,
        slip_bps: float | None,
        symbols: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        days_i = int(days)
        if days_i < 30 or days_i > 1500:
            raise ValueError("days must be between 30 and 1500")
        tf = str(bar_timeframe or "1Day").strip()
        if tf != "1Day":
            raise ValueError("Pair backtest currently supports 1Day bars only")
        cash = float(initial_cash)
        if cash < 100:
            raise ValueError("initial_cash must be at least 100")

        with self.lock:
            s = self.settings
            desk_preset = s.pair_preset
            desk_sma = s.pair_sma_period
            desk_lb = s.pair_lookback
            desk_imp = s.pair_impulse_pct
            desk_weak = s.pair_weak_side
            desk_long = s.pair_long_symbol
            desk_short = s.pair_short_symbol
            desk_symbols = s.symbols

        preset_id = resolve_pair_preset_id(pair_preset or desk_preset)
        preset = get_pair_preset(preset_id)
        if preset_id == "custom":
            sma_p = int(pair_sma_period if pair_sma_period is not None else desk_sma)
            look = int(pair_lookback if pair_lookback is not None else desk_lb)
            impulse = float(
                pair_impulse_pct if pair_impulse_pct is not None else desk_imp
            )
            weak = normalize_weak_side(
                pair_weak_side if pair_weak_side is not None else desk_weak
            )
        else:
            sma_p, look = preset.sma_period, preset.lookback
            impulse = preset.impulse_pct
            weak = normalize_weak_side(
                pair_weak_side if pair_weak_side is not None else preset.weak_side
            )

        long_hint = str(
            pair_long_symbol if pair_long_symbol is not None else desk_long or ""
        ).strip().upper()
        short_hint = str(
            pair_short_symbol if pair_short_symbol is not None else desk_short or ""
        ).strip().upper()
        symbols_raw = str(symbols or symbol or desk_symbols or "").strip()
        long_s, short_s = parse_pair_symbols(
            symbols_raw,
            long_symbol=long_hint,
            short_symbol=short_hint,
        )

        strategy = build_pair_strategy(
            sma_period=sma_p,
            lookback=look,
            impulse_pct=impulse,
            weak_side=weak,
            long_symbol=long_s,
            short_symbol=short_s,
        )
        params = {
            "pair_preset": (
                preset_id
                if preset_id != "custom"
                else match_pair_preset_id(sma_p, look, impulse, weak)
            ),
            "pair_sma_period": sma_p,
            "pair_lookback": look,
            "pair_impulse_pct": impulse,
            "pair_weak_side": weak,
            "pair_long_symbol": long_s,
            "pair_short_symbol": short_s,
            "label": (
                preset.label
                if preset_id != "custom"
                else f"Custom SMA{sma_p}/{look}d/{impulse:g}%"
            ),
            "slip_bps": float(slip_bps if slip_bps is not None else 5.0),
        }

        pad = max(sma_p + look + 30, 120)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days_i + pad)
        config = self._base_config()
        service = AlpacaService(config)
        long_bars = service.get_bars_range(
            long_s, start=start, end=end, timeframe=tf
        )
        short_bars = service.get_bars_range(
            short_s, start=start, end=end, timeframe=tf
        )
        if long_bars.empty or short_bars.empty:
            raise ValueError(f"No bar data for {long_s}/{short_s}")
        long_use = self._trim_backtest_bars(long_bars, days_i=days_i, end=end)
        short_use = self._trim_backtest_bars(short_bars, days_i=days_i, end=end)
        result = run_pair_backtest(
            long_use,
            short_use,
            strategy,
            initial_cash=cash,
            slip_bps=float(params["slip_bps"]),
        )
        result["symbol"] = f"{long_s}/{short_s}"
        result["mode"] = "pair"
        result["bar_timeframe"] = tf
        result["days"] = days_i
        result["params"] = params
        result["run_kind"] = "pair"
        result["symbols"] = [long_s, short_s]
        result["meta"] = {
            "mode": "pair",
            "bar_timeframe": tf,
            "days": days_i,
            "params": params,
            "run_kind": "pair",
            "symbols": [long_s, short_s],
        }
        # History is persisted by the /api/backtest handler (same as sma/dip).
        return result

    def _run_ls_backtest(
        self,
        *,
        days: int,
        bar_timeframe: str,
        initial_cash: float,
        symbols: str | None,
        symbol: str,
        run_kind: str = "per_symbol",
        ls_ema_fast: int | None = None,
        ls_ema_slow: int | None = None,
        ls_adx_min: float | None = None,
        ls_atr_stop_mult: float | None = None,
        ls_risk_pct: float | None = None,
        ls_rr: float | None = None,
        ls_time_stop_bars: int | None = None,
        ls_commission_pct: float | None = None,
        ls_slippage_pct: float | None = None,
    ) -> dict[str, Any]:
        """Walk-forward Regime Dual Momentum long/short (daily bars)."""
        days_i = int(days)
        if days_i < 30 or days_i > 1500:
            raise ValueError("days must be between 30 and 1500")
        tf = "1Day"
        cash = float(initial_cash)
        if cash < 100:
            raise ValueError("initial_cash must be at least 100")

        kind = str(run_kind or "per_symbol").strip().lower().replace("-", "_")
        if kind not in {"per_symbol", "portfolio"}:
            raise ValueError("run_kind must be per_symbol or portfolio")

        symbol_list = parse_backtest_symbols(symbols, symbol)

        with self.lock:
            s = self.settings
            desk_fast = s.ls_ema_fast
            desk_slow = s.ls_ema_slow
            desk_adx = s.ls_adx_min
            desk_atr = s.ls_atr_stop_mult
            desk_risk = s.ls_risk_pct
            desk_rr = s.ls_rr
            desk_time = s.ls_time_stop_bars

        ema_f = int(ls_ema_fast if ls_ema_fast is not None else desk_fast)
        ema_s = int(ls_ema_slow if ls_ema_slow is not None else desk_slow)
        if ema_f >= ema_s:
            raise ValueError("ls_ema_fast must be < ls_ema_slow")
        adx_min = float(ls_adx_min if ls_adx_min is not None else desk_adx)
        atr_mult = float(
            ls_atr_stop_mult if ls_atr_stop_mult is not None else desk_atr
        )
        risk_pct = float(ls_risk_pct if ls_risk_pct is not None else desk_risk)
        rr = float(ls_rr if ls_rr is not None else desk_rr)
        time_stop = int(
            ls_time_stop_bars if ls_time_stop_bars is not None else desk_time
        )
        commission = float(
            ls_commission_pct if ls_commission_pct is not None else 0.05
        )
        slippage = float(ls_slippage_pct if ls_slippage_pct is not None else 0.02)

        strategy = LongShortRegimeStrategy(
            ema_fast=ema_f, ema_slow=ema_s, adx_min=adx_min
        )
        risk = LSRiskParams(
            atr_stop_mult=atr_mult,
            risk_pct=risk_pct,
            rr=rr,
            time_stop_bars=time_stop,
            commission_pct=commission,
            slippage_pct=slippage,
        )
        params = {
            "ls_ema_fast": ema_f,
            "ls_ema_slow": ema_s,
            "ls_adx_min": adx_min,
            "ls_atr_stop_mult": atr_mult,
            "ls_risk_pct": risk_pct,
            "ls_rr": rr,
            "ls_time_stop_bars": time_stop,
            "ls_commission_pct": commission,
            "ls_slippage_pct": slippage,
            "label": f"LS EMA{ema_f}/{ema_s} ADX≥{adx_min:g}",
        }

        pad = max(strategy.bars_needed + 30, 120)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days_i + pad)
        config = self._base_config()
        service = AlpacaService(config)

        def _one(sym: str, sleeve_cash: float) -> dict[str, Any]:
            bars = service.get_bars_range(sym, start=start, end=end, timeframe=tf)
            if bars.empty:
                raise ValueError(f"No bar data returned for {sym}")
            bars_use = self._trim_backtest_bars(bars, days_i=days_i, end=end)
            # Eval starts at the lookback window (after warmup retained in bars_use)
            cutoff = end - timedelta(days=days_i)
            if bars_use.index.tz is None:
                cutoff = cutoff.replace(tzinfo=None)
            else:
                cutoff = cutoff.astimezone(bars_use.index.tz)
            eval_start = bars_use.index[bars_use.index >= cutoff]
            es = eval_start[0] if len(eval_start) else bars_use.index[min(strategy.bars_needed, len(bars_use) - 1)]
            result = run_ls_backtest(
                bars_use,
                strategy,
                risk,
                initial_cash=sleeve_cash,
                eval_start=es,
            )
            result["symbol"] = sym
            result["mode"] = "ls"
            result["bar_timeframe"] = tf
            result["days"] = days_i
            result["params"] = params
            return result

        if kind == "per_symbol" and len(symbol_list) == 1:
            result = _one(symbol_list[0], cash)
            result["run_kind"] = "per_symbol"
            result["symbols"] = symbol_list
            result["meta"] = {
                "mode": "ls",
                "bar_timeframe": tf,
                "days": days_i,
                "params": params,
                "run_kind": "per_symbol",
                "symbols": symbol_list,
            }
            return result

        if kind == "portfolio" or len(symbol_list) > 1:
            bars_by_symbol: dict[str, pd.DataFrame] = {}
            errors: list[dict[str, Any]] = []
            for sym in symbol_list:
                try:
                    bars = service.get_bars_range(
                        sym, start=start, end=end, timeframe=tf
                    )
                    if bars.empty:
                        raise ValueError(f"No bar data returned for {sym}")
                    bars_by_symbol[sym] = self._trim_backtest_bars(
                        bars, days_i=days_i, end=end
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append({"symbol": sym, "error": str(exc)})

            if not bars_by_symbol:
                detail = "; ".join(f"{e['symbol']}: {e['error']}" for e in errors)
                raise ValueError(detail or "No bar data for LS symbols")

            cutoff = end - timedelta(days=days_i)
            # Align eval_start from first available series
            sample = next(iter(bars_by_symbol.values()))
            if sample.index.tz is None:
                cutoff_ts = cutoff.replace(tzinfo=None)
            else:
                cutoff_ts = cutoff.astimezone(sample.index.tz)
            eval_idx = sample.index[sample.index >= cutoff_ts]
            es = (
                eval_idx[0]
                if len(eval_idx)
                else sample.index[min(strategy.bars_needed, len(sample) - 1)]
            )

            if kind == "portfolio":
                book = run_ls_portfolio_backtest(
                    bars_by_symbol,
                    strategy,
                    risk,
                    initial_cash=cash,
                    eval_start=es,
                    max_concurrent=4,
                )
                legs = list(book.pop("per_symbol", []) or [])
                for leg in legs:
                    leg.update(
                        {
                            "mode": "ls",
                            "bar_timeframe": tf,
                            "days": days_i,
                            "params": params,
                            "run_kind": "portfolio_leg",
                        }
                    )
                book.update(
                    {
                        "mode": "ls",
                        "bar_timeframe": tf,
                        "days": days_i,
                        "params": params,
                        "run_kind": "portfolio",
                        "symbols": list(bars_by_symbol.keys()),
                        "results": legs,
                        "summary": [summary_from_result(r) for r in legs],
                        "errors": errors,
                        "meta": {
                            "mode": "ls",
                            "bar_timeframe": tf,
                            "days": days_i,
                            "params": params,
                            "run_kind": "portfolio",
                            "symbols": list(bars_by_symbol.keys()),
                        },
                    }
                )
                return book

            # Compare / per_symbol multi
            results: list[dict[str, Any]] = []
            for sym, bars_use in bars_by_symbol.items():
                if bars_use.index.tz is None:
                    c = cutoff.replace(tzinfo=None)
                else:
                    c = cutoff.astimezone(bars_use.index.tz)
                eidx = bars_use.index[bars_use.index >= c]
                esym = (
                    eidx[0]
                    if len(eidx)
                    else bars_use.index[min(strategy.bars_needed, len(bars_use) - 1)]
                )
                r = run_ls_backtest(
                    bars_use,
                    strategy,
                    risk,
                    initial_cash=cash,
                    eval_start=esym,
                )
                r["symbol"] = sym
                r["mode"] = "ls"
                r["bar_timeframe"] = tf
                r["days"] = days_i
                r["params"] = params
                r["run_kind"] = "per_symbol"
                results.append(r)
            for err in errors:
                results.append(
                    {
                        "symbol": err["symbol"],
                        "error": err["error"],
                        "mode": "ls",
                        "days": days_i,
                    }
                )
            ok_results = [r for r in results if not r.get("error")]
            if not ok_results:
                detail = "; ".join(
                    f"{r.get('symbol')}: {r.get('error')}" for r in results
                )
                raise ValueError(detail or "All LS symbol backtests failed")

            summary = [summary_from_result(r) for r in results]
            rets = [float(r["total_return_pct"]) for r in ok_results]
            holds = [float(r["buy_hold_return_pct"]) for r in ok_results]
            dds = [float(r["max_drawdown_pct"]) for r in ok_results]
            eqs = [float(r["final_equity"]) for r in ok_results]
            n_ok = len(ok_results)
            avg_ret = sum(rets) / n_ok
            avg_hold = sum(holds) / n_ok
            avg_equity = sum(eqs) / n_ok
            total_round = sum(int(r.get("round_trips") or 0) for r in ok_results)
            total_wins = sum(int(r.get("wins") or 0) for r in ok_results)
            label = "+".join(symbol_list)
            return {
                "symbol": label,
                "mode": "ls",
                "bar_timeframe": tf,
                "days": days_i,
                "params": params,
                "run_kind": "per_symbol",
                "symbols": symbol_list,
                "initial_cash": cash,
                "final_equity": round(avg_equity, 2),
                "total_return_pct": round(avg_ret, 2),
                "buy_hold_return_pct": round(avg_hold, 2),
                "max_drawdown_pct": round(max(dds) if dds else 0.0, 2),
                "realized_pnl": round(
                    sum(float(r.get("realized_pnl") or 0) for r in ok_results), 2
                ),
                "trades": sum(int(r.get("trades") or 0) for r in ok_results),
                "round_trips": total_round,
                "wins": total_wins,
                "losses": sum(int(r.get("losses") or 0) for r in ok_results),
                "win_rate": (
                    round((total_wins / total_round), 4) if total_round else 0.0
                ),
                "start": min(
                    (r["start"] for r in ok_results if r.get("start")), default=None
                ),
                "end": max(
                    (r["end"] for r in ok_results if r.get("end")), default=None
                ),
                "evaluated_bars": sum(
                    int(r.get("evaluated_bars") or 0) for r in ok_results
                ),
                # Per-symbol compare: no shared book curve — pick a symbol in the UI.
                "equity_curve": [],
                "trade_list": [],
                "results": results,
                "summary": summary,
                "errors": errors,
                "open_qty": 0,
                "open_entry": None,
                "meta": {
                    "mode": "ls",
                    "bar_timeframe": tf,
                    "days": days_i,
                    "params": params,
                    "run_kind": "per_symbol",
                    "symbols": symbol_list,
                },
            }

        # Fallback single
        return _one(symbol_list[0], cash)

    def run_strategy_backtest(
        self,
        *,
        mode: str,
        symbol: str = "AAPL",
        symbols: str | None = None,
        run_kind: str = "per_symbol",
        days: int = 365,
        bar_timeframe: str = "1Day",
        qty: float | None = None,
        initial_cash: float = 10_000.0,
        fast_sma: int | None = None,
        slow_sma: int | None = None,
        sma_preset: str | None = None,
        dip_preset: str | None = None,
        dip_rsi_buy: float | None = None,
        dip_rsi_sell: float | None = None,
        dip_skip_bearish: bool | None = None,
        stop_loss_pct: float | None = None,
        pair_preset: str | None = None,
        pair_sma_period: int | None = None,
        pair_lookback: int | None = None,
        pair_impulse_pct: float | None = None,
        pair_weak_side: str | None = None,
        pair_long_symbol: str | None = None,
        pair_short_symbol: str | None = None,
        slip_bps: float | None = None,
        ls_ema_fast: int | None = None,
        ls_ema_slow: int | None = None,
        ls_adx_min: float | None = None,
        ls_atr_stop_mult: float | None = None,
        ls_risk_pct: float | None = None,
        ls_rr: float | None = None,
        ls_time_stop_bars: int | None = None,
        ls_commission_pct: float | None = None,
        ls_slippage_pct: float | None = None,
    ) -> dict[str, Any]:
        """Fetch history and walk-forward SMA, dip, pair, or LS (no live orders)."""
        mode_key = str(mode or "sma").strip().lower()
        if mode_key not in {"sma", "dip", "pair", "ls"}:
            raise ValueError("Backtest supports sma, dip, pair, or ls only (not AI)")

        if mode_key == "pair":
            return self._run_pair_backtest(
                days=days,
                bar_timeframe=bar_timeframe,
                initial_cash=initial_cash,
                pair_preset=pair_preset,
                pair_sma_period=pair_sma_period,
                pair_lookback=pair_lookback,
                pair_impulse_pct=pair_impulse_pct,
                pair_weak_side=pair_weak_side,
                pair_long_symbol=pair_long_symbol,
                pair_short_symbol=pair_short_symbol,
                slip_bps=slip_bps,
                symbols=symbols,
                symbol=symbol,
            )

        if mode_key == "ls":
            return self._run_ls_backtest(
                days=days,
                bar_timeframe=bar_timeframe,
                initial_cash=initial_cash,
                symbols=symbols,
                symbol=symbol,
                run_kind=run_kind,
                ls_ema_fast=ls_ema_fast,
                ls_ema_slow=ls_ema_slow,
                ls_adx_min=ls_adx_min,
                ls_atr_stop_mult=ls_atr_stop_mult,
                ls_risk_pct=ls_risk_pct,
                ls_rr=ls_rr,
                ls_time_stop_bars=ls_time_stop_bars,
                ls_commission_pct=ls_commission_pct,
                ls_slippage_pct=ls_slippage_pct,
            )

        kind = str(run_kind or "per_symbol").strip().lower().replace("-", "_")
        if kind not in {"per_symbol", "portfolio"}:
            raise ValueError("run_kind must be per_symbol or portfolio")

        symbol_list = parse_backtest_symbols(symbols, symbol)

        days_i = int(days)
        if days_i < 30 or days_i > 1500:
            raise ValueError("days must be between 30 and 1500")

        tf = str(bar_timeframe or "1Day").strip()
        allowed_tf = {"1Min", "5Min", "15Min", "1Hour", "1Day"}
        if tf not in allowed_tf:
            raise ValueError(f"bar_timeframe must be one of {sorted(allowed_tf)}")
        if tf != "1Day" and days_i > 60:
            raise ValueError("Intraday backtests are limited to 60 days")

        cash = float(initial_cash)
        if cash < 100:
            raise ValueError("initial_cash must be at least 100")

        with self.lock:
            s = self.settings
            trade_qty = float(qty if qty is not None else s.trade_qty)
            stop_pct = (
                float(s.stop_loss_pct or 0)
                if stop_loss_pct is None
                else float(stop_loss_pct)
            )
            desk_sma_preset = s.sma_preset
            desk_fast = s.fast_sma
            desk_slow = s.slow_sma
            desk_dip_buy = s.dip_rsi_buy
            desk_dip_sell = s.dip_rsi_sell
            desk_dip_skip = s.dip_skip_bearish

        if trade_qty <= 0:
            raise ValueError("qty must be > 0")

        params: dict[str, Any]
        if mode_key == "sma":
            preset_id = resolve_sma_preset_id(sma_preset or desk_sma_preset)
            preset = get_sma_preset(preset_id)
            if preset_id == "custom":
                fast = int(fast_sma if fast_sma is not None else desk_fast)
                slow = int(slow_sma if slow_sma is not None else desk_slow)
            else:
                fast, slow = preset.fast_sma, preset.slow_sma
            if fast >= slow:
                raise ValueError("fast_sma must be < slow_sma")
            strategy = build_strategy("sma", fast_sma=fast, slow_sma=slow)
            params = {
                "sma_preset": preset_id if preset_id != "custom" else match_preset_id(fast, slow),
                "fast_sma": fast,
                "slow_sma": slow,
                "label": (
                    preset.label
                    if preset_id != "custom"
                    else f"Custom {fast}/{slow}"
                ),
            }
        else:
            dip_id = resolve_dip_preset_id(dip_preset or "deep")
            dip = get_dip_preset(dip_id)
            if dip_id == "custom":
                rsi_buy = float(
                    dip_rsi_buy if dip_rsi_buy is not None else desk_dip_buy
                )
                rsi_sell = float(
                    dip_rsi_sell if dip_rsi_sell is not None else desk_dip_sell
                )
                skip = (
                    bool(desk_dip_skip)
                    if dip_skip_bearish is None
                    else bool(dip_skip_bearish)
                )
                use_band = True
            else:
                rsi_buy, rsi_sell = dip.rsi_buy, dip.rsi_sell
                skip = dip.skip_bearish
                use_band = dip.use_lower_band
            strategy = build_strategy(
                "dip",
                dip_rsi_buy=rsi_buy,
                dip_rsi_sell=rsi_sell,
                dip_skip_bearish=skip,
                use_lower_band=use_band,
            )
            params = {
                "dip_preset": dip_id,
                "dip_rsi_buy": rsi_buy,
                "dip_rsi_sell": rsi_sell,
                "dip_skip_bearish": skip,
                "use_lower_band": use_band,
                "label": dip.label if dip_id != "custom" else "Custom dip",
            }

        # Extra calendar days so indicators have warmup before the window.
        pad = 120 if tf == "1Day" else max(days_i, 14)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days_i + pad)

        meta = {
            "mode": mode_key,
            "bar_timeframe": tf,
            "days": days_i,
            "qty": trade_qty,
            "stop_loss_pct": stop_pct,
            "params": params,
            "run_kind": kind,
            "symbols": symbol_list,
        }

        # Single-symbol per_symbol keeps the legacy flat payload.
        if kind == "per_symbol" and len(symbol_list) == 1:
            sym = symbol_list[0]
            config = self._base_config()
            config = config.override(bar_timeframe=tf, symbol=sym, symbols=sym)
            service = AlpacaService(config)
            bars = service.get_bars_range(sym, start=start, end=end, timeframe=tf)
            if bars.empty:
                raise ValueError(f"No bar data returned for {sym}")
            bars_use = self._trim_backtest_bars(bars, days_i=days_i, end=end)
            result = run_backtest(
                bars_use,
                strategy,
                qty=trade_qty,
                initial_cash=cash,
                stop_loss_pct=stop_pct,
            )
            return {"symbol": sym, **meta, **result}

        config = self._base_config()
        primary = symbol_list[0]
        config = config.override(
            bar_timeframe=tf,
            symbol=primary,
            symbols=",".join(symbol_list),
        )
        service = AlpacaService(config)

        if kind == "portfolio":
            bars_by_symbol: dict[str, pd.DataFrame] = {}
            errors: list[dict[str, Any]] = []
            for sym in symbol_list:
                try:
                    bars = service.get_bars_range(
                        sym, start=start, end=end, timeframe=tf
                    )
                    if bars.empty:
                        raise ValueError(f"No bar data returned for {sym}")
                    bars_by_symbol[sym] = self._trim_backtest_bars(
                        bars, days_i=days_i, end=end
                    )
                except Exception as exc:  # noqa: BLE001 — per-symbol soft fail
                    errors.append({"symbol": sym, "error": str(exc)})

            if not bars_by_symbol:
                detail = "; ".join(f"{e['symbol']}: {e['error']}" for e in errors)
                raise ValueError(detail or "No bar data for portfolio symbols")

            book = run_portfolio_backtest(
                bars_by_symbol,
                strategy,
                qty=trade_qty,
                initial_cash=cash,
                stop_loss_pct=stop_pct,
            )
            legs = list(book.pop("results", []) or [])
            # Attach shared meta onto each leg for UI detail views.
            for leg in legs:
                leg.update(
                    {
                        "mode": mode_key,
                        "bar_timeframe": tf,
                        "days": days_i,
                        "qty": trade_qty,
                        "stop_loss_pct": stop_pct,
                        "params": params,
                        "run_kind": "portfolio_leg",
                        "initial_cash": cash,
                    }
                )
            for err in errors:
                legs.append(err)
            summary = [summary_from_result(r) for r in legs]
            label = "+".join(symbol_list)
            # Prefer requested symbol order; book.symbols would otherwise drop failures.
            return {
                "symbol": label,
                **meta,
                **book,
                "symbols": symbol_list,
                "results": legs,
                "summary": summary,
                "errors": errors,
            }

        # per_symbol multi: independent cash per symbol.
        results: list[dict[str, Any]] = []
        for sym in symbol_list:
            try:
                bars = service.get_bars_range(sym, start=start, end=end, timeframe=tf)
                if bars.empty:
                    raise ValueError(f"No bar data returned for {sym}")
                bars_use = self._trim_backtest_bars(bars, days_i=days_i, end=end)
                one = run_backtest(
                    bars_use,
                    strategy,
                    qty=trade_qty,
                    initial_cash=cash,
                    stop_loss_pct=stop_pct,
                )
                results.append(
                    {
                        "symbol": sym,
                        "mode": mode_key,
                        "bar_timeframe": tf,
                        "days": days_i,
                        "qty": trade_qty,
                        "stop_loss_pct": stop_pct,
                        "params": params,
                        "run_kind": "per_symbol",
                        **one,
                    }
                )
            except Exception as exc:  # noqa: BLE001 — keep other symbols running
                results.append({"symbol": sym, "error": str(exc)})

        ok_results = [r for r in results if not r.get("error")]
        if not ok_results:
            detail = "; ".join(
                f"{r.get('symbol')}: {r.get('error')}" for r in results
            )
            raise ValueError(detail or "All symbol backtests failed")

        summary = [summary_from_result(r) for r in results]
        # Aggregate headline metrics (mean return / equity across successful legs).
        # Each leg used the same initial_cash, so average equity aligns with avg return.
        rets = [float(r["total_return_pct"]) for r in ok_results]
        holds = [float(r["buy_hold_return_pct"]) for r in ok_results]
        dds = [float(r["max_drawdown_pct"]) for r in ok_results]
        eqs = [float(r["final_equity"]) for r in ok_results]
        n_ok = len(ok_results)
        avg_ret = sum(rets) / n_ok
        avg_hold = sum(holds) / n_ok
        avg_equity = sum(eqs) / n_ok
        total_round = sum(int(r.get("round_trips") or 0) for r in ok_results)
        total_wins = sum(int(r.get("wins") or 0) for r in ok_results)
        label = "+".join(symbol_list)
        return {
            "symbol": label,
            **meta,
            "initial_cash": cash,
            "final_equity": round(avg_equity, 2),
            "total_return_pct": round(avg_ret, 2),
            "buy_hold_return_pct": round(avg_hold, 2),
            "max_drawdown_pct": round(max(dds) if dds else 0.0, 2),
            "realized_pnl": round(
                sum(float(r.get("realized_pnl") or 0) for r in ok_results), 2
            ),
            "trades": sum(int(r.get("trades") or 0) for r in ok_results),
            "round_trips": total_round,
            "wins": total_wins,
            "losses": sum(int(r.get("losses") or 0) for r in ok_results),
            "win_rate": round((total_wins / total_round), 4) if total_round else 0.0,
            "start": min((r["start"] for r in ok_results if r.get("start")), default=None),
            "end": max((r["end"] for r in ok_results if r.get("end")), default=None),
            "evaluated_bars": sum(int(r.get("evaluated_bars") or 0) for r in ok_results),
            # Per-symbol compare: no shared book curve — pick a symbol in the UI.
            "equity_curve": [],
            "trade_list": [],
            "results": results,
            "summary": summary,
            "open_qty": 0,
            "open_entry": None,
        }

    def save_backtest_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Persist a completed backtest and return the history entry."""
        return backtest_store.append_result(result)

    def list_backtest_history(self) -> list[dict[str, Any]]:
        return backtest_store.list_summaries()

    def get_backtest_history_entry(self, entry_id: int) -> dict[str, Any] | None:
        return backtest_store.get_entry(entry_id)

    def delete_backtest_history_entry(self, entry_id: int) -> bool:
        return backtest_store.delete_entry(entry_id)

    def clear_backtest_history(self) -> None:
        backtest_store.clear_all()

    def compare_backtests(self, ids: list[int]) -> list[dict[str, Any]]:
        return backtest_store.compare_entries(ids)

    @staticmethod
    def _parse_range_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
        """Parse a `YYYY-MM-DD` (or full ISO) custom-range bound as an ET instant."""
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            if len(raw) == 10:
                day = datetime.strptime(raw, "%Y-%m-%d")
                # Alpaca's `until` is exclusive. The next ET midnight includes
                # every sub-second fill on the selected date without leaking the
                # following day into the range.
                if end_of_day:
                    day += timedelta(days=1)
                return day.replace(tzinfo=_ET).astimezone(timezone.utc)
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"date must be YYYY-MM-DD or ISO-8601, got: {raw}"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_ET)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _opening_position(
        service: Any,
        window_orders: list[dict[str, Any]],
        *,
        until: datetime | None,
    ) -> tuple[dict[str, float], dict[str, float | None], str | None]:
        """Signed quantity per symbol the book held when the window opened.

        Alpaca is the authority on what is held *now*, and the fill window says
        what moved since — so subtracting the one from the other reconstructs
        the starting quantity exactly. Also returns each symbol's average entry,
        which is only a fallback price for shares no fill can account for.
        """
        try:
            positions = service.get_all_positions()
        except Exception as exc:  # a fills page must not die over a seed
            return {}, {}, str(exc)

        rows = list(window_orders)
        if until is not None and until < datetime.now(timezone.utc):
            # A range that ends in the past: fills *after* it moved the book
            # too, so they have to come out of the subtraction as well.
            try:
                tail = service.fetch_fill_activities(
                    after=until,
                    page_size=_FILL_PAGE_SIZE,
                    max_pages=_TRAILING_FILL_PAGES,
                )
            except Exception as exc:
                return {}, {}, str(exc)
            if tail.get("truncated"):
                return {}, {}, "too many fills since this range to reconstruct it"
            rows += list(tail["orders"])

        current: dict[str, float] = {}
        avg_entry: dict[str, float | None] = {}
        for pos in positions or []:
            sym = str(pos.get("symbol") or "").upper()
            if not sym:
                continue
            current[sym] = float(pos.get("signed_qty") or 0.0)
            price = pos.get("avg_entry_price")
            avg_entry[sym] = float(price) if price else None

        moved: dict[str, float] = {}
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            side = str(row.get("side") or "").lower()
            qty = float(row.get("qty") or 0)
            if not sym or qty <= 0 or side not in {"buy", "sell"}:
                continue
            moved[sym] = moved.get(sym, 0.0) + (qty if side == "buy" else -qty)

        start: dict[str, float] = {}
        usable_entry: dict[str, float | None] = {}
        for sym in set(current) | set(moved):
            qty = round(current.get(sym, 0.0) - moved.get(sym, 0.0), 9)
            if abs(qty) <= 1e-9:
                continue
            start[sym] = qty
            # The average entry describes the position as it stands now, so it
            # only prices the opening one while that position still runs the
            # same way. Flat or flipped since, there is no basis to borrow.
            price = avg_entry.get(sym)
            usable_entry[sym] = (
                price if price is not None and current.get(sym, 0.0) * qty > 0 else None
            )
        return start, usable_entry, None

    @staticmethod
    def _opening_inventory(
        service: Any,
        window_orders: list[dict[str, Any]],
        *,
        after: datetime | None,
        until: datetime | None,
    ) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
        """The FIFO parcels the book already held when the window opened.

        Quantity comes from the position arithmetic above and is exact. Price
        comes from the execution fills that actually opened those parcels, read
        from before the window. The opening walk and the visible window must use
        the same ledger: closed-order rows collapse partial executions and can
        assign them a different average price or time, which makes the same
        closing fill report different P&L under Day, Week, and Month.

        Only the shares no pre-window fill can account for fall back to Alpaca's
        average entry, and they are flagged ``estimated`` so the page can say
        so. Shares with no basis at all are priced ``None`` — the close is real,
        its profit is not knowable, and the page reports that rather than a
        number built from half the shares.
        """
        start, avg_entry, error = AppState._opening_position(
            service, window_orders, until=until
        )
        if error is not None or not start:
            return {}, error

        residual: dict[str, list[dict[str, Any]]] = {}
        lookback_error: str | None = None
        # `after is None` means the window already reaches back to the account's
        # first fill, so there is nothing older to read.
        if after is not None:
            try:
                older = service.fetch_fill_activities(
                    until=after,
                    page_size=_FILL_PAGE_SIZE,
                )
                if older.get("truncated"):
                    lookback_error = (
                        "fill history before this range was truncated; "
                        "opening cost basis may be incomplete"
                    )
                else:
                    # Alpaca's `until` boundary can be inclusive. Do not seed a
                    # boundary execution that the selected window also owns.
                    prefix = []
                    for row in older["orders"]:
                        raw_time = row.get("filled_at") or row.get("submitted_at")
                        try:
                            filled_at = datetime.fromisoformat(
                                str(raw_time).replace("Z", "+00:00")
                            )
                        except (TypeError, ValueError):
                            continue
                        if filled_at.tzinfo is None:
                            filled_at = filled_at.replace(tzinfo=timezone.utc)
                        if filled_at.astimezone(timezone.utc) < after:
                            prefix.append(row)
                    residual = AlpacaService.open_lots_from_orders(
                        prefix, symbols=set(start)
                    )
            except Exception as exc:
                lookback_error = str(exc)

        lots: dict[str, list[dict[str, Any]]] = {}
        for sym, signed in start.items():
            direction = 1 if signed > 0 else -1
            wanted = abs(signed)
            parcels = [
                dict(lot)
                for lot in residual.get(sym, [])
                if lot.get("direction") == direction
            ]
            # Alpaca is the authority on the quantity. Anything the walk shows
            # beyond it was retired by a sell older than the lookback, and FIFO
            # retires the oldest parcels first.
            excess = round(sum(p["qty"] for p in parcels) - wanted, 9)
            while excess > 1e-9 and parcels:
                trim = min(parcels[0]["qty"], excess)
                parcels[0]["qty"] = round(parcels[0]["qty"] - trim, 9)
                excess = round(excess - trim, 9)
                if parcels[0]["qty"] <= 1e-9:
                    parcels.pop(0)

            seeded = [
                {
                    "qty": p["qty"],
                    "price": p.get("price"),
                    "direction": direction,
                    "estimated": False,
                }
                for p in parcels
            ]
            shortfall = round(wanted - sum(p["qty"] for p in seeded), 9)
            if shortfall > 1e-9:
                # Entries older than the lookback: fall back to the average
                # entry, which `_opening_position` has already vetted.
                seeded.insert(
                    0,
                    {
                        "qty": shortfall,
                        "price": avg_entry.get(sym),
                        "direction": direction,
                        "estimated": True,
                    },
                )
            lots[sym] = seeded
        return lots, lookback_error

    def trade_history(
        self,
        *,
        range_key: str = "month",
        symbol: str | None = None,
        side: str | None = None,
        limit: int = 100,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Alpaca fills + portfolio P&L for a named time range."""
        key = str(range_key or "month").strip().lower()
        if key not in _HISTORY_RANGES:
            raise ValueError(
                f"range must be one of: {', '.join(_HISTORY_RANGES)}"
            )
        meta = _HISTORY_RANGES[key]
        now = datetime.now(timezone.utc)
        after: datetime | None = None
        until: datetime | None = None
        if key == "custom":
            after = self._parse_range_bound(start, end_of_day=False)
            until = self._parse_range_bound(end, end_of_day=True)
            if after is None and until is None:
                raise ValueError("custom range needs a start or an end date")
            if after and until and after > until:
                raise ValueError("custom range start must be before end")
        elif key == "ytd":
            after = datetime(now.astimezone(_ET).year, 1, 1, tzinfo=_ET).astimezone(
                timezone.utc
            )
        elif meta["days"] is not None:
            after = now - timedelta(days=int(meta["days"]))

        sym = (symbol or "").strip().upper() or None
        side_f = (side or "").strip().lower() or None
        if side_f and side_f not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")

        # A limit of 0 means "no display cap" — the window is already loaded, so
        # listing all of it costs nothing beyond the render.
        raw_limit = int(limit or 0)
        cap = None if raw_limit <= 0 else max(1, min(raw_limit, 500))
        bot = self._build_algo_bot()
        # FIFO runs over the full fill ledger; the cap only trims what the page
        # lists. Activities are true executions (including partial fills), and
        # their page token can walk the complete range without a 500-order wall.
        window = bot.service.fetch_fill_activities(
            after=after,
            until=until,
            page_size=_FILL_PAGE_SIZE,
        )
        window_orders = window["orders"]
        window_truncated = bool(window["truncated"])
        # Seed FIFO with what the book already held when the window opened, or
        # a sell whose buy predates the range reads as a new short — and the
        # next buy of that symbol "covers" it and invents a profit on an entry.
        opening_lots, opening_error = self._opening_inventory(
            bot.service, window_orders, after=after, until=until
        )
        fifo = AlpacaService.realized_pnl_from_orders(
            window_orders, opening_lots=opening_lots
        )
        rows = list(fifo["orders"])
        if sym:
            rows = [o for o in rows if o.get("symbol") == sym]
        if side_f:
            rows = [o for o in rows if o.get("side") == side_f]
        match_count = len(rows)
        trades = rows if cap is None else rows[:cap]
        capped = cap is not None and match_count > cap

        filtered = bool(sym or side_f)
        # A symbol filter has an exact FIFO answer; a side filter does not change
        # realized P&L at all, since realized only exists where a buy meets a sell.
        realized = (
            fifo["by_symbol"].get(sym, 0.0) if sym else fifo["realized_pnl"]
        )
        realized_scope = "symbol" if sym else "range"

        portfolio: dict[str, Any] | None = None
        portfolio_error: str | None = None
        try:
            if key == "custom":
                portfolio = bot.service.portfolio_history_summary(
                    start=after or (until - timedelta(days=1)),
                    end=until,
                    label="custom",
                )
            elif key == "ytd":
                portfolio = bot.service.portfolio_history_summary(
                    start=after, label="ytd"
                )
            else:
                portfolio = bot.service.portfolio_history_summary(
                    period=str(meta["period"])
                )
        except Exception as exc:
            portfolio_error = str(exc)

        return {
            "range": key,
            "after": after.isoformat() if after else None,
            "until": until.isoformat() if until else None,
            "symbol": sym,
            "side": side_f,
            "limit": cap,
            "capped": capped,
            "window_truncated": window_truncated,
            "window_limit": None,
            "window_page_count": int(window.get("page_count") or 0),
            "filtered": filtered,
            # Account P&L is account-wide by definition; filters never scope it.
            "portfolio": portfolio,
            "portfolio_error": portfolio_error,
            "portfolio_scope": "account",
            "realized_pnl": realized,
            "realized_scope": realized_scope,
            "realized_range_pnl": fifo["realized_pnl"],
            "realized_by_symbol": fifo["by_symbol"],
            "matched_sells": fifo["matched_sells"],
            "open_lot_qty": fifo["open_lot_qty"],
            "realized_by_entry_order": fifo["realized_by_entry_order"],
            "unmatched_sells": fifo["unmatched_sells"],
            "unmatched_sell_qty": fifo["unmatched_sell_qty"],
            "opening_inventory": opening_lots,
            "opening_inventory_error": opening_error,
            "estimated_close_qty": fifo["estimated_close_qty"],
            "stats": fifo["stats"],
            # Client-side symbol/side filters need the whole broker window;
            # `trades` remains display-capped for API compatibility.
            "window_trades": fifo["orders"],
            "trades": trades,
            "trade_count": len(trades),
            "match_count": match_count,
            "window_trade_count": len(fifo["orders"]),
        }

    # ── History insights ────────────────────────────────────────────

    def _insight_provider(self) -> Any:
        """The configured LLM, or a ValueError naming the missing key."""
        from bot.ai_providers import build_provider

        config = self._base_config()
        if config.ai_provider == "openai" and not config.openai_api_key:
            raise ValueError("OpenAI API key missing — paste it on Configuration")
        if config.ai_provider == "gemini" and not config.gemini_api_key:
            raise ValueError("Gemini API key missing — paste it on Configuration")
        return build_provider(
            config.ai_provider,
            openai_key=config.openai_api_key,
            gemini_key=config.gemini_api_key,
            openai_model=config.openai_model,
            gemini_model=config.gemini_model,
        )

    def _ai_configured(self) -> bool:
        try:
            config = self._base_config()
        except Exception:
            return False
        if config.ai_provider == "gemini":
            return bool(config.gemini_api_key)
        return bool(config.openai_api_key)

    def history_insights(
        self,
        *,
        range_key: str = "month",
        start: str | None = None,
        end: str | None = None,
        lang: str | None = None,
        scope: str = "debrief",
        narrate_range: bool = True,
    ) -> dict[str, Any]:
        """Deterministic fact sheet for a range, optionally narrated.

        The facts are always returned. Narration is the only part that needs a
        provider, so a desk with no AI key still gets calibration, attribution,
        the execution audit and the flags — just without the prose.

        The range is the only scope. The page's symbol/side controls filter the
        fill list; they do not filter the FIFO walk behind these numbers, so
        forwarding them here would hand the model a filter label its own figures
        never honoured.

        ``lang`` is the language the caller is displaying. It wins over the
        saved desk language, which the page only updates when someone touches
        the switcher — a browser restored into Bangla never announces itself,
        and the review would come back in English beside its Bangla flags.
        """
        trades = self.trade_history(range_key=range_key, limit=0, start=start, end=end)
        with self.lock:
            sessions = [dict(s) for s in self.loop_sessions]
            events = list(self.desk_events)
            lang = normalize_lang(lang or self.settings.lang)
        facts = build_facts(trades, sessions=sessions, events=events)

        payload: dict[str, Any] = {
            "facts": facts,
            "scope": scope,
            "lang": lang,
            "ai_available": self._ai_configured(),
            "narration": None,
            "narration_error": None,
            "cached": False,
        }
        if not narrate_range:
            return payload
        if not payload["ai_available"]:
            payload["narration_error"] = "no_ai_key"
            return payload

        key = self._insight_cache_key(trades, scope, lang, facts)
        cached = self._insight_cache.get(key)
        if cached is not None:
            payload["narration"] = cached
            payload["cached"] = True
            return payload
        try:
            provider = self._insight_provider()
            narration = narrate(
                facts,
                provider=provider,
                scope=scope,
                lang=lang,
                saved_lessons=lessons_store.list_lessons(),
            )
        except Exception as exc:
            logger.warning("history narration failed: %s", exc)
            payload["narration_error"] = str(exc)
            return payload
        # Bounded: one entry per (range, filters, scope, language, data state).
        # Re-clicking Explain on unchanged data must not re-bill the user.
        if len(self._insight_cache) >= 24:
            self._insight_cache.clear()
        self._insight_cache[key] = narration
        payload["narration"] = narration
        return payload

    @staticmethod
    def _insight_cache_key(
        trades: dict[str, Any], scope: str, lang: str, facts: dict[str, Any]
    ) -> str:
        totals = facts.get("totals") or {}
        return "|".join(
            str(part)
            for part in (
                scope,
                lang,
                trades.get("range"),
                trades.get("after"),
                trades.get("until"),
                totals.get("fills"),
                totals.get("realized"),
                totals.get("matched_sells"),
            )
        )

    def history_query(
        self,
        text: str,
        *,
        symbols: list[str] | None = None,
        lang: str | None = None,
    ) -> dict:
        with self.lock:
            code = normalize_lang(lang or self.settings.lang)
        provider = self._insight_provider()
        return parse_query(
            text, provider=provider, known_symbols=symbols or [], lang=code
        )

    # ── Approved lessons ────────────────────────────────────────────

    def list_lessons(self) -> list[dict[str, Any]]:
        return lessons_store.list_lessons()

    def save_lesson(self, payload: dict[str, Any]) -> dict[str, Any]:
        return lessons_store.add_lesson(payload)

    def set_lesson_enabled(self, lesson_id: int, enabled: bool) -> dict[str, Any]:
        return lessons_store.set_enabled(lesson_id, enabled)

    def delete_lesson(self, lesson_id: int) -> bool:
        return lessons_store.delete_lesson(lesson_id)

    def refresh_account(self) -> dict[str, Any]:
        bot = self._build_algo_bot()
        summary = bot.service.account_summary()
        with self.lock:
            self.account = summary
            self.error = None
            self._key_status_locked()
        return summary

    def refresh_quote(self, force: bool = False) -> dict[str, Any] | None:
        now = time.time()
        with self.lock:
            symbol = self.settings.symbol
            age = now - self._quote_fetched_at
            ttl = 3 if force else 15
            cache_ok = (
                self.quote is not None
                and self._quote_symbol == symbol
                and age < ttl
            )
            if cache_ok:
                return self.quote
        try:
            bot = self._build_algo_bot()
            quote = bot.service.get_mark_price(symbol)
            with self.lock:
                self.quote = quote
                self._quote_symbol = symbol
                self._quote_fetched_at = time.time()
                self.error = None
            return quote
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            return None

    def watch_quotes(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Live marks for the watchlist so rows are not frozen at signal time.

        Watchlist rows carry the price captured when the engine last ran; without
        this the desk shows a stale number between cycles.
        """
        config = self._base_config()
        wanted = [str(s).upper().strip() for s in (symbols or []) if str(s).strip()]
        if not wanted:
            wanted = [config.symbol, *config.symbols]
        if not wanted:
            return {}
        service = AlpacaService(config)
        return service.get_mark_prices(wanted)

    def run_once(self) -> dict[str, Any]:
        self._begin_poll()
        try:
            with self.lock:
                mode = self.settings.strategy_mode
            self._require_live_execution()
            if mode == "ai":
                return self._run_ai_once()
            if mode == "pair":
                return self._run_pair_once()
            if mode == "ls":
                return self._run_ls_once()
            return self._run_sma_once()
        finally:
            self._end_poll()

    def _run_pair_once(self) -> dict[str, Any]:
        bot = self._build_pair_bot()
        bundle = bot.run_once()
        primary = bundle["primary"]
        results = bundle["results"]
        summary = bot.service.account_summary()
        symbol = primary.get("symbol") or self.settings.pair_long_symbol
        try:
            quote = bot.service.get_mark_price(symbol)
        except Exception:
            quote = None
        strategy = bot.as_strategy_result(primary)
        position = float(primary.get("position") or 0)
        payload = self._store_result(
            strategy, position, summary, quote, ai_extra=primary
        )
        self._set_last_ai_results(results)
        self._record_trade_history(self._expand_leg_history(results, "pair"))
        return payload

    @staticmethod
    def _expand_leg_history(
        results: list[dict[str, Any]], engine: str
    ) -> list[dict[str, Any]]:
        """Flatten nested leg actions so History sees each submitted order."""
        history_items: list[dict[str, Any]] = []
        for item in results:
            actions = item.get("actions") or []
            if not actions:
                if item.get("order_id"):
                    history_items.append(item)
                continue
            for action in actions:
                # Live fills need order_id.
                if not action.get("order_id"):
                    continue
                history_items.append(
                    {
                        **item,
                        "symbol": action.get("symbol") or item.get("symbol"),
                        "signal": action.get("side") or item.get("signal"),
                        "order_id": action.get("order_id"),
                        "order_qty": action.get("qty"),
                        "price": action.get("price") or item.get("price"),
                        "reason": action.get("reason") or item.get("reason"),
                        "engine": engine,
                    }
                )
        return history_items

    def _cancelled_cycle(
        self, primary: dict[str, Any], history_items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Wind up a cycle that Stop cut short.

        Orders that already went out still belong in History, but the trailing
        account/quote round trips are skipped and nothing is published to the
        desk — Stop has already cleared the watchlist.
        """
        self._record_trade_history(history_items)
        return {**(primary or {}), "cancelled": True}

    def _run_ls_once(self) -> dict[str, Any]:
        bot = self._build_ls_bot()
        bundle = bot.run_once(should_stop=self._cycle_cancelled)
        primary = bundle["primary"]
        results = bundle["results"]
        if self._cycle_cancelled():
            return self._cancelled_cycle(
                primary, self._expand_leg_history(results, "ls")
            )
        summary = bot.service.account_summary()
        symbol = primary.get("symbol") or self.settings.symbol
        try:
            quote = bot.service.get_mark_price(symbol)
        except Exception:
            quote = None
        strategy = bot.as_strategy_result(primary)
        position = float(primary.get("position") or 0)
        payload = self._store_result(
            strategy, position, summary, quote, ai_extra=primary
        )
        self._set_last_ai_results(results)
        self._record_trade_history(self._expand_leg_history(results, "ls"))
        return payload

    def _run_sma_once(self) -> dict[str, Any]:
        bot = self._build_algo_bot()
        bundle = bot.run_once(should_stop=self._cycle_cancelled)
        primary = bundle["primary"]
        results = bundle["results"]
        if self._cycle_cancelled():
            return self._cancelled_cycle(primary, results)
        summary = bot.service.account_summary()
        symbol = primary.get("symbol") or self.settings.symbol
        try:
            quote = bot.service.get_mark_price(symbol)
        except Exception:
            quote = None
        strategy = bot.as_strategy_result(primary)
        position = float(primary.get("position") or 0)
        payload = self._store_result(
            strategy, position, summary, quote, ai_extra=primary
        )
        self._set_last_ai_results(results)
        self._record_trade_history(results)
        return payload

    def _run_ai_once(self) -> dict[str, Any]:
        bot = self._build_ai_bot()
        bundle = bot.run_once(should_stop=self._cycle_cancelled)
        primary = bundle["primary"]
        results = bundle["results"]
        if self._cycle_cancelled():
            return self._cancelled_cycle(primary, results)
        summary = bot.service.account_summary()
        symbol = primary.get("symbol") or self.settings.symbol
        try:
            quote = bot.service.get_mark_price(symbol)
        except Exception:
            quote = None
        strategy = bot.as_strategy_result(primary)
        position = float(primary.get("position") or 0)
        payload = self._store_result(
            strategy, position, summary, quote, ai_extra=primary
        )
        self._set_last_ai_results(results)
        self._record_trade_history(results)
        return payload

    def _store_result(
        self,
        result: StrategyResult,
        position: float,
        summary: dict[str, Any],
        quote: dict[str, Any] | None = None,
        ai_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mark = (quote or {}).get("price", result.price)
        payload = {
            "signal": result.signal.value,
            "price": mark,
            "bar_close": result.price,
            "session": (quote or {}).get("session"),
            "price_source": (quote or {}).get("source"),
            "price_asof": (quote or {}).get("asof"),
            "fast_sma": result.fast_sma,
            "slow_sma": result.slow_sma,
            "reason": result.reason,
            "position": position,
        }
        if ai_extra:
            for key in (
                "confidence",
                "provider",
                "model",
                "thesis",
                "risks",
                "thesis_en",
                "risks_en",
                "note_lang",
                "news_bias",
                "ta_bias",
                "news_count",
                "calendar_count",
                "earnings_stance",
                "earnings_result",
                "earnings_blackout",
                "position_side",
                "intent",
                # Risk-engine read-outs the signal wall renders. Without these the
                # featured symbol loses ADX/ATR/R/spread whenever the wall falls
                # back to last_result instead of the watchlist row.
                "regime",
                "adx",
                "atr_pct",
                "htf_bias",
                "spread_bps",
                "r_multiple",
                "avg_entry",
                "unrealized_pct",
                "risk_blocked",
                "managed",
                "context_summary",
                "symbol",
                "order_id",
                "order_qty",
                "stop_loss",
                "stop_loss_pct",
                "rsi",
                "bb_pct_b",
                "engine",
                "trend_bias",
            ):
                if key in ai_extra:
                    payload[key] = ai_extra[key]
        with self.lock:
            self.last_result = payload
            self.last_position = position
            self.account = summary
            if quote is not None:
                self.quote = quote
                self._quote_symbol = self.settings.symbol
                self._quote_fetched_at = time.time()
            self.error = None
        return payload

    def _record_trade_history(self, results: list[dict[str, Any]]) -> None:
        """Keep History focused on successfully submitted BUY/SELL orders."""
        trades: list[dict[str, Any]] = []
        for item in results or []:
            sig = str(item.get("signal") or "hold").lower()
            if sig not in ("buy", "sell"):
                continue
            # Only filled-intent submissions — skip position-state
            # skips, market-closed, open-order skips, etc.
            order_id = item.get("order_id")
            if not order_id:
                continue
            trades.append(item)

        with self.lock:
            now = datetime.now()
            iso = now.isoformat(timespec="seconds")
            # Every result contributes events, including the ones that never
            # became a fill — those are exactly what the execution audit is for.
            for item in results or []:
                for event in desk_events_from_result(item, iso):
                    self.desk_events.appendleft(event)
            if self.loop_running and self._active_loop_session is not None:
                session = self._active_loop_session
                session["poll_count"] = int(session.get("poll_count") or 0) + 1
                session["updated_at"] = iso
                for item in trades:
                    self._push_trade_entry_locked(item, session)
                return

            if not trades:
                return

            self._loop_session_seq += 1
            first = trades[0]
            if first.get("engine") == "manual" or first.get("mode") == "manual":
                mode = "manual"
                provider = None
                preset_id, preset_label = None, "Manual"
            else:
                mode = (
                    "ai"
                    if first.get("provider")
                    else self.settings.strategy_mode
                )
                provider = (
                    self.settings.ai_provider
                    if self.settings.strategy_mode == "ai"
                    else None
                )
                preset_id, preset_label = self._preset_meta_locked()
            session = {
                "id": self._loop_session_seq,
                "kind": "once",
                "status": "done",
                "started_ts": now.strftime("%H:%M:%S"),
                "started_at": iso,
                "stopped_at": iso,
                "duration_seconds": 0.0,
                "mode": mode,
                "preset_id": preset_id,
                "preset": preset_label,
                "symbol": first.get("symbol") or self.settings.symbol,
                "symbols": first.get("symbol") or self.settings.symbols,
                "poll_seconds": self.settings.poll_seconds,
                "provider": provider,
                "poll_count": 1,
                "error_count": 0,
                "last_signal": None,
                "last_symbol": None,
                "updated_at": iso,
                "results": [],
            }
            self.loop_sessions.appendleft(session)
            for item in trades:
                self._push_trade_entry_locked(item, session)

    def _push_trade_entry_locked(
        self, item: dict[str, Any], session: dict[str, Any]
    ) -> None:
        now = datetime.now()
        self._history_seq += 1
        signal = str(item.get("signal") or "").lower()
        symbol = str(item.get("symbol") or self.settings.symbol or "") or None
        reason = str(item.get("thesis") or item.get("reason") or "")
        price = item.get("price")
        entry = {
            "id": self._history_seq,
            "poll": self._active_poll,
            "ts": now.strftime("%H:%M:%S"),
            "iso": now.isoformat(timespec="seconds"),
            "signal": signal,
            "symbol": symbol,
            "price": price,
            "reason": reason[:160],
            "mode": (
                "manual"
                if item.get("engine") == "manual" or item.get("mode") == "manual"
                else ("ai" if item.get("provider") else "sma")
            ),
            "provider": item.get("provider"),
            "confidence": item.get("confidence"),
            "position": item.get("position"),
            "order_id": item.get("order_id"),
            "order_qty": item.get("order_qty"),
            "via": "manual"
            if item.get("engine") == "manual" or item.get("mode") == "manual"
            else ("loop" if self.loop_running else "once"),
            "kind": "signal",
            "loop_id": session["id"],
            # Entry conditions kept beside the trade so History can review the
            # decision later, not just list it.
            "context": entry_context_from_result(item),
        }
        self.result_history.appendleft(entry)
        session["results"].insert(0, entry)
        session["last_signal"] = signal
        session["last_symbol"] = symbol
        session["updated_at"] = entry["iso"]

    def _append_session_error_locked(
        self, message: str, poll: int | None = None
    ) -> None:
        if self._active_loop_session is None:
            return
        now = datetime.now()
        self._history_seq += 1
        entry = {
            "id": self._history_seq,
            "poll": poll,
            "ts": now.strftime("%H:%M:%S"),
            "iso": now.isoformat(timespec="seconds"),
            "signal": None,
            "symbol": self.settings.symbol,
            "price": None,
            "reason": message[:200],
            "mode": self.settings.strategy_mode,
            "provider": None,
            "confidence": None,
            "position": None,
            "via": "loop",
            "kind": "error",
            "loop_id": self._active_loop_session["id"],
        }
        self._active_loop_session["results"].insert(0, entry)
        self._active_loop_session["error_count"] = int(
            self._active_loop_session.get("error_count") or 0
        ) + 1
        self._active_loop_session["updated_at"] = entry["iso"]
        self.result_history.appendleft(entry)

    def _preset_meta_locked(self) -> tuple[str, str]:
        """Return (preset_id, preset_label) for the active strategy mode."""
        if self.settings.strategy_mode == "ai":
            preset = get_preset(self.settings.ai_preset)
            return preset.id, preset.label
        if self.settings.strategy_mode == "dip":
            preset = get_dip_preset(self.settings.dip_preset)
            return preset.id, preset.label
        if self.settings.strategy_mode == "pair":
            preset = get_pair_preset(self.settings.pair_preset)
            return preset.id, preset.label
        if self.settings.strategy_mode == "ls":
            s = self.settings
            label = (
                f"Regime LS EMA{s.ls_ema_fast}/{s.ls_ema_slow} "
                f"ADX≥{s.ls_adx_min:g}"
            )
            return "regime_dual", label
        preset = get_sma_preset(self.settings.sma_preset)
        return preset.id, preset.label

    def _begin_loop_session_locked(self) -> dict[str, Any]:
        now = datetime.now()
        self._loop_session_seq += 1
        preset_id, preset_label = self._preset_meta_locked()
        session = {
            "id": self._loop_session_seq,
            "kind": "loop",
            "status": "running",
            "started_ts": now.strftime("%H:%M:%S"),
            "started_at": now.isoformat(timespec="seconds"),
            "stopped_at": None,
            "duration_seconds": None,
            "mode": self.settings.strategy_mode,
            "preset_id": preset_id,
            "preset": preset_label,
            "symbol": self.settings.symbol,
            "symbols": self.settings.symbols,
            "poll_seconds": self.settings.poll_seconds,
            "provider": self.settings.ai_provider
            if self.settings.strategy_mode == "ai"
            else None,
            "poll_count": 0,
            "error_count": 0,
            "last_signal": None,
            "last_symbol": None,
            "updated_at": now.isoformat(timespec="seconds"),
            "results": [],
        }
        self._active_loop_session = session
        self.loop_sessions.appendleft(session)
        return session

    def _finish_loop_session_locked(self, duration: float | None) -> None:
        session = self._active_loop_session
        if session is None:
            return
        now = datetime.now()
        session["status"] = "stopped"
        session["stopped_at"] = now.isoformat(timespec="seconds")
        session["duration_seconds"] = (
            float(duration) if duration is not None else None
        )
        session["updated_at"] = session["stopped_at"]
        self._active_loop_session = None

    def _manual_bar_read(
        self, service: AlpacaService, symbol: str
    ) -> tuple[float | None, dict[str, Any]]:
        """One daily-bar fetch feeding both the stop distance and the rail.

        Deliberately pinned to ``_MANUAL_ATR_TIMEFRAME`` rather than the desk's
        ``bar_timeframe``: a manual ticket is held for days, but the loop may be
        scanning 1-minute bars, where ATR is cents wide. Sizing off that stop
        distance buys the whole account and then gets stopped out by noise.

        ATR and the day/52-week statistics come from the same frame because
        they are the same bars — fetching twice would double the cost of every
        keystroke for numbers that must agree with each other anyway.
        """
        now_mono = time.monotonic()
        cached = self._manual_atr_cache.get(symbol)
        if cached and cached[0] > now_mono:
            return cached[1], cached[2]
        atr: float | None = None
        stats: dict[str, Any] = {}
        try:
            bars = service.get_bars(
                symbol, limit=_MANUAL_BAR_LIMIT, timeframe=_MANUAL_ATR_TIMEFRAME
            )
            atr = atr_from_bars(bars)
            try:
                stats = daily_bar_stats(bars)
            except Exception as exc:  # stats are decoration; ATR is not
                logger.warning("manual bar stats unavailable for %s: %s", symbol, exc)
                stats = {}
        except Exception as exc:
            logger.warning("manual ATR unavailable for %s: %s", symbol, exc)
        self._manual_atr_cache[symbol] = (now_mono + _MANUAL_ATR_TTL, atr, stats)
        return atr, stats

    def _manual_atr(self, service: AlpacaService, symbol: str) -> float | None:
        """Daily ATR14 for the ticket — see :meth:`_manual_bar_read`."""
        return self._manual_bar_read(service, symbol)[0]

    def _manual_asset_info(
        self, service: AlpacaService, symbol: str
    ) -> dict[str, Any]:
        """Tradability flags, cached hard — a listing does not change intraday."""
        now_mono = time.monotonic()
        cached = self._manual_asset_cache.get(symbol)
        if cached and cached[0] > now_mono:
            return cached[1]
        try:
            info = service.get_asset_info(symbol)
        except Exception as exc:
            logger.warning("asset info unavailable for %s: %s", symbol, exc)
            info = {}
        self._manual_asset_cache[symbol] = (now_mono + _MANUAL_ASSET_TTL, info)
        return info

    def _manual_earnings(self, symbol: str) -> dict[str, Any]:
        """Whether a print is near enough to matter, trimmed to what the rail shows.

        The AI desk reads the whole snapshot; a ticket only needs the date, how
        long until it, and whether the desk would call this a blackout. Scraping
        is slow, so a failure returns an empty dict and the rail shows a dash
        rather than blocking the quote behind it.
        """
        now_mono = time.monotonic()
        cached = self._manual_earnings_cache.get(symbol)
        if cached and cached[0] > now_mono:
            return cached[1]
        trimmed: dict[str, Any] = {}
        try:
            snapshot = fetch_earnings(symbol)
        except Exception as exc:
            logger.warning("earnings unavailable for %s: %s", symbol, exc)
            snapshot = None
        if snapshot and snapshot.get("ok"):
            upcoming = snapshot.get("next") or {}
            trimmed = {
                "blackout": bool(snapshot.get("blackout")),
                "stance": snapshot.get("stance"),
                "last_result": snapshot.get("last_result"),
                "next_date": upcoming.get("date"),
                "next_when_et": upcoming.get("when_et"),
                "next_session": upcoming.get("session"),
                "hours_until": upcoming.get("hours_until"),
            }
        self._manual_earnings_cache[symbol] = (now_mono + _MANUAL_EARNINGS_TTL, trimmed)
        return trimmed

    def _manual_portfolio_heat(self, service: AlpacaService) -> dict[str, Any]:
        """Total open risk across every position, cached across symbol edits."""
        now_mono = time.monotonic()
        if self._manual_heat_cache and self._manual_heat_cache[0] > now_mono:
            return self._manual_heat_cache[1]
        try:
            positions = service.get_all_positions()
        except Exception as exc:
            logger.warning("could not read positions for portfolio heat: %s", exc)
            positions = []
        try:
            orders = service.get_open_orders_summary()
        except Exception:
            orders = {}
        # Heat feeds a risk gate, so a broker read that came back as something
        # other than the documented shape is treated as "unknown", never
        # coerced into numbers that would silently under-report exposure.
        if not isinstance(positions, list):
            positions = []
        if not isinstance(orders, dict):
            orders = {}
        with self.lock:
            equity = float((self.account or {}).get("equity") or 0)
        heat = portfolio_heat(positions, orders, equity)
        # The rail lists names and totals; the per-position rows are only used
        # to compute them and would triple the payload on a wide book.
        heat.pop("rows", None)
        self._manual_heat_cache = (now_mono + _MANUAL_HEAT_TTL, heat)
        return heat

    def _invalidate_manual_heat(self) -> None:
        """Anything that changes the book makes the cached heat a lie."""
        self._manual_heat_cache = None

    def manual_ticket_context(self, symbol: str) -> dict[str, Any]:
        """Quote, session, position, ATR, and account for a Manual Order symbol.

        ``atr`` and ``stop_loss_pct`` ship with the quote so the browser can
        reproduce ``Config.ai_stop_distance`` / ``ai_qty_for_risk`` exactly — a
        preview sized off a guessed stop is worse than no preview at all.
        """
        symbol = str(symbol or "").upper().strip()
        if not symbol:
            raise ValueError("Symbol is required")
        config = self._base_config()
        service = AlpacaService(config)
        try:
            mark = service.get_mark_price(symbol)
        except Exception as exc:
            raise ValueError(f"Could not quote {symbol}: {exc}") from exc
        session_info = service.market_session()
        atr, bar_stats = self._manual_bar_read(service, symbol)
        # `get_position_qty` stays the authority on size — the detail read is
        # for the avg entry and open P&L the rail shows beside it, and a failure
        # there must not change what the ticket believes it is holding.
        try:
            position = float(service.get_position_qty(symbol))
        except Exception:
            position = 0.0
        try:
            position_detail = service.get_position_detail(symbol)
        except Exception:
            position_detail = None
        if not isinstance(position_detail, dict):
            position_detail = {}
        position_detail = {**position_detail, "qty": position}
        try:
            summary = service.account_summary()
        except Exception:
            summary = None
        try:
            orders_by_symbol = service.get_open_orders_summary()
        except Exception:
            orders_by_symbol = {}
        open_orders = orders_by_symbol.get(symbol, [])
        with self.lock:
            if summary is not None:
                self.account = summary
            # Keep Auto Trade quote cache in sync when symbols match.
            self.quote = mark
            self._quote_symbol = symbol
            self._quote_fetched_at = time.time()
            self.error = None
            account = self.account
            desk_stop_pct = float(self.settings.stop_loss_pct or 0)

        heat = self._manual_portfolio_heat(service)
        asset = self._manual_asset_info(service, symbol)
        earnings = self._manual_earnings(symbol)
        # The resting stop is what the "move to breakeven" control acts on, so
        # it is published as its own field rather than left for the browser to
        # dig back out of the open-orders list.
        exit_side = "sell" if position > 0 else "buy" if position < 0 else None
        current_stop = next(
            (
                float(o["stop_price"])
                for o in open_orders
                if o.get("is_stop") and o.get("stop_price")
                and exit_side is not None
                and str(o.get("side") or exit_side).lower() == exit_side
            ),
            None,
        )
        breaches = self._manual_ticket_breaches(
            config,
            service,
            symbol,
            mark=mark,
            heat=heat,
            holds_position=position != 0,
            account=account,
        )
        return {
            "symbol": symbol,
            "quote": mark,
            "session": mark.get("session") or session_info.get("session"),
            "is_open": bool(session_info.get("is_open")),
            "position": position,
            "position_detail": position_detail,
            "current_stop": current_stop,
            "atr": atr,
            "stats": bar_stats or {},
            "asset": asset,
            "earnings": earnings,
            "heat": heat,
            "breaches": breaches,
            # Flat-percent fallback the risk engine uses when ATR is unusable.
            "stop_loss_pct": desk_stop_pct,
            "stop_limit_offset_pct": float(self.settings.stop_limit_offset_pct or 0),
            "open_orders": open_orders,
            "buying_power": (account or {}).get("buying_power"),
            "equity": (account or {}).get("equity"),
            "day_pl_pct": (account or {}).get("day_pl_pct"),
            "account": account,
        }

    def _manual_ticket_breaches(
        self,
        config: Config,
        service: AlpacaService,
        symbol: str,
        *,
        mark: dict[str, Any] | None,
        heat: dict[str, Any] | None,
        holds_position: bool,
        account: dict[str, Any] | None,
        ticket_risk: float | None = None,
    ) -> list[dict[str, Any]]:
        """Desk limits a new entry in this symbol would cross, as advisory rows.

        Read on every context refresh so the page can warn before the user has
        typed a size, and again at submit time with the real ticket risk. Broker
        lookups here are best-effort: a guard that cannot be evaluated must not
        take the whole ticket down with it.
        """
        try:
            activity = service.recent_activity(symbol)
        except Exception:
            activity = None
        if not isinstance(activity, dict):
            activity = {}
        positions = int((heat or {}).get("positions") or 0)
        return manual_entry_breaches(
            config,
            mark=mark,
            open_positions=positions,
            day_pl_pct=(account or {}).get("day_pl_pct"),
            activity=activity,
            adding_to_position=holds_position,
            heat=heat,
            ticket_risk=ticket_risk,
            equity=(account or {}).get("equity"),
        )

    def place_manual_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str = "market",
        qty: float | None = None,
        size_mode: str | None = None,
        notional: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trail_percent: float | None = None,
        trail_price: float | None = None,
        time_in_force: str = "day",
        extended_hours: bool = False,
        stop_loss_pct: float | None = None,
        ai_risk_pct: float | None = None,
        ai_atr_stop_mult: float | None = None,
        take_profit_r: float | None = None,
        stop_limit_offset_pct: float | None = None,
        stop_limit_price: float | None = None,
        preview: bool = False,
        confirm_adjusted_qty: bool = False,
        override_breaches: bool = False,
        ticket_id: str | None = None,
        reinvest: dict[str, Any] | None = None,
        followon: dict[str, Any] | None = None,
        dip_hunt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Place a one-off ticket of any supported type, sized by the risk engine.

        ``side`` is one of four *actions*, not two broker sides. Alpaca only
        knows buy and sell, which makes "sell" ambiguous the moment shorting
        exists — the same button would close a long or open a short depending
        on state the user cannot see from the button. Spelling the intent out
        means a mis-click is rejected instead of silently reversing a position:

        ``buy``   open or add to a long        ``sell``  close part of a long
        ``short`` open or add to a short       ``cover`` close part of a short
        """
        if self.loop_running:
            raise ValueError("Stop the strategy loop before placing a manual order.")

        symbol = str(symbol or "").upper().strip()
        if not symbol:
            raise ValueError("Symbol is required")

        side_raw = str(side or "").strip().lower()
        if side_raw not in _MANUAL_ACTIONS:
            raise ValueError(
                "side must be one of: " + ", ".join(sorted(_MANUAL_ACTIONS))
            )
        order_side = (
            OrderSide.BUY if side_raw in {"buy", "cover"} else OrderSide.SELL
        )
        is_entry = side_raw in {"buy", "short"}
        is_short_side = side_raw in {"short", "cover"}

        # Validated up front: a bad buy-back price should stop the ticket
        # before the sell goes out, not after it has already filled.
        reinvest_plan = self.normalize_reinvest_request(reinvest, side=side_raw)
        followon_plan = self.normalize_followon_request(
            followon, side=side_raw, close_symbol=symbol
        )
        dip_hunt_plan = self.normalize_dip_hunt_request(dip_hunt, side=side_raw)
        if reinvest_plan is not None and followon_plan is not None:
            raise ValueError(
                "Choose either a buy-back or a next ticket after this close, "
                "not both."
            )

        otype = str(order_type or "market").strip().lower()
        if otype not in MANUAL_ORDER_TYPES:
            raise ValueError(
                "order_type must be one of: " + ", ".join(sorted(MANUAL_ORDER_TYPES))
            )
        tif = str(time_in_force or "day").strip().lower()
        if tif not in MANUAL_TIME_IN_FORCE:
            raise ValueError(
                "time_in_force must be one of: "
                + ", ".join(sorted(MANUAL_TIME_IN_FORCE))
            )
        extended = bool(extended_hours)
        if extended and (otype != "limit" or tif not in {"day", "gtc"}):
            raise ValueError(
                "Extended-hours orders must be limit orders with DAY or GTC "
                "time in force."
            )

        with self.lock:
            desk_stop = float(self.settings.stop_loss_pct or 0)
            desk_risk = float(self.settings.ai_risk_pct or 0)
            desk_atr = float(self.settings.ai_atr_stop_mult or 0)
            desk_tp_r = float(self.settings.ai_take_profit_r or 0)
            desk_stop_limit = float(self.settings.stop_limit_offset_pct or 0)

        # 0 turns the target off and leaves a stop-only OTO entry.
        tp_r = desk_tp_r if take_profit_r is None else max(0.0, min(20.0, float(take_profit_r)))

        if stop_loss_pct is None:
            stop_pct = desk_stop
        else:
            stop_pct = max(0.0, min(50.0, float(stop_loss_pct)))

        if stop_limit_offset_pct is None:
            stop_limit_offset = desk_stop_limit
        else:
            stop_limit_offset = max(0.0, min(50.0, float(stop_limit_offset_pct)))

        exit_limit: float | None = None
        if stop_limit_price is not None and float(stop_limit_price) > 0:
            exit_limit = normalize_stock_order_price(
                stop_limit_price, field="stop_limit_price"
            )

        risk_pct = (
            desk_risk if ai_risk_pct is None else max(0.0, min(10.0, float(ai_risk_pct)))
        )
        atr_mult = (
            desk_atr
            if ai_atr_stop_mult is None
            else max(0.0, min(10.0, float(ai_atr_stop_mult)))
        )

        config = self._base_config()
        config = config.override(
            stop_loss_pct=stop_pct,
            ai_risk_pct=risk_pct,
            ai_atr_stop_mult=atr_mult,
            stop_limit_offset_pct=stop_limit_offset,
        )
        service = AlpacaService(config)

        try:
            mark = service.get_mark_price(symbol)
            price = float(mark["price"])
        except Exception as exc:
            raise ValueError(f"Could not quote {symbol}: {exc}") from exc

        atr = None
        stop_distance = 0.0
        if atr_mult > 0 or risk_pct > 0:
            # Same ATR the preview was sized from, so the ticket the user
            # confirmed is the ticket that goes out.
            atr = self._manual_atr(service, symbol)
            stop_distance = stop_distance_for(config, price, atr)

        # ATR path owns the stop when a distance is available. Derived percents
        # can exceed the 50% user-facing cap on flat stop_loss_pct (volatile /
        # cheap names). ``Config.override`` would clamp that and recreate the
        # broker client; keep the exact distance on the existing service.
        if stop_distance > 0 and price > 0:
            stop_pct = (stop_distance / price) * 100.0
            config = replace(config, stop_loss_pct=stop_pct)
            service.config = config

        if is_entry and stop_pct > 0:
            if otype not in _ATTACHABLE_ENTRY_TYPES:
                raise ValueError(
                    "Protected entries must use Market or Limit. Alpaca cannot attach "
                    "an OTO/bracket stop to a stop, stop-limit, or trailing-stop entry."
                )
            if tif not in {"day", "gtc"}:
                raise ValueError(
                    "Protected OTO/bracket entries only support DAY or GTC time in force."
                )
            if extended:
                raise ValueError(
                    "Protective OTO/bracket exits cannot execute in extended hours. "
                    "Turn off extended-hours fills to queue the protected entry for RTH."
                )

        mode = str(size_mode or config.size_mode or "qty").strip().lower()
        if mode not in {"qty", "notional", "risk"}:
            mode = "qty"
        estimated_shares: float | None = None
        equity = 0.0
        try:
            summary = service.account_summary()
            equity = float(summary.get("equity") or 0)
        except Exception:
            summary = None

        if mode == "risk":
            if risk_pct <= 0:
                raise ValueError("Risk sizing needs Risk per trade % greater than 0")
            if stop_distance <= 0:
                raise ValueError(
                    "Risk sizing needs a stop distance — set Stop = ATR × > 0 "
                    "(with enough bars) or a flat stop loss %"
                )
            order_qty = risk_qty_for(config, price, stop_distance, equity)
            if order_qty is None or order_qty <= 0:
                raise ValueError("Could not size from risk — check equity and stop")
            estimated_shares = float(order_qty)
        elif mode == "notional":
            dollars = float(notional if notional is not None else config.trade_notional)
            if dollars <= 0:
                raise ValueError("Dollar amount must be greater than 0")
            if price <= 0:
                raise ValueError("Need a positive mark price to size by dollars")
            order_qty = dollars / price
            estimated_shares = order_qty
        else:
            if qty is None:
                order_qty = float(config.trade_qty)
            else:
                order_qty = float(qty)
            if order_qty <= 0:
                raise ValueError("Shares / qty must be greater than 0")

        requested_qty = float(order_qty)
        session_info = service.market_session()
        qty_truncated = False

        # Alpaca never borrows a fraction of a share, so a short is whole-share
        # in every session — not only outside regular hours.
        qty_whole_for_short = False
        if side_raw == "short" and order_qty > 0 and order_qty != float(int(order_qty)):
            whole = float(int(order_qty))
            if whole < 1:
                raise ValueError(
                    f"This ticket sizes to {order_qty:.2f} shares of {symbol}, and "
                    "Alpaca does not short fractional shares. Raise Risk per trade % "
                    "or lower Stop = ATR ×."
                )
            order_qty = whole
            qty_whole_for_short = True

        # A protective stop rides along as an OTO leg, and Alpaca refuses
        # fractional quantities on anything but a simple order ("fractional
        # orders must be simple orders"). Size the ticket in whole shares here,
        # not at submit time, so the preview and the confirm dialog show the qty
        # that actually goes out.
        qty_whole_for_stop = False
        attaches_stop = is_entry and stop_pct > 0 and otype in _ATTACHABLE_ENTRY_TYPES
        if attaches_stop and order_qty > 0:
            whole_qty, can_attach_stop = whole_qty_for_attached_stop(order_qty)
            if not can_attach_stop:
                raise ValueError(
                    f"This ticket sizes to {order_qty:.2f} shares of {symbol}, and a "
                    "protective stop needs at least 1 whole share. Raise Risk per "
                    "trade % or lower Stop = ATR ×."
                )
            if whole_qty != order_qty:
                order_qty = whole_qty
                qty_whole_for_stop = True

        if dip_hunt_plan is not None and stop_pct <= 0:
            raise ValueError(
                "Dip hunt after stop-loss needs a protective stop on the buy."
            )

        qty_clamped = False
        position = float(service.get_position_qty(symbol))
        held = abs(position)
        asset = self._manual_asset_info(service, symbol)
        if isinstance(asset, dict) and asset and asset.get("tradable") is False:
            raise ValueError(
                f"{symbol} is not tradable at Alpaca — this ticket would be rejected."
            )
        # Each action states which side of the book it belongs on, so the desk
        # can refuse a mis-click instead of reversing a position by accident.
        if side_raw == "sell":
            if position < 0:
                raise ValueError(
                    f"{symbol} is short {held:g} shares — use Cover to close a "
                    "short, or Short to add to it."
                )
            if position == 0:
                raise ValueError(f"No long position in {symbol} to sell")
        elif side_raw == "cover":
            if position > 0:
                raise ValueError(
                    f"{symbol} is long {held:g} shares — use Sell to close a long."
                )
            if position == 0:
                raise ValueError(f"No short position in {symbol} to cover")
        elif side_raw == "buy":
            if position < 0:
                raise ValueError(
                    f"{symbol} is short {held:g} shares — use Cover to buy those "
                    "back, so the ticket cannot flip you long by accident."
                )
        elif side_raw == "short":
            if position > 0:
                raise ValueError(
                    f"{symbol} is long {held:g} shares — sell the long before "
                    "opening a short in the same symbol."
                )

        opens_short = side_raw == "short" or (
            side_raw == "sell"
            and followon_plan is not None
            and followon_plan["kind"] == "reverse"
        )
        # An empty dict means the lookup failed, not that the asset is
        # unshortable — refusing on a failed lookup would be a worse error than
        # letting the broker give its own.
        if opens_short and asset and not asset.get("shortable"):
            raise ValueError(
                f"{symbol} is not shortable at Alpaca — the borrow is not "
                "available, so this ticket would be rejected."
            )

        if side_raw in _MANUAL_EXIT_ACTIONS and order_qty > held + 1e-9:
            order_qty = held
            qty_clamped = True

        if followon_plan is not None and followon_plan["kind"] == "reverse":
            if order_qty + 1e-9 < held:
                action = "sell" if side_raw == "sell" else "cover"
                raise ValueError(
                    "A reverse next ticket needs the whole position closed — "
                    f"{action} all of {symbol}, or the desk would still be "
                    f"{'long' if side_raw == 'sell' else 'short'} when the "
                    "opposite order fired."
                )

        fractional_qty = order_qty != float(int(order_qty))
        if fractional_qty and isinstance(asset, dict) and asset.get("fractionable") is False:
            raise ValueError(
                f"{symbol} does not support fractional shares at Alpaca."
            )
        if fractional_qty and tif != "day":
            raise ValueError("Fractional stock orders must use DAY time in force.")

        limit = None
        if otype in {"limit", "stop_limit"}:
            if limit_price is None or float(limit_price) <= 0:
                raise ValueError("Limit price is required for limit orders")
            limit = normalize_stock_order_price(limit_price, field="limit_price")

        trigger = None
        if otype in {"stop", "stop_limit"}:
            if stop_price is None or float(stop_price) <= 0:
                raise ValueError(
                    "Stop price is required for stop and stop-limit orders"
                )
            trigger = normalize_stock_order_price(stop_price, field="stop_price")

        trail_pct_val = float(trail_percent or 0) or None
        trail_amt_val = float(trail_price or 0) or None
        if otype == "trailing_stop":
            if trail_pct_val and trail_amt_val:
                raise ValueError(
                    "A trailing stop takes either a trail percent or a trail "
                    "amount, not both."
                )
            if not trail_pct_val and not trail_amt_val:
                raise ValueError(
                    "A trailing stop needs a trail percent or a trail amount "
                    "greater than 0"
                )
            if trail_pct_val and trail_pct_val > 50:
                raise ValueError("Trail percent must be between 0 and 50")

        stop_preview = None
        stop_limit_preview = None
        take_profit_price = None
        if is_entry and stop_pct > 0:
            # The entry reference is whichever price this ticket actually gets
            # filled at: the limit, or the stop trigger on a conditional entry.
            entry_ref = limit if limit is not None else (trigger or price)
            stop_preview = service.stop_price_for_entry(
                entry_ref, pct=stop_pct, short=is_short_side
            )
            if stop_preview is not None:
                if exit_limit is not None:
                    stop_limit_preview = normalize_stop_exit_limit(
                        stop_preview,
                        exit_limit,
                        short=is_short_side,
                        clamp=False,
                    )
                else:
                    stop_limit_preview = limit_price_for_stop(
                        stop_preview, stop_limit_offset, short=is_short_side
                    )
            # The target is priced in R — the same stop distance the size was
            # derived from — so reward and risk stay tied to one number.
            if tp_r > 0 and stop_preview is not None:
                risk_per_share = (
                    stop_preview - entry_ref if is_short_side else entry_ref - stop_preview
                )
                if risk_per_share > 0:
                    take_profit_price = normalize_stock_order_price(
                        entry_ref - risk_per_share * tp_r
                        if is_short_side
                        else entry_ref + risk_per_share * tp_r,
                        field="take_profit_price",
                    )

        warnings: list[str] = []
        if qty_whole_for_short:
            warnings.append(
                "Whole shares — Alpaca does not short fractions: "
                f"{requested_qty:g} → {order_qty:g}"
            )
        if qty_whole_for_stop:
            warnings.append(
                "Whole shares so the protective stop can attach: "
                f"{requested_qty:g} → {order_qty:g}"
            )
        if qty_truncated:
            warnings.append(
                f"Whole-share truncation outside RTH: {requested_qty:g} → {order_qty:g} shares"
            )
        if qty_clamped:
            warnings.append(
                f"{side_raw.capitalize()} qty clamped to position: "
                f"{requested_qty:g} → {order_qty:g} (held {held:g})"
            )
        # Risk in dollars is what the heat guard measures the book against, and
        # it is the one number the preview and the gate must agree on.
        ticket_risk = None
        if stop_preview is not None and order_qty > 0:
            entry_ref = limit if limit is not None else (trigger or price)
            per_share = (
                stop_preview - entry_ref if is_short_side else entry_ref - stop_preview
            )
            if per_share > 0:
                ticket_risk = round(per_share * order_qty, 2)

        breaches: list[dict[str, Any]] = []
        if is_entry:
            with self.lock:
                account_now = self.account
            breaches = self._manual_ticket_breaches(
                config,
                service,
                symbol,
                mark=mark,
                heat=self._manual_portfolio_heat(service),
                holds_position=position != 0,
                account=account_now or summary,
                ticket_risk=ticket_risk,
            )

        payload: dict[str, Any] = {
            "symbol": symbol,
            "signal": side_raw,
            "side": side_raw,
            "broker_side": order_side.value,
            "order_type": otype,
            "time_in_force": tif,
            "extended_hours": extended,
            "price": price,
            "limit_price": limit,
            "stop_price": trigger,
            "trail_percent": trail_pct_val,
            "trail_price": trail_amt_val,
            "session": mark.get("session") or session_info.get("session"),
            "is_open": bool(session_info.get("is_open")),
            "price_source": mark.get("source"),
            "requested_qty": requested_qty,
            "order_qty": order_qty,
            "estimated_shares": estimated_shares,
            "qty_truncated": qty_truncated,
            "qty_clamped": qty_clamped,
            "qty_whole_for_stop": qty_whole_for_stop,
            "qty_whole_for_short": qty_whole_for_short,
            "warnings": warnings,
            "breaches": breaches,
            "size_mode": mode,
            "stop_loss_pct": stop_pct,
            "stop_distance": stop_distance or None,
            "ticket_risk": ticket_risk,
            "ai_risk_pct": risk_pct,
            "ai_atr_stop_mult": atr_mult,
            "stop_preview": stop_preview,
            "stop_limit_preview": stop_limit_preview,
            "stop_limit_offset_pct": stop_limit_offset,
            "stop_limit_price": exit_limit,
            "take_profit_price": take_profit_price,
            "take_profit_r": tp_r,
            "position": position,
            "engine": "manual",
            "mode": "manual",
            "preview": bool(preview),
            "needs_confirm": False,
            "reason": (
                f"Manual {otype} {side_raw} {tif.upper()}"
                + (f" @ ${limit:.2f}" if limit is not None else "")
                + (f" stop ${trigger:.2f}" if trigger is not None else "")
                + (f" trail {trail_pct_val:g}%" if trail_pct_val else "")
                + (f" trail ${trail_amt_val:.2f}" if trail_amt_val else "")
                + (f" · SL {stop_pct:.2f}%" if stop_pct > 0 and is_entry else "")
                + (f" · risk {risk_pct:g}%" if mode == "risk" else "")
            ),
        }

        if reinvest_plan is not None:
            # "Match" is only an estimate until the sell reports its fill — the
            # plan itself re-reads the filled qty before it buys anything back.
            planned_qty = (
                float(reinvest_plan["qty"])
                if reinvest_plan["qty_mode"] == "custom"
                else order_qty
            )
            payload["reinvest"] = {
                **reinvest_plan,
                "planned_qty": planned_qty,
                "estimated_cost": round(planned_qty * reinvest_plan["limit_price"], 2),
                "status": "preview",
            }
            payload["reason"] += (
                f" | re-invest {planned_qty:g} @ ${reinvest_plan['limit_price']:.2f}"
            )

        if followon_plan is not None:
            planned_qty = (
                float(followon_plan["qty"])
                if followon_plan["qty_mode"] == "custom"
                else order_qty
            )
            next_symbol = str(followon_plan["target_symbol"])
            next_side = str(followon_plan["next_side"])
            next_type = str(followon_plan.get("order_type") or "limit")
            next_limit = followon_plan.get("limit_price")
            estimated_cost = (
                round(planned_qty * float(next_limit), 2)
                if next_type == "limit" and next_limit
                else None
            )
            payload["followon"] = {
                **followon_plan,
                "planned_qty": planned_qty,
                "estimated_cost": estimated_cost,
                "status": "preview",
            }
            price_bit = (
                "at market"
                if next_type == "market"
                else f"@ ${float(next_limit):.2f}"
            )
            payload["reason"] += (
                f" | next {next_side} {planned_qty:g} {next_symbol} {price_bit}"
            )

        if dip_hunt_plan is not None:
            payload["dip_hunt"] = {
                **dip_hunt_plan,
                "qty": order_qty,
                "stop_loss_pct": stop_pct,
                "take_profit_r": tp_r,
                "status": "preview",
            }
            payload["reason"] += (
                f" | dip hunt {dip_hunt_plan['wait_minutes']:g}m / "
                f"{dip_hunt_plan['dip_pct']:g}% after stop-out"
            )

        needs_qty_confirm = (qty_truncated or qty_clamped) and not confirm_adjusted_qty
        if needs_qty_confirm and not preview:
            payload["needs_confirm"] = True
            payload["confirm_kind"] = "qty"
            payload["reason"] += " | qty adjustment needs confirm"
            return payload

        # A desk limit does not refuse a hand-typed ticket the way it refuses
        # the AI — the person can see something the rules cannot. It does
        # refuse to be silent: the ticket comes back unsent with the breaches
        # attached, and only goes out once the user has said yes to them.
        if breaches and not override_breaches and not preview:
            payload["needs_confirm"] = True
            payload["confirm_kind"] = "breach"
            payload["reason"] += " | desk risk limits need confirm"
            return payload
        if breaches and override_breaches:
            payload["overridden"] = [b["code"] for b in breaches]
            payload["reason"] += " | limits overridden: " + ", ".join(
                payload["overridden"]
            )

        if preview:
            with self.lock:
                self.last_result = {
                    **payload,
                    "thesis": payload["reason"] + " | preview (not submitted)",
                }
                self.error = None
            return payload

        self._require_live_execution()

        ticket_key = self._manual_ticket_key(payload, ticket_id)
        duplicate = self._claim_manual_ticket(ticket_key, payload)
        if duplicate is not None:
            return duplicate

        # Exiting cancels resting protection first: Alpaca counts a stop's
        # shares as unavailable, so the exit would be rejected for size while
        # the very shares it wants to sell sit reserved behind the stop.
        cancelled_stops = 0
        if side_raw in _MANUAL_EXIT_ACTIONS:
            cancelled = service.cancel_open_stop_orders(symbol)
            cancelled_stops = cancelled if isinstance(cancelled, int) else 0

        try:
            submitted, oto_stop = service.submit_manual_order(
                symbol,
                order_qty,
                order_side,
                order_type=otype,
                limit_price=limit,
                stop_price=trigger,
                trail_percent=trail_pct_val,
                trail_price=trail_amt_val,
                time_in_force=tif,
                extended_hours=extended,
                stop_loss_pct=stop_pct if is_entry else 0.0,
                stop_limit_offset_pct=stop_limit_offset if is_entry else 0.0,
                stop_limit_price=exit_limit if is_entry else None,
                take_profit_price=take_profit_price,
                short_entry=side_raw == "short",
                client_order_id=ticket_id,
            )
        except Exception:
            # The order never went out, so the fingerprint must not block the
            # user's corrected retry thirty seconds from now.
            self._release_manual_ticket(ticket_key)
            # An exit that cancelled its stop and then failed to submit has left
            # the position naked — put the protection back before re-raising.
            if side_raw in _MANUAL_EXIT_ACTIONS and cancelled_stops > 0:
                self._rearm_stop_after_exit(service, symbol, stop_pct, payload)
            raise
        payload["order_id"] = str(submitted.id)
        payload["order_qty"] = float(getattr(submitted, "qty", None) or order_qty)
        payload["submitted_type"] = str(getattr(submitted, "type", otype))

        # The sell is live, so the buy-back can be armed against a real order id.
        if reinvest_plan is not None:
            armed = self._register_reinvest_plan(
                symbol=symbol,
                sell_order_id=payload["order_id"],
                sell_qty=payload["order_qty"],
                sell_limit_price=limit,
                plan=reinvest_plan,
            )
            payload["reinvest"] = {**payload.get("reinvest", {}), **armed}
            payload["reason"] += f" | re-invest armed ({armed['id']})"

        if followon_plan is not None:
            armed_next = self._register_followon_plan(
                symbol=symbol,
                close_side=side_raw,
                close_order_id=payload["order_id"],
                close_qty=payload["order_qty"],
                close_limit_price=limit,
                plan=followon_plan,
            )
            payload["followon"] = {**payload.get("followon", {}), **armed_next}
            payload["reason"] += f" | next ticket armed ({armed_next['id']})"

        stop_info = oto_stop
        if is_entry and stop_pct > 0 and stop_info is None:
            try:
                armed = service.ensure_stop_loss(symbol, pct=stop_pct)
                if armed:
                    stop_info = armed
                    sign = "+" if armed.get("side") == "buy" else "-"
                    payload["reason"] += (
                        f" | stop @{armed['stop_price']:.2f} ({sign}{armed['pct']:.2f}%)"
                    )
            except Exception as exc:
                payload["reason"] += f" | stop arm failed: {exc}"
        elif stop_info is not None:
            payload["reason"] += (
                f" | OTO stop @{stop_info['stop_price']:.2f} "
                f"(-{stop_info['pct']:.2f}%)"
            )

        if stop_info is not None:
            payload["stop_loss"] = stop_info
            legs = service.exit_leg_ids(submitted)
            if legs.get("stop_order_id") and not stop_info.get("id"):
                stop_info["id"] = legs["stop_order_id"]
            if legs.get("take_profit_order_id"):
                stop_info["take_profit_order_id"] = legs["take_profit_order_id"]

        if dip_hunt_plan is not None:
            armed_hunt = self._register_dip_hunt_plan(
                symbol=symbol,
                buy_order_id=payload["order_id"],
                qty=payload["order_qty"],
                stop_loss_pct=stop_pct,
                take_profit_r=tp_r,
                stop_order_id=(stop_info or {}).get("id"),
                take_profit_order_id=(stop_info or {}).get("take_profit_order_id"),
                plan=dip_hunt_plan,
            )
            payload["dip_hunt"] = {**payload.get("dip_hunt", {}), **armed_hunt}
            payload["reason"] += f" | dip hunt armed ({armed_hunt['id']})"

        position_after = service.get_position_qty(symbol)
        payload["position"] = position_after

        # A partial exit cancelled the stop that covered the *whole* position
        # and then sold only part of it. Put that protection back over the
        # leftover shares — but only if a stop was actually cancelled. A sell
        # that never had a stop must not invent a GTC sell the user did not
        # place.
        if side_raw in _MANUAL_EXIT_ACTIONS and cancelled_stops > 0:
            remaining = max(0.0, abs(position) - float(payload["order_qty"]))
            if remaining > 1e-9 and abs(position_after) > 1e-9:
                rearmed = self._rearm_stop_after_exit(
                    service, symbol, stop_pct, payload, qty=remaining
                )
                if rearmed is not None:
                    payload["stop_loss"] = rearmed
                    payload["stop_rearmed"] = True

        try:
            summary = service.account_summary()
        except Exception:
            summary = None

        # Filling, exiting, or arming a stop all change what the book is
        # risking, so the next ticket must not read a stale total.
        self._invalidate_manual_heat()

        with self.lock:
            self.last_result = payload
            self.last_position = position_after
            if summary is not None:
                self.account = summary
            self.quote = mark
            self._quote_symbol = symbol
            self._quote_fetched_at = time.time()
            self.error = None

        self._remember_manual_ticket(ticket_key, payload)
        self._record_trade_history([payload])
        return payload

    def _rearm_stop_after_exit(
        self,
        service: AlpacaService,
        symbol: str,
        stop_pct: float,
        payload: dict[str, Any],
        *,
        qty: float | None = None,
    ) -> dict[str, Any] | None:
        """Put protection back over whatever the exit left behind.

        Called only after this ticket cancelled a resting stop: on the happy
        path after a partial exit, and again if the exit itself failed to
        submit. ``qty`` is the remainder the exit did not claim; ``None``
        means the whole position (the failed-submit case).

        Falls back to the desk's flat stop percentage when the ticket carried
        none, because the stop we cancelled has to come back. A failure is
        recorded in the reason line rather than raised: the exit itself
        already succeeded and must still be reported to the user.
        """
        pct = float(stop_pct or 0)
        if pct <= 0:
            with self.lock:
                pct = float(self.settings.stop_loss_pct or 0)
        if pct <= 0:
            payload["reason"] += (
                " | remainder left without a stop: no stop % configured"
            )
            payload.setdefault("warnings", []).append(
                "The shares left over are not protected — no stop percentage is "
                "set on Auto Trade, so the desk had nothing to re-arm with."
            )
            return None
        try:
            armed = service.ensure_stop_loss(symbol, pct=pct, qty=qty)
        except Exception as exc:
            payload["reason"] += f" | stop re-arm failed: {exc}"
            payload.setdefault("warnings", []).append(
                f"The remaining shares could not be re-protected: {exc}"
            )
            return None
        if armed:
            payload["reason"] += (
                f" | stop re-armed on the remainder @{armed['stop_price']:.2f}"
            )
            payload.setdefault("warnings", []).append(
                f"Stop re-armed on the {armed['qty']:g} shares left at "
                f"${armed['stop_price']:.2f}."
            )
        return armed

    # ------------------------------------------------------------------
    # Duplicate suppression
    #
    # A slow broker response invites a second click, and a network retry can
    # replay the same POST. Neither should become a second position. The desk
    # fingerprints the terms of a ticket as it goes out and refuses an
    # identical one for `_MANUAL_DUPLICATE_WINDOW` seconds — long enough to
    # cover a retry, short enough that deliberately scaling into a position a
    # minute later still works.
    # ------------------------------------------------------------------

    @staticmethod
    def _manual_fingerprint(payload: dict[str, Any]) -> str:
        reinvest = payload.get("reinvest")
        reinvest_terms = (
            tuple(sorted(reinvest.items())) if isinstance(reinvest, dict) else ()
        )
        followon = payload.get("followon")
        followon_terms = (
            tuple(sorted(followon.items())) if isinstance(followon, dict) else ()
        )
        dip_hunt = payload.get("dip_hunt")
        dip_hunt_terms = (
            tuple(sorted(dip_hunt.items())) if isinstance(dip_hunt, dict) else ()
        )
        parts = [
            str(payload.get("symbol")),
            str(payload.get("side")),
            str(payload.get("order_type")),
            str(payload.get("time_in_force")),
            str(bool(payload.get("extended_hours"))),
            f"{float(payload.get('order_qty') or 0):.6f}",
            f"{float(payload.get('limit_price') or 0):.2f}",
            f"{float(payload.get('stop_price') or 0):.2f}",
            f"{float(payload.get('trail_percent') or 0):.6f}",
            f"{float(payload.get('trail_price') or 0):.6f}",
            f"{float(payload.get('stop_loss_pct') or 0):.6f}",
            f"{float(payload.get('stop_limit_offset_pct') or 0):.6f}",
            f"{float(payload.get('stop_limit_price') or 0):.4f}",
            f"{float(payload.get('take_profit_price') or 0):.2f}",
            repr(reinvest_terms),
            repr(followon_terms),
            repr(dip_hunt_terms),
        ]
        return "|".join(parts)

    def _manual_ticket_key(
        self, payload: dict[str, Any], ticket_id: str | None
    ) -> str:
        """An explicit browser id if there is one, else the ticket's own terms.

        The id is the stronger signal — the same id is by definition the same
        click — and it stays stable even when the broker reports back a qty
        that differs from the one requested.
        """
        return str(ticket_id or "").strip() or self._manual_fingerprint(payload)

    def _claim_manual_ticket(
        self, key: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Reserve this ticket, or hand back the one it duplicates."""
        now = time.time()
        with self.lock:
            previous = self._last_manual_ticket
            if (
                previous is not None
                and previous[1] == key
                and now - previous[0] < _MANUAL_DUPLICATE_WINDOW
            ):
                echo = dict(previous[2])
                echo["duplicate_of"] = echo.get("order_id")
                echo["duplicate"] = True
                echo["warnings"] = list(echo.get("warnings") or []) + [
                    "This ticket matched one submitted moments ago, so it was not "
                    "sent again. Change something on it, or wait a few seconds, to "
                    "place a second order."
                ]
                return echo
            # Claimed before submitting: a second request arriving while the
            # first is still on the wire is exactly the case this exists for.
            self._last_manual_ticket = (now, key, dict(payload))
        return None

    def _release_manual_ticket(self, key: str) -> None:
        """Drop the claim when the order never made it to the broker."""
        with self.lock:
            if self._last_manual_ticket and self._last_manual_ticket[1] == key:
                self._last_manual_ticket = None

    def _remember_manual_ticket(self, key: str, payload: dict[str, Any]) -> None:
        """Store the submitted result so a duplicate can echo it back."""
        with self.lock:
            self._last_manual_ticket = (time.time(), key, dict(payload))

    def manage_position_stop(
        self,
        *,
        symbol: str,
        action: str,
        stop_price: float | None = None,
        stop_pct: float | None = None,
        trail_percent: float | None = None,
    ) -> dict[str, Any]:
        """Move, re-price, or replace the protection on an open position.

        Until now the ticket could only *cancel* a resting stop, which is the
        one operation that makes a position more dangerous. These are the three
        that make it safer:

        ``breakeven``  move the stop to the average entry, so the trade can no
                       longer lose money (offset a cent past entry, because
                       Alpaca rejects a stop sitting exactly on the base price)
        ``price``      set an explicit stop price, or one a percent away
        ``trail``      swap the fixed stop for a trailing one

        Every action replaces what is resting rather than adding to it — two
        stops over one position would exit it twice and leave a stray order.
        """
        symbol = str(symbol or "").upper().strip()
        if not symbol:
            raise ValueError("Symbol is required")
        action = str(action or "").strip().lower()
        if action not in {"breakeven", "price", "trail"}:
            raise ValueError("action must be breakeven, price, or trail")
        if self.loop_running:
            raise ValueError(
                "Stop the strategy loop before changing a stop by hand — the "
                "loop manages its own exits."
            )
        self._require_live_execution()

        service = AlpacaService(self._base_config())
        position = float(service.get_position_qty(symbol))
        if position == 0:
            raise ValueError(f"No open position in {symbol} to protect")
        is_short = position < 0

        if action == "trail":
            pct = float(trail_percent or 0)
            if pct <= 0:
                raise ValueError("Trailing stop needs a trail percent greater than 0")
            armed = service.arm_trailing_stop(symbol, trail_percent=pct)
            if not armed:
                raise ValueError("Could not arm the trailing stop")
            self._invalidate_manual_heat()
            return {"symbol": symbol, "action": action, "stop": armed}

        if action == "breakeven":
            entry = service.get_avg_entry_price(symbol)
            if entry is None or entry <= 0:
                raise ValueError(
                    f"Alpaca has no average entry price for {symbol}, so the desk "
                    "cannot work out where breakeven is."
                )
            # A cent past entry on the safe side: exactly-at-entry is rejected
            # by the broker, and this way a fill can never be a loss.
            target = normalize_stock_order_price(
                entry + 0.01 if is_short else entry - 0.01,
                field="stop_price",
            )
        else:
            explicit = float(stop_price or 0)
            if explicit > 0:
                target = round(explicit, 2)
            else:
                pct = float(stop_pct or 0)
                if pct <= 0:
                    raise ValueError(
                        "Give the stop a price, or a percent away from the mark"
                    )
                mark = float(service.get_mark_price(symbol)["price"])
                computed = service.stop_price_for_entry(mark, pct=pct, short=is_short)
                if computed is None:
                    raise ValueError("Could not price the stop from that percentage")
                target = computed

        mark_price = float(service.get_mark_price(symbol)["price"])
        # A stop on the wrong side of the market fills instantly at whatever
        # the book offers — that is a market order wearing a stop's name, and
        # never what someone adjusting protection meant to do.
        if not is_short and target >= mark_price:
            raise ValueError(
                f"A long's stop must sit below the market. ${target:.2f} is at or "
                f"above the ${mark_price:.2f} mark, so it would fill immediately."
            )
        if is_short and target <= mark_price:
            raise ValueError(
                f"A short's stop must sit above the market. ${target:.2f} is at or "
                f"below the ${mark_price:.2f} mark, so it would fill immediately."
            )

        armed = service.replace_stop_loss(symbol, target)
        if not armed:
            raise ValueError("Could not move the stop")
        self._invalidate_manual_heat()
        return {
            "symbol": symbol,
            "action": action,
            "stop": armed,
            "mark": mark_price,
        }

    def manual_order_status(self, order_id: str) -> dict[str, Any]:
        """Terminal state of a submitted ticket — acceptance is not a fill."""
        order_id = str(order_id or "").strip()
        if not order_id:
            raise ValueError("Order id is required")
        service = AlpacaService(self._base_config())
        try:
            return service.get_order_snapshot(order_id)
        except Exception as exc:
            raise ValueError(f"Could not read order {order_id}: {exc}") from exc

    def cancel_manual_order(
        self, *, order_id: str = "", symbol: str = ""
    ) -> dict[str, Any]:
        """Cancel one resting order by id, or every open order for a symbol."""
        order_id = str(order_id or "").strip()
        symbol = str(symbol or "").upper().strip()
        if not order_id and not symbol:
            raise ValueError("Pass an order id or a symbol to cancel")
        self._require_manual_book_control()
        self._require_live_execution()
        service = AlpacaService(self._base_config())
        if order_id:
            try:
                service.cancel_order(order_id)
            except Exception as exc:
                raise ValueError(f"Could not cancel order: {exc}") from exc
            plans_cancelled = self._cancel_followon_plans_for_close(
                order_ids={order_id},
                message="Cancelled because its close order was cancelled.",
            )
            reinvest_cancelled = self._cancel_reinvest_plans_for_sell(
                order_ids={order_id},
                message="Cancelled because its sell order was cancelled.",
            )
            dip_hunt_cancelled = self._cancel_dip_hunt_plans_for_orders(
                order_ids={order_id},
                message="Cancelled because its watched order was cancelled.",
            )
            return {
                "cancelled": 1,
                "order_id": order_id,
                "symbol": symbol,
                "followon_cancelled": plans_cancelled,
                "reinvest_cancelled": reinvest_cancelled,
                "dip_hunt_cancelled": dip_hunt_cancelled,
            }
        try:
            cancelled_ids = service.cancel_open_order_ids_for_symbol(symbol)
        except Exception as exc:
            raise ValueError(f"Could not cancel orders for {symbol}: {exc}") from exc
        ids = set(cancelled_ids)
        plans_cancelled = self._cancel_followon_plans_for_close(
            order_ids=ids,
            message="Cancelled because its close order was cancelled.",
        )
        reinvest_cancelled = self._cancel_reinvest_plans_for_sell(
            order_ids=ids,
            message="Cancelled because its sell order was cancelled.",
        )
        dip_hunt_cancelled = self._cancel_dip_hunt_plans_for_orders(
            order_ids=ids,
            message="Cancelled because its watched order was cancelled.",
        )
        return {
            "cancelled": len(cancelled_ids),
            "symbol": symbol,
            "followon_cancelled": plans_cancelled,
            "reinvest_cancelled": reinvest_cancelled,
            "dip_hunt_cancelled": dip_hunt_cancelled,
        }

    def list_orders(
        self,
        *,
        status: str = "open",
        symbol: str = "",
        side: str = "",
        limit: int = 200,
        after: str = "",
        until: str = "",
    ) -> dict[str, Any]:
        """Account-wide working (or recently closed) orders for the blotter."""
        status_key = str(status or "open").strip().lower()
        if status_key not in {"open", "closed"}:
            raise ValueError("status must be open or closed")
        symbol = str(symbol or "").upper().strip()
        side_key = str(side or "").strip().lower()
        if side_key and side_key not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        after_dt = _blotter_window_bound(after, field="after")
        until_dt = _blotter_window_bound(until, field="until", end_of_day=True)
        if after_dt and until_dt and after_dt > until_dt:
            raise ValueError("after must come before until")
        service = AlpacaService(self._base_config())
        symbols = [symbol] if symbol else None
        orders = service.list_orders(
            status=status_key,
            symbols=symbols,
            side=side_key,
            limit=limit,
            after=after_dt,
            until=until_dt,
        )
        # The KPI rail and the cancel-all count speak for the whole account, so
        # they never inherit the symbol, side, or window scoping the list uses.
        if status_key == "open" and not symbols and not side_key and not after_dt and not until_dt:
            open_rows = orders
            open_limit = limit
        else:
            open_rows = service.list_orders(status="open", limit=500)
            open_limit = 500
        working = [o for o in open_rows if not o.get("is_stop")]
        conditional = [o for o in open_rows if o.get("is_stop")]
        partial = [
            o
            for o in open_rows
            if (o.get("filled_qty") or 0) > 0
            and (o.get("qty") is None or (o.get("filled_qty") or 0) < float(o.get("qty") or 0))
        ]
        desk = self._desk_plans_bundle()
        self._attach_desk_plans_to_orders(orders, desk)
        # `open_rows` is priced too, so the account-wide KPIs below can talk
        # about distance even when the list itself is scoped or on Closed.
        self._attach_marks_to_orders(service, orders, open_rows)
        committed = self._open_order_value(open_rows)
        with self.lock:
            loop_running = bool(self.loop_running)
        return {
            "orders": orders,
            "count": len(orders),
            "count_limited": len(orders) >= limit,
            "status": status_key,
            "symbol": symbol,
            "side": side_key,
            "after": after_dt.date().isoformat() if after_dt else "",
            "until": until_dt.date().isoformat() if until_dt else "",
            "loop_running": loop_running,
            "trading_mode": "paper" if paper_mode_from_env() else "live",
            "open_count": len(open_rows),
            "open_count_limited": len(open_rows) >= open_limit,
            "working_count": len(working),
            "conditional_count": len(conditional),
            "partial_count": len(partial),
            "open_value": committed["total"],
            "open_value_buy": committed["buy"],
            "open_value_sell": committed["sell"],
            "open_value_partial": committed["partial"],
            "nearest_trigger": self._nearest_trigger(open_rows),
            "desk_plans": desk,
        }

    @staticmethod
    def _open_order_value(open_rows: list[dict[str, Any]]) -> dict[str, Any]:
        """What the resting book is worth if every ticket filled as written.

        A count of open orders says nothing about exposure — one ticket can be
        worth more than twenty. Each row is valued at the price it is actually
        waiting for, falling back to the mark for market orders, and only the
        unfilled remainder counts because the filled part is already a position.
        """
        total = buy = sell = 0.0
        priced = True
        for row in open_rows:
            price = None
            for key in ("limit_price", "stop_price", "mark_price"):
                value = row.get(key)
                if value is not None:
                    price = float(value)
                    break
            qty = row.get("qty")
            if qty is None:
                # A notional ticket already carries its own dollar amount.
                notional = row.get("notional")
                value = float(notional) if notional is not None else None
            elif price is None:
                value = None
            else:
                remaining = max(0.0, float(qty) - float(row.get("filled_qty") or 0))
                value = remaining * price
            if value is None:
                priced = False
                continue
            total += value
            if str(row.get("side") or "").lower() == "sell":
                sell += value
            else:
                buy += value
        return {
            "total": round(total, 2),
            "buy": round(buy, 2),
            "sell": round(sell, 2),
            # True when at least one row could not be valued, so the UI can
            # show the figure as a floor rather than an exact total.
            "partial": not priced,
        }

    @staticmethod
    def _nearest_trigger(open_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The resting ticket closest to firing.

        Scanning a blotter for the one order about to go off is the job this
        does for the operator; without it they have to read every row's
        distance themselves.
        """
        best = None
        for row in open_rows:
            pct = row.get("trigger_distance_pct")
            if pct is None:
                continue
            away = abs(float(pct))
            if best is None or away < best["distance_pct"]:
                best = {
                    "order_id": row.get("id"),
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "type": row.get("type"),
                    "trigger_price": row.get("trigger_price"),
                    "mark_price": row.get("mark_price"),
                    "distance_pct": away,
                    "signed_pct": float(pct),
                }
        return best

    @staticmethod
    def _attach_marks_to_orders(
        service: Any,
        orders: list[dict[str, Any]],
        also: list[dict[str, Any]] | None = None,
    ) -> None:
        """Stamp every row with the symbol's current mark, and resting rows
        with how far that mark sits from their trigger.

        A limit or stop price on its own says nothing about whether the ticket
        is about to fire or parked a mile away, which is the one thing an
        operator scanning a blotter needs. Marks are batched and served from
        the client's mark cache, so polling costs at most one snapshot per TTL.
        """
        # One list when the blotter already is the open book, two when it is
        # scoped or on Closed. Either way it stays a single batched call.
        rows = list(orders)
        seen_ids = {id(o) for o in rows}
        for row in also or []:
            if id(row) not in seen_ids:
                rows.append(row)
                seen_ids.add(id(row))
        wanted = sorted({str(o.get("symbol") or "") for o in rows} - {""})
        if not wanted:
            return
        try:
            # Snapshot only. The scrape fallback costs one web request per
            # symbol whenever the market is shut, which is most of the time a
            # blotter is being read, and the column reports its own age anyway.
            marks = service.get_mark_prices(wanted, scrape_fallback=False)
        except Exception:
            # A blotter that lists tickets is more useful than one that 500s
            # because the quote feed hiccuped.
            return
        for order in rows:
            mark = marks.get(str(order.get("symbol") or ""))
            price = (mark or {}).get("price")
            if price is None:
                continue
            order["mark_price"] = float(price)
            # Age and session let the page say "last price" honestly out of
            # hours instead of presenting a stale tick as the live one.
            order["mark_age_seconds"] = (mark or {}).get("age_seconds")
            order["mark_session"] = (mark or {}).get("session")
            order["mark_is_open"] = bool((mark or {}).get("is_open"))
            bar_close = (mark or {}).get("bar_close")
            if bar_close is not None and float(bar_close) > 0:
                order["mark_change_pct"] = (
                    (float(price) - float(bar_close)) / float(bar_close)
                ) * 100.0
            # Distance is only meaningful while the ticket is still waiting on
            # a price; a filled or cancelled one has nothing left to trigger.
            if not order.get("is_cancelable"):
                continue
            trigger = order.get("stop_price")
            if trigger is None:
                trigger = order.get("limit_price")
            if trigger is None or float(price) <= 0:
                continue
            order["trigger_price"] = float(trigger)
            order["trigger_distance_pct"] = (
                (float(trigger) - float(price)) / float(price)
            ) * 100.0

    def _desk_plans_bundle(self) -> dict[str, Any]:
        """Buy-backs, next tickets, and dip hunts for the Orders blotter."""
        reinvest = self.reinvest_plans_payload()
        followon = self.followon_plans_payload()
        dip_hunt = self.dip_hunt_plans_payload()
        armed = (
            sum(1 for p in reinvest if p.get("status") in _REINVEST_LIVE_STATUSES)
            + sum(1 for p in followon if p.get("status") in _FOLLOWON_LIVE_STATUSES)
            + sum(1 for p in dip_hunt if p.get("status") in _DIP_HUNT_ACTIVE)
        )
        return {
            "reinvest": reinvest,
            "followon": followon,
            "dip_hunt": dip_hunt,
            "armed_count": armed,
        }

    @staticmethod
    def _attach_desk_plans_to_orders(
        orders: list[dict[str, Any]], desk: dict[str, Any]
    ) -> None:
        """Stamp each blotter row with the desk plan that is waiting on it."""
        by_id: dict[str, list[dict[str, Any]]] = {}

        def add(order_id: Any, item: dict[str, Any]) -> None:
            oid = str(order_id or "").strip()
            if not oid:
                return
            by_id.setdefault(oid, []).append(item)

        for plan in desk.get("reinvest") or []:
            base = {
                "queue": "reinvest",
                "plan_id": plan.get("id"),
                "status": plan.get("status"),
                "live": plan.get("status") in _REINVEST_LIVE_STATUSES,
            }
            add(plan.get("sell_order_id"), {**base, "role": "trigger"})
            add(plan.get("buy_order_id"), {**base, "role": "result"})
        for plan in desk.get("followon") or []:
            base = {
                "queue": "followon",
                "plan_id": plan.get("id"),
                "status": plan.get("status"),
                "live": plan.get("status") in _FOLLOWON_LIVE_STATUSES,
            }
            add(plan.get("close_order_id"), {**base, "role": "trigger"})
            add(plan.get("next_order_id"), {**base, "role": "result"})
        for plan in desk.get("dip_hunt") or []:
            base = {
                "queue": "dip_hunt",
                "plan_id": plan.get("id"),
                "status": plan.get("status"),
                "live": plan.get("status") in _DIP_HUNT_ACTIVE,
            }
            add(plan.get("buy_order_id"), {**base, "role": "trigger"})
            add(plan.get("stop_order_id"), {**base, "role": "stop"})
            add(plan.get("dip_buy_order_id"), {**base, "role": "result"})
        for order in orders:
            order["desk"] = list(by_id.get(str(order.get("id") or ""), []))

    def cancel_all_open_orders(self) -> dict[str, Any]:
        """Cancel every open order. Blocked while the loop owns the book."""
        self._require_manual_book_control()
        self._require_live_execution()
        service = AlpacaService(self._base_config())
        try:
            result = service.cancel_all_open_orders()
        except Exception as exc:
            raise ValueError(f"Could not cancel open orders: {exc}") from exc
        if int(result.get("cancelled") or 0) > 0:
            failed_ids = {
                str(error.get("id") or "")
                for error in result.get("errors") or []
                if isinstance(error, dict)
            }
            result["followon_cancelled"] = self._cancel_followon_plans_for_close(
                all_waiting=True,
                exclude_order_ids=failed_ids,
                message="Cancelled because all open orders were cancelled.",
            )
            result["reinvest_cancelled"] = self._cancel_reinvest_plans_for_sell(
                all_waiting=True,
                exclude_order_ids=failed_ids,
                message="Cancelled because all open orders were cancelled.",
            )
            result["dip_hunt_cancelled"] = self._cancel_dip_hunt_plans_for_orders(
                all_watching=True,
                exclude_order_ids=failed_ids,
                message="Cancelled because all open orders were cancelled.",
            )
        return result

    def replace_manual_order(
        self,
        *,
        order_id: str,
        qty: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        time_in_force: str | None = None,
        trail: float | None = None,
    ) -> dict[str, Any]:
        """Replace a resting limit/stop. Same guards as cancel-all."""
        self._require_manual_book_control()
        self._require_live_execution()
        order_id = str(order_id or "").strip()
        service = AlpacaService(self._base_config())
        self._begin_rewriting_order(order_id)
        try:
            try:
                order = service.replace_order(
                    order_id,
                    qty=qty,
                    limit_price=limit_price,
                    stop_price=stop_price,
                    time_in_force=time_in_force,
                    trail=trail,
                )
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"Could not replace order: {exc}") from exc
            # Alpaca cancel-and-recreates on replace (and on accepted rewrite).
            # Keep any desk plan watching the successor.
            self._retarget_desk_plans_after_replace(
                old_order_id=order_id, new_order=order
            )
            return {"order": order}
        finally:
            self._end_rewriting_order(order_id)

    def _begin_rewriting_order(self, order_id: str) -> None:
        oid = str(order_id or "").strip()
        if not oid:
            return
        with self.lock:
            self._rewriting_order_ids.add(oid)

    def _end_rewriting_order(self, order_id: str) -> None:
        oid = str(order_id or "").strip()
        if not oid:
            return
        with self.lock:
            self._rewriting_order_ids.discard(oid)

    def _is_rewriting_order(self, order_id: str) -> bool:
        oid = str(order_id or "").strip()
        if not oid:
            return False
        with self.lock:
            return oid in self._rewriting_order_ids

    def _retarget_desk_plans_after_replace(
        self, *, old_order_id: str, new_order: dict[str, Any]
    ) -> None:
        """Point waiting desk plans at the order Alpaca created on replace."""
        old_order_id = str(old_order_id or "").strip()
        new_id = str(new_order.get("id") or "").strip() or old_order_id
        if not old_order_id:
            return
        qty = new_order.get("qty")
        limit_price = new_order.get("limit_price")
        persist_followon = False
        persist_reinvest = False
        persist_dip = False
        with self.lock:
            for plan in self.followon_plans.values():
                if plan.get("status") != "waiting":
                    continue
                if str(plan.get("close_order_id") or "") != old_order_id:
                    continue
                plan["close_order_id"] = new_id
                if qty is not None:
                    plan["close_qty"] = qty
                if "limit_price" in new_order:
                    plan["close_limit_price"] = limit_price
                plan["message"] = "Close was edited — still waiting for the fill."
                persist_followon = True
            for plan in self.reinvest_plans.values():
                status = str(plan.get("status") or "")
                if status == "waiting" and str(plan.get("sell_order_id") or "") == old_order_id:
                    plan["sell_order_id"] = new_id
                    if qty is not None:
                        plan["sell_qty"] = qty
                    if "limit_price" in new_order:
                        plan["sell_limit_price"] = limit_price
                    plan["message"] = "Sell was edited — still waiting for the fill."
                    persist_reinvest = True
                elif status == "awaiting_fill" and str(plan.get("buy_order_id") or "") == old_order_id:
                    plan["buy_order_id"] = new_id
                    if qty is not None:
                        plan["buy_qty"] = qty
                    if "limit_price" in new_order:
                        plan["limit_price"] = limit_price
                    plan["message"] = "Buy-back was edited — still waiting for a fill."
                    persist_reinvest = True
            for plan in self.dip_hunt_plans.values():
                if plan.get("status") not in _DIP_HUNT_ACTIVE:
                    continue
                rebound = False
                if str(plan.get("buy_order_id") or "") == old_order_id:
                    plan["buy_order_id"] = new_id
                    if qty is not None and plan.get("status") == "watching_entry":
                        plan["qty"] = qty
                    rebound = True
                if str(plan.get("stop_order_id") or "") == old_order_id:
                    plan["stop_order_id"] = new_id
                    rebound = True
                if str(plan.get("take_profit_order_id") or "") == old_order_id:
                    plan["take_profit_order_id"] = new_id
                    rebound = True
                if str(plan.get("dip_buy_order_id") or "") == old_order_id:
                    plan["dip_buy_order_id"] = new_id
                    rebound = True
                if rebound:
                    plan["message"] = (
                        "Order was edited — the hunt is still watching."
                    )
                    persist_dip = True
        if persist_followon:
            self._persist_followon_plans()
        if persist_reinvest:
            self._persist_reinvest_plans()
        if persist_dip:
            self._persist_dip_hunt_plans()

    @staticmethod
    def _follow_replaced_order(
        service: AlpacaService, order: dict[str, Any]
    ) -> dict[str, Any]:
        """Walk Alpaca's replace chain until the live successor.

        A replaced ticket is terminal and usually unfilled. Treating that as a
        cancel would drop the desk plan when the user only edited price or qty.
        """
        seen: set[str] = set()
        while str(order.get("status") or "").lower() == "replaced":
            current = str(order.get("id") or "").strip()
            successor = str(order.get("replaced_by") or "").strip()
            if current:
                seen.add(current)
            if not successor or successor in seen:
                return order
            order = service.get_order_snapshot(successor)
        return order

    def _desk_plan_should_wait_for_successor(
        self, order: dict[str, Any], watched_id: str
    ) -> bool:
        """True while a cancel/replace is mid-flight — do not drop the plan."""
        status = str(order.get("status") or "").lower()
        if status == "replaced":
            return True
        return status in {"canceled", "cancelled"} and self._is_rewriting_order(
            watched_id
        )

    # ------------------------------------------------------------------
    # Re-investment — buy the shares back after a sell fills
    #
    # A sell ticket can carry a standing instruction: "when this fills, put a
    # limit buy back on for the same shares, lower". The desk cannot express
    # that as one broker order (Alpaca has no sell-then-buy bracket), so it
    # holds the plan here and watches the sell order until it reaches a
    # terminal state. The buy is priced by the user, never inferred.
    #
    # The wait clock starts at that fill, not when the sell was placed — a
    # resting sell must not burn the fill window. Once the buy-back is sent it
    # rests until it fills or expire_minutes elapses, then the desk cancels it.
    # ------------------------------------------------------------------

    @staticmethod
    def _reinvest_fill_window_started(plan: dict[str, Any]) -> bool:
        """True once the sell has filled and the buy-back clock is running."""
        if plan.get("wait_started_at") not in (None, ""):
            return True
        return bool(str(plan.get("buy_order_id") or "").strip())

    def _arm_reinvest_fill_window_locked(
        self, plan: dict[str, Any], *, now: float | None = None
    ) -> None:
        """Start expire_minutes from this moment — the sell just filled."""
        if plan.get("expires_at") not in (None, "") and self._reinvest_fill_window_started(
            plan
        ):
            return
        stamp = float(now if now is not None else time.time())
        minutes = float(plan.get("expire_minutes") or _REINVEST_DEFAULT_MINUTES)
        plan["wait_started_at"] = stamp
        plan["expires_at"] = stamp + minutes * 60.0

    @staticmethod
    def _reinvest_fill_window_expired(
        plan: dict[str, Any], *, now: float | None = None
    ) -> bool:
        if not AppState._reinvest_fill_window_started(plan):
            return False
        expires_at = plan.get("expires_at")
        if expires_at in (None, ""):
            return False
        return float(now if now is not None else time.time()) > float(expires_at)

    @staticmethod
    def normalize_reinvest_request(
        raw: dict[str, Any] | None, *, side: str
    ) -> dict[str, Any] | None:
        """Validate the re-investment block that rides along with a sell ticket.

        Returns None when the ticket carries no plan. Raises ValueError with a
        sentence the ticket can print when the plan is unusable.
        """
        if not raw:
            return None
        if not bool(raw.get("enabled")):
            return None
        if str(side or "").lower() != "sell":
            raise ValueError(
                "Re-investment only attaches to a sell — a buy ticket has "
                "nothing to buy back."
            )

        qty_mode = str(raw.get("qty_mode") or "match").strip().lower()
        if qty_mode not in {"match", "custom"}:
            qty_mode = "match"

        qty: float | None = None
        if qty_mode == "custom":
            try:
                qty = float(raw.get("qty") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("Re-investment shares must be a number") from exc
            if qty <= 0:
                raise ValueError("Re-investment shares must be greater than 0")

        try:
            limit_price = float(raw.get("limit_price") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Re-investment limit price must be a number") from exc
        if limit_price <= 0:
            raise ValueError(
                "Re-investment needs a buy limit price greater than $0.00"
            )
        limit_price = normalize_stock_order_price(
            limit_price, field="reinvest limit_price"
        )

        minutes_raw = raw.get("expire_minutes")
        if minutes_raw in (None, ""):
            minutes = float(_REINVEST_DEFAULT_MINUTES)
        else:
            try:
                minutes = float(minutes_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Re-investment wait must be a number of minutes") from exc
        minutes = max(1.0, min(float(_REINVEST_MAX_MINUTES), minutes))

        tif_raw = raw.get("time_in_force") if isinstance(raw, dict) else None
        tif = str(tif_raw or "day").strip().lower()
        if tif not in MANUAL_TIME_IN_FORCE:
            raise ValueError(
                "Re-investment time_in_force must be one of: "
                + ", ".join(sorted(MANUAL_TIME_IN_FORCE))
            )
        extended = bool(raw.get("extended_hours"))
        if extended and tif not in {"day", "gtc"}:
            raise ValueError(
                "Re-investment orders in the 24-hour market must use Day or GTC time in force."
            )

        return {
            "enabled": True,
            "qty_mode": qty_mode,
            "qty": qty,
            "limit_price": limit_price,
            "expire_minutes": minutes,
            "time_in_force": tif,
            "extended_hours": extended,
        }

    def _persist_reinvest_plans(self) -> None:
        """Mirror the ledger to disk. Best-effort — never breaks a live ticket."""
        with self.lock:
            snapshot = {pid: dict(p) for pid, p in self.reinvest_plans.items()}
        try:
            reinvest_store.save_plans(snapshot, paper=paper_mode_from_env())
        except Exception as exc:  # pragma: no cover - disk issues are non-fatal
            logger.warning("could not persist re-investment plans: %s", exc)

    def bootstrap_reinvest_plans(self) -> int:
        """Reload plans left behind by a previous run and resume watching them.

        Called once at startup. A plan that was still ``waiting`` is picked up
        where it left off — the sell order id is a broker fact that outlived
        the process, so the watcher can simply ask again whether it filled.
        An ``awaiting_fill`` plan is the same for the buy-back itself. A leftover
        sell-watch deadline from an older build is cleared until the fill.
        """
        paper = paper_mode_from_env()
        try:
            stored = reinvest_store.load_plans(paper=paper)
        except Exception as exc:
            logger.warning("could not load re-investment plans: %s", exc)
            return 0
        if not stored:
            return 0
        resumed = 0
        with self.lock:
            for plan_id, plan in stored.items():
                status = str(plan.get("status") or "")
                if status == "waiting":
                    if not self._reinvest_fill_window_started(plan):
                        # Older builds stamped expires_at when the sell was
                        # placed. That clock does not apply until the fill.
                        plan["expires_at"] = None
                        plan["wait_started_at"] = None
                        resumed += 1
                    else:
                        resumed += 1
                elif status == "awaiting_fill":
                    # Even a lapsed clock must be resumed so the watcher can
                    # cancel the live buy-back; settling here would leave it.
                    resumed += 1
                self.reinvest_plans[plan_id] = plan
            self._reinvest_seq = max(
                self._reinvest_seq, reinvest_store.max_sequence(self.reinvest_plans)
            )
        if resumed:
            logger.info("resumed %d re-investment plan(s) from disk", resumed)
            self._start_reinvest_watcher()
        self._persist_reinvest_plans()
        return resumed

    def _register_reinvest_plan(
        self,
        *,
        symbol: str,
        sell_order_id: str,
        sell_qty: float,
        sell_limit_price: float | None,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Record an armed plan and make sure the watcher is running."""
        now = time.time()
        with self.lock:
            self._reinvest_seq += 1
            entry: dict[str, Any] = {
                "id": f"ri-{self._reinvest_seq}",
                "symbol": symbol,
                "sell_order_id": str(sell_order_id),
                "sell_qty": float(sell_qty),
                "sell_limit_price": sell_limit_price,
                "qty_mode": plan["qty_mode"],
                "qty": plan["qty"],
                "limit_price": plan["limit_price"],
                "expire_minutes": plan["expire_minutes"],
                "time_in_force": plan.get("time_in_force", "day"),
                "extended_hours": bool(plan.get("extended_hours")),
                "created_at": now,
                "created_at_iso": datetime.fromtimestamp(now).isoformat(
                    timespec="seconds"
                ),
                "wait_started_at": None,
                "expires_at": None,
                "status": "waiting",
                "message": "Waiting for the sell to fill.",
                "buy_order_id": None,
                "buy_qty": None,
                "sell_filled_qty": None,
                "error_count": 0,
            }
            self.reinvest_plans[entry["id"]] = entry
            self._trim_reinvest_plans_locked()
            snapshot = dict(entry)
        # Written before the watcher starts: a crash in the next millisecond
        # should still leave a record of what the desk owes the user.
        self._persist_reinvest_plans()
        self._start_reinvest_watcher()
        return snapshot

    def _trim_reinvest_plans_locked(self, keep: int = 40) -> None:
        """Bound the ledger — finished plans are history, not state."""
        if len(self.reinvest_plans) <= keep:
            return
        finished = [
            (p.get("created_at") or 0.0, pid)
            for pid, p in self.reinvest_plans.items()
            if p.get("status") not in _REINVEST_LIVE_STATUSES
        ]
        finished.sort()
        for _, pid in finished[: len(self.reinvest_plans) - keep]:
            self.reinvest_plans.pop(pid, None)

    def _start_reinvest_watcher(self) -> None:
        with self.lock:
            alive = self._reinvest_thread is not None and self._reinvest_thread.is_alive()
            if alive:
                return
            self._reinvest_stop.clear()
            thread = threading.Thread(
                target=self._bound_worker(self._reinvest_worker),
                name="reinvest-watcher",
                daemon=True,
            )
            self._reinvest_thread = thread
        thread.start()

    def _reinvest_worker(self) -> None:
        """Poll every waiting plan until none are left, then exit."""
        while not self._reinvest_stop.is_set():
            with self.lock:
                pending = [
                    pid
                    for pid, p in self.reinvest_plans.items()
                    if p.get("status") in {"waiting", "awaiting_fill"}
                ]
                if not pending:
                    # Clear this while holding the same lock registration uses.
                    # Otherwise a plan can be registered after the empty scan,
                    # see an ostensibly live watcher, and then be left behind
                    # when that watcher returns.
                    self._reinvest_thread = None
                    return
            for plan_id in pending:
                if self._reinvest_stop.is_set():
                    return
                try:
                    self._advance_reinvest_plan(plan_id)
                except Exception:  # never let one bad plan kill the watcher
                    logger.exception("Re-investment plan %s failed to advance", plan_id)
            self._reinvest_stop.wait(_REINVEST_POLL_SECONDS)

    def _settle_reinvest_plan(self, plan_id: str, status: str, message: str, **extra) -> None:
        with self.lock:
            plan = self.reinvest_plans.get(plan_id)
            if plan is None:
                return
            plan["status"] = status
            plan["message"] = message
            plan["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
            plan.update(extra)
        self._persist_reinvest_plans()

    def _note_plan_broker_error(
        self,
        *,
        plans: dict[str, dict[str, Any]],
        persist,
        settle,
        plan_id: str,
        exc: BaseException,
        what: str,
        max_errors: int,
        live_statuses: frozenset[str] | set[str] | None = None,
    ) -> None:
        """Keep waiting through DNS blips; fail immediately on a dead order id."""
        allowed = live_statuses or {"waiting"}
        kind = broker_error_kind(exc)
        message = describe_plan_read_error(exc, what=what)
        if kind == "permanent":
            settle(plan_id, "failed", message)
            return
        give_up = False
        errors = 0
        with self.lock:
            plan = plans.get(plan_id)
            if plan is None or plan.get("status") not in allowed:
                return
            plan["message"] = message
            if kind != "transient":
                errors = int(plan.get("error_count") or 0) + 1
                plan["error_count"] = errors
                give_up = errors >= max_errors
        persist()
        if give_up:
            settle(
                plan_id,
                "failed",
                f"Gave up reading the {what} after {errors} errors: {message}",
            )

    def _advance_reinvest_plan(self, plan_id: str) -> None:
        """One poll of one plan: read the sell, and buy back when it filled."""
        with self.lock:
            plan = self.reinvest_plans.get(plan_id)
            if plan is None or plan.get("status") not in {"waiting", "awaiting_fill"}:
                return
            snapshot = dict(plan)
            loop_running = self.loop_running

        if snapshot.get("status") == "awaiting_fill":
            self._advance_reinvest_awaiting_fill(plan_id, snapshot, loop_running)
            return

        # The strategy loop owns the account while it runs, and manual tickets
        # are refused for the same reason — a plan armed before it started must
        # not fire behind its back.
        if loop_running:
            self._settle_reinvest_plan(
                plan_id,
                "cancelled",
                "Strategy loop started — the re-investment buy was not placed.",
            )
            return

        try:
            service = AlpacaService(self._base_config())
            order = service.get_order_snapshot(str(snapshot["sell_order_id"]))
            order = self._follow_replaced_order(service, order)
        except Exception as exc:
            self._note_plan_broker_error(
                plans=self.reinvest_plans,
                persist=self._persist_reinvest_plans,
                settle=self._settle_reinvest_plan,
                plan_id=plan_id,
                exc=exc,
                what="sell order",
                max_errors=_REINVEST_MAX_ERRORS,
            )
            return

        successor_id = str(order.get("id") or "").strip()
        watched_id = str(snapshot.get("sell_order_id") or "").strip()
        if successor_id and successor_id != watched_id:
            with self.lock:
                live = self.reinvest_plans.get(plan_id)
                if live is None or live.get("status") != "waiting":
                    return
                live["sell_order_id"] = successor_id
                if order.get("qty") is not None:
                    live["sell_qty"] = order.get("qty")
                if "limit_price" in order:
                    live["sell_limit_price"] = order.get("limit_price")
                live["message"] = "Sell was edited — still waiting for the fill."
                snapshot = dict(live)
            self._persist_reinvest_plans()

        with self.lock:
            live = self.reinvest_plans.get(plan_id)
            if live is not None and int(live.get("error_count") or 0):
                live["error_count"] = 0

        filled = float(order.get("filled_qty") or 0.0)
        status = str(order.get("status") or "").lower()
        terminal = bool(order.get("is_terminal"))

        if status == "replaced" or (
            status in {"canceled", "cancelled"} and self._is_rewriting_order(watched_id)
        ):
            return  # successor not yet known — don't drop the buy-back
        if status != "filled" and not terminal:
            return  # still working — check again next poll

        if filled <= 0:
            with self.lock:
                live = self.reinvest_plans.get(plan_id)
                if live is None or live.get("status") != "waiting":
                    return
                if str(live.get("sell_order_id") or "") != watched_id:
                    return
            self._settle_reinvest_plan(
                plan_id,
                "cancelled",
                f"The sell ended {status or 'unfilled'} without a fill — nothing was bought back.",
                sell_filled_qty=0.0,
            )
            return

        self._place_reinvest_buy(plan_id, snapshot, service, filled_qty=filled)

    def _place_reinvest_buy(
        self,
        plan_id: str,
        plan: dict[str, Any],
        service: AlpacaService,
        *,
        filled_qty: float,
    ) -> None:
        """Send the buy leg once the sell is known to have filled."""
        try:
            # A live plan can survive a restart. Re-check the live kill-switch
            # when the delayed buy is actually sent, not only when its sell
            # ticket was created.
            self._require_live_execution()
        except ValueError as exc:
            self._settle_reinvest_plan(
                plan_id,
                "failed",
                f"The sell filled but the buy-back was blocked: {exc}",
                sell_filled_qty=filled_qty,
            )
            return

        symbol = str(plan["symbol"])
        limit_price = float(plan["limit_price"])
        # "Match" follows the shares that actually sold, so a partial fill buys
        # back what left rather than what was asked for. A custom count is the
        # user's own number and is honoured as written.
        if plan["qty_mode"] == "custom":
            buy_qty = float(plan["qty"] or 0)
        else:
            buy_qty = filled_qty

        notes: list[str] = []
        try:
            session_info = service.market_session()
        except Exception:
            session_info = {"is_open": True, "session": "unknown"}

        # Outside RTH Alpaca takes whole-share limit orders only.
        if not session_info.get("is_open") and buy_qty != float(int(buy_qty)):
            truncated = float(int(buy_qty))
            notes.append(f"whole shares outside regular hours ({buy_qty:g} → {truncated:g})")
            buy_qty = truncated

        if buy_qty <= 0:
            self._settle_reinvest_plan(
                plan_id,
                "failed",
                f"Nothing to buy back — the re-investment sized to {buy_qty:g} shares.",
                sell_filled_qty=filled_qty,
            )
            return

        # Claim the plan before spending anything. Cancel only applies to a
        # waiting plan, so this closes the window where a user cancels while
        # the buy is already on the wire and is told it never happened.
        cancelled_for_loop = False
        with self.lock:
            live = self.reinvest_plans.get(plan_id)
            if live is None or live.get("status") != "waiting":
                return
            if self.loop_running:
                live["status"] = "cancelled"
                live["message"] = (
                    "Strategy loop started — the re-investment buy was not placed."
                )
                live["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
                cancelled_for_loop = True
            else:
                live["status"] = "placing"
                live["message"] = "The sell filled — sending the buy-back…"
        # Persist the uncertainty boundary immediately before touching the
        # broker. A restart then marks this interrupted instead of sending it
        # twice from a stale waiting state.
        self._persist_reinvest_plans()
        if cancelled_for_loop:
            return

        client_order_id = (
            f"reinvest-{plan_id}-{str(plan.get('sell_order_id') or '')}"
        )[:128]
        tif = str(plan.get("time_in_force") or "day").strip().lower()
        extended = bool(plan.get("extended_hours"))
        try:
            submitted, _ = service.submit_manual_order(
                symbol,
                buy_qty,
                OrderSide.BUY,
                order_type="limit",
                limit_price=limit_price,
                # A plain limit buy: an attached stop would force whole shares
                # and a second price the user never chose.
                stop_loss_pct=0.0,
                time_in_force=tif,
                extended_hours=extended,
                client_order_id=client_order_id,
            )
        except Exception as exc:
            self._settle_reinvest_plan(
                plan_id,
                "failed",
                f"The sell filled but the buy-back was rejected: {humanize_alpaca_error(exc)}",
                sell_filled_qty=filled_qty,
            )
            return

        order_id = str(getattr(submitted, "id", "") or "")
        if not order_id:
            self._settle_reinvest_plan(
                plan_id,
                "failed",
                "The sell filled but the buy-back came back without an order id.",
                buy_qty=buy_qty,
                sell_filled_qty=filled_qty,
            )
            return

        with self.lock:
            live = self.reinvest_plans.get(plan_id)
            if live is None or live.get("status") != "placing":
                return
            live["status"] = "awaiting_fill"
            live["buy_order_id"] = order_id
            live["buy_qty"] = buy_qty
            live["sell_filled_qty"] = filled_qty
            live["message"] = (
                f"Buy-back resting @ ${limit_price:.2f} — waiting for a fill."
            )
            if notes:
                live["message"] += " · " + ", ".join(notes)
            self._arm_reinvest_fill_window_locked(live)
        self._persist_reinvest_plans()

        # History is where the user reconciles the day, and this buy was never
        # typed into a ticket — without this row it would appear from nowhere.
        self._record_trade_history(
            [
                {
                    "symbol": symbol,
                    "signal": "buy",
                    "side": "buy",
                    "order_type": "limit",
                    "price": limit_price,
                    "limit_price": limit_price,
                    "order_qty": buy_qty,
                    "order_id": order_id,
                    "engine": "manual",
                    "mode": "manual",
                    "session": session_info.get("session"),
                    "reason": (
                        f"Re-investment after sell {plan['sell_order_id'][:8]} "
                        f"filled {filled_qty:g} — limit buy {buy_qty:g} @ ${limit_price:.2f}"
                    ),
                }
            ]
        )

    def _advance_reinvest_awaiting_fill(
        self,
        plan_id: str,
        snapshot: dict[str, Any],
        loop_running: bool,
    ) -> None:
        """Watch a resting buy-back until it fills, expires, or is cancelled."""
        buy_order_id = str(snapshot.get("buy_order_id") or "").strip()
        if not buy_order_id:
            self._settle_reinvest_plan(
                plan_id,
                "failed",
                "The buy-back was sent without an order id — nothing to watch.",
            )
            return

        if loop_running:
            try:
                self._cancel_resting_reinvest_buy(snapshot)
            except Exception as exc:
                self._settle_reinvest_plan(
                    plan_id,
                    "interrupted",
                    (
                        "Strategy loop started, but the buy-back could not be "
                        f"cancelled — check Positions: {exc}"
                    ),
                )
                return
            self._settle_reinvest_plan(
                plan_id,
                "cancelled",
                "Strategy loop started — the buy-back was cancelled.",
            )
            return

        try:
            service = AlpacaService(self._base_config())
            order = service.get_order_snapshot(buy_order_id)
            order = self._follow_replaced_order(service, order)
        except Exception as exc:
            self._note_plan_broker_error(
                plans=self.reinvest_plans,
                persist=self._persist_reinvest_plans,
                settle=self._settle_reinvest_plan,
                plan_id=plan_id,
                exc=exc,
                what="buy-back order",
                max_errors=_REINVEST_MAX_ERRORS,
                live_statuses={"awaiting_fill"},
            )
            return

        successor_id = str(order.get("id") or "").strip()
        if successor_id and successor_id != buy_order_id:
            with self.lock:
                live = self.reinvest_plans.get(plan_id)
                if live is None or live.get("status") != "awaiting_fill":
                    return
                live["buy_order_id"] = successor_id
                snapshot = dict(live)
            self._persist_reinvest_plans()
            buy_order_id = successor_id

        with self.lock:
            live = self.reinvest_plans.get(plan_id)
            if live is not None and int(live.get("error_count") or 0):
                live["error_count"] = 0

        filled = float(order.get("filled_qty") or 0.0)
        status = str(order.get("status") or "").lower()
        terminal = bool(order.get("is_terminal"))
        watched_id = str(snapshot.get("buy_order_id") or "").strip()

        if status == "replaced" or (
            status in {"canceled", "cancelled"} and self._is_rewriting_order(watched_id)
        ):
            return

        if status == "filled" or (terminal and filled > 0):
            symbol = str(snapshot.get("symbol") or "")
            limit_price = float(snapshot.get("limit_price") or 0)
            qty = filled or float(snapshot.get("buy_qty") or 0)
            self._settle_reinvest_plan(
                plan_id,
                "placed",
                f"Bought back {qty:g} {symbol} @ ${limit_price:.2f} (limit)",
                buy_qty=qty,
                sell_filled_qty=snapshot.get("sell_filled_qty"),
            )
            return

        if terminal and filled <= 0:
            self._settle_reinvest_plan(
                plan_id,
                "cancelled",
                (
                    f"The buy-back ended {status or 'unfilled'} without a fill."
                ),
            )
            return

        if not self._reinvest_fill_window_expired(snapshot):
            return

        try:
            self._cancel_resting_reinvest_buy(snapshot, service=service)
        except Exception as exc:
            with self.lock:
                live = self.reinvest_plans.get(plan_id)
                if live is None or live.get("status") != "awaiting_fill":
                    return
                live["message"] = (
                    "The wait ended but the buy-back could not be cancelled "
                    f"yet: {exc}"
                )
            self._persist_reinvest_plans()
            return

        minutes = snapshot.get("expire_minutes")
        symbol = str(snapshot.get("symbol") or "")
        limit_price = float(snapshot.get("limit_price") or 0)
        if filled > 0:
            self._settle_reinvest_plan(
                plan_id,
                "placed",
                (
                    f"Bought back {filled:g} {symbol} @ ${limit_price:.2f} "
                    f"(partial) — wait ended after {minutes:g} minutes."
                ),
                buy_qty=filled,
                sell_filled_qty=snapshot.get("sell_filled_qty"),
            )
            return
        self._settle_reinvest_plan(
            plan_id,
            "expired",
            (
                f"The buy-back did not fill within {minutes:g} minutes — "
                "the order was cancelled."
            ),
        )

    def _cancel_resting_reinvest_buy(
        self,
        plan: dict[str, Any],
        *,
        service: AlpacaService | None = None,
    ) -> None:
        order_id = str(plan.get("buy_order_id") or "")
        if not order_id:
            return
        (service or AlpacaService(self._base_config())).cancel_order(order_id)

    def _cancel_reinvest_plans_for_sell(
        self,
        *,
        order_ids: set[str] | None = None,
        all_waiting: bool = False,
        exclude_order_ids: set[str] | None = None,
        message: str,
    ) -> int:
        """Disarm waiting buy-backs whose sell (or resting buy) was cancelled."""
        wanted = {str(value or "").strip() for value in (order_ids or set())}
        wanted.discard("")
        excluded = {
            str(value or "").strip() for value in (exclude_order_ids or set())
        }
        excluded.discard("")
        cancelled = 0
        now_iso = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            for plan in self.reinvest_plans.values():
                status = str(plan.get("status") or "")
                if status not in {"waiting", "awaiting_fill"}:
                    continue
                ids = {
                    str(plan.get("sell_order_id") or ""),
                    str(plan.get("buy_order_id") or ""),
                }
                ids.discard("")
                if ids & excluded:
                    continue
                if not all_waiting and not (ids & wanted):
                    continue
                plan["status"] = "cancelled"
                plan["message"] = message
                plan["settled_at_iso"] = now_iso
                cancelled += 1
        if cancelled:
            self._persist_reinvest_plans()
        return cancelled

    def reinvest_plans_payload(self, symbol: str = "") -> list[dict[str, Any]]:
        """Plans for the rail — newest first, optionally one symbol only."""
        symbol = str(symbol or "").upper().strip()
        with self.lock:
            plans = [dict(p) for p in self.reinvest_plans.values()]
        if symbol:
            plans = [p for p in plans if p.get("symbol") == symbol]
        plans.sort(key=lambda p: p.get("created_at") or 0.0, reverse=True)
        now = time.time()
        for plan in plans:
            plan.pop("error_count", None)
            plan.pop("position_error_count", None)
            plan.pop("flat_check_count", None)
            status = str(plan.get("status") or "")
            if status == "waiting":
                started = self._reinvest_fill_window_started(plan)
                plan["wait_started"] = started
                plan["seconds_left"] = None
            elif status == "awaiting_fill":
                plan["wait_started"] = True
                expires_at = plan.get("expires_at")
                if expires_at not in (None, ""):
                    plan["seconds_left"] = max(0.0, float(expires_at) - now)
                else:
                    plan["seconds_left"] = None
        return plans

    def cancel_reinvest_plan(self, plan_id: str) -> dict[str, Any]:
        """Disarm a waiting plan. A resting buy-back is cancelled; the sell is not."""
        plan_id = str(plan_id or "").strip()
        if not plan_id:
            raise ValueError("Plan id is required")
        with self.lock:
            plan = self.reinvest_plans.get(plan_id)
            if plan is None:
                raise ValueError(f"No re-investment plan {plan_id}")
            status = str(plan.get("status") or "")
            if status not in {"waiting", "awaiting_fill"}:
                raise ValueError(
                    f"This re-investment is already {status} — nothing to cancel."
                )
            snapshot = dict(plan)
            if status == "awaiting_fill":
                plan["status"] = "placing"
                plan["message"] = "Cancelling the buy-back…"
            else:
                plan["status"] = "cancelled"
                plan["message"] = "Cancelled before the sell filled."
                plan["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
                result = dict(plan)
        if snapshot.get("status") == "awaiting_fill":
            self._persist_reinvest_plans()
            try:
                self._cancel_resting_reinvest_buy(snapshot)
            except Exception as exc:
                with self.lock:
                    live = self.reinvest_plans.get(plan_id)
                    if live is not None and live.get("status") == "placing":
                        live["status"] = "awaiting_fill"
                        live["message"] = (
                            "Could not cancel the buy-back; it is still being "
                            f"watched: {exc}"
                        )
                self._persist_reinvest_plans()
                raise ValueError(
                    f"Could not cancel the buy-back: {exc}"
                ) from exc
            self._settle_reinvest_plan(
                plan_id,
                "cancelled",
                "Cancelled before the buy-back filled.",
            )
            with self.lock:
                live = self.reinvest_plans.get(plan_id)
                result = dict(live) if live is not None else dict(snapshot)
        self._persist_reinvest_plans()
        result.pop("error_count", None)
        return result

    # ------------------------------------------------------------------
    # Follow-on — next ticket after a close fills
    #
    # A sell or cover can carry a standing instruction: reverse the same
    # name (long → short, short → long) at a typed price, or buy a different
    # symbol once the close fills. Alpaca will not flip a long into a short
    # on one order, so the desk waits for the close, then sends the next
    # ticket itself. The wait clock starts at that fill, not when the close
    # was placed — a resting sell must not burn the send window.
    # ------------------------------------------------------------------

    def _cancel_followon_plans_for_close(
        self,
        *,
        order_ids: set[str] | None = None,
        all_waiting: bool = False,
        exclude_order_ids: set[str] | None = None,
        message: str,
    ) -> int:
        """Disarm waiting next tickets whose close orders were cancelled."""
        wanted = {str(value or "").strip() for value in (order_ids or set())}
        excluded = {
            str(value or "").strip() for value in (exclude_order_ids or set())
        }
        cancelled = 0
        now_iso = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            for plan in self.followon_plans.values():
                close_order_id = str(plan.get("close_order_id") or "")
                if plan.get("status") != "waiting" or close_order_id in excluded:
                    continue
                if not all_waiting and close_order_id not in wanted:
                    continue
                plan["status"] = "cancelled"
                plan["message"] = message
                plan["settled_at_iso"] = now_iso
                cancelled += 1
        if cancelled:
            self._persist_followon_plans()
        return cancelled

    @staticmethod
    def _followon_send_window_started(plan: dict[str, Any]) -> bool:
        """True once the close has filled and the send clock is running."""
        if plan.get("wait_started_at") not in (None, ""):
            return True
        return float(plan.get("close_filled_qty") or 0) > 0

    def _arm_followon_send_window_locked(
        self, plan: dict[str, Any], *, now: float | None = None
    ) -> None:
        """Start expire_minutes from this moment — the close just filled."""
        if plan.get("expires_at") not in (None, "") and self._followon_send_window_started(
            plan
        ):
            return
        stamp = float(now if now is not None else time.time())
        minutes = float(plan.get("expire_minutes") or _FOLLOWON_DEFAULT_MINUTES)
        plan["wait_started_at"] = stamp
        plan["expires_at"] = stamp + minutes * 60.0

    @staticmethod
    def _followon_send_window_expired(
        plan: dict[str, Any], *, now: float | None = None
    ) -> bool:
        if not AppState._followon_send_window_started(plan):
            return False
        expires_at = plan.get("expires_at")
        if expires_at in (None, ""):
            return False
        return float(now if now is not None else time.time()) > float(expires_at)

    @staticmethod
    def _followon_request_order_type(raw: dict[str, Any]) -> str:
        """Limit vs market for a next ticket.

        ``market`` / ``ticket_type`` are read first so a missing nested
        ``order_type`` cannot be filled in as a limit that then demands a price.
        """
        flag = raw.get("market")
        if flag is True or str(flag or "").strip().lower() in {"true", "1", "yes"}:
            return "market"
        unknown = ""
        for key in ("ticket_type", "followon_order_type", "order_type"):
            raw_val = raw.get(key)
            if raw_val in (None, ""):
                continue
            val = str(raw_val).strip().lower()
            if val in {"market", "limit"}:
                return val
            if not unknown:
                unknown = val
        return unknown or "limit"

    @staticmethod
    def normalize_followon_request(
        raw: dict[str, Any] | None, *, side: str, close_symbol: str
    ) -> dict[str, Any] | None:
        """Validate the next-ticket block that rides along with a close.

        Returns None when the ticket carries no plan. Raises ValueError with a
        sentence the ticket can print when the plan is unusable.
        """
        if not raw:
            return None
        if not bool(raw.get("enabled")):
            return None
        side_l = str(side or "").strip().lower()
        if side_l not in {"sell", "cover"}:
            raise ValueError(
                "A next ticket only attaches to a sell or a cover — there is "
                "no close to wait for."
            )

        kind = str(raw.get("kind") or "reverse").strip().lower()
        if kind not in {"reverse", "rotate"}:
            raise ValueError("Next ticket kind must be reverse or rotate")

        close_symbol = str(close_symbol or "").upper().strip()
        if kind == "rotate":
            target = str(raw.get("target_symbol") or "").upper().strip()
            if not target:
                raise ValueError(
                    "Buying another stock needs a symbol for the next ticket"
                )
            if target == close_symbol:
                raise ValueError(
                    "Buy another stock needs a different symbol than the one "
                    "you are closing"
                )
            next_side = "buy"
        else:
            target = close_symbol
            next_side = "short" if side_l == "sell" else "buy"

        qty_mode = str(raw.get("qty_mode") or "match").strip().lower()
        if qty_mode not in {"match", "custom"}:
            qty_mode = "match"

        qty: float | None = None
        if qty_mode == "custom":
            try:
                qty = float(raw.get("qty") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("Next-ticket shares must be a number") from exc
            if qty <= 0:
                raise ValueError("Next-ticket shares must be greater than 0")

        order_type = AppState._followon_request_order_type(raw)
        if order_type not in {"market", "limit"}:
            raise ValueError("Next-ticket type must be market or limit")

        limit_price: float | None = None
        if order_type == "limit":
            try:
                limit_price = float(raw.get("limit_price") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Next-ticket limit price must be a number"
                ) from exc
            if limit_price <= 0:
                raise ValueError(
                    "A next ticket needs a limit price greater than $0.00"
                )
            limit_price = normalize_stock_order_price(
                limit_price, field="followon limit_price"
            )

        minutes_raw = raw.get("expire_minutes")
        if minutes_raw in (None, ""):
            minutes = float(_FOLLOWON_DEFAULT_MINUTES)
        else:
            try:
                minutes = float(minutes_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Next-ticket wait must be a number of minutes"
                ) from exc
        minutes = max(1.0, min(float(_FOLLOWON_MAX_MINUTES), minutes))

        tif_raw = raw.get("time_in_force") if isinstance(raw, dict) else None
        tif = str(tif_raw or "day").strip().lower()
        if tif not in MANUAL_TIME_IN_FORCE:
            raise ValueError(
                "Next-ticket time_in_force must be one of: "
                + ", ".join(sorted(MANUAL_TIME_IN_FORCE))
            )
        extended = bool(raw.get("extended_hours"))
        if extended and tif not in {"day", "gtc"}:
            raise ValueError(
                "Next-ticket orders in the 24-hour market must use Day or GTC time in force."
            )

        if extended and order_type != "limit":
            raise ValueError(
                "Next-ticket market orders are not supported in the 24-hour market; choose limit."
            )

        return {
            "enabled": True,
            "kind": kind,
            "target_symbol": target,
            "next_side": next_side,
            "qty_mode": qty_mode,
            "qty": qty,
            "order_type": order_type,
            "limit_price": limit_price,
            "expire_minutes": minutes,
            "time_in_force": tif,
            "extended_hours": extended,
        }

    def _persist_followon_plans(self) -> None:
        with self.lock:
            snapshot = {pid: dict(p) for pid, p in self.followon_plans.items()}
        try:
            followon_store.save_plans(snapshot, paper=paper_mode_from_env())
        except Exception as exc:  # pragma: no cover - disk issues are non-fatal
            logger.warning("could not persist follow-on plans: %s", exc)

    def bootstrap_followon_plans(self) -> int:
        """Reload next tickets left behind by a previous run and resume them."""
        paper = paper_mode_from_env()
        try:
            stored = followon_store.load_plans(paper=paper)
        except Exception as exc:
            logger.warning("could not load follow-on plans: %s", exc)
            return 0
        if not stored:
            return 0
        now = time.time()
        resumed = 0
        with self.lock:
            for plan_id, plan in stored.items():
                if plan.get("status") == "waiting":
                    if not self._followon_send_window_started(plan):
                        # Older builds stamped expires_at when the close was
                        # placed. That clock does not apply until the fill.
                        plan["expires_at"] = None
                        plan["wait_started_at"] = None
                        resumed += 1
                    elif self._followon_send_window_expired(plan, now=now):
                        plan["status"] = "expired"
                        plan["message"] = (
                            "Expired while the desk was closed — the next "
                            "ticket was not sent in time after the close filled."
                        )
                    else:
                        resumed += 1
                self.followon_plans[plan_id] = plan
            self._followon_seq = max(
                self._followon_seq, followon_store.max_sequence(self.followon_plans)
            )
        if resumed:
            logger.info("resumed %d follow-on plan(s) from disk", resumed)
            self._start_followon_watcher()
        self._persist_followon_plans()
        return resumed

    def _register_followon_plan(
        self,
        *,
        symbol: str,
        close_side: str,
        close_order_id: str,
        close_qty: float,
        close_limit_price: float | None,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            self._followon_seq += 1
            entry: dict[str, Any] = {
                "id": f"fo-{self._followon_seq}",
                "symbol": symbol,
                "close_side": close_side,
                "close_order_id": str(close_order_id),
                "close_qty": float(close_qty),
                "close_limit_price": close_limit_price,
                "kind": plan["kind"],
                "target_symbol": plan["target_symbol"],
                "next_side": plan["next_side"],
                "qty_mode": plan["qty_mode"],
                "qty": plan["qty"],
                "order_type": plan.get("order_type") or "limit",
                "limit_price": plan.get("limit_price"),
                "expire_minutes": plan["expire_minutes"],
                "time_in_force": plan.get("time_in_force", "day"),
                "extended_hours": bool(plan.get("extended_hours")),
                "created_at": now,
                "created_at_iso": datetime.fromtimestamp(now).isoformat(
                    timespec="seconds"
                ),
                "wait_started_at": None,
                "expires_at": None,
                "status": "waiting",
                "message": "Waiting for the close to fill.",
                "next_order_id": None,
                "next_qty": None,
                "close_filled_qty": None,
                "error_count": 0,
                "position_error_count": 0,
                "flat_check_count": 0,
            }
            self.followon_plans[entry["id"]] = entry
            self._trim_followon_plans_locked()
            snapshot = dict(entry)
        self._persist_followon_plans()
        self._start_followon_watcher()
        return snapshot

    def _trim_followon_plans_locked(self, keep: int = 40) -> None:
        if len(self.followon_plans) <= keep:
            return
        finished = [
            (p.get("created_at") or 0.0, pid)
            for pid, p in self.followon_plans.items()
            if p.get("status") != "waiting"
        ]
        finished.sort()
        for _, pid in finished[: len(self.followon_plans) - keep]:
            self.followon_plans.pop(pid, None)

    def _start_followon_watcher(self) -> None:
        with self.lock:
            alive = (
                self._followon_thread is not None and self._followon_thread.is_alive()
            )
            if alive:
                return
            self._followon_stop.clear()
            thread = threading.Thread(
                target=self._bound_worker(self._followon_worker),
                name="followon-watcher",
                daemon=True,
            )
            self._followon_thread = thread
        thread.start()

    def _followon_worker(self) -> None:
        while not self._followon_stop.is_set():
            with self.lock:
                pending = [
                    pid
                    for pid, p in self.followon_plans.items()
                    if p.get("status") == "waiting"
                ]
                if not pending:
                    # Clear this while holding the same lock registration uses.
                    # Otherwise a plan can be registered after the empty scan,
                    # see an ostensibly live watcher, and then be left behind
                    # when that watcher returns.
                    self._followon_thread = None
                    return
            for plan_id in pending:
                if self._followon_stop.is_set():
                    return
                try:
                    self._advance_followon_plan(plan_id)
                except Exception:
                    logger.exception("Follow-on plan %s failed to advance", plan_id)
            self._followon_stop.wait(_FOLLOWON_POLL_SECONDS)

    def _settle_followon_plan(
        self, plan_id: str, status: str, message: str, **extra
    ) -> None:
        with self.lock:
            plan = self.followon_plans.get(plan_id)
            if plan is None:
                return
            plan["status"] = status
            plan["message"] = message
            plan["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
            plan.update(extra)
        self._persist_followon_plans()

    def _advance_followon_plan(self, plan_id: str) -> None:
        with self.lock:
            plan = self.followon_plans.get(plan_id)
            if plan is None or plan.get("status") != "waiting":
                return
            snapshot = dict(plan)
            loop_running = self.loop_running

        if loop_running:
            self._settle_followon_plan(
                plan_id,
                "cancelled",
                "Strategy loop started — the next ticket was not placed.",
            )
            return

        try:
            service = AlpacaService(self._base_config())
            order = service.get_order_snapshot(str(snapshot["close_order_id"]))
            order = self._follow_replaced_order(service, order)
        except Exception as exc:
            if self._followon_send_window_expired(snapshot):
                close_filled = float(snapshot.get("close_filled_qty") or 0)
                if close_filled > 0:
                    self._settle_followon_plan(
                        plan_id,
                        "failed",
                        (
                            f"The close filled {close_filled:g} shares, but the "
                            "position could not be verified safe before the "
                            "next-ticket wait ended."
                        ),
                    )
                else:
                    self._settle_followon_plan(
                        plan_id,
                        "expired",
                        (
                            "The next ticket could not be sent within "
                            f"{snapshot['expire_minutes']:g} minutes after "
                            "the close filled."
                        ),
                    )
                return
            self._note_plan_broker_error(
                plans=self.followon_plans,
                persist=self._persist_followon_plans,
                settle=self._settle_followon_plan,
                plan_id=plan_id,
                exc=exc,
                what="close order",
                max_errors=_FOLLOWON_MAX_ERRORS,
            )
            return

        successor_id = str(order.get("id") or "").strip()
        watched_id = str(snapshot.get("close_order_id") or "").strip()
        if successor_id and successor_id != watched_id:
            with self.lock:
                live = self.followon_plans.get(plan_id)
                if live is None or live.get("status") != "waiting":
                    return
                live["close_order_id"] = successor_id
                if order.get("qty") is not None:
                    live["close_qty"] = order.get("qty")
                if "limit_price" in order:
                    live["close_limit_price"] = order.get("limit_price")
                live["message"] = "Close was edited — still waiting for the fill."
                snapshot = dict(live)
            self._persist_followon_plans()

        with self.lock:
            live = self.followon_plans.get(plan_id)
            if live is not None and int(live.get("error_count") or 0):
                live["error_count"] = 0

        filled = float(order.get("filled_qty") or 0.0)
        status = str(order.get("status") or "").lower()
        terminal = bool(order.get("is_terminal"))

        if filled > 0:
            already_trying = False
            with self.lock:
                live = self.followon_plans.get(plan_id)
                if live is None or live.get("status") != "waiting":
                    return
                already_trying = self._followon_send_window_started(live)
                self._arm_followon_send_window_locked(live)
                live["close_filled_qty"] = filled
                snapshot = dict(live)
            self._persist_followon_plans()
            if already_trying and self._followon_send_window_expired(snapshot):
                self._settle_followon_plan(
                    plan_id,
                    "failed",
                    (
                        f"The close filled {filled:g} shares, but the "
                        "position could not be verified safe before the "
                        "next-ticket wait ended."
                    ),
                    close_filled_qty=filled,
                )
                return
            self._place_followon_order(plan_id, snapshot, service, filled_qty=filled)
            return

        if status == "replaced" or (
            status in {"canceled", "cancelled"} and self._is_rewriting_order(watched_id)
        ):
            return  # successor not yet known — don't drop the next ticket
        if status != "filled" and not terminal:
            return

        with self.lock:
            live = self.followon_plans.get(plan_id)
            if live is None or live.get("status") != "waiting":
                return
            if str(live.get("close_order_id") or "") != watched_id:
                return

        self._settle_followon_plan(
            plan_id,
            "cancelled",
            (
                f"The close ended {status or 'unfilled'} without a fill — "
                "the next ticket was not sent."
            ),
            close_filled_qty=0.0,
        )

    def _defer_followon_plan(
        self,
        plan_id: str,
        *,
        counter: str,
        message: str,
        max_attempts: int,
        failure_message: str,
        filled_qty: float,
    ) -> None:
        """Keep a filled-close plan waiting while broker state catches up."""
        with self.lock:
            live = self.followon_plans.get(plan_id)
            if live is None or live.get("status") != "waiting":
                return
            attempts = int(live.get(counter) or 0) + 1
            live[counter] = attempts
            live["close_filled_qty"] = filled_qty
            self._arm_followon_send_window_locked(live)
            if attempts >= max_attempts:
                live["status"] = "failed"
                live["message"] = failure_message
                live["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
            else:
                live["message"] = message
        self._persist_followon_plans()

    def _place_followon_order(
        self,
        plan_id: str,
        plan: dict[str, Any],
        service: AlpacaService,
        *,
        filled_qty: float,
    ) -> None:
        try:
            # A live plan can survive a restart. Re-check the live kill-switch
            # when the delayed order is actually sent, not only when its close
            # ticket was created.
            self._require_live_execution()
        except ValueError as exc:
            self._settle_followon_plan(
                plan_id,
                "failed",
                f"The close filled but the next ticket was blocked: {exc}",
                close_filled_qty=filled_qty,
            )
            return

        kind = str(plan.get("kind") or "reverse")
        close_side = str(plan.get("close_side") or "sell")
        close_symbol = str(plan["symbol"])
        target_symbol = str(plan.get("target_symbol") or close_symbol)
        next_side = str(plan.get("next_side") or "buy")
        order_type = str(plan.get("order_type") or "limit").strip().lower()
        if order_type not in {"market", "limit"}:
            order_type = "limit"
        limit_price: float | None = None
        if order_type == "limit":
            limit_price = float(plan["limit_price"])

        if plan.get("qty_mode") == "custom":
            next_qty = float(plan.get("qty") or 0)
        else:
            next_qty = filled_qty

        notes: list[str] = []
        try:
            session_info = service.market_session()
        except Exception:
            session_info = {"is_open": True, "session": "unknown"}

        if kind == "reverse":
            try:
                remaining = float(service.get_position_qty_strict(close_symbol))
            except Exception as exc:
                detail = humanize_alpaca_error(exc)
                self._defer_followon_plan(
                    plan_id,
                    counter="position_error_count",
                    message=(
                        f"The close filled; waiting to verify that {close_symbol} "
                        f"is flat ({detail})."
                    ),
                    max_attempts=_FOLLOWON_MAX_ERRORS,
                    failure_message=(
                        f"Could not verify that {close_symbol} was flat after "
                        f"{_FOLLOWON_MAX_ERRORS} attempts: {detail}"
                    ),
                    filled_qty=filled_qty,
                )
                return
            if abs(remaining) > 1e-9:
                current_side = "long" if remaining > 0 else "short"
                intended = "short" if close_side == "sell" else "buy"
                still_closing = (close_side == "sell" and remaining > 0) or (
                    close_side == "cover" and remaining < 0
                )
                if still_closing:
                    self._defer_followon_plan(
                        plan_id,
                        counter="flat_check_count",
                        message=(
                            f"The close filled; waiting for Alpaca to report "
                            f"{close_symbol} flat (still {current_side} "
                            f"{abs(remaining):g})."
                        ),
                        max_attempts=_FOLLOWON_FLAT_MAX_CHECKS,
                        failure_message=(
                            f"{close_symbol} still reports {current_side} "
                            f"{abs(remaining):g} after "
                            f"{_FOLLOWON_FLAT_MAX_CHECKS} checks — the reverse "
                            f"{intended} was not placed."
                        ),
                        filled_qty=filled_qty,
                    )
                else:
                    self._settle_followon_plan(
                        plan_id,
                        "failed",
                        (
                            f"{close_symbol} is already {current_side} "
                            f"{abs(remaining):g} — the reverse {intended} was "
                            "not placed again."
                        ),
                        close_filled_qty=filled_qty,
                    )
                return
        else:
            try:
                target_pos = float(service.get_position_qty_strict(target_symbol))
            except Exception as exc:
                detail = humanize_alpaca_error(exc)
                self._defer_followon_plan(
                    plan_id,
                    counter="position_error_count",
                    message=(
                        f"The close filled; waiting to verify the "
                        f"{target_symbol} position ({detail})."
                    ),
                    max_attempts=_FOLLOWON_MAX_ERRORS,
                    failure_message=(
                        f"Could not verify the {target_symbol} position after "
                        f"{_FOLLOWON_MAX_ERRORS} attempts: {detail}"
                    ),
                    filled_qty=filled_qty,
                )
                return
            if target_pos < 0:
                self._settle_followon_plan(
                    plan_id,
                    "failed",
                    (
                        f"{target_symbol} is already short {abs(target_pos):g} "
                        "— cover that first so the buy cannot flip it long."
                    ),
                    close_filled_qty=filled_qty,
                )
                return

        short_entry = next_side == "short"
        if short_entry and next_qty != float(int(next_qty)):
            truncated = float(int(next_qty))
            notes.append(
                f"whole shares — Alpaca does not short fractions "
                f"({next_qty:g} → {truncated:g})"
            )
            next_qty = truncated

        if next_qty <= 0 or (short_entry and next_qty < 1):
            self._settle_followon_plan(
                plan_id,
                "failed",
                f"Nothing to send — the next ticket sized to {next_qty:g} shares.",
                close_filled_qty=filled_qty,
            )
            return

        cancelled_for_loop = False
        with self.lock:
            live = self.followon_plans.get(plan_id)
            if live is None or live.get("status") != "waiting":
                return
            if self.loop_running:
                live["status"] = "cancelled"
                live["message"] = (
                    "Strategy loop started — the next ticket was not placed."
                )
                live["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
                cancelled_for_loop = True
            else:
                live["status"] = "placing"
                live["message"] = "The close filled — sending the next ticket…"
        # Persist the uncertainty boundary immediately before touching the
        # broker. A restart then marks this interrupted instead of sending it
        # twice from a stale waiting state.
        self._persist_followon_plans()
        if cancelled_for_loop:
            return

        order_side = OrderSide.SELL if short_entry else OrderSide.BUY
        next_client_order_id = (
            f"followon-{plan_id}-{str(plan.get('close_order_id') or '')}"
        )[:128]
        tif = str(plan.get("time_in_force") or "day").strip().lower()
        extended = bool(plan.get("extended_hours"))
        submit_kwargs: dict[str, Any] = {
            "order_type": order_type,
            "stop_loss_pct": 0.0,
            "short_entry": short_entry,
            "client_order_id": next_client_order_id,
            "time_in_force": tif,
            "extended_hours": extended,
        }
        if order_type == "limit":
            submit_kwargs["limit_price"] = limit_price
        try:
            submitted, _ = service.submit_manual_order(
                target_symbol,
                next_qty,
                order_side,
                **submit_kwargs,
            )
        except Exception as exc:
            self._settle_followon_plan(
                plan_id,
                "failed",
                (
                    "The close filled but the next ticket was rejected: "
                    f"{humanize_alpaca_error(exc)}"
                ),
                close_filled_qty=filled_qty,
            )
            return

        order_id = str(getattr(submitted, "id", "") or "")
        verb = "Short" if short_entry else "Buy"
        price_bit = (
            "at market"
            if order_type == "market"
            else f"@ ${float(limit_price):.2f} (limit)"
        )
        detail = f"{verb} {next_qty:g} {target_symbol} {price_bit}"
        if notes:
            detail += " · " + ", ".join(notes)
        self._settle_followon_plan(
            plan_id,
            "placed",
            detail,
            next_order_id=order_id,
            next_qty=next_qty,
            close_filled_qty=filled_qty,
        )

        history_side = "short" if short_entry else "buy"
        history_price = (
            "at market"
            if order_type == "market"
            else f"@ ${float(limit_price):.2f}"
        )
        self._record_trade_history(
            [
                {
                    "symbol": target_symbol,
                    "signal": "sell" if short_entry else "buy",
                    "side": history_side,
                    "order_type": order_type,
                    "price": limit_price,
                    "limit_price": limit_price,
                    "order_qty": next_qty,
                    "order_id": order_id,
                    "engine": "manual",
                    "mode": "manual",
                    "session": session_info.get("session"),
                    "reason": (
                        f"Next ticket after {close_side} "
                        f"{plan['close_order_id'][:8]} filled {filled_qty:g} "
                        f"{close_symbol} — {history_side} {next_qty:g} "
                        f"{target_symbol} {history_price}"
                    ),
                }
            ]
        )

    def followon_plans_payload(self, symbol: str = "") -> list[dict[str, Any]]:
        symbol = str(symbol or "").upper().strip()
        with self.lock:
            plans = [dict(p) for p in self.followon_plans.values()]
        if symbol:
            plans = [
                p
                for p in plans
                if p.get("symbol") == symbol or p.get("target_symbol") == symbol
            ]
        plans.sort(key=lambda p: p.get("created_at") or 0.0, reverse=True)
        now = time.time()
        for plan in plans:
            plan.pop("error_count", None)
            if plan.get("status") == "waiting":
                started = self._followon_send_window_started(plan)
                plan["wait_started"] = started
                expires_at = plan.get("expires_at")
                if started and expires_at not in (None, ""):
                    plan["seconds_left"] = max(0.0, float(expires_at) - now)
                else:
                    plan["seconds_left"] = None
        return plans

    def cancel_followon_plan(self, plan_id: str) -> dict[str, Any]:
        """Disarm a waiting next ticket. The close itself is left alone."""
        plan_id = str(plan_id or "").strip()
        if not plan_id:
            raise ValueError("Plan id is required")
        with self.lock:
            plan = self.followon_plans.get(plan_id)
            if plan is None:
                raise ValueError(f"No next-ticket plan {plan_id}")
            if plan.get("status") != "waiting":
                raise ValueError(
                    f"This next ticket is already {plan.get('status')} — "
                    "nothing to cancel."
                )
            plan["status"] = "cancelled"
            plan["message"] = (
                "Cancelled before the next ticket was sent."
                if self._followon_send_window_started(plan)
                else "Cancelled before the close filled."
            )
            plan["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
            result = dict(plan)
        self._persist_followon_plans()
        result.pop("error_count", None)
        return result

    # ------------------------------------------------------------------
    # Dip hunt — re-enter cheaper after a protective stop fills
    #
    # A buy ticket can carry a standing instruction: "when the stop fills,
    # wait up to N minutes for a further D% drop, or buy immediately if that
    # drop prints first." Alpaca has no such order class, so the desk watches
    # the stop and places the cheaper limit itself. See `bot.dip_hunt`.
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_dip_hunt_request(
        raw: dict[str, Any] | None, *, side: str
    ) -> dict[str, Any] | None:
        """Validate the dip-hunt block that rides along with a buy ticket."""
        if not raw:
            return None
        if not bool(raw.get("enabled")):
            return None
        if str(side or "").lower() != "buy":
            raise ValueError(
                "Dip hunt after stop-loss only attaches to a buy — a sell has "
                "no stop-out to hunt from."
            )

        wait_raw = raw.get("wait_minutes")
        if wait_raw in (None, ""):
            wait_minutes = float(WAIT_MINUTES_DEFAULT)
        else:
            try:
                wait_minutes = float(wait_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Dip hunt wait must be a number of minutes") from exc
        wait_minutes = max(1.0, min(float(WAIT_MINUTES_MAX), wait_minutes))

        dip_raw = raw.get("dip_pct")
        if dip_raw in (None, ""):
            dip_pct = float(DIP_PCT_DEFAULT)
        else:
            try:
                dip_pct = float(dip_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Dip hunt drop must be a percentage") from exc
        if dip_pct <= 0:
            raise ValueError("Dip hunt needs a drop greater than 0%")
        dip_pct = min(float(DIP_PCT_MAX), dip_pct)

        return {
            "enabled": True,
            "wait_minutes": wait_minutes,
            "dip_pct": dip_pct,
        }

    def _persist_dip_hunt_plans(self) -> None:
        with self.lock:
            snapshot = {pid: dict(p) for pid, p in self.dip_hunt_plans.items()}
        try:
            dip_hunt_store.save_plans(snapshot, paper=paper_mode_from_env())
        except Exception as exc:  # pragma: no cover - disk issues are non-fatal
            logger.warning("could not persist dip-hunt plans: %s", exc)

    def bootstrap_dip_hunt_plans(self) -> int:
        """Reload hunts left behind by a previous run and resume watching them."""
        paper = paper_mode_from_env()
        try:
            stored = dip_hunt_store.load_plans(paper=paper)
        except Exception as exc:
            logger.warning("could not load dip-hunt plans: %s", exc)
            return 0
        if not stored:
            return 0
        resumed = 0
        with self.lock:
            for plan_id, plan in stored.items():
                if str(plan.get("status") or "") in _DIP_HUNT_ACTIVE:
                    resumed += 1
                self.dip_hunt_plans[plan_id] = plan
            self._dip_hunt_seq = max(
                self._dip_hunt_seq, dip_hunt_store.max_sequence(self.dip_hunt_plans)
            )
        if resumed:
            logger.info("resumed %d dip-hunt plan(s) from disk", resumed)
            self._start_dip_hunt_watcher()
        self._persist_dip_hunt_plans()
        return resumed

    def _register_dip_hunt_plan(
        self,
        *,
        symbol: str,
        buy_order_id: str,
        qty: float,
        stop_loss_pct: float,
        take_profit_r: float,
        stop_order_id: str | None,
        take_profit_order_id: str | None,
        plan: dict[str, Any],
        cycle: int = 1,
    ) -> dict[str, Any]:
        now = time.time()
        status = "watching_stop" if stop_order_id else "watching_entry"
        with self.lock:
            self._dip_hunt_seq += 1
            entry: dict[str, Any] = {
                "id": f"dh-{self._dip_hunt_seq}",
                "symbol": symbol,
                "buy_order_id": str(buy_order_id),
                "stop_order_id": str(stop_order_id or "") or None,
                "take_profit_order_id": str(take_profit_order_id or "") or None,
                "qty": float(qty),
                "stop_loss_pct": float(stop_loss_pct),
                "take_profit_r": float(take_profit_r or 0),
                "wait_minutes": float(plan["wait_minutes"]),
                "dip_pct": float(plan["dip_pct"]),
                "cycle": int(cycle),
                "created_at": now,
                "created_at_iso": datetime.fromtimestamp(now).isoformat(
                    timespec="seconds"
                ),
                "status": status,
                "message": (
                    "Watching the protective stop."
                    if status == "watching_stop"
                    else "Waiting for the buy to fill so the stop can be watched."
                ),
                "stop_fill_price": None,
                "target_price": None,
                "hunt_started_at": None,
                "lowest_price": None,
                "dip_buy_order_id": None,
                "dip_buy_qty": None,
                "error_count": 0,
            }
            self.dip_hunt_plans[entry["id"]] = entry
            self._trim_dip_hunt_plans_locked()
            snapshot = dict(entry)
        self._persist_dip_hunt_plans()
        self._start_dip_hunt_watcher()
        return snapshot

    def _trim_dip_hunt_plans_locked(self, keep: int = 40) -> None:
        if len(self.dip_hunt_plans) <= keep:
            return
        finished = [
            (p.get("created_at") or 0.0, pid)
            for pid, p in self.dip_hunt_plans.items()
            if p.get("status") not in _DIP_HUNT_ACTIVE
        ]
        finished.sort()
        for _, pid in finished[: len(self.dip_hunt_plans) - keep]:
            self.dip_hunt_plans.pop(pid, None)

    def _start_dip_hunt_watcher(self) -> None:
        with self.lock:
            alive = (
                self._dip_hunt_thread is not None and self._dip_hunt_thread.is_alive()
            )
            if alive:
                return
            self._dip_hunt_stop.clear()
            thread = threading.Thread(
                target=self._bound_worker(self._dip_hunt_worker),
                name="dip-hunt-watcher",
                daemon=True,
            )
            self._dip_hunt_thread = thread
        thread.start()

    def _dip_hunt_worker(self) -> None:
        while not self._dip_hunt_stop.is_set():
            with self.lock:
                pending = [
                    pid
                    for pid, p in self.dip_hunt_plans.items()
                    if p.get("status") in _DIP_HUNT_ACTIVE
                ]
            if not pending:
                return
            for plan_id in pending:
                if self._dip_hunt_stop.is_set():
                    return
                try:
                    self._advance_dip_hunt_plan(plan_id)
                except Exception:
                    logger.exception("Dip-hunt plan %s failed to advance", plan_id)
            self._dip_hunt_stop.wait(_DIP_HUNT_POLL_SECONDS)

    def _settle_dip_hunt_plan(
        self, plan_id: str, status: str, message: str, **extra
    ) -> None:
        with self.lock:
            plan = self.dip_hunt_plans.get(plan_id)
            if plan is None:
                return
            plan["status"] = status
            plan["message"] = message
            plan["settled_at_iso"] = datetime.now().isoformat(timespec="seconds")
            plan.update(extra)
        self._persist_dip_hunt_plans()

    def _note_dip_hunt(self, plan_id: str, message: str, **extra) -> None:
        with self.lock:
            plan = self.dip_hunt_plans.get(plan_id)
            if plan is None or plan.get("status") not in _DIP_HUNT_ACTIVE:
                return
            plan["message"] = message
            plan.update(extra)
        self._persist_dip_hunt_plans()

    def _advance_dip_hunt_plan(self, plan_id: str) -> None:
        with self.lock:
            plan = self.dip_hunt_plans.get(plan_id)
            if plan is None or plan.get("status") not in _DIP_HUNT_ACTIVE:
                return
            snapshot = dict(plan)
            loop_running = self.loop_running

        if loop_running:
            try:
                self._cancel_parked_dip_buy(snapshot)
            except Exception as exc:
                self._note_dip_hunt(
                    plan_id,
                    "Strategy loop started, but the parked dip buy could not be "
                    f"cancelled; the desk will retry: {exc}",
                )
                return
            self._settle_dip_hunt_plan(
                plan_id,
                "cancelled",
                "Strategy loop started — the dip hunt was disarmed.",
            )
            return

        status = str(snapshot.get("status") or "")
        try:
            service = AlpacaService(self._base_config())
            if status == "placing":
                # A leftover claim: the send is not in flight on this poll
                # (the worker is single-threaded). Recover so the hunt can
                # retry or follow a ticket that did land.
                if snapshot.get("dip_buy_order_id"):
                    status = "awaiting_fill"
                    self._note_dip_hunt(
                        plan_id,
                        "Rechecking the dip buy after a mid-send interruption.",
                        status="awaiting_fill",
                    )
                elif snapshot.get("target_price") and snapshot.get("hunt_started_at"):
                    status = "hunting"
                    self._note_dip_hunt(
                        plan_id,
                        "Retrying the dip buy — the last send did not finish.",
                        status="hunting",
                    )
                else:
                    return
                with self.lock:
                    live = self.dip_hunt_plans.get(plan_id)
                    if live is None or live.get("status") not in _DIP_HUNT_ACTIVE:
                        return
                    snapshot = dict(live)
            if status in {"watching_entry", "watching_stop"}:
                self._advance_dip_hunt_watch_stop(plan_id, snapshot, service)
            elif status == "hunting":
                self._advance_dip_hunt_hunting(plan_id, snapshot, service)
            elif status == "awaiting_fill":
                self._advance_dip_hunt_awaiting_fill(plan_id, snapshot, service)
        except Exception as exc:
            self._note_plan_broker_error(
                plans=self.dip_hunt_plans,
                persist=self._persist_dip_hunt_plans,
                settle=self._settle_dip_hunt_plan,
                plan_id=plan_id,
                exc=exc,
                what="dip-hunt order",
                max_errors=_DIP_HUNT_MAX_ERRORS,
                live_statuses=_DIP_HUNT_ACTIVE,
            )

    def _advance_dip_hunt_watch_stop(
        self,
        plan_id: str,
        plan: dict[str, Any],
        service: AlpacaService,
    ) -> None:
        symbol = str(plan["symbol"])
        buy_id = str(plan.get("buy_order_id") or "")
        stop_id = str(plan.get("stop_order_id") or "")
        tp_id = str(plan.get("take_profit_order_id") or "")
        expects_tp = float(plan.get("take_profit_r") or 0) > 0

        if buy_id and (not stop_id or (expects_tp and not tp_id)):
            buy = self._follow_replaced_order(
                service, service.get_order_snapshot(buy_id)
            )
            successor = str(buy.get("id") or "").strip()
            if successor and successor != buy_id:
                self._note_dip_hunt(
                    plan_id,
                    "The buy was replaced — watching the new one.",
                    buy_order_id=successor,
                )
                buy_id = successor
                plan["buy_order_id"] = successor
            if str(buy.get("status") or "").lower() == "replaced":
                return
            if not stop_id:
                stop_id = str(buy.get("stop_order_id") or "")
            if not tp_id:
                tp_id = str(buy.get("take_profit_order_id") or "")
            if buy.get("is_terminal") and float(buy.get("filled_qty") or 0) <= 0:
                if self._desk_plan_should_wait_for_successor(buy, buy_id):
                    return
                self._settle_dip_hunt_plan(
                    plan_id,
                    "cancelled",
                    "The buy ended without a fill — nothing to hunt after.",
                )
                return
            # Do not bind an unrelated stop already resting on the same symbol
            # while this entry is still unfilled. The fallback is only safe
            # once the parent buy is known to have created a position.
            if (
                not stop_id
                and buy.get("is_terminal")
                and float(buy.get("filled_qty") or 0) > 0
            ):
                stop_id = service.open_protective_stop_id(symbol) or ""
            if stop_id or tp_id:
                self._note_dip_hunt(
                    plan_id,
                    "Watching the protective stop.",
                    stop_order_id=stop_id or None,
                    take_profit_order_id=tp_id or None,
                    status="watching_stop" if stop_id else plan.get("status"),
                )
                plan["stop_order_id"] = stop_id or None
                plan["take_profit_order_id"] = tp_id or None

        if tp_id:
            tp = service.get_order_snapshot(tp_id)
            if str(tp.get("status") or "").lower() == "filled":
                self._settle_dip_hunt_plan(
                    plan_id,
                    "cancelled",
                    "Take-profit filled — the dip hunt ended with the trade.",
                )
                return

        if stop_id:
            stop = self._follow_replaced_order(
                service, service.get_order_snapshot(stop_id)
            )
            successor = str(stop.get("id") or "").strip()
            if successor and successor != stop_id:
                self._note_dip_hunt(
                    plan_id,
                    "The stop was replaced — watching the new one.",
                    stop_order_id=successor,
                )
                stop_id = successor
                plan["stop_order_id"] = successor
            if str(stop.get("status") or "").lower() == "replaced":
                return
            filled = float(stop.get("filled_qty") or 0)
            if str(stop.get("status") or "").lower() == "filled" or (
                stop.get("is_terminal") and filled > 0
            ):
                fill_price = float(stop.get("filled_avg_price") or 0) or float(
                    stop.get("stop_price") or 0
                )
                self._begin_dip_hunt(
                    plan_id, plan, fill_price=fill_price, filled_qty=filled
                )
                with self.lock:
                    live = self.dip_hunt_plans.get(plan_id)
                    if live is None or live.get("status") != "hunting":
                        return
                    snapshot = dict(live)
                self._advance_dip_hunt_hunting(plan_id, snapshot, service)
                return
            if stop.get("is_terminal") and filled <= 0:
                if self._desk_plan_should_wait_for_successor(stop, stop_id):
                    return
                position = float(service.get_position_qty(symbol) or 0)
                if position > 0:
                    discovered = service.open_protective_stop_id(symbol)
                    if discovered and discovered != stop_id:
                        self._note_dip_hunt(
                            plan_id,
                            "The stop was replaced — watching the new one.",
                            stop_order_id=discovered,
                        )
                    return
                if self._rebind_dip_hunt_to_working_buy(
                    plan_id, plan, service, buy_id=buy_id
                ):
                    return
                self._settle_dip_hunt_plan(
                    plan_id,
                    "cancelled",
                    "Position closed without a stop-out — the dip hunt ended.",
                )
                return
            return

        position = float(service.get_position_qty(symbol) or 0)
        if position <= 0 and buy_id:
            buy = self._follow_replaced_order(
                service, service.get_order_snapshot(buy_id)
            )
            successor = str(buy.get("id") or "").strip()
            if successor and successor != buy_id:
                self._note_dip_hunt(
                    plan_id,
                    "The buy was replaced — watching the new one.",
                    buy_order_id=successor,
                )
                buy_id = successor
            if self._desk_plan_should_wait_for_successor(buy, buy_id):
                return
            if buy.get("is_terminal") and float(buy.get("filled_qty") or 0) > 0:
                self._settle_dip_hunt_plan(
                    plan_id,
                    "cancelled",
                    "Position closed without a watched stop-out — the dip hunt ended.",
                )

    def _rebind_dip_hunt_to_working_buy(
        self,
        plan_id: str,
        plan: dict[str, Any],
        service: AlpacaService,
        *,
        buy_id: str,
    ) -> bool:
        """Keep the hunt alive when the stop died but the entry is still working.

        A cancel-and-rewrite of the buy kills the OTO stop. Treat that as
        watching the successor entry, not as a stop-out that never happened.
        """
        buy_id = str(buy_id or "").strip()
        if not buy_id:
            return False
        try:
            buy = self._follow_replaced_order(
                service, service.get_order_snapshot(buy_id)
            )
        except Exception:
            return False
        successor = str(buy.get("id") or "").strip()
        extras: dict[str, Any] = {
            "stop_order_id": None,
            "status": "watching_entry",
        }
        if successor and successor != buy_id:
            extras["buy_order_id"] = successor
            buy_id = successor
            plan["buy_order_id"] = successor
        if self._desk_plan_should_wait_for_successor(buy, buy_id):
            self._note_dip_hunt(
                plan_id,
                "The buy is being edited — still waiting for the new ticket.",
                **extras,
            )
            return True
        if buy and not buy.get("is_terminal"):
            self._note_dip_hunt(
                plan_id,
                "Waiting for the buy to fill so the stop can be watched.",
                **extras,
            )
            return True
        if buy.get("is_terminal") and float(buy.get("filled_qty") or 0) > 0:
            return False
        if self._is_rewriting_order(str(plan.get("buy_order_id") or buy_id)):
            self._note_dip_hunt(
                plan_id,
                "The buy is being edited — still waiting for the new ticket.",
                **extras,
            )
            return True
        return False

    def _begin_dip_hunt(
        self,
        plan_id: str,
        plan: dict[str, Any],
        *,
        fill_price: float,
        filled_qty: float,
    ) -> None:
        if fill_price <= 0:
            self._settle_dip_hunt_plan(
                plan_id,
                "failed",
                "The stop filled but reported no price, so no dip target could be set.",
            )
            return
        try:
            target = target_buy_price(fill_price, float(plan["dip_pct"]))
        except ValueError as exc:
            self._settle_dip_hunt_plan(plan_id, "failed", str(exc))
            return
        qty = float(filled_qty or plan.get("qty") or 0)
        now = time.time()
        self._note_dip_hunt(
            plan_id,
            (
                f"Stop filled @ ${fill_price:.2f} — hunting a "
                f"{float(plan['dip_pct']):g}% drop to ${target:.2f}."
            ),
            status="hunting",
            stop_fill_price=fill_price,
            target_price=target,
            hunt_started_at=now,
            lowest_price=fill_price,
            qty=qty if qty > 0 else plan.get("qty"),
        )

    def _advance_dip_hunt_hunting(
        self,
        plan_id: str,
        plan: dict[str, Any],
        service: AlpacaService,
    ) -> None:
        started = float(plan.get("hunt_started_at") or 0)
        target = float(plan.get("target_price") or 0)
        if started <= 0 or target <= 0:
            self._settle_dip_hunt_plan(
                plan_id, "failed", "Dip hunt lost its target price."
            )
            return

        mark: float | None = None
        try:
            quote = service.get_mark_price(str(plan["symbol"]))
            mark = float(quote.get("price") or 0) or None
        except Exception:
            mark = None

        extras: dict[str, Any] = {}
        if mark is not None:
            lowest = float(plan.get("lowest_price") or mark)
            extras["lowest_price"] = min(lowest, mark)

        action = hunt_action(
            mark=mark,
            target=target,
            started_at=started,
            wait_minutes=float(plan["wait_minutes"]),
            now=time.time(),
        )
        if action == "wait":
            wait_left = max(
                0.0, started + float(plan["wait_minutes"]) * 60.0 - time.time()
            )
            self._note_dip_hunt(
                plan_id,
                (
                    f"Hunting ${target:.2f} "
                    f"({float(plan['dip_pct']):g}% below the stop-out). "
                    f"{wait_left / 60.0:.0f}m of the wait left"
                    + (f" · mark ${mark:.2f}" if mark else "")
                    + "."
                ),
                **extras,
            )
            return
        early = action == "buy_now"
        self._place_dip_hunt_buy(
            plan_id,
            plan,
            service,
            early=early,
            mark=mark,
        )

    def _place_dip_hunt_buy(
        self,
        plan_id: str,
        plan: dict[str, Any],
        service: AlpacaService,
        *,
        early: bool,
        mark: float | None,
    ) -> None:
        with self.lock:
            live = self.dip_hunt_plans.get(plan_id)
            if live is None or live.get("status") != "hunting":
                return
            live["status"] = "placing"
            live["message"] = (
                "Dip printed — sending the cheaper buy…"
                if early
                else "Wait elapsed — parking a limit at the dip target…"
            )
        # Persist the claim before money goes on the wire. If the process dies
        # after Alpaca accepts the order but before its id is recorded, startup
        # will surface an interrupted state instead of sending a duplicate.
        self._persist_dip_hunt_plans()

        symbol = str(plan["symbol"])
        target = float(plan["target_price"])
        buy_qty = float(plan.get("qty") or 0)
        stop_pct = float(plan.get("stop_loss_pct") or 0)
        tp_r = float(plan.get("take_profit_r") or 0)

        try:
            session_info = service.market_session()
        except Exception:
            session_info = {"is_open": True, "session": "unknown"}

        if not session_info.get("is_open") and buy_qty != float(int(buy_qty)):
            buy_qty = float(int(buy_qty))

        whole_qty, can_attach = whole_qty_for_attached_stop(buy_qty)
        if can_attach:
            buy_qty = whole_qty
        if buy_qty <= 0:
            self._settle_dip_hunt_plan(
                plan_id,
                "failed",
                f"Nothing to buy back — the dip hunt sized to {buy_qty:g} shares.",
            )
            return

        take_profit_price = None
        if tp_r > 0 and stop_pct > 0:
            stop_preview = service.stop_price_for_entry(target, pct=stop_pct)
            if stop_preview is not None and target > stop_preview:
                take_profit_price = normalize_stock_order_price(
                    target + (target - stop_preview) * tp_r,
                    field="take_profit_price",
                )

        try:
            submitted, oto_stop = service.submit_manual_order(
                symbol,
                buy_qty,
                OrderSide.BUY,
                order_type="limit",
                limit_price=target,
                time_in_force="gtc",
                stop_loss_pct=stop_pct if can_attach else 0.0,
                take_profit_price=take_profit_price,
                client_order_id=(
                    f"dip-hunt-{plan_id}-c{int(plan.get('cycle') or 1)}"
                ),
            )
        except ValueError as exc:
            text = str(exc)
            if "closed" in text.lower() or "weekend" in text.lower():
                self._note_dip_hunt(
                    plan_id,
                    "Market closed — the dip buy waits for the open.",
                    status="hunting",
                )
                return
            self._settle_dip_hunt_plan(
                plan_id,
                "failed",
                f"The dip buy was rejected: {humanize_alpaca_error(exc)}",
            )
            return
        except Exception as exc:
            self._settle_dip_hunt_plan(
                plan_id,
                "failed",
                f"The dip buy was rejected: {humanize_alpaca_error(exc)}",
            )
            return

        order_id = str(getattr(submitted, "id", "") or "")
        legs = service.exit_leg_ids(submitted)
        stop_id = (oto_stop or {}).get("id") or legs.get("stop_order_id")
        tp_id = (oto_stop or {}).get("take_profit_order_id") or legs.get(
            "take_profit_order_id"
        )
        why = (
            "Dip printed before the wait ended"
            if early
            else f"Wait of {float(plan['wait_minutes']):g}m elapsed"
        )
        detail = (
            f"{why} — limit buy {buy_qty:g} {symbol} @ ${target:.2f}"
            + (f" (mark ${mark:.2f})" if mark else "")
        )
        self._note_dip_hunt(
            plan_id,
            detail,
            status="awaiting_fill",
            dip_buy_order_id=order_id,
            dip_buy_qty=buy_qty,
            buy_order_id=order_id,
            stop_order_id=stop_id,
            take_profit_order_id=tp_id,
        )
        self._record_trade_history(
            [
                {
                    "symbol": symbol,
                    "signal": "buy",
                    "side": "buy",
                    "order_type": "limit",
                    "price": target,
                    "limit_price": target,
                    "order_qty": buy_qty,
                    "order_id": order_id,
                    "engine": "manual",
                    "mode": "manual",
                    "session": session_info.get("session"),
                    "reason": (
                        f"Dip hunt cycle {int(plan.get('cycle') or 1)} after stop-out "
                        f"@ ${float(plan.get('stop_fill_price') or 0):.2f} — "
                        f"limit buy {buy_qty:g} @ ${target:.2f}"
                    ),
                }
            ]
        )

    def _advance_dip_hunt_awaiting_fill(
        self,
        plan_id: str,
        plan: dict[str, Any],
        service: AlpacaService,
    ) -> None:
        order_id = str(plan.get("dip_buy_order_id") or plan.get("buy_order_id") or "")
        if not order_id:
            self._settle_dip_hunt_plan(
                plan_id, "failed", "The dip buy was sent but no order id was recorded."
            )
            return
        watched_id = order_id
        order = self._follow_replaced_order(
            service, service.get_order_snapshot(order_id)
        )
        successor = str(order.get("id") or "").strip()
        if successor and successor != order_id:
            self._note_dip_hunt(
                plan_id,
                "The dip buy was replaced — watching the new one.",
                dip_buy_order_id=successor,
                buy_order_id=successor,
            )
            order_id = successor
            plan["dip_buy_order_id"] = successor
            plan["buy_order_id"] = successor
        if self._desk_plan_should_wait_for_successor(order, watched_id):
            return
        filled = float(order.get("filled_qty") or 0)
        if str(order.get("status") or "").lower() == "filled" or (
            order.get("is_terminal") and filled > 0
        ):
            stop_id = str(order.get("stop_order_id") or plan.get("stop_order_id") or "")
            if not stop_id:
                stop_id = service.open_protective_stop_id(str(plan["symbol"])) or ""
            tp_id = str(
                order.get("take_profit_order_id")
                or plan.get("take_profit_order_id")
                or ""
            )
            cycle = int(plan.get("cycle") or 1) + 1
            self._note_dip_hunt(
                plan_id,
                (
                    f"Dip buy filled — cycle {cycle} is watching the new stop "
                    f"({float(plan['dip_pct']):g}% hunt still armed)."
                ),
                status="watching_stop" if stop_id else "watching_entry",
                buy_order_id=order_id,
                stop_order_id=stop_id or None,
                take_profit_order_id=tp_id or None,
                qty=filled or plan.get("qty"),
                cycle=cycle,
                dip_buy_order_id=None,
                stop_fill_price=None,
                target_price=None,
                hunt_started_at=None,
                lowest_price=None,
            )
            return
        if order.get("is_terminal") and filled <= 0:
            self._settle_dip_hunt_plan(
                plan_id,
                "cancelled",
                "The dip buy ended without a fill — the hunt stopped.",
            )
            return
        target = float(plan.get("target_price") or 0)
        self._note_dip_hunt(
            plan_id,
            f"Dip buy resting @ ${target:.2f} — waiting for a fill.",
        )

    def _cancel_parked_dip_buy(
        self,
        plan: dict[str, Any],
        *,
        service: AlpacaService | None = None,
    ) -> None:
        order_id = str(plan.get("dip_buy_order_id") or "")
        if not order_id:
            return
        (service or AlpacaService(self._base_config())).cancel_order(order_id)

    def dip_hunt_plans_payload(self, symbol: str = "") -> list[dict[str, Any]]:
        symbol = str(symbol or "").upper().strip()
        with self.lock:
            plans = [dict(p) for p in self.dip_hunt_plans.values()]
        if symbol:
            plans = [p for p in plans if p.get("symbol") == symbol]
        plans.sort(key=lambda p: p.get("created_at") or 0.0, reverse=True)
        now = time.time()
        for plan in plans:
            plan.pop("error_count", None)
            started = plan.get("hunt_started_at")
            if plan.get("status") == "hunting" and started:
                wait_end = float(started) + float(plan.get("wait_minutes") or 0) * 60.0
                plan["seconds_left"] = max(0.0, wait_end - now)
                plan["hunting_seconds"] = max(0.0, now - float(started))
        return plans

    def _cancel_dip_hunt_plans_for_orders(
        self,
        *,
        order_ids: set[str] | None = None,
        all_watching: bool = False,
        exclude_order_ids: set[str] | None = None,
        message: str,
    ) -> int:
        """Disarm hunts whose buy, stop, or parked dip-buy was cancelled.

        A hunt that is already looking for the dip (stop already filled) is
        left armed on cancel-all — it has no resting ticket to cancel.
        """
        wanted = {str(value or "").strip() for value in (order_ids or set())}
        wanted.discard("")
        excluded = {
            str(value or "").strip() for value in (exclude_order_ids or set())
        }
        excluded.discard("")
        cancelled = 0
        now_iso = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            for plan in self.dip_hunt_plans.values():
                status = str(plan.get("status") or "")
                if status not in _DIP_HUNT_CANCELLABLE:
                    continue
                ids = {
                    str(plan.get("buy_order_id") or ""),
                    str(plan.get("stop_order_id") or ""),
                    str(plan.get("dip_buy_order_id") or ""),
                }
                ids.discard("")
                if ids & excluded:
                    continue
                if status == "hunting":
                    if not (ids & wanted):
                        continue
                elif not all_watching and not (ids & wanted):
                    continue
                plan["status"] = "cancelled"
                plan["message"] = message
                plan["settled_at_iso"] = now_iso
                cancelled += 1
        if cancelled:
            self._persist_dip_hunt_plans()
        return cancelled

    def cancel_dip_hunt_plan(self, plan_id: str) -> dict[str, Any]:
        """Disarm a live hunt. A parked dip-buy is cancelled; the stop is not."""
        plan_id = str(plan_id or "").strip()
        if not plan_id:
            raise ValueError("Plan id is required")
        with self.lock:
            plan = self.dip_hunt_plans.get(plan_id)
            if plan is None:
                raise ValueError(f"No dip-hunt plan {plan_id}")
            status = str(plan.get("status") or "")
            if status not in _DIP_HUNT_CANCELLABLE:
                raise ValueError(
                    f"This dip hunt is already {status} — nothing to cancel."
                )
            snapshot = dict(plan)
            if status == "awaiting_fill":
                # Claim the plan so the watcher cannot simultaneously turn a
                # fill into the next cycle while cancellation is in flight.
                plan["status"] = "placing"
                plan["message"] = "Cancelling the parked dip buy…"
            else:
                plan["status"] = "cancelled"
                plan["message"] = "Cancelled by the user."
                plan["settled_at_iso"] = datetime.now().isoformat(
                    timespec="seconds"
                )
        if snapshot.get("status") == "awaiting_fill":
            self._persist_dip_hunt_plans()
            try:
                self._cancel_parked_dip_buy(snapshot)
            except Exception as exc:
                with self.lock:
                    live = self.dip_hunt_plans.get(plan_id)
                    if live is not None and live.get("status") == "placing":
                        live["status"] = "awaiting_fill"
                        live["message"] = (
                            "Could not cancel the parked dip buy; it is still "
                            f"being watched: {exc}"
                        )
                self._persist_dip_hunt_plans()
                self._start_dip_hunt_watcher()
                raise ValueError(
                    f"Could not cancel the parked dip buy: {exc}"
                ) from exc
            with self.lock:
                live = self.dip_hunt_plans.get(plan_id)
                if live is not None:
                    live["status"] = "cancelled"
                    live["message"] = "Cancelled by the user."
                    live["settled_at_iso"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
        self._persist_dip_hunt_plans()
        with self.lock:
            result = dict(self.dip_hunt_plans[plan_id])
        result.pop("error_count", None)
        return result

    def start_loop(self) -> None:
        self._require_live_execution()
        with self.lock:
            if self.loop_running or (self._thread and self._thread.is_alive()):
                return
            self.loop_running = True
            self.loop_stopping = False
            self.loop_started_at = time.time()
            self.loop_last_duration_seconds = None
            self._begin_loop_session_locked()
            self._stop.clear()
        self._thread = threading.Thread(
            target=self._bound_worker(self._loop_worker), daemon=True
        )
        self._thread.start()

    def _set_last_ai_results(self, results: list[dict[str, Any]] | None) -> None:
        """Publish cycle rows unless Stop already dropped the watchlist."""
        with self.lock:
            if self._cycle_cancelled():
                return
            self.last_ai_results = results

    def stop_loop(self) -> None:
        self._stop.set()
        with self.lock:
            self.last_ai_results = None
            self.loop_stopping = self.loop_running

    def _cycle_cancelled(self) -> bool:
        """Cooperative cancel signal handed to the engines mid-cycle.

        Only a running loop can be cancelled — a manual "Run once" must finish
        even when a previous Stop left the event set.
        """
        return self.loop_running and self._stop.is_set()

    def _loop_worker(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_once()
                    with self.lock:
                        wait = self.settings.poll_seconds
                except Exception as exc:
                    with self.lock:
                        self.error = str(exc)
                        failed_poll = self._poll_seq or None
                        self._append_session_error_locked(str(exc), failed_poll)
                    wait = 30
                # Interruptible wait — Stop wakes this immediately.
                if self._stop.wait(timeout=max(wait, 1)):
                    break
        finally:
            with self.lock:
                duration = None
                if self.loop_started_at is not None:
                    duration = max(0.0, time.time() - self.loop_started_at)
                    self.loop_last_duration_seconds = duration
                self.loop_started_at = None
                self.loop_running = False
                self.loop_stopping = False
                # Drop cycle rows so the Auto Trade watchlist doesn't linger
                # after Stop. Run once still populates this independently.
                self.last_ai_results = None
                self._finish_loop_session_locked(duration)

    def positions_overview(self) -> dict[str, Any]:
        """Aggregate all open positions and account metrics for the Positions page."""
        service = AlpacaService(self._base_config())
        positions = service.get_all_positions()

        account = None
        try:
            account = service.account_summary()
        except Exception:
            account = None

        with self.lock:
            cached_account = dict(self.account or {}) if self.account else {}
            loop_running = self.loop_running

        # Merge live summary if available, fallback to cached
        acc_equity = float((account or {}).get("equity") or cached_account.get("equity") or 0.0)
        acc_cash = float((account or {}).get("cash") or cached_account.get("cash") or 0.0)
        acc_bp = float((account or {}).get("buying_power") or cached_account.get("buying_power") or 0.0)

        total_market_value = sum(abs(float(p.get("market_value") or 0.0)) for p in positions)
        total_cost_basis = sum(abs(float(p.get("cost_basis") or 0.0)) for p in positions)
        total_unrealized_pl = sum(float(p.get("unrealized_pl") or 0.0) for p in positions)
        total_intraday_pl = sum(float(p.get("unrealized_intraday_pl") or 0.0) for p in positions)

        total_unrealized_pct = None
        if total_cost_basis > 0:
            total_unrealized_pct = round((total_unrealized_pl / total_cost_basis) * 100.0, 3)

        # Today's move measured against what the book was worth at yesterday's
        # close, so the KPI reads as a return rather than a bare dollar figure.
        total_intraday_pct = None
        prior_value = total_market_value - total_intraday_pl
        if prior_value > 0:
            total_intraday_pct = round((total_intraday_pl / prior_value) * 100.0, 3)

        # Share of equity actually at risk in the market; the rest is cash.
        invested_pct = (
            round((total_market_value / acc_equity) * 100.0, 2) if acc_equity > 0 else 0.0
        )

        # Fetch open orders to attach protection status (Stop Loss / Take Profit)
        open_orders_map = {}
        try:
            open_orders_map = service.get_open_orders_summary()
        except Exception:
            open_orders_map = {}

        # Keep allocation on a real 0–100 scale. When gross exposure exceeds
        # equity (margin/short books), equity-denominated weights sum above
        # 100% and the CSS has to shrink them, making the visual proportions lie.
        base_denom = max(acc_equity, total_market_value, 1.0)
        winners = 0
        losers = 0
        for p in positions:
            mv = abs(float(p.get("market_value") or 0.0))
            p["allocation_pct"] = round((mv / base_denom) * 100.0, 2) if base_denom > 0 else 0.0
            upl = float(p.get("unrealized_pl") or 0.0)
            # Keep summary counters aligned with the page filters: any positive
            # or negative broker-reported P&L belongs to the corresponding set.
            if upl > 0:
                winners += 1
            elif upl < 0:
                losers += 1

            sym = p.get("symbol", "")
            sym_orders = open_orders_map.get(sym, [])
            pos_side = str(p.get("side") or "long").lower()
            exit_side = "sell" if pos_side == "long" else "buy"
            # Only a stop on the exit side protects this position — a resting
            # buy-stop beside a long is an entry, and badging it "SL" would
            # tell the reader they are covered when they are not.
            stops = [
                o
                for o in sym_orders
                if (o.get("is_stop") or o.get("stop_price"))
                and str(o.get("side") or exit_side).lower() == exit_side
            ]
            limits = [
                o
                for o in sym_orders
                if not o.get("is_stop")
                and o.get("limit_price")
                and str(o.get("side") or "").lower() == exit_side
            ]
            p["has_stop_loss"] = len(stops) > 0
            p["stop_loss_price"] = stops[0].get("stop_price") if stops else None
            p["has_take_profit"] = len(limits) > 0
            p["take_profit_price"] = limits[0].get("limit_price") if limits else None
            p["open_orders_count"] = len(sym_orders)

            # How much room is left before the stop fires — the number that
            # actually tells you whether a holding is protected or cornered.
            p["stop_distance_pct"] = None
            stop_px = p["stop_loss_price"]
            current_px = float(p.get("current_price") or 0.0)
            if stop_px and current_px > 0:
                gap = (current_px - float(stop_px)) / current_px * 100.0
                # A short's stop sits above the mark, so flip the sign to keep
                # "distance to stop" positive for a healthy position either way.
                p["stop_distance_pct"] = round(gap if pos_side == "long" else -gap, 2)

        # Sort positions by market_value desc by default
        positions.sort(key=lambda x: abs(float(x.get("market_value") or 0.0)), reverse=True)

        return {
            "positions": positions,
            "count": len(positions),
            "winners_count": winners,
            "losers_count": losers,
            "total_market_value": round(total_market_value, 2),
            "total_cost_basis": round(total_cost_basis, 2),
            "total_unrealized_pl": round(total_unrealized_pl, 2),
            "total_unrealized_pct": total_unrealized_pct,
            "total_intraday_pl": round(total_intraday_pl, 2),
            "total_intraday_pct": total_intraday_pct,
            "invested_pct": invested_pct,
            "account": {
                "equity": acc_equity,
                "cash": acc_cash,
                "buying_power": acc_bp,
                "status": (account or {}).get("status") or cached_account.get("status") or "ACTIVE",
                "currency": (account or {}).get("currency") or "USD",
            },
            "loop_running": loop_running,
            "paper": paper_mode_from_env(),
            "trading_mode": "paper" if paper_mode_from_env() else "live",
            "live_authorized": bool(self._live_session_authorized)
            and not paper_mode_from_env(),
        }

    def position_lots(self, symbol: str, *, lookback_days: int = 365) -> dict[str, Any]:
        """The individual share parcels behind one open position, oldest first.

        The blotter only ever showed one blended average entry, which hides the
        thing that decides an exit: a position built from three buys can be
        half in profit and half under water at the same mark.
        """
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise ValueError("Symbol is required")

        service = AlpacaService(self._base_config())
        position = next(
            (p for p in service.get_all_positions() if p.get("symbol") == sym),
            None,
        )
        if position is None:
            raise ValueError(f"No open position for {sym}")

        days = max(1, min(int(lookback_days or 365), 1825))
        after = datetime.now(timezone.utc) - timedelta(days=days)
        # A single 500-order page is not enough for active names; walk the same
        # paginated window History uses so FIFO does not invent carried lots
        # from fills that were merely off-page.
        window = service.fetch_closed_order_window(
            after=after,
            symbols=[sym],
            page_limit=_FIFO_WINDOW_LIMIT,
            max_pages=_FIFO_WINDOW_MAX_PAGES,
        )
        raw_lots = AlpacaService.open_lots_from_orders(
            window["orders"], symbols={sym}
        ).get(sym, [])

        side = str(position.get("side") or "long").lower()
        want_direction = -1 if side == "short" else 1
        raw_lots = [lot for lot in raw_lots if lot["direction"] == want_direction]

        held = abs(float(position.get("qty") or 0.0))
        current_px = float(position.get("current_price") or 0.0)
        avg_entry = float(position.get("avg_entry_price") or 0.0)

        # Alpaca is the authority on how many shares are held; the fill window
        # is only ever a reconstruction. Reconcile the two rather than letting
        # the lot list quietly disagree with the row that opened it.
        recon_qty = sum(lot["qty"] for lot in raw_lots)
        while recon_qty - held > 1e-6 and raw_lots:
            # Sells older than the window retired the oldest parcels first.
            trim = min(raw_lots[0]["qty"], recon_qty - held)
            raw_lots[0]["qty"] = round(raw_lots[0]["qty"] - trim, 9)
            recon_qty -= trim
            if raw_lots[0]["qty"] <= 1e-9:
                raw_lots.pop(0)

        carried_qty = round(held - recon_qty, 9)
        if carried_qty > 1e-6:
            # Entries older than the window. Price them at the blended average
            # and flag them, instead of dropping shares the user does hold.
            raw_lots.insert(
                0,
                {
                    "direction": want_direction,
                    "qty": carried_qty,
                    "price": avg_entry,
                    "order_id": "",
                    "opened_at": None,
                    "order_type": "",
                    "estimated": True,
                },
            )

        now = datetime.now(timezone.utc)
        lots: list[dict[str, Any]] = []
        for index, lot in enumerate(raw_lots):
            qty = round(float(lot["qty"]), 9)
            price = float(lot["price"] or 0.0)
            cost_basis = qty * price
            market_value = qty * current_px
            unrealized = (
                (current_px - price) * qty
                if side != "short"
                else (price - current_px) * qty
            )
            age_days = None
            opened_at = lot.get("opened_at")
            if opened_at:
                try:
                    opened = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=timezone.utc)
                    age_days = max(0.0, round((now - opened).total_seconds() / 86400.0, 2))
                except ValueError:
                    age_days = None
            lots.append(
                {
                    "index": index + 1,
                    "qty": qty,
                    "price": round(price, 4),
                    "cost_basis": round(cost_basis, 2),
                    "market_value": round(market_value, 2),
                    "unrealized_pl": round(unrealized, 2),
                    "unrealized_pct": (
                        round(unrealized / cost_basis * 100.0, 3)
                        if cost_basis > 0
                        else None
                    ),
                    "weight_pct": round(qty / held * 100.0, 2) if held > 0 else 0.0,
                    "opened_at": opened_at,
                    "age_days": age_days,
                    "order_id": lot.get("order_id") or "",
                    "order_type": lot.get("order_type") or "",
                    "estimated": bool(lot.get("estimated")),
                }
            )

        total_cost = sum(lot["cost_basis"] for lot in lots)
        total_qty = sum(lot["qty"] for lot in lots)
        return {
            "symbol": sym,
            "side": side,
            "qty": held,
            "current_price": current_px or None,
            "avg_entry_price": avg_entry or None,
            "lot_count": len(lots),
            "lots": lots,
            "total_qty": round(total_qty, 9),
            "total_cost_basis": round(total_cost, 2),
            "total_unrealized_pl": round(
                sum(lot["unrealized_pl"] for lot in lots), 2
            ),
            # A blended average that disagrees with Alpaca's means the window
            # missed fills; the page says so rather than presenting a guess.
            "weighted_avg_price": (
                round(total_cost / total_qty, 4) if total_qty > 0 else None
            ),
            "estimated_qty": round(max(0.0, carried_qty), 9),
            "lookback_days": days,
            "window_truncated": bool(window["truncated"]),
        }

    def _require_manual_book_control(self) -> None:
        """Manual closes must not race the Auto Trade loop for the same book."""
        if self.loop_running:
            raise ValueError(
                "Stop the Auto Trade loop before closing positions manually."
            )

    def close_single_position(
        self,
        symbol: str,
        *,
        qty: float | None = None,
        percentage: float | None = None,
        cancel_orders: bool | None = None,
    ) -> dict[str, Any]:
        """Liquidate a single open position (full or partial)."""
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            raise ValueError("Symbol is required")
        self._require_manual_book_control()
        self._require_live_execution()
        service = AlpacaService(self._base_config())
        result = service.close_position(
            symbol,
            qty=qty,
            percentage=percentage,
            cancel_orders=cancel_orders,
        )
        try:
            self.refresh_account()
            self.refresh_quote(force=True)
        except Exception:
            pass
        return result

    def close_batch_positions(
        self, symbols: list[str], cancel_orders: bool = True
    ) -> dict[str, Any]:
        """Liquidate selected open positions."""
        if not symbols:
            raise ValueError("No symbols provided for batch close")
        self._require_manual_book_control()
        self._require_live_execution()
        service = AlpacaService(self._base_config())
        results = service.close_batch_positions(
            symbols, cancel_orders=cancel_orders
        )
        try:
            self.refresh_account()
            self.refresh_quote(force=True)
        except Exception:
            pass
        failed = sum(self._close_result_failed(result) for result in results)
        accepted = len(results) - failed
        return {
            # Kept for API compatibility; this now excludes rejected attempts.
            "closed_count": accepted,
            "submitted_count": accepted,
            "failed_count": failed,
            "results": results,
        }

    def close_all_positions(self, cancel_orders: bool = True) -> dict[str, Any]:
        """Liquidate all open positions."""
        self._require_manual_book_control()
        self._require_live_execution()
        service = AlpacaService(self._base_config())
        results = service.close_all_positions(
            cancel_orders=cancel_orders
        )
        try:
            self.refresh_account()
            self.refresh_quote(force=True)
        except Exception:
            pass
        failed = sum(self._close_result_failed(result) for result in results)
        accepted = len(results) - failed
        return {
            "closed_count": accepted,
            "submitted_count": accepted,
            "failed_count": failed,
            "results": results,
        }

    @staticmethod
    def _close_result_failed(result: Any) -> bool:
        """Recognize both batch exceptions and Alpaca's 207 status rows."""
        if not isinstance(result, dict):
            return False
        if result.get("ok") is False:
            return True
        status = str(result.get("status") or "").strip().lower()
        if status in {"failed", "rejected", "canceled", "cancelled", "expired"}:
            return True
        try:
            return int(status) >= 400
        except (TypeError, ValueError):
            return False


STATE = AppState()
