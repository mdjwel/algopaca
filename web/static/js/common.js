/**
 * Common Shared JavaScript for AlgoPaca
 * Core utilities, state management, API client, NiceSelect, formatting, toasts, and status polling.
 */

// Initialize global theme from localStorage if set
(function initGlobalTheme() {
  try {
    const savedTheme = localStorage.getItem("algopaca_theme");
    if (savedTheme && ["obsidian", "midnight", "emerald", "daylight"].includes(savedTheme)) {
      document.documentElement.setAttribute("data-theme", savedTheme);
    }
  } catch (e) {}
})();

const $ = (id) => document.getElementById(id);

/** Nice Select 2 helpers — keep custom dropdowns in sync with native <select>. */
function niceSelectPlaceholder(el) {
  const first = el?.options?.[0];
  if (!first || first.value !== "") return null;
  return first.textContent.trim() || null;
}

function initNiceSelects(root = document) {
  if (typeof NiceSelect === "undefined") return;
  root.querySelectorAll("select").forEach((el) => {
    if (el._niceSelect) return;
    if (el.hasAttribute("data-native-select")) return;
    if (!el.classList.contains("lang-select")) {
      el.classList.add("wide");
    }
    const placeholder = niceSelectPlaceholder(el);
    NiceSelect.bind(el, {
      searchable: false,
      ...(placeholder ? { placeholder } : {}),
    });
    // `bind` only trusts a `selected` attribute in the markup and throws away a
    // value assigned from script — so a select set before binding (restored
    // History filters, desk settings) would render its default. `update` reads
    // the live selection, so one pass right after binding shows the truth.
    refreshNiceSelect(el);
    // Label[for] focuses the hidden native select — open the custom UI instead if not open.
    el.addEventListener("focus", () => {
      if (document.activeElement === el && el._niceSelect?.dropdown) {
        if (!el._niceSelect.dropdown.classList.contains("open")) {
          el._niceSelect.focus("focus_event");
        }
      }
    });
  });
}

function refreshNiceSelect(el) {
  if (!el || !el._niceSelect) return;
  const inst = el._niceSelect;
  if (inst.dropdown) {
    if (inst.dropdown.classList.contains("open")) return;
    if (
      document.activeElement &&
      (inst.dropdown.contains(document.activeElement) || document.activeElement === el)
    ) {
      return;
    }
  }
  // Options may have been re-translated since bind time.
  const placeholder = niceSelectPlaceholder(el);
  if (placeholder) inst.placeholder = placeholder;
  inst.update();
}

function refreshNiceSelects(root = document) {
  if (!root?.querySelectorAll) return;
  root.querySelectorAll("select").forEach(refreshNiceSelect);
}

function syncNiceSelectDisabled(root = document) {
  if (!root?.querySelectorAll) return;
  root.querySelectorAll("select").forEach((el) => {
    const inst = el._niceSelect;
    if (!inst) return;
    if (el.disabled) inst.disable();
    else inst.enable();
  });
}

function ensureNiceSelect(el) {
  if (!el || typeof NiceSelect === "undefined") return;
  if (el.tagName !== "SELECT") return;
  if (el.hasAttribute("data-native-select")) return;
  if (el._niceSelect) {
    refreshNiceSelect(el);
    return;
  }
  if (!el.classList.contains("lang-select")) {
    el.classList.add("wide");
  }
  NiceSelect.bind(el, { searchable: false });
  refreshNiceSelect(el);
  el.addEventListener("focus", () => {
    if (document.activeElement === el && el._niceSelect?.dropdown) {
      if (!el._niceSelect.dropdown.classList.contains("open")) {
        el._niceSelect.focus("focus_event");
      }
    }
  });
}

/** Global State */
let lastDeskSettings = null;
let lastAccount = null;
let lastAlpacaStatus = null;
let lastKeyStatus = null;
let statusGen = 0;
let busy = false;
/** Desk loop state — other pages lock their inputs while the loop runs. */
let loopRunning = false;
let currentPage = "auto-trade";

/** Page Routing & Detection */
const PAGES = [
  "auto-trade",
  "backtest",
  "backtest-history",
  "backtest-compare",
  "manual-order",
  "positions",
  "orders",
  "history",
  "configuration",
  "admin",
];

const PAGE_PATHS = {
  "auto-trade": "/auto-trade",
  backtest: "/backtest",
  "backtest-history": "/backtest/history",
  "backtest-compare": "/backtest/compare",
  "manual-order": "/manual-order",
  positions: "/positions",
  orders: "/orders",
  history: "/history",
  configuration: "/configuration",
  admin: "/admin",
};

function isBacktestFamily(page = currentPage) {
  return (
    page === "backtest" ||
    page === "backtest-history" ||
    page === "backtest-compare"
  );
}

function normalizePage(raw) {
  if (!raw) return "auto-trade";
  const p = String(raw).trim().toLowerCase();
  if (p === "auto-trade" || p === "auto" || p === "trade") return "auto-trade";
  if (p === "backtest" || p === "bt" || p === "backtesting") return "backtest";
  if (
    p === "backtest-history" ||
    p === "backtest/history" ||
    p === "bt-history"
  ) {
    return "backtest-history";
  }
  if (
    p === "backtest-compare" ||
    p === "backtest/compare" ||
    p === "bt-compare"
  ) {
    return "backtest-compare";
  }
  if (
    p === "manual-order" ||
    p === "manual" ||
    p === "order" ||
    p === "advanced-order" ||
    p === "advanced"
  )
    return "manual-order";
  if (p === "positions" || p === "position" || p === "pos" || p === "holdings")
    return "positions";
  if (p === "orders") return "orders";
  if (p === "history" || p === "trades") return "history";
  if (p === "configuration" || p === "config" || p === "settings")
    return "configuration";
  return PAGES.includes(p) ? p : "auto-trade";
}

function pagePath(page) {
  return PAGE_PATHS[normalizePage(page)] || "/auto-trade";
}

function detectCurrentPage() {
  const root = document.querySelector(".app[data-page]");
  if (root) {
    const declared = root.getAttribute("data-page");
    if (declared) return normalizePage(declared);
  }
  const path = (location.pathname || "/").replace(/^\/+/, "");
  if (path === "" || path === "index.html") return "auto-trade";
  return normalizePage(path);
}

