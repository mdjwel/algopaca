/**
 * Common Shared JavaScript for AlgoPaca
 * Core utilities, state management, API client, NiceSelect, formatting, toasts, and status polling.
 */

// Initialize global theme from localStorage or cookies if set
(function initGlobalTheme() {
  try {
    const validThemes = ["obsidian", "midnight", "emerald", "daylight"];
    let savedTheme = localStorage.getItem("algopaca_theme");
    if (!savedTheme || !validThemes.includes(savedTheme)) {
      const match = document.cookie.match(/(?:^|;\s*)algopaca_theme=([^;]+)/);
      if (match && validThemes.includes(decodeURIComponent(match[1]))) {
        savedTheme = decodeURIComponent(match[1]);
      }
    }
    if (savedTheme && validThemes.includes(savedTheme)) {
      document.documentElement.setAttribute("data-theme", savedTheme);
    }
  } catch (e) {}
})();

function setDeskTheme(themeName) {
  const validThemes = ["obsidian", "midnight", "emerald", "daylight"];
  if (!validThemes.includes(themeName)) return;
  document.documentElement.setAttribute("data-theme", themeName);
  try {
    localStorage.setItem("algopaca_theme", themeName);
    document.cookie = `algopaca_theme=${encodeURIComponent(themeName)}; path=/; max-age=31536000; SameSite=Lax`;
  } catch (e) {}
  window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: themeName } }));
}

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

// Whenever a <details> accordion is expanded, refresh its child nice-selects
document.addEventListener("toggle", (ev) => {
  if (ev.target instanceof HTMLDetailsElement && ev.target.open) {
    refreshNiceSelects(ev.target);
  }
}, true);

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
  "api-keys",
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
  "api-keys": "/api-keys",
  admin: "/admin",
  "setup-wizard": "/setup-wizard",
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
  if (p === "configuration" || p === "config" || p === "api-keys")
    return "api-keys";
  if (p === "settings" || p === "user-settings")
    return "settings";
  if (p === "setup-wizard" || p === "wizard" || p === "setup" || p === "onboarding")
    return "setup-wizard";
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
 * Global timezone and time format retrieval & resolution for AlgoPaca desk.
 */
function getDeskTimezone() {
  try {
    let saved = localStorage.getItem("algopaca_timezone");
    if (!saved) {
      const match = document.cookie.match(/(?:^|;\s*)algopaca_timezone=([^;]+)/);
      if (match) saved = decodeURIComponent(match[1]);
    }
    return saved || "local";
  } catch (_) {
    return "local";
  }
}

function getEffectiveDeskTimezone(tzOverride) {
  const tz = tzOverride || getDeskTimezone();
  if (tz === "local") {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
    } catch (_) {
      return undefined;
    }
  }
  if (tz === "exchange") return "America/New_York";
  if (tz === "utc") return "UTC";
  return tz;
}

function getDeskTimeFormat() {
  try {
    let saved = localStorage.getItem("algopaca_time_format");
    if (!saved) {
      const match = document.cookie.match(/(?:^|;\s*)algopaca_time_format=([^;]+)/);
      if (match) saved = decodeURIComponent(match[1]);
    }
    return saved === "24h" ? "24h" : "12h";
  } catch (_) {
    return "12h";
  }
}

function isDeskHour12() {
  return getDeskTimeFormat() !== "24h";
}

/**
 * Wall-clock parts for an instant in the user's selected desk timezone.
 * Defaults to the user's configured timezone (Local / Exchange / UTC / IANA).
 */
let deskPartsFormatter = null;
let deskPartsKey = null;

function deskTimeParts(ms, tzOverride) {
  const effectiveTz = getEffectiveDeskTimezone(tzOverride);
  const hour12 = isDeskHour12();
  const cacheKey = `${effectiveTz || "default"}_${hour12 ? "12" : "24"}`;
  if (!deskPartsFormatter || deskPartsKey !== cacheKey) {
    try {
      const opts = {
        year: "numeric",
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: hour12,
        timeZoneName: "short",
      };
      if (effectiveTz) opts.timeZone = effectiveTz;
      deskPartsFormatter = new Intl.DateTimeFormat("en-US", opts);
      deskPartsKey = cacheKey;
    } catch (err) {
      deskPartsFormatter = null;
      return null;
    }
  }
  try {
    const out = {};
    for (const part of deskPartsFormatter.formatToParts(new Date(ms))) {
      out[part.type] = part.value;
    }
    const dayPeriod = out.dayPeriod || "";
    return {
      year: Number(out.year),
      month: Number(out.month) - 1,
      day: Number(out.day),
      hour: out.hour === "24" ? "00" : out.hour,
      minute: out.minute,
      dayPeriod: dayPeriod,
      zone: out.timeZoneName || "",
    };
  } catch (err) {
    return null;
  }
}

