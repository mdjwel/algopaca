"""AI-driven multi-symbol paper trading."""

from __future__ import annotations

import logging
from typing import Any, Callable

from alpaca.trading.enums import OrderSide

from bot.ai_brain import AiBrain
from bot.ai_providers import build_provider
from bot.ai_risk import (
    clear_trade_state,
    desired_stop,
    entry_gates,
    load_trade_state,
    save_trade_state,
    should_scale_out,
)
from bot.client import AlpacaService
from bot.config import Config, normalize_lang
from bot.strategy import Signal, StrategyResult

logger = logging.getLogger(__name__)


class CycleStopped(Exception):
    """Raised inside a cycle when Stop was pressed — abort, don't error."""


class AiTradingBot:
    def __init__(self, config: Config, service: AlpacaService | None = None) -> None:
        self.config = config
        self.service = service or AlpacaService(config)
        provider = build_provider(
            config.ai_provider,
            openai_key=config.openai_api_key,
            gemini_key=config.gemini_api_key,
            openai_model=config.openai_model,
            gemini_model=config.gemini_model,
        )
        self.brain = AiBrain(config, self.service, provider)

    def run_once(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        symbols = self.config.primary_symbols()
        stopping = should_stop or (lambda: False)
        if stopping():
            return self._empty_bundle()
        # Portfolio state is read once so every symbol this cycle is judged
        # against the same exposure picture.
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

        results: list[dict[str, Any]] = []
        for symbol in symbols:
            # Stop is checked per symbol so the watchlist tail is skipped
            # instead of waiting out a full pass of LLM round trips.
            if stopping():
                logger.info("AI cycle stopped before %s", symbol)
                break
            try:
                result = self._run_symbol(
                    symbol,
                    open_positions=open_positions,
                    day_pl_pct=day_pl_pct,
                    should_stop=stopping,
                )
                intent = result.get("intent")
                if intent in {"open_long", "open_short"}:
                    open_positions += 1
                elif intent in {"close_long", "cover"}:
                    # A slot freed earlier this cycle must be spendable later in
                    # it, the same way one taken is counted against later symbols.
                    open_positions = max(0, open_positions - 1)
                results.append(result)
            except CycleStopped:
                logger.info("AI cycle stopped during %s", symbol)
                break
            except Exception as exc:
                logger.exception("AI iteration failed for %s", symbol)
                results.append(
                    {
                        "symbol": symbol,
                        "signal": Signal.HOLD.value,
                        "price": 0.0,
                        "reason": f"error: {exc}",
                        "error": str(exc),
                        "provider": self.config.ai_provider,
                    }
                )
        primary = results[0] if results else self._empty_bundle()["primary"]
        return {"primary": primary, "results": results}

    def _empty_bundle(self) -> dict[str, Any]:
        return {
            "primary": {
                "symbol": self.config.symbol,
                "signal": Signal.HOLD.value,
                "price": 0.0,
                "reason": "no symbols",
            },
            "results": [],
        }

    def _run_symbol(
        self,
        symbol: str,
        *,
        open_positions: int = 0,
        day_pl_pct: float | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        stopping = should_stop or (lambda: False)
        context = self.brain.build_context(symbol)

        # Manage what is already open before asking the model anything. Stops and
        # take-profits are mechanical and must not depend on an LLM round trip.
        managed: dict[str, Any] = {}
        if float(context.get("position_qty") or 0) != 0:
            managed = self._manage_open_position(symbol, context)
        elif float(context.get("position_qty") or 0) == 0:
            clear_trade_state(symbol, paper=bool(self.config.paper))

        gate = entry_gates(
            self.config,
            context,
            open_positions=open_positions,
            day_pl_pct=day_pl_pct,
        )

        # Open risk is managed above because stops are mechanical; the model
        # round trip below is the slow part and is worth skipping on Stop.
        if stopping():
            raise CycleStopped(symbol)

        decision, context = self.brain.decide(symbol, context)
        mark = context.get("mark") or {}
        price = float(mark.get("price") or context.get("technicals", {}).get("price") or 0)
        position_qty = float(context.get("position_qty") or 0)
        session = (context.get("session") or {}).get("session", "?")

        # Portfolio guards only ever block *new* risk — closing and covering stay open.
        opens_new_risk = position_qty == 0 and decision.action in {
            Signal.BUY.value,
            Signal.SELL.value,
        }
        blocked_reason = ""
        if opens_new_risk and not gate.allowed:
            blocked_reason = gate.reason
            decision = self.brain.hold_decision(decision, gate.reason)

        reason = (
            f"[{decision.provider}/{decision.model}] conf={decision.confidence:.2f} "
            f"ta={decision.ta_bias} news={decision.news_bias} | {decision.thesis}"
        )
        if decision.risks:
            reason += f" | risks: {decision.risks}"
        if managed.get("actions"):
            reason += " | " + "; ".join(managed["actions"])

        logger.info(
            "%s | AI signal=%s mark=%.2f session=%s pos=%.4f conf=%.2f | %s",
            symbol,
            decision.action,
            price,
            session,
            position_qty,
            decision.confidence,
            decision.thesis,
        )

        payload: dict[str, Any] = {
            "symbol": symbol,
            "signal": decision.action,
            "price": price,
            "bar_close": context.get("technicals", {}).get("price"),
            "session": session,
            "price_source": mark.get("source"),
            "price_asof": mark.get("asof"),
            "fast_sma": (context.get("technicals") or {}).get("sma", {}).get("10") or 0,
            "slow_sma": (context.get("technicals") or {}).get("sma", {}).get("50") or 0,
            "rsi": (context.get("technicals") or {}).get("rsi_14"),
            "trend_bias": (context.get("technicals") or {}).get("trend_bias"),
            "atr_pct": (context.get("technicals") or {}).get("atr_pct"),
            "adx": (context.get("technicals") or {}).get("adx_14"),
            "regime": (context.get("technicals") or {}).get("regime"),
            "htf_bias": (context.get("higher_timeframe") or {}).get("bias"),
            "spread_bps": context.get("spread_bps"),
            "r_multiple": (context.get("position") or {}).get("r_multiple"),
            "avg_entry": (context.get("position") or {}).get("avg_entry"),
            "unrealized_pct": (context.get("position") or {}).get("unrealized_pct"),
            "risk_blocked": blocked_reason or None,
            "managed": managed or None,
            "reason": reason,
            "position": position_qty,
            "position_side": (
                "long" if position_qty > 0 else "short" if position_qty < 0 else "flat"
            ),
            "confidence": decision.confidence,
            "provider": decision.provider,
            "model": decision.model,
            "thesis": decision.thesis,
            "risks": decision.risks,
            # The English originals plus the language the localized pair is in,
            # so the UI can render either without re-running the model.
            "thesis_en": decision.thesis_en,
            "risks_en": decision.risks_en,
            "note_lang": normalize_lang(getattr(self.config, "lang", None)),
            "news_bias": decision.news_bias,
            "ta_bias": decision.ta_bias,
            "news_count": len(context.get("news") or []),
            "calendar_count": len(context.get("economic_calendar") or []),
            "earnings_stance": (context.get("earnings") or {}).get("stance"),
            "earnings_result": (context.get("earnings") or {}).get("last_result"),
            "earnings_blackout": bool((context.get("earnings") or {}).get("blackout")),
            "stop_loss_pct": self.config.stop_loss_pct,
            "context_summary": {
                "rsi": (context.get("technicals") or {}).get("rsi_14"),
                "trend": (context.get("technicals") or {}).get("trend_bias"),
                "sma10": (context.get("technicals") or {}).get("sma", {}).get("10"),
                "sma50": (context.get("technicals") or {}).get("sma", {}).get("50"),
                "earnings": {
                    "stance": (context.get("earnings") or {}).get("stance"),
                    "blackout": (context.get("earnings") or {}).get("blackout"),
                    "plan": (context.get("earnings") or {}).get("plan"),
                    "next": (context.get("earnings") or {}).get("next"),
                    "last_result": (context.get("earnings") or {}).get("last_result"),
                },
                "upcoming_events": [
                    {
                        "title": e.get("title"),
                        "when_et": e.get("when_et"),
                        "impact": e.get("impact"),
                    }
                    for e in (context.get("economic_calendar") or [])[:5]
                ],
                "headlines": [n.get("title") for n in (context.get("news") or [])[:5]],
            },
        }

        if decision.action == Signal.HOLD.value:
            if position_qty != 0:
                self._arm_stop(symbol, payload)
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

        qty = decision.qty
        stop_distance = (context.get("risk") or {}).get("stop_distance") or 0
        entry_stop_long = (
            round(price - float(stop_distance), 2)
            if (stop_distance and price > 0)
            else None
        )
        entry_stop_short = (
            round(price + float(stop_distance), 2)
            if (stop_distance and price > 0)
            else None
        )
        if decision.action == Signal.BUY.value and position_qty < 0:
            cancelled = self.service.cancel_open_stop_orders(symbol)
            if cancelled:
                logger.info("cancelled %s protective stop(s) before COVER", cancelled)
            cover_qty = min(abs(position_qty), qty if qty > 0 else abs(position_qty))
            sized = self._qty_for_session(cover_qty, whole=abs(position_qty) >= 1)
            if sized is None:
                payload["reason"] += " | skipped: qty"
                return payload
            order = self.service.submit_order(
                symbol, sized, OrderSide.BUY, protect=False
            )
            logger.info("AI COVER submitted: id=%s qty=%s", order.id, sized)
            payload["order_id"] = str(order.id)
            payload["order_qty"] = sized
            payload["signal"] = Signal.BUY.value
            payload["intent"] = "cover"
        elif decision.action == Signal.BUY.value and position_qty == 0:
            sized = self._qty_for_session(qty)
            if sized is None:
                payload["reason"] += " | skipped: qty"
                return payload
            order = self.service.submit_order(
                symbol, sized, OrderSide.BUY, stop_price=entry_stop_long
            )
            logger.info(
                "AI BUY submitted: id=%s qty=%s stop=%s",
                order.id,
                sized,
                entry_stop_long or f"{self.config.stop_loss_pct or 0}%",
            )
            payload["order_id"] = str(order.id)
            payload["order_qty"] = sized
            payload["intent"] = "open_long"
            self._arm_stop(symbol, payload, stop_distance)
        elif decision.action == Signal.SELL.value and position_qty > 0:
            cancelled = self.service.cancel_open_stop_orders(symbol)
            if cancelled:
                logger.info("cancelled %s protective stop(s) before SELL", cancelled)
            sell_qty = min(position_qty, qty if qty > 0 else position_qty)
            sized = self._qty_for_session(sell_qty)
            if sized is None:
                payload["reason"] += " | skipped: qty"
                return payload
            order = self.service.submit_order(
                symbol, sized, OrderSide.SELL, protect=False
            )
            logger.info("AI SELL submitted: id=%s qty=%s", order.id, sized)
            payload["order_id"] = str(order.id)
            payload["order_qty"] = sized
            payload["intent"] = "close_long"
        elif decision.action == Signal.SELL.value and position_qty == 0:
            # Alpaca does not short fractionals — whole shares only.
            sized = self._qty_for_session(qty, whole=True)
            if sized is None:
                payload["reason"] += " | skipped: qty (shorts need whole shares)"
                return payload
            order = self.service.submit_order(
                symbol,
                sized,
                OrderSide.SELL,
                protect=True,
                stop_price=entry_stop_short,
            )
            logger.info(
                "AI SHORT submitted: id=%s qty=%s stop=%s",
                order.id,
                sized,
                entry_stop_short or f"{self.config.stop_loss_pct or 0}%",
            )
            payload["order_id"] = str(order.id)
            payload["order_qty"] = sized
            payload["intent"] = "open_short"
            self._arm_stop(symbol, payload, stop_distance)
        else:
            logger.info("no action (already in desired state)")
            payload["reason"] += " | no action (position state)"
            if position_qty != 0:
                self._arm_stop(symbol, payload)

        return payload

    def _manage_open_position(
        self, symbol: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Ratchet the stop and take partial profit on an open position.

        Runs every cycle before the model is consulted, so risk management never
        waits on an LLM call and never depends on the model choosing to exit.
        """
        position = context.get("position") or {}
        side = str(position.get("side") or "flat")
        if side not in {"long", "short"}:
            return {}
        entry = position.get("avg_entry")
        price = float((context.get("mark") or {}).get("price") or 0)
        stop_distance = (context.get("risk") or {}).get("stop_distance")
        actions: list[str] = []
        out: dict[str, Any] = {}

        state = load_trade_state(symbol, entry, paper=bool(self.config.paper))
        r = position.get("r_multiple")
        if r is not None:
            state["peak_r"] = max(float(state.get("peak_r") or 0.0), float(r))

        # 1) Scale out half at the take-profit multiple, once per position.
        if should_scale_out(self.config, r=r, already_scaled=state.get("scaled_out")):
            trimmed = self._scale_out(symbol, position)
            if trimmed:
                state["scaled_out"] = True
                out["scale_out"] = trimmed
                actions.append(
                    f"scaled out {trimmed['qty']:g} @ +{float(r):.1f}R"
                )

        # 2) Move the stop to breakeven, then trail it. Never loosens.
        # Skipped in the same cycle as a trim: the sell is still pending, so a new
        # stop would be sized against a position that is about to shrink. The next
        # cycle sees no resting stop and arms it against the settled size.
        if out.get("scale_out"):
            save_trade_state(symbol, state, paper=bool(self.config.paper))
            out["actions"] = actions
            logger.info("%s | managed: %s", symbol, "; ".join(actions))
            return out

        target = desired_stop(
            self.config,
            side=side,
            entry=entry,
            price=price,
            stop_distance=stop_distance,
            current_stop=position.get("stop_price"),
        )
        if target:
            current = position.get("stop_price")
            # Only re-place when the stop meaningfully improves. A one-cent trail
            # would cancel and resubmit every poll, which is pure API churn and
            # briefly leaves the position unprotected each time.
            step = max(0.01, float(stop_distance or 0) * 0.1)
            moved = (
                current is None
                or abs(float(current) - target["stop_price"]) >= step
            )
            if moved:
                try:
                    armed = self.service.replace_stop_loss(symbol, target["stop_price"])
                except Exception as exc:
                    logger.warning("could not move stop for %s: %s", symbol, exc)
                    armed = None
                if armed:
                    out["stop_moved"] = {**armed, "stage": target["stage"]}
                    actions.append(
                        f"stop {target['stage']} @{target['stop_price']:.2f}"
                    )

        save_trade_state(symbol, state, paper=bool(self.config.paper))
        if actions:
            out["actions"] = actions
            logger.info("%s | managed: %s", symbol, "; ".join(actions))
        return out

    def _scale_out(
        self, symbol: str, position: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Trim half the position at the take-profit target."""
        qty = abs(float(position.get("qty") or 0))
        if qty <= 0:
            return None
        trim = qty / 2
        # Whole shares only when the remainder must stay shortable / stoppable.
        trim = float(int(trim)) if qty >= 2 else 0.0
        if trim <= 0:
            return None
        session_info = self.service.market_session()
        if not session_info["is_open"]:
            return None
        is_short = float(position.get("qty") or 0) < 0
        self.service.cancel_open_stop_orders(symbol)
        order = self.service.submit_order(
            symbol,
            trim,
            OrderSide.BUY if is_short else OrderSide.SELL,
            protect=False,
        )
        return {"id": str(order.id), "qty": trim}

    def _arm_stop(
        self, symbol: str, payload: dict[str, Any], stop_distance: float | None = None
    ) -> None:
        """Arm a protective stop if none is resting.

        `stop_distance` (ATR-derived, in dollars) is converted to the percent form
        ``ensure_stop_loss`` expects so extended-hours entries — which cannot carry
        an OTO leg — still get the volatility-scaled stop rather than the flat one.
        """
        pct: float | None = None
        price = float(payload.get("price") or 0)
        if stop_distance and price > 0:
            pct = float(stop_distance) / price * 100
        try:
            armed = self.service.ensure_stop_loss(symbol, pct)
        except Exception as exc:
            logger.warning("could not arm stop loss for %s: %s", symbol, exc)
            payload["reason"] += f" | stop arm failed: {exc}"
            return
        if armed:
            logger.info(
                "Stop loss armed: %s qty=%s @ %.2f (%.2f%%)",
                symbol,
                armed["qty"],
                armed["stop_price"],
                armed["pct"],
            )
            payload["stop_loss"] = armed
            sign = "+" if armed.get("side") == "buy" else "-"
            payload["reason"] += (
                f" | stop @{armed['stop_price']:.2f} ({sign}{armed['pct']:.2f}%)"
            )

    def _qty_for_session(self, qty: float, *, whole: bool = False) -> float | None:
        session_info = self.service.market_session()
        need_whole = whole or not session_info["is_open"]
        if not need_whole:
            return float(qty)
        whole_qty = int(qty)
        if whole_qty < 1:
            logger.warning("skipping order — need whole shares")
            return None
        if whole_qty != qty:
            logger.info("truncating qty %.4f → %d for whole-share order", qty, whole_qty)
        return float(whole_qty)

    def as_strategy_result(self, payload: dict[str, Any]) -> StrategyResult:
        return StrategyResult(
            signal=Signal(payload.get("signal", "hold")),
            price=float(payload.get("price") or 0),
            fast_sma=float(payload.get("fast_sma") or 0),
            slow_sma=float(payload.get("slow_sma") or 0),
            reason=str(payload.get("reason") or ""),
        )