function markCurrent(el, active) {
  el.classList.toggle("is-active", active);
  if (active) el.setAttribute("aria-current", "page");
  else el.removeAttribute("aria-current");
}

function initRouting() {
  currentPage = detectCurrentPage();
  // Every nav destination now resolves to exactly one page. The old
  // "Backtest stays lit across its sub-pages" rule moved up to the group
  // trigger, which owns the whole family.
  document.querySelectorAll("a[data-page]").forEach((a) => {
    markCurrent(a, normalizePage(a.getAttribute("data-page")) === currentPage);
  });
  document.querySelectorAll(".desk-nav-trigger[data-group-pages]").forEach((btn) => {
    const pages = btn.getAttribute("data-group-pages").split(/\s+/).filter(Boolean);
    // `aria-current` belongs on the link that is the page, not on a button
    // that merely contains it, so only the styling hook is toggled here.
    btn.classList.toggle("is-active", pages.includes(currentPage));
  });
  const subByPage = { backtest: "run", "backtest-history": "history", "backtest-compare": "compare" };
  document.querySelectorAll(".bt-subnav a[data-bt-sub]").forEach((a) => {
    markCurrent(a, a.getAttribute("data-bt-sub") === subByPage[currentPage]);
  });
}

/**
 * Desk nav dropdowns — the ARIA disclosure-navigation pattern: a button that
 * toggles a plain list of links. Pointer devices also get hover-to-open;
 * touch and keyboard drive it through click and the arrow keys.
 */
let deskNavBound = false;

function initDeskNav() {
  // Both startup paths below can fire in the same load, and unlike
  // `initRouting` this one binds listeners — running it twice would double
  // every handler.
  if (deskNavBound) return;
  deskNavBound = true;

  const groups = Array.from(document.querySelectorAll(".desk-nav-group"))
    .map((root) => ({
      root,
      trigger: root.querySelector(".desk-nav-trigger"),
      menu: root.querySelector(".desk-menu"),
    }))
    .filter((g) => g.trigger && g.menu);
  if (!groups.length) return;

  const canHover = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  let closeTimer = null;

  const items = (g) => Array.from(g.menu.querySelectorAll(".desk-menu-link"));

  function close(g) {
    g.menu.hidden = true;
    g.menu.classList.remove("is-flipped");
    g.menu.style.left = "";
    g.menu.style.right = "";
    g.trigger.setAttribute("aria-expanded", "false");
  }

  /**
   * Keep the panel inside the viewport: right-align it under its trigger if
   * it would run off the right edge, then nudge it back if it is still
   * clipped — which happens when the panel is wider than the space beside
   * the trigger, e.g. long labels on a narrow screen. The nudge is done with
   * `left`/`right` rather than a transform so it does not fight the opening
   * animation, which owns `transform`.
   */
  function place(g) {
    const pad = 8;
    const vw = document.documentElement.clientWidth;
    g.menu.classList.remove("is-flipped");
    g.menu.style.left = "";
    g.menu.style.right = "";

    let box = g.menu.getBoundingClientRect();
    if (box.right > vw - pad) {
      g.menu.classList.add("is-flipped");
      box = g.menu.getBoundingClientRect();
    }

    const flipped = g.menu.classList.contains("is-flipped");
    let shift = 0;
    if (box.left < pad) shift = pad - box.left;
    else if (box.right > vw - pad) shift = vw - pad - box.right;
    if (!shift) return;
    // Both anchors move the panel rightward for a positive shift.
    if (flipped) g.menu.style.right = `${Math.round(-shift)}px`;
    else g.menu.style.left = `${Math.round(shift)}px`;
  }

  function closeAll(except) {
    groups.forEach((g) => {
      if (g !== except) close(g);
    });
  }

  function open(g) {
    clearTimeout(closeTimer);
    closeAll(g);
    g.menu.hidden = false;
    g.trigger.setAttribute("aria-expanded", "true");
    place(g);
  }

  function isOpen(g) {
    return !g.menu.hidden;
  }

  function focusItem(g, index) {
    const list = items(g);
    if (!list.length) return;
    const i = (index + list.length) % list.length;
    list[i].focus();
  }

  groups.forEach((g) => {
    g.trigger.addEventListener("click", (e) => {
      e.preventDefault();
      if (isOpen(g)) close(g);
      else open(g);
    });

    g.trigger.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        open(g);
        focusItem(g, 0);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        open(g);
        focusItem(g, -1);
      } else if (e.key === "Escape" && isOpen(g)) {
        close(g);
      }
    });

    g.menu.addEventListener("keydown", (e) => {
      const list = items(g);
      const at = list.indexOf(document.activeElement);
      if (e.key === "ArrowDown") {
        e.preventDefault();
        focusItem(g, at + 1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        focusItem(g, at - 1);
      } else if (e.key === "Escape") {
        e.preventDefault();
        close(g);
        g.trigger.focus();
      } else if (e.key === "Tab") {
        close(g);
      }
    });

    if (canHover) {
      g.root.addEventListener("mouseenter", () => open(g));
      g.root.addEventListener("mouseleave", () => {
        clearTimeout(closeTimer);
        closeTimer = setTimeout(() => close(g), 140);
      });
    }
  });

  // Anything that moves focus or intent away from the nav closes it.
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".desk-nav-group")) closeAll();
  });
  document.addEventListener("focusin", (e) => {
    if (!e.target.closest(".desk-nav-group")) closeAll();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeAll();
  });
}

/** Localization helper */
function tx(key, fallback, params) {
  return typeof window.t === "function"
    ? window.t(key, fallback, params || {})
    : fallback;
}

