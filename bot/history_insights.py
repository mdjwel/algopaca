"""Deterministic History analytics, plus an LLM that only narrates them.

The split here is the whole design. Everything a reader could act on — win
rates, attribution, calibration, hold times, flags — is computed in Python from
the same fill ledger the page already renders. The model is handed those
finished numbers and asked for prose; it is never asked to add, divide, or
recall a figure, because a hallucinated P&L on a trading desk is worse than no
prose at all.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from bot.config import language_name, normalize_lang

logger = logging.getLogger(__name__)

# Every timestamp the desk shows is read in New York time. The hour-of-day
# breakdown has to agree with them, or "09:00" would name the server's morning
# instead of the market's — and the whole point of that chart is the session.
_ET = ZoneInfo("America/New_York")

# Confidence buckets for the calibration read. Below 0.5 the desk's own gates
# almost never let an entry through, so a finer low end would be all noise.
CONFIDENCE_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.0, 0.5),
    (0.5, 0.6),
    (0.6, 0.7),
    (0.7, 0.8),
    (0.8, 1.01),
)

# A win rate computed on a handful of trades is a coin flip with a decimal
# point. Below this the UI and the prompt both say so out loud.
MIN_MEANINGFUL_SAMPLE = 10

# Post-mortems are ranked by |P&L| and cut here: the trades that moved the
# range, not a transcript of every fill.
MAX_POSTMORTEMS = 12

# How many post-mortems reach the model. Each carries a thesis, so this is the
# dominant term in prompt size.
PROMPT_POSTMORTEMS = 8

INSIGHT_SCOPES = ("debrief", "postmortem", "audit")


# ── desk result → durable records ───────────────────────────────────

# Diagnostic fields worth keeping on a history entry. The desk used to store
# only signal/price/confidence, which is enough to list a trade and not nearly
# enough to review one.
_CONTEXT_FIELDS = (
    "regime",
    "atr_pct",
    "adx",
    "rsi",
    "trend_bias",
    "htf_bias",
    "spread_bps",
    "ta_bias",
    "news_bias",
    "r_multiple",
    "avg_entry",
    "unrealized_pct",
    "earnings_stance",
    "earnings_blackout",
    "news_count",
    "session",
    "intent",
    "order_qty",
    "risk_blocked",
    "note_lang",
)


def entry_context_from_result(item: dict[str, Any]) -> dict[str, Any]:
    """The reviewable slice of a cycle result, bounded for storage."""
    context: dict[str, Any] = {}
    for field in _CONTEXT_FIELDS:
        value = item.get(field)
        if value is None or value == "":
            continue
        context[field] = value
    risks = str(item.get("risks") or "").strip()
    if risks:
        context["risks"] = risks[:240]
    thesis_en = str(item.get("thesis_en") or "").strip()
    if thesis_en:
        context["thesis_en"] = thesis_en[:320]
    stop = item.get("stop_loss")
    if isinstance(stop, dict):
        context["stop_price"] = stop.get("stop_price")
        context["stop_pct"] = stop.get("pct")
    managed = item.get("managed")
    if isinstance(managed, dict) and managed.get("actions"):
        context["managed"] = [str(a)[:80] for a in managed["actions"][:4]]
    return context


def _skip_reasons(reason: str) -> list[str]:
    """Pull the desk's own ` | skipped: x` markers out of a reason string."""
    out: list[str] = []
    for chunk in str(reason or "").split("|"):
        text = chunk.strip()
        if text.lower().startswith("skipped:"):
            out.append(text.split(":", 1)[1].strip()[:60] or "unknown")
    return out


