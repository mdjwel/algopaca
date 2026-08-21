"""LLM providers: OpenAI and Google Gemini."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

from bot.ai_models import DEFAULT_GEMINI_MODEL, DEFAULT_OPENAI_MODEL

logger = logging.getLogger(__name__)


def _request_timeout() -> float:
    """Seconds a single model call may take before it is abandoned.

    The SDK defaults (10 minutes, two silent retries) let one wedged request
    hold a trading cycle — and therefore Stop — hostage for many minutes.
    """
    default = 60.0
    try:
        value = float(os.getenv("AI_REQUEST_TIMEOUT", default))
    except ValueError:
        return default
    return value if value > 0 else default


SYSTEM_PROMPT = """You are an equity trading decision engine for a PAPER trading bot.
You must choose buy, sell, or hold for ONE stock based on the supplied context.
This desk can be long or short.

Rules:
- Respond with ONLY valid JSON (no markdown fences).
- Prefer hold when evidence is mixed, thin, or confidence is low.
- Respect the user's custom instructions when they do not conflict with safety.
- Never invent prices, news, calendar events, or earnings figures that are not in the context.
- Weigh technicals, news tone, nearby high-impact economic events, and earnings together.
- Go long when the tape is clearly bullish; go short when clearly bearish; otherwise hold.
- Buy while flat opens a long. Buy while short covers. Sell while flat opens a short. Sell while long exits.
- Do not add to an existing long or short.
- Follow earnings.plan: no new long or short during an earnings blackout; after a print, trade the beat/miss.
- qty should be a positive number <= max_qty when action is buy or sell; else 0.
- When size_mode is "ai", YOU choose qty from market conditions (volatility, regime,
  spread, conviction, buying power). Size toward max_qty only when trend, news, and
  higher timeframe agree. Cut size in chop, wide spreads, extended moves, or mixed
  evidence. Never exceed max_qty.
- When size_mode is qty or notional, max_qty is already risk-sized, so use it in full
  for a high-conviction entry. Omitting qty gets you a reduced size.
- When a position is open, read position.r_multiple before deciding. The bot handles breakeven stops,
  trailing, and scaling out — exit only when the entry thesis is broken.
- Prefer hold when technicals.regime is "chop" unless the playbook is explicitly mean-reversion.
- confidence must reflect genuine evidence strength; it scales position size, not just the yes/no gate.
- LANGUAGE RULE: write "thesis" and "risks" in the target language requested in target_language
  (e.g., Bangla for bn, Spanish for es, French for fr, Hindi for hi, English for en), and ALWAYS
  write "thesis_en" and "risks_en" in English. Both pairs must say the same thing — the desk stores
  the English pair so the UI can switch back to English without re-running the model. When
  target_language is English, repeat the same text in both pairs.

JSON schema:
{
  "action": "buy" | "sell" | "hold",
  "confidence": 0.0-1.0,
  "qty": number,
  "thesis": "short rationale written in the requested target_language",
  "risks": "key risks in one sentence written in the requested target_language",
  "thesis_en": "the same rationale, always in English",
  "risks_en": "the same risks, always in English",
  "news_bias": "bullish" | "bearish" | "neutral",
  "ta_bias": "bullish" | "bearish" | "neutral"
}
"""


@dataclass(frozen=True)
class AiDecision:
    action: str
    confidence: float
    qty: float
    thesis: str
    risks: str
    # English originals kept beside the localized pair. Without them a note
    # written in Bangla stays Bangla forever — the UI has no way back.
    thesis_en: str
    risks_en: str
    news_bias: str
    ta_bias: str
    raw: dict[str, Any]
    provider: str
    model: str


class AiProvider(Protocol):
    name: str
    model: str

    def complete_json(
        self, user_prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        ...


class OpenAiProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str = DEFAULT_OPENAI_MODEL) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is missing")
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(
            api_key=api_key, timeout=_request_timeout(), max_retries=1
        )

    def complete_json(
        self, user_prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system or SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        # GPT-5 / o-series often reject custom temperature.
        if not _omit_temperature(self.model):
            kwargs["temperature"] = 0.2
        response = self._client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or "{}"
        return _parse_json(content)


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str = DEFAULT_GEMINI_MODEL) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is missing")
        from google import genai

        self.model = model
        # HttpOptions.timeout is milliseconds.
        self._client = genai.Client(
            api_key=api_key,
            http_options={"timeout": int(_request_timeout() * 1000)},
        )

    def complete_json(
        self, user_prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        response = self._client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config={
                "temperature": 0.2,
                "response_mime_type": "application/json",
                "system_instruction": system or SYSTEM_PROMPT,
            },
        )
        content = getattr(response, "text", None) or "{}"
        return _parse_json(content)


def build_provider(
    provider: str,
    *,
    openai_key: str,
    gemini_key: str,
    openai_model: str,
    gemini_model: str,
) -> AiProvider:
    name = (provider or "openai").strip().lower()
    if name == "gemini":
        return GeminiProvider(gemini_key, gemini_model)
    if name == "openai":
        return OpenAiProvider(openai_key, openai_model)
    raise ValueError(f"Unknown AI provider: {provider!r} (use openai or gemini)")


_BIAS_VALUES = frozenset({"bullish", "bearish", "neutral"})


def _omit_temperature(model: str) -> bool:
    name = (model or "").strip().lower()
    return name.startswith("gpt-5") or name.startswith("o1") or name.startswith("o3") or name.startswith("o4")


def _normalize_bias(value: Any) -> str:
    text = str(value or "neutral").strip().lower()
    if text in _BIAS_VALUES:
        return text
    if "bull" in text:
        return "bullish"
    if "bear" in text:
        return "bearish"
    return "neutral"


def normalize_decision(
    raw: dict[str, Any],
    *,
    provider: str,
    model: str,
    max_qty: float,
) -> AiDecision:
    action = str(raw.get("action") or "hold").strip().lower()
    if action not in {"buy", "sell", "hold"}:
        action = "hold"
    try:
        confidence = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    try:
        qty = float(raw.get("qty", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    qty = max(0.0, min(float(max_qty), qty))
    if action == "hold":
        qty = 0.0
    elif qty <= 0:
        # A missing or malformed size used to fall through to the *largest*
        # allowed position — a fail-open on risk. Treat it as low conviction and
        # halve it, keeping at least one share when whole shares are tradeable.
        qty = float(max_qty) * 0.5
        if float(max_qty) >= 1:
            qty = max(1.0, qty)
    thesis = str(raw.get("thesis") or "").strip()[:500]
    risks = str(raw.get("risks") or "").strip()[:400]
    return AiDecision(
        action=action,
        confidence=confidence,
        qty=qty,
        thesis=thesis,
        risks=risks,
        # A model that ignored the English fields still leaves a usable note —
        # the localized text — rather than a blank one.
        thesis_en=str(raw.get("thesis_en") or "").strip()[:500] or thesis,
        risks_en=str(raw.get("risks_en") or "").strip()[:400] or risks,
        news_bias=_normalize_bias(raw.get("news_bias")),
        ta_bias=_normalize_bias(raw.get("ta_bias")),
        raw=raw,
        provider=provider,
        model=model,
    )


def _parse_json(text: str) -> dict[str, Any]:
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