/** Formatting & Utilities */
function money(n) {
  return `$${Number(n).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/**
 * Wall-clock parts for an instant in US market time.
 * Fills come back from Alpaca in UTC; a US-equities desk reads them in ET,
 * so every timestamp on the desk is converted once, here.
 */
let etPartsFormatter = null;
function etParts(ms) {
  if (!etPartsFormatter) {
    try {
      etPartsFormatter = new Intl.DateTimeFormat("en-US", {
        timeZone: "America/New_York",
        year: "numeric",
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZoneName: "short",
      });
    } catch (err) {
      etPartsFormatter = null;
      return null;
    }
  }
  try {
    const out = {};
    for (const part of etPartsFormatter.formatToParts(new Date(ms))) {
      out[part.type] = part.value;
    }
    return {
      year: Number(out.year),
      month: Number(out.month) - 1,
      day: Number(out.day),
      // Intl can emit "24" for midnight under hour12:false.
      hour: out.hour === "24" ? "00" : out.hour,
      minute: out.minute,
      zone: out.timeZoneName || "ET",
    };
  } catch (err) {
    return null;
  }
}

function parseBtTime(iso) {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : NaN;
}

const MONTHS_LONG = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const MONTHS_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/** e.g. 12 June 2026 — optional · HH:MM for timed bars */
function formatDisplayDate(iso, { withTime = false } = {}) {
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) {
    const raw = String(iso || "").trim();
    return raw ? raw.replace("T", " ").slice(0, 19) : "—";
  }
  const d = new Date(t);
  const base = `${d.getUTCDate()} ${MONTHS_LONG[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  if (!withTime) return base;
  if (!/T\d{2}:\d{2}/.test(String(iso || ""))) return base;
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${base} · ${hh}:${mm}`;
}

/** e.g. 13 August 2026 · 15:22 EDT — market time, always labelled. */
function formatEtDate(iso, { withTime = false } = {}) {
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return formatDisplayDate(iso, { withTime });
  const p = etParts(t);
  if (!p) return formatDisplayDate(iso, { withTime });
  const base = `${p.day} ${MONTHS_LONG[p.month]} ${p.year}`;
  if (!withTime) return base;
  if (!/T\d{2}:\d{2}/.test(String(iso || ""))) return base;
  return `${base} · ${p.hour}:${p.minute} ${p.zone}`;
}

/** Sortable ET calendar-day key, used to group fills into trading days. */
function etDayKey(iso) {
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return "";
  const p = etParts(t);
  if (!p) return String(iso || "").slice(0, 10);
  return `${p.year}-${String(p.month + 1).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}

function formatEtDayLabel(iso) {
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return "—";
  const p = etParts(t);
  if (!p) return formatDisplayDate(iso);
  return `${p.day} ${MONTHS_LONG[p.month]} ${p.year}`;
}

function formatAge(seconds) {
  if (seconds < 90) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatPnl(n) {
  if (n == null || !Number.isFinite(Number(n))) return "—";
  const v = Number(n);
  // Sign belongs outside the currency symbol: -$62.00, never $-62.00.
  if (v < 0) return `-${money(Math.abs(v))}`;
  return `${v > 0 ? "+" : ""}${money(v)}`;
}

function formatQty(n) {
  if (n == null || n === "") return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

function formatPnlPct(n) {
  if (n == null || !Number.isFinite(Number(n))) return "";
  const v = Number(n);
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function setPnlTone(el, n) {
  if (!el) return;
  el.classList.remove("pos", "neg");
  if (n == null || !Number.isFinite(Number(n))) return;
  if (Number(n) > 0) el.classList.add("pos");
  if (Number(n) < 0) el.classList.add("neg");
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/**
 * Build avatar initials from a display name.
 * Multi-word names use the first letter of the first two words ("Eh Jewel" -> "EJ");
 * single words fall back to their first two letters ("algotrader" -> "AL").
 */
function computeInitials(name, fallback = "T") {
  const words = String(name || "")
    .trim()
    .split(/[\s._-]+/)
    .filter(Boolean);
  if (words.length === 0) return fallback.slice(0, 2).toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function showToast(message, kind = "error", extraHtml = "") {
  const el = $("toast");
  if (!el) return;
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    el.classList.remove("is-fresh");
    return;
  }
  el.hidden = false;
  el.dataset.kind = kind;
  const text = String(message);
  // Missing Alpaca credentials — link to the Configuration page.
  if (/ALPACA_(API|SECRET)_KEY/.test(text)) {
    el.innerHTML =
      escapeHtml(text).replaceAll("\n", "<br>") +
      ` <a href="${pagePath("configuration")}">Open Configuration</a>`;
  } else if (extraHtml) {
    el.innerHTML = escapeHtml(text).replaceAll("\n", "<br>") + extraHtml;
  } else {
    el.textContent = text;
  }
  el.classList.remove("is-fresh");
  void el.offsetWidth;
  el.classList.add("is-fresh");
}

/** Centralized API Client */
async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 401) {
    if (
      !window.location.pathname.startsWith("/login") &&
      !window.location.pathname.startsWith("/signup")
    ) {
      const next = encodeURIComponent(
        window.location.pathname + window.location.search
      );
      window.location.href = `/login?next=${next}`;
      return new Promise(() => {}); // Halt execution while navigating
    }
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
      : detail || res.statusText || "Request failed";
    throw new Error(message);
  }
  return data;
}


/** Account & Masthead State */
function applyAccount(account) {
  lastAccount = account || null;
  const statusEl = $("acct-status");
  if (!statusEl) {
    syncConfigConnectionSafe();
    return;
  }
  if (!account) {
    statusEl.textContent = "Not connected";
    $("acct-equity").textContent = "—";
    $("acct-cash").textContent = "—";
    $("acct-bp").textContent = "—";
    syncConfigConnectionSafe();
    return;
  }
  statusEl.textContent = `${account.status} · ${
    account.paper === false || account.trading_mode === "live" ? "live" : "paper"
  }`;
  $("acct-equity").textContent = money(account.equity);
  $("acct-cash").textContent = money(account.cash);
  $("acct-bp").textContent = money(account.buying_power);
  // The account payload knows the environment. Keep the banner in sync with
  // whatever the last status poll reported for live_authorized.
  applyTradingEnv({
    mode: account.trading_mode || (account.paper === false ? "live" : "paper"),
    paper: account.paper !== false,
    live_authorized: !!(lastAlpacaStatus || {}).live_authorized,
  });
  syncConfigConnectionSafe();
}

/** Shared busy gate — page scripts opt in by defining the sync hooks. */
function setBusy(isBusy, label) {
  busy = isBusy;
  if (typeof syncManualBusyHint === "function") {
    if (!isBusy) manualBusyLabel = null;
    else if (label) manualBusyLabel = label;
  }
  if (typeof applyLoop === "function") applyLoop(loopRunning);
  if (typeof syncConfigBusyUi === "function") syncConfigBusyUi();
  if (isBusy && label) {
    const loopEl = $("loop-state");
    if (loopEl) loopEl.textContent = label;
  }
  if (typeof syncManualBusyHint === "function") syncManualBusyHint();
}

async function refreshStatus({ forceSettings = false } = {}) {
  const gen = ++statusGen;
  const state = await api("/api/status");
  if (gen !== statusGen) return state;

  if (state.account) {
    applyAccount(state.account);
  }
  lastDeskSettings = state.settings || null;
  lastAlpacaStatus = state.alpaca_key_status || null;
  lastKeyStatus = state.ai_key_status || null;
  loopRunning = !!state.loop_running;
  syncDeskLanguage(state.settings?.lang);
  applyTradingEnv(state.trading_mode || state.alpaca_key_status);

  // Let active page handler consume state updates
  if (typeof onDeskStatusUpdate === "function") {
    onDeskStatusUpdate(state, { forceSettings });
  }

  return state;
}

let langSyncInFlight = false;

/** Push the picked UI language onto the desk when the two have drifted.
 *
 *  The AI writes its thesis in the desk language, and only the dropdown's
 *  change event used to save it — so a desk left on `bn` by an earlier session
 *  or by LANG_CODE kept producing Bangla notes for a UI showing English. */
function syncDeskLanguage(deskLang) {
  if (typeof i18n === "undefined" || langSyncInFlight) return;
  const uiLang = i18n.getCurrentLanguage();
  if (!deskLang || !uiLang || deskLang === uiLang) return;
  langSyncInFlight = true;
  fetch("/api/lang", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lang: uiLang }),
  })
    .catch(() => {})
    .finally(() => {
      langSyncInFlight = false;
    });
}

/** Link into /history carrying the current (or given) filters. The filter
 *  state lives on the History page, so fall back to defaults elsewhere. */
function historyHref(extra = {}) {
  const params = new URLSearchParams();
  const range = extra.range || (typeof historyRange !== "undefined" ? historyRange : "") || "month";
  const source = extra.source || (typeof historySource !== "undefined" ? historySource : "") || "alpaca";
  // 0 is a real choice here ("All"), so `||` chaining would swallow it.
  const limitRaw =
    extra.limit != null
      ? Number(extra.limit)
      : typeof historyLimit !== "undefined"
        ? Number(historyLimit)
        : 100;
  const limit = Number.isFinite(limitRaw) ? limitRaw : 100;
  const symbol =
    extra.symbol != null
      ? String(extra.symbol).trim().toUpperCase()
      : String($("history-symbol")?.value || "")
          .trim()
          .toUpperCase();
  const side =
    extra.side != null
      ? String(extra.side).trim().toLowerCase()
      : String($("history-side")?.value || "")
          .trim()
          .toLowerCase();
  if (range && range !== "month") params.set("range", range);
  if (source && source !== "alpaca") params.set("source", source);
  if (symbol) params.set("symbol", symbol);
  if (side === "buy" || side === "sell") params.set("side", side);
  if (limit !== 100) params.set("limit", String(limit));
  if (range === "custom") {
    const start = extra.start != null ? extra.start : (typeof historyStart !== "undefined" ? historyStart : "");
    const end = extra.end != null ? extra.end : (typeof historyEnd !== "undefined" ? historyEnd : "");
    if (start) params.set("start", start);
    if (end) params.set("end", end);
  }
  const qs = params.toString();
  return qs ? `${pagePath("history")}?${qs}` : pagePath("history");
}

const SESSION_LABELS = {
  regular: ["session_regular", "Regular hours"],
  premarket: ["session_premarket", "Pre-market"],
  afterhours: ["session_afterhours", "After hours"],
  overnight: ["session_overnight", "Overnight"],
  closed: ["session_closed", "Closed"],
};

function formatSession(session) {
  const label = SESSION_LABELS[session];
  return label ? tx(label[0], label[1]) : session || "—";
}

function syncConfigConnectionSafe() {
  if (typeof syncConfigConnection === "function") syncConfigConnection();
}

function applyAiKeys(aiReady, keyStatus) {
  lastKeyStatus = keyStatus || lastKeyStatus;
  const el = $("ai-keys");
  const status = keyStatus || {};
  const o = status.openai || { set: !!aiReady?.openai, source: "none", hint: "" };
  const g = status.gemini || { set: !!aiReady?.gemini, source: "none", hint: "" };
  const fmt = (entry, name) => {
    if (!entry.set) return `${name} missing`;
    const src = entry.source === "ui" ? "UI" : entry.source === "env" ? ".env" : "";
    return `${name} saved${src ? ` (${src})` : ""}`;
  };
  if (el) {
    const missing = !o.set || !g.set;
    el.innerHTML =
      `AI keys: ${escapeHtml(fmt(o, "OpenAI"))} · ${escapeHtml(fmt(g, "Gemini"))}` +
      (missing
        ? ` — <a href="${pagePath("configuration")}">Configuration</a>`
        : "");
  }

  const oHint = $("openai-key-hint");
  const gHint = $("gemini-key-hint");
  if (oHint) {
    oHint.textContent = o.set
      ? `Saved (${o.source}): ${o.hint || "••••"} — leave blank to keep`
      : "Not set — paste an OpenAI key, then Save AI keys";
  }
  if (gHint) {
    gHint.textContent = g.set
      ? `Saved (${g.source}): ${g.hint || "••••"} — leave blank to keep`
      : "Not set — paste a Gemini key, then Save AI keys";
  }

  const openaiField = $("field-openai-key");
  const geminiField = $("field-gemini-key");
  if (openaiField && !openaiField.value) {
    openaiField.placeholder = o.set ? `saved ${o.hint || "••••"}` : "sk-… paste here";
  }
  if (geminiField && !geminiField.value) {
    geminiField.placeholder = g.set ? `saved ${g.hint || "••••"}` : "AIza… paste here";
  }
  syncConfigConnectionSafe();
}

function applyAlpacaKeys(status) {
  lastAlpacaStatus = status || lastAlpacaStatus;
  const s = lastAlpacaStatus || {};
  const mode = s.trading_mode || (s.paper === false ? "live" : "paper");
  const paperKeys = s.paper_keys || s;
  const liveKeys = s.live_keys || {};
  const line = $("alpaca-keys-line");
  if (line) {
    if (s.account_error) {
      line.textContent = `Alpaca: failed — ${s.account_error}`;
    } else if (s.set) {
      line.textContent = `Alpaca: ${mode} · saved (${s.api_key_hint || "••••"})`;
    } else {
      line.innerHTML = `Alpaca: missing — <a href="${pagePath("configuration")}">set keys on Configuration</a>`;
    }
  }
  const keyHint = $("alpaca-key-hint");
  const secretHint = $("alpaca-secret-hint");
  if (keyHint) {
    keyHint.textContent = paperKeys.api_key_set
      ? `Saved: ${paperKeys.api_key_hint || "••••"} — leave blank to keep`
      : "Not set — paste your paper API key";
  }
  if (secretHint) {
    secretHint.textContent = paperKeys.secret_set
      ? `Saved: ${paperKeys.secret_hint || "••••"} — leave blank to keep`
      : "Not set — paste your paper secret key";
  }
  const liveKeyHint = $("live-key-hint");
  const liveSecretHint = $("live-secret-hint");
  if (liveKeyHint) {
    liveKeyHint.textContent = liveKeys.api_key_set
      ? `Saved: ${liveKeys.api_key_hint || "••••"} — leave blank to keep`
      : "Not set — paste your live API key";
  }
  if (liveSecretHint) {
    liveSecretHint.textContent = liveKeys.secret_set
      ? `Saved: ${liveKeys.secret_hint || "••••"} — leave blank to keep`
      : "Not set — paste your live secret key";
  }
  const keyField = $("field-alpaca-key");
  const secretField = $("field-alpaca-secret");
  if (keyField && !keyField.value) {
    keyField.placeholder = paperKeys.api_key_set
      ? `saved ${paperKeys.api_key_hint || "••••"}`
      : "PK… paste here";
  }
  if (secretField && !secretField.value) {
    secretField.placeholder = paperKeys.secret_set
      ? `saved ${paperKeys.secret_hint || "••••"}`
      : "Secret… paste here";
  }
  const liveKeyField = $("field-live-key");
  const liveSecretField = $("field-live-secret");
  if (liveKeyField && !liveKeyField.value) {
    liveKeyField.placeholder = liveKeys.api_key_set
      ? `saved ${liveKeys.api_key_hint || "••••"}`
      : "AK… paste here";
  }
  if (liveSecretField && !liveSecretField.value) {
    liveSecretField.placeholder = liveKeys.secret_set
      ? `saved ${liveKeys.secret_hint || "••••"}`
      : "Secret… paste here";
  }

  if (s.account && !s.account_error) {
    lastAccount = {
      ...(lastAccount || {}),
      id: s.account.id || lastAccount?.id,
      status: s.account.status || lastAccount?.status || "ACTIVE",
      equity: s.account.equity ?? lastAccount?.equity,
      paper: s.account.paper !== false,
      trading_mode: s.account.trading_mode || mode,
    };
  }
  applyTradingEnv(s);
  syncConfigConnectionSafe();
}

/** Persistent Paper/Live environment cue across every desk page.
 *
 *  `trading_mode` arrives as a status object from /api/status and as a bare
 *  "paper"/"live" string on the account and positions payloads — accept both
 *  so no caller has to know which shape it happens to be holding. */
function applyTradingEnv(info) {
  const s =
    (typeof info === "string" ? { mode: info } : info) || lastAlpacaStatus || {};
  const mode =
    s.mode || s.trading_mode || (s.paper === false ? "live" : "paper");
  const isLive = mode === "live";

  document.body.dataset.tradingMode = mode;
  document.body.classList.toggle("is-live-env", isLive);

  const eyebrows = document.querySelectorAll(".eyebrow");
  eyebrows.forEach((el) => {
    el.dataset.i18n = isLive ? "eyebrow_live" : "eyebrow";
    el.textContent = isLive
      ? tx("eyebrow_live", "Alpaca · Live")
      : tx("eyebrow", "Alpaca · Simulated");
    el.classList.toggle("is-live", isLive);
  });

  let banner = $("env-banner");
  if (!banner) {
    const app = document.querySelector(".app");
    if (app) {
      banner = document.createElement("div");
      banner.id = "env-banner";
      banner.className = "env-banner";
      banner.setAttribute("role", "status");
      const toast = $("toast");
      if (toast) app.insertBefore(banner, toast);
      else app.insertBefore(banner, app.firstChild?.nextSibling || null);
    }
  }
  if (banner) {
    if (isLive) {
      banner.hidden = false;
      banner.dataset.kind = "live";
      banner.textContent = tx(
        "live_banner_authorized",
        "LIVE TRADING — real money. Orders will reach the live account."
      );
    } else {
      banner.hidden = true;
      banner.textContent = "";
    }
  }
}

function nyTodayIso() {
  const p = etParts(Date.now());
  if (!p) {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }
  return `${p.year}-${String(p.month + 1).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}

function deskDateLocale() {
  const lang =
    (typeof i18n !== "undefined" && i18n.getCurrentLanguage?.()) ||
    document.documentElement.lang ||
    "en";
  return { en: "en-US", bn: "bn", es: "es", fr: "fr", hi: "hi" }[lang] || "en-US";
}

function parseIsoDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || "").trim());
  if (!m) return null;
  return { y: Number(m[1]), m: Number(m[2]) - 1, d: Number(m[3]) };
}

function isoFromParts(y, m, d) {
  return `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

function formatFieldDate(iso) {
  const parts = parseIsoDate(iso);
  if (!parts) return "";
  try {
    return new Intl.DateTimeFormat(deskDateLocale(), {
      timeZone: "UTC",
      day: "numeric",
      month: "short",
      year: "numeric",
    }).format(new Date(Date.UTC(parts.y, parts.m, parts.d)));
  } catch {
    return `${parts.d} ${MONTHS_SHORT[parts.m]} ${parts.y}`;
  }
}

function syncDateFieldDisplay(input) {
  const valueEl = input?.closest(".date-field-shell")?.querySelector(".date-field-value");
  if (!valueEl) return;
  if (input.value) {
    valueEl.textContent = formatFieldDate(input.value);
    valueEl.classList.remove("is-placeholder");
  } else {
    valueEl.textContent = tx("date_placeholder", "Pick a day");
    valueEl.classList.add("is-placeholder");
  }
}

const deskDatePicker = {
  el: null,
  input: null,
  shell: null,
  trigger: null,
  viewY: 0,
  viewM: 0,
  open: false,
};

function datePickerMate(input) {
  const range = input?.closest(".date-range");
  if (!range) return null;
  return [...range.querySelectorAll('input[type="date"]')].find((el) => el !== input) || null;
}

function ensureDeskDatePicker() {
  if (deskDatePicker.el) return deskDatePicker.el;
  const el = document.createElement("div");
  el.id = "desk-date-picker";
  el.className = "date-picker";
  el.hidden = true;
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-modal", "true");
  el.innerHTML =
    `<div class="date-picker-head">` +
    `<button type="button" class="date-picker-nav" data-dir="-1" aria-label="">` +
    `<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false"><path fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" d="M14.5 5.5L8 12l6.5 6.5"/></svg>` +
    `</button>` +
    `<h3 class="date-picker-month" id="date-picker-month"></h3>` +
    `<button type="button" class="date-picker-nav" data-dir="1" aria-label="">` +
    `<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false"><path fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" d="M9.5 5.5L16 12l-6.5 6.5"/></svg>` +
    `</button>` +
    `</div>` +
    `<div class="date-picker-weekdays" aria-hidden="true"></div>` +
    `<div class="date-picker-grid" role="grid" aria-labelledby="date-picker-month"></div>` +
    `<div class="date-picker-foot">` +
    `<button type="button" class="date-picker-today"></button>` +
    `</div>`;
  document.body.appendChild(el);
  deskDatePicker.el = el;
  el.querySelectorAll(".date-picker-nav").forEach((btn) => {
    btn.addEventListener("click", () => {
      const dir = Number(btn.dataset.dir) || 0;
      let m = deskDatePicker.viewM + dir;
      let y = deskDatePicker.viewY;
      if (m < 0) {
        m = 11;
        y -= 1;
      } else if (m > 11) {
        m = 0;
        y += 1;
      }
      deskDatePicker.viewY = y;
      deskDatePicker.viewM = m;
      renderDeskDatePicker({ focusDay: false });
    });
  });
  el.querySelector(".date-picker-today")?.addEventListener("click", () => {
    commitDeskDate(nyTodayIso());
  });
  el.addEventListener("click", (event) => {
    const day = event.target.closest(".date-picker-day");
    if (!day?.dataset.iso) return;
    commitDeskDate(day.dataset.iso);
  });
  el.addEventListener("keydown", onDeskDatePickerKeydown);
  return el;
}

function refreshDatePickerChrome() {
  const el = deskDatePicker.el;
  if (!el) return;
  el.setAttribute("aria-label", tx("date_picker_label", "Choose a date"));
  const prev = el.querySelector('[data-dir="-1"]');
  const next = el.querySelector('[data-dir="1"]');
  if (prev) prev.setAttribute("aria-label", tx("date_prev_month", "Previous month"));
  if (next) next.setAttribute("aria-label", tx("date_next_month", "Next month"));
  const todayBtn = el.querySelector(".date-picker-today");
  if (todayBtn) todayBtn.textContent = tx("date_today_ny", "Today · New York");
  const weekdays = el.querySelector(".date-picker-weekdays");
  if (weekdays) {
    const loc = deskDateLocale();
    weekdays.innerHTML = Array.from({ length: 7 }, (_, i) => {
      const d = new Date(Date.UTC(2023, 0, 1 + i));
      let label = "";
      try {
        label = new Intl.DateTimeFormat(loc, { weekday: "narrow", timeZone: "UTC" }).format(d);
      } catch {
        label = "SMTWTFS".charAt(i);
      }
      const weekend = i === 0 || i === 6 ? " is-weekend" : "";
      return `<span class="date-picker-weekday${weekend}">${escapeHtml(label)}</span>`;
    }).join("");
  }
}

function renderDeskDatePicker({ focusDay = true } = {}) {
  const el = ensureDeskDatePicker();
  refreshDatePickerChrome();
  const monthEl = el.querySelector(".date-picker-month");
  const grid = el.querySelector(".date-picker-grid");
  if (!monthEl || !grid) return;
  const { viewY: y, viewM: m } = deskDatePicker;
  try {
    monthEl.textContent = new Intl.DateTimeFormat(deskDateLocale(), {
      month: "long",
      year: "numeric",
    }).format(new Date(y, m, 1));
  } catch {
    monthEl.textContent = `${MONTHS_LONG[m]} ${y}`;
  }
  const selected = deskDatePicker.input?.value || "";
  const mate = datePickerMate(deskDatePicker.input)?.value || "";
  const today = nyTodayIso();
  const startIso = selected && mate && selected > mate ? mate : selected && mate ? selected : "";
  const endIso = selected && mate && selected > mate ? selected : selected && mate ? mate : "";
  const firstDow = new Date(y, m, 1).getDay();
  const leading = firstDow;
  const cells = [];
  for (let i = 0; i < 42; i += 1) {
    const dayNum = i - leading + 1;
    const cell = new Date(y, m, dayNum);
    const iso = isoFromParts(cell.getFullYear(), cell.getMonth(), cell.getDate());
    const outside = cell.getMonth() !== m;
    const weekend = cell.getDay() === 0 || cell.getDay() === 6;
    const isSelected = iso === selected;
    const inRange = Boolean(startIso && endIso && iso >= startIso && iso <= endIso);
    const classes = ["date-picker-day"];
    if (outside) classes.push("is-outside");
    if (weekend) classes.push("is-weekend");
    if (iso === today) classes.push("is-today");
    if (isSelected) classes.push("is-selected");
    if (inRange) classes.push("is-in-range");
    if (iso === startIso) classes.push("is-range-start");
    if (iso === endIso) classes.push("is-range-end");
    let aria = iso;
    try {
      aria = new Intl.DateTimeFormat(deskDateLocale(), {
        timeZone: "UTC",
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric",
      }).format(new Date(Date.UTC(cell.getFullYear(), cell.getMonth(), cell.getDate())));
    } catch {
      aria = iso;
    }
    cells.push(
      `<button type="button" class="${classes.join(" ")}" data-iso="${iso}" aria-label="${escapeHtml(aria)}" aria-pressed="${isSelected ? "true" : "false"}">${cell.getDate()}</button>`
    );
  }
  grid.innerHTML = cells.join("");
  positionDeskDatePicker();
  if (!focusDay) return;
  const focusIso = selected || today;
  const focusBtn =
    grid.querySelector(`.date-picker-day[data-iso="${focusIso}"]`) ||
    grid.querySelector(".date-picker-day:not(.is-outside)");
  focusBtn?.focus();
}

function positionDeskDatePicker() {
  const panel = deskDatePicker.el;
  const shell = deskDatePicker.shell;
  if (!panel || !shell || panel.hidden) return;
  const rect = shell.getBoundingClientRect();
  const pw = panel.offsetWidth;
  const ph = panel.offsetHeight;
  let top = rect.bottom + 8;
  let left = rect.left;
  if (top + ph > window.innerHeight - 8) {
    top = Math.max(8, rect.top - ph - 8);
  }
  if (left + pw > window.innerWidth - 8) {
    left = Math.max(8, rect.right - pw);
  }
  if (left < 8) left = 8;
  panel.style.top = `${Math.round(top)}px`;
  panel.style.left = `${Math.round(left)}px`;
}

function commitDeskDate(iso) {
  const input = deskDatePicker.input;
  if (!input) return;
  input.value = iso;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
  syncDateFieldDisplay(input);
  const mate = datePickerMate(input);
  const wasFrom = mate && [...(input.closest(".date-range")?.querySelectorAll('input[type="date"]') || [])][0] === input;
  closeDeskDatePicker();
  if (wasFrom && mate && !mate.value) {
    openDeskDatePicker(mate);
  }
}

function closeDeskDatePicker() {
  if (!deskDatePicker.open) return;
  const trigger = deskDatePicker.trigger;
  deskDatePicker.el.hidden = true;
  deskDatePicker.open = false;
  deskDatePicker.shell?.classList.remove("is-open");
  trigger?.setAttribute("aria-expanded", "false");
  deskDatePicker.input = null;
  deskDatePicker.shell = null;
  deskDatePicker.trigger = null;
  trigger?.focus();
}

function openDeskDatePicker(input) {
  if (!input) return;
  const shell = input.closest(".date-field-shell");
  if (!shell) return;
  if (deskDatePicker.open && deskDatePicker.input === input) {
    closeDeskDatePicker();
    return;
  }
  const el = ensureDeskDatePicker();
  const selected = parseIsoDate(input.value);
  const today = parseIsoDate(nyTodayIso()) || { y: new Date().getFullYear(), m: new Date().getMonth(), d: 1 };
  deskDatePicker.input = input;
  deskDatePicker.shell = shell;
  deskDatePicker.trigger = shell.querySelector(".date-field-trigger");
  deskDatePicker.viewY = selected?.y ?? today.y;
  deskDatePicker.viewM = selected?.m ?? today.m;
  deskDatePicker.open = true;
  el.hidden = false;
  shell.classList.add("is-open");
  deskDatePicker.trigger?.setAttribute("aria-expanded", "true");
  deskDatePicker.trigger?.setAttribute("aria-controls", "desk-date-picker");
  renderDeskDatePicker();
}

function onDeskDatePickerKeydown(event) {
  if (!deskDatePicker.open) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeDeskDatePicker();
    return;
  }
  const current = event.target.closest?.(".date-picker-day");
  if (!current?.dataset.iso) return;
  const parts = parseIsoDate(current.dataset.iso);
  if (!parts) return;
  const shift = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[event.key];
  if (shift) {
    event.preventDefault();
    const next = new Date(parts.y, parts.m, parts.d + shift);
    const iso = isoFromParts(next.getFullYear(), next.getMonth(), next.getDate());
    if (next.getMonth() !== deskDatePicker.viewM || next.getFullYear() !== deskDatePicker.viewY) {
      deskDatePicker.viewY = next.getFullYear();
      deskDatePicker.viewM = next.getMonth();
      renderDeskDatePicker({ focusDay: false });
    }
    deskDatePicker.el.querySelector(`.date-picker-day[data-iso="${iso}"]`)?.focus();
    return;
  }
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    commitDeskDate(current.dataset.iso);
  }
}

function initDateFields(root = document) {
  root.querySelectorAll(".date-field-shell").forEach((shell) => {
    const input = shell.querySelector('input[type="date"]');
    if (!input || input.dataset.dateFieldBound) return;
    input.dataset.dateFieldBound = "1";
    shell.classList.add("is-enhanced");
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "date-field-trigger";
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-expanded", "false");
    const valueEl = document.createElement("span");
    valueEl.className = "date-field-value";
    trigger.appendChild(valueEl);
    const caption = shell.closest("label")?.querySelector(":scope > span");
    if (caption) {
      if (!caption.id) caption.id = `${input.id}-caption`;
      valueEl.id = `${input.id}-value`;
      trigger.setAttribute("aria-labelledby", `${caption.id} ${valueEl.id}`);
    }
    input.tabIndex = -1;
    input.setAttribute("aria-hidden", "true");
    shell.insertBefore(trigger, input);
    syncDateFieldDisplay(input);
    shell.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openDeskDatePicker(input);
    });
    input.addEventListener("input", () => syncDateFieldDisplay(input));
    input.addEventListener("change", () => syncDateFieldDisplay(input));
    input.addEventListener("focus", () => openDeskDatePicker(input));
  });
}

