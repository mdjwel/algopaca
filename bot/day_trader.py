"""Order execution and session-managed live paper trading bot for Day Trading."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, time as dtime, timezone
from typing import Any, Callable

from alpaca.trading.enums import OrderSide

from bot.ai_risk import entry_gates, reversal_gate
from bot.client import AlpacaService
from bot.config import Config
from bot.day_ai import DayAiBrain
from bot.day_presets import DEFAULT_PRESET_ID as DEFAULT_DAY_PRESET_ID
from bot.day_presets import get_preset as get_day_preset
from bot.day_strategy import (
    ET_TZ,
    MARKET_OPEN_ET,
    DayTradingStrategy,
    compute_intraday_vwap,
    compute_opening_range,
)
from bot.desk_risk import (
    arm_protective_stop,
    atr_from_bars,
    manage_open_position,
    risk_qty_for,
    stop_distance_for,
)
from bot.options_overlay import apply_options_overlays
from bot.strategy import Signal, StrategyResult

logger = logging.getLogger(__name__)

def _now_et(now: datetime | None = None) -> datetime:
    """`now` in US/Eastern — the clock every intraday session rule is written in."""
    moment = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        if moment.tzinfo is None:
            return moment.replace(tzinfo=ZoneInfo(ET_TZ))
        return moment.astimezone(ZoneInfo(ET_TZ))
    except Exception:
        return moment


def _session_close_et(session_info: dict[str, Any]) -> dtime | None:
    """Today's real closing bell from the broker clock, so half-days work too."""
    raw = session_info.get("next_close")
    if not raw:
        return None
    try:
        closes = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=timezone.utc)
    return _now_et(closes).time()


# Daily trade execution tracking across sessions, keyed by (account scope, symbol).
# The scope keeps one desk's trade cap from consuming another's in a multi-user
# deployment, where every user's cycle runs in this same process.
_DAILY_TRADES: dict[tuple[str, str], int] = {}
_LAST_TRADE_DATE: str = ""


def _trading_day(now: datetime | None = None) -> str:
    """Calendar day of the US trading session, in Eastern time."""
    return _now_et(now).strftime("%Y-%m-%d")


def reset_daily_trades() -> None:
    global _LAST_TRADE_DATE
    _DAILY_TRADES.clear()
    _LAST_TRADE_DATE = _trading_day()


def _check_and_reset_daily_trades() -> None:
    global _LAST_TRADE_DATE
    today_str = _trading_day()
    if _LAST_TRADE_DATE != today_str:
        _DAILY_TRADES.clear()
        _LAST_TRADE_DATE = today_str


def get_daily_trades_count(symbol: str, scope: str = "") -> int:
    _check_and_reset_daily_trades()
    return _DAILY_TRADES.get((scope, symbol.upper()), 0)


def increment_daily_trades_count(symbol: str, scope: str = "") -> int:
    """Count one *new* intraday position. Exits are not capped, so they do not count."""
    _check_and_reset_daily_trades()
    key = (scope, symbol.upper())
    _DAILY_TRADES[key] = _DAILY_TRADES.get(key, 0) + 1
    return _DAILY_TRADES[key]


def trade_scope_for(config: Any) -> str:
    """Stable, non-reversible key for the brokerage account a config trades."""
    api_key = str(getattr(config, "api_key", "") or "")
    mode = "paper" if bool(getattr(config, "paper", True)) else "live"
    if not api_key:
        return mode
    return f"{mode}:{hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:12]}"


