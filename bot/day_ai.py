"""Intraday AI confirmation engine and real-time catalyst/sentiment filter for Day Trading."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from bot.ai_models import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_XAI_MODEL,
)
from bot.ai_providers import (
    AiProvider,
    build_provider,
)
from bot.analysis import compute_technicals, htf_trend
from bot.client import AlpacaService
from bot.config import Config, language_name
from bot.earnings import fetch_earnings
from bot.econ_calendar import fetch_economic_calendar
from bot.news import fetch_news
from bot.strategy import Signal

logger = logging.getLogger(__name__)

DAY_AI_SYSTEM_PROMPT = """You are an elite Intraday Day Trading Risk and Execution Specialist.
Your job is to critically evaluate an algorithmic technical intraday trade signal (VWAP crossover, Opening Range Breakout, or Momentum Scalp) for a single stock before execution.

Rules:
1. Respond with ONLY valid JSON (no markdown fences, no extra text).
2. Evaluate if the technical setup is backed by institutional tape, sentiment, and market structure, or if it is a TRAP / CHOP / HIGH-RISK setup.
3. Be especially vigilant about:
   - Impending high-impact economic releases (e.g. FOMC, CPI, Fed speeches within the next 1-2 hours) -> VETO or reduce confidence.
   - Earnings releases today or recent unexpected earnings reactions -> VETO or proceed with extreme caution.
   - Low-volume false breakouts or intraday breakouts fighting the Higher Timeframe (Daily) dominant trend.
   - Extended RSI or overstretched price far from VWAP.
4. If the setup is clean, aligned with news/trend, and has high probability of continuation, set confirm=true and confidence >= 0.70.
5. If the setup is high risk, counter-trend without catalyst, or trapped in chop, set confirm=false and explain why in thesis and risk_warning.
6. LANGUAGE RULE: write "thesis" and "risk_warning" in the target language requested in target_language (e.g. Bangla for bn, Spanish for es, French for fr, Hindi for hi, English for en), and ALWAYS write "thesis_en" and "risk_warning_en" in English.