document.addEventListener("mousedown", (event) => {
  if (!deskDatePicker.open) return;
  if (deskDatePicker.el?.contains(event.target)) return;
  if (deskDatePicker.shell?.contains(event.target)) return;
  closeDeskDatePicker();
});

window.addEventListener("resize", () => {
  if (deskDatePicker.open) positionDeskDatePicker();
});

document.addEventListener(
  "scroll",
  () => {
    if (deskDatePicker.open) positionDeskDatePicker();
  },
  true
);

function providerKeyReady(provider) {
  const status = lastKeyStatus || {};
  const entry = provider === "gemini" ? status.gemini : status.openai;
  if (entry?.set) return true;
  // Fresh paste in the form counts for this session once saved — but before
  // save, still allow run if the field has a value (run-once posts the key).
  const field =
    provider === "gemini" ? $("field-gemini-key") : $("field-openai-key");
  return !!(field?.value || "").trim();
}

// Global Polling loop.
setInterval(() => {
  if (document.hidden) return;
  refreshStatus().catch(() => {});
  if (typeof onDeskStatusInterval === "function") {
    onDeskStatusInterval();
  }
}, 2000);

// Global Language change listener
window.addEventListener("languageChange", () => {
  if (typeof i18n !== "undefined" && i18n.translateDOM) {
    i18n.translateDOM();
  }
  refreshNiceSelects();
  document.querySelectorAll(".date-field-shell input[type='date']").forEach(syncDateFieldDisplay);
  if (deskDatePicker.open) renderDeskDatePicker();
  if (typeof onDeskLanguageChange === "function") {
    onDeskLanguageChange();
  }
});