/** Legacy alias for backwards compatibility */
function etParts(ms) {
  return deskTimeParts(ms);
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

/** e.g. 12 June 2026 — optional · HH:MM for timed bars in active timezone & format */
function formatDisplayDate(iso, { withTime = false, hour12 } = {}) {
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) {
    const raw = String(iso || "").trim();
    return raw ? raw.replace("T", " ").slice(0, 19) : "—";
  }
  const p = deskTimeParts(t);
  if (!p) {
    const d = new Date(t);
    return `${d.getUTCDate()} ${MONTHS_LONG[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  }
  const base = `${p.day} ${MONTHS_LONG[p.month]} ${p.year}`;
  if (!withTime) return base;
  if (!/T\d{2}:\d{2}/.test(String(iso || ""))) return base;
  const ampm = p.dayPeriod ? ` ${p.dayPeriod}` : "";
  return `${base} · ${p.hour}:${p.minute}${ampm}`;
}

/** e.g. 13 August 2026 · 03:22 PM EDT — formatted in the user's active desk timezone & format */
function formatEtDate(iso, { withTime = false } = {}) {
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return formatDisplayDate(iso, { withTime });
  const p = deskTimeParts(t);
  if (!p) return formatDisplayDate(iso, { withTime });
  const base = `${p.day} ${MONTHS_LONG[p.month]} ${p.year}`;
  if (!withTime) return base;
  if (!/T\d{2}:\d{2}/.test(String(iso || ""))) return base;
  const ampm = p.dayPeriod ? ` ${p.dayPeriod}` : "";
  const zoneStr = p.zone ? ` ${p.zone}` : "";
  return `${base} · ${p.hour}:${p.minute}${ampm}${zoneStr}`;
}

/** Sortable calendar-day key in active desk timezone, used to group fills into trading days. */
function etDayKey(iso) {
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return "";
  const p = deskTimeParts(t);
  if (!p) return String(iso || "").slice(0, 10);
  return `${p.year}-${String(p.month + 1).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}

function formatEtDayLabel(iso) {
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return "—";
  const p = deskTimeParts(t);
  if (!p) return formatDisplayDate(iso);
  return `${p.day} ${MONTHS_LONG[p.month]} ${p.year}`;
}

/** Formats a clock time in active desk timezone & 12h/24h format */
function formatDeskTime(isoString, { withSeconds = false, hour12 } = {}) {
  if (!isoString) return "";
  const t = typeof isoString === "number" ? isoString : parseBtTime(isoString);
  if (!Number.isFinite(t)) return "";
  const d = new Date(t);
  const effectiveTz = getEffectiveDeskTimezone();
  const locale = document.documentElement.lang || undefined;
  const useHour12 = hour12 !== undefined ? hour12 : isDeskHour12();
  const opts = {
    hour: useHour12 ? "numeric" : "2-digit",
    minute: "2-digit",
    hour12: useHour12,
    ...(withSeconds ? { second: "2-digit" } : {}),
  };
  if (effectiveTz) opts.timeZone = effectiveTz;
  try {
    return new Intl.DateTimeFormat(locale, opts).format(d);
  } catch (_) {
    return d.toLocaleTimeString(locale, opts);
  }
}

/** Formats a date + time in active desk timezone & 12h/24h format */
function formatDeskDateTime(isoString, { withTime = true, shortDate = false, hour12 } = {}) {
  if (!isoString) return "—";
  const t = typeof isoString === "number" ? isoString : parseBtTime(isoString);
  if (!Number.isFinite(t)) return String(isoString || "—");
  const d = new Date(t);
  const effectiveTz = getEffectiveDeskTimezone();
  const locale = document.documentElement.lang || undefined;
  const useHour12 = hour12 !== undefined ? hour12 : isDeskHour12();
  const opts = {
    year: "numeric",
    month: shortDate ? "short" : "short",
    day: "numeric",
  };
  if (withTime) {
    opts.hour = useHour12 ? "numeric" : "2-digit";
    opts.minute = "2-digit";
    opts.hour12 = useHour12;
  }
  if (effectiveTz) opts.timeZone = effectiveTz;
  try {
    return new Intl.DateTimeFormat(locale, opts).format(d);
  } catch (_) {
    return d.toLocaleString(locale, opts);
  }
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

const OCC_RE = /^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/;
const OCC_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function isOccSymbol(symbol) {
  return OCC_RE.test(String(symbol || "").trim().toUpperCase());
}

function parseOcc(symbol) {
  const s = String(symbol || "").trim().toUpperCase();
  const match = s.match(OCC_RE);
  if (!match) return null;
  const root = match[1];
  const yy = parseInt(match[2], 10);
  const mm = parseInt(match[3], 10);
  const dd = parseInt(match[4], 10);
  const year = yy < 80 ? 2000 + yy : 1900 + yy;
  const cp = match[5];
  const strike = parseInt(match[6], 10) / 1000;
  const mon = OCC_MONTHS[mm - 1] || `${mm}`;
  const formattedStrike = Number.isInteger(strike) ? `$${strike}` : `$${strike.toFixed(2).replace(/\.?0+$/, "")}`;
  const formattedExpiry = `${dd} ${mon} ${String(year).slice(-2)}`;
  const label = `${root} ${String(dd).padStart(2, "0")}${mon}${String(year).slice(-2)} ${Number.isInteger(strike) ? strike : strike.toFixed(2)}${cp}`;
  return {
    symbol: match[0],
    root,
    year,
    month: mm,
    day: dd,
    type: cp === "C" ? "call" : "put",
    cp,
    strike,
    formattedStrike,
    formattedExpiry,
    label,
  };
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

const _avatarHashCache = new Map();

/**
 * Compute SHA-256 hex string for a given text (e.g. user email for Gravatar).
 * Uses Web Crypto API when available, with an in-memory cache.
 */
async function computeSha256Hex(str) {
  const norm = String(str || "").trim().toLowerCase();
  if (!norm) return "";
  if (_avatarHashCache.has(norm)) return _avatarHashCache.get(norm);
  try {
    if (window.crypto && crypto.subtle) {
      const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(norm));
      const hash = Array.from(new Uint8Array(buf))
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
      _avatarHashCache.set(norm, hash);
      return hash;
    }
  } catch (err) {
    console.warn("Crypto digest unavailable:", err);
  }
  return "";
}
window.computeSha256Hex = computeSha256Hex;

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
  // Missing Alpaca credentials — link to the API Keys page.
  if (/ALPACA_(API|SECRET)_KEY/.test(text)) {
    el.innerHTML =
      escapeHtml(text).replaceAll("\n", "<br>") +
      ` <a href="${pagePath("api-keys")}">Open API Keys</a>`;
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
    const curPath = window.location.pathname;
    if (
      !curPath.startsWith("/login") &&
      !curPath.startsWith("/signup") &&
      !curPath.startsWith("/setup-wizard") &&
      !curPath.startsWith("/wizard")
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
  const a = status.anthropic || { set: !!aiReady?.anthropic, source: "none", hint: "" };
  const x = status.xai || { set: !!aiReady?.xai, source: "none", hint: "" };
  const fmt = (entry, name) => {
    if (!entry.set) return `${name} missing`;
    const src = entry.source === "ui" ? "UI" : entry.source === "env" ? ".env" : "";
    return `${name} saved${src ? ` (${src})` : ""}`;
  };
  if (el) {
    const missing = !o.set || !g.set || !a.set || !x.set;
    el.innerHTML =
      `AI keys: ${escapeHtml(fmt(o, "OpenAI"))} · ${escapeHtml(fmt(g, "Gemini"))} · ${escapeHtml(fmt(a, "Anthropic"))} · ${escapeHtml(fmt(x, "xAI"))}` +
      (missing
        ? ` — <a href="${pagePath("api-keys")}">API Keys</a>`
        : "");
  }

  const oHint = $("openai-key-hint");
  const gHint = $("gemini-key-hint");
  const aHint = $("anthropic-key-hint");
  const xHint = $("xai-key-hint");
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
  if (aHint) {
    aHint.textContent = a.set
      ? `Saved (${a.source}): ${a.hint || "••••"} — leave blank to keep`
      : "Not set — paste an Anthropic key, then Save AI keys";
  }
  if (xHint) {
    xHint.textContent = x.set
      ? `Saved (${x.source}): ${x.hint || "••••"} — leave blank to keep`
      : "Not set — paste an xAI key, then Save AI keys";
  }

  const openaiField = $("field-openai-key");
  const geminiField = $("field-gemini-key");
  const anthropicField = $("field-anthropic-key");
  const xaiField = $("field-xai-key");
  if (openaiField && !openaiField.value) {
    openaiField.placeholder = o.set ? `saved ${o.hint || "••••"}` : "sk-… paste here";
  }
  if (geminiField && !geminiField.value) {
    geminiField.placeholder = g.set ? `saved ${g.hint || "••••"}` : "AIza… paste here";
  }
  if (anthropicField && !anthropicField.value) {
    anthropicField.placeholder = a.set ? `saved ${a.hint || "••••"}` : "sk-ant-… paste here";
  }
  if (xaiField && !xaiField.value) {
    xaiField.placeholder = x.set ? `saved ${x.hint || "••••"}` : "xai-… paste here";
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
      line.innerHTML = `Alpaca: missing — <a href="${pagePath("api-keys")}">set keys on API Keys</a>`;
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
    if (el.dataset.i18n === "eyebrow" || el.dataset.i18n === "eyebrow_live" || !el.dataset.i18n) {
      el.dataset.i18n = isLive ? "eyebrow_live" : "eyebrow";
      el.textContent = isLive
        ? tx("eyebrow_live", "AlgoPaca · Live")
        : tx("eyebrow", "AlgoPaca · Simulated");
      el.classList.toggle("is-live", isLive);
    }
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

const AI_PROVIDER_KEY_FIELD_IDS = {
  openai: "field-openai-key",
  gemini: "field-gemini-key",
  anthropic: "field-anthropic-key",
  xai: "field-xai-key",
};

function providerKeyReady(provider) {
  const status = lastKeyStatus || {};
  const entry = status[provider];
  if (entry?.set) return true;
  // Fresh paste in the form counts for this session once saved — but before
  // save, still allow run if the field has a value (run-once posts the key).
  const field = $(AI_PROVIDER_KEY_FIELD_IDS[provider] || "field-openai-key");
  return !!(field?.value || "").trim();
}

// Global Polling loop.
let currentPollInterval = 2000;
let pollTimerId = null;

async function doPoll() {
  const curPath = window.location.pathname;
  if (
    curPath.startsWith("/login") ||
    curPath.startsWith("/signup") ||
    curPath.startsWith("/setup-wizard") ||
    curPath.startsWith("/wizard")
  ) {
    return;
  }

  if (document.hidden) {
    pollTimerId = setTimeout(doPoll, 2000);
    return;
  }

  let success = false;
  try {
    await refreshStatus();
    success = true;
  } catch (e) {
    // fetch threw an error (likely connection refused)
  }

  if (success) {
    currentPollInterval = 2000;
    if (typeof onDeskStatusInterval === "function") {
      onDeskStatusInterval();
    }
  } else {
    // Exponential backoff to avoid spamming the console when the server is down
    currentPollInterval = Math.min(currentPollInterval * 1.5, 30000);
  }

  pollTimerId = setTimeout(doPoll, currentPollInterval);
}

const curPathOnLoad = window.location.pathname;
if (
  !curPathOnLoad.startsWith("/login") &&
  !curPathOnLoad.startsWith("/signup") &&
  !curPathOnLoad.startsWith("/setup-wizard") &&
  !curPathOnLoad.startsWith("/wizard")
) {
  pollTimerId = setTimeout(doPoll, currentPollInterval);
}

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
window.handleUserLogout = handleUserLogout;

async function initUserAuthStatus() {
  const mastheadRight = document.querySelector(".masthead-right");
  if (!mastheadRight) return;

  const user = await checkAuthStatus();

  if (!user) {
    const path = window.location.pathname;
    if (
      !path.startsWith("/login") &&
      !path.startsWith("/signup") &&
      !path.startsWith("/setup-wizard") &&
      !path.startsWith("/wizard")
    ) {
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
    const isUserMenuActive = ["/settings", "/api-keys", "/admin", "/setup-wizard"].includes(currentPath);
    const avatarUrl = user.avatar_url || "";
    const activeTheme = document.documentElement.getAttribute("data-theme") || localStorage.getItem("algopaca_theme") || "obsidian";

    widget.innerHTML = `
      <div class="masthead-user-widget">
        <button type="button" class="masthead-user-trigger ${isUserMenuActive ? 'is-active-page' : ''}" id="masthead-user-trigger" aria-haspopup="true" aria-expanded="false" aria-label="${displayName}" title="${displayName}">
          <span class="masthead-user-avatar" id="masthead-user-avatar">
            ${avatarUrl ? `
              <img
                src="${escapeHtml(avatarUrl)}"
                alt="${displayName}"
                class="masthead-user-avatar-img"
                loading="eager"
                decoding="async"
                onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='flex';"
              />
              <span class="masthead-user-avatar-fallback" style="display:none">${initials}</span>
            ` : `
              <span class="masthead-user-avatar-fallback">${initials}</span>
            `}
          </span>
        </button>
        <div class="masthead-user-menu" id="masthead-user-menu" aria-labelledby="masthead-user-trigger" hidden>
          <div class="masthead-user-info">
            <div class="masthead-info-name">${displayName}</div>
            ${user.email ? `<div class="masthead-info-email">${escapeHtml(user.email)}</div>` : ''}
            <span class="masthead-info-role ${isAdmin ? 'is-admin-role' : ''}">${role}</span>
          </div>
          <a href="/setup-wizard" data-page="setup-wizard" class="masthead-menu-item ${currentPath === "/setup-wizard" ? "is-active" : ""}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon>
            </svg>
            <span data-i18n="nav_setup_wizard">Setup Wizard</span>
          </a>
          <a href="/settings" data-page="settings" class="masthead-menu-item ${currentPath === "/settings" ? "is-active" : ""}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
              <circle cx="12" cy="7" r="4"></circle>
            </svg>
            <span data-i18n="user_settings">User Settings</span>
          </a>
          <a href="/api-keys" data-page="api-keys" class="masthead-menu-item ${currentPath === "/api-keys" ? "is-active" : ""}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4"></path>
              <path d="m21 2-9.6 9.6"></path>
              <circle cx="7.5" cy="15.5" r="5.5"></circle>
            </svg>
            <span data-i18n="nav_api_keys">API Keys</span>
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
          <div class="masthead-menu-theme-section">
            <div class="masthead-theme-header">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="masthead-theme-icon" aria-hidden="true">
                <circle cx="13.5" cy="6.5" r=".5" fill="currentColor"></circle>
                <circle cx="17.5" cy="10.5" r=".5" fill="currentColor"></circle>
                <circle cx="8.5" cy="7.5" r=".5" fill="currentColor"></circle>
                <circle cx="6.5" cy="12.5" r=".5" fill="currentColor"></circle>
                <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.563-2.512 5.563-5.563C22 6.5 17.5 2 12 2z"></path>
              </svg>
              <span class="masthead-theme-title" data-i18n="settings_theme_title">Terminal Theme</span>
            </div>
            <div class="masthead-theme-picker" id="masthead-theme-picker" role="radiogroup" aria-label="Terminal Themes" data-i18n-aria-label="terminal_themes">
              <button type="button" class="masthead-theme-btn ${activeTheme === 'obsidian' ? 'is-active' : ''}" data-theme-val="obsidian" role="radio" aria-checked="${activeTheme === 'obsidian'}" title="Obsidian Night" data-i18n-title="theme_obsidian">
                <span class="masthead-theme-swatch obsidian"></span>
                <span class="masthead-theme-label" data-i18n="theme_short_obsidian">Obsidian</span>
              </button>
              <button type="button" class="masthead-theme-btn ${activeTheme === 'midnight' ? 'is-active' : ''}" data-theme-val="midnight" role="radio" aria-checked="${activeTheme === 'midnight'}" title="Midnight Slate" data-i18n-title="theme_midnight">
                <span class="masthead-theme-swatch midnight"></span>
                <span class="masthead-theme-label" data-i18n="theme_short_midnight">Midnight</span>
              </button>
              <button type="button" class="masthead-theme-btn ${activeTheme === 'emerald' ? 'is-active' : ''}" data-theme-val="emerald" role="radio" aria-checked="${activeTheme === 'emerald'}" title="Emerald Forest" data-i18n-title="theme_emerald">
                <span class="masthead-theme-swatch emerald"></span>
                <span class="masthead-theme-label" data-i18n="theme_short_emerald">Emerald</span>
              </button>
              <button type="button" class="masthead-theme-btn ${activeTheme === 'daylight' ? 'is-active' : ''}" data-theme-val="daylight" role="radio" aria-checked="${activeTheme === 'daylight'}" title="Daylight Desk" data-i18n-title="theme_daylight">
                <span class="masthead-theme-swatch daylight"></span>
                <span class="masthead-theme-label" data-i18n="theme_short_daylight">Daylight</span>
              </button>
            </div>
          </div>
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

    // Dynamic resolution if avatarUrl was not provided directly
    if (!avatarUrl && user.email) {
      computeSha256Hex(user.email).then((hash) => {
        if (!hash) return;
        const imgUrl = `https://www.gravatar.com/avatar/${hash}?s=80&d=404`;
        const avatarWrap = widget.querySelector("#masthead-user-avatar");
        if (avatarWrap) {
          avatarWrap.innerHTML = `
            <img
              src="${imgUrl}"
              alt="${displayName}"
              class="masthead-user-avatar-img"
              loading="eager"
              decoding="async"
              onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='flex';"
            />
            <span class="masthead-user-avatar-fallback" style="display:none">${initials}</span>
          `;
        }
      });
    }

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

    // Theme switching inside masthead user menu
    const themeBtns = widget.querySelectorAll(".masthead-theme-btn");
    themeBtns.forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const themeVal = btn.getAttribute("data-theme-val");
        if (themeVal) {
          if (typeof setDeskTheme === "function") {
            setDeskTheme(themeVal);
          } else {
            document.documentElement.setAttribute("data-theme", themeVal);
            try {
              localStorage.setItem("algopaca_theme", themeVal);
              document.cookie = `algopaca_theme=${encodeURIComponent(themeVal)}; path=/; max-age=31536000; SameSite=Lax`;
            } catch (err) {}
            window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: themeVal } }));
          }
          syncMastheadThemeButtons();
        }
      });
    });

    function syncMastheadThemeButtons() {
      if (!widget.isConnected) {
        window.removeEventListener("themechange", syncMastheadThemeButtons);
        return;
      }
      const active = document.documentElement.getAttribute("data-theme") || localStorage.getItem("algopaca_theme") || "obsidian";
      widget.querySelectorAll(".masthead-theme-btn").forEach((btn) => {
        const isSelected = btn.getAttribute("data-theme-val") === active;
        btn.classList.toggle("is-active", isSelected);
        btn.setAttribute("aria-checked", isSelected ? "true" : "false");
      });
    }

    const themePicker = widget.querySelector("#masthead-theme-picker");
    themePicker?.addEventListener("keydown", (e) => {
      const buttons = Array.from(themePicker.querySelectorAll(".masthead-theme-btn"));
      const currentIndex = buttons.indexOf(document.activeElement);
      if (currentIndex === -1) return;

      let nextIndex = null;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") {
        nextIndex = (currentIndex + 1) % buttons.length;
      } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        nextIndex = (currentIndex - 1 + buttons.length) % buttons.length;
      }

      if (nextIndex !== null) {
        e.preventDefault();
        buttons[nextIndex].focus();
        buttons[nextIndex].click();
      }
    });

    window.addEventListener("themechange", syncMastheadThemeButtons);

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

  // Synchronize Mobile Drawer Profile State
  syncMobileProfileDrawer(user);
}