JSON Schema:
{
  "confirm": true | false,
  "confidence": 0.0 to 1.0,
  "action_bias": "bullish" | "bearish" | "neutral",
  "thesis": "concise explanation in target_language",
  "thesis_en": "concise explanation in English",
  "risk_warning": "key intraday risk in target_language",
  "risk_warning_en": "key intraday risk in English",
  "target_r_adjustment": number or null
}
"""


@dataclass(frozen=True)
class DayAiDecision:
    confirm: bool
    confidence: float
    action_bias: str
    thesis: str
    thesis_en: str
    risk_warning: str
    risk_warning_en: str
    target_r_adjustment: float | None
    raw: dict[str, Any]
    provider: str = ""
    model: str = ""


def _parse_day_ai_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data
    raise ValueError(f"Model did not return JSON object: {text[:240]!r}")


def normalize_day_ai_decision(
    raw: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
) -> DayAiDecision:
    confirm = bool(raw.get("confirm", False))
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    action_bias = str(raw.get("action_bias") or "neutral").strip().lower()
    if action_bias not in {"bullish", "bearish", "neutral"}:
        action_bias = "neutral"

    thesis = str(raw.get("thesis") or "").strip()[:400]
    thesis_en = str(raw.get("thesis_en") or "").strip()[:400] or thesis
    risk_warning = str(raw.get("risk_warning") or "").strip()[:300]
    risk_warning_en = str(raw.get("risk_warning_en") or "").strip()[:300] or risk_warning

    target_r = None
    try:
        if raw.get("target_r_adjustment") is not None:
            val = float(raw["target_r_adjustment"])
            if 0.5 <= val <= 5.0:
                target_r = round(val, 2)
    except (TypeError, ValueError):
        pass

    return DayAiDecision(
        confirm=confirm,
        confidence=confidence,
        action_bias=action_bias,
        thesis=thesis,
        thesis_en=thesis_en,
        risk_warning=risk_warning,
        risk_warning_en=risk_warning_en,
        target_r_adjustment=target_r,
        raw=raw,
        provider=provider,
        model=model,
    )


class DayAiBrain:
    """Intraday AI confirmation brain for validating Day Trading signals."""

    def __init__(
        self,
        config: Config,
        service: AlpacaService,
        provider: AiProvider | None = None,
    ) -> None:
        self.config = config
        self.service = service
        self._provider = provider

    def get_provider(self) -> AiProvider | None:
        if self._provider is not None:
            return self._provider
        try:
            p_name = getattr(self.config, "day_ai_provider", None) or self.config.ai_provider
            self._provider = build_provider(
                p_name,
                openai_key=self.config.openai_api_key,
                gemini_key=self.config.gemini_api_key,
                anthropic_key=self.config.anthropic_api_key,
                xai_key=self.config.xai_api_key,
                openai_model=getattr(self.config, "openai_model", DEFAULT_OPENAI_MODEL),
                gemini_model=getattr(self.config, "gemini_model", DEFAULT_GEMINI_MODEL),
                anthropic_model=getattr(self.config, "anthropic_model", DEFAULT_ANTHROPIC_MODEL),
                xai_model=getattr(self.config, "xai_model", DEFAULT_XAI_MODEL),
            )
            return self._provider
        except Exception as exc:
            logger.warning("Could not instantiate AI provider for Day Trading: %s", exc)
            return None

    def build_intraday_context(
        self,
        symbol: str,
        signal: Signal,
        trigger_price: float,
        trigger_reason: str,
        bars: pd.DataFrame,
        vwap_info: dict[str, Any] | None = None,
        orb_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Construct a dense, fast intraday context bundle for LLM evaluation."""
        symbol = symbol.upper().strip()
        technicals = compute_technicals(bars) if not bars.empty else {}
        news = fetch_news(symbol, limit=5)
        calendar = fetch_economic_calendar(hours_ahead=24, hours_behind=4)
        earnings = fetch_earnings(symbol)
        
        # High timeframe trend check (Daily)
        htf = None
        try:
            daily_bars = self.service.get_bars(symbol, limit=60, timeframe="1Day")
            if not daily_bars.empty:
                htf = htf_trend(daily_bars)
        except Exception as exc:
            logger.debug("HTF trend lookup failed for %s: %s", symbol, exc)

        try:
            mark = self.service.get_mark_price(symbol)
        except Exception:
            mark = {"price": trigger_price}

        return {
            "symbol": symbol,
            "target_language": language_name(getattr(self.config, "lang", "en")),
            "signal": signal.value,
            "trigger_price": trigger_price,
            "trigger_reason": trigger_reason,
            "mark": mark,
            "vwap_data": vwap_info or {},
            "orb_data": orb_info or {},
            "intraday_technicals": technicals,
            "higher_timeframe_daily": htf,
            "breaking_news": [
                {"headline": n.get("headline"), "summary": n.get("summary")[:140] if n.get("summary") else ""}
                for n in news[:4]
            ],
            "upcoming_economic_events": [
                {"event": e.get("event"), "impact": e.get("impact"), "time": e.get("time")}
                for e in calendar[:3]
            ],
            "earnings": {
                "blackout": earnings.get("blackout", False),
                "last_result": earnings.get("last_result"),
                "plan": earnings.get("plan"),
            },
        }

    def _format_prompt(self, context: dict[str, Any]) -> str:
        return (
            "Evaluate the following Intraday Day Trading signal and decide whether to CONFIRM or VETO:\n\n"
            + json.dumps(context, indent=2, default=str)
        )

    def evaluate_signal(
        self,
        symbol: str,
        signal: Signal,
        trigger_price: float,
        trigger_reason: str,
        bars: pd.DataFrame,
        vwap_info: dict[str, Any] | None = None,
        orb_info: dict[str, Any] | None = None,
    ) -> DayAiDecision:
        """Query the AI model to confirm or veto the day trading signal."""
        provider = self.get_provider()
        if provider is None:
            # Fallback: if no AI provider configured, confirm with moderate confidence
            return DayAiDecision(
                confirm=True,
                confidence=0.75,
                action_bias="bullish" if signal == Signal.BUY else "bearish",
                thesis="AI provider not configured; proceeding with technical rules.",
                thesis_en="AI provider not configured; proceeding with technical rules.",
                risk_warning="",
                risk_warning_en="",
                target_r_adjustment=None,
                raw={},
                provider="none",
                model="none",
            )

        context = self.build_intraday_context(
            symbol, signal, trigger_price, trigger_reason, bars, vwap_info, orb_info
        )
        prompt = self._format_prompt(context)

        try:
            raw = provider.complete_json(prompt, system=DAY_AI_SYSTEM_PROMPT)
            return normalize_day_ai_decision(
                raw,
                provider=getattr(provider, "name", "ai"),
                model=getattr(provider, "model", ""),
            )
        except Exception as exc:
            logger.warning("AI confirmation call failed for %s: %s", symbol, exc)
            # Safe fallback on error
            return DayAiDecision(
                confirm=False,
                confidence=0.0,
                action_bias="neutral",
                thesis=f"AI evaluation failed: {exc}",
                thesis_en=f"AI evaluation failed: {exc}",
                risk_warning=str(exc),
                risk_warning_en=str(exc),
                target_r_adjustment=None,
                raw={"error": str(exc)},
                provider=getattr(provider, "name", "ai"),
                model=getattr(provider, "model", ""),
            )
