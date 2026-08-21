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

DEFAULT_OPENAI_MODEL = OPENAI_MODELS[2]["id"]  # Luna — good desk default
DEFAULT_GEMINI_MODEL = GEMINI_MODELS[2]["id"]  # 3.5 Flash-Lite — cheap / fast


def list_models() -> dict[str, list[dict[str, str]]]:
    return {
        "openai": [dict(m) for m in OPENAI_MODELS],
        "gemini": [dict(m) for m in GEMINI_MODELS],
        "defaults": {
            "openai": DEFAULT_OPENAI_MODEL,
            "gemini": DEFAULT_GEMINI_MODEL,
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


def known_model_ids(provider: str) -> set[str]:
    if provider == "gemini":
        return {m["id"] for m in GEMINI_MODELS}
    return {m["id"] for m in OPENAI_MODELS}


def catalog_payload() -> dict[str, Any]:
    """Shape returned to the web UI."""
    return list_models()