function syncMobileProfileDrawer(user) {
  const mobileProfileName = document.getElementById("mobile-profile-name");
  const mobileProfileAvatar = document.getElementById("mobile-profile-avatar");
  const mobileProfileRole = document.getElementById("mobile-profile-role");
  const mobileProfileMode = document.getElementById("mobile-profile-mode");
  const mobileAdminLink = document.getElementById("mobile-sheet-admin-link");
  const mobileSheetActions = document.getElementById("mobile-sheet-actions");

  if (!mobileProfileName && !mobileSheetActions) return;

  if (user) {
    const displayName = escapeHtml(user.display_name || user.username || "Trader");
    const role = escapeHtml(user.role || "trader");
    const initials = computeInitials(user.display_name || user.username);
    const roleLower = String(user.role || "trader").toLowerCase();
    const isAdmin = roleLower === "admin" || roleLower === "owner";
    const avatarUrl = user.avatar_url || "";

    if (mobileProfileName) mobileProfileName.textContent = displayName;
    if (mobileProfileAvatar) {
      if (avatarUrl) {
        mobileProfileAvatar.innerHTML = `
          <img
            src="${escapeHtml(avatarUrl)}"
            alt="${displayName}"
            class="mobile-profile-avatar-img"
            onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='flex';"
          />
          <span class="mobile-profile-avatar-fallback" style="display:none">${initials}</span>
        `;
      } else {
        mobileProfileAvatar.innerHTML = `<span class="mobile-profile-avatar-fallback">${initials}</span>`;
        if (user.email) {
          computeSha256Hex(user.email).then((hash) => {
            if (!hash) return;
            const imgUrl = `https://www.gravatar.com/avatar/${hash}?s=96&d=404`;
            mobileProfileAvatar.innerHTML = `
              <img
                src="${imgUrl}"
                alt="${displayName}"
                class="mobile-profile-avatar-img"
                onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='flex';"
              />
              <span class="mobile-profile-avatar-fallback" style="display:none">${initials}</span>
            `;
          });
        }
      }
    }
    if (mobileProfileRole) {
      mobileProfileRole.textContent = role;
      mobileProfileRole.classList.toggle("is-admin", isAdmin);
    }
    if (mobileAdminLink) {
      mobileAdminLink.hidden = !isAdmin;
    }
    if (mobileSheetActions) {
      mobileSheetActions.innerHTML = `
        <button type="button" class="mobile-sheet-btn is-logout" id="btn-mobile-logout">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg>
          <span data-i18n="nav_sign_out">Sign Out</span>
        </button>
      `;
      document.getElementById("btn-mobile-logout")?.addEventListener("click", handleUserLogout);
    }
  } else {
    if (mobileProfileName) mobileProfileName.textContent = "Guest";
    if (mobileProfileAvatar) mobileProfileAvatar.textContent = "G";
    if (mobileProfileRole) mobileProfileRole.textContent = "Guest";
    if (mobileAdminLink) mobileAdminLink.hidden = true;
    if (mobileSheetActions) {
      const currentPath = window.location.pathname + window.location.search;
      const nextParam =
        currentPath && currentPath !== "/login" && currentPath !== "/signup"
          ? `?next=${encodeURIComponent(currentPath)}`
          : "";
      mobileSheetActions.innerHTML = `
        <a href="/login${nextParam}" class="mobile-sheet-btn is-primary" data-i18n="nav_sign_in">Sign In</a>
        <a href="/signup${nextParam}" class="mobile-sheet-btn is-secondary" data-i18n="nav_sign_up">Sign Up</a>
      `;
    }
  }

  const modeBadge = document.querySelector(".mode-badge");
  if (modeBadge && mobileProfileMode) {
    const isLive = modeBadge.classList.contains("env-live") || modeBadge.textContent.toLowerCase().includes("live");
    mobileProfileMode.className = `mobile-profile-badge ${isLive ? "live" : "armed"}`;
    mobileProfileMode.textContent = isLive ? "Live" : "Paper";
  }
}
window.syncMobileProfileDrawer = syncMobileProfileDrawer;

