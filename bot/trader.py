"""Order execution helpers for algorithmic paper bots (SMA + buy-the-dip)."""

from __future__ import annotations

import logging
from typing import Any, Callable

from alpaca.trading.enums import OrderSide

from bot.ai_risk import entry_gates, reversal_gate
from bot.client import AlpacaService
from bot.config import Config
from bot.desk_risk import (
    arm_protective_stop,
    atr_from_bars,
    manage_open_position,
    risk_qty_for,
    stop_distance_for,
)
from bot.dip_presets import get_preset as get_dip_preset
from bot.options_overlay import apply_options_overlays
from bot.strategy import BuyTheDipStrategy, Signal, SmaCrossoverStrategy, StrategyResult

logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(
        self,
        config: Config,
        service: AlpacaService | None = None,
        approval_handler: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.service = service or AlpacaService(config)
        self.approval_handler = approval_handler
        if config.strategy_mode == "dip":
            dip = get_dip_preset(config.dip_preset)
            self.strategy: SmaCrossoverStrategy | BuyTheDipStrategy = BuyTheDipStrategy(
                config.dip_rsi_buy,
                config.dip_rsi_sell,
                skip_bearish=config.dip_skip_bearish,
                use_lower_band=dip.use_lower_band,
            )
            self._engine = "dip"
        else:
            self.strategy = SmaCrossoverStrategy(config.fast_sma, config.slow_sma)
            self._engine = "sma"

    def run_once(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        symbols = self.config.primary_symbols()
        stopping = should_stop or (lambda: False)
        open_positions = 0
        day_pl_pct: float | None = None
        try:
            account = self.service.account_summary()
            day_pl_pct = account.get("day_pl_pct")
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
                        equity=float((account or {}).get("equity") or 0),
                    )
                )
                # Keep the same-cycle exposure picture accurate after fills.
                pos = float(results[-1].get("position") or 0)
                intent = results[-1].get("intent")
                if intent == "open_long" and pos > 0:
                    open_positions += 1
                elif intent == "close_long":
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
        if self._engine == "dip":
            return max(80, 40)
        return max(self.config.slow_sma + 5, 40)

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

        stop_distance = stop_distance_for(self.config, display_price, atr)
        activity = {}
        try:
            activity = self.service.recent_activity(symbol)
        except Exception:
            pass

        managed: dict[str, Any] = {}
        if position_qty > 0:
            managed = manage_open_position(
                self.config,
                self.service,
                symbol,
                side="long",
                entry=position.get("avg_entry"),
                price=display_price,
                qty=position_qty,
                stop_distance=stop_distance,
                current_stop=self.service.current_stop_price(symbol),
            )
            if managed.get("scale_out"):
                position_qty = self.service.get_position_qty(symbol)

        metric_a = "rsi" if self._engine == "dip" else "fast"
        metric_b = "%b" if self._engine == "dip" else "slow"
        logger.info(
            "%s | %s | signal=%s mark=%.2f bar_close=%.2f session=%s "
            "%s=%.2f %s=%.2f pos=%.4f | %s",
            symbol,
            self._engine,
            result.signal.value,
            display_price,
            result.price,
            session,
            metric_a,
            result.fast_sma,
            metric_b,
            result.slow_sma,
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
            "reason": result.reason,
            "position": position_qty,
            "stop_loss_pct": self.config.stop_loss_pct,
            "stop_distance": stop_distance or None,
            "managed": managed or None,
            "engine": self._engine,
        }
        if self._engine == "dip":
            payload["rsi"] = result.fast_sma
            payload["bb_pct_b"] = result.slow_sma / 100.0 if result.slow_sma else 0.0

        gate_ctx = {"mark": mark, "activity": activity}
        if result.signal is Signal.HOLD:
            if position_qty > 0:
                arm_protective_stop(self.service, symbol, payload, stop_distance)
            return payload

        session_info = self.service.market_session()
        if session_info["session"] == "closed":
            logger.warning(
                "skipping order — market closed until %s",
                session_info.get("next_open"),
            )
            payload["reason"] += " | skipped: market closed"
            return payload

        if self.service.has_open_orders(symbol):
            logger.warning("skipping — open orders already exist for %s", symbol)
            payload["reason"] += " | skipped: open orders"
            return payload

        if result.signal is Signal.BUY and position_qty <= 0:
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
                logger.info(
                    "BUY pending approval: id=%s qty=%s stop=%s",
                    appr.get("id"),
                    qty,
                    entry_stop or f"{self.config.stop_loss_pct or 0}%",
                )
                payload["order_id"] = None
                payload["order_qty"] = qty
                payload["pending_approval_id"] = appr.get("id")
                payload["approval_required"] = True
                payload["intent"] = "open_long"
                payload["reason"] += " | Pending user approval"
            else:
                order = self.service.submit_order(
                    symbol, qty, OrderSide.BUY, stop_price=entry_stop
                )
                logger.info(
                    "BUY submitted: id=%s qty=%s type=%s stop=%s size=%s",
                    order.id,
                    qty,
                    order.type,
                    entry_stop or f"{self.config.stop_loss_pct or 0}%",
                    self.config.size_summary(),
                )
                payload["order_id"] = str(order.id)
                payload["order_qty"] = qty
                payload["intent"] = "open_long"
                arm_protective_stop(self.service, symbol, payload, stop_distance)
        elif result.signal is Signal.SELL and position_qty > 0:
            # Confidence=1 so only min-hold applies (no AI conf bump).
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
                # Stage before touching the resting stop: the exit may sit in the
                # queue for hours, and a cancelled stop would leave the long naked.
                appr = self.approval_handler(
                    symbol=symbol,
                    action="SELL",
                    qty=qty,
                    price=display_price,
                    reason=result.reason,
                    engine=self._engine,
                    cancel_stops=True,
                )
                logger.info(
                    "SELL pending approval: id=%s qty=%s",
                    appr.get("id"),
                    qty,
                )
                payload["order_id"] = None
                payload["order_qty"] = qty
                payload["pending_approval_id"] = appr.get("id")
                payload["approval_required"] = True
                payload["intent"] = "close_long"
                payload["reason"] += " | Pending user approval"
            else:
                cancelled = self.service.cancel_open_stop_orders(symbol)
                if cancelled:
                    logger.info(
                        "cancelled %s protective stop(s) before SELL", cancelled
                    )
                order = self.service.submit_order(symbol, qty, OrderSide.SELL)
                logger.info(
                    "SELL submitted: id=%s qty=%s type=%s size=%s",
                    order.id,
                    qty,
                    order.type,
                    self.config.size_summary(),
                )
                payload["order_id"] = str(order.id)
                payload["order_qty"] = qty
                payload["intent"] = "close_long"
        else:
            logger.info("no action (already in desired state)")
            payload["reason"] += " | no action (position state)"
            if position_qty > 0:
                arm_protective_stop(self.service, symbol, payload, stop_distance)

        return payload

    def _entry_qty(
        self, price: float, stop_distance: float, equity: float
    ) -> float:
        """Risk-engine size when configured; otherwise desk qty/notional."""
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
        """Outside RTH, Alpaca needs whole shares — truncate and skip if < 1."""
        session_info = self.service.market_session()
        if session_info["is_open"]:
            return float(qty)
        whole = int(qty)
        if whole < 1:
            logger.warning("skipping order — need whole shares outside regular hours")
            return None
        if whole != qty:
            logger.info("truncating qty %.4f → %d for extended hours", qty, whole)
        return float(whole)
