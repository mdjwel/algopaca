"""Build market context and ask an LLM for a trade decision."""

from __future__ import annotations

import json
import logging
from typing import Any

from bot.ai_providers import AiDecision, AiProvider, normalize_decision
from bot.ai_risk import confidence_scaled_qty, r_multiple, reversal_gate, spread_bps
from bot.analysis import compute_technicals, htf_trend
from bot.client import AlpacaService
from bot.config import Config, language_name, normalize_size_mode
from bot.earnings import fetch_earnings
from bot.econ_calendar import fetch_economic_calendar
from bot.lessons_store import lessons_for_prompt
from bot.metals_intel import fetch_metals_macro_context, is_precious_metal
from bot.news import fetch_news

logger = logging.getLogger(__name__)

# Enough history for SMA50/ADX to be meaningful rather than barely defined.
BAR_LOOKBACK = 250


class AiBrain:
    def __init__(self, config: Config, service: AlpacaService, provider: AiProvider) -> None:
        self.config = config
        self.service = service
        self.provider = provider

    def decide(
        self, symbol: str, context: dict[str, Any] | None = None
    ) -> tuple[AiDecision, dict[str, Any]]:
        context = context if context is not None else self.build_context(symbol)
        prompt = self._format_prompt(symbol, context)
        max_qty = self._max_qty(context)
        raw = self.provider.complete_json(prompt)
        decision = normalize_decision(
            raw,
            provider=self.provider.name,
            model=self.provider.model,
            max_qty=max_qty,
        )
        decision = self._apply_safety_gates(decision, context)
        return decision, context

    def _size_mode(self) -> str:
        return normalize_size_mode(getattr(self.config, "size_mode", "qty"))

    def _max_qty(self, context: dict[str, Any]) -> float:
        """Position size for a new entry — risk-based when possible.

        Risk sizing makes every trade cost the same fraction of equity at its
        stop, which fixed share/dollar sizing does not. In AI size mode this
        value is a ceiling — the model may pick a smaller qty from the tape.
        """
        mark = context.get("mark") or {}
        try:
            price = float(mark.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0

        risk = context.get("risk") or {}
        stop_distance = risk.get("stop_distance")
        equity = ((context.get("account") or {}).get("equity")) or 0
        sizer = getattr(self.config, "ai_qty_for_risk", None)
        if callable(sizer):
            risk_qty = sizer(price, stop_distance or 0, equity)
            if risk_qty and risk_qty > 0:
                return float(risk_qty)

        if self._size_mode() == "ai":
            return self._ai_qty_cap(context, price)

        resolver = getattr(self.config, "order_qty_for_price", None)
        if callable(resolver) and price > 0:
            try:
                return float(resolver(price))
            except ValueError:
                pass
        return float(getattr(self.config, "trade_qty", 1) or 1)

    def _ai_qty_cap(self, context: dict[str, Any], price: float) -> float:
        """Hard ceiling when the model picks size and risk sizing is off."""
        account = context.get("account") or {}
        try:
            buying_power = float(account.get("buying_power") or 0)
        except (TypeError, ValueError):
            buying_power = 0.0
        try:
            equity = float(account.get("equity") or 0)
        except (TypeError, ValueError):
            equity = 0.0
        budget = buying_power or equity
        if price > 0 and budget > 0:
            # One name should not consume the whole book if risk % is off.
            return max(0.0, (budget * 0.25) / price)
        return float(getattr(self.config, "trade_qty", 1) or 1)

    def _apply_safety_gates(
        self, decision: AiDecision, context: dict[str, Any]
    ) -> AiDecision:
        """Align model output with paper-desk rules (confidence + position)."""
        position_qty = float(context.get("position_qty") or 0)
        reason: str | None = None
        action = decision.action

        earnings = context.get("earnings") or {}
        last_result = str(earnings.get("last_result") or "")
        react_miss = earnings.get("stance") == "react" and last_result == "miss"
        react_beat = earnings.get("stance") == "react" and last_result == "beat"

        if action == "buy" and position_qty > 0:
            reason = "Buy requested while already long — holding."
            action = "hold"
        elif action == "sell" and position_qty < 0:
            reason = "Sell requested while already short — holding."
            action = "hold"
        elif (
            earnings.get("blackout")
            and action == "buy"
            and position_qty == 0
        ):
            reason = (
                "Earnings blackout — holding new longs until after the print. "
                + str(earnings.get("plan") or "")
            ).strip()
            action = "hold"
        elif (
            earnings.get("blackout")
            and action == "sell"
            and position_qty == 0
        ):
            reason = (
                "Earnings blackout — holding new shorts until after the print. "
                + str(earnings.get("plan") or "")
            ).strip()
            action = "hold"
        elif action == "buy" and position_qty == 0 and react_miss:
            reason = (
                "Earnings miss — holding new longs. "
                + str(earnings.get("plan") or "")
            ).strip()
            action = "hold"
        elif action == "sell" and position_qty == 0 and react_beat:
            reason = (
                "Earnings beat — holding new shorts. "
                + str(earnings.get("plan") or "")
            ).strip()
            action = "hold"
        elif (
            action in {"buy", "sell"}
            and decision.confidence < self.config.ai_min_confidence
        ):
            reason = (
                f"Confidence {decision.confidence:.2f} below threshold "
                f"{self.config.ai_min_confidence:.2f}."
            )
            action = "hold"
        elif action in {"buy", "sell"} and position_qty != 0:
            # Closing or flipping an existing position — churn guards apply.
            gate = reversal_gate(self.config, context, decision.confidence)
            if not gate.allowed:
                reason = gate.reason
                action = "hold"

        if action == decision.action:
            return self._size_decision(decision, context)
        return self.hold_decision(decision, reason or "")

    def hold_decision(self, decision: AiDecision, reason: str) -> AiDecision:
        """Rewrite a decision as a flat hold, preserving the original rationale."""
        thesis = decision.thesis
        thesis_en = decision.thesis_en
        if reason:
            # The gate reason is authored in English; it prefixes both copies so
            # neither the localized note nor the English one loses the override.
            thesis = f"{reason} Original: {thesis}" if thesis else reason
            thesis_en = f"{reason} Original: {thesis_en}" if thesis_en else reason
        return normalize_decision(
            {
                **decision.raw,
                "action": "hold",
                "qty": 0,
                "thesis": thesis,
                "thesis_en": thesis_en,
                "risks": decision.risks,
                "risks_en": decision.risks_en,
                "news_bias": decision.news_bias,
                "ta_bias": decision.ta_bias,
                "confidence": decision.confidence,
            },
            provider=decision.provider,
            model=decision.model,
            max_qty=0,
        )

    def _size_decision(
        self, decision: AiDecision, context: dict[str, Any]
    ) -> AiDecision:
        """Scale an approved entry by confidence; exits always use full size.

        When size_mode is ai the model already sized from market conditions,
        so confidence is only a yes/no gate — not a second size dial.
        """
        if decision.action == "hold" or decision.qty <= 0:
            return decision
        if float(context.get("position_qty") or 0) != 0:
            # Closing or covering — never trim the size that gets us flat.
            return decision
        if self._size_mode() == "ai":
            return decision
        scaled = confidence_scaled_qty(self.config, decision.qty, decision.confidence)
        if abs(scaled - decision.qty) < 1e-9:
            return decision
        return normalize_decision(
            {**decision.raw, "action": decision.action, "qty": scaled},
            provider=decision.provider,
            model=decision.model,
            max_qty=decision.qty,
        )

    def build_context(
        self,
        symbol: str,
        *,
        shared_account: dict[str, Any] | None = None,
        shared_calendar: list[dict[str, Any]] | None = None,
        shared_session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbol = symbol.upper().strip()
        bars = self.service.get_bars(symbol, limit=BAR_LOOKBACK)
        technicals = compute_technicals(bars)
        news = fetch_news(symbol, limit=8)
        calendar = (
            shared_calendar
            if shared_calendar is not None
            else fetch_economic_calendar(hours_ahead=72, hours_behind=12)
        )
        earnings = fetch_earnings(symbol)
        position = self.service.get_position_detail(symbol)
        position_qty = float(position.get("qty") or 0)
        account = (
            shared_account
            if shared_account is not None
            else self.service.account_summary()
        )
        activity = self.service.recent_activity(symbol)
        try:
            mark = self.service.get_mark_price(symbol)
        except Exception as exc:
            mark = {"error": str(exc)}
        if (
            technicals.get("ok")
            and isinstance(mark.get("price"), (int, float))
            and float(mark["price"]) > 0
        ):
            technicals["price"] = round(float(mark["price"]), 4)
            technicals["price_kind"] = "live_mark"
        session = (
            shared_session
            if shared_session is not None
            else self.service.market_session()
        )
        position_side = (
            "long" if position_qty > 0 else "short" if position_qty < 0 else "flat"
        )

        price = 0.0
        if isinstance(mark.get("price"), (int, float)):
            price = float(mark["price"])
        atr = technicals.get("atr_14") if technicals.get("ok") else None
        distancer = getattr(self.config, "ai_stop_distance", None)
        stop_distance = distancer(price, atr) if callable(distancer) else 0.0
        avg_entry = position.get("avg_entry")
        open_r = r_multiple(
            side=position_side,
            entry=avg_entry,
            price=price,
            stop_distance=stop_distance,
        )

        context: dict[str, Any] = {
            "symbol": symbol,
            "bar_timeframe": self.config.bar_timeframe,
            "session": session,
            "mark": mark,
            "spread_bps": spread_bps(mark),
            "position_qty": position_qty,
            "position_side": position_side,
            "position": {
                "side": position_side,
                "qty": position_qty,
                "avg_entry": avg_entry,
                "unrealized_pct": position.get("unrealized_pct"),
                "unrealized_pl": position.get("unrealized_pl"),
                "r_multiple": open_r,
                "stop_price": self.service.current_stop_price(symbol),
                "age_minutes": activity.get("last_fill_age_min"),
            },
            "activity": activity,
            "account": {
                "equity": account.get("equity"),
                "cash": account.get("cash"),
                "buying_power": account.get("buying_power"),
                "day_pl_pct": account.get("day_pl_pct"),
                "paper": account.get("paper"),
            },
            "technicals": technicals,
            "higher_timeframe": self._higher_timeframe(symbol),
            "news": news,
            "economic_calendar": calendar,
            "earnings": earnings,
            "risk": {
                "stop_distance": round(stop_distance, 4) if stop_distance else None,
                "stop_distance_pct": (
                    round(stop_distance / price * 100, 2)
                    if (stop_distance and price > 0)
                    else None
                ),
                "risk_pct_per_trade": getattr(self.config, "ai_risk_pct", None),
                "atr_stop_mult": getattr(self.config, "ai_atr_stop_mult", None),
                "take_profit_r": getattr(self.config, "ai_take_profit_r", None),
                "trail_after_r": getattr(self.config, "ai_trail_after_r", None),
                "max_positions": getattr(self.config, "ai_max_positions", None),
                "daily_loss_limit_pct": getattr(
                    self.config, "ai_daily_loss_limit_pct", None
                ),
                "min_hold_minutes": getattr(self.config, "ai_min_hold_minutes", None),
                "max_spread_bps": getattr(self.config, "ai_max_spread_bps", None),
            },
        }
        # Rules the trader approved on the History page after reviewing what
        # actually happened. Advisory only — they never bypass a risk gate.
        context["desk_lessons"] = self._desk_lessons(symbol)
        context["limits"] = {
            "max_qty": self._max_qty(context),
            "size_mode": getattr(self.config, "size_mode", "qty"),
            "trade_notional": getattr(self.config, "trade_notional", None),
            "min_confidence": self.config.ai_min_confidence,
            "stop_loss_pct": self.config.stop_loss_pct,
        }
        if is_precious_metal(symbol):
            context["precious_metals_intel"] = fetch_metals_macro_context(
                self.service, symbol, calendar=calendar
            )
        return context

    def _desk_lessons(self, symbol: str) -> list[str]:
        """Human-approved lessons from History that apply to this name."""
        try:
            return lessons_for_prompt(
                symbol=symbol, preset_id=getattr(self.config, "ai_preset", None)
            )
        except Exception as exc:
            # A missing or corrupt lessons file must never stop a trading cycle.
            logger.debug("desk lessons unavailable: %s", exc)
            return []

    def _higher_timeframe(self, symbol: str) -> dict[str, Any]:
        """Daily trend read used to veto counter-trend intraday entries."""
        timeframe = str(self.config.bar_timeframe or "").lower()
        if timeframe in {"1day", "1d", "day"}:
            return {"ok": False, "bias": "same_as_primary"}
        try:
            daily = self.service.get_bars(symbol, limit=120, timeframe="1Day")
        except Exception as exc:
            logger.debug("higher timeframe bars unavailable for %s: %s", symbol, exc)
            return {"ok": False, "bias": "unknown"}
        return htf_trend(daily)

    def _format_prompt(self, symbol: str, context: dict[str, Any]) -> str:
        instructions = (self.config.ai_instructions or "").strip() or "(none)"
        max_qty = self._max_qty(context)
        target_lang = language_name(getattr(self.config, "lang", None))
        payload = {
            "symbol": symbol,
            "target_language": target_lang,
            "strategy_preset": self.config.ai_preset,
            "instructions": instructions,
            "bar_timeframe": context.get("bar_timeframe") or self.config.bar_timeframe,
            "position_qty": context["position_qty"],
            "position_side": context.get("position_side")
            or (
                "long"
                if float(context.get("position_qty") or 0) > 0
                else "short"
                if float(context.get("position_qty") or 0) < 0
                else "flat"
            ),
            "max_qty": max_qty,
            "size_mode": getattr(self.config, "size_mode", "qty"),
            "trade_notional": getattr(self.config, "trade_notional", None),
            "min_confidence": self.config.ai_min_confidence,
            "stop_loss_pct": self.config.stop_loss_pct,
            "session": context["session"],
            "mark": context["mark"],
            "spread_bps": context.get("spread_bps"),
            "account": context["account"],
            "position": context.get("position") or {},
            "risk": context.get("risk") or {},
            "technicals": context["technicals"],
            "higher_timeframe": context.get("higher_timeframe") or {},
            "news": context.get("news") or [],
            "economic_calendar": context.get("economic_calendar") or [],
            "earnings": context.get("earnings") or {},
            "desk_lessons": context.get("desk_lessons") or [],
            "precious_metals_intel": context.get("precious_metals_intel") or {},
        }
        return (
            f"Decide buy/sell/hold for this symbol using the JSON context.\n"
            f"CRITICAL LANGUAGE REQUIREMENT: write 'thesis' and 'risks' entirely in {target_lang}, "
            f"and always write 'thesis_en' and 'risks_en' in English. Both pairs must carry the same "
            f"reasoning — the desk keeps the English pair so the reader can switch languages later.\n"
            "Follow the playbook in instructions (from strategy_preset or custom text).\n"
            "Technicals are computed on bar_timeframe bars.\n"
            "This desk is long/short. position_qty > 0 is long, < 0 is short, 0 is flat.\n"
            "Buy while flat opens a long. Buy while short covers the short.\n"
            "Sell while flat opens a short (whole shares). Sell while long exits the long.\n"
            "Do not add to an existing long or short. Do not size above max_qty.\n"
            "Go long when analysis is clearly bullish; short when clearly bearish; "
            "hold when mixed, thin, or near high-impact USD events unless instructions "
            "say otherwise.\n"
            "\n"
            + (
                "SIZING (size_mode is ai):\n"
                "You choose qty from market conditions. Use technicals.atr_pct, "
                "technicals.regime, spread_bps, account.buying_power, and your "
                "confidence. Size toward max_qty only when trend, news, and "
                "higher timeframe agree. Cut size in chop, wide spreads, extended "
                "moves (dist_sma50_atr beyond +/-2), or mixed evidence. "
                "Never exceed max_qty. Exits still flatten the full position.\n"
                "\n"
                if self._size_mode() == "ai"
                else ""
            )
            + (
                "PRECIOUS METALS & CROSS-ASSET MACRO INTELLIGENCE:\n"
                "This symbol is precious metals / commodities related. Use precious_metals_intel in your analysis.\n"
                "These fields are calibrated against 2007-2025 forward-return studies on GLD — where they\n"
                "contradict chart intuition, the calibration wins.\n"
                "- macro_composite_score (-3..+3) and metals_macro_bias: a weighted blend of rates (0.40),\n"
                "  Gold/Silver ratio (0.35), long-term trend (0.15) and the dollar (0.10). Mean forward 20d\n"
                "  GLD return by score band was: <=-1.5 -> -0.29%, -1.5..-0.5 -> -0.18%, -0.5..+0.5 -> +0.53%,\n"
                "  +0.5..+1.5 -> +1.14%, >=+1.5 -> +1.09%.\n"
                "  * Score >= +0.5: a genuine tailwind. Above +1.5, size up and consider higher-beta vehicles.\n"
                "  * Score <= -0.5: forward returns were NEGATIVE on average. Treat as a no-buy band, not a dip.\n"
                "  * factor_scores breaks the score into its parts if you need to see what is driving it.\n"
                "- trend_regime: 'bullish_above_sma200' or 'bearish_below_sma200'. This is the participation\n"
                "  gate that keeps the desk out of multi-year metals bear markets. Respect it.\n"
                "- Gold/Silver Ratio: gsr_live is the live GLD/SLV ratio; gsr_z_score is its deviation from the\n"
                "  gsr_z_window mean (1 year when history allows — far more informative than a 20d window).\n"
                "  * gsr_z_score >= +1.2: the ratio is stretched. Historically a risk-off bid that lifted the\n"
                "    whole complex, with silver the higher-beta catch-up expression. Bullish, not just relative.\n"
                "  * gsr_z_score <= -1.2: the ratio is compressed — a risk-on tell that preceded BELOW-average\n"
                "    bullion returns. Do not read it as 'gold is cheap, buy gold'.\n"
                "- miners_signal / gdx_gld_ratio: context only. Miner leadership was measured to have no\n"
                "  predictive power for gold (IC -0.01). Never let it carry a trade decision on its own.\n"
                "- Gold's short-term momentum mean-reverts: 5-20 day momentum is NEGATIVELY correlated with the\n"
                "  next month's return. Favour pullback entries inside an intact uptrend over fresh breakouts.\n"
                "- If macro_risk_level is 'imminent_release' (high-impact FOMC/CPI within 45 mins), HOLD unless instructions permit trading catalysts.\n"
                "\n"
                if context.get("precious_metals_intel")
                else ""
            )
            + "MANAGING AN OPEN POSITION (position.qty != 0):\n"
            "position.r_multiple is open profit in units of initial risk — the single "
            "most important number for an exit. Negative means underwater.\n"
            "The bot already moves the stop to breakeven and trails it past "
            "risk.trail_after_r, and scales out at risk.take_profit_r. Do not exit "
            "early just to lock a small gain — that is handled. Exit when the reason "
            "you entered is gone (trend flip, momentum loss, bearish catalyst), not "
            "because of normal noise.\n"
            "Never exit a losing position manually unless the thesis is broken; the "
            "protective stop defines the loss.\n"
            "\n"
            "REGIME AND COST:\n"
            "technicals.regime is 'chop' when ADX < 20 — trend and momentum setups "
            "fail there, so prefer hold unless the playbook is mean-reversion.\n"
            "technicals.atr_pct is bar volatility as a percent of price; "
            "technicals.dist_sma50_atr is how stretched price is from SMA50 in ATR "
            "units (beyond +/-3 is extended — do not chase).\n"
            "higher_timeframe.bias is the daily trend; avoid opening against it.\n"
            "spread_bps is the round-trip cost proxy — a thesis worth less than the "
            "spread is not a trade.\n"
            "\n"
            + (
                "DESK LESSONS:\n"
                "desk_lessons are rules the trader approved after reviewing this "
                "desk's own past results. Treat them as strong preferences that "
                "sit above the generic playbook. They never authorize a trade the "
                "risk rules forbid — when a lesson and a safety rule disagree, the "
                "safety rule wins.\n"
                "\n"
                if context.get("desk_lessons")
                else ""
            )
            + "Follow earnings.plan. If earnings.blackout is true, do not open a new long "
            "or short (flattening is allowed). If stance is react: a beat can open/hold "
            "a long and should cover a short; a miss can open a short and should exit "
            "a long. Do not invent an earnings date or EPS figure.\n"
            "mark.price is the live last sale. Technicals use that live mark as the "
            "in-progress bar close (not the 09:30 open or prior session close).\n"
            "\n"
            "SESSION AND EXTENDED-HOURS TRADING:\n"
            "The desk trades 24/5 across regular, pre-market, after-hours, and overnight sessions using extended-hours limit orders.\n"
            "When session is 'overnight', 'premarket', or 'afterhours', mark and technical indicators reflect real-time live trading data. "
            "Do NOT hold merely because regular market hours are closed or session is 'overnight' / 'premarket' / 'afterhours'. "
            "Actively evaluate the setup, momentum, trend, spread, and catalysts and decide buy/sell/hold accordingly.\n"
            f"REMINDER: 'thesis' and 'risks' in {target_lang}; 'thesis_en' and 'risks_en' in English.\n\n"
            f"{json.dumps(payload, default=str, indent=2)}"
        )
