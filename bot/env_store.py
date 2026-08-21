"""Helpers to update local .env without rewriting unrelated values."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def _env_path(path: Path | None = None) -> Path:
    return path or ENV_PATH


def _sync_process(
    target: Path,
    *,
    removed: list[str] | tuple[str, ...] | None = None,
) -> None:
    if removed:
        for key in removed:
            os.environ.pop(key, None)
    if target.exists():
        load_dotenv(target, override=True)


def _format_env_value(value: str) -> str:
    """Quote values that would break KEY=value .env parsing."""
    text = "" if value is None else str(value)
    if text == "":
        return ""
    needs_quotes = (
        any(ch in text for ch in "\n\r#\"'")
        or text.strip() != text
        or " " in text
    )
    if not needs_quotes:
        return text
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def upsert_env_values(updates: dict[str, str], path: Path | None = None) -> Path:
    """Set or replace KEY=value lines in `.env`. Creates the file if needed."""
    target = _env_path(path)
    original = {k: v for k, v in updates.items() if v is not None}
    lines: list[str] = []
    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()

    remaining = {k: v for k, v in updates.items() if v is not None}
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={_format_env_value(remaining.pop(key))}")
        else:
            out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Updated from AlgoPaca UI")
        for key, value in remaining.items():
            out.append(f"{key}={_format_env_value(value)}")

    text = "\n".join(out).rstrip() + "\n"
    target.write_text(text, encoding="utf-8")
    _sync_process(target)
    return target


def remove_env_keys(keys: list[str] | tuple[str, ...], path: Path | None = None) -> Path:
    """Remove KEY= lines from `.env` and drop them from the process environment."""
    target = _env_path(path)
    keyset = {k for k in keys if k}
    if not keyset:
        return target

    lines: list[str] = []
    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in keyset:
            continue
        out.append(line)

    text = ("\n".join(out).rstrip() + "\n") if out else ""
    if text:
        target.write_text(text, encoding="utf-8")
    elif target.exists():
        target.write_text("", encoding="utf-8")

    _sync_process(target, removed=list(keyset))
    return target


def mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}…{value[-4:]}"