/**
 * Adaptive Device Shell Loader
 * Loads mobile-shell.js on mobile (<= 768px) and desk-shell.js on desktop (> 768px).
 * Prevents mobile scripts & DOM from polluting desktop, and avoids desktop dropdown logic on mobile.
 */
function loadDeviceScript(src, id) {
  if (document.getElementById(id)) return;
  const s = document.createElement("script");
  s.id = id;
  s.src = src;
  s.defer = true;
  document.body.appendChild(s);
}

function initDeviceShell() {
  const isMobile = window.matchMedia("(max-width: 768px)").matches;
  if (isMobile) {
    loadDeviceScript("/static/js/mobile-shell.js", "script-mobile-shell");
  } else {
    loadDeviceScript("/static/js/desk-shell.js", "script-desk-shell");
  }
}

// Media query listener for responsive transitions (desktop window resizing / mobile devtools)
try {
  const mobileMql = window.matchMedia("(max-width: 768px)");
  const handleDeviceChange = (e) => {
    if (e.matches) {
      loadDeviceScript("/static/js/mobile-shell.js", "script-mobile-shell");
      if (typeof window.initMobileAppShell === "function") {
        window.initMobileAppShell();
      }
    } else {
      loadDeviceScript("/static/js/desk-shell.js", "script-desk-shell");
      if (typeof window.initDeskNav === "function") {
        window.initDeskNav();
      }
    }
  };
  if (typeof mobileMql.addEventListener === "function") {
    mobileMql.addEventListener("change", handleDeviceChange);
  } else if (typeof mobileMql.addListener === "function") {
    mobileMql.addListener(handleDeviceChange);
  }
} catch (err) {
  console.warn("Device shell MQL setup error:", err);
}

