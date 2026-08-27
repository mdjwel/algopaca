"""Curated LLM model catalogs for the AlgoPaca UI.

Keep the newest ~5 chat-capable models per provider. IDs are API model strings.
"""

from __future__ import annotations

from typing import Any

# Newest first. Labels are what the dropdown shows.
OPENAI_MODELS: tuple[dict[str, str], ...] = (
    {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol (flagship)"},
    {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra (balanced)"},
    {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna (fast / low cost)"},
    {"id": "gpt-5.4", "label": "GPT-5.4"},
    {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini"},
)

GEMINI_MODELS: tuple[dict[str, str], ...] = (
    {"id": "gemini-3.6-flash", "label": "Gemini 3.6 Flash (latest)"},
    {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash"},
    {"id": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite"},
    {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro (preview)"},
    {"id": "gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash-Lite"},
)

ANTHROPIC_MODELS: tuple[dict[str, str], ...] = (
    {"id": "claude-opus-5", "label": "Claude Opus 5 (flagship)"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5 (balanced)"},
    {"id": "claude-fable-5", "label": "Claude Fable 5"},
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5 (fast / low cost)"},
)

XAI_MODELS: tuple[dict[str, str], ...] = (
    {"id": "grok-4", "label": "Grok 4 (flagship)"},
    {"id": "grok-4-fast", "label": "Grok 4 Fast (balanced)"},
    {"id": "grok-3", "label": "Grok 3"},
    {"id": "grok-3-mini", "label": "Grok 3 Mini (fast / low cost)"},
)

DEFAULT_OPENAI_MODEL = OPENAI_MODELS[2]["id"]  # Luna — good desk default
DEFAULT_GEMINI_MODEL = GEMINI_MODELS[2]["id"]  # 3.5 Flash-Lite — cheap / fast
DEFAULT_ANTHROPIC_MODEL = ANTHROPIC_MODELS[3]["id"]  # Haiku 4.5 — cheap / fast default
DEFAULT_XAI_MODEL = XAI_MODELS[3]["id"]  # Grok 3 Mini — cheap / fast default


def list_models() -> dict[str, list[dict[str, str]]]:
    return {
        "openai": [dict(m) for m in OPENAI_MODELS],
        "gemini": [dict(m) for m in GEMINI_MODELS],
        "anthropic": [dict(m) for m in ANTHROPIC_MODELS],
        "xai": [dict(m) for m in XAI_MODELS],
        "defaults": {
            "openai": DEFAULT_OPENAI_MODEL,
            "gemini": DEFAULT_GEMINI_MODEL,
            "anthropic": DEFAULT_ANTHROPIC_MODEL,
            "xai": DEFAULT_XAI_MODEL,
        },
    }


def resolve_openai_model(model: str | None) -> str:
    text = (model or "").strip()
    if not text:
        return DEFAULT_OPENAI_MODEL
    return text


def resolve_gemini_model(model: str | None) -> str:
    text = (model or "").strip()
    if not text:
        return DEFAULT_GEMINI_MODEL
    return text


def resolve_anthropic_model(model: str | None) -> str:
    text = (model or "").strip()
    if not text:
        return DEFAULT_ANTHROPIC_MODEL
    return text


def resolve_xai_model(model: str | None) -> str:
    text = (model or "").strip()
    if not text:
        return DEFAULT_XAI_MODEL
    return text


def known_model_ids(provider: str) -> set[str]:
    if provider == "gemini":
        return {m["id"] for m in GEMINI_MODELS}
    if provider == "anthropic":
        return {m["id"] for m in ANTHROPIC_MODELS}
    if provider == "xai":
        return {m["id"] for m in XAI_MODELS}
    return {m["id"] for m in OPENAI_MODELS}


def catalog_payload() -> dict[str, Any]:
    """Shape returned to the web UI."""
    return list_models()
