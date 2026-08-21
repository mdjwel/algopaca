"""Approved lessons drawn from History, fed back into the AI desk.

A lesson only lands here because a human read it and pressed Save. That gate is
deliberate: this is the one History feature that changes what the live engine
does, and an LLM's read of forty paper trades is not something to wire straight
into an order path. Everything else on the page is a report; this is a control.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
LESSONS_PATH = ROOT / ".desk_lessons.json"


def _path(path: Path | None = None) -> Path:
    return path or LESSONS_PATH

# Enough to shape a playbook, small enough that the trading prompt stays about
# the market. Past this the oldest lesson is dropped when a new one is saved.
MAX_LESSONS = 20

_SCOPES = frozenset({"global", "symbol", "preset"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_lesson(raw: dict[str, Any]) -> dict[str, Any]:
    text = str((raw or {}).get("text") or "").strip()[:280]
    text_en = str((raw or {}).get("text_en") or "").strip()[:280] or text
    if not text and not text_en:
        raise ValueError("lesson text is required")
    scope = str((raw or {}).get("scope") or "global").strip().lower()
    if scope not in _SCOPES:
        scope = "global"
    target = str((raw or {}).get("target") or "").strip()[:40]
    if scope == "symbol":
        target = target.upper()
    if scope != "global" and not target:
        # A lesson that names no symbol or preset is a general rule, whatever
        # the model labelled it — mislabelling it would silo it from every prompt.
        scope = "global"
    return {
        "text": text or text_en,
        "text_en": text_en,
        "scope": scope,
        "target": target if scope != "global" else "",
        "lang": str((raw or {}).get("lang") or "en").strip().lower()[:5],
        "source_range": str((raw or {}).get("source_range") or "").strip()[:20],
        "created_at": _now_iso(),
        "enabled": True,
    }


def _read(path: Path | None = None) -> list[dict[str, Any]]:
    target = _path(path)
    if not target.exists():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("lessons file unreadable — starting empty")
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("text_en") or "").strip()
        if not text:
            continue
        out.append(
            {
                "id": int(item.get("id") or index + 1),
                "text": text[:280],
                "text_en": str(item.get("text_en") or text).strip()[:280],
                "scope": str(item.get("scope") or "global").lower(),
                "target": str(item.get("target") or ""),
                "lang": str(item.get("lang") or "en"),
                "source_range": str(item.get("source_range") or ""),
                "created_at": str(item.get("created_at") or ""),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return out


def _write(rows: list[dict[str, Any]], path: Path | None = None) -> None:
    target = _path(path)
    target.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def list_lessons(path: Path | None = None) -> list[dict[str, Any]]:
    return _read(path)


def add_lesson(raw: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    rows = _read(path)
    lesson = normalize_lesson(raw)
    duplicate = lesson["text_en"].strip().lower()
    for existing in rows:
        if str(existing.get("text_en", "")).strip().lower() == duplicate:
            raise ValueError("that lesson is already saved")
    next_id = max((int(r.get("id") or 0) for r in rows), default=0) + 1
    lesson["id"] = next_id
    rows.append(lesson)
    if len(rows) > MAX_LESSONS:
        rows = rows[-MAX_LESSONS:]
    _write(rows, path)
    return lesson


def set_enabled(
    lesson_id: int, enabled: bool, path: Path | None = None
) -> dict[str, Any]:
    rows = _read(path)
    for row in rows:
        if int(row.get("id") or 0) == int(lesson_id):
            row["enabled"] = bool(enabled)
            _write(rows, path)
            return row
    raise ValueError(f"no lesson with id {lesson_id}")


def delete_lesson(lesson_id: int, path: Path | None = None) -> bool:
    rows = _read(path)
    kept = [r for r in rows if int(r.get("id") or 0) != int(lesson_id)]
    if len(kept) == len(rows):
        return False
    _write(kept, path)
    return True


def clear_lessons(path: Path | None = None) -> None:
    _write([], path)


def lessons_for_prompt(
    symbol: str | None = None,
    preset_id: str | None = None,
    path: Path | None = None,
) -> list[str]:
    """Enabled lessons that apply to this symbol/preset, English, prompt-ready.

    English rather than the desk language because this text joins the trading
    prompt, whose reasoning is English; the localized copy stays for the UI.
    """
    wanted_symbol = str(symbol or "").strip().upper()
    wanted_preset = str(preset_id or "").strip()
    out: list[str] = []
    for row in _read(path):
        if not row.get("enabled", True):
            continue
        scope = str(row.get("scope") or "global")
        target = str(row.get("target") or "")
        if scope == "symbol" and target.upper() != wanted_symbol:
            continue
        if scope == "preset" and target != wanted_preset:
            continue
        text = str(row.get("text_en") or row.get("text") or "").strip()
        if text:
            out.append(text)
    return out