def desk_events_from_result(
    item: dict[str, Any], iso: str
) -> list[dict[str, Any]]:
    """Cycle outcomes that never became a fill, as flat auditable rows."""
    symbol = str(item.get("symbol") or "") or None
    mode = "ai" if item.get("provider") else str(item.get("mode") or "")
    base = {"iso": iso, "symbol": symbol, "mode": mode}
    events: list[dict[str, Any]] = []

    blocked = str(item.get("risk_blocked") or "").strip()
    if blocked:
        events.append({**base, "kind": "blocked", "reason": blocked[:120]})

    for reason in _skip_reasons(item.get("reason") or ""):
        events.append({**base, "kind": "skipped", "reason": reason})

    managed = item.get("managed")
    if isinstance(managed, dict):
        if managed.get("scale_out"):
            events.append({**base, "kind": "scale_out", "reason": "take-profit trim"})
        moved = managed.get("stop_moved")
        if isinstance(moved, dict):
            events.append(
                {
                    **base,
                    "kind": "stop_moved",
                    "reason": str(moved.get("stage") or "trail")[:40],
                }
            )
    return events


# ── time helpers ────────────────────────────────────────────────────


def _as_utc(value: Any) -> datetime | None:
    """Parse an ISO timestamp; naive values are read as server-local time.

    Desk entries are stamped with a naive ``datetime.now()`` while Alpaca fills
    carry UTC offsets. The History page already resolves that the same way — a
    naive stamp is local — so the server must not disagree with it.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def _in_range(
    value: Any, after: datetime | None, until: datetime | None
) -> bool:
    stamp = _as_utc(value)
    if stamp is None:
        # An unparseable stamp is kept: dropping it would silently shrink the
        # denominator of every rate on the page.
        return True
    if after is not None and stamp < after:
        return False
    if until is not None and stamp >= until:
        return False
    return True


def _round(value: Any, digits: int = 2) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, digits)


def _pct(part: float, whole: float) -> float | None:
    return round(part / whole * 100, 1) if whole else None


# ── index building ──────────────────────────────────────────────────


def _entry_index(
    sessions: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Map broker order id → the desk entry (and session) that placed it."""
    index: dict[str, dict[str, Any]] = {}
    for session in sessions or []:
        if not isinstance(session, dict):
            continue
        for entry in session.get("results") or []:
            order_id = str((entry or {}).get("order_id") or "")
            if not order_id:
                continue
            index[order_id] = {
                "entry": entry,
                "mode": str(session.get("mode") or entry.get("mode") or "").lower(),
                "preset": str(session.get("preset") or "").strip(),
                "preset_id": str(session.get("preset_id") or "").strip(),
                "session_id": session.get("id"),
                "kind": session.get("kind"),
            }
    return index


