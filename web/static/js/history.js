/**
 * Trade History Page JavaScript for AlgoPaca
 * Date range filters, closed trade stats, realized P&L by symbol and strategy, Chart.js equity curve, and CSV export.
 */
/* Constants first: `const` is not initialized until its own line runs, so the
   filter state below cannot read one that is declared after it. */
const HISTORY_RANGES = [
  "day",
  "week",
  "month",
  "quarter",
  "ytd",
  "all",
  "custom",
];
const HISTORY_SOURCES = ["alpaca", "desk", "both"];
/** Display cap for the fill list. 0 = show everything the window matched. */
const HISTORY_LIMITS = [0, 100, 250, 500];
const HISTORY_LIMIT_DEFAULT = 100;
const HISTORY_FILTERS_KEY = "desk_history_filters";

let historyRange = "month";
let historySource = "alpaca";
let historyLimit = HISTORY_LIMIT_DEFAULT;
let historyStart = "";
let historyEnd = "";
const HISTORY_FILTER_QUERY_KEYS = [
  "range",
  "source",
  "limit",
  "start",
  "end",
  "symbol",
  "side",
];
let tradesBusy = false;
let lastTradesPayload = null;
/**
 * Extra row filters the AI query produced — outcome and realized-P&L bounds,
 * which the plain controls cannot express. `null` when no query is active.
 * Owned here because `filterTradeRows` is the one place rows are narrowed.
 */
let historyQueryFilter = null;
/** Monotonic request id — a newer History fetch always beats an older one. */
let tradesRequestSeq = 0;
let tradesAbort = null;
let historyEquityChart = null;
let deskHistoryLoaded = false;
let lastDeskOrderSignature = null;
/** Desk-loop context for the session list — fed by /api/status polls. */
let resultHistory = [];
let loopStartedAtMs = null;
/** Which ET fill-days are expanded; absent keys default to newest-day open. */
const historyDayOpen = new Map();

/**
 * This file's init runs while the document is still parsing, so `currentPage`
 * is still at its default — common.js only routes on DOMContentLoaded. Read the
 * page off the document instead, or the restore below never runs at all.
 */
function onHistoryPage() {
  return currentPage === "history" || document.body?.dataset?.page === "history";
}

function historyFiltersActive() {
  const symbol = String($("history-symbol")?.value || "").trim();
  const side = String($("history-side")?.value || "").trim();
  return (
    historyRange !== "month" ||
    historySource !== "alpaca" ||
    historyLimit !== HISTORY_LIMIT_DEFAULT ||
    !!symbol ||
    !!side ||
    !!historyQueryFilter ||
    (historyRange === "custom" && !!(historyStart || historyEnd))
  );
}

function applyHistoryFilterState({
  range,
  source,
  limit,
  start,
  end,
  symbol,
  side,
} = {}) {
  if (HISTORY_RANGES.includes(range)) historyRange = range;
  if (HISTORY_SOURCES.includes(source)) historySource = source;
  if (HISTORY_LIMITS.includes(limit)) {
    historyLimit = limit;
  } else if (historyRange === "all") {
    // A bare `?range=all` means all rows too; an explicit limit still wins.
    historyLimit = 0;
  }
  historyStart = normalizeDateInput(start);
  historyEnd = normalizeDateInput(end);
  const symbolEl = $("history-symbol");
  const sideEl = $("history-side");
  const sourceEl = $("history-source");
  const limitEl = $("history-limit");
  if (symbolEl) {
    symbolEl.value = String(symbol || "")
      .trim()
      .toUpperCase();
  }
  const sideNorm = String(side || "")
    .trim()
    .toLowerCase();
  if (sideEl && (sideNorm === "buy" || sideNorm === "sell" || sideNorm === "")) {
    sideEl.value = sideNorm;
  }
  if (sourceEl) sourceEl.value = historySource;
  if (limitEl) limitEl.value = String(historyLimit);
  const startEl = $("history-start");
  const endEl = $("history-end");
  if (startEl) startEl.value = historyStart;
  if (endEl) endEl.value = historyEnd;
  // A custom range with no dates would 400 on the server — fall back.
  if (historyRange === "custom" && !historyStart && !historyEnd) {
    historyRange = "month";
  }
}

function readHistoryFiltersFromUrl() {
  if (!onHistoryPage()) return false;
  const q = new URLSearchParams(window.location.search);
  if (!HISTORY_FILTER_QUERY_KEYS.some((key) => q.has(key))) return false;
  applyHistoryFilterState({
    range: String(q.get("range") || "").toLowerCase(),
    source: String(q.get("source") || "").toLowerCase(),
    limit: q.has("limit") ? Number(q.get("limit")) : NaN,
    start: q.get("start"),
    end: q.get("end"),
    symbol: q.get("symbol"),
    side: q.get("side"),
  });
  return true;
}

function readHistoryFiltersFromStorage() {
  if (!onHistoryPage()) return false;
  let raw;
  try {
    raw = localStorage.getItem(HISTORY_FILTERS_KEY);
  } catch {
    return false;
  }
  if (!raw) return false;
  let stored;
  try {
    stored = JSON.parse(raw);
  } catch {
    return false;
  }
  if (!stored || typeof stored !== "object") return false;
  applyHistoryFilterState({
    range: String(stored.range || "").toLowerCase(),
    source: String(stored.source || "").toLowerCase(),
    limit: stored.limit == null ? NaN : Number(stored.limit),
    start: stored.start,
    end: stored.end,
    symbol: stored.symbol,
    side: stored.side,
  });
  return true;
}

function persistHistoryFilters() {
  if (!onHistoryPage()) return;
  const state = {
    range: historyRange,
    source: historySource,
    limit: historyLimit,
    start: historyStart,
    end: historyEnd,
    symbol: historySymbolFilter(),
    side: historySideFilter(),
  };
  try {
    localStorage.setItem(HISTORY_FILTERS_KEY, JSON.stringify(state));
  } catch {
    /* private mode / quota — URL still carries the filters */
  }
}

function normalizeDateInput(value) {
  const raw = String(value || "").trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(raw) ? raw : "";
}

function writeHistoryFiltersToUrl() {
  if (!onHistoryPage()) return;
  const url = historyHref();
  const here = `${window.location.pathname}${window.location.search}`;
  if (here !== url) window.history.replaceState({}, "", url);
  persistHistoryFilters();
}

function loadHistoryFilters() {
  if (!onHistoryPage()) return;
  if (!readHistoryFiltersFromUrl()) {
    readHistoryFiltersFromStorage();
  }
}

/* ── History: empty / note rendering ─────────────────────────────── */

function historyEmptyMarkup(kind) {
  if (kind === "alpaca") {
    return (
      `<div class="history-empty">` +
      `<p class="history-empty-lead">${escapeHtml(tx("history_empty_alpaca", "No Alpaca fills in this range."))}</p>` +
      `<p>${escapeHtml(tx("history_empty_alpaca_hint", "Place a paper order, then refresh fills."))}</p>` +
      `<div class="history-empty-actions">` +
      `<a href="${pagePath("manual-order")}">${escapeHtml(tx("place_manual_order", "Place a manual order"))}</a>` +
      `<a href="${pagePath("auto-trade")}">${escapeHtml(tx("run_auto_trade", "Run Auto Trade"))}</a>` +
      `</div></div>`
    );
  }
  return (
    `<div class="history-empty">` +
    `<p class="history-empty-lead">${escapeHtml(tx("history_empty_desk", "No desk orders in this range."))}</p>` +
    `<p>${escapeHtml(tx("history_empty_desk_hint", "History only keeps buy/sell when an order is submitted."))}</p>` +
    `<div class="history-empty-actions">` +
    `<a href="${pagePath("auto-trade")}">${escapeHtml(tx("run_auto_trade", "Run Auto Trade"))}</a>` +
    `</div></div>`
  );
}

/** Filters matched nothing, but the range itself has rows — different advice. */
function historyNoMatchMarkup(kind) {
  const lead =
    kind === "desk"
      ? tx("history_no_match_desk", "No desk orders match these filters.")
      : tx("history_no_match", "No fills match these filters.");
  return (
    `<div class="history-empty">` +
    `<p class="history-empty-lead">${escapeHtml(lead)}</p>` +
    `<p>${escapeHtml(tx("history_no_match_hint", "Clear the symbol or side filter to see the whole range."))}</p>` +
    `<div class="history-empty-actions">` +
    `<button type="button" class="linklike" data-history-clear-filters>${escapeHtml(tx("clear_filters", "Clear filters"))}</button>` +
    `</div></div>`
  );
}

function renderHistoryListHead() {
  return (
    `<div class="history-list-head" aria-hidden="true">` +
    `<span>${escapeHtml(tx("col_time", "Time"))}</span>` +
    `<span>${escapeHtml(tx("col_side", "Side"))}</span>` +
    `<span>${escapeHtml(tx("col_symbol", "Symbol"))}</span>` +
    `<span>${escapeHtml(tx("col_price", "Price"))}</span>` +
    `</div>`
  );
}

function applyHistoryView() {
  applyTrades(lastTradesPayload || {});
  applyHistory(resultHistory, null);
  writeHistoryFiltersToUrl();
}

