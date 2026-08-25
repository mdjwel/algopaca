"""Live paper trader for Regime Dual Momentum (long/short per symbol)."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from alpaca.trading.enums import OrderSide

from bot.client import AlpacaService
from bot.config import Config
from bot.ls_backtest import LSRiskParams, _position_qty, _stop_distance
from bot.ls_strategy import LSSide, LongShortRegimeStrategy
from bot.options_overlay import apply_options_overlays
from bot.strategy import Signal, StrategyResult

logger = logging.getLogger(__name__)


def _risk_from_config(config: Config) -> LSRiskParams:
    return LSRiskParams(
        atr_stop_mult=float(getattr(config, "ls_atr_stop_mult", 1.5) or 1.5),
        rr=float(getattr(config, "ls_rr", 2.0) or 2.0),
        risk_pct=float(getattr(config, "ls_risk_pct", 1.0) or 1.0),
        time_stop_bars=int(getattr(config, "ls_time_stop_bars", 15) or 15),
    )


def _bars_held(created_at: Any) -> int | None:
    """Approximate daily bars held from Alpaca position created_at."""
    if created_at is None:
        return None
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(created_at, datetime):
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max(0, (now - created_at.astimezone(timezone.utc)).days)


class LsTradingBot:
    """Take long or short positions from EMA/ADX/MACD regime signals.

    Matches backtest hold semantics: FLAT / no-entry means keep the open trade.
    Exits on opposite signal, EMA regime flip, ATR stop, R:R target, or time stop.
    Position size uses equity × risk% / ATR stop distance (desk qty is fallback).
    """

    def __init__(self, config: Config, service: AlpacaService | None = None) -> None:
        if config.bar_timeframe != "1Day":
            logger.warning(
                "LS mode forces 1Day bars (configured %s)", config.bar_timeframe
            )
            config = replace(config, bar_timeframe="1Day")
        # Desk % stop is for SMA/dip buys; LS arms ATR-based stops instead.
        if float(getattr(config, "stop_loss_pct", 0) or 0) > 0:
            logger.info(
                "LS mode uses ATR stops; ignoring desk stop_loss_pct=%.2f",
                config.stop_loss_pct,
            )
            config = replace(config, stop_loss_pct=0.0)
        self.config = config
        self.service = service or AlpacaService(config)
        self.strategy = LongShortRegimeStrategy(
            ema_fast=int(getattr(config, "ls_ema_fast", 21) or 21),
            ema_slow=int(getattr(config, "ls_ema_slow", 55) or 55),
            adx_min=float(getattr(config, "ls_adx_min", 20.0) or 20.0),
        )
        self.risk = _risk_from_config(config)
        self._engine = "ls"

    def run_once(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        symbols = self.config.primary_symbols()
        stopping = should_stop or (lambda: False)
        results: list[dict[str, Any]] = []
        for symbol in symbols:
            # Stop lands between symbols instead of after the whole watchlist.
            if stopping():
                logger.info("ls cycle stopped before %s", symbol)
                break
            try:
                results.append(self._run_symbol(symbol))
            except Exception as exc:
                logger.exception("ls iteration failed for %s", symbol)
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
                        "ls_side": LSSide.FLAT.value,
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
            "ls_side": LSSide.FLAT.value,
        }
        apply_options_overlays(self.config, self.service, results)
        if results:
            primary = results[0]
        return {"primary": primary, "results": results}

    def as_strategy_result(self, primary: dict[str, Any]) -> StrategyResult:
        """Adapt LS payload into StrategyResult for the desk signal wall."""
        side = str(primary.get("ls_side") or LSSide.FLAT.value)
        if side == LSSide.LONG.value and primary.get("signal") == Signal.BUY.value:
            sig = Signal.BUY
        elif side == LSSide.SHORT.value and primary.get("signal") in {
            Signal.SELL.value,
            "short",
        }:
            sig = Signal.SELL
        else:
            raw = str(primary.get("signal") or Signal.HOLD.value).lower()
            sig = Signal.BUY if raw == "buy" else Signal.SELL if raw == "sell" else Signal.HOLD
        return StrategyResult(
            signal=sig,
            price=float(primary.get("price") or 0),
            fast_sma=float(primary.get("fast_sma") or 0),
            slow_sma=float(primary.get("slow_sma") or 0),
            reason=str(primary.get("reason") or ""),
        )

    def _account_equity(self) -> float:
        try:
            summary = self.service.account_summary()
            return float(summary.get("equity") or 0)
        except Exception:
            return 0.0

    def _entry_qty(self, price: float, atr: float) -> float:
        """ATR risk sizing with desk qty/notional as fallback."""
        stop_dist = _stop_distance(price, atr, self.risk)
        equity = self._account_equity()
        qty = _position_qty(equity, stop_dist, self.risk) if equity > 0 else 0.0
        if qty > 0:
            return qty
        try:
            return float(self.config.order_qty_for_price(price))
        except ValueError:
            return 0.0

    def _position_meta(self, symbol: str) -> tuple[float, float | None, Any]:
        qty = float(self.service.get_position_qty(symbol) or 0)
        avg = self.service.get_avg_entry_price(symbol)
        created = None
        try:
            pos = self.service.trading.get_open_position(symbol)
            created = getattr(pos, "created_at", None)
        except Exception:
            pass
        return qty, avg, created

    def _exit_reason(
        self,
        *,
        position_qty: float,
        avg_entry: float | None,
        created_at: Any,
        decision,
        mark: float,
    ) -> str | None:
        """Return exit reason when an open trade should flatten (backtest parity)."""
        if position_qty == 0:
            return None

        is_long = position_qty > 0
        atr = float(decision.atr or 0)
        entry = float(avg_entry or mark or decision.price or 0)
        if entry > 0 and atr >= 0:
            stop_dist = _stop_distance(entry, atr, self.risk)
            if is_long:
                stop_px = entry - stop_dist
                target_px = entry + stop_dist * float(self.risk.rr)
                if mark <= stop_px:
                    return f"stop @ {stop_px:.4f}"
                if mark >= target_px:
                    return f"take profit {self.risk.rr:g}R @ {target_px:.4f}"
            else:
                stop_px = entry + stop_dist
                target_px = entry - stop_dist * float(self.risk.rr)
                if mark >= stop_px:
                    return f"stop @ {stop_px:.4f}"
                if mark <= target_px:
                    return f"take profit {self.risk.rr:g}R @ {target_px:.4f}"

        held = _bars_held(created_at)
        if held is not None and held >= int(self.risk.time_stop_bars):
            return f"time stop after {held} bars"

        # Opposite directional entry signal
        if is_long and decision.side is LSSide.SHORT:
            return "regime/signal flip to short"
        if (not is_long) and decision.side is LSSide.LONG:
            return "regime/signal flip to long"

        # Soft exit: EMA regime flip against position
        if is_long and decision.ema_fast < decision.ema_slow:
            return "bearish EMA regime flip"
        if (not is_long) and decision.ema_fast > decision.ema_slow:
            return "bullish EMA regime flip"

        return None

    def _run_symbol(self, symbol: str) -> dict[str, Any]:
        need = max(self.strategy.bars_needed + 5, 80)
        bars = self.service.get_bars(symbol, limit=need)
        decision = self.strategy.evaluate(bars)
        position_qty, avg_entry, created_at = self._position_meta(symbol)

        try:
            mark = self.service.get_mark_price(symbol)
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
            "LS | %s | side=%s mark=%.2f ADX=%.1f EMA %.2f/%.2f pos=%.4f | %s",
            symbol,
            decision.side.value,
            display_price,
            decision.adx,
            decision.ema_fast,
            decision.ema_slow,
            position_qty,
            decision.reason,
        )

        payload: dict[str, Any] = {
            "symbol": symbol,
            "signal": Signal.HOLD.value,
            "price": display_price,
            "bar_close": decision.price,
            "session": session,
            "is_open": mark.get("is_open"),
            "price_source": price_source,
            "price_asof": price_asof,
            "fast_sma": decision.ema_fast,
            "slow_sma": decision.ema_slow,
            "reason": decision.reason,
            "position": position_qty,
            "engine": self._engine,
            "ls_side": decision.side.value,
            "adx": decision.adx,
            "atr": decision.atr,
        }

        session_info = self.service.market_session()
        market_closed = session_info.get("session") == "closed"

        def _order(side: OrderSide, qty: float, reason: str) -> dict[str, Any]:
            if market_closed:
                return {
                    "symbol": symbol,
                    "side": side.value.lower(),
                    "qty": round(qty, 6),
                    "price": display_price,
                    "reason": reason + " | skipped: market closed",
                    "order_id": None,
                }
            if self.service.has_open_orders(symbol):
                return {
                    "symbol": symbol,
                    "side": side.value.lower(),
                    "qty": round(qty, 6),
                    "price": display_price,
                    "reason": reason + " | skipped: open orders",
                    "order_id": None,
                }
            if side == OrderSide.SELL and position_qty > 0:
                try:
                    cancelled = self.service.cancel_open_stop_orders(symbol)
                    if cancelled:
                        logger.info(
                            "cancelled %s protective stop(s) before SELL %s",
                            cancelled,
                            symbol,
                        )
                except Exception as exc:
                    logger.warning("stop cancel failed for %s: %s", symbol, exc)
            order = self.service.submit_order(symbol, qty, side)
            oid = str(getattr(order, "id", "") or "")
            return {
                "symbol": symbol,
                "side": side.value.lower(),
                "qty": round(qty, 6),
                "price": display_price,
                "reason": reason,
                "order_id": oid or None,
            }

        actions: list[dict[str, Any]] = []

        # Manage open trade first (hold through FLAT / no-entry).
        if position_qty != 0:
            exit_reason = self._exit_reason(
                position_qty=position_qty,
                avg_entry=avg_entry,
                created_at=created_at,
                decision=decision,
                mark=float(display_price),
            )
            if exit_reason:
                if position_qty > 0:
                    actions.append(
                        _order(OrderSide.SELL, abs(position_qty), exit_reason)
                    )
                else:
                    actions.append(
                        _order(OrderSide.BUY, abs(position_qty), exit_reason)
                    )
                payload["signal"] = (
                    Signal.SELL.value if position_qty > 0 else Signal.BUY.value
                )
                payload["reason"] = exit_reason
                if not market_closed:
                    position_qty = 0.0
                    payload["position"] = 0.0
            else:
                # Still aligned / waiting — re-arm long ATR stop if needed.
                payload["signal"] = Signal.HOLD.value
                payload["reason"] = f"holding | {decision.reason}"
                payload["ls_side"] = (
                    LSSide.LONG.value if position_qty > 0 else LSSide.SHORT.value
                )
                if (
                    position_qty > 0
                    and not market_closed
                    and avg_entry
                    and decision.atr > 0
                ):
                    stop_dist = _stop_distance(avg_entry, decision.atr, self.risk)
                    pct = (stop_dist / avg_entry) * 100.0 if avg_entry else 0.0
                    if pct > 0:
                        try:
                            armed = self.service.ensure_stop_loss(symbol, pct=pct)
                            if armed:
                                payload["stop_loss"] = armed
                                payload["reason"] += (
                                    f" | stop @{armed['stop_price']:.2f} "
                                    f"(-{armed['pct']:.2f}%)"
                                )
                        except Exception as exc:
                            logger.warning(
                                "could not arm LS stop for %s: %s", symbol, exc
                            )

        # Open toward entry signal only when flat (may reverse after an exit above).
        if position_qty == 0 and decision.side in (LSSide.LONG, LSSide.SHORT):
            qty = self._entry_qty(display_price, decision.atr)
            if qty <= 0:
                payload["reason"] = (
                    f"{payload['reason']} | size error: no risk/desk qty"
                    if actions
                    else f"{decision.reason} | size error: no risk/desk qty"
                )
            elif decision.side is LSSide.LONG:
                actions.append(_order(OrderSide.BUY, qty, decision.reason))
                payload["signal"] = Signal.BUY.value
                payload["ls_side"] = LSSide.LONG.value
                payload["reason"] = (
                    f"{payload['reason']} → enter long"
                    if len(actions) > 1
                    else decision.reason
                )
                if not market_closed and decision.atr > 0:
                    stop_dist = _stop_distance(
                        float(display_price), decision.atr, self.risk
                    )
                    pct = (
                        (stop_dist / float(display_price)) * 100.0
                        if display_price
                        else 0.0
                    )
                    if pct > 0:
                        try:
                            armed = self.service.ensure_stop_loss(symbol, pct=pct)
                            if armed:
                                payload["stop_loss"] = armed
                                payload["reason"] += (
                                    f" | stop @{armed['stop_price']:.2f} "
                                    f"(-{armed['pct']:.2f}%)"
                                )
                        except Exception as exc:
                            logger.warning(
                                "could not arm LS stop for %s: %s", symbol, exc
                            )
            else:
                actions.append(_order(OrderSide.SELL, qty, decision.reason))
                payload["signal"] = Signal.SELL.value
                payload["ls_side"] = LSSide.SHORT.value
                payload["reason"] = (
                    f"{payload['reason']} → enter short"
                    if len(actions) > 1
                    else decision.reason
                )
        elif position_qty == 0 and not actions:
            # Flat with no entry — keep HOLD. Do not clobber a flatten
            # that just zeroed the position (SELL/BUY must stay on the payload).
            payload["signal"] = Signal.HOLD.value
            payload["ls_side"] = LSSide.FLAT.value

        if market_closed and actions:
            payload["reason"] += f" | market closed until {session_info.get('next_open')}"

        payload["actions"] = actions
        if actions and actions[-1].get("order_id"):
            payload["order_id"] = actions[-1]["order_id"]
            payload["order_qty"] = actions[-1].get("qty")
        elif actions:
            payload["order_qty"] = actions[-1].get("qty")
        return payload