def _fill_index(trades: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = trades.get("window_trades") or trades.get("trades") or []
    return {str(row.get("id") or ""): row for row in rows if row.get("id")}


# ── fact builders ───────────────────────────────────────────────────


def _build_attribution(
    by_entry: dict[str, float], index: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Realized P&L credited to the mode and preset that opened the position."""
    modes: dict[str, dict[str, Any]] = {}
    presets: dict[str, dict[str, Any]] = {}

    def bump(bucket: dict[str, dict[str, Any]], key: str, label: str, pnl: float):
        row = bucket.setdefault(
            key, {"key": key, "label": label, "realized": 0.0, "entries": 0, "wins": 0}
        )
        row["realized"] += pnl
        row["entries"] += 1
        if pnl > 0:
            row["wins"] += 1

    attributed = 0
    for order_id, raw in by_entry.items():
        pnl = float(raw or 0)
        hit = index.get(str(order_id))
        if hit and hit.get("mode"):
            attributed += 1
            bump(modes, hit["mode"], hit["mode"].upper(), pnl)
            bump(
                presets,
                hit.get("preset_id") or hit.get("preset") or "none",
                hit.get("preset") or "—",
                pnl,
            )
        else:
            bump(modes, "manual", "Manual", pnl)

    def finish(bucket: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for row in bucket.values():
            rows.append(
                {
                    **row,
                    "realized": _round(row["realized"]),
                    "win_rate": _pct(row["wins"], row["entries"]),
                }
            )
        return sorted(rows, key=lambda r: r["realized"] or 0, reverse=True)

    return {
        "modes": finish(modes),
        "presets": finish(presets),
        "attributed": attributed,
        "unattributed": max(0, len(by_entry) - attributed),
    }


def _build_calibration(
    by_entry: dict[str, float], index: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Does a confident entry actually pay better than a hesitant one?

    The desk gates entries on ``ai_min_confidence`` and scales size by the same
    number. If the buckets come out flat — or inverted — that dial is costing
    money rather than earning it, and nothing else on the page would show it.
    """
    buckets = [
        {"lo": lo, "hi": hi, "entries": 0, "wins": 0, "realized": 0.0}
        for lo, hi in CONFIDENCE_BUCKETS
    ]
    sample = 0
    for order_id, raw in by_entry.items():
        hit = index.get(str(order_id))
        entry = (hit or {}).get("entry") or {}
        try:
            confidence = float(entry.get("confidence"))
        except (TypeError, ValueError):
            continue
        pnl = float(raw or 0)
        for bucket in buckets:
            if bucket["lo"] <= confidence < bucket["hi"]:
                bucket["entries"] += 1
                bucket["realized"] += pnl
                if pnl > 0:
                    bucket["wins"] += 1
                sample += 1
                break

    rows = []
    for bucket in buckets:
        entries = bucket["entries"]
        rows.append(
            {
                "label": f"{bucket['lo']:.2f}–{min(bucket['hi'], 1.0):.2f}",
                "lo": bucket["lo"],
                "hi": min(bucket["hi"], 1.0),
                "entries": entries,
                "wins": bucket["wins"],
                "win_rate": _pct(bucket["wins"], entries),
                "realized": _round(bucket["realized"]),
                "avg_pnl": _round(bucket["realized"] / entries) if entries else None,
            }
        )

    populated = [r for r in rows if r["entries"]]
    inverted = False
    if len(populated) >= 2:
        low, high = populated[0], populated[-1]
        if (
            low["win_rate"] is not None
            and high["win_rate"] is not None
            and high["win_rate"] + 5 < low["win_rate"]
        ):
            inverted = True

    return {
        "buckets": rows,
        "sample": sample,
        "sufficient": sample >= MIN_MEANINGFUL_SAMPLE,
        "inverted": inverted,
    }


def _build_execution(
    events: list[dict[str, Any]],
    index: dict[str, dict[str, Any]],
    by_entry: dict[str, float],
    after: datetime | None,
    until: datetime | None,
) -> dict[str, Any]:
    """What the desk did around the fills — blocks, skips, stop work, timing."""
    counts: dict[str, dict[str, int]] = {
        "blocked": {},
        "skipped": {},
    }
    stop_moves = 0
    scale_outs = 0
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if not _in_range(event.get("iso"), after, until):
            continue
        kind = str(event.get("kind") or "")
        reason = str(event.get("reason") or "unknown")
        if kind in counts:
            counts[kind][reason] = counts[kind].get(reason, 0) + 1
        elif kind == "stop_moved":
            stop_moves += 1
        elif kind == "scale_out":
            scale_outs += 1

    def top(bucket: dict[str, int]) -> list[dict[str, Any]]:
        rows = [{"reason": k, "count": v} for k, v in bucket.items()]
        return sorted(rows, key=lambda r: r["count"], reverse=True)[:6]

    # Entry conditions, read off the stored context of orders that actually
    # opened something and later closed.
    spreads: list[float] = []
    wide_spread_losses = 0
    wide_spread_entries = 0
    by_hour: dict[int, dict[str, Any]] = {}
    for order_id, raw in by_entry.items():
        hit = index.get(str(order_id))
        entry = (hit or {}).get("entry") or {}
        context = entry.get("context") or {}
        pnl = float(raw or 0)
        spread = _round(context.get("spread_bps"))
        if spread is not None:
            spreads.append(spread)
            if spread >= 20:
                wide_spread_entries += 1
                if pnl < 0:
                    wide_spread_losses += 1
        stamp = _as_utc(entry.get("iso"))
        if stamp is not None:
            hour = stamp.astimezone(_ET).hour
            row = by_hour.setdefault(
                hour, {"hour": hour, "entries": 0, "wins": 0, "realized": 0.0}
            )
            row["entries"] += 1
            row["realized"] += pnl
            if pnl > 0:
                row["wins"] += 1

    hours = sorted(by_hour.values(), key=lambda r: r["hour"])
    for row in hours:
        row["realized"] = _round(row["realized"])
        row["win_rate"] = _pct(row["wins"], row["entries"])

    return {
        "blocked": top(counts["blocked"]),
        "blocked_total": sum(counts["blocked"].values()),
        "skipped": top(counts["skipped"]),
        "skipped_total": sum(counts["skipped"].values()),
        "stop_moves": stop_moves,
        "scale_outs": scale_outs,
        "spread_bps": {
            "samples": len(spreads),
            "avg": _round(sum(spreads) / len(spreads)) if spreads else None,
            "max": _round(max(spreads)) if spreads else None,
            "wide_entries": wide_spread_entries,
            "wide_losses": wide_spread_losses,
        },
        "by_hour": hours,
    }


def _build_concentration(
    by_symbol: dict[str, float], stats: dict[str, Any]
) -> dict[str, Any]:
    rows = [
        {"symbol": sym, "realized": _round(value)}
        for sym, value in by_symbol.items()
    ]
    rows.sort(key=lambda r: r["realized"] or 0, reverse=True)
    gross_profit = float(stats.get("gross_profit") or 0)
    largest_win = float(stats.get("largest_win") or 0)
    return {
        "symbol_count": len(rows),
        "top": rows[:5],
        "bottom": list(reversed(rows[-5:])) if len(rows) > 5 else [],
        # One trade carrying the range is the single most common way a losing
        # system looks profitable over a short window.
        "best_trade_share_pct": _pct(largest_win, gross_profit),
    }


def _build_postmortems(
    by_entry: dict[str, float],
    index: dict[str, dict[str, Any]],
    fills: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Entry thesis beside what the position actually did."""
    rows: list[dict[str, Any]] = []
    for order_id, raw in by_entry.items():
        pnl = float(raw or 0)
        hit = index.get(str(order_id))
        entry = (hit or {}).get("entry") or {}
        fill = fills.get(str(order_id)) or {}
        context = entry.get("context") or {}
        opened = _as_utc(fill.get("filled_at") or entry.get("iso"))
        cost = 0.0
        try:
            cost = float(fill.get("qty") or 0) * float(fill.get("filled_avg_price") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        rows.append(
            {
                "order_id": str(order_id),
                "symbol": str(fill.get("symbol") or entry.get("symbol") or "") or None,
                "side": str(fill.get("side") or entry.get("signal") or "") or None,
                "realized": _round(pnl),
                "realized_pct": _round(pnl / cost * 100) if cost > 0 else None,
                "opened_at": opened.isoformat() if opened else None,
                "qty": _round(fill.get("qty"), 6),
                "entry_price": _round(fill.get("filled_avg_price"), 4),
                "mode": (hit or {}).get("mode"),
                "preset": (hit or {}).get("preset"),
                "confidence": _round(entry.get("confidence")),
                # The localized note is what the desk showed at the time; the
                # English one is what the model can reliably reason over.
                "thesis": str(entry.get("reason") or "")[:320] or None,
                "thesis_en": context.get("thesis_en"),
                "risks": context.get("risks"),
                "regime": context.get("regime"),
                "spread_bps": _round(context.get("spread_bps")),
                "atr_pct": _round(context.get("atr_pct")),
                "adx": _round(context.get("adx")),
                "rsi": _round(context.get("rsi")),
                "htf_bias": context.get("htf_bias"),
                "ta_bias": context.get("ta_bias"),
                "news_bias": context.get("news_bias"),
                "managed": context.get("managed"),
                "attributed": bool(hit),
            }
        )
    rows.sort(key=lambda r: abs(r["realized"] or 0), reverse=True)
    return rows[:MAX_POSTMORTEMS]


def _build_flags(
    totals: dict[str, Any],
    stats: dict[str, Any],
    calibration: dict[str, Any],
    execution: dict[str, Any],
    concentration: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deterministic callouts.

    Every flag carries a code and its numbers, never a sentence: the UI renders
    them through the locale files, so a Bangla desk gets Bangla warnings without
    a model round trip.
    """
    flags: list[dict[str, Any]] = []
    closed = int(stats.get("closed_trades") or 0)

    def add(code: str, severity: str, **values: Any) -> None:
        flags.append({"code": code, "severity": severity, "values": values})

    if closed and closed < MIN_MEANINGFUL_SAMPLE:
        add("small_sample", "info", count=closed, threshold=MIN_MEANINGFUL_SAMPLE)

    share = concentration.get("best_trade_share_pct")
    if closed >= 3 and share is not None and share >= 50:
        add("concentrated", "warn", pct=share)

    profit_factor = stats.get("profit_factor")
    if closed >= MIN_MEANINGFUL_SAMPLE and profit_factor is not None:
        if profit_factor < 1:
            add("losing_edge", "risk", value=profit_factor, count=closed)
        elif profit_factor < 1.2:
            add("thin_edge", "warn", value=profit_factor, count=closed)

    avg_win = stats.get("avg_win")
    avg_loss = stats.get("avg_loss")
    win_rate = stats.get("win_rate")
    if (
        closed >= MIN_MEANINGFUL_SAMPLE
        and avg_win is not None
        and avg_loss is not None
        and abs(avg_loss) > avg_win
        and (win_rate or 0) < 50
    ):
        add("payoff_upside_down", "risk", avg_win=avg_win, avg_loss=avg_loss)

    if calibration.get("inverted") and calibration.get("sufficient"):
        add("calibration_inverted", "risk", sample=calibration.get("sample"))
    elif calibration.get("sample") and not calibration.get("sufficient"):
        add("calibration_thin", "info", sample=calibration.get("sample"))

    spread = execution.get("spread_bps") or {}
    if spread.get("wide_entries"):
        add(
            "wide_spread_entries",
            "warn",
            count=spread["wide_entries"],
            losses=spread.get("wide_losses") or 0,
        )

    fills = int(totals.get("fills") or 0)
    blocked = int(execution.get("blocked_total") or 0)
    if blocked and blocked > max(fills, 1):
        add("gates_dominant", "warn", blocked=blocked, fills=fills)

    for row in execution.get("skipped") or []:
        if "closed" in str(row.get("reason", "")).lower() and row.get("count"):
            add("market_closed_skips", "info", count=row["count"])
            break

    if int(totals.get("unmatched_sells") or 0):
        add(
            "unmatched_sells",
            "info",
            count=int(totals["unmatched_sells"]),
        )

    if totals.get("window_truncated"):
        add("window_truncated", "warn")

    return flags


def build_facts(
    trades: dict[str, Any],
    *,
    sessions: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Every number the History review shows, computed without a model.

    Scoped to the range and nothing else. Calibration needs every entry to have
    a sample, attribution compares presets against each other and the execution
    audit is desk-wide by nature, so narrowing these to one ticker would not
    make them sharper — it would make them meaningless. The page's own closed-
    trade stats are range-wide for the same reason.
    """
    trades = trades or {}
    after = _as_utc(trades.get("after"))
    until = _as_utc(trades.get("until"))
    index = _entry_index(sessions)
    fills = _fill_index(trades)
    by_entry = {
        str(k): float(v or 0)
        for k, v in (trades.get("realized_by_entry_order") or {}).items()
    }
    by_symbol = {
        str(k): float(v or 0)
        for k, v in (trades.get("realized_by_symbol") or {}).items()
    }
    stats = dict(trades.get("stats") or {})
    portfolio = trades.get("portfolio") or {}

    totals = {
        "realized": _round(trades.get("realized_range_pnl")),
        "account_pl": _round(portfolio.get("profit_loss")),
        "account_pl_pct": _round(
            (portfolio.get("profit_loss_pct") or 0) * 100
            if portfolio.get("profit_loss_pct") is not None
            else None
        ),
        "fills": int(trades.get("window_trade_count") or 0),
        "matched_sells": int(trades.get("matched_sells") or 0),
        "unmatched_sells": int(trades.get("unmatched_sells") or 0),
        "window_truncated": bool(trades.get("window_truncated")),
    }

    attribution = _build_attribution(by_entry, index)
    calibration = _build_calibration(by_entry, index)
    execution = _build_execution(events or [], index, by_entry, after, until)
    concentration = _build_concentration(by_symbol, stats)
    postmortems = _build_postmortems(by_entry, index, fills)
    flags = _build_flags(totals, stats, calibration, execution, concentration)

    return {
        "range": {
            "key": trades.get("range"),
            "after": trades.get("after"),
            "until": trades.get("until"),
        },
        "totals": totals,
        "stats": stats,
        "concentration": concentration,
        "attribution": attribution,
        "calibration": calibration,
        "execution": execution,
        "postmortems": postmortems,
        "flags": flags,
    }


# ── narration ───────────────────────────────────────────────────────


NARRATE_SYSTEM_PROMPT = """You review a completed trading log for a PAPER desk.

You are a narrator, not a calculator. Every number you may use is already in
the JSON facts you are given.

Hard rules:
- Respond with ONLY valid JSON (no markdown fences).
- NEVER compute, estimate, infer, or invent a number. Quote figures verbatim
  from the facts. If a figure is not in the facts, do not mention it.
- NEVER claim a trend, edge, or pattern the facts do not already contain.
- When facts.stats.closed_trades is small, say the sample is too small to
  conclude anything, and do not issue confident advice anyway.
- Explain WHY the numbers look the way they do and what the reader should
  check next. Do not restate the numbers as a list — the page already shows
  them beside your text.
- Be specific and plain. No hype, no hedging filler, no motivational tone.
- This is a review of past paper trades, not investment advice, and never a
  prediction about future prices.
- LANGUAGE RULE: write "headline", "bullets" and each lesson "text" in the
  language named by target_language. ALWAYS write "headline_en", "bullets_en"
  and each lesson "text_en" in English, saying the same thing. When
  target_language is English, repeat the same text in both.

Lessons are optional, at most three, and must each be a concrete rule the desk
could actually apply next time (an instruction, not an observation). Omit them
entirely when the sample does not support one.

JSON schema:
{
  "headline": "one sentence naming what actually happened, in target_language",
  "headline_en": "the same sentence in English",
  "bullets": ["2-4 short paragraphs of analysis in target_language"],
  "bullets_en": ["the same paragraphs in English"],
  "lessons": [
    {
      "text": "an actionable rule in target_language",
      "text_en": "the same rule in English",
      "scope": "global" | "symbol" | "preset",
      "target": "the symbol or preset id when scope is not global, else \\"\\""
    }
  ]
}
"""

_SCOPE_BRIEF = {
    "debrief": (
        "Focus: what drove this range's result overall — realized versus account "
        "P&L, where the money came from, whether the edge is real or one trade, "
        "and which strategy or preset earned it."
    ),
    "postmortem": (
        "Focus: facts.postmortems. For the biggest winners and losers, compare "
        "the entry thesis with what the trade actually did, and name the entry "
        "condition (regime, spread_bps, adx, htf_bias) that best explains the "
        "gap. Judge the entry, not the outcome alone."
    ),
    "audit": (
        "Focus: facts.execution and facts.calibration — risk-gate blocks, skipped "
        "cycles, stop moves, scale-outs, entry spreads, hour-of-day, and whether "
        "confidence actually predicted the result. These are desk mechanics, not "
        "market calls."
    ),
}


def facts_for_prompt(facts: dict[str, Any]) -> dict[str, Any]:
    """Trim the fact sheet to what a model can usefully read in one pass."""
    trimmed = dict(facts)
    trimmed["postmortems"] = [
        {
            key: value
            for key, value in row.items()
            if key not in {"order_id", "attributed"} and value is not None
        }
        for row in (facts.get("postmortems") or [])[:PROMPT_POSTMORTEMS]
    ]
    execution = dict(facts.get("execution") or {})
    # Hour buckets are for the chart; as prompt text they are mostly zeros.
    execution["by_hour"] = [
        row for row in (execution.get("by_hour") or []) if row.get("entries")
    ]
    trimmed["execution"] = execution
    return trimmed


def narrate(
    facts: dict[str, Any],
    *,
    provider: Any,
    scope: str = "debrief",
    lang: str | None = None,
    saved_lessons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ask the model to explain the fact sheet. Numbers are never its job."""
    scope = scope if scope in INSIGHT_SCOPES else "debrief"
    code = normalize_lang(lang)
    target = language_name(code)
    payload = {
        "target_language": target,
        "scope": scope,
        "facts": facts_for_prompt(facts),
    }
    if saved_lessons:
        payload["already_saved_lessons"] = [
            str(item.get("text_en") or item.get("text") or "")[:200]
            for item in saved_lessons[:12]
        ]
    prompt = (
        f"Review this trading range and return the JSON object.\n"
        f"{_SCOPE_BRIEF[scope]}\n"
        f"CRITICAL: every number you write must appear verbatim in the facts "
        f"below. Do not do arithmetic.\n"
        f"CRITICAL LANGUAGE REQUIREMENT: 'headline', 'bullets' and lesson 'text' "
        f"in {target}; 'headline_en', 'bullets_en' and lesson 'text_en' always in "
        f"English.\n"
        + (
            "Do not repeat a lesson already listed in already_saved_lessons.\n"
            if saved_lessons
            else ""
        )
        + f"\n{json.dumps(payload, default=str, indent=2)}"
    )
    raw = provider.complete_json(prompt, system=NARRATE_SYSTEM_PROMPT)
    return normalize_narration(
        raw,
        provider=getattr(provider, "name", "?"),
        model=getattr(provider, "model", "?"),
        scope=scope,
        lang=code,
    )


def _clean_list(value: Any, *, limit: int, size: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        text = str(item or "").strip()
        if text:
            out.append(text[:size])
    return out


def normalize_narration(
    raw: dict[str, Any],
    *,
    provider: str,
    model: str,
    scope: str,
    lang: str,
) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    headline = str(raw.get("headline") or "").strip()[:240]
    headline_en = str(raw.get("headline_en") or "").strip()[:240] or headline
    bullets = _clean_list(raw.get("bullets"), limit=4, size=700)
    bullets_en = _clean_list(raw.get("bullets_en"), limit=4, size=700) or bullets

    lessons: list[dict[str, Any]] = []
    # Cap the lessons that survive validation, not the raw items: slicing first
    # lets one malformed entry silently eat a good one behind it.
    for item in (raw.get("lessons") or [])[:12]:
        if len(lessons) >= 3:
            break
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:280]
        text_en = str(item.get("text_en") or "").strip()[:280] or text
        if not text and not text_en:
            continue
        lesson_scope = str(item.get("scope") or "global").strip().lower()
        if lesson_scope not in {"global", "symbol", "preset"}:
            lesson_scope = "global"
        lessons.append(
            {
                "text": text or text_en,
                "text_en": text_en,
                "scope": lesson_scope,
                "target": str(item.get("target") or "").strip()[:40],
            }
        )

    return {
        "headline": headline,
        "headline_en": headline_en,
        "bullets": bullets,
        "bullets_en": bullets_en,
        "lessons": lessons,
        "lang": lang,
        "scope": scope,
        "provider": provider,
        "model": model,
    }


# ── natural-language filter ─────────────────────────────────────────


QUERY_SYSTEM_PROMPT = """You translate a trader's plain-language request into a
filter for a table of stock fills. You never see the trades and you never answer
the question — you only emit the filter.

Rules:
- Respond with ONLY valid JSON (no markdown fences).
- Use only the fields in the schema. Omit anything the request does not state.
- "symbol" must be one of the tickers in known_symbols, uppercase, or omitted.
- "range" is one of: day, week, month, quarter, ytd, all.
- "side" is "buy" or "sell".
- "outcome" is "win" for profitable closes, "loss" for losing ones.
- min_pnl / max_pnl are realized dollars and may be negative.
- "explain" is a short restatement of what you filtered for, in the language
  named by target_language, so the user can see whether you understood.
- If the request is not a filter over trades at all, return
  {"unsupported": true, "explain": "..."}.

JSON schema:
{
  "symbol": "AAPL",
  "side": "buy" | "sell",
  "range": "day" | "week" | "month" | "quarter" | "ytd" | "all",
  "outcome": "win" | "loss",
  "min_pnl": number,
  "max_pnl": number,
  "explain": "short restatement in target_language",
  "unsupported": false
}
"""

QUERY_RANGES = ("day", "week", "month", "quarter", "ytd", "all")


def parse_query(
    text: str,
    *,
    provider: Any,
    known_symbols: list[str] | None = None,
    lang: str | None = None,
) -> dict[str, Any]:
    """Turn "AAPL shorts that lost over $50" into a filter the page applies.

    The model never receives trade data and never executes anything: it returns
    a filter, and the browser applies it to fills it already holds. That keeps
    the call cheap, the result deterministic, and the ledger private.
    """
    question = str(text or "").strip()
    if not question:
        raise ValueError("query text is required")
    code = normalize_lang(lang)
    payload = {
        "target_language": language_name(code),
        "request": question[:400],
        "known_symbols": sorted({str(s).upper() for s in (known_symbols or [])})[:80],
    }
    raw = provider.complete_json(
        json.dumps(payload, default=str, indent=2), system=QUERY_SYSTEM_PROMPT
    )
    return normalize_query(raw, known_symbols=known_symbols, lang=code)


def normalize_query(
    raw: dict[str, Any],
    *,
    known_symbols: list[str] | None = None,
    lang: str = "en",
) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    known = {str(s).upper() for s in (known_symbols or [])}
    out: dict[str, Any] = {
        "explain": str(raw.get("explain") or "").strip()[:240],
        "lang": lang,
        "unsupported": bool(raw.get("unsupported")),
    }
    symbol = str(raw.get("symbol") or "").strip().upper()
    # A ticker the window never traded would filter the table to nothing and
    # look like a bug; drop it and let the rest of the filter stand.
    if symbol and (not known or symbol in known):
        out["symbol"] = symbol
    side = str(raw.get("side") or "").strip().lower()
    if side in {"buy", "sell"}:
        out["side"] = side
    range_key = str(raw.get("range") or "").strip().lower()
    if range_key in QUERY_RANGES:
        out["range"] = range_key
    outcome = str(raw.get("outcome") or "").strip().lower()
    if outcome in {"win", "loss"}:
        out["outcome"] = outcome
    for bound in ("min_pnl", "max_pnl"):
        value = _round(raw.get(bound), 4)
        if value is not None:
            out[bound] = value
    if out.get("min_pnl") is not None and out.get("max_pnl") is not None:
        if out["min_pnl"] > out["max_pnl"]:
            out["min_pnl"], out["max_pnl"] = out["max_pnl"], out["min_pnl"]
    filters = [k for k in ("symbol", "side", "range", "outcome", "min_pnl", "max_pnl") if k in out]
    if not filters and not out["unsupported"]:
        out["unsupported"] = True
    out["applied"] = filters
    return out