/**
 * Header Stock Ticker Search
 * Interactive search form in the masthead with real-time suggestion dropdown,
 * keyboard navigation (↑/↓/Enter/Esc), global shortcut (⌘K / Ctrl+K / /),
 * recent search history, portfolio indicators, and instant navigation/form loading.
 */
function initHeaderTickerSearch() {
  const mastheadTop = document.querySelector(".masthead-top");
  if (!mastheadTop) return;

  // Check if search form already exists, or inject it
  let searchContainer = document.getElementById("masthead-search-container");
  if (searchContainer && searchContainer.dataset.searchInitialized === "true") {
    return;
  }

  if (!searchContainer) {
    searchContainer = document.createElement("div");
    searchContainer.id = "masthead-search-container";
    searchContainer.className = "masthead-search";
    const mastheadRight = mastheadTop.querySelector(".masthead-right");
    if (mastheadRight) {
      mastheadTop.insertBefore(searchContainer, mastheadRight);
    } else {
      mastheadTop.appendChild(searchContainer);
    }
  }

  searchContainer.dataset.searchInitialized = "true";

  // If inside searchContainer HTML is not already populated:
  if (!searchContainer.querySelector(".masthead-search-form")) {
    const isMac = typeof navigator !== "undefined" && /Mac|iPod|iPhone|iPad/.test(navigator.platform || "");
    const shortcutText = isMac ? "⌘K" : "Ctrl+K";
    const placeholderText = tx("header_search_placeholder", "Search ticker or company… (e.g. AAPL, NVDA)");
    const ariaLabel = tx("header_search_aria", "Search stock tickers and markets");
    const shortcutTitle = tx("header_search_shortcut_title", "Press / or ⌘K to search tickers");
    const clearLabel = tx("header_search_clear", "Clear search");

    searchContainer.innerHTML = `
      <form class="masthead-search-form" id="header-ticker-search-form" role="search" onsubmit="return false;">
        <div class="masthead-search-box" id="header-ticker-search-box">
          <svg class="masthead-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="11" cy="11" r="8"></circle>
            <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
          </svg>
          <input
            type="search"
            id="header-ticker-search-input"
            class="masthead-search-input"
            placeholder="${escapeHtml(placeholderText)}"
            data-i18n-placeholder="header_search_placeholder"
            autocomplete="off"
            spellcheck="false"
            aria-label="${escapeHtml(ariaLabel)}"
            data-i18n-aria-label="header_search_aria"
            aria-autocomplete="list"
            aria-expanded="false"
            aria-controls="header-search-results"
          />
          <div class="masthead-search-controls">
            <button type="button" id="header-search-clear" class="masthead-search-clear" aria-label="${escapeHtml(clearLabel)}" data-i18n-aria-label="header_search_clear" hidden>
              <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <line x1="18" y1="6" x2="6" y2="18"></line>
                <line x1="6" y1="6" x2="18" y2="18"></line>
              </svg>
            </button>
            <kbd class="masthead-search-kbd" id="header-search-kbd" title="${escapeHtml(shortcutTitle)}" data-i18n-title="header_search_shortcut_title">${shortcutText}</kbd>
          </div>
        </div>
        <div id="header-search-dropdown" class="masthead-search-dropdown" hidden></div>
      </form>
    `;

    if (typeof i18n !== "undefined" && i18n.translateDOM) {
      i18n.translateDOM(searchContainer);
    }
  }

  const form = document.getElementById("header-ticker-search-form");
  const box = document.getElementById("header-ticker-search-box");
  const input = document.getElementById("header-ticker-search-input");
  const clearBtn = document.getElementById("header-search-clear");
  const kbd = document.getElementById("header-search-kbd");
  const dropdown = document.getElementById("header-search-dropdown");

  if (!input || !dropdown) return;

  // Platform check for KBD text
  const isMac = typeof navigator !== "undefined" && /Mac|iPod|iPhone|iPad/.test(navigator.platform || "");
  if (kbd) {
    kbd.textContent = isMac ? "⌘K" : "Ctrl+K";
  }

  let debounceTimer = null;
  let currentRequestId = 0;
  let selectedIndex = -1;
  let currentResults = [];

  const RECENT_KEY = "algopaca_recent_tickers";

  function getRecentSearches() {
    try {
      const raw = localStorage.getItem(RECENT_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  function saveRecentSearch(item) {
    if (!item || !item.symbol) return;
    try {
      const current = getRecentSearches().filter((x) => x.symbol !== item.symbol);
      current.unshift({
        symbol: item.symbol,
        name: item.name || item.symbol,
        exchange: item.exchange || "US",
        timestamp: Date.now(),
      });
      localStorage.setItem(RECENT_KEY, JSON.stringify(current.slice(0, 6)));
    } catch {}
  }

  function removeRecentSearch(symbol) {
    try {
      const current = getRecentSearches().filter((x) => x.symbol !== symbol);
      localStorage.setItem(RECENT_KEY, JSON.stringify(current));
    } catch {}
  }

  function closeDropdown() {
    dropdown.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    selectedIndex = -1;
    currentResults = [];
    if (box) box.classList.remove("has-focus");
  }

  function openDropdown() {
    dropdown.hidden = false;
    input.setAttribute("aria-expanded", "true");
    if (box) box.classList.add("has-focus");
  }

  function selectResult(item, action = "trade") {
    if (!item || !item.symbol) return;
    const symbol = item.symbol.toUpperCase();
    saveRecentSearch(item);
    closeDropdown();

    if (action === "auto") {
      if (window.location.pathname.startsWith("/auto-trade")) {
        const featInput = document.getElementById("field-symbol");
        if (featInput) {
          featInput.value = symbol;
          featInput.dispatchEvent(new Event("input", { bubbles: true }));
          featInput.dispatchEvent(new Event("change", { bubbles: true }));
          featInput.focus();
          if (typeof showToast === "function") {
            showToast(tx("symbol_loaded", "Loaded {symbol} in Auto Trade", { symbol }), "ok");
          }
          return;
        }
      }
      window.location.href = `/auto-trade?symbol=${encodeURIComponent(symbol)}`;
      return;
    }

    // If currently on /manual-order, seamlessly update form in place
    if (window.location.pathname.startsWith("/manual-order")) {
      const symInput = document.getElementById("manual-symbol");
      if (symInput) {
        symInput.value = symbol;
        symInput.dispatchEvent(new Event("input", { bubbles: true }));
        symInput.dispatchEvent(new Event("change", { bubbles: true }));
        symInput.focus();
        try {
          const newUrl = new URL(window.location.href);
          newUrl.searchParams.set("symbol", symbol);
          window.history.replaceState(null, "", newUrl.toString());
        } catch {}
        if (typeof showToast === "function") {
          showToast(tx("symbol_loaded", "Loaded {symbol} in order ticket", { symbol }), "ok");
        }
        return;
      }
    }

    // Default: navigate to /manual-order with symbol
    window.location.href = `/manual-order?symbol=${encodeURIComponent(symbol)}`;
  }

  function renderDropdown(items, isRecentView = false, query = "") {
    currentResults = items || [];
    selectedIndex = -1;

    if (!items || items.length === 0) {
      if (query) {
        dropdown.innerHTML = `
          <div class="search-empty-msg">
            <p>${escapeHtml(tx("header_search_no_results", 'No symbols or companies found for "{query}"', { query }))}</p>
            <button type="button" class="search-action-btn is-trade" style="margin-top: 0.5rem;" id="btn-search-force-symbol">
              ${escapeHtml(tx("header_search_action_trade", "Trade"))} ${escapeHtml(query.toUpperCase())} →
            </button>
          </div>
        `;
        const forceBtn = dropdown.querySelector("#btn-search-force-symbol");
        forceBtn?.addEventListener("click", () => {
          selectResult({ symbol: query.toUpperCase(), name: `${query.toUpperCase()} Equity` }, "trade");
        });
        openDropdown();
        return;
      } else {
        dropdown.innerHTML = `
          <div class="search-empty-msg">
            <p>${escapeHtml(tx("header_search_placeholder", "Search ticker or company… (e.g. AAPL, NVDA)"))}</p>
          </div>
        `;
        openDropdown();
        return;
      }
    }

    let html = "";
    if (isRecentView) {
      html += `
        <div class="search-section-label">
          <span>${escapeHtml(tx("header_search_recent", "Recent Searches"))}</span>
        </div>
      `;
    } else {
      const hasHoldings = items.some((x) => x.in_portfolio);
      const sectionTitle = hasHoldings
        ? tx("header_search_holdings", "Your Portfolio Holdings") + " & " + tx("header_search_popular", "Market Tickers")
        : (query ? tx("search_results", "Search Results") : tx("header_search_popular", "Popular Stocks & ETFs"));

      html += `
        <div class="search-section-label">
          <span>${escapeHtml(sectionTitle)}</span>
        </div>
      `;
    }

    html += `<ul class="search-results-list" role="listbox" id="header-search-results">`;

    items.forEach((item, idx) => {
      const sym = escapeHtml(item.symbol || "");
      const name = escapeHtml(item.name || sym);

      html += `
        <li class="search-result-item" id="search-opt-${idx}" role="option" aria-selected="false" data-index="${idx}" data-symbol="${sym}">
          <div class="search-item-main">
            <div class="search-item-top">
              <span class="search-item-symbol">${sym}</span>
            </div>
            <div class="search-item-name">${name}</div>
          </div>
          <div class="search-item-actions">
            ${isRecentView ? `
              <button type="button" class="masthead-search-clear btn-remove-recent" data-symbol="${sym}" aria-label="Remove from recent" title="Remove">
                <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18"></line>
                  <line x1="6" y1="6" x2="18" y2="18"></line>
                </svg>
              </button>
            ` : ""}
            <a href="/manual-order?symbol=${encodeURIComponent(item.symbol)}" class="search-action-btn is-trade" data-action="trade" title="Open in Advanced Order">
              ${escapeHtml(tx("header_search_action_trade", "Trade"))}
            </a>
            <a href="/auto-trade?symbol=${encodeURIComponent(item.symbol)}" class="search-action-btn" data-action="auto" title="View in Auto Trade">
              ${escapeHtml(tx("header_search_action_auto", "Auto"))}
            </a>
          </div>
        </li>
      `;
    });

    html += `</ul>`;
    html += `
      <div class="search-dropdown-footer">
        <span>↑↓ ${escapeHtml(tx("nav_navigate", "Navigate"))}</span>
        <span>↵ ${escapeHtml(tx("nav_select", "Select"))}</span>
        <span>Esc ${escapeHtml(tx("nav_close", "Close"))}</span>
      </div>
    `;

    dropdown.innerHTML = html;

    // Attach row click events
    dropdown.querySelectorAll(".search-result-item").forEach((el) => {
      el.addEventListener("click", (e) => {
        const tradeBtn = e.target.closest('.search-action-btn[data-action="trade"]');
        if (tradeBtn) {
          if (window.location.pathname.startsWith("/manual-order")) {
            e.preventDefault();
            const index = parseInt(el.getAttribute("data-index"), 10);
            if (!isNaN(index) && currentResults[index]) {
              selectResult(currentResults[index], "trade");
            }
          }
          return;
        }

        const autoBtn = e.target.closest('.search-action-btn[data-action="auto"]');
        if (autoBtn) {
          if (window.location.pathname.startsWith("/auto-trade")) {
            e.preventDefault();
            const index = parseInt(el.getAttribute("data-index"), 10);
            if (!isNaN(index) && currentResults[index]) {
              selectResult(currentResults[index], "auto");
            }
          }
          return;
        }

        if (e.target.closest(".btn-remove-recent")) {
          return;
        }

        const index = parseInt(el.getAttribute("data-index"), 10);
        if (!isNaN(index) && currentResults[index]) {
          selectResult(currentResults[index], "trade");
        }
      });
    });

    // Remove recent handler
    dropdown.querySelectorAll(".btn-remove-recent").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const sym = btn.getAttribute("data-symbol");
        if (sym) {
          removeRecentSearch(sym);
          const recents = getRecentSearches();
          if (recents.length > 0) {
            renderDropdown(recents, true, "");
          } else {
            fetchSearchResults("");
          }
        }
      });
    });

    openDropdown();
  }

  async function fetchSearchResults(query) {
    const q = String(query || "").trim();
    const reqId = ++currentRequestId;

    try {
      const data = await api(`/api/tickers/search?q=${encodeURIComponent(q)}&limit=10`);
      if (reqId !== currentRequestId) return;
      if (data && Array.isArray(data.results)) {
        renderDropdown(data.results, false, q);
      }
    } catch (err) {
      if (reqId !== currentRequestId) return;
      console.warn("Ticker search API error:", err);
      const recents = getRecentSearches();
      if (!q && recents.length > 0) {
        renderDropdown(recents, true, "");
      } else {
        renderDropdown([], false, q);
      }
    }
  }

  function handleInput() {
    const val = input.value.trim();
    if (clearBtn) clearBtn.hidden = val.length === 0;

    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      if (!val) {
        const recents = getRecentSearches();
        if (recents.length > 0) {
          renderDropdown(recents, true, "");
        } else {
          fetchSearchResults("");
        }
      } else {
        fetchSearchResults(val);
      }
    }, 150);
  }

  function updateSelectedRow(newIndex) {
    const items = dropdown.querySelectorAll(".search-result-item");
    if (!items.length) return;

    items.forEach((el, idx) => {
      if (idx === newIndex) {
        el.classList.add("is-selected");
        el.setAttribute("aria-selected", "true");
        input.setAttribute("aria-activedescendant", el.id);
        el.scrollIntoView({ block: "nearest", behavior: "smooth" });
      } else {
        el.classList.remove("is-selected");
        el.setAttribute("aria-selected", "false");
      }
    });
    selectedIndex = newIndex;
  }

  function handleKeyDown(e) {
    if (dropdown.hidden) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        handleInput();
        return;
      }
    }

    if (e.key === "ArrowDown") {
      e.preventDefault();
      const items = dropdown.querySelectorAll(".search-result-item");
      if (items.length > 0) {
        const next = selectedIndex < items.length - 1 ? selectedIndex + 1 : 0;
        updateSelectedRow(next);
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const items = dropdown.querySelectorAll(".search-result-item");
      if (items.length > 0) {
        const prev = selectedIndex > 0 ? selectedIndex - 1 : items.length - 1;
        updateSelectedRow(prev);
      }
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (selectedIndex >= 0 && currentResults[selectedIndex]) {
        selectResult(currentResults[selectedIndex], "trade");
      } else if (currentResults.length > 0) {
        selectResult(currentResults[0], "trade");
      } else if (input.value.trim()) {
        const raw = input.value.trim().toUpperCase();
        selectResult({ symbol: raw, name: `${raw} Equity` }, "trade");
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeDropdown();
      input.blur();
    }
  }

  input.addEventListener("input", handleInput);
  input.addEventListener("keydown", handleKeyDown);

  input.addEventListener("focus", () => {
    openDropdown();
    const val = input.value.trim();
    if (!val) {
      const recents = getRecentSearches();
      if (recents.length > 0) {
        renderDropdown(recents, true, "");
      } else {
        fetchSearchResults("");
      }
    } else {
      fetchSearchResults(val);
    }
  });

  clearBtn?.addEventListener("click", () => {
    input.value = "";
    clearBtn.hidden = true;
    input.focus();
    const recents = getRecentSearches();
    if (recents.length > 0) {
      renderDropdown(recents, true, "");
    } else {
      fetchSearchResults("");
    }
  });

  document.addEventListener("click", (e) => {
    if (!searchContainer.contains(e.target)) {
      closeDropdown();
    }
  });

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      input.focus();
      input.select();
      openDropdown();
      return;
    }

    if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const activeTag = document.activeElement ? document.activeElement.tagName.toLowerCase() : "";
      const isInput = activeTag === "input" || activeTag === "textarea" || activeTag === "select" || document.activeElement?.isContentEditable;
      if (!isInput) {
        e.preventDefault();
        input.focus();
        input.select();
        openDropdown();
      }
    }
  });
}
window.initHeaderTickerSearch = initHeaderTickerSearch;

// Auto-run common routing, adaptive device shell, & UI sync on load
document.addEventListener("DOMContentLoaded", () => {
  initRouting();
  initDeviceShell();
  initNiceSelects();
  initDateFields();
  initUserAuthStatus();
  initHeaderTickerSearch();
});
if (document.readyState === "interactive" || document.readyState === "complete") {
  initRouting();
  initDeviceShell();
  initNiceSelects();
  initDateFields();
  initUserAuthStatus();
  initHeaderTickerSearch();
}