/** User Authentication Status & Masthead Integration */
let currentUser = null;

async function checkAuthStatus() {
  try {
    const res = await fetch("/api/auth/me");
    if (!res.ok) return null;
    const data = await res.json();
    if (data.ok && data.authenticated && data.user) {
      currentUser = data.user;
      return data.user;
    }
  } catch (err) {
    console.warn("Auth check failed:", err);
  }
  currentUser = null;
  return null;
}

async function handleUserLogout() {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } catch (e) {}
  window.location.href = "/login";
}

async function initUserAuthStatus() {
  const mastheadRight = document.querySelector(".masthead-right");
  if (!mastheadRight) return;

  const user = await checkAuthStatus();

  if (!user) {
    const path = window.location.pathname;
    if (!path.startsWith("/login") && !path.startsWith("/signup")) {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = `/login?next=${next}`;
      return;
    }
  }

  // Remove any existing widget if re-initializing
  const existing = mastheadRight.querySelector(".masthead-auth-widget");
  if (existing) existing.remove();

  const widget = document.createElement("div");
  widget.className = "masthead-auth-widget";

  if (user) {
    const displayName = escapeHtml(user.display_name || user.username || "Trader");
    const role = escapeHtml(user.role || "trader");
    const initials = computeInitials(user.display_name || user.username);
    const roleLower = String(user.role || "trader").toLowerCase();
    const isAdmin = roleLower === "admin" || roleLower === "owner";
    const currentPath = window.location.pathname;
    const isUserMenuActive = ["/settings", "/configuration", "/admin"].includes(currentPath);

    widget.innerHTML = `
      <div class="masthead-user-widget">
        <button type="button" class="masthead-user-trigger ${isUserMenuActive ? 'is-active-page' : ''}" id="masthead-user-trigger" aria-haspopup="true" aria-expanded="false" aria-label="User Account">
          <span class="masthead-user-avatar">${initials}</span>
          <span class="masthead-user-name">${displayName}</span>
          <svg class="masthead-user-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="6 9 12 15 18 9"></polyline>
          </svg>
        </button>
        <div class="masthead-user-menu" id="masthead-user-menu" aria-labelledby="masthead-user-trigger" hidden>
          <div class="masthead-user-info">
            <div class="masthead-info-name">${displayName}</div>
            <span class="masthead-info-role ${isAdmin ? 'is-admin-role' : ''}">${role}</span>
          </div>
          <a href="/settings" data-page="settings" class="masthead-menu-item ${currentPath === "/settings" ? "is-active" : ""}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
              <circle cx="12" cy="7" r="4"></circle>
            </svg>
            <span data-i18n="user_settings">User Settings</span>
          </a>
          <a href="/configuration" data-page="configuration" class="masthead-menu-item ${currentPath === "/configuration" ? "is-active" : ""}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="4" y1="21" x2="4" y2="14"></line>
              <line x1="4" y1="10" x2="4" y2="3"></line>
              <line x1="12" y1="21" x2="12" y2="12"></line>
              <line x1="12" y1="8" x2="12" y2="3"></line>
              <line x1="20" y1="21" x2="20" y2="16"></line>
              <line x1="20" y1="12" x2="20" y2="3"></line>
              <line x1="1" y1="14" x2="7" y2="14"></line>
              <line x1="9" y1="8" x2="15" y2="8"></line>
              <line x1="17" y1="16" x2="23" y2="16"></line>
            </svg>
            <span data-i18n="nav_configuration">Configuration</span>
          </a>
          ${isAdmin ? `
          <a href="/admin" data-page="admin" class="masthead-menu-item is-admin-link ${currentPath === "/admin" ? "is-active" : ""}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
            </svg>
            <span data-i18n="admin_dashboard">Admin Dashboard</span>
          </a>
          ` : ''}
          <div class="masthead-menu-divider" role="separator"></div>
          <button type="button" class="masthead-menu-item is-logout" id="btn-masthead-logout">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>
              <polyline points="16 17 21 12 16 7"></polyline>
              <line x1="21" y1="12" x2="9" y2="12"></line>
            </svg>
            <span data-i18n="sign_out">Sign Out</span>
          </button>
        </div>
      </div>
    `;

    const trigger = widget.querySelector("#masthead-user-trigger");
    const menu = widget.querySelector("#masthead-user-menu");
    const logoutBtn = widget.querySelector("#btn-masthead-logout");

    trigger?.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = !menu.hidden;
      menu.hidden = isOpen;
      trigger.setAttribute("aria-expanded", (!isOpen).toString());
    });

    document.addEventListener("click", (e) => {
      if (!widget.contains(e.target) && menu && !menu.hidden) {
        menu.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && menu && !menu.hidden) {
        menu.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
        trigger.focus();
      }
    });

    logoutBtn?.addEventListener("click", handleUserLogout);
  } else {
    const currentPath = window.location.pathname + window.location.search;
    const nextParam =
      currentPath && currentPath !== "/login" && currentPath !== "/signup"
        ? `?next=${encodeURIComponent(currentPath)}`
        : "";
    widget.innerHTML = `
      <div class="masthead-guest-actions">
        <a href="/login${nextParam}" class="masthead-auth-link is-login" data-i18n="nav_sign_in">Sign In</a>
        <a href="/signup${nextParam}" class="masthead-auth-link is-signup" data-i18n="nav_sign_up">Sign Up</a>
      </div>
    `;
  }

  mastheadRight.appendChild(widget);
  if (typeof i18n !== "undefined" && i18n.translateDOM) {
    i18n.translateDOM(widget);
  }

}

// Auto-run common routing & UI sync on load
document.addEventListener("DOMContentLoaded", () => {
  initRouting();
  initDeskNav();
  initNiceSelects();
  initDateFields();
  initUserAuthStatus();
});
if (document.readyState === "interactive" || document.readyState === "complete") {
  initRouting();
  initDeskNav();
  initNiceSelects();
  initDateFields();
  initUserAuthStatus();
}