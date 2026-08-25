"""Live paper rotator for long/short regime-impulse strategy."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable

from alpaca.trading.enums import OrderSide

from bot.client import AlpacaService
from bot.config import Config
from bot.desk_risk import (
    arm_protective_stop,
    atr_from_bars,
    manage_open_position,
    stop_distance_for,
)
from bot.pair_presets import get_preset, normalize_weak_side
from bot.pair_strategy import PairTarget, SoxRegimeImpulseStrategy, parse_pair_symbols
from bot.options_overlay import apply_pair_options_overlay
from bot.strategy import Signal, StrategyResult

logger = logging.getLogger(__name__)


class PairTradingBot:
    """Keep equity in at most one of {long, short}; flatten the other / cash.

    Live sizing deploys available cash into the target leg after flattening
    (research backtests rotate full equity). Signals always use daily bars.
    """

    def __init__(self, config: Config, service: AlpacaService | None = None) -> None:
        if config.bar_timeframe != "1Day":
            logger.warning(
                "Pair mode forces 1Day bars (configured %s)", config.bar_timeframe
            )
            config = replace(config, bar_timeframe="1Day")
        self.config = config
        self.service = service or AlpacaService(config)
        long_s, short_s = parse_pair_symbols(
            config.symbols,
            long_symbol=config.pair_long_symbol,
            short_symbol=config.pair_short_symbol,
        )
        self.strategy = SoxRegimeImpulseStrategy(
            sma_period=config.pair_sma_period,
            lookback=config.pair_lookback,
            impulse_pct=config.pair_impulse_pct,
            weak_side=normalize_weak_side(config.pair_weak_side),
            long_symbol=long_s,
            short_symbol=short_s,
        )
        self._engine = "pair"

    def run_once(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        # `should_stop` is accepted for a uniform bot interface but deliberately
        # unused: a pair cycle is one rotation (close one leg, open the other)
        # and bailing halfway would leave the book lopsided. It is short anyway
        # — no LLM round trip, just bars plus at most two orders.
        long_s = self.strategy.long_symbol
        short_s = self.strategy.short_symbol
        need = max(self.strategy.bars_needed, 80)
        bars = self.service.get_bars(long_s, limit=need)
        decision = self.strategy.evaluate(bars)

        long_qty = self.service.get_position_qty(long_s)
        short_qty = self.service.get_position_qty(short_s)
        target_sym = decision.symbol

        try:
            mark = self.service.get_mark_price(long_s)
            display_price = mark["price"]
            session = mark["session"]
            price_source = mark.get("source")
            price_asof = mark.get("asof")
        except Exception:
            mark = {}
            display_price = decision.price
            session = "?"
            price_source = None
            price_asof = None

        logger.info(
            "PAIR | target=%s %s | mark=%.2f sma=%.2f lookback=%.1f%% | "
            "pos %s=%.4f %s=%.4f | %s",
            decision.target.value,
            target_sym or "CASH",
            display_price,
            decision.sma,
            decision.lookback_return_pct,
            long_s,
            long_qty,
            short_s,
            short_qty,
            decision.reason,
        )

        actions: list[dict[str, Any]] = []
        primary: dict[str, Any] = {
            "symbol": target_sym or long_s,
            "signal": Signal.HOLD.value,
            "price": display_price,
            "bar_close": decision.price,
            "session": session,
            "is_open": mark.get("is_open"),
            "price_source": price_source,
            "price_asof": price_asof,
            "fast_sma": decision.lookback_return_pct,
            "slow_sma": decision.sma,
            "reason": decision.reason,
            "position": 0.0,
            "engine": self._engine,
            "pair_target": decision.target.value,
            "long_symbol": long_s,
            "short_symbol": short_s,
            "actions": actions,
        }

        session_info = self.service.market_session()
        if session_info.get("session") == "closed":
            logger.warning(
                "skipping pair rotation — market closed until %s",
                session_info.get("next_open"),
            )
            primary["reason"] += " | skipped: market closed"
            primary["position"] = (
                self.service.get_position_qty(target_sym) if target_sym else 0.0
            )
            apply_pair_options_overlay(self.config, self.service, primary)
            return {"primary": primary, "results": [primary]}

        # Flatten legs that are not the target.
        for sym, qty in ((long_s, long_qty), (short_s, short_qty)):
            if qty <= 0:
                continue
            if target_sym and sym == target_sym:
                continue
            if self.service.has_open_orders(sym):
                logger.warning("skipping close — open orders already exist for %s", sym)
                primary["reason"] += f" | skipped close {sym}: open orders"
                continue
            cancelled = self.service.cancel_open_stop_orders(sym)
            if cancelled:
                logger.info("cancelled %s protective stop(s) before SELL %s", cancelled, sym)
            try:
                actions.append(
                    self._close(sym, qty, reason=decision.reason)
                )
            except Exception as exc:
                logger.exception("pair close failed for %s", sym)
                primary["reason"] += f" | close {sym} failed: {exc}"
                primary["error"] = str(exc)
                primary["actions"] = actions
                apply_pair_options_overlay(self.config, self.service, primary)
                return {"primary": primary, "results": [primary]}

        # Open target if flat (after flatten cash is available).
        if target_sym:
            if self.service.has_open_orders(target_sym):
                logger.warning(
                    "skipping open — open orders already exist for %s", target_sym
                )
                primary["reason"] += f" | skipped open {target_sym}: open orders"
            else:
                held = self.service.get_position_qty(target_sym)
                if held <= 0:
                    try:
                        actions.append(
                            self._open(
                                target_sym, reason=decision.reason
                            )
                        )
                    except Exception as exc:
                        logger.exception("pair open failed for %s", target_sym)
                        primary["reason"] += f" | open {target_sym} failed: {exc}"
                        primary["error"] = str(exc)

        primary["signal"] = self._signal_from_actions(decision, actions)
        primary["position"] = (
            self.service.get_position_qty(target_sym) if target_sym else 0.0
        )
        primary["actions"] = actions
        # Risk-engine ATR stop (or flat % fallback) on the live long leg.
        if target_sym and primary["position"] > 0:
            self._protect_long(target_sym, primary)
        apply_pair_options_overlay(self.config, self.service, primary)
        return {"primary": primary, "results": [primary]}

    @staticmethod
    def _signal_from_actions(decision, actions: list[dict[str, Any]]) -> str:
        if any(a.get("side") == "buy" for a in actions):
            return Signal.BUY.value
        if any(a.get("side") == "sell" for a in actions):
            return Signal.SELL.value
        if decision.target is PairTarget.CASH:
            return Signal.HOLD.value
        return Signal.HOLD.value

    def _close(self, symbol: str, qty: float, *, reason: str) -> dict[str, Any]:
        qty_out = self._qty_for_session(qty)
        if qty_out is None:
            return {
                "symbol": symbol,
                "side": "sell",
                "qty": qty,
                "reason": reason + " | skipped: qty",
            }
        order = self.service.submit_order(symbol, qty_out, OrderSide.SELL)
        return {
            "symbol": symbol,
            "side": "sell",
            "qty": qty_out,
            "order_id": str(getattr(order, "id", "") or ""),
            "reason": reason,
        }

    def _open(self, symbol: str, *, reason: str) -> dict[str, Any]:
        mark = self.service.get_mark_price(symbol)
        price = float(mark["price"])
        qty = self._size_qty(price)
        qty_out = self._qty_for_session(qty)
        if qty_out is None:
            return {
                "symbol": symbol,
                "side": "buy",
                "qty": qty,
                "price": price,
                "reason": reason + " | skipped: qty",
            }
        order = self.service.submit_order(symbol, qty_out, OrderSide.BUY)
        return {
            "symbol": symbol,
            "side": "buy",
            "qty": qty_out,
            "price": price,
            "order_id": str(getattr(order, "id", "") or ""),
            "reason": reason,
        }

    def _size_qty(self, price: float) -> float:
        """Prefer deploying available cash (post-flatten) to match full-equity BT."""
        mark = float(price or 0)
        if mark <= 0:
            raise ValueError("Need a positive mark price to size pair entry")
        cash = equity = 0.0
        try:
            summary = self.service.account_summary()
            cash = float(summary.get("cash") or 0)
            equity = float(summary.get("equity") or 0)
        except Exception:
            pass
        # After a flatten, cash can lag briefly — fall back to equity when flat.
        deploy = cash
        if deploy <= mark and equity > mark:
            deploy = equity
        if deploy > mark:
            # Leave a small buffer so the order isn't rejected on residual holds.
            return (deploy * 0.99) / mark
        return self.config.order_qty_for_price(mark)

    def _qty_for_session(self, qty: float) -> float | None:
        session_info = self.service.market_session()
        if session_info.get("is_open"):
            return float(qty)
        whole = int(qty)
        if whole < 1:
            logger.warning("skipping order — need whole shares outside regular hours")
            return None
        if whole != qty:
            logger.info("truncating qty %.4f → %d for extended hours", qty, whole)
        return float(whole)

    def _protect_long(self, symbol: str, payload: dict[str, Any]) -> None:
        """Manage trail/TP then arm a protective stop from ATR when available."""
        price = float(payload.get("price") or 0)
        if price <= 0:
            try:
                price = float(self.service.get_mark_price(symbol)["price"])
                payload["price"] = price
            except Exception:
                price = 0.0
        atr = None
        try:
            bars = self.service.get_bars(symbol, limit=40)
            atr = atr_from_bars(bars)
        except Exception as exc:
            logger.warning("pair ATR unavailable for %s: %s", symbol, exc)
        stop_distance = stop_distance_for(self.config, price, atr)
        payload["stop_distance"] = stop_distance or None
        detail = self.service.get_position_detail(symbol)
        qty = float(detail.get("qty") or payload.get("position") or 0)
        if qty > 0:
            managed = manage_open_position(
                self.config,
                self.service,
                symbol,
                side="long",
                entry=detail.get("avg_entry"),
                price=price,
                qty=qty,
                stop_distance=stop_distance,
                current_stop=self.service.current_stop_price(symbol),
            )
            if managed:
                payload["managed"] = managed
            if managed.get("scale_out"):
                qty = float(self.service.get_position_qty(symbol) or 0)
                payload["position"] = qty
        if qty > 0 and (
            stop_distance > 0
            or (self.config.stop_loss_pct and self.config.stop_loss_pct > 0)
        ):
            arm_protective_stop(self.service, symbol, payload, stop_distance)

    def as_strategy_result(self, payload: dict[str, Any]) -> StrategyResult:
        raw = str(payload.get("signal") or Signal.HOLD.value).lower()
        try:
            signal = Signal(raw)
        except ValueError:
            signal = Signal.HOLD
        return StrategyResult(
            signal=signal,
            price=float(payload.get("bar_close") or payload.get("price") or 0),
            fast_sma=float(payload.get("fast_sma") or 0),
            slow_sma=float(payload.get("slow_sma") or 0),
            reason=str(payload.get("reason") or ""),
        )
