"""AI-driven multi-symbol paper trading."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from bot.ai_backtest import OPPOSING_METALS_PAIRS, UNSHORTABLE_SYMBOLS
from bot.client import AlpacaService
from bot.config import Config, normalize_lang
from bot.metals_intel import (
    check_imminent_economic_events,
    is_precious_metal,
    protect_metals_position_before_event,
)
from bot.options_overlay import apply_options_overlays
from bot.strategy import Signal, StrategyResult

logger = logging.getLogger(__name__)


class CycleStopped(Exception):
    """Raised inside a cycle when Stop was pressed — abort, don't error."""


class AiTradingBot:
    def __init__(
        self,
        config: Config,
        service: AlpacaService | None = None,
        approval_handler: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.service = service or AlpacaService(config)
        self.approval_handler = approval_handler
        provider = build_provider(
            config.ai_provider,
            openai_key=config.openai_api_key,
            gemini_key=config.gemini_api_key,
            anthropic_key=config.anthropic_api_key,
            xai_key=config.xai_api_key,
            openai_model=config.openai_model,
            gemini_model=config.gemini_model,
            anthropic_model=config.anthropic_model,
            xai_model=config.xai_model,
        )
        self.brain = AiBrain(config, self.service, provider)
        self._execution_lock = threading.Lock()
        self._open_positions = 0

    def run_once(
        self,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> dict[str, Any]:
        symbols = self.config.primary_symbols()
        stopping = should_stop or (lambda: False)
        if stopping():
            return self._empty_bundle()

        # Portfolio state and market session are read once per cycle so every
        # symbol shares the same exposure picture and avoids redundant network calls.
        day_pl_pct: float | None = None
        shared_account: dict[str, Any] = {}
        shared_session: dict[str, Any] = {}
        try:
            shared_account = self.service.account_summary()
            day_pl_pct = shared_account.get("day_pl_pct")
            self._open_positions = sum(
                1 for s in symbols if self.service.get_position_qty(s) != 0
            )
        except Exception as exc:
            logger.warning("could not read portfolio state: %s", exc)

        try:
            shared_session = self.service.market_session()
        except Exception:
            shared_session = {}

        results_by_symbol: dict[str, dict[str, Any]] = {}
        results_lock = threading.Lock()

        def _evaluate(sym: str) -> dict[str, Any]:
            if stopping():
                raise CycleStopped(sym)
            return self._run_symbol(
                sym,
                day_pl_pct=day_pl_pct,
                shared_account=shared_account,
                shared_session=shared_session,
                should_stop=stopping,
            )

        def _publish_progress() -> None:
            if on_progress:
                current = [results_by_symbol[s] for s in symbols if s in results_by_symbol]
                try:
                    on_progress(current)
                except Exception as exc:
                    logger.debug("on_progress notification failed: %s", exc)

        workers = min(8, len(symbols)) if len(symbols) > 1 else 1

        if workers <= 1:
            for symbol in symbols:
                if stopping():
                    logger.info("AI cycle stopped before %s", symbol)
                    break
                try:
                    res = _evaluate(symbol)
                    with results_lock:
                        results_by_symbol[symbol] = res
                        _publish_progress()
                except CycleStopped:
                    logger.info("AI cycle stopped during %s", symbol)
                    break
                except Exception as exc:
                    logger.exception("AI iteration failed for %s", symbol)
                    with results_lock:
                        results_by_symbol[symbol] = {
                            "symbol": symbol,
                            "signal": Signal.HOLD.value,
                            "price": 0.0,
                            "reason": f"error: {exc}",
                            "error": str(exc),
                            "provider": self.config.ai_provider,
                        }
                        _publish_progress()
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_evaluate, s): s for s in symbols}
                for fut in as_completed(futures):
                    sym = futures[fut]
                    if stopping():
                        logger.info("AI cycle stopped while evaluating %s", sym)
                        continue
                    try:
                        res = fut.result()
                    except CycleStopped:
                        logger.info("AI cycle stopped during %s", sym)
                        continue
                    except Exception as exc:
                        logger.exception("AI iteration failed for %s", sym)
                        res = {
                            "symbol": sym,
                            "signal": Signal.HOLD.value,
                            "price": 0.0,
                            "reason": f"error: {exc}",
                            "error": str(exc),
                            "provider": self.config.ai_provider,
                        }
                    with results_lock:
                        results_by_symbol[sym] = res
                        _publish_progress()

        results = [results_by_symbol[s] for s in symbols if s in results_by_symbol]
        primary = results[0] if results else self._empty_bundle()["primary"]
        apply_options_overlays(self.config, self.service, results)
        if results:
            primary = results[0]
        return {"primary": primary, "results": results}

    def _empty_bundle(self) -> dict[str, Any]:
        has_symbols = bool(getattr(self, "symbols", None)) or bool(getattr(self.config, "symbol", None))
        return {
            "primary": {
                "symbol": self.config.symbol,
                "signal": Signal.HOLD.value,
                "price": 0.0,
                "reason": "Cycle stopped" if has_symbols else "no symbols",
            },
            "results": [],
        }

    def _run_symbol(
        self,
        symbol: str,
        *,
        day_pl_pct: float | None = None,
        shared_account: dict[str, Any] | None = None,
        shared_session: dict[str, Any] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        stopping = should_stop or (lambda: False)
        context = self.brain.build_context(
            symbol,
            shared_account=shared_account,
            shared_session=shared_session,
        )

        # Manage what is already open before asking the model anything. Stops and
        # take-profits are mechanical and must not depend on an LLM round trip.
        managed: dict[str, Any] = {}
        if float(context.get("position_qty") or 0) != 0:
            managed = self._manage_open_position(symbol, context)
        elif float(context.get("position_qty") or 0) == 0:
            clear_trade_state(symbol, paper=bool(self.config.paper))

        # Open risk is managed above because stops are mechanical; the model
        # round trip below is the slow part and is worth skipping on Stop.
        if stopping():
            raise CycleStopped(symbol)

        preset_id = getattr(self.config, "ai_preset", "")
        sym_upper = symbol.upper().strip()
        if preset_id == "gold_silver_macro" and sym_upper in {"GDX", "GDXJ", "DUST", "UGL"}:
            if float(context.get("position_qty") or 0) == 0:
                logger.info("%s is strictly excluded from AI Gold & Silver Macro playbook", symbol)
                return {
                    "symbol": symbol,
                    "signal": Signal.HOLD.value,
                    "price": float((context.get("mark") or {}).get("price") or 0),
                    "reason": f"{symbol} is strictly excluded from the AI Gold & Silver Macro playbook",
                    "session": (context.get("session") or {}).get("session", "?"),
                    "position": 0.0,
                    "confidence": 0.0,
                }

        decision, context = self.brain.decide(symbol, context)
        if stopping():
            raise CycleStopped(symbol)

        mark = context.get("mark") or {}
        price = float(mark.get("price") or context.get("technicals", {}).get("price") or 0)
        display_price = price
        position_qty = float(context.get("position_qty") or 0)
        session = (context.get("session") or {}).get("session", "?")

        with self._execution_lock:
            gate = entry_gates(
                self.config,
                context,
                open_positions=self._open_positions,
                day_pl_pct=day_pl_pct,
                action=decision.action,
            )

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
                "is_open": mark.get("is_open"),
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
                "thesis_en": decision.thesis_en,
                "risks_en": decision.risks_en,
                "note_lang": normalize_lang(getattr(self.config, "lang", None)),
                "news_bias": decision.news_bias,
                "ta_bias": decision.ta_bias,
                "news_count": len(context.get("news") or []),
                "calendar_count": len(context.get("economic_calendar") or []),
                "active_catalyst": (context.get("precious_metals_intel") or {}).get("active_catalyst"),
                "macro_risk_level": (context.get("precious_metals_intel") or {}).get("macro_risk_level"),
                "earnings_stance": (context.get("earnings") or {}).get("stance"),
                "earnings_result": (context.get("earnings") or {}).get("last_result"),
                "earnings_blackout": bool((context.get("earnings") or {}).get("blackout")),
                "stop_loss_pct": self.config.stop_loss_pct,
                "context_summary": {
                    "rsi": (context.get("technicals") or {}).get("rsi_14"),
                    "trend": (context.get("technicals") or {}).get("trend_bias"),
                    "sma10": (context.get("technicals") or {}).get("sma", {}).get("10"),
                    "sma50": (context.get("technicals") or {}).get("sma", {}).get("50"),
                    "active_catalyst": (context.get("precious_metals_intel") or {}).get("active_catalyst"),
                    "earnings": {
                        "stance": (context.get("earnings") or {}).get("stance"),
                        "blackout": (context.get("earnings") or {}).get("blackout"),
                        "plan": (context.get("earnings") or {}).get("plan"),
                        "next": (context.get("earnings") or {}).get("next"),
                        "last_result": (context.get("earnings") or {}).get("last_result"),
                    },
                    "released_events": [
                        {
                            "title": e.get("title"),
                            "when_et": e.get("when_et"),
                            "impact": e.get("impact"),
                            "actual": e.get("actual"),
                            "status": e.get("status", "released"),
                            "outcome": e.get("outcome"),
                        }
                        for e in (context.get("economic_calendar") or [])
                        if e.get("released")
                        or e.get("status") == "released"
                        or str(e.get("actual") or "").strip()
                    ][:5],
                    "upcoming_events": [
                        {
                            "title": e.get("title"),
                            "when_et": e.get("when_et"),
                            "impact": e.get("impact"),
                            "minutes_away": e.get("minutes_away"),
                        }
                        for e in (context.get("economic_calendar") or [])
                        if not (
                            e.get("released")
                            or e.get("status") == "released"
                            or str(e.get("actual") or "").strip()
                        )
                    ][:5],
                    "headlines": [n.get("title") for n in (context.get("news") or [])[:5]],
                },
            }

            if decision.action == Signal.HOLD.value:
                if position_qty != 0:
                    self._arm_stop(symbol, payload)
                return payload

            session_info = self.service.market_session()
            if session_info.get("session") == "closed":
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
                cover_qty = min(abs(position_qty), qty if qty > 0 else abs(position_qty))
                sized = self._qty_for_session(cover_qty, whole=abs(position_qty) >= 1)
                if sized is None:
                    payload["reason"] += " | skipped: qty"
                    return payload
                if self.config.require_approval and self.approval_handler:
                    appr = self.approval_handler(
                        symbol=symbol,
                        action="COVER",
                        qty=sized,
                        price=display_price,
                        protect=False,
                        reason=decision.reason,
                        engine="ai",
                        thesis=decision.thesis,
                        confidence=decision.confidence,
                        cancel_stops=True,
                    )
                    logger.info("AI COVER pending approval: id=%s qty=%s", appr.get("id"), sized)
                    payload["order_id"] = None
                    payload["order_qty"] = sized
                    payload["pending_approval_id"] = appr.get("id")
                    payload["approval_required"] = True
                    payload["signal"] = Signal.BUY.value
                    payload["intent"] = "cover"
                    payload["reason"] += " | Pending user approval"
                    self._open_positions = max(0, self._open_positions - 1)
                else:
                    cancelled = self.service.cancel_open_stop_orders(symbol)
                    if cancelled:
                        logger.info(
                            "cancelled %s protective stop(s) before COVER", cancelled
                        )
                    order = self.service.submit_order(
                        symbol, sized, OrderSide.BUY, protect=False
                    )
                    logger.info("AI COVER submitted: id=%s qty=%s", order.id, sized)
                    payload["order_id"] = str(order.id)
                    payload["order_qty"] = sized
                    payload["signal"] = Signal.BUY.value
                    payload["intent"] = "cover"
                    self._open_positions = max(0, self._open_positions - 1)

                    # If this cover was part of a reversal to long (e.g. mixed dollar index data on metals or explicit reversal),
                    # immediately enter the Long position so the desk is not left flat.
                    metals_intel = context.get("precious_metals_intel") or {}
                    is_dollar_mixed = bool(metals_intel.get("dollar_mixed"))
                    thesis_text = (str(decision.thesis or "") + " " + str(decision.thesis_en or "")).lower()
                    wants_long_reversal = (
                        is_dollar_mixed
                        or bool(decision.raw.get("reverse_to_long"))
                        or "long" in thesis_text
                        or "revers" in thesis_text
                    )
                    if wants_long_reversal:
                        long_sized = self._qty_for_session(qty if qty > 0 else abs(position_qty))
                        if long_sized and long_sized > 0:
                            try:
                                long_order = self.service.submit_order(
                                    symbol, long_sized, OrderSide.BUY, stop_price=entry_stop_long
                                )
                                stop_repr = entry_stop_long if entry_stop_long else f"{getattr(self.config, 'stop_loss_pct', 0) or 0}%"
                                logger.info(
                                    "AI REVERSAL TO LONG submitted: id=%s qty=%s stop=%s",
                                    long_order.id,
                                    long_sized,
                                    stop_repr,
                                )
                                payload["reversal_to_long"] = True
                                payload["long_order_id"] = str(long_order.id)
                                payload["long_order_qty"] = long_sized
                                payload["intent"] = "reverse_to_long"
                                self._open_positions += 1
                                self._arm_stop(symbol, payload, stop_distance)
                            except Exception as rev_err:
                                logger.warning("Failed to submit long order on reversal for %s: %s", symbol, rev_err)
            elif decision.action == Signal.BUY.value and position_qty == 0:
                sym_upper = symbol.upper()
                opp_sym = OPPOSING_METALS_PAIRS.get(sym_upper)
                if opp_sym and self.service.get_position_qty(opp_sym) != 0:
                    payload["reason"] += f" | skipped: opposing leveraged position in {opp_sym} is currently open"
                    logger.info("AI BUY skipped for %s: opposing %s position is active", symbol, opp_sym)
                    return payload
                sized = self._qty_for_session(qty)
                if sized is None:
                    payload["reason"] += " | skipped: qty"
                    return payload
                if self.config.require_approval and self.approval_handler:
                    appr = self.approval_handler(
                        symbol=symbol,
                        action="BUY",
                        qty=sized,
                        price=display_price,
                        stop_price=entry_stop_long,
                        stop_distance=stop_distance,
                        reason=decision.reason,
                        engine="ai",
                        thesis=decision.thesis,
                        confidence=decision.confidence,
                    )
                    logger.info("AI BUY pending approval: id=%s qty=%s stop=%s", appr.get("id"), sized, entry_stop_long)
                    payload["order_id"] = None
                    payload["order_qty"] = sized
                    payload["pending_approval_id"] = appr.get("id")
                    payload["approval_required"] = True
                    payload["intent"] = "open_long"
                    payload["reason"] += " | Pending user approval"
                    self._open_positions += 1
                else:
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
                    self._open_positions += 1
                    self._arm_stop(symbol, payload, stop_distance)
            elif decision.action == Signal.SELL.value and position_qty > 0:
                sell_qty = min(position_qty, qty if qty > 0 else position_qty)
                sized = self._qty_for_session(sell_qty)
                if sized is None:
                    payload["reason"] += " | skipped: qty"
                    return payload
                if self.config.require_approval and self.approval_handler:
                    appr = self.approval_handler(
                        symbol=symbol,
                        action="SELL",
                        qty=sized,
                        price=display_price,
                        protect=False,
                        reason=decision.reason,
                        engine="ai",
                        thesis=decision.thesis,
                        confidence=decision.confidence,
                        cancel_stops=True,
                    )
                    logger.info("AI SELL pending approval: id=%s qty=%s", appr.get("id"), sized)
                    payload["order_id"] = None
                    payload["order_qty"] = sized
                    payload["pending_approval_id"] = appr.get("id")
                    payload["approval_required"] = True
                    payload["intent"] = "close_long"
                    payload["reason"] += " | Pending user approval"
                    self._open_positions = max(0, self._open_positions - 1)
                else:
                    cancelled = self.service.cancel_open_stop_orders(symbol)
                    if cancelled:
                        logger.info(
                            "cancelled %s protective stop(s) before SELL", cancelled
                        )
                    order = self.service.submit_order(
                        symbol, sized, OrderSide.SELL, protect=False
                    )
                    logger.info("AI SELL submitted: id=%s qty=%s", order.id, sized)
                    payload["order_id"] = str(order.id)
                    payload["order_qty"] = sized
                    payload["intent"] = "close_long"
                    self._open_positions = max(0, self._open_positions - 1)
            elif decision.action == Signal.SELL.value and position_qty == 0:
                sym_upper = symbol.upper()
                if sym_upper in UNSHORTABLE_SYMBOLS:
                    payload["reason"] += f" | skipped: {symbol} is unshortable at Alpaca (long-only vehicle)"
                    logger.info("AI SHORT skipped for %s: unshortable instrument", symbol)
                    return payload
                # Alpaca does not short fractionals — whole shares only.
                sized = self._qty_for_session(qty, whole=True)
                if sized is None:
                    payload["reason"] += " | skipped: qty (shorts need whole shares)"
                    return payload
                if self.config.require_approval and self.approval_handler:
                    appr = self.approval_handler(
                        symbol=symbol,
                        action="SHORT",
                        qty=sized,
                        price=display_price,
                        stop_price=entry_stop_short,
                        stop_distance=stop_distance,
                        protect=True,
                        reason=decision.reason,
                        engine="ai",
                        thesis=decision.thesis,
                        confidence=decision.confidence,
                    )
                    logger.info("AI SHORT pending approval: id=%s qty=%s stop=%s", appr.get("id"), sized, entry_stop_short)
                    payload["order_id"] = None
                    payload["order_qty"] = sized
                    payload["pending_approval_id"] = appr.get("id")
                    payload["approval_required"] = True
                    payload["intent"] = "open_short"
                    payload["reason"] += " | Pending user approval"
                    self._open_positions += 1
                else:
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
                    self._open_positions += 1
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

        # 2) Inverse ETF volatility decay protection (GLL, GDXD, etc.)
        sym_upper = symbol.upper().strip()
        if sym_upper in {"GLL", "GDXD", "ZSL", "JDST"} and side == "long" and not out.get("scale_out"):
            rsi = (context.get("technicals") or {}).get("rsi_14")
            if rsi is not None and float(rsi) >= 65.0 and not state.get("decay_trimmed"):
                trimmed = self._scale_out(symbol, position)
                if trimmed:
                    state["decay_trimmed"] = True
                    state["scaled_out"] = True
                    out["scale_out"] = trimmed
                    actions.append(
                        f"inverse decay protection trim {trimmed['qty']:g} (RSI {float(rsi):.1f} >= 65.0)"
                    )

        # 3) Move the stop to breakeven, then trail it. Never loosens.
        # Skipped in the same cycle as a trim: the sell is still pending, so a new
        # stop would be sized against a position that is about to shrink. The next
        # cycle sees no resting stop and arms it against the settled size.
        if out.get("scale_out"):
            save_trade_state(symbol, state, paper=bool(self.config.paper))
            out["actions"] = actions
            logger.info("%s | managed: %s", symbol, "; ".join(actions))
            return out

        # 2) Pre-event economic data protection for Gold & Silver (5m release window)
        track_dollar_only = bool(getattr(self.config, "metals_dollar_index_only", False))
        if is_precious_metal(symbol) and not track_dollar_only:
            cal = context.get("economic_calendar")
            events_5m = check_imminent_economic_events(cal, window_minutes=5.0)
            if events_5m:
                prot = protect_metals_position_before_event(
                    service=self.service,
                    symbol=symbol,
                    event=events_5m[0],
                    reversal_buy=bool(getattr(self.config, "metals_reversal_buy_on_stop", True)),
                    track_dollar_only=track_dollar_only,
                )
                if prot:
                    out["event_protection"] = prot
                    if prot.get("action_taken") == "updated":
                        actions.append(
                            f"event stop 5m before {prot['event_title'][:15]} @${prot['stop_price']:.2f}"
                        )
                        position["stop_price"] = prot["stop_price"]

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
