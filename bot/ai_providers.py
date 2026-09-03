"""LLM providers: OpenAI, Google Gemini, Anthropic, and xAI."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

from bot.ai_models import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_XAI_MODEL,
)

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


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = DEFAULT_ANTHROPIC_MODEL) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is missing")
        from anthropic import Anthropic

        self.model = model
        self._client = Anthropic(
            api_key=api_key, timeout=_request_timeout(), max_retries=1
        )

    def complete_json(
        self, user_prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 2048,
            "system": system or SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        # Claude 5 family models think by default; sampling params (temperature/
        # top_p/top_k) are rejected while thinking is on. Only the Haiku 4.5
        # tier (budget_tokens-based thinking, off by default) accepts them.
        if not _omit_temperature(self.model):
            kwargs["temperature"] = 0.2
        response = self._client.messages.create(**kwargs)
        content = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        return _parse_json(content or "{}")


class XaiProvider:
    name = "xai"

    def __init__(self, api_key: str, model: str = DEFAULT_XAI_MODEL) -> None:
        if not api_key:
            raise ValueError("XAI_API_KEY is missing")
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1",
            timeout=_request_timeout(),
            max_retries=1,
        )

    def complete_json(
        self, user_prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        response = self._client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system or SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content or "{}"
        return _parse_json(content)


def build_provider(
    provider: str,
    *,
    openai_key: str,
    gemini_key: str,
    anthropic_key: str,
    xai_key: str,
    openai_model: str,
    gemini_model: str,
    anthropic_model: str,
    xai_model: str,
) -> AiProvider:
    name = (provider or "openai").strip().lower()
    if name == "gemini":
        return GeminiProvider(gemini_key, gemini_model)
    if name == "anthropic":
        return AnthropicProvider(anthropic_key, anthropic_model)
    if name == "xai":
        return XaiProvider(xai_key, xai_model)
    if name == "openai":
        return OpenAiProvider(openai_key, openai_model)
    raise ValueError(f"Unknown AI provider: {provider!r} (use openai, gemini, anthropic, or xai)")


_BIAS_VALUES = frozenset({"bullish", "bearish", "neutral"})


def _omit_temperature(model: str) -> bool:
    name = (model or "").strip().lower()
    return (
        name.startswith("gpt-5")
        or name.startswith("o1")
        or name.startswith("o3")
        or name.startswith("o4")
        or name.startswith("claude-opus-5")
        or name.startswith("claude-sonnet-5")
        or name.startswith("claude-fable-5")
    )


def _normalize_bias(value: Any) -> str:
    text = str(value or "neutral").strip().lower()
    if text in _BIAS_VALUES:
        return text
    if "bull" in text:
        return "bullish"
    if "bear" in text:
        return "bearish"
    return "neutral"


# Fraction of `max_qty` used when the model returns no usable size.
#
# In the desk's own size modes `max_qty` *is* the intended position (fixed qty,
# a dollar amount, or the risk-engine size), so a missing qty is only a missing
# echo — half of it is already conservative. In AI size mode the model was asked
# to pick the size itself and `max_qty` is nothing but a ceiling, so a missing
# qty means no intent was expressed at all: take a starter position instead of
# half the largest one allowed.
DEFAULT_MISSING_QTY_SCALE = 0.5
AI_SIZED_MISSING_QTY_SCALE = 0.25


def normalize_decision(
    raw: dict[str, Any],
    *,
    provider: str,
    model: str,
    max_qty: float,
    qty_fallback_scale: float = DEFAULT_MISSING_QTY_SCALE,
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
        # scale it down, keeping at least one share when whole shares are
        # tradeable (shorts and extended-hours orders cannot be fractional).
        scale = max(0.0, min(1.0, float(qty_fallback_scale)))
        qty = float(max_qty) * scale
        if float(max_qty) >= 1:
            qty = max(1.0, qty)
        # A model that keeps skipping qty is a prompt or provider problem, and
        # it is invisible otherwise — every cycle just silently gets this size.
        logger.warning(
            "%s/%s returned no usable qty for a %s; falling back to %.4f "
            "(%.0f%% of max_qty %.4f)",
            provider,
            model,
            action,
            qty,
            scale * 100,
            float(max_qty),
        )
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