/* ── History: client-side filtering ──────────────────────────────── */

function historySymbolFilter() {
  return String($("history-symbol")?.value || "")
    .trim()
    .toUpperCase();
}

function historySideFilter() {
  return String($("history-side")?.value || "")
    .trim()
    .toLowerCase();
}

/**
 * Symbol / side filtering runs here, not on the server: the payload already
 * holds every fill in the range (FIFO needs the unfiltered window), so a
 * round-trip per keystroke buys nothing. Symbol matching is a prefix match.
 */
/**
 * Install the row filter an AI query produced. Symbol and side are pushed into
 * the ordinary controls so the user sees — and can undo — exactly what changed;
 * only outcome and P&L bounds, which have no control, live in the extra filter.
 */
function setHistoryQueryFilter(spec) {
  const parsed = spec && typeof spec === "object" ? spec : null;
  if (!parsed) {
    historyQueryFilter = null;
    return;
  }
  const extra = {};
  if (parsed.outcome === "win" || parsed.outcome === "loss") {
    extra.outcome = parsed.outcome;
  }
  if (Number.isFinite(Number(parsed.min_pnl))) extra.min_pnl = Number(parsed.min_pnl);
  if (Number.isFinite(Number(parsed.max_pnl))) extra.max_pnl = Number(parsed.max_pnl);
  historyQueryFilter = Object.keys(extra).length ? extra : null;
}

function clearHistoryQueryFilter() {
  historyQueryFilter = null;
  const note = $("history-query-note");
  const clearBtn = $("btn-history-query-clear");
  const input = $("history-query");
  if (input) input.value = "";
  if (clearBtn) clearBtn.hidden = true;
  if (note) {
    note.textContent = tx(
      "ai_query_help",
      "Plain language, turned into filters the page applies to fills it already loaded."
    );
    note.classList.remove("is-active", "is-error");
  }
}

function filterTradeRows(rows) {
  const symbol = historySymbolFilter();
  const side = historySideFilter();
  const query = historyQueryFilter;
  return (Array.isArray(rows) ? rows : []).filter((row) => {
    if (side && String(row.side || "").toLowerCase() !== side) return false;
    if (symbol && !String(row.symbol || "").toUpperCase().startsWith(symbol)) {
      return false;
    }
    if (!query) return true;
    // Outcome and P&L bounds only exist on fills the FIFO walk could close
    // (long sells or short covers). An opening fill has no realized number.
    const pnl = row.realized_pnl;
    if (query.outcome === "win" && !(Number(pnl) > 0)) return false;
    if (query.outcome === "loss" && !(Number(pnl) < 0)) return false;
    if (query.min_pnl != null && !(Number(pnl) >= query.min_pnl)) return false;
    if (query.max_pnl != null && !(Number(pnl) <= query.max_pnl)) return false;
    return true;
  });
}

/**
 * The server keeps `trades` capped for API compatibility, while
 * `window_trades` carries the complete broker window needed by instant
 * symbol/side filters. Apply the display cap only after those filters.
 */
function alpacaTradeView(payload = lastTradesPayload) {
  const all = Array.isArray(payload?.window_trades)
    ? payload.window_trades
    : Array.isArray(payload?.trades)
      ? payload.trades
      : [];
  const matching = filterTradeRows(all);
  const rawLimit = payload?.limit ?? historyLimit ?? HISTORY_LIMIT_DEFAULT;
  const limit = Number(rawLimit);
  const capped = Number.isFinite(limit) && limit > 0 && matching.length > limit;
  const rows = capped ? matching.slice(0, limit) : matching;
  return { all, matching, rows, limit, capped };
}

/** Realized P&L scoped to the active symbol filter, from the server's map. */
function scopedRealized(payload) {
  const symbol = historySymbolFilter();
  const bySymbol = payload?.realized_by_symbol || {};
  if (!symbol) {
    return { value: payload?.realized_range_pnl ?? payload?.realized_pnl, scope: "range" };
  }
  const exact = Object.keys(bySymbol).filter((s) => s.startsWith(symbol));
  if (!exact.length) return { value: 0, scope: "symbol", symbols: [] };
  const total = exact.reduce((n, s) => n + Number(bySymbol[s] || 0), 0);
  return { value: total, scope: "symbol", symbols: exact };
}