class DayTradingBot:
    """Intraday Day Trading bot with VWAP, ORB, EMA momentum, and automatic EOD square-off."""

    def __init__(
        self,
        config: Config,
        service: AlpacaService | None = None,
        approval_handler: Callable[..., dict[str, Any]] | None = None,
        ai_brain: DayAiBrain | None = None,
    ) -> None:
        self.config = config
        self.service = service or AlpacaService(config)
        self.approval_handler = approval_handler

        preset = get_day_preset(getattr(config, "day_preset", DEFAULT_DAY_PRESET_ID))
        sub_mode = getattr(config, "day_sub_mode", preset.sub_mode) or preset.sub_mode
        ema_fast = getattr(config, "day_ema_fast", preset.ema_fast) or preset.ema_fast
        ema_slow = getattr(config, "day_ema_slow", preset.ema_slow) or preset.ema_slow
        orb_minutes = getattr(config, "day_orb_minutes", preset.orb_minutes) or preset.orb_minutes
        side = getattr(config, "day_side", preset.side) or preset.side

        use_ai_confirm = getattr(config, "day_use_ai_confirm", preset.use_ai_confirm)
        if use_ai_confirm is None:
            use_ai_confirm = preset.use_ai_confirm
        self.use_ai_confirm = bool(use_ai_confirm)
        self.ai_min_confidence = float(
            getattr(config, "day_ai_min_confidence", preset.ai_min_confidence)
            or preset.ai_min_confidence
        )
        self.ai_brain = ai_brain or DayAiBrain(self.config, self.service)

        self.strategy = DayTradingStrategy(
            sub_mode=sub_mode,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            orb_minutes=orb_minutes,
            side=side,
        )
        self._engine = "day"
        self._trade_scope = trade_scope_for(config)

    def run_once(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        symbols = self.config.primary_symbols()
        stopping = should_stop or (lambda: False)
        open_positions = 0
        day_pl_pct: float | None = None
        equity = 0.0

        try:
            account = self.service.account_summary()
            day_pl_pct = account.get("day_pl_pct")
            equity = float((account or {}).get("equity") or 0)
            open_positions = sum(
                1 for s in symbols if self.service.get_position_qty(s) != 0
            )
        except Exception as exc:
            logger.warning("could not read portfolio state: %s", exc)
            account = {}

        results: list[dict[str, Any]] = []
        for symbol in symbols:
            if stopping():
                logger.info("%s cycle stopped before %s", self._engine, symbol)
                break
            try:
                results.append(
                    self._run_symbol(
                        symbol,
                        open_positions=open_positions,
                        day_pl_pct=day_pl_pct,
                        equity=equity,
                    )
                )
                pos = float(results[-1].get("position") or 0)
                intent = results[-1].get("intent")
                if intent in {"open_long", "open_short"} and pos != 0:
                    open_positions += 1
                elif intent in {"close_long", "close_short"}:
                    open_positions = max(0, open_positions - 1)
            except Exception as exc:
                logger.exception("%s iteration failed for %s", self._engine, symbol)
                results.append(
                    {
                        "symbol": symbol,
                        "signal": Signal.HOLD.value,
                        "price": 0.0,
                        "fast_sma": 0.0,
                        "slow_sma": 0.0,
                        "reason": f"error: {exc}",
                        "error": str(exc),
                        "position": 0.0,
                        "engine": self._engine,
                    }
                )

        primary = results[0] if results else {
            "symbol": self.config.symbol,
            "signal": Signal.HOLD.value,
            "price": 0.0,
            "fast_sma": 0.0,
            "slow_sma": 0.0,
            "reason": "no symbols",
            "position": 0.0,
            "engine": self._engine,
        }
        apply_options_overlays(self.config, self.service, results)
        if results:
            primary = results[0]
        return {"primary": primary, "results": results}

    def _bars_needed(self) -> int:
        # Use the strategy's resolved slow EMA, not the raw config value: a preset
        # can override it, and the strategy re-clamps fast < slow.
        return max(self.strategy.ema_slow + 10, 50)

    def _is_market_session_open(self) -> tuple[bool, str, dict[str, Any]]:
        session_info = self.service.market_session()
        is_open = session_info.get("is_open", False)
        session_name = session_info.get("session", "closed")
        return is_open, session_name, session_info

    def _check_session_timing(
        self,
        now: datetime | None = None,
        *,
        close_time: dtime | None = None,
    ) -> tuple[bool, bool, str]:
        """Check intraday timing: (inside_open_buffer, inside_eod_flatten_window, message).

        ``close_time`` is the session's real closing bell in Eastern time; pass the
        broker clock's value so early-close days flatten at the right hour instead
        of assuming 16:00.
        """
        open_buffer = max(0, int(getattr(self.config, "day_open_buffer_mins", 15) or 0))
        eod_mins = max(0, int(getattr(self.config, "day_eod_flatten_mins", 15) or 0))
        eod_enabled = bool(getattr(self.config, "day_eod_flatten", True))

        current_time = _now_et(now).time()
        mkt_open = MARKET_OPEN_ET
        mkt_close = close_time or dtime(16, 0)

        def _clock(total_minutes: int) -> dtime:
            total = max(0, min(24 * 60 - 1, total_minutes))
            return dtime(total // 60, total % 60)

        open_minutes = mkt_open.hour * 60 + mkt_open.minute
        close_minutes = mkt_close.hour * 60 + mkt_close.minute

        # 1. Open buffer check — skip new entries for the first `open_buffer` minutes.
        buf_time = _clock(open_minutes + open_buffer)
        is_in_open_buf = open_buffer > 0 and mkt_open <= current_time < buf_time

        # 2. EOD flatten window check — square off in the last `eod_mins` minutes.
        flat_time = _clock(close_minutes - eod_mins)
        is_in_eod_window = (
            eod_enabled and eod_mins > 0 and flat_time <= current_time < mkt_close
        )

        msg = ""
        if is_in_eod_window:
            msg = f"EOD auto-flatten window active ({eod_mins}m before market close)"
        elif is_in_open_buf:
            msg = f"Market open buffer active (waiting {open_buffer}m after bell)"

        return is_in_open_buf, is_in_eod_window, msg

    def _run_symbol(
        self,
        symbol: str,
        *,
        open_positions: int = 0,
        day_pl_pct: float | None = None,
        equity: float = 0.0,
    ) -> dict[str, Any]:
        bars = self.service.get_bars(symbol, limit=self._bars_needed())
        result = self.strategy.evaluate(bars)
        atr = atr_from_bars(bars)
        vwap_data = compute_intraday_vwap(bars)
        orb_data = compute_opening_range(bars, self.strategy.orb_minutes)
        vwap_value = vwap_data.get("vwap")

        position = self.service.get_position_detail(symbol)
        position_qty = float(position.get("qty") or 0)

        try:
            mark = self.service.get_mark_price(symbol)
            display_price = float(mark["price"])
            session = mark["session"]
            price_source = mark.get("source")
            price_asof = mark.get("asof")
        except Exception:
            mark = {}
            display_price = result.price
            session = "?"
            price_source = None
            price_asof = None

        # Stop distance: use configured ATR multiplier or desk risk engine
        stop_mult = float(getattr(self.config, "day_stop_atr_mult", 1.5) or 1.5)
        stop_distance = (atr * stop_mult) if (atr and atr > 0) else stop_distance_for(self.config, display_price, atr)

        activity = {}
        try:
            activity = self.service.recent_activity(symbol)
        except Exception:
            pass

        # Check session timing: open buffer & EOD flatten, against the real bell.
        # A clock hiccup must not fail the whole cycle — fall back to "closed",
        # which blocks orders but still reports the signal.
        try:
            session_info = self.service.market_session()
        except Exception as exc:
            logger.warning("could not read market session for %s: %s", symbol, exc)
            session_info = {"session": "closed", "is_open": False}
        market_is_open = bool(session_info.get("is_open"))
        is_in_open_buf, is_in_eod_window, timing_msg = self._check_session_timing(
            close_time=_session_close_et(session_info) if market_is_open else None
        )

        # EOD AUTO-FLATTEN / SQUARE-OFF RULE. The window is pure clock maths, so it
        # also matches weekends and holidays — only square off with the market open.
        if is_in_eod_window and position_qty != 0 and market_is_open:
            logger.info("EOD auto-flatten triggered for %s (pos=%.4f)", symbol, position_qty)
            payload = {
                "symbol": symbol,
                "signal": Signal.SELL.value,
                "price": display_price,
                "bar_close": result.price,
                "session": session,
                "is_open": mark.get("is_open"),
                "price_source": price_source,
                "price_asof": price_asof,
                "fast_sma": result.fast_sma,
                "slow_sma": result.slow_sma,
                "vwap": vwap_value,
                "reason": f"EOD auto-flatten — square off intraday position before close | {timing_msg}",
                "position": position_qty,
                "stop_loss_pct": self.config.stop_loss_pct,
                "stop_distance": stop_distance or None,
                "managed": None,
                "engine": self._engine,
                "day_sub_mode": self.strategy.sub_mode,
                "trades_today": self._trades_today(symbol),
            }

            qty = self._qty_for_session(abs(position_qty))
            if qty is not None and qty > 0:
                if self.config.require_approval and self.approval_handler:
                    action = "SELL" if position_qty > 0 else "BUY"
                    appr = self.approval_handler(
                        symbol=symbol,
                        action=action,
                        qty=qty,
                        price=display_price,
                        reason=payload["reason"],
                        engine=self._engine,
                        cancel_stops=True,
                    )
                    payload["pending_approval_id"] = appr.get("id")
                    payload["approval_required"] = True
                    payload["intent"] = "close_long" if position_qty > 0 else "close_short"
                else:
                    self.service.cancel_open_stop_orders(symbol)
                    side = OrderSide.SELL if position_qty > 0 else OrderSide.BUY
                    order = self.service.submit_order(symbol, qty, side)
                    payload["order_id"] = str(order.id)
                    payload["order_qty"] = qty
                    payload["intent"] = "close_long" if position_qty > 0 else "close_short"
            return payload

        managed: dict[str, Any] = {}
        if position_qty != 0:
            managed = manage_open_position(
                self.config,
                self.service,
                symbol,
                side="long" if position_qty > 0 else "short",
                entry=position.get("avg_entry"),
                price=display_price,
                qty=abs(position_qty),
                stop_distance=stop_distance,
                current_stop=self.service.current_stop_price(symbol),
                take_profit_r=self._profit_target_r(),
            )
            if managed.get("scale_out"):
                position_qty = self.service.get_position_qty(symbol)

        logger.info(
            "%s | day | signal=%s mark=%.2f vwap=%.2f pos=%.4f | %s",
            symbol,
            result.signal.value,
            display_price,
            vwap_value if vwap_value is not None else display_price,
            position_qty,
            result.reason,
        )

        payload: dict[str, Any] = {
            "symbol": symbol,
            "signal": result.signal.value,
            "price": display_price,
            "bar_close": result.price,
            "session": session,
            "is_open": mark.get("is_open"),
            "price_source": price_source,
            "price_asof": price_asof,
            "fast_sma": result.fast_sma,
            "slow_sma": result.slow_sma,
            "vwap": vwap_value,
            "reason": result.reason,
            "position": position_qty,
            "stop_loss_pct": self.config.stop_loss_pct,
            "stop_distance": stop_distance or None,
            "managed": managed or None,
            "engine": self._engine,
            "day_sub_mode": self.strategy.sub_mode,
            "trades_today": self._trades_today(symbol),
        }

        if session_info.get("session") == "closed":
            logger.warning("skipping order — market closed until %s", session_info.get("next_open"))
            payload["reason"] += " | skipped: market closed"
            return payload

        # If HOLD signal or flat and inside open buffer
        if result.signal is Signal.HOLD:
            if position_qty > 0:
                arm_protective_stop(self.service, symbol, payload, stop_distance)
            return payload

        if self.service.has_open_orders(symbol):
            logger.warning("skipping — open orders already exist for %s", symbol)
            payload["reason"] += " | skipped: open orders"
            return payload

        # Check Max Daily Trades limit — caps new positions, never exits.
        max_daily_trades = max(0, int(getattr(self.config, "day_max_trades_per_day", 5) or 0))
        current_daily_trades = self._trades_today(symbol)
        if max_daily_trades > 0 and current_daily_trades >= max_daily_trades and position_qty == 0:
            logger.info("skipping new entry for %s — daily trade cap (%d) reached", symbol, max_daily_trades)
            payload["reason"] += f" | skipped: daily trade limit ({max_daily_trades}) reached"
            payload["risk_blocked"] = f"Max {max_daily_trades} trades/day reached"
            return payload

        # Check Open Buffer for new entries
        if is_in_open_buf and position_qty == 0:
            logger.info("skipping new entry for %s — market open buffer active", symbol)
            payload["reason"] += f" | skipped: {timing_msg}"
            return payload

        # No new intraday positions inside the square-off window — they would be
        # flattened on the next cycle anyway.
        if is_in_eod_window and position_qty == 0:
            logger.info("skipping new entry for %s — EOD flatten window active", symbol)
            payload["reason"] += f" | skipped: {timing_msg}"
            return payload

        gate_ctx = {"mark": mark, "activity": activity}

        # BUY SIGNAL (Open Long or Close Short)
        if result.signal is Signal.BUY:
            if position_qty < 0:
                # Cover Short
                target = abs(position_qty)
                qty = self._qty_for_session(target)
                if qty is None:
                    payload["reason"] += " | skipped: qty"
                    return payload
                if self.config.require_approval and self.approval_handler:
                    appr = self.approval_handler(
                        symbol=symbol,
                        action="BUY",
                        qty=qty,
                        price=display_price,
                        reason=result.reason,
                        engine=self._engine,
                        cancel_stops=True,
                    )
                    payload["pending_approval_id"] = appr.get("id")
                    payload["approval_required"] = True
                    payload["intent"] = "close_short"
                    payload["reason"] += " | Pending user approval"
                else:
                    self.service.cancel_open_stop_orders(symbol)
                    order = self.service.submit_order(symbol, qty, OrderSide.BUY)
                    payload["order_id"] = str(order.id)
                    payload["order_qty"] = qty
                    payload["intent"] = "close_short"
            elif position_qty == 0:
                # Open Long
                gate = entry_gates(
                    self.config,
                    gate_ctx,
                    open_positions=open_positions,
                    day_pl_pct=day_pl_pct,
                )
                if not gate:
                    payload["reason"] += f" | skipped: {gate.reason}"
                    payload["risk_blocked"] = gate.reason
                    return payload

                # AI Trade Confirmation & Real-time Catalyst Gate
                if self.use_ai_confirm:
                    ai_dec = self.ai_brain.evaluate_signal(
                        symbol=symbol,
                        signal=Signal.BUY,
                        trigger_price=display_price,
                        trigger_reason=result.reason,
                        bars=bars,
                        vwap_info=vwap_data,
                        orb_info=orb_data,
                    )
                    payload["ai_confirmed"] = ai_dec.confirm
                    payload["ai_confidence"] = ai_dec.confidence
                    payload["ai_thesis"] = ai_dec.thesis
                    payload["ai_thesis_en"] = ai_dec.thesis_en
                    payload["ai_risk_warning"] = ai_dec.risk_warning
                    payload["ai_action_bias"] = ai_dec.action_bias

                    if not ai_dec.confirm or ai_dec.confidence < self.ai_min_confidence:
                        conf_pct = int(ai_dec.confidence * 100)
                        min_pct = int(self.ai_min_confidence * 100)
                        logger.info(
                            "AI vetoed day buy for %s (conf=%d%% < min=%d%%): %s",
                            symbol,
                            conf_pct,
                            min_pct,
                            ai_dec.thesis or ai_dec.risk_warning,
                        )
                        payload["signal"] = Signal.HOLD.value
                        payload["reason"] = (
                            f"[AI Vetoed ({conf_pct}%)] {ai_dec.thesis or ai_dec.risk_warning or 'Low confidence'} | Orig: {result.reason}"
                        )
                        payload["risk_blocked"] = (
                            f"AI Veto ({conf_pct}% < {min_pct}%): {ai_dec.risk_warning or ai_dec.thesis}"
                        )
                        return payload
                    else:
                        conf_pct = int(ai_dec.confidence * 100)
                        payload["reason"] = f"[AI Confirmed {conf_pct}%] {ai_dec.thesis} | {result.reason}"
                try:
                    target = self._entry_qty(display_price, stop_distance, equity)
                except ValueError as exc:
                    payload["reason"] += f" | skipped: {exc}"
                    return payload
                qty = self._qty_for_session(target)
                if qty is None:
                    payload["reason"] += " | skipped: qty"
                    return payload
                entry_stop = (
                    round(display_price - float(stop_distance), 2)
                    if stop_distance and display_price > 0
                    else None
                )
                if self.config.require_approval and self.approval_handler:
                    appr = self.approval_handler(
                        symbol=symbol,
                        action="BUY",
                        qty=qty,
                        price=display_price,
                        stop_price=entry_stop,
                        stop_distance=stop_distance,
                        reason=result.reason,
                        engine=self._engine,
                    )
                    payload["pending_approval_id"] = appr.get("id")
                    payload["approval_required"] = True
                    payload["intent"] = "open_long"
                    payload["reason"] += " | Pending user approval"
                else:
                    order = self.service.submit_order(
                        symbol, qty, OrderSide.BUY, stop_price=entry_stop
                    )
                    payload["order_id"] = str(order.id)
                    payload["order_qty"] = qty
                    payload["intent"] = "open_long"
                    arm_protective_stop(self.service, symbol, payload, stop_distance)
                    payload["trades_today"] = increment_daily_trades_count(
                        symbol, self._trade_scope
                    )

        # SELL SIGNAL (Close Long or Open Short)
        elif result.signal is Signal.SELL:
            if position_qty > 0:
                # Close Long
                hold = reversal_gate(self.config, gate_ctx, confidence=1.0)
                if not hold:
                    payload["reason"] += f" | skipped: {hold.reason}"
                    payload["risk_blocked"] = hold.reason
                    arm_protective_stop(self.service, symbol, payload, stop_distance)
                    return payload
                try:
                    target = self.config.order_qty_for_price(display_price)
                except ValueError as exc:
                    payload["reason"] += f" | skipped: {exc}"
                    return payload
                qty = self._qty_for_session(min(position_qty, target))
                if qty is None:
                    payload["reason"] += " | skipped: qty"
                    return payload
                if self.config.require_approval and self.approval_handler:
                    appr = self.approval_handler(
                        symbol=symbol,
                        action="SELL",
                        qty=qty,
                        price=display_price,
                        reason=result.reason,
                        engine=self._engine,
                        cancel_stops=True,
                    )
                    payload["pending_approval_id"] = appr.get("id")
                    payload["approval_required"] = True
                    payload["intent"] = "close_long"
                    payload["reason"] += " | Pending user approval"
                else:
                    self.service.cancel_open_stop_orders(symbol)
                    order = self.service.submit_order(symbol, qty, OrderSide.SELL)
                    payload["order_id"] = str(order.id)
                    payload["order_qty"] = qty
                    payload["intent"] = "close_long"
            elif position_qty == 0 and self.strategy.side == "long_short":
                # Open Short (if long_short enabled)
                gate = entry_gates(
                    self.config,
                    gate_ctx,
                    open_positions=open_positions,
                    day_pl_pct=day_pl_pct,
                )
                if not gate:
                    payload["reason"] += f" | skipped: {gate.reason}"
                    payload["risk_blocked"] = gate.reason
                    return payload

                # AI Trade Confirmation & Real-time Catalyst Gate
                if self.use_ai_confirm:
                    ai_dec = self.ai_brain.evaluate_signal(
                        symbol=symbol,
                        signal=Signal.SELL,
                        trigger_price=display_price,
                        trigger_reason=result.reason,
                        bars=bars,
                        vwap_info=vwap_data,
                        orb_info=orb_data,
                    )
                    payload["ai_confirmed"] = ai_dec.confirm
                    payload["ai_confidence"] = ai_dec.confidence
                    payload["ai_thesis"] = ai_dec.thesis
                    payload["ai_thesis_en"] = ai_dec.thesis_en
                    payload["ai_risk_warning"] = ai_dec.risk_warning
                    payload["ai_action_bias"] = ai_dec.action_bias

                    if not ai_dec.confirm or ai_dec.confidence < self.ai_min_confidence:
                        conf_pct = int(ai_dec.confidence * 100)
                        min_pct = int(self.ai_min_confidence * 100)
                        logger.info(
                            "AI vetoed day short for %s (conf=%d%% < min=%d%%): %s",
                            symbol,
                            conf_pct,
                            min_pct,
                            ai_dec.thesis or ai_dec.risk_warning,
                        )
                        payload["signal"] = Signal.HOLD.value
                        payload["reason"] = (
                            f"[AI Vetoed ({conf_pct}%)] {ai_dec.thesis or ai_dec.risk_warning or 'Low confidence'} | Orig: {result.reason}"
                        )
                        payload["risk_blocked"] = (
                            f"AI Veto ({conf_pct}% < {min_pct}%): {ai_dec.risk_warning or ai_dec.thesis}"
                        )
                        return payload
                    else:
                        conf_pct = int(ai_dec.confidence * 100)
                        payload["reason"] = f"[AI Confirmed {conf_pct}%] {ai_dec.thesis} | {result.reason}"
                try:
                    target = self._entry_qty(display_price, stop_distance, equity)
                except ValueError as exc:
                    payload["reason"] += f" | skipped: {exc}"
                    return payload
                qty = self._qty_for_session(target)
                if qty is None:
                    payload["reason"] += " | skipped: qty"
                    return payload
                entry_stop = (
                    round(display_price + float(stop_distance), 2)
                    if stop_distance and display_price > 0
                    else None
                )
                if self.config.require_approval and self.approval_handler:
                    appr = self.approval_handler(
                        symbol=symbol,
                        action="SELL",
                        qty=qty,
                        price=display_price,
                        stop_price=entry_stop,
                        stop_distance=stop_distance,
                        reason=result.reason,
                        engine=self._engine,
                    )
                    payload["pending_approval_id"] = appr.get("id")
                    payload["approval_required"] = True
                    payload["intent"] = "open_short"
                    payload["reason"] += " | Pending user approval"
                else:
                    order = self.service.submit_order(
                        symbol, qty, OrderSide.SELL, stop_price=entry_stop
                    )
                    payload["order_id"] = str(order.id)
                    payload["order_qty"] = qty
                    payload["intent"] = "open_short"
                    payload["trades_today"] = increment_daily_trades_count(
                        symbol, self._trade_scope
                    )
            else:
                payload["reason"] += " | no action (flat account)"

        return payload

    def _trades_today(self, symbol: str) -> int:
        return get_daily_trades_count(symbol, self._trade_scope)

    def _profit_target_r(self) -> float | None:
        """Day Trading's own take-profit R, overriding the desk-wide setting."""
        try:
            target = float(getattr(self.config, "day_profit_target_r", 0) or 0)
        except (TypeError, ValueError):
            return None
        return target if target > 0 else None

    def _entry_qty(
        self, price: float, stop_distance: float, equity: float
    ) -> float:
        risk_qty = risk_qty_for(self.config, price, stop_distance, equity)
        if risk_qty is not None and risk_qty > 0:
            return float(risk_qty)
        return self.config.order_qty_for_price(price)

    def as_strategy_result(self, payload: dict[str, Any]) -> StrategyResult:
        return StrategyResult(
            signal=Signal(payload.get("signal", "hold")),
            price=float(payload.get("bar_close") or payload.get("price") or 0),
            fast_sma=float(payload.get("fast_sma") or 0),
            slow_sma=float(payload.get("slow_sma") or 0),
            reason=str(payload.get("reason") or ""),
        )

    def _qty_for_session(self, qty: float) -> float | None:
        session_info = self.service.market_session()
        if session_info.get("is_open"):
            return float(qty)
        whole = int(qty)
        if whole < 1:
            logger.warning("skipping order — need whole shares outside regular hours")
            return None
        return float(whole)
