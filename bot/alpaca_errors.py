"""Turn raw Alpaca API errors into sentences a desk user can act on.

``alpaca-py`` raises ``APIError`` with the broker's JSON body as its string, so
an unhandled rejection reaches the browser as
``{"code":42210000,"message":"fractional orders must be simple orders"}``.
The numeric codes are stable; the wording is terse. Unwrap the message and,
for the rejections this desk can actually provoke, append what to do next.
"""

from __future__ import annotations

import json
from typing import Any

# Alpaca error code -> what the user should do about it.
_HINTS: dict[int, str] = {
    40310000: "Lower the risk % or free up cash before retrying.",
    42210000: (
        "A protective stop cannot ride along with a fractional order — "
        "size the ticket in whole shares."
    ),
    42910000: "Too many requests — wait a moment and retry.",
}


def _payload(text: str) -> dict[str, Any]:
    """Alpaca's JSON body when the error string carries one, else empty."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def humanize_alpaca_error(exc: Exception | str) -> str:
    """Readable one-liner for an Alpaca rejection; passes other errors through."""
    text = str(exc).strip()
    if not text:
        return "The broker rejected this request."
    data = _payload(text)
    message = str(data.get("message") or "").strip()
    if not message:
        return text
    message = message[:1].upper() + message[1:]
    if not message.endswith((".", "!", "?")):
        message += "."
    try:
        hint = _HINTS.get(int(data.get("code")))
    except (TypeError, ValueError):
        hint = None
    return f"{message} {hint}" if hint else message


_TRANSIENT_MARKERS = (
    "nameresolutionerror",
    "nodename nor servname",
    "failed to resolve",
    "max retries exceeded",
    "connection aborted",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "httpsconnectionpool",
    "newconnectionerror",
    "connecttimeout",
    "readtimeout",
)
_PERMANENT_MARKERS = (
    "badly formed hexadecimal uuid",
    "not a valid alpaca order id",
    "order does not exist",
    "order not found",
)
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_PERMANENT_STATUS = frozenset({400, 401, 403, 404, 422})


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    for raw in (
        getattr(response, "status_code", None),
        getattr(exc, "status_code", None),
    ):
        try:
            status = int(raw)
        except (TypeError, ValueError):
            continue
        if status > 0:
            return status
    return None


def broker_error_kind(exc: BaseException) -> str:
    """Classify a broker read as ``transient``, ``permanent``, or ``unknown``.

    A DNS blip must not kill a buy-back that still has an hour to wait.
    A garbage order id will never start working, so it should fail now.
    """
    status = _http_status(exc)
    if status in _TRANSIENT_STATUS:
        return "transient"
    if status in _PERMANENT_STATUS:
        return "permanent"
    text = str(exc).lower()
    if any(marker in text for marker in _PERMANENT_MARKERS):
        return "permanent"
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return "transient"
    return "unknown"


def describe_plan_read_error(exc: BaseException, *, what: str) -> str:
    """Queue copy for a failed sell/close/stop read — never a urllib dump."""
    kind = broker_error_kind(exc)
    if kind == "transient":
        return (
            f"Could not reach Alpaca while reading the {what} — "
            "the desk will keep watching until the wait expires."
        )
    text = str(exc).lower()
    if "uuid" in text or "not a valid alpaca order id" in text:
        return (
            f"The {what} id is not a valid Alpaca order id, so this plan "
            "cannot fire."
        )
    return f"Could not read the {what}: {humanize_alpaca_error(exc)}"