function csvCell(value) {
  const s = value == null ? "" : String(value);
  if (/[",\n]/.test(s)) return `"${s.replaceAll('"', '""')}"`;
  return s;
}

/** Desk order rows the export would write — error entries are not orders. */
function deskExportEntries() {
  const out = [];
  visibleDeskSessions().forEach((session) => {
    (Array.isArray(session.results) ? session.results : []).forEach((entry) => {
      if (entry?.kind === "error") return;
      out.push({ session, entry });
    });
  });
  return out;
}

/** How many rows Export CSV would produce right now, across both sources. */
function exportableRowCount() {
  const showAlpaca = historySource === "alpaca" || historySource === "both";
  const showDesk = historySource === "desk" || historySource === "both";
  const alpaca = showAlpaca ? alpacaTradeView().rows.length : 0;
  const desk = showDesk ? deskExportEntries().length : 0;
  return alpaca + desk;
}

function syncExportButton() {
  const exportBtn = $("btn-export-history");
  if (!exportBtn) return;
  exportBtn.disabled = !exportableRowCount();
  exportBtn.title = exportBtn.disabled
    ? tx("export_nothing_title", "Nothing to export for the current filters.")
    : tx("export_csv_title", "Download the rows shown as CSV.");
}

/** Export whatever is on screen — fills, desk orders, or both. */
function exportHistoryCsv() {
  const showAlpaca = historySource === "alpaca" || historySource === "both";
  const showDesk = historySource === "desk" || historySource === "both";
  const deskIndex = deskOrderIndex();
  const lines = [
    [
      "source",
      "filled_at_utc",
      "filled_at_et",
      "side",
      "symbol",
      "qty",
      "price",
      "notional",
      "fill_type",
      "mode",
      "order_id",
      "realized_pnl",
    ].join(","),
  ];
  let rows = 0;

  if (showAlpaca) {
    alpacaTradeView().rows.forEach((row) => {
      const when = row.filled_at || row.submitted_at || "";
      // `fill_type` is the ledger's own fill/partial_fill; `mode` is the desk
      // strategy that placed it, when the desk's log can claim the order id.
      const hit = deskIndex && row.id ? deskIndex.get(String(row.id)) : null;
      lines.push(
        [
          csvCell("alpaca"),
          csvCell(when),
          csvCell(formatEtDate(when, { withTime: true })),
          csvCell(row.side || ""),
          csvCell(row.symbol || ""),
          csvCell(row.qty ?? ""),
          csvCell(row.filled_avg_price ?? ""),
          csvCell(row.notional ?? ""),
          csvCell(row.fill_type || ""),
          csvCell(hit?.mode || ""),
          csvCell(row.id || ""),
          csvCell(row.realized_pnl ?? ""),
        ].join(",")
      );
      rows += 1;
    });
  }

  if (showDesk) {
    deskExportEntries().forEach(({ session, entry }) => {
      lines.push(
        [
          csvCell(`desk:${session.kind === "once" ? "once" : `loop ${session.id}`}`),
          csvCell(entry.iso || ""),
          csvCell(formatEtDate(entry.iso || entry.ts, { withTime: true })),
          csvCell(entry.signal || ""),
          csvCell(entry.symbol || ""),
          csvCell(entry.order_qty ?? ""),
          csvCell(entry.price ?? ""),
          csvCell(""),
          csvCell(""),
          csvCell(session.mode || entry.mode || ""),
          csvCell(entry.order_id || ""),
          csvCell(""),
        ].join(",")
      );
      rows += 1;
    });
  }

  if (!rows) {
    showToast(
      tx("export_empty", "Nothing to export for the current filters."),
      "error"
    );
    return;
  }

  const bits = ["algopaca", historySource, historyRange];
  const symbol = historySymbolFilter();
  const side = historySideFilter();
  if (symbol) bits.push(symbol.toLowerCase());
  if (side) bits.push(side);
  bits.push(new Date().toISOString().slice(0, 10));

  const blob = new Blob([`${lines.join("\n")}\n`], {
    type: "text/csv;charset=utf-8",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${bits.join("-")}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
  showToast(
    tx("export_done", "Exported {count} row(s).", { count: String(rows) }),
    "ok"
  );
}

function resetHistoryFilters() {
  historyRange = "month";
  historySource = "alpaca";
  historyLimit = HISTORY_LIMIT_DEFAULT;
  historyStart = "";
  historyEnd = "";
  const symbol = $("history-symbol");
  const side = $("history-side");
  const source = $("history-source");
  const limit = $("history-limit");
  const start = $("history-start");
  const end = $("history-end");
  if (symbol) symbol.value = "";
  if (side) side.value = "";
  if (source) source.value = "alpaca";
  if (limit) limit.value = "100";
  if (start) start.value = "";
  if (end) end.value = "";
  clearHistoryQueryFilter();
  try {
    localStorage.removeItem(HISTORY_FILTERS_KEY);
  } catch {
    /* ignore */
  }
  syncHistoryFiltersUi();
  writeHistoryFiltersToUrl();
  refreshTrades().catch(() => {});
}

/** Clear symbol/side only — the range and its loaded payload stay put. */
function clearHistoryRowFilters() {
  const symbol = $("history-symbol");
  const side = $("history-side");
  if (symbol) symbol.value = "";
  if (side) side.value = "";
  clearHistoryQueryFilter();
  refreshNiceSelect(side);
  syncHistoryFiltersUi();
  applyHistoryView();
}

/* ── History: range bounds ───────────────────────────────────────── */

/**
 * Start of the active range, in ms. Prefers the server's own boundary so the
 * desk-session list and the Alpaca fill list agree on where the range begins.
 */
function historyRangeStartMs() {
  const serverAfter = lastTradesPayload?.after;
  if (serverAfter) {
    const t = Date.parse(serverAfter);
    if (Number.isFinite(t)) return t;
  }
  if (historyRange === "custom") {
    const t = Date.parse(`${historyStart}T00:00:00`);
    return historyStart && Number.isFinite(t) ? t : null;
  }
  const now = Date.now();
  const day = 24 * 60 * 60 * 1000;
  if (historyRange === "day") return now - day;
  if (historyRange === "week") return now - 7 * day;
  if (historyRange === "month") return now - 31 * day;
  if (historyRange === "quarter") return now - 93 * day;
  if (historyRange === "ytd") {
    const d = new Date();
    return new Date(d.getFullYear(), 0, 1).getTime();
  }
  return null;
}

function historyRangeEndMs() {
  const serverUntil = lastTradesPayload?.until;
  if (serverUntil) {
    const t = Date.parse(serverUntil);
    if (Number.isFinite(t)) return t;
  }
  if (historyRange === "custom" && historyEnd) {
    const t = Date.parse(`${historyEnd}T23:59:59`);
    if (Number.isFinite(t)) return t;
  }
  return null;
}

function entryInHistoryRange(entry) {
  const start = historyRangeStartMs();
  const end = historyRangeEndMs();
  if (start == null && end == null) return true;
  const iso = entry?.iso || entry?.filled_at || entry?.started_at;
  if (iso) {
    const t = Date.parse(iso);
    if (Number.isFinite(t)) {
      if (start != null && t < start) return false;
      if (end != null && t >= end) return false;
      return true;
    }
  }
  return true;
}

function sessionInHistoryRange(session) {
  const start = historyRangeStartMs();
  const end = historyRangeEndMs();
  if (start == null && end == null) return true;
  if (session?.status === "running") return true;
  const iso = session?.started_at;
  if (iso) {
    const t = Date.parse(iso);
    if (Number.isFinite(t)) {
      const afterStart = start == null || t >= start;
      const beforeEnd = end == null || t < end;
      if (afterStart && beforeEnd) return true;
    }
  }
  const results = Array.isArray(session?.results) ? session.results : [];
  return results.some((r) => entryInHistoryRange(r));
}

/* ── History: fill rows ──────────────────────────────────────────── */

function renderAlpacaTrade(row, deskIndex) {
  const side = String(row.side || "").toLowerCase();
  const symbol = String(row.symbol || "");
  const px = row.filled_avg_price != null ? money(row.filled_avg_price) : "—";
  const when = escapeHtml(
    formatEtDate(row.filled_at || row.submitted_at, { withTime: true })
  );
  const pct =
    row.realized_pnl_pct != null ? ` ${formatPnlPct(row.realized_pnl_pct)}` : "";
  // Cost basis carried in from Alpaca's blended average entry rather than from
  // a fill this window saw. Real number, softer claim — say which it is.
  const approx = row.basis_estimated ? "~" : "";
  const pnl =
    row.realized_pnl != null
      ? `<span class="history-pnl ${Number(row.realized_pnl) >= 0 ? "pos" : "neg"}"${
          row.basis_estimated
            ? ` title="${escapeHtml(tx("estimated_basis_title", "This position was opened before the range, so its cost basis comes from Alpaca's average entry price."))}"`
            : ""
        }>${escapeHtml(approx + formatPnl(row.realized_pnl))}${escapeHtml(pct)}</span>`
      : "";
  // What the fill actually cost or raised — the number that reconciles against
  // a broker statement, and the one a qty-and-price row makes you work out.
  const gross =
    row.notional != null
      ? `<span class="history-notional">${escapeHtml(money(row.notional))}</span>`
      : "";
  // The ledger reports each execution separately, so one order can produce
  // several rows. Say which ones are partial, or the tape looks duplicated.
  const partial =
    String(row.fill_type || "").toLowerCase() === "partial_fill"
      ? `<span class="history-flag" title="${escapeHtml(tx("partial_fill_title", "One execution of an order that filled in several pieces."))}">${escapeHtml(
          tx("partial_fill_short", "Partial")
        )}</span>`
      : "";
  // No `role="listitem"`: the day `<details>` around this row is the list item,
  // and nesting listitems inside one another is invalid.
  return (
    `<div class="history-row" data-signal="${escapeHtml(side)}">` +
    `<div class="history-row-top">` +
    `<span class="history-time">${when}</span>` +
    `<span class="history-signal ${escapeHtml(side)}">${escapeHtml(side.toUpperCase() || "—")}</span>` +
    `<span class="history-symbol">${escapeHtml(symbol || "—")}</span>` +
    `<span class="history-price">${escapeHtml(px)}</span>` +
    `</div>` +
    `<div class="history-row-meta">` +
    (deskIndex ? renderTradeSourceBadge(row.id, deskIndex) + " · " : "") +
    `${escapeHtml(tx("qty_short", "Qty"))} ${escapeHtml(formatQty(row.qty))}` +
    (gross ? ` · ${gross}` : "") +
    (partial ? ` · ${partial}` : "") +
    (pnl ? ` · ${pnl}` : "") +
    `</div>` +
    `</div>`
  );
}

/**
 * Order IDs the desk placed, mapped to the session that placed them. Lets a
 * fill say whether the bot or a manual ticket produced it — the question this
 * page exists to answer — without splitting the timeline in two.
 *
 * A protective stop and its take-profit twin are separate Alpaca orders with
 * their own ids, so indexing entries alone left every stop-out and every target
 * fill badged "Manual". The desk records both ids on the entry it armed them
 * with; claim them here, tagged by the role they played.
 */
function deskOrderIndex() {
  // Before the first status poll lands, every fill would look "Manual".
  // Show no badge at all rather than a wrong one.
  if (!deskHistoryLoaded) return null;
  const index = new Map();
  (Array.isArray(resultHistory) ? resultHistory : []).forEach((session) => {
    (Array.isArray(session?.results) ? session.results : []).forEach((entry) => {
      const base = {
        kind: session.kind === "once" ? "once" : "loop",
        id: session.id,
        mode: String(session.mode || entry?.mode || "").toLowerCase(),
        preset: String(session.preset || "").trim(),
        presetId: String(session.preset_id || "").trim(),
      };
      if (entry?.order_id) {
        index.set(String(entry.order_id), { ...base, role: "entry" });
      }
      deskProtectiveLegs(entry).forEach(({ id, role }) => {
        index.set(id, { ...base, role });
      });
    });
  });
  return index;
}

/** The stop / take-profit order ids a desk entry armed, if any. */
function deskProtectiveLegs(entry) {
  const stop = entry?.stop_loss;
  if (!stop || typeof stop !== "object") return [];
  const legs = [];
  if (stop.id) legs.push({ id: String(stop.id), role: "stop" });
  if (stop.take_profit_order_id) {
    legs.push({ id: String(stop.take_profit_order_id), role: "target" });
  }
  return legs;
}

function renderTradeSourceBadge(orderId, index) {
  const hit = orderId ? index.get(String(orderId)) : null;
  if (!hit) {
    return `<span class="history-source-badge" data-source="manual">${escapeHtml(tx("source_badge_manual", "Manual"))}</span>`;
  }
  // A stop or target fill is a protective exit firing on its own — naming the
  // session that armed it would read as a discretionary decision it was not.
  if (hit.role === "stop") {
    return `<span class="history-source-badge" data-source="stop">${escapeHtml(tx("source_badge_stop", "Stop"))}</span>`;
  }
  if (hit.role === "target") {
    return `<span class="history-source-badge" data-source="target">${escapeHtml(tx("source_badge_target", "Target"))}</span>`;
  }
  const label =
    hit.kind === "once"
      ? tx("source_badge_once", "Desk · Once")
      : tx("source_badge_loop", "Desk · Loop {id}", { id: String(hit.id ?? "?") });
  return `<span class="history-source-badge" data-source="desk">${escapeHtml(label)}</span>`;
}

/** Group fills into ET trading days, each a collapsible accordion with P&L. */
function isHistoryDayOpen(key, isFirst) {
  if (historyDayOpen.has(key)) return historyDayOpen.get(key);
  return isFirst;
}

function renderTradeDayGroups(rows) {
  const deskIndex = deskOrderIndex();
  const groups = [];
  const index = new Map();
  rows.forEach((row) => {
    const iso = row.filled_at || row.submitted_at;
    const key = etDayKey(iso) || "—";
    if (!index.has(key)) {
      const group = { key, iso, rows: [], realized: 0, hasRealized: false };
      index.set(key, group);
      groups.push(group);
    }
    const group = index.get(key);
    group.rows.push(row);
    if (row.realized_pnl != null && Number.isFinite(Number(row.realized_pnl))) {
      group.realized += Number(row.realized_pnl);
      group.hasRealized = true;
    }
  });

  return groups
    .map((group, idx) => {
      const count = group.rows.length;
      const open = isHistoryDayOpen(group.key, idx === 0);
      const subtotal = group.hasRealized
        ? `<span class="history-day-pnl ${group.realized >= 0 ? "pos" : "neg"}">${escapeHtml(formatPnl(group.realized))}</span>`
        : "";
      const label = escapeHtml(formatEtDayLabel(group.iso));
      const countLabel = escapeHtml(
        tx(count === 1 ? "count_fill" : "count_fills", count === 1 ? "1 fill" : "{count} fills", {
          count: String(count),
        })
      );
      return (
        `<details class="history-day" data-day-key="${escapeHtml(group.key)}" role="listitem"${open ? " open" : ""}>` +
        `<summary class="history-day-head">` +
        `<span class="history-day-caret" aria-hidden="true"></span>` +
        `<span class="history-day-label">${label}</span>` +
        `<span class="history-day-meta">${countLabel}</span>` +
        subtotal +
        `</summary>` +
        `<div class="history-day-body">` +
        group.rows.map((row) => renderAlpacaTrade(row, deskIndex)).join("") +
        `</div>` +
        `</details>`
      );
    })
    .join("");
}

/* ── History: summary tiles, stats, chart ────────────────────────── */

function setTileCaveat(id, text) {
  const el = $(id);
  if (!el) return;
  el.textContent = text || "";
  el.hidden = !text;
}

function renderHistoryWarnings(payload) {
  const box = $("history-warnings");
  if (!box) return;
  const showAlpaca = historySource === "alpaca" || historySource === "both";
  const notes = [];

  if (showAlpaca && payload?.window_truncated) {
    notes.push({
      tone: "warn",
      text: tx(
        "warn_window_truncated",
        "History could not finish loading this range. Some older fills and realized P&L may be missing — refresh or narrow the range and try again."
      ),
    });
  }

  // Without the starting inventory FIFO reads a pre-range position as a brand
  // new short, and the buy that closes it books a profit that never happened.
  if (showAlpaca && payload?.opening_inventory_error) {
    notes.push({
      tone: "warn",
      text: tx(
        "warn_opening_inventory",
        "History could not read what this account held before the range ({error}), so P&L on fills that close an older position may be wrong.",
        { error: String(payload.opening_inventory_error) }
      ),
    });
  }

  const estimated = payload?.estimated_close_qty || {};
  const estimatedKeys = Object.keys(estimated);
  if (showAlpaca && estimatedKeys.length) {
    const detail = estimatedKeys
      .map((sym) => `${sym} ${formatQty(estimated[sym])}`)
      .join(" · ");
    notes.push({
      tone: "info",
      text: tx(
        "warn_estimated_basis",
        "Closed from a position opened before this range and since exited, so Alpaca has no entry price left to cost it against: {detail}",
        { detail }
      ),
    });
  }

  const open = payload?.open_lot_qty || {};
  const openKeys = Object.keys(open);
  if (showAlpaca && openKeys.length) {
    const detail = openKeys.map((sym) => `${sym} ${formatQty(open[sym])}`).join(" · ");
    notes.push({
      tone: "info",
      text: tx("warn_open_lots", "Still open from this range: {detail}", {
        detail,
      }),
    });
  }

  box.innerHTML = notes
    .map(
      (note) =>
        `<p class="history-warning" data-tone="${escapeHtml(note.tone)}">${escapeHtml(note.text)}</p>`
    )
    .join("");
  box.hidden = !notes.length;
}

function applyHistoryStats(payload) {
  const block = $("history-stats-block");
  if (!block) return;
  const stats = payload?.stats || null;
  const showAlpaca = historySource === "alpaca" || historySource === "both";
  const closed = Number(stats?.closed_trades || 0);
  if (!showAlpaca || !stats || !closed) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  const set = (id, value, tone) => {
    const el = $(id);
    if (!el) return;
    el.textContent = value;
    if (tone !== undefined) setPnlTone(el, tone);
  };
  set("stat-closed", String(closed));
  set(
    "stat-win-rate",
    stats.win_rate == null ? "—" : `${Number(stats.win_rate).toFixed(1)}%`
  );
  set("stat-avg-win", formatPnl(stats.avg_win), stats.avg_win);
  set("stat-avg-loss", formatPnl(stats.avg_loss), stats.avg_loss);
  set("stat-best", formatPnl(stats.largest_win), stats.largest_win);
  set("stat-worst", formatPnl(stats.largest_loss), stats.largest_loss);
  set(
    "stat-profit-factor",
    stats.profit_factor == null ? "—" : Number(stats.profit_factor).toFixed(2)
  );
}

function applyHistorySymbolPnl(payload) {
  const block = $("history-symbol-block");
  const box = $("history-symbol-pnl");
  if (!block || !box) return;
  const showAlpaca = historySource === "alpaca" || historySource === "both";
  const bySymbol = payload?.realized_by_symbol || {};
  const entries = Object.keys(bySymbol)
    .map((sym) => [sym, Number(bySymbol[sym] || 0)])
    .sort((a, b) => b[1] - a[1]);
  if (!showAlpaca || !entries.length) {
    block.hidden = true;
    box.innerHTML = "";
    return;
  }
  const active = historySymbolFilter();
  block.hidden = false;
  box.innerHTML = entries
    .map(([sym, value]) => {
      const on = active && sym.startsWith(active);
      return (
        `<button type="button" class="symbol-pnl-chip${on ? " is-active" : ""}" ` +
        `data-filter-symbol="${escapeHtml(sym)}" aria-pressed="${on ? "true" : "false"}">` +
        `<span class="symbol-pnl-sym">${escapeHtml(sym)}</span>` +
        `<span class="symbol-pnl-val ${value >= 0 ? "pos" : "neg"}">${escapeHtml(formatPnl(value))}</span>` +
        `</button>`
      );
    })
    .join("");
}

/** Human label for an Auto Trade strategy mode, reusing the desk's own names. */
function modeLabel(mode) {
  const key = String(mode || "").toLowerCase();
  if (!key) return tx("unattributed", "Unattributed");
  const known = { sma: "mode_sma", dip: "mode_dip", pair: "mode_pair", ls: "mode_ls", ai: "mode_ai" };
  return known[key] ? tx(known[key], key.toUpperCase()) : key.toUpperCase();
}

/**
 * Realized P&L grouped by the Auto Trade mode and preset that OPENED each
 * position. The server credits P&L to the entry order; the desk's own session
 * log says which strategy placed that order.
 */
function buildStrategyPnl(payload) {
  const byEntry = payload?.realized_by_entry_order || {};
  const index = deskOrderIndex();
  const modes = new Map();
  const presets = new Map();
  let attributed = 0;

  const bump = (map, key, label, value) => {
    const row = map.get(key) || { label, value: 0, trades: 0 };
    row.value += value;
    row.trades += 1;
    map.set(key, row);
  };

  Object.keys(byEntry).forEach((orderId) => {
    const value = Number(byEntry[orderId] || 0);
    const hit = index ? index.get(String(orderId)) : null;
    if (hit && hit.mode) {
      attributed += 1;
      bump(modes, hit.mode, modeLabel(hit.mode), value);
      const presetKey = hit.presetId || hit.preset || "—";
      bump(presets, presetKey, hit.preset || tx("no_preset", "No preset"), value);
    } else {
      bump(modes, "__manual__", tx("source_badge_manual", "Manual"), value);
    }
  });

  const sort = (map) =>
    [...map.entries()]
      .map(([key, row]) => ({ key, ...row }))
      .sort((a, b) => b.value - a.value);
  return { modes: sort(modes), presets: sort(presets), attributed };
}

function renderStrategyChips(rows) {
  return rows
    .map((row) => {
      const count = tx(
        row.trades === 1 ? "count_entry" : "count_entries",
        row.trades === 1 ? "1 entry" : "{count} entries",
        { count: String(row.trades) }
      );
      return (
        `<span class="symbol-pnl-chip is-static">` +
        `<span class="symbol-pnl-sym">${escapeHtml(row.label)}</span>` +
        `<span class="symbol-pnl-val ${row.value >= 0 ? "pos" : "neg"}">${escapeHtml(formatPnl(row.value))}</span>` +
        `<span class="symbol-pnl-count">${escapeHtml(count)}</span>` +
        `</span>`
      );
    })
    .join("");
}

function applyHistoryStrategyPnl(payload) {
  const block = $("history-strategy-block");
  const modeBox = $("history-mode-pnl");
  const presetBox = $("history-preset-pnl");
  if (!block || !modeBox || !presetBox) return;
  const showAlpaca = historySource === "alpaca" || historySource === "both";
  const { modes, presets, attributed } = buildStrategyPnl(payload);
  // Nothing to say until the desk has actually placed something in this range.
  if (!showAlpaca || !attributed) {
    block.hidden = true;
    modeBox.innerHTML = "";
    presetBox.innerHTML = "";
    return;
  }
  block.hidden = false;
  modeBox.innerHTML = renderStrategyChips(modes);
  presetBox.innerHTML = renderStrategyChips(presets);
}

function destroyHistoryEquityChart() {
  if (historyEquityChart) {
    historyEquityChart.destroy();
    historyEquityChart = null;
  }
}

function renderHistoryEquityChart(payload) {
  const block = $("history-equity-block");
  const canvas = $("history-equity-chart");
  if (!block || !canvas) return;
  const showAlpaca = historySource === "alpaca" || historySource === "both";
  const series = Array.isArray(payload?.portfolio?.series)
    ? payload.portfolio.series
    : [];
  destroyHistoryEquityChart();
  if (!showAlpaca || typeof Chart === "undefined" || series.length < 2) {
    block.hidden = true;
    return;
  }
  block.hidden = false;

  const labels = series.map((p) => formatEtDayLabel(new Date(p.t * 1000).toISOString()));
  const values = series.map((p) => Number(p.equity));
  const base = Number(payload?.portfolio?.base_value) || values[0];
  const up = values[values.length - 1] >= base;
  const copper = cssVar("--copper", "#d4894c");
  const buy = cssVar("--buy", "#3fbf8f");
  const sell = cssVar("--sell", "#e35d5d");
  const muted = cssVar("--muted", "#9aa8b8");
  const line = cssVar("--line", "#2a384c");
  const stroke = up ? buy : sell;

  historyEquityChart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: tx("account_equity_heading", "Account equity"),
          data: values,
          borderColor: stroke,
          backgroundColor: up
            ? "rgba(63, 191, 143, 0.12)"
            : "rgba(227, 93, 93, 0.12)",
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: stroke,
          tension: 0.15,
          fill: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => money(ctx.parsed.y),
          },
        },
        annotation: undefined,
      },
      scales: {
        x: {
          grid: { color: line, drawTicks: false },
          ticks: {
            color: muted,
            maxTicksLimit: 6,
            autoSkip: true,
            font: { size: 10 },
          },
        },
        y: {
          grid: { color: line, drawTicks: false },
          ticks: {
            color: muted,
            font: { size: 10 },
            callback: (value) => money(value),
          },
        },
      },
    },
  });

  // Baseline reference so the curve reads as gain or loss, not just a shape.
  if (Number.isFinite(base)) {
    const ds = historyEquityChart.data.datasets;
    ds.push({
      label: tx("starting_equity", "Start"),
      data: values.map(() => base),
      borderColor: copper,
      borderWidth: 1,
      borderDash: [4, 4],
      pointRadius: 0,
      fill: false,
    });
    historyEquityChart.update("none");
  }
}

function applyTrades(payload) {
  lastTradesPayload = payload || null;
  const box = $("alpaca-trades");
  const notesBox = $("alpaca-trades-notes");
  const deskHeading = $("desk-history-heading");
  const deskBox = $("history");
  const showAlpaca = historySource === "alpaca" || historySource === "both";
  const showDesk = historySource === "desk" || historySource === "both";

  if (box) box.hidden = !showAlpaca;
  if (notesBox) notesBox.hidden = !showAlpaca;
  const fillsConsole = $("fills-console");
  if (fillsConsole) fillsConsole.hidden = !showAlpaca;
  document
    .querySelector(".history-layout")
    ?.classList.toggle("is-single", !showAlpaca);
  if (deskHeading) deskHeading.hidden = !showDesk;
  if (deskBox) deskBox.hidden = !showDesk;

  const acctEl = $("pnl-account");
  const realEl = $("pnl-realized");
  const fillsEl = $("pnl-fills");
  const fillsLabel = $("pnl-fills-label");
  const realLabel = $("pnl-realized-label");
  const note = $("pnl-note");
  const summary = $("pnl-summary");
  const portfolio = payload?.portfolio;
  const acctPl = portfolio?.profit_loss;
  const acctPct = portfolio?.profit_loss_pct;
  const dayPl = portfolio?.day_profit_loss;
  const tradeView = alpacaTradeView(payload);
  const loaded = tradeView.all;
  const rows = tradeView.rows;
  const realized = scopedRealized(payload);
  const symbolFilter = historySymbolFilter();
  const sideFilter = historySideFilter();
  // An AI query narrows the tape exactly like symbol/side do, so it has to
  // count here too — otherwise the Fills tile drops rows with no explanation.
  const filtered = !!(symbolFilter || sideFilter || historyQueryFilter);
  const limit = tradeView.limit;
  const capped = tradeView.capped;
  const matchCount = tradeView.matching.length;
  const naLabel = tx("pnl_na", "n/a");

  if (summary) summary.classList.toggle("is-desk-only", historySource === "desk");
  if (fillsLabel) {
    fillsLabel.textContent =
      historySource === "desk"
        ? tx("desk_orders", "Desk orders")
        : tx("fills", "Fills");
  }

  /* Account P&L — account-wide by definition; filters never scope it. */
  if (acctEl) {
    if (acctPl == null) {
      acctEl.classList.remove("pos", "neg");
      acctEl.classList.add("is-na");
      acctEl.textContent = payload?.portfolio_error ? "—" : naLabel;
    } else {
      acctEl.classList.remove("is-na");
      acctEl.textContent = `${formatPnl(acctPl)}${
        acctPct != null ? ` (${(Number(acctPct) * 100).toFixed(2)}%)` : ""
      }`;
      setPnlTone(acctEl, acctPl);
    }
    setTileCaveat(
      "pnl-account-sub",
      filtered
        ? tx("caveat_account_wide", "Whole account — not filtered")
        : dayPl != null
          ? tx("caveat_today", "Today {value}", { value: formatPnl(dayPl) })
          : ""
    );
  }

  /* Realized (FIFO) — follows the symbol filter, using the server's map. */
  if (realLabel) {
    realLabel.textContent =
      realized.scope === "symbol" && symbolFilter
        ? tx("realized_fifo_symbol", "Realized · {symbol}", {
            symbol: symbolFilter,
          })
        : tx("realized_fifo", "Realized (FIFO)");
  }
  if (realEl) {
    const showRealized = showAlpaca && realized.value != null;
    realEl.classList.toggle("is-na", !showRealized);
    realEl.textContent = showRealized ? formatPnl(realized.value) : naLabel;
    setPnlTone(realEl, showRealized ? realized.value : null);
    setTileCaveat(
      "pnl-realized-sub",
      !showAlpaca
        ? ""
        : sideFilter
          ? tx(
              "caveat_side_ignored",
              "Side filter does not change realized P&L"
            )
          : realized.scope === "symbol"
            ? tx("caveat_symbol_scope", "Scoped to your symbol filter")
            : tx("caveat_matched_sells", "{count} matched close(s)", {
                count: String(Number(payload?.matched_sells || 0)),
              })
    );
  }

  /* Fills — how many are shown, out of how many were loaded. */
  if (fillsEl) {
    if (historySource === "desk") {
      fillsEl.textContent = "—";
      fillsEl.classList.remove("pos", "neg");
      setTileCaveat("pnl-fills-sub", "");
    } else {
      fillsEl.textContent = String(rows.length);
      fillsEl.classList.remove("pos", "neg", "is-na");
      setTileCaveat(
        "pnl-fills-sub",
        capped
          ? tx("caveat_capped", "capped at {limit} of {total}", {
              limit: String(limit),
              total: String(matchCount),
            })
          : filtered && rows.length !== loaded.length
            ? tx("caveat_of_loaded", "of {count} loaded", {
                count: String(loaded.length),
              })
            : ""
      );
    }
  }

  /* One stable sentence; every conditional caveat now lives on its tile. */
  if (note) {
    if (historySource === "desk") {
      note.textContent = tx(
        "pnl_note_desk",
        "Account P&L is the move in account equity for this range. Desk sessions below are local loop/once orders."
      );
    } else if (payload?.portfolio_error) {
      const err = String(payload.portfolio_error);
      note.textContent = err.includes("ALPACA_")
        ? tx(
            "pnl_note_connect",
            "Connect Alpaca paper keys on Configuration to load fills and account P&L."
          )
        : tx("pnl_note_error", "Portfolio P&L unavailable: {error}", {
            error: err,
          });
    } else {
      note.textContent = tx(
        "pnl_note",
        "Account P&L is the move in account equity across the range. Realized P&L pairs buys→sells FIFO from fills in the range."
      );
    }
  }

  renderHistoryWarnings(payload);
  applyHistoryStats(payload);
  applyHistoryStrategyPnl(payload);
  applyHistorySymbolPnl(payload);
  renderHistoryEquityChart(payload);
  syncHistorySymbolOptions(payload);

  syncExportButton();

  // The review panel lives in its own file so this one stays about fills; it
  // may legitimately be absent (script blocked, older cache) — never throw.
  if (typeof onHistoryTradesRendered === "function") {
    onHistoryTradesRendered(payload);
  }

  if (notesBox) {
    // The note used to say "Load more" with nothing to click — the only way to
    // lift the cap was the separate Load select, which reads as unrelated to
    // the Time range the user just widened. Put the action in the sentence.
    notesBox.dataset.capNote = capped
      ? `<p class="history-cap-note">${escapeHtml(
          tx("history_cap_note", "Showing the {limit} most recent of {total} fills.", {
            limit: String(limit),
            total: String(matchCount),
          })
        )} <button type="button" class="linklike" data-history-show-all>${escapeHtml(
          tx("show_all_fills", "Show all {total}", { total: String(matchCount) })
        )}</button></p>`
      : "";
  }

  renderAlpacaFillList();
}

/** Just the fill list — re-runnable on its own when only desk context changes. */
function renderAlpacaFillList() {
  const box = $("alpaca-trades");
  if (!box) return;
  const showAlpaca = historySource === "alpaca" || historySource === "both";
  if (!showAlpaca) {
    box.innerHTML = "";
    return;
  }
  const tradeView = alpacaTradeView();
  const loaded = tradeView.all;
  const rows = tradeView.rows;
  // Empty states and notes are not list items: they go in the sibling
  // container, so `role="list"` only ever contains `role="listitem"`.
  const notes = $("alpaca-trades-notes");
  if (!rows.length) {
    box.innerHTML = "";
    if (notes) {
      notes.innerHTML =
        (notes.dataset.capNote || "") +
        (loaded.length
          ? historyNoMatchMarkup("alpaca")
          : historyEmptyMarkup("alpaca"));
      notes.hidden = false;
    }
    return;
  }
  if (notes) {
    notes.innerHTML = notes.dataset.capNote || "";
    notes.hidden = !notes.innerHTML;
  }
  box.innerHTML = renderHistoryListHead() + renderTradeDayGroups(rows);
}

/** Datalist of symbols actually traded in the range — typeahead for the filter. */
function syncHistorySymbolOptions(payload) {
  const list = $("history-symbol-options");
  if (!list) return;
  const seen = new Set();
  alpacaTradeView(payload).all.forEach((row) => {
    const sym = String(row.symbol || "").toUpperCase();
    if (sym) seen.add(sym);
  });
  const options = [...seen].sort();
  const signature = options.join(",");
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.innerHTML = options
    .map((sym) => `<option value="${escapeHtml(sym)}"></option>`)
    .join("");
}

function setTradesBusy(on) {
  tradesBusy = !!on;
  const btn = $("btn-refresh-trades");
  if (btn) {
    btn.disabled = !!on;
    btn.setAttribute("aria-busy", on ? "true" : "false");
  }
  // The tape is what the fetch actually replaces — dim it with the analysis
  // column, or the one list that is about to change looks settled.
  ["history-panel", "fills-panel"].forEach((id) => {
    const panel = $(id);
    if (!panel) return;
    panel.classList.toggle("is-loading", !!on);
    panel.setAttribute("aria-busy", on ? "true" : "false");
  });
  const summary = $("pnl-summary");
  if (summary) summary.classList.toggle("is-stale", !!on);
}

/**
 * Fetch the range window. Requests are sequenced, not blocked: a newer filter
 * change aborts the in-flight request and always wins, so the chips and the
 * numbers can never disagree.
 */
async function refreshTrades() {
  const seq = ++tradesRequestSeq;
  if (tradesAbort) tradesAbort.abort();
  const controller =
    typeof AbortController !== "undefined" ? new AbortController() : null;
  tradesAbort = controller;
  setTradesBusy(true);
  try {
    const params = new URLSearchParams({
      range: historyRange || "month",
      limit: String(historyLimit ?? HISTORY_LIMIT_DEFAULT),
    });
    if (historyRange === "custom") {
      if (historyStart) params.set("start", historyStart);
      if (historyEnd) params.set("end", historyEnd);
    }
    // Symbol / side are deliberately NOT sent: the payload must stay unfiltered
    // so FIFO stays whole and filtering can happen instantly on the client.
    const data = await api(`/api/trades?${params.toString()}`, {
      signal: controller ? controller.signal : undefined,
    });
    if (seq !== tradesRequestSeq) return; // superseded by a newer request
    applyTrades(data);
    applyHistory(resultHistory, null);
    writeHistoryFiltersToUrl();
  } catch (err) {
    if (err?.name === "AbortError" || seq !== tradesRequestSeq) return;
    applyTrades({
      trades: [],
      trade_count: 0,
      realized_pnl: null,
      portfolio: null,
      portfolio_error: err.message,
    });
    const msg = String(err.message || "");
    if (
      onHistoryPage() &&
      !msg.includes("ALPACA_API_KEY") &&
      !msg.includes("ALPACA_SECRET_KEY")
    ) {
      showToast(err.message, "error");
    }
  } finally {
    if (seq === tradesRequestSeq) {
      tradesAbort = null;
      setTradesBusy(false);
    }
  }
}

function syncHistoryFiltersUi() {
  document.querySelectorAll(".range-chips .chip").forEach((btn) => {
    const on = btn.dataset.range === historyRange;
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    if (btn.dataset.range === "custom") {
      btn.setAttribute("aria-expanded", on ? "true" : "false");
    }
  });
  const custom = $("history-custom-range");
  if (custom) custom.hidden = historyRange !== "custom";
  syncDateFieldChrome();

  const source = $("history-source");
  if (source) {
    source.value = historySource;
    refreshNiceSelect(source);
  }
  const limit = $("history-limit");
  if (limit) {
    limit.value = String(historyLimit);
    refreshNiceSelect(limit);
  }
  const side = $("history-side");
  if (side) refreshNiceSelect(side);
  const reset = $("btn-reset-history-filters");
  if (reset) {
    reset.disabled = !historyFiltersActive();
    reset.title = reset.disabled
      ? tx("reset_filters_none_title", "No filters are set.")
      : tx("reset_filters_title", "Back to the default month view.");
  }
  syncClearDeskButton();
}

/**
 * Clear desk only means something when the desk list is on screen AND there is
 * something to clear — offering it against an empty log is a dead click. Kept
 * apart from `syncHistoryFiltersUi` so status polls can refresh it without
 * rebuilding every nice-select every two seconds.
 */
function syncClearDeskButton() {
  const clearBtn = $("btn-clear-history");
  if (!clearBtn) return;
  const shown = historySource !== "alpaca";
  // Anything the clear would actually remove — sessions, not just filled orders.
  const hasRows = (Array.isArray(resultHistory) ? resultHistory : []).length > 0;
  clearBtn.disabled = !shown || !hasRows;
  clearBtn.title = !shown
    ? tx(
        "clear_desk_alpaca_title",
        "Switch Source to Desk sessions to clear local desk history."
      )
    : hasRows
      ? tx("title_clear_desk", "Clear desk loop history")
      : tx("desk_history_empty_toast", "Desk history is already empty.");
}

/* ── History: desk sessions ──────────────────────────────────────── */

/** Any submitted desk order at all, before any filtering. */
function deskOrdersExist() {
  return (Array.isArray(resultHistory) ? resultHistory : []).some((session) =>
    (Array.isArray(session?.results) ? session.results : []).some(
      (entry) => entry?.kind !== "error" && !!entry?.order_id
    )
  );
}

/** Sessions after range + symbol/side filtering — shared by the list and CSV. */
function visibleDeskSessions() {
  const symbolFilter = historySymbolFilter();
  const sideFilter = historySideFilter();
  return (Array.isArray(resultHistory) ? resultHistory : [])
    .map((session) => {
      const results = (Array.isArray(session.results) ? session.results : []).filter(
        (entry) => {
          if (entry?.kind === "error") return true;
          const sig = String(entry?.signal || "").toLowerCase();
          if (sig !== "buy" && sig !== "sell") return false;
          if (sideFilter && sig !== sideFilter) return false;
          if (
            symbolFilter &&
            !String(entry?.symbol || "").toUpperCase().startsWith(symbolFilter)
          ) {
            return false;
          }
          if (!entryInHistoryRange(entry)) return false;
          return !!entry?.order_id;
        }
      );
      return { ...session, results };
    })
    .filter((session) => {
      if (!sessionInHistoryRange(session)) return false;
      if (session.status === "running" || session.kind === "loop") return true;
      return (session.results || []).length > 0;
    });
}

function applyHistory(sessions, flatFallback) {
  const box = $("history");
  const countEl = $("history-count");
  const notesBox = $("history-notes");
  if (!box) return;

  let list = Array.isArray(sessions) ? sessions : [];
  // Older snapshots without loop_history — wrap flat rows as one-shots.
  if (!list.length && Array.isArray(flatFallback) && flatFallback.length) {
    list = flatFallback.map((entry, idx) => ({
      id: entry.loop_id || entry.id || idx + 1,
      kind: entry.via === "loop" ? "loop" : "once",
      status: entry.via === "loop" ? "stopped" : "done",
      started_ts: entry.ts,
      started_at: entry.iso,
      duration_seconds: null,
      mode: entry.mode,
      symbol: entry.symbol,
      poll_seconds: null,
      poll_count: 1,
      error_count: entry.kind === "error" ? 1 : 0,
      last_signal: entry.signal,
      results: [entry],
    }));
  }

  // Keep the unfiltered list for later re-applies when only filters change.
  if (Array.isArray(sessions)) {
    resultHistory = sessions;
    deskHistoryLoaded = true;
  } else if (Array.isArray(flatFallback) && flatFallback.length) {
    resultHistory = list;
    deskHistoryLoaded = true;
  }

  // Desk attribution feeds the fill rows' source badges: when the set of desk
  // order IDs changes, the already-rendered fill list needs to catch up.
  // Protective legs feed the badges too, so a poll that only arms a stop still
  // has to move this signature — otherwise the tape keeps the "Manual" badge.
  const signature = (Array.isArray(resultHistory) ? resultHistory : [])
    .flatMap((s) => (Array.isArray(s.results) ? s.results : []))
    .flatMap((e) => [e?.order_id, ...deskProtectiveLegs(e).map((leg) => leg.id)])
    .filter(Boolean)
    .join(",");
  if (signature !== lastDeskOrderSignature) {
    lastDeskOrderSignature = signature;
    if (lastTradesPayload) {
      renderAlpacaFillList();
      applyHistoryStrategyPnl(lastTradesPayload);
    }
  }

  const visible = visibleDeskSessions();
  const tradeTotal = visible.reduce(
    (n, s) =>
      n +
      (Array.isArray(s.results)
        ? s.results.filter((r) => r?.kind !== "error").length
        : 0),
    0
  );
  const tradeView = alpacaTradeView();
  const alpacaCount = tradeView.rows.length;
  const alpacaCapped = tradeView.capped;
  const alpacaTotal = tradeView.matching.length;
  const sessionLabel = tx(
    visible.length === 1 ? "count_session" : "count_sessions",
    visible.length === 1 ? "1 session" : "{count} sessions",
    { count: String(visible.length) }
  );
  const orderLabel = tx(
    tradeTotal === 1 ? "count_order" : "count_orders",
    tradeTotal === 1 ? "1 order" : "{count} orders",
    { count: String(tradeTotal) }
  );
  const fillLabel = alpacaCapped
    ? tx("count_fills_max", "Showing {count} of {total} fills", {
        count: String(alpacaCount),
        total: String(alpacaTotal),
      })
    : tx(
        alpacaCount === 1 ? "count_fill" : "count_fills",
        alpacaCount === 1 ? "1 fill" : "{count} fills",
        { count: String(alpacaCount) }
      );
  // The tape has its own count in the sidebar; the main panel's count now
  // speaks only for what the main panel actually shows — desk sessions.
  const fillsCountEl = $("fills-count");
  if (fillsCountEl) fillsCountEl.textContent = fillLabel;
  if (countEl) {
    const showDesk = historySource === "desk" || historySource === "both";
    countEl.hidden = !showDesk;
    if (showDesk) {
      countEl.textContent =
        visible.length === 0
          ? tx("count_sessions_zero", "0 sessions")
          : `${sessionLabel} · ${orderLabel}`;
    }
  }

  const fillsEl = $("pnl-fills");
  if (fillsEl && historySource === "desk") {
    fillsEl.textContent = String(tradeTotal);
    fillsEl.classList.remove("is-na", "pos", "neg");
  }

  // Desk rows land from /api/status, after the trades payload — the header
  // buttons have to catch up or they stay disabled with rows on screen.
  syncExportButton();
  syncClearDeskButton();

  if (historySource === "alpaca") {
    box.innerHTML = "";
    if (notesBox) {
      notesBox.innerHTML = "";
      notesBox.hidden = true;
    }
    return;
  }

  if (!visible.length) {
    box.innerHTML = "";
    if (notesBox) {
      // `historyFiltersActive()` counts the Source select itself as a filter,
      // and Source is never "alpaca" here — so it can only ever say "filtered".
      // Only the row-level filters can hide desk orders that actually exist.
      const rowFiltered = !!(historySymbolFilter() || historySideFilter());
      notesBox.innerHTML =
        rowFiltered && deskOrdersExist()
          ? historyNoMatchMarkup("desk")
          : historyEmptyMarkup("desk");
      notesBox.hidden = false;
    }
    return;
  }

  if (notesBox) {
    notesBox.innerHTML = "";
    notesBox.hidden = true;
  }
  box.innerHTML = visible.map((session) => renderHistorySession(session)).join("");
}

function renderHistorySession(session) {
  const id = escapeHtml(String(session.id ?? "?"));
  const kind = session.kind === "once" ? "once" : "loop";
  const status = String(session.status || "stopped");
  const statusLabel =
    status === "running"
      ? tx("status_running", "Running")
      : kind === "once"
        ? tx("status_once", "Once")
        : tx("status_stopped", "Stopped");
  const mode = String(session.mode || "sma").toUpperCase();
  const preset = session.preset
    ? escapeHtml(String(session.preset))
    : "";
  const symbol = escapeHtml(session.symbols || session.symbol || "—");
  const provider = session.provider
    ? ` · ${escapeHtml(String(session.provider))}`
    : "";
  const polls = Number(session.poll_count || 0);
  const errors = Number(session.error_count || 0);
  const duration =
    status === "running" && loopStartedAtMs
      ? formatDuration((Date.now() - loopStartedAtMs) / 1000)
      : session.duration_seconds != null
        ? formatDuration(session.duration_seconds)
        : null;
  const durationBit = duration ? ` · ${duration}` : "";
  const started = escapeHtml(
    formatEtDate(session.started_at || session.started_ts, { withTime: true })
  );
  const results = Array.isArray(session.results) ? session.results : [];
  const rows = results.length
    ? results.map((entry) => renderHistoryResult(entry)).join("")
    : `<div class="history-session-empty">${escapeHtml(tx("history_session_empty", "No orders submitted yet — waiting for a buy/sell fill."))}</div>`;

  return (
    `<article class="history-session" data-status="${escapeHtml(status)}" data-kind="${escapeHtml(kind)}" role="listitem">` +
    `<header class="history-session-head">` +
    `<div class="history-session-title">` +
    `<span class="history-session-id">${kind === "once" ? escapeHtml(tx("status_once", "Once")) : `${escapeHtml(tx("loop_label", "Loop"))} ${id}`}</span>` +
    `<span class="history-session-status ${escapeHtml(status)}">${statusLabel}</span>` +
    (preset ? `<span class="history-session-preset">${preset}</span>` : "") +
    `<span class="history-session-time">${started}${durationBit}</span>` +
    `</div>` +
    `<div class="history-session-meta">${escapeHtml(mode)}${provider}` +
    ` · ${symbol} · ${polls} ${escapeHtml(tx(polls === 1 ? "poll_one" : "poll_many", polls === 1 ? "poll" : "polls"))}` +
    (errors ? ` · ${errors} ${escapeHtml(tx("err_abbrev", "err"))}` : "") +
    `</div>` +
    `</header>` +
    `<div class="history-session-results">${rows}</div>` +
    `</article>`
  );
}

/** "Poll 3", or an em dash when the entry carries no poll number. */
function pollLabel(poll) {
  return poll == null
    ? "—"
    : escapeHtml(tx("poll_n", "Poll {count}", { count: String(poll) }));
}

function renderHistoryResult(entry) {
  if (entry?.kind === "error") {
    const pollBit =
      entry.poll != null ? `${pollLabel(entry.poll)} · ` : "";
    return (
      `<div class="history-row is-error" data-signal="error">` +
      `<div class="history-row-top">` +
      `<span class="history-time">${escapeHtml(formatEtDate(entry.iso || entry.ts, { withTime: true }))}</span>` +
      `<span class="history-signal error">ERR</span>` +
      `<span class="history-symbol">${escapeHtml(entry.symbol || "—")}</span>` +
      `<span class="history-price">—</span>` +
      `</div>` +
      `<div class="history-row-meta">${pollBit}` +
      `${escapeHtml(tx("error_label", "Error"))}</div>` +
      `<p class="history-reason">${escapeHtml(entry.reason || tx("unknown_error", "Unknown error"))}</p>` +
      `</div>`
    );
  }
  const signal = String(entry.signal || "hold").toLowerCase();
  const price =
    entry.price != null && entry.price !== "" ? money(entry.price) : "—";
  const poll = pollLabel(entry.poll);
  const conf =
    entry.confidence != null && entry.mode === "ai"
      ? ` · ${escapeHtml(tx("confidence_short", "conf"))} ${Number(entry.confidence).toFixed(2)}`
      : "";
  const qty =
    entry.order_qty != null && entry.order_qty !== ""
      ? ` · ${escapeHtml(tx("qty_short", "Qty"))} ${escapeHtml(String(entry.order_qty))}`
      : "";
  const reason = escapeHtml(entry.reason || "");
  return (
    `<div class="history-row" data-signal="${escapeHtml(signal)}">` +
    `<div class="history-row-top">` +
    `<span class="history-time">${escapeHtml(formatEtDate(entry.iso || entry.ts, { withTime: true }))}</span>` +
    `<span class="history-signal ${escapeHtml(signal)}">${escapeHtml(signal.toUpperCase())}</span>` +
    `<span class="history-symbol">${escapeHtml(entry.symbol || "—")}</span>` +
    `<span class="history-price">${escapeHtml(price)}</span>` +
    `</div>` +
    `<div class="history-row-meta">${poll}${qty}${conf}</div>` +
    (reason ? `<p class="history-reason">${reason}</p>` : "") +
    `</div>`
  );
}

function syncDateFieldChrome() {
  const start = $("history-start");
  const end = $("history-end");
  const startShell = start?.closest(".date-field-shell");
  const endShell = end?.closest(".date-field-shell");
  if (startShell) startShell.classList.toggle("is-filled", Boolean(start.value));
  if (endShell) endShell.classList.toggle("is-filled", Boolean(end.value));
  const inverted = Boolean(start?.value && end?.value && start.value > end.value);
  startShell?.classList.toggle("is-invalid", inverted);
  endShell?.classList.toggle("is-invalid", inverted);
  start?.setAttribute("aria-invalid", inverted ? "true" : "false");
  end?.setAttribute("aria-invalid", inverted ? "true" : "false");
  const range = startShell?.closest(".date-range") || endShell?.closest(".date-range");
  range?.classList.toggle("is-complete", Boolean(start?.value && end?.value) && !inverted);
  if (start) syncDateFieldDisplay(start);
  if (end) syncDateFieldDisplay(end);
}

function bindDateFieldShells() {
  initDateFields();
  ["history-start", "history-end"].forEach((id) => {
    const el = $(id);
    if (!el || el.dataset.dateChromeBound) return;
    el.dataset.dateChromeBound = "1";
    el.addEventListener("input", syncDateFieldChrome);
    el.addEventListener("change", syncDateFieldChrome);
  });
  syncDateFieldChrome();
}

/**
 * Clear the desk's own session log. Alpaca fills are the broker's record and
 * are untouched — only the local loop/once history goes.
 */
async function onClearDeskHistory() {
  const hasDeskRows = (Array.isArray(resultHistory) ? resultHistory : []).length > 0;
  if (!hasDeskRows) {
    showToast(
      tx("desk_history_empty_toast", "Desk history is already empty."),
      "ok"
    );
    return;
  }
  const ok = window.confirm(
    tx(
      "clear_desk_confirm",
      "Clear all desk session history? This cannot be undone.\n\nAlpaca fills are unchanged."
    )
  );
  if (!ok) return;
  const btn = $("btn-clear-history");
  if (btn) {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
  }
  try {
    await api("/api/history/clear", { method: "POST", body: "{}" });
    applyHistory([], null);
    showToast(tx("desk_history_cleared", "Desk history cleared."), "ok");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    if (btn) btn.setAttribute("aria-busy", "false");
    await refreshStatus().catch(() => {});
    syncHistoryFiltersUi();
  }
}

$("btn-clear-history")?.addEventListener("click", onClearDeskHistory);
$("btn-reset-history-filters")?.addEventListener("click", resetHistoryFilters);
$("btn-export-history")?.addEventListener("click", exportHistoryCsv);

$("btn-refresh-trades")?.addEventListener("click", () => {
  refreshTrades().catch((err) => showToast(err.message, "error"));
});
document.querySelectorAll(".range-chips .chip").forEach((btn) => {
  btn.addEventListener("click", () => {
    const next = btn.dataset.range || "month";
    historyRange = next;
    if (next === "all") {
      // "All" in the Time range must not leave a hidden 100-row cap behind.
      historyLimit = 0;
    }
    syncHistoryFiltersUi();
    if (next === "custom") {
      // Wait for dates — firing now would 400 on an unbounded custom range.
      if (!historyStart && !historyEnd) {
        $("history-start")?.focus();
        return;
      }
    }
    writeHistoryFiltersToUrl();
    refreshTrades().catch(() => {});
  });
});
$("btn-apply-custom-range")?.addEventListener("click", () => {
  historyStart = normalizeDateInput($("history-start")?.value);
  historyEnd = normalizeDateInput($("history-end")?.value);
  if (!historyStart && !historyEnd) {
    showToast(
      tx("custom_range_needs_date", "Pick a start or end date first."),
      "error"
    );
    return;
  }
  if (historyStart && historyEnd && historyStart > historyEnd) {
    showToast(
      tx("custom_range_inverted", "The start date must come before the end date."),
      "error"
    );
    return;
  }
  historyRange = "custom";
  syncHistoryFiltersUi();
  writeHistoryFiltersToUrl();
  refreshTrades().catch(() => {});
});

/* Range and load-size hit the server; symbol and side filter what is already
   loaded, so they re-render instantly instead of round-tripping. */
["history-source", "history-limit"].forEach((id) => {
  $(id)?.addEventListener("change", () => {
    if (id === "history-source") {
      historySource = String($("history-source")?.value || "alpaca");
      syncHistoryFiltersUi();
      if (lastTradesPayload) {
        applyHistoryView();
        return;
      }
      refreshTrades().catch(() => {});
      return;
    }
    const next = Number($("history-limit")?.value || 100);
    historyLimit = HISTORY_LIMITS.includes(next)
      ? next
      : HISTORY_LIMIT_DEFAULT;
    syncHistoryFiltersUi();
    refreshTrades().catch(() => {});
  });
});

["history-symbol", "history-side"].forEach((id) => {
  $(id)?.addEventListener("change", () => {
    syncHistoryFiltersUi();
    applyHistoryView();
  });
});
$("history-symbol")?.addEventListener("input", () => {
  const el = $("history-symbol");
  const upper = String(el.value || "").toUpperCase();
  if (el.value !== upper) {
    const at = el.selectionStart;
    el.value = upper;
    if (at != null) el.setSelectionRange(at, at);
  }
  syncHistoryFiltersUi();
  applyHistoryView();
});

/* Symbol chips and clear-filters inside the history lists. */
document.addEventListener("click", (ev) => {
  const symBtn = ev.target.closest("[data-filter-symbol]");
  if (symBtn) {
    ev.preventDefault();
    const input = $("history-symbol");
    if (!input) return;
    const symbol = String(symBtn.dataset.filterSymbol || "").toUpperCase();
    // Clicking the active chip again clears the filter.
    input.value = input.value.trim().toUpperCase() === symbol ? "" : symbol;
    syncHistoryFiltersUi();
    applyHistoryView();
    return;
  }
  const clearBtn = ev.target.closest("[data-history-clear-filters]");
  if (clearBtn) {
    ev.preventDefault();
    clearHistoryRowFilters();
    return;
  }
  // "Show all" lifts the display cap — the window is already fetched, so this
  // is the same range with nothing trimmed off the list.
  const showAllBtn = ev.target.closest("[data-history-show-all]");
  if (showAllBtn) {
    ev.preventDefault();
    historyLimit = 0;
    const limitEl = $("history-limit");
    if (limitEl) limitEl.value = "0";
    syncHistoryFiltersUi();
    writeHistoryFiltersToUrl();
    refreshTrades().catch(() => {});
  }
});

/* toggle does not bubble — capture so re-renders keep each day's open state. */
$("alpaca-trades")?.addEventListener(
  "toggle",
  (ev) => {
    const day = ev.target;
    if (!(day instanceof HTMLDetailsElement) || !day.classList.contains("history-day")) {
      return;
    }
    const key = day.dataset.dayKey;
    if (key) historyDayOpen.set(key, day.open);
  },
  true
);

// Initialization — URL wins when present; otherwise restore the last session.
bindDateFieldShells();
loadHistoryFilters();
syncHistoryFiltersUi();
writeHistoryFiltersToUrl();
refreshTrades().catch(() => {});
refreshStatus().catch(() => {});

function onDeskStatusUpdate(state) {
  if (state.loop_running) {
    if (state.loop_started_at_ms != null && Number.isFinite(Number(state.loop_started_at_ms))) {
      loopStartedAtMs = Number(state.loop_started_at_ms);
    } else if (state.loop_started_at) {
      const parsed = Date.parse(state.loop_started_at);
      loopStartedAtMs = Number.isFinite(parsed) ? parsed : Date.now();
    } else if (loopStartedAtMs == null) {
      loopStartedAtMs = Date.now();
    }
  } else {
    loopStartedAtMs = null;
  }
  // The desk session list, the fills' source badges and the strategy P&L all
  // read from `resultHistory` — feed the poll straight into it. Routing this
  // through `applyTrades` instead would leave the desk data permanently empty.
  if (state.loop_history || state.result_history) {
    applyHistory(state.loop_history, state.result_history);
  }
}

function onDeskLanguageChange() {
  applyHistoryView();
  if (typeof onReviewLanguageChange === "function") onReviewLanguageChange();
  // Reset/Clear titles are set imperatively; `translateDOM` has just restored
  // the `data-i18n-title` text over them, so re-assert the state-aware copy.
  syncHistoryFiltersUi();
  if (tradesBusy) setTradesBusy(true);
}
