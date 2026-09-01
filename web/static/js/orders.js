/**
 * Orders blotter — account-wide working and recently closed tickets.
 * Placement stays on Advanced Order; this page lists, cancels, and replaces.
 */

const ORD_REFRESH_MS = 15000;
const ORD_ARMED_REFRESH_MS = 5000;
const ORD_MODAL_IDS = [
  "ord-cancel-modal",
  "ord-cancel-all-modal",
  "ord-cancel-selected-modal",
  "ord-detail-modal",
  "ord-replace-modal",
];
const ORD_TYPES = new Set(["market", "limit", "stop", "stop_limit", "trailing_stop"]);
const ORD_KINDS = new Set(["working", "conditional", "attached"]);
const ORD_SORT_KEYS = new Set([
  "submitted",
  "symbol",
  "side",
  "type",
  "qty",
  "price",
  "mark",
  "distance",
  "status",
]);
// Text columns read best A→Z; everything else leads with the largest.
const ORD_SORT_TEXT_KEYS = new Set(["symbol", "side", "type", "status"]);
const ORD_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const REINVEST_LIVE = new Set(["waiting", "placing", "awaiting_fill"]);
const FOLLOWON_LIVE = new Set(["waiting", "placing"]);
const DIP_HUNT_LIVE = new Set([
  "watching_entry",
  "watching_stop",
  "hunting",
  "awaiting_fill",
  "placing",
]);
const DIP_HUNT_CANCELLABLE = new Set([
  "watching_entry",
  "watching_stop",
  "hunting",
  "awaiting_fill",
]);

let ordersPayload = null;
let ordersBusy = false;
let ordActiveRequests = 0;
let ordRequestSeq = 0;
let ordFilterStatus = "open";
let ordFilterSearch = "";
let ordFilterSide = "all";
let ordFilterKind = "all";
let ordFilterType = "all";
let ordLastFetchStartedAt = 0;
let ordSyncState = "live";
let ordLastUpdatedTime = null;
let ordLastUpdatedTimer = null;
let ordModalReturnFocus = null;
let ordReplaceTarget = null;
let ordCancelTarget = null;
let ordLastLoopRunning = null;
let ordOpenPlanFolds = new Set();
let ordFilterAfter = "";
let ordFilterUntil = "";
let ordSortKey = localStorage.getItem("desk_ord_sort_key") || "submitted";
let ordSortDir = localStorage.getItem("desk_ord_sort_dir") || "desc";
let ordSelectedIds = new Set();
let ordExpandedCardActions = new Set();
// Ids the broker has been told to drop but has not reported on yet; the row
// says so immediately instead of looking untouched until the next poll.
let ordCancelingIds = new Set();
let ordDetailTarget = null;
// Set only while a symbol search has been escalated to the broker; see the
// note in refreshOrders for why this cannot simply track the search box.
let ordSymbolScope = "";
let ordSymbolScopeTimer = null;

if (!ORD_SORT_KEYS.has(ordSortKey)) ordSortKey = "submitted";
if (ordSortDir !== "asc" && ordSortDir !== "desc") ordSortDir = "desc";

function isLiveEnv() {
  return document.body?.classList.contains("is-live-env");
}

function isAnyOrdModalOpen() {
  return ORD_MODAL_IDS.some((id) => {
    const el = $(id);
    return el && !el.hidden;
  });
}

function topmostOpenOrdModal() {
  for (let i = ORD_MODAL_IDS.length - 1; i >= 0; i -= 1) {
    const el = $(ORD_MODAL_IDS[i]);
    if (el && !el.hidden) return el;
  }
  return null;
}

function trapOrdModalFocus(event) {
  if (event.key !== "Tab") return;
  const modal = topmostOpenOrdModal();
  if (!modal) return;
  const focusables = Array.from(
    modal.querySelectorAll(
      "input:not([type=hidden]):not([disabled]), button:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex='-1'])"
    )
  ).filter((el) => el.offsetParent !== null);
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function mutationsLocked() {
  return !!loopRunning;
}

function readOrdQuery() {
  const params = new URLSearchParams(location.search);
  const symbol = String(params.get("symbol") || "").trim().toUpperCase();
  const status = String(params.get("status") || "").trim().toLowerCase();
  const side = String(params.get("side") || "").trim().toLowerCase();
  const kind = String(params.get("kind") || "").trim().toLowerCase();
  const type = String(params.get("type") || "").trim().toLowerCase();
  if (symbol) ordFilterSearch = symbol;
  if (status === "open" || status === "closed") ordFilterStatus = status;
  if (side === "buy" || side === "sell") ordFilterSide = side;
  if (ORD_KINDS.has(kind)) ordFilterKind = kind;
  if (ORD_TYPES.has(type)) ordFilterType = type;
  const after = String(params.get("after") || "").trim();
  const until = String(params.get("until") || "").trim();
  if (ORD_DATE_RE.test(after)) ordFilterAfter = after;
  if (ORD_DATE_RE.test(until)) ordFilterUntil = until;
  const sort = String(params.get("sort") || "").trim().toLowerCase();
  if (ORD_SORT_KEYS.has(sort)) ordSortKey = sort;
  const dir = String(params.get("dir") || "").trim().toLowerCase();
  if (dir === "asc" || dir === "desc") ordSortDir = dir;
}

function writeOrdQuery() {
  const params = new URLSearchParams();
  const symbol = String(ordFilterSearch || "").trim().toUpperCase();
  if (symbol) params.set("symbol", symbol);
  if (ordFilterStatus === "closed") params.set("status", "closed");
  if (ordFilterSide === "buy" || ordFilterSide === "sell") params.set("side", ordFilterSide);
  if (ORD_KINDS.has(ordFilterKind)) {
    params.set("kind", ordFilterKind);
  }
  if (ORD_TYPES.has(ordFilterType)) params.set("type", ordFilterType);
  if (ordFilterStatus === "closed") {
    if (ordFilterAfter) params.set("after", ordFilterAfter);
    if (ordFilterUntil) params.set("until", ordFilterUntil);
  }
  if (ordSortKey !== "submitted") params.set("sort", ordSortKey);
  if (ordSortDir !== "desc") params.set("dir", ordSortDir);
  const qs = params.toString();
  const next = qs ? `${location.pathname}?${qs}` : location.pathname;
  if (`${location.pathname}${location.search}` !== next) {
    history.replaceState(null, "", next);
  }
}

/** Closed tickets do not move, so the page stops polling for them unless a desk
 *  plan is still armed. The indicator has to admit that rather than keep
 *  claiming "Live" over a list nothing is refreshing. */
function ordAutoRefreshActive() {
  return ordFilterStatus === "open" || armedDeskCount() > 0;
}

function ordSyncLabel(state) {
  if (state === "error") return tx("positions_sync_error", "Sync error");
  if (state === "paused") return tx("paused", "Paused");
  if (state === "manual") return tx("ord_sync_manual", "Manual refresh");
  return tx("live_status", "Live");
}

function setOrdSyncState(state) {
  ordSyncState = state;
  const el = $("ord-sync-indicator");
  if (!el) return;
  el.dataset.state = state;
  el.classList.toggle("is-error", state === "error");
  el.classList.toggle("is-paused", state === "paused" || state === "manual");
  const label = ordSyncLabel(state);
  el.title =
    state === "manual"
      ? tx("ord_sync_manual_hint", "Closed tickets do not change — use Refresh to re-read them.")
      : label;
  el.setAttribute("aria-label", label);
  const text = $("ord-last-updated-text");
  if (text) text.textContent = label;
}

function startOrdSyncTimer() {
  if (ordLastUpdatedTimer) return;
  ordLastUpdatedTimer = setInterval(() => {
    const el = $("ord-last-updated-text");
    if (!el || !ordLastUpdatedTime) return;
    const age = Math.max(0, Math.round((Date.now() - ordLastUpdatedTime) / 1000));
    if (ordSyncState !== "live") {
      el.textContent = ordSyncLabel(ordSyncState);
      return;
    }
    el.textContent = age < 3 ? tx("live_status", "Live") : formatAge(age);
  }, 1000);
}

function ordTime(iso) {
  if (!iso) return "—";
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return "—";
  const p = etParts(t);
  if (!p) return formatEtDate(iso, { withTime: true });
  const today = etParts(Date.now());
  const locale = document.documentElement.lang || undefined;
  const date = new Date(t);
  const clock = new Intl.DateTimeFormat(locale, {
    timeZone: "America/New_York",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
  if (today && today.year === p.year && today.month === p.month && today.day === p.day) {
    return clock;
  }
  const day = new Intl.DateTimeFormat(locale, {
    timeZone: "America/New_York",
    day: "numeric",
    month: "short",
  }).format(date);
  return `${day} · ${clock}`;
}

function orderMoney(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat(document.documentElement.lang || undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(n);
}

function orderQty(value) {
  if (value == null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat(document.documentElement.lang || undefined, {
    maximumFractionDigits: 9,
  }).format(n);
}

function orderTypeLabel(type) {
  const key = String(type || "").toLowerCase();
  const map = {
    market: tx("market", "Market"),
    limit: tx("limit", "Limit"),
    stop: tx("type_stop", "Stop"),
    stop_limit: tx("type_stop_limit", "Stop limit"),
    trailing_stop: tx("type_trailing_stop", "Trailing stop"),
  };
  return map[key] || key || "—";
}

function tifLabel(tif) {
  const key = String(tif || "").toLowerCase();
  const map = {
    day: tx("tif_day", "Day"),
    gtc: tx("tif_gtc", "GTC — until cancelled"),
    ioc: tx("tif_ioc", "IOC — fill now or drop"),
    fok: tx("tif_fok", "FOK — all now or none"),
    opg: tx("tif_opg", "OPG — at the open"),
    cls: tx("tif_cls", "CLS — at the close"),
  };
  return map[key] || key || "—";
}

function orderTifLabel(order) {
  const base = tifLabel(order.time_in_force);
  return order.extended_hours
    ? `${base} · ${tx("extended_hours", "Extended hours")}`
    : base;
}

function statusLabel(status) {
  const key = String(status || "").toLowerCase();
  const map = {
    new: tx("ord_status_new", "New"),
    accepted: tx("ord_status_accepted", "Accepted"),
    pending_new: tx("ord_status_pending", "Pending"),
    partially_filled: tx("ord_status_partial", "Partial"),
    filled: tx("ord_status_filled", "Filled"),
    canceled: tx("ord_status_canceled", "Canceled"),
    cancelled: tx("ord_status_canceled", "Canceled"),
    expired: tx("ord_status_expired", "Expired"),
    rejected: tx("ord_status_rejected", "Rejected"),
    pending_cancel: tx("ord_status_pending_cancel", "Canceling"),
    pending_replace: tx("ord_status_pending_replace", "Replacing"),
    done_for_day: tx("ord_status_done_for_day", "Done for day"),
    replaced: tx("ord_status_replaced", "Replaced"),
    accepted_for_bidding: tx("ord_status_bidding", "Bidding"),
    stopped: tx("ord_status_stopped", "Stopped"),
    suspended: tx("ord_status_suspended", "Suspended"),
    calculated: tx("ord_status_calculated", "Calculated"),
  };
  return map[key] || key.replaceAll("_", " ") || "—";
}

function statusClass(status) {
  const key = String(status || "").toLowerCase();
  if (key === "partially_filled") return "is-partial";
  if (key === "filled") return "is-filled";
  if (["canceled", "cancelled", "expired", "rejected"].includes(key)) return "is-canceled";
  if (
    ["new", "accepted", "pending_new", "pending_cancel", "pending_replace", "accepted_for_bidding"].includes(
      key
    )
  ) {
    return "is-open";
  }
  return "";
}

function orderPriceText(order) {
  const parts = [];
  if (order.limit_price != null) {
    parts.push(`${tx("limit", "Limit")} ${orderMoney(order.limit_price)}`);
  }
  if (order.stop_price != null) {
    parts.push(`${tx("type_stop", "Stop")} ${orderMoney(order.stop_price)}`);
  }
  if (order.trail_percent != null) {
    parts.push(`${tx("trail_percent", "Trail %")} ${Number(order.trail_percent)}%`);
  } else if (order.trail_price != null) {
    parts.push(`${tx("trail_amount", "Trail amount")} ${orderMoney(order.trail_price)}`);
  }
  if (order.notional != null && !parts.length) {
    parts.push(orderMoney(order.notional));
  }
  if (order.filled_avg_price != null && Number(order.filled_qty || 0) > 0) {
    parts.push(`${tx("ord_avg_fill", "Avg")} ${orderMoney(order.filled_avg_price)}`);
  }
  return parts;
}

function qtyText(order) {
  const qty = order.qty;
  const filled = Number(order.filled_qty || 0);
  if (qty == null && order.notional != null) {
    return { main: orderMoney(order.notional), sub: tx("ord_notional", "Notional"), pct: null };
  }
  const main = orderQty(qty);
  if (filled > 0 && (qty == null || filled < Number(qty))) {
    const total = Number(qty);
    const pct =
      Number.isFinite(total) && total > 0
        ? Math.max(0, Math.min(100, (filled / total) * 100))
        : null;
    return {
      main,
      sub: tx("ord_filled_of", "{filled} filled", { filled: orderQty(filled) }),
      pct,
    };
  }
  return { main, sub: "", pct: null };
}

/** A partial fill is a proportion, and a proportion reads faster as a bar than
 *  as two numbers the eye has to divide. */
function fillProgressMarkup(qty) {
  if (qty.pct == null) return "";
  const pct = qty.pct.toFixed(1);
  const label = tx("ord_fill_progress", "{pct}% filled", { pct });
  return `<span class="ord-fill-track" role="img" aria-label="${escapeHtml(label)}" title="${escapeHtml(
    label
  )}"><span class="ord-fill-bar" style="width:${escapeHtml(pct)}%"></span></span>`;
}

/** How far the mark sits from the price this ticket is waiting for. A limit or
 *  stop on its own never says whether it is about to fire or parked a mile
 *  away, which is the one thing a resting blotter has to answer. */
function triggerDistanceMarkup(order) {
  const pct = Number(order.trigger_distance_pct);
  if (!Number.isFinite(pct)) return "";
  const away = Math.abs(pct);
  // Under a percent, the ticket is realistically in play this session.
  const tone = away < 1 ? " is-near" : "";
  const text = tx("ord_distance_away", "{pct}% away", { pct: away.toFixed(1) });
  const title = tx("ord_distance_hint", "Mark {price} · {pct}% from the trigger", {
    price: orderMoney(order.mark_price),
    pct: away.toFixed(1),
  });
  return `<small class="ord-distance${tone}" title="${escapeHtml(title)}">${escapeHtml(
    text
  )}</small>`;
}

/** The symbol's live mark, with the day's move beneath it — same $X.XX +
 *  change-pill stack the Positions blotter uses, so the two pages agree.
 *  Out of hours the tooltip says how old the print is, so a stale tick is
 *  never passed off as the current one. */
function markPriceMarkup(order) {
  const price = Number(order.mark_price);
  if (!Number.isFinite(price)) {
    return `<span class="ord-mark-empty" title="${escapeHtml(
      tx("ord_mark_unavailable", "No quote available for this symbol right now")
    )}">—</span>`;
  }
  const age = Number(order.mark_age_seconds);
  const title = Number.isFinite(age)
    ? tx("ord_mark_age", "Last price {age} ago", { age: formatAge(age) })
    : tx("current_price_label", "Current Price");
  const stale = order.mark_is_open === false ? " is-stale" : "";
  const change = Number(order.mark_change_pct);
  const changeBit = Number.isFinite(change)
    ? `<small class="pos-chg-pill ${change >= 0 ? "pos" : "neg"}">${escapeHtml(
        formatPnlPct(change)
      )}</small>`
    : "";
  return `<div class="ord-mark${stale}" title="${escapeHtml(title)}">$${price.toFixed(
    2
  )}</div>${changeBit}`;
}

function orderIsCanceling(order) {
  return (
    ordCancelingIds.has(String(order?.id || "")) ||
    String(order?.status || "").toLowerCase() === "pending_cancel"
  );
}

function orderCheckboxMarkup(order) {
  const id = String(order.id || "");
  const selectable = !!order.is_cancelable && !mutationsLocked() && ordFilterStatus === "open";
  const label = tx("ord_select_order", "Select the {symbol} order", {
    symbol: String(order.symbol || ""),
  });
  return `<input type="checkbox" class="pos-check-input ord-row-check" data-order-id="${escapeHtml(
    id
  )}" ${ordSelectedIds.has(id) ? "checked" : ""} ${
    selectable ? "" : "disabled"
  } aria-label="${escapeHtml(label)}" />`;
}

function orderDeskLinks(order) {
  return Array.isArray(order?.desk) ? order.desk : [];
}

function liveDeskLink(order) {
  return orderDeskLinks(order).find((link) => link.live) || null;
}

function deskBadge(order) {
  const link = liveDeskLink(order) || orderDeskLinks(order)[0];
  if (!link) return "";
  const live = !!link.live;
  const role = String(link.role || "");
  const queue = String(link.queue || "");
  let label = "";
  if (queue === "reinvest") {
    label =
      live && role === "trigger"
        ? tx("orders_buyback_armed", "Buy-back armed")
        : tx("orders_buyback_sent", "Buy-back");
  } else if (queue === "followon") {
    label =
      live && role === "trigger"
        ? tx("orders_next_ticket_armed", "Next ticket armed")
        : tx("orders_next_ticket_sent", "Next ticket");
  } else if (queue === "dip_hunt") {
    label =
      live && role !== "result"
        ? tx("orders_dip_hunt_armed", "Dip hunt armed")
        : tx("orders_dip_hunt_buy", "Dip buy");
  }
  if (!label) return "";
  const planId = String(link.plan_id || "");
  const isArmed = live && role === "trigger";
  const foldable = isArmed && planId;
  const pulseMarkup = isArmed ? `<span class="ord-plan-pulse" aria-hidden="true"></span>` : "";
  if (foldable) {
    const open = ordOpenPlanFolds.has(planId);
    return `<button type="button" class="ord-plan-badge ord-plan-fold-toggle" data-toggle-plan="${escapeHtml(
      planId
    )}" aria-expanded="${open ? "true" : "false"}" aria-controls="ord-fold-${escapeHtml(
      planId
    )}" title="${escapeHtml(
      tx("orders_plan_toggle", "Show or hide the plan waiting on this ticket")
    )}">${pulseMarkup}<span class="ord-plan-badge-text">${escapeHtml(
      label
    )}</span><span class="ord-plan-fold-caret" aria-hidden="true"></span></button>`;
  }
  return `<span class="ord-plan-badge">${pulseMarkup}<span class="ord-plan-badge-text">${escapeHtml(label)}</span></span>`;
}

function orderClassBadge(order) {
  const key = String(order.order_class || "").toLowerCase();
  if (!key || key === "simple") return "";
  const map = {
    oto: tx("order_class_oto", "OTO"),
    bracket: tx("order_class_bracket", "Bracket"),
    oco: tx("order_class_oco", "OCO"),
  };
  const label = map[key];
  if (!label) return "";
  return `<span class="ord-class-badge">${escapeHtml(label)}</span>`;
}

function orderEventTime(order) {
  const status = String(order.status || "").toLowerCase();
  if (status === "filled" && order.filled_at) return order.filled_at;
  if (["canceled", "cancelled"].includes(status) && order.canceled_at) {
    return order.canceled_at;
  }
  return order.submitted_at;
}

function orderMatchesFilters(o) {
  const q = String(ordFilterSearch || "").trim().toUpperCase();
  if (q && !String(o.symbol || "").includes(q)) return false;
  if (ordFilterSide !== "all" && String(o.side || "") !== ordFilterSide) return false;
  if (ordFilterKind === "working" && o.is_stop) return false;
  if (ordFilterKind === "conditional" && !o.is_stop) return false;
  if (ordFilterKind === "attached" && !orderDeskLinks(o).length) return false;
  if (ordFilterType !== "all" && String(o.type || "") !== ordFilterType) return false;
  return true;
}

/** The number a Price-sorted blotter should rank on: whatever the ticket is
 *  actually waiting for, falling back to what it filled at. */
function orderSortPrice(o) {
  const candidates = [o.limit_price, o.stop_price, o.filled_avg_price, o.notional];
  for (const value of candidates) {
    const n = Number(value);
    if (value != null && Number.isFinite(n)) return n;
  }
  return null;
}

function orderSortValue(o, key) {
  if (key === "submitted") return parseBtTime(orderEventTime(o));
  if (key === "symbol") return String(o.symbol || "");
  if (key === "side") return String(o.side || "");
  if (key === "type") return orderTypeLabel(o.type);
  if (key === "status") return statusLabel(o.status);
  if (key === "qty") {
    const qty = Number(o.qty);
    if (Number.isFinite(qty)) return qty;
    // A notional ticket has no share count; rank it by the dollars instead so
    // it lands somewhere meaningful rather than always at the bottom.
    const notional = Number(o.notional);
    return Number.isFinite(notional) ? notional : null;
  }
  if (key === "price") return orderSortPrice(o);
  if (key === "mark") {
    const n = Number(o.mark_price);
    return Number.isFinite(n) ? n : null;
  }
  if (key === "distance") {
    const pct = Number(o.trigger_distance_pct);
    return Number.isFinite(pct) ? Math.abs(pct) : null;
  }
  return null;
}

function sortOrders(rows) {
  const key = ORD_SORT_KEYS.has(ordSortKey) ? ordSortKey : "submitted";
  const dirMul = ordSortDir === "asc" ? 1 : -1;
  const isText = ORD_SORT_TEXT_KEYS.has(key);
  return rows.slice().sort((a, b) => {
    const va = orderSortValue(a, key);
    const vb = orderSortValue(b, key);
    if (isText) {
      const cmp = String(va || "").localeCompare(
        String(vb || ""),
        document.documentElement.lang || undefined
      );
      if (cmp !== 0) return cmp * dirMul;
    } else {
      // Rows with nothing to rank on sink to the bottom either way, so a
      // missing price never displaces a real one just by flipping direction.
      const aMissing = va == null || !Number.isFinite(Number(va));
      const bMissing = vb == null || !Number.isFinite(Number(vb));
      if (aMissing !== bMissing) return aMissing ? 1 : -1;
      if (!aMissing && Number(va) !== Number(vb)) {
        return (Number(va) - Number(vb)) * dirMul;
      }
    }
    // One stable tiebreak keeps rows from shuffling between polls.
    return String(a.id || "").localeCompare(String(b.id || ""));
  });
}

function filteredOrders() {
  const rows = Array.isArray(ordersPayload?.orders) ? ordersPayload.orders : [];
  return sortOrders(rows.filter(orderMatchesFilters));
}

function setOrdSort(key, { toggle = false } = {}) {
  if (!ORD_SORT_KEYS.has(key)) return;
  if (toggle && ordSortKey === key) {
    ordSortDir = ordSortDir === "asc" ? "desc" : "asc";
  } else {
    ordSortKey = key;
    ordSortDir = ORD_SORT_TEXT_KEYS.has(key) ? "asc" : "desc";
  }
  try {
    localStorage.setItem("desk_ord_sort_key", ordSortKey);
    localStorage.setItem("desk_ord_sort_dir", ordSortDir);
  } catch (e) {}
  writeOrdQuery();
  renderOrdersPage();
}

function updateOrdSortUi() {
  document.querySelectorAll(".pos-th-sortable").forEach((th) => {
    const col = th.dataset.sortCol;
    const icon = th.querySelector(".pos-sort-icon");
    if (col === ordSortKey) {
      const isAsc = ordSortDir === "asc";
      th.setAttribute("aria-sort", isAsc ? "ascending" : "descending");
      if (icon) icon.textContent = isAsc ? "▲" : "▼";
    } else {
      th.setAttribute("aria-sort", "none");
      if (icon) icon.textContent = "↕";
    }
  });
  const select = $("ord-sort-select");
  if (select && select.value !== ordSortKey) {
    select.value = ordSortKey;
    refreshNiceSelect(select);
  }
}

function filtersActive() {
  return (
    !!String(ordFilterSearch || "").trim() ||
    ordFilterSide !== "all" ||
    ordFilterKind !== "all" ||
    ordFilterType !== "all" ||
    !!ordFilterAfter ||
    !!ordFilterUntil
  );
}

function syncSeg(attr, value) {
  document.querySelectorAll(`[${attr}]`).forEach((btn) => {
    const on = btn.getAttribute(attr) === value;
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function syncOrdFiltersUi() {
  const search = $("ord-search");
  const clear = $("btn-clear-ord-search");
  if (search && search.value !== ordFilterSearch) search.value = ordFilterSearch;
  if (clear) clear.hidden = !String(ordFilterSearch || "").trim();
  syncSeg("data-filter-status", ordFilterStatus);
  syncSeg("data-filter-side", ordFilterSide);
  syncSeg("data-filter-kind", ordFilterKind);
  syncSeg("data-kpi-kind", ordFilterKind);
  const typeSel = $("ord-type-select");
  if (typeSel) {
    typeSel.value = ordFilterType;
    refreshNiceSelect(typeSel);
  }
  const window = $("ord-window");
  if (window) window.hidden = ordFilterStatus !== "closed";
  const after = $("ord-window-after");
  if (after && after.value !== ordFilterAfter) after.value = ordFilterAfter;
  const until = $("ord-window-until");
  if (until && until.value !== ordFilterUntil) until.value = ordFilterUntil;
  const reset = $("btn-reset-ord-filters");
  if (reset) reset.disabled = !filtersActive();
  const badge = $("ord-filters-active-count");
  if (badge) {
    const count =
      (String(ordFilterSearch || "").trim() ? 1 : 0) +
      (ordFilterStatus !== "open" ? 1 : 0) +
      (ordFilterSide !== "all" ? 1 : 0) +
      (ordFilterKind !== "all" ? 1 : 0) +
      (ordFilterType !== "all" ? 1 : 0) +
      (ordFilterAfter || ordFilterUntil ? 1 : 0);
    badge.textContent = String(count);
    badge.hidden = count === 0;
  }
  updateOrdSortUi();
}

function toggleOrdFiltersBar() {
  const bar = $("ord-filters-bar");
  const btn = $("btn-toggle-ord-filters");
  if (!bar || !btn) return;
  const open = !bar.classList.contains("is-open");
  bar.classList.toggle("is-open", open);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
}

function resetOrdFilters() {
  const hadWindow = !!(ordFilterAfter || ordFilterUntil);
  ordFilterSearch = "";
  ordFilterSide = "all";
  ordFilterKind = "all";
  ordFilterType = "all";
  ordFilterAfter = "";
  ordFilterUntil = "";
  const hadScope = !!ordSymbolScope;
  ordSymbolScope = "";
  writeOrdQuery();
  syncOrdFiltersUi();
  // Both the window and the symbol escalation are server-side bounds, so
  // dropping either needs fresh rows.
  if (hadWindow || hadScope) {
    refreshOrders().catch(() => {});
    return;
  }
  renderOrdersPage();
}

function orderActions(order, variant = "table") {
  const locked = mutationsLocked();
  const cancelable = !!order.is_cancelable && !locked;
  const replaceable = !!order.is_replaceable && !locked;
  const sym = encodeURIComponent(order.symbol || "");
  const cancelTitle = locked
    ? tx("ord_loop_locked_short", "Stop the Auto Trade loop to manage orders")
    : tx("cancel", "Cancel");
  const editTitle = editOrderTitle(order, locked);
  // Table actions share Positions' quiet text buttons; cards keep the outlined
  // ghost chips the Positions cards already use.
  const isCard = variant === "card";
  const actClass = isCard ? "ghost" : "pos-act";
  const cancelClass = isCard ? "ghost ghost-danger" : "pos-act pos-act-close";
  const bits = [];
  if (ordFilterStatus === "open") {
    bits.push(
      `<button type="button" class="${cancelClass} ord-act-cancel" data-order-id="${escapeHtml(
        order.id || ""
      )}" ${cancelable ? "" : "disabled"} title="${escapeHtml(cancelTitle)}">${escapeHtml(
        tx("cancel", "Cancel")
      )}</button>`
    );
    bits.push(
      `<button type="button" class="${actClass} ord-act-replace" data-order-id="${escapeHtml(
        order.id || ""
      )}" ${replaceable ? "" : "disabled"} title="${escapeHtml(editTitle)}">${escapeHtml(
        tx("edit_order", "Edit")
      )}</button>`
    );
  }
  bits.push(
    `<button type="button" class="${actClass} ord-act-detail" data-order-id="${escapeHtml(
      order.id || ""
    )}" title="${escapeHtml(
      tx("ord_details_hint", "Order id, client id, timestamps, and routing")
    )}">${escapeHtml(tx("ord_details", "Details"))}</button>`
  );
  bits.push(
    `<a class="${actClass}" href="${pagePath("positions")}?symbol=${sym}" title="${escapeHtml(
      tx("view_position", "View position")
    )}">${escapeHtml(tx("nav_positions", "Positions"))}</a>`
  );
  return bits.join("");
}

function orderSymbolLink(order) {
  const sym = String(order?.symbol || "");
  return `<a href="${pagePath("manual-order")}?symbol=${encodeURIComponent(
    sym
  )}" class="pos-sym-link" title="${escapeHtml(
    tx("trade_symbol", "Trade this symbol")
  )}">${escapeHtml(sym)}</a>`;
}

function editOrderTitle(order, locked) {
  if (locked) {
    return tx("ord_loop_locked_short", "Stop the Auto Trade loop to manage orders");
  }
  if (order.is_replaceable) {
    return tx("edit_order_hint", "Edit this order's quantity, price, trail, or time in force");
  }
  if (order.notional != null) {
    return tx(
      "edit_order_notional_locked",
      "Notional orders cannot be edited; cancel and submit a new order"
    );
  }
  const status = String(order.status || "").toLowerCase();
  if (["pending_cancel", "pending_replace"].includes(status)) {
    return tx(
      "edit_order_pending_locked",
      "This order is pending at the broker. Refresh and edit it once it is working."
    );
  }
  if (String(order.type || "").toLowerCase() === "market") {
    return tx(
      "edit_order_market_locked",
      "Market orders cannot be edited; cancel and submit a new order"
    );
  }
  return tx(
    "edit_order_unavailable",
    "This order can no longer be edited. Cancel and submit a new order if needed."
  );
}

function renderOrderRow(order) {
  const side = String(order.side || "").toLowerCase();
  const qty = qtyText(order);
  const prices = orderPriceText(order);
  const kind = order.is_stop
    ? `<span class="ord-kind-badge">${escapeHtml(tx("orders_conditional", "Conditional"))}</span>`
    : "";
  const desk = deskBadge(order);
  const klass = orderClassBadge(order);
  const canceling = orderIsCanceling(order);
  const selected = ordSelectedIds.has(String(order.id || ""));
  return `<tr class="pos-table-row${canceling ? " is-canceling" : ""}${
    selected ? " is-selected" : ""
  }" data-order-id="${escapeHtml(order.id || "")}" data-side="${escapeHtml(side)}">
    <td class="pos-cell-check">${orderCheckboxMarkup(order)}</td>
    <td>
      <div class="ord-sym-cell">
        <strong title="${escapeHtml(order.id || "")}">${orderSymbolLink(order)}</strong>
        ${kind}${desk}${klass}
      </div>
    </td>
    <td><span class="side-badge ${escapeHtml(side)}">${escapeHtml(
      side === "sell" ? tx("sell", "Sell") : tx("buy", "Buy")
    )}</span></td>
    <td>${escapeHtml(orderTypeLabel(order.type))}</td>
    <td class="pos-num mono">
      <div class="ord-qty-stack">
        <strong>${escapeHtml(qty.main)}</strong>
        ${qty.sub ? `<small>${escapeHtml(qty.sub)}</small>` : ""}
        ${fillProgressMarkup(qty)}
      </div>
    </td>
    <td class="pos-num mono">
      <div class="ord-price-stack">
        ${
          prices.length
            ? prices.map((p) => `<span>${escapeHtml(p)}</span>`).join("")
            : "—"
        }
      </div>
    </td>
    <td class="pos-cell-price pos-num mono">
      <div class="ord-price-stack">
        ${markPriceMarkup(order)}
        ${triggerDistanceMarkup(order)}
      </div>
    </td>
    <td>${escapeHtml(orderTifLabel(order))}</td>
    <td><span class="ord-status ${canceling ? "is-open" : statusClass(order.status)}">${escapeHtml(
      canceling ? tx("ord_status_pending_cancel", "Canceling") : statusLabel(order.status)
    )}</span></td>
    <td class="pos-cell-actions"><div class="pos-action-row">${orderActions(order)}</div></td>
  </tr>${renderOrderPlanRows(order)}`;
}

function renderOrderCard(order) {
  const side = String(order.side || "").toLowerCase();
  const qty = qtyText(order);
  const prices = orderPriceText(order);
  // The card is the table row at narrow widths, so it carries the same three
  // signals: the status colour, the Conditional mark, and the desk badges.
  const kind = order.is_stop
    ? `<span class="ord-kind-badge">${escapeHtml(tx("orders_conditional", "Conditional"))}</span>`
    : "";
  const desk = deskBadge(order);
  const klass = orderClassBadge(order);
  const canceling = orderIsCanceling(order);
  const selected = ordSelectedIds.has(String(order.id || ""));
  const actionsOpen = ordExpandedCardActions.has(String(order.id || ""));
  const eventTime = orderEventTime(order);

  return `<div class="pos-card${canceling ? " is-canceling" : ""}${
    selected ? " is-selected" : ""
  }" role="listitem" data-order-id="${escapeHtml(order.id || "")}">
    <div class="pos-card-head">
      <div class="pos-card-sym-wrap">
        ${orderCheckboxMarkup(order)}
        <strong class="pos-card-sym" title="${escapeHtml(order.id || "")}">${orderSymbolLink(order)}</strong>
        ${kind}
        ${desk}
        ${klass}
        <span class="side-badge ${escapeHtml(side)}">${escapeHtml(
          side === "sell" ? tx("sell", "Sell") : tx("buy", "Buy")
        )}</span>
      </div>
      <div class="pos-card-mv">
        <span class="ord-status ${canceling ? "is-open" : statusClass(order.status)}">${escapeHtml(
          canceling ? tx("ord_status_pending_cancel", "Canceling") : statusLabel(order.status)
        )}</span>
      </div>
    </div>
    <div class="pos-card-grid mono">
      <div>
        <span>${escapeHtml(tx("order_type", "Order type"))}</span>
        <strong>${escapeHtml(orderTypeLabel(order.type))} · ${escapeHtml(orderTifLabel(order))}</strong>
      </div>
      <div>
        <span>${escapeHtml(tx("shares_qty", "Shares / Qty"))}</span>
        <strong>${escapeHtml(qty.main)}${qty.sub ? ` <small class="ord-card-qty-sub">${escapeHtml(qty.sub)}</small>` : ""}</strong>
        ${fillProgressMarkup(qty)}
      </div>
      <div>
        <span>${escapeHtml(tx("order_price", "Price"))}</span>
        <strong>${prices.length ? escapeHtml(prices.join(" · ")) : "—"}</strong>
      </div>
      <div>
        <span>${escapeHtml(tx("current_price_label", "Current Price"))}</span>
        <div class="pos-kpi-valrow">
          ${markPriceMarkup(order)}
          ${triggerDistanceMarkup(order)}
        </div>
      </div>
      <div>
        <span>${escapeHtml(tx("submitted_at", "Submitted"))}</span>
        <span class="ord-time">${escapeHtml(ordTime(eventTime))}</span>
      </div>
      <div>
        <span>${escapeHtml(tx("status", "Status"))}</span>
        <span class="ord-status ${canceling ? "is-open" : statusClass(order.status)}">${escapeHtml(
          canceling ? tx("ord_status_pending_cancel", "Canceling") : statusLabel(order.status)
        )}</span>
      </div>
    </div>
    <button type="button" class="pos-card-actions-toggle" aria-expanded="${actionsOpen ? "true" : "false"}">
      <span>${escapeHtml(tx("actions", "Actions"))}</span>
      <svg class="pos-card-actions-chevron" viewBox="0 0 24 24" width="12" height="12" aria-hidden="true" focusable="false">
        <path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M6 9l6 6 6-6"/>
      </svg>
    </button>
    <div class="pos-card-actions" ${actionsOpen ? "" : "hidden"}>
      ${orderActions(order, "card")}
    </div>
    ${renderOrderPlanCards(order)}
  </div>`;
}

function renderKpis() {
  const data = ordersPayload || {};
  const set = (id, value) => {
    const el = $(id);
    if (el) el.textContent = value == null ? "—" : String(value);
  };
  const openCount = data.open_count ?? 0;
  const openDisplay = data.open_count_limited ? `≥${openCount}` : String(openCount);
  set("ord-kpi-open", openDisplay);
  set("ord-kpi-working", data.working_count ?? 0);
  set("ord-kpi-conditional", data.conditional_count ?? 0);
  set("ord-kpi-partial", data.partial_count ?? 0);
  set("ord-kpi-armed", data.desk_plans?.armed_count ?? 0);
  const armedTile = $("ord-kpi-armed-tile");
  if (armedTile) {
    const armed = Number(data.desk_plans?.armed_count || 0);
    armedTile.classList.toggle("is-interactive", armed > 0);
    if (armed > 0) {
      armedTile.setAttribute("role", "button");
      armedTile.setAttribute("tabindex", "0");
      armedTile.title = tx("orders_armed_jump", "Show the armed desk plans");
    } else {
      armedTile.removeAttribute("role");
      armedTile.removeAttribute("tabindex");
      armedTile.removeAttribute("title");
    }
  }
  const sub = $("ord-kpi-open-sub");
  if (sub) {
    // The count already sits beside this line, so the phrase carries no
    // second copy of it.
    sub.textContent = tx("orders_open_sub", "resting at the broker");
  }

  const committed = Number(data.open_value);
  const committedEl = $("ord-kpi-committed");
  if (committedEl) {
    committedEl.textContent = Number.isFinite(committed)
      ? `${data.open_value_partial ? "≥" : ""}${orderMoney(committed)}`
      : "—";
  }
  const committedSub = $("ord-kpi-committed-sub");
  if (committedSub) {
    const buy = Number(data.open_value_buy || 0);
    const sell = Number(data.open_value_sell || 0);
    committedSub.textContent =
      buy > 0 || sell > 0
        ? `${tx("buy", "Buy")} ${orderMoney(buy)} · ${tx("sell", "Sell")} ${orderMoney(sell)}`
        : tx("ord_committed_hint", "Value of the resting book");
  }

  const near = data.nearest_trigger || null;
  const nearEl = $("ord-kpi-nearest");
  const nearSub = $("ord-kpi-nearest-sub");
  const nearTile = $("ord-kpi-nearest-tile");
  if (nearEl) {
    nearEl.textContent = near
      ? tx("ord_distance_away", "{pct}% away", {
          pct: Number(near.distance_pct).toFixed(1),
        })
      : "—";
  }
  if (nearSub) {
    nearSub.textContent = near
      ? `${near.symbol || ""} · ${orderTypeLabel(near.type)} ${
          near.trigger_price != null ? orderMoney(near.trigger_price) : ""
        }`.trim()
      : tx("ord_nearest_trigger_hint", "Closest resting ticket");
  }
  if (nearTile) {
    // Under a percent this is the thing to look at, so the tile picks up the
    // same copper the near-distance rows use.
    nearTile.classList.toggle(
      "is-hot",
      !!near && Number(near.distance_pct) < 1
    );
    const jumpable = !!near?.order_id;
    nearTile.classList.toggle("is-interactive", jumpable);
    if (jumpable) {
      nearTile.setAttribute("role", "button");
      nearTile.setAttribute("tabindex", "0");
      nearTile.title = tx("ord_nearest_trigger_jump", "Show this ticket on the blotter");
    } else {
      nearTile.removeAttribute("role");
      nearTile.removeAttribute("tabindex");
      nearTile.removeAttribute("title");
    }
  }
  const badge = $("ord-mode-badge");
  if (badge) {
    const env = data.trading_mode || (isLiveEnv() ? "live" : "paper");
    if (env === "live") {
      badge.className = "mode-badge env-live";
      badge.dataset.i18n = "live_armed";
      badge.textContent = tx("live_armed", "Live · Orders on");
    } else {
      badge.className = "mode-badge armed";
      badge.dataset.i18n = "paper_trading";
      badge.textContent = tx("paper_trading", "Paper trading");
    }
  }
}

function renderEmpty(rows) {
  const empty = $("ord-empty-state");
  const table = $("ord-table-wrap");
  const cards = $("ord-cards-list");
  const title = $("ord-empty-title");
  const desc = $("ord-empty-desc");
  const actions = $("ord-empty-actions");
  const clear = $("btn-empty-clear-ord-filters");
  const raw = Array.isArray(ordersPayload?.orders) ? ordersPayload.orders : [];
  const filteredOut = raw.length > 0 && rows.length === 0;
  if (rows.length) {
    if (empty) empty.hidden = true;
    if (table) table.hidden = false;
    if (cards) cards.hidden = false;
    return;
  }
  if (table) table.hidden = true;
  if (cards) cards.hidden = true;
  if (empty) empty.hidden = false;
  if (clear) clear.hidden = !filteredOut;
  if (actions) actions.hidden = filteredOut;
  if (title) {
    title.textContent = filteredOut
      ? tx("no_matching_orders", "No matching orders")
      : ordFilterStatus === "closed"
        ? tx("no_closed_orders", "No recent closed orders")
        : tx("no_open_orders", "No open orders");
  }
  if (desc) {
    desc.textContent = filteredOut
      ? tx("no_matching_orders_desc", "Nothing matches the current search or filters.")
      : ordFilterStatus === "closed"
        ? tx("no_closed_orders_desc", "Canceled, expired, rejected, and filled tickets from the latest window will show here.")
        : tx("no_open_orders_desc", "Nothing is resting at the broker. Buy-backs and next tickets wait in Desk queues above until they fire. Place a ticket on Advanced Order, or switch to Closed to see recent fills and cancels.");
  }
}

function deskPlans() {
  return ordersPayload?.desk_plans || {};
}

function armedDeskCount() {
  return Number(deskPlans().armed_count || 0);
}

function planTouchesBlotter(plan, ids) {
  return [
    "sell_order_id",
    "buy_order_id",
    "close_order_id",
    "next_order_id",
    "stop_order_id",
    "dip_buy_order_id",
  ].some((key) => plan[key] && ids.has(plan[key]));
}

function planMatchesSearch(plan) {
  const q = String(ordFilterSearch || "").trim().toUpperCase();
  if (!q) return true;
  return [plan.symbol, plan.target_symbol]
    .filter(Boolean)
    .some((sym) => String(sym).toUpperCase().includes(q));
}

/** The side a plan will eventually send. Buy-backs and dip hunts always buy; a
 *  next ticket carries its own side, where a short entry sells. */
function planSide(queue, plan) {
  if (queue !== "followon") return "buy";
  const next = String(plan.next_side || "buy").toLowerCase();
  return next === "short" || next === "sell" ? "sell" : "buy";
}

function planOrderType(plan) {
  return String(plan.order_type || "limit").toLowerCase() === "market"
    ? "market"
    : "limit";
}

/** Desk plans answer the same filter bar the blotter does. Leaving them
 *  unfiltered made "Side: Sell" still show buy-back plans, which reads as the
 *  filter being broken rather than as the queues being exempt. */
function planMatchesFilters(queue, plan) {
  if (!planMatchesSearch(plan)) return false;
  if (ordFilterSide !== "all" && planSide(queue, plan) !== ordFilterSide) return false;
  // A plan is never a resting stop, and it is always attached to a ticket.
  if (ordFilterKind === "conditional") return false;
  if (ordFilterType !== "all" && planOrderType(plan) !== ordFilterType) return false;
  return true;
}

function visibleDeskPlans(queue, plans, liveSet) {
  const ids = new Set((ordersPayload?.orders || []).map((o) => o.id));
  return (Array.isArray(plans) ? plans : []).filter((plan) => {
    if (!planMatchesFilters(queue, plan)) return false;
    if (liveSet.has(String(plan.status || "").toLowerCase())) return true;
    return planTouchesBlotter(plan, ids);
  });
}

function planRelatedOrderId(queue, plan) {
  if (queue === "reinvest") return plan.buy_order_id || plan.sell_order_id || "";
  if (queue === "followon") return plan.next_order_id || plan.close_order_id || "";
  return plan.dip_buy_order_id || plan.stop_order_id || plan.buy_order_id || "";
}

function formatPlanQty(plan, keys) {
  for (const key of keys) {
    if (plan[key] != null && plan[key] !== "") return orderQty(plan[key]);
  }
  return "—";
}

function formatReinvestPlan(plan) {
  const status = String(plan?.status || "").toLowerCase();
  const qty = formatPlanQty(plan, ["buy_qty", "qty", "sell_qty"]);
  const priceLabel = orderMoney(plan.limit_price);
  const head = `${plan.symbol || "—"} · ${qty} @ ${priceLabel}`;
  const base = {
    side: "buy",
    symbol: plan.symbol || "—",
    qty,
    orderType: "limit",
    priceLabel,
    head,
  };
  if (status === "waiting") {
    const started = plan.wait_started === true || plan.seconds_left != null;
    if (!started) {
      return {
        ...base,
        kind: "waiting",
        note: tx("reinvest_state_waiting", "Waiting for the sell to fill"),
        cancellable: true,
      };
    }
    const mins = Math.max(0, Math.ceil(Number(plan.seconds_left || 0) / 60));
    return {
      ...base,
      kind: "waiting",
      note: tx(
        "reinvest_state_wait_after_fill",
        "Sell filled · buy-back resting · {mins}m left",
        { mins: String(mins) }
      ),
      cancellable: true,
    };
  }
  if (status === "awaiting_fill") {
    const mins = Math.max(0, Math.ceil(Number(plan.seconds_left || 0) / 60));
    return {
      ...base,
      kind: "waiting",
      note: tx(
        "reinvest_state_wait_after_fill",
        "Sell filled · buy-back resting · {mins}m left",
        { mins: String(mins) }
      ),
      cancellable: true,
    };
  }
  if (status === "placing") {
    return {
      ...base,
      kind: "waiting",
      note: tx("reinvest_state_placing", "The sell filled — sending the buy-back…"),
      cancellable: false,
    };
  }
  if (status === "placed") {
    return {
      ...base,
      kind: "ok",
      note: tx("reinvest_state_placed", "Buy-back filled"),
      cancellable: false,
    };
  }
  if (status === "expired") {
    return {
      ...base,
      kind: "warn",
      note: tx("reinvest_state_expired", "Expired — the buy-back did not fill in time"),
      cancellable: false,
    };
  }
  if (status === "interrupted") {
    return {
      ...base,
      kind: "error",
      note:
        plan.message ||
        tx(
          "reinvest_state_interrupted",
          "The desk restarted while this was being sent — check Positions."
        ),
      cancellable: false,
    };
  }
  if (status === "failed") {
    return {
      ...base,
      kind: "error",
      note: plan.message || tx("reinvest_state_failed", "Buy-back failed"),
      cancellable: false,
    };
  }
  return {
    ...base,
    kind: "warn",
    note: plan.message || tx("reinvest_state_cancelled", "Cancelled"),
    cancellable: false,
  };
}

function formatFollowOnPlan(plan) {
  const status = String(plan?.status || "").toLowerCase();
  const nextSide = String(plan.next_side || "buy").toLowerCase();
  const target = String(plan.target_symbol || plan.symbol || "");
  const qty = formatPlanQty(plan, ["next_qty", "qty", "close_qty"]);
  const sideLabel =
    nextSide === "short" ? tx("short_side", "Short") : tx("buy", "Buy");
  const isMarket = String(plan.order_type || "limit").toLowerCase() === "market";
  const priceLabel = isMarket ? tx("market", "Market") : orderMoney(plan.limit_price);
  const head = `${sideLabel} ${target} · ${qty} @ ${priceLabel}`;
  const base = {
    side: nextSide,
    symbol: target,
    qty,
    orderType: isMarket ? "market" : "limit",
    priceLabel,
    head,
  };
  if (status === "waiting") {
    return {
      ...base,
      kind: "waiting",
      note: tx("followon_state_waiting", "Waiting for the close to fill"),
      cancellable: true,
    };
  }
  if (status === "placing") {
    return {
      ...base,
      kind: "waiting",
      note: tx("followon_state_placing", "The close filled — sending the next ticket…"),
      cancellable: false,
    };
  }
  if (status === "placed") {
    return {
      ...base,
      kind: "ok",
      note: tx("followon_state_placed", "Next ticket sent"),
      cancellable: false,
    };
  }
  if (status === "expired") {
    return {
      ...base,
      kind: "error",
      note: tx("followon_state_expired", "Expired — the next ticket was not sent in time"),
      cancellable: false,
    };
  }
  if (status === "interrupted") {
    return {
      ...base,
      kind: "error",
      note:
        plan.message ||
        tx(
          "reinvest_state_interrupted",
          "The desk restarted while this was being sent — check Positions."
        ),
      cancellable: false,
    };
  }
  if (status === "failed") {
    return {
      ...base,
      kind: "error",
      note: plan.message || tx("followon_state_failed", "Next ticket failed"),
      cancellable: false,
    };
  }
  return {
    ...base,
    kind: "error",
    note: plan.message || tx("followon_state_cancelled", "Cancelled"),
    cancellable: false,
  };
}

function formatDipHuntPlan(plan) {
  const status = String(plan?.status || "").toLowerCase();
  const cycle =
    Number(plan.cycle) > 1
      ? ` · ${tx("dip_hunt_cycle", "cycle {n}", { n: String(plan.cycle) })}`
      : "";
  const head = `${plan.symbol || "—"}${cycle} · ${tx("dip_hunt_head", "{wait}m / {dip}%", {
    wait: String(plan.wait_minutes ?? "—"),
    dip: String(plan.dip_pct ?? "—"),
  })}`;
  const cancellable = DIP_HUNT_CANCELLABLE.has(status);
  const target = plan.target_price != null ? orderMoney(plan.target_price) : "—";
  const base = {
    side: "buy",
    symbol: (plan.symbol || "—") + cycle,
    qty: formatPlanQty(plan, ["buy_qty", "qty"]),
    orderType: "limit",
    priceLabel: target !== "—" ? target : `${plan.dip_pct ?? "—"}% dip`,
    head,
  };
  if (status === "watching_entry") {
    return {
      ...base,
      kind: "waiting",
      note: tx("dip_hunt_state_watching_entry", "Waiting for the buy to fill"),
      cancellable,
    };
  }
  if (status === "watching_stop") {
    return {
      ...base,
      kind: "waiting",
      note: tx("dip_hunt_state_watching_stop", "Watching the protective stop"),
      cancellable,
    };
  }
  if (status === "hunting") {
    const mins = Math.ceil(Number(plan.seconds_left || 0) / 60);
    return {
      ...base,
      kind: "waiting",
      note:
        mins > 0
          ? tx("dip_hunt_state_hunting", "Hunting {price} · {mins}m of the wait left", {
              price: target,
              mins: String(mins),
            })
          : tx("dip_hunt_state_hunting_ready", "Wait ended — limit parked at {price} when sent", {
              price: target,
            }),
      cancellable,
    };
  }
  if (status === "placing") {
    return {
      ...base,
      kind: "waiting",
      note: tx("dip_hunt_state_placing", "Sending the cheaper buy…"),
      cancellable: false,
    };
  }
  if (status === "awaiting_fill") {
    return {
      ...base,
      kind: "waiting",
      note: tx("dip_hunt_state_awaiting_fill", "Dip buy resting at {price}", {
        price: target,
      }),
      cancellable,
    };
  }
  if (status === "expired") {
    return {
      ...base,
      kind: "warn",
      note: plan.message || tx("dip_hunt_state_expired", "Expired — the drop never printed"),
      cancellable: false,
    };
  }
  if (status === "interrupted") {
    return {
      ...base,
      kind: "error",
      note:
        plan.message ||
        tx(
          "dip_hunt_state_interrupted",
          "The desk restarted while this was being sent — check Positions."
        ),
      cancellable,
    };
  }
  if (status === "failed") {
    return {
      ...base,
      kind: "error",
      note: plan.message || tx("dip_hunt_state_failed", "Dip buy failed"),
      cancellable: false,
    };
  }
  return {
    ...base,
    kind: "warn",
    note: plan.message || tx("dip_hunt_state_cancelled", "Cancelled"),
    cancellable: false,
  };
}

function planFormatter(queue) {
  if (queue === "reinvest") return formatReinvestPlan;
  if (queue === "followon") return formatFollowOnPlan;
  return formatDipHuntPlan;
}

function queueTitle(queue) {
  if (queue === "reinvest") return tx("reinvest_queue", "Re-investment queue");
  if (queue === "followon") return tx("followon_queue", "Next-ticket queue");
  return tx("dip_hunt_queue", "Dip-hunt queue");
}

function planTriggerOrderId(queue, plan) {
  if (queue === "reinvest") return String(plan.sell_order_id || "");
  if (queue === "followon") return String(plan.close_order_id || "");
  const status = String(plan?.status || "").toLowerCase();
  if (status === "awaiting_fill" || status === "placing") {
    return String(plan.dip_buy_order_id || plan.buy_order_id || "");
  }
  if (status === "watching_stop" || status === "hunting") {
    return String(plan.stop_order_id || plan.buy_order_id || "");
  }
  return String(plan.buy_order_id || plan.stop_order_id || "");
}

function attachedPlansForOrder(order) {
  const id = String(order?.id || "");
  if (!id) return [];
  const desk = deskPlans();
  const attached = [];
  const groups = [
    ["followon", visibleDeskPlans("followon", desk.followon, FOLLOWON_LIVE)],
    ["reinvest", visibleDeskPlans("reinvest", desk.reinvest, REINVEST_LIVE)],
    ["dip_hunt", visibleDeskPlans("dip_hunt", desk.dip_hunt, DIP_HUNT_LIVE)],
  ];
  // Only a plan the row actually shows a fold toggle for may be folded away —
  // otherwise it collapses with no control left to bring it back.
  const foldableIds = new Set(
    orderDeskLinks(order)
      .filter((link) => link.live && link.role === "trigger" && link.plan_id)
      .map((link) => String(link.plan_id))
  );
  groups.forEach(([queue, plans]) => {
    plans.forEach((plan) => {
      if (planTriggerOrderId(queue, plan) === id) {
        attached.push({ queue, plan, foldable: foldableIds.has(String(plan.id || "")) });
      }
    });
  });
  return attached;
}

function renderPlanItem(queue, plan, { inline = false, foldable = false } = {}) {
  const view = planFormatter(queue)(plan);
  const related = planRelatedOrderId(queue, plan);
  const cancelAttr =
    queue === "reinvest"
      ? "data-cancel-reinvest"
      : queue === "followon"
        ? "data-cancel-followon"
        : "data-cancel-dip-hunt";
  const locked = mutationsLocked();
  const actions = [];
  if (!inline && related) {
    actions.push(
      `<button type="button" class="ghost ord-plan-btn-jump" data-jump-order="${escapeHtml(
        related
      )}"><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 3H14V7M14 3L7 10M3 7V13H9"></path></svg>${escapeHtml(tx("ord_jump_related", "Show on blotter"))}</button>`
    );
  }
  if (view.cancellable) {
    actions.push(
      `<button type="button" class="ghost ghost-danger ord-plan-btn-cancel" ${cancelAttr}="${escapeHtml(
        String(plan.id || "")
      )}" ${locked ? "disabled" : ""}><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="4" x2="4" y2="12"></line><line x1="4" y1="4" x2="12" y2="12"></line></svg>${escapeHtml(tx("cancel", "Cancel"))}</button>`
    );
  }

  const queueKey = queue === "followon" ? "followon" : queue === "reinvest" ? "reinvest" : "dip_hunt";
  const side = view.side ? String(view.side).toLowerCase() : "buy";
  const sideLabel = side === "short" || side === "sell"
    ? (side === "short" ? tx("short_side", "Short") : tx("sell", "Sell"))
    : tx("buy", "Buy");

  let statusIconSvg = "";
  if (view.kind === "waiting") {
    statusIconSvg = `<span class="ord-plan-pulse-dot" aria-hidden="true"></span>`;
  } else if (view.kind === "ok") {
    statusIconSvg = `<svg class="ord-plan-icon-ok" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 9 7 13 13 4"></polyline></svg>`;
  } else {
    statusIconSvg = `<svg class="ord-plan-icon-err" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="8" r="6"></circle><line x1="8" y1="5" x2="8" y2="8"></line><line x1="8" y1="11" x2="8.01" y2="11"></line></svg>`;
  }

  const chipContent = view.symbol
    ? `<span class="ord-plan-queue-tag tag-${escapeHtml(queueKey)}">
        <svg class="ord-plan-chain-icon" viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M6.5 9.5l3-3m-1-2.5l1.5-1.5a2.121 2.121 0 0 1 3 3L11.5 7a2.121 2.121 0 0 1-3 0M7.5 12l-1.5 1.5a2.121 2.121 0 0 1-3-3L4.5 9a2.121 2.121 0 0 1 3 0"></path>
        </svg>
        ${escapeHtml(queueTitle(queue))}
      </span>
      <span class="side-badge ${escapeHtml(side)}">${escapeHtml(sideLabel)}</span>
      <strong class="ord-plan-sym-text">${escapeHtml(view.symbol)}</strong>
      <span class="ord-plan-dot-sep">·</span>
      <span class="ord-plan-spec-val">${escapeHtml(view.qty || "—")}</span>
      <span class="ord-plan-spec-muted">@</span>
      <span class="ord-plan-spec-val ${view.priceLabel === tx("market", "Market") ? "is-market" : ""}">${escapeHtml(view.priceLabel || "—")}</span>`
    : `<span class="ord-queue-head">${escapeHtml(view.head)}</span>`;

  const body = `<div class="ord-queue-item${inline ? " ord-inline-plan" : ""}" data-kind="${escapeHtml(
    view.kind
  )}" data-plan-id="${escapeHtml(String(plan.id || ""))}">
        <div class="ord-queue-row">
          <div class="ord-plan-chips">${chipContent}</div>
          <div class="ord-queue-actions">${actions.join("")}</div>
        </div>
        <div class="ord-plan-status-row">
          <div class="ord-plan-status-pill kind-${escapeHtml(view.kind)}">
            ${statusIconSvg}
            <span class="ord-plan-note-text">${escapeHtml(view.note)}</span>
          </div>
        </div>
      </div>`;
  const planId = String(plan.id || "");
  if (!inline || !foldable || !planId) return body;
  const open = ordOpenPlanFolds.has(planId) ? " open" : "";
  return `<details class="ord-plan-fold ord-plan-fold-bound" id="ord-fold-${escapeHtml(
    planId
  )}" data-plan-fold="${escapeHtml(planId)}"${open}>
    <summary class="ord-plan-fold-head sr-only">${escapeHtml(queueTitle(queue))}</summary>
    ${body}
  </details>`;
}

function renderOrderPlanRows(order) {
  const attached = attachedPlansForOrder(order);
  if (!attached.length) return "";
  return attached
    .map(
      ({ queue, plan, foldable }) =>
        `<tr class="ord-plan-row" data-order-id="${escapeHtml(order.id || "")}">
      <td colspan="10">${renderPlanItem(queue, plan, { inline: true, foldable })}</td>
    </tr>`
    )
    .join("");
}

function renderOrderPlanCards(order) {
  const attached = attachedPlansForOrder(order);
  if (!attached.length) return "";
  return `<div class="ord-card-plans">${attached
    .map(({ queue, plan, foldable }) =>
      renderPlanItem(queue, plan, { inline: true, foldable })
    )
    .join("")}</div>`;
}

function standaloneDeskPlans(queue, plans, liveSet) {
  const visible = new Set(filteredOrders().map((o) => o.id));
  return visibleDeskPlans(queue, plans, liveSet).filter(
    (plan) => !visible.has(planTriggerOrderId(queue, plan))
  );
}

function renderQueueList(queue, plans) {
  const wrap = $(
    queue === "reinvest"
      ? "ord-queue-reinvest"
      : queue === "followon"
        ? "ord-queue-followon"
        : "ord-queue-dip-hunt"
  );
  const list = $(
    queue === "reinvest"
      ? "ord-reinvest-list"
      : queue === "followon"
        ? "ord-followon-list"
        : "ord-dip-hunt-list"
  );
  if (!wrap || !list) return 0;
  if (!plans.length) {
    wrap.hidden = true;
    list.innerHTML = "";
    if (queue === "followon") {
      const count = $("ord-followon-count");
      if (count) count.hidden = true;
    }
    return 0;
  }
  wrap.hidden = false;
  list.innerHTML = plans
    .map((plan) => `<li>${renderPlanItem(queue, plan)}</li>`)
    .join("");
  if (queue === "followon") {
    const count = $("ord-followon-count");
    if (count) {
      count.textContent = String(plans.length);
      count.hidden = plans.length < 1;
    }
  }
  return plans.length;
}

let ordPendingApprovals = [];

async function fetchPendingApprovals() {
  try {
    const res = await api("/api/auto-trade/approvals");
    if (res && res.ok && Array.isArray(res.pending_approvals)) {
      ordPendingApprovals = res.pending_approvals;
    }
  } catch (e) {
    // Non-fatal if auto-trade approvals cannot be fetched
  }
}

function renderDeskQueues() {
  const panel = $("ord-desk-panel");
  const desk = deskPlans();
  const reinvest = standaloneDeskPlans("reinvest", desk.reinvest, REINVEST_LIVE);
  const followon = standaloneDeskPlans("followon", desk.followon, FOLLOWON_LIVE);
  const dipHunt = standaloneDeskPlans("dip_hunt", desk.dip_hunt, DIP_HUNT_LIVE);

  const apprList = $("ord-approvals-list");
  const apprQueue = $("ord-queue-approvals");
  const numApprovals = ordPendingApprovals.length;
  if (apprList && apprQueue) {
    if (numApprovals > 0) {
      apprQueue.hidden = false;
      apprList.innerHTML = ordPendingApprovals.map((item) => {
        const action = String(item.action || "ORDER").toUpperCase();
        const isLong = action === "BUY" || action === "COVER";
        const spec = `${escapeHtml(String(item.qty || ""))} @ ${
          item.price ? "$" + Number(item.price).toFixed(2) : "MKT"
        }`;
        return `
        <li>
          <div class="ord-queue-item" data-kind="waiting">
            <div class="ord-queue-row">
              <div class="ord-plan-chips">
                <span class="ord-plan-sym-text">${escapeHtml(item.symbol || "")}</span>
                <span class="side-badge ${isLong ? "buy" : "sell"}">${escapeHtml(action)}</span>
                <span class="ord-plan-dot-sep" aria-hidden="true">·</span>
                <span class="ord-plan-spec-val">${spec}</span>
              </div>
              <div class="ord-queue-actions">
                <a href="/auto-trade?approval=${encodeURIComponent(item.id)}" class="ghost">${escapeHtml(tx("ord_approval_review", "Review & Execute"))}</a>
              </div>
            </div>
            <div class="ord-plan-status-row">
              <div class="ord-plan-status-pill kind-waiting">
                <span class="ord-plan-pulse-dot" aria-hidden="true"></span>
                <span class="ord-plan-note-text">${escapeHtml(item.reason || item.thesis || tx("pending_approval_note", "Awaiting your approval"))}</span>
              </div>
            </div>
          </div>
        </li>`;
      }).join("");
    } else {
      apprQueue.hidden = true;
      apprList.innerHTML = "";
    }
  }

  const n =
    numApprovals +
    renderQueueList("reinvest", reinvest) +
    renderQueueList("followon", followon) +
    renderQueueList("dip_hunt", dipHunt);
  if (panel) panel.hidden = n < 1;
  const count = $("ord-desk-count");
  if (count) {
    count.textContent = String(n);
    count.hidden = n < 1;
  }
}

/** Rows the operator may act on in bulk: cancelable, on the Open tab, and not
 *  already on their way out. */
function selectableOrders() {
  if (ordFilterStatus !== "open" || mutationsLocked()) return [];
  return filteredOrders().filter((o) => o.is_cancelable && !orderIsCanceling(o));
}

function selectedOrders() {
  return selectableOrders().filter((o) => ordSelectedIds.has(String(o.id || "")));
}

/** A tick on a row that has since filled or vanished must not survive into the
 *  next bulk cancel. */
function pruneOrdSelection() {
  const live = new Set(selectableOrders().map((o) => String(o.id || "")));
  for (const id of Array.from(ordSelectedIds)) {
    if (!live.has(id)) ordSelectedIds.delete(id);
  }
}

function syncSelectionUi() {
  const selectable = selectableOrders();
  const selected = selectedOrders();
  const selectAll = $("ord-select-all");
  if (selectAll) {
    selectAll.disabled = selectable.length < 1;
    selectAll.checked = selectable.length > 0 && selected.length === selectable.length;
    selectAll.indeterminate = selected.length > 0 && selected.length < selectable.length;
  }
  const btn = $("btn-cancel-selected-orders");
  const text = $("btn-cancel-selected-text");
  if (btn) btn.hidden = selected.length < 1;
  if (text) {
    text.textContent = `${tx("ord_cancel_selected", "Cancel selected")} (${selected.length})`;
  }

  const floatingBar = $("ord-batch-floating-bar");
  const floatingCount = $("ord-batch-count");
  const floatingVal = $("ord-batch-val");
  const floatingBtnText = $("btn-ord-batch-cancel-text");

  if (floatingBar) floatingBar.hidden = selected.length < 1;
  if (floatingCount) floatingCount.textContent = String(selected.length);
  if (floatingBtnText) {
    floatingBtnText.textContent = `${tx("ord_cancel_selected", "Cancel selected")} (${selected.length})`;
  }
  if (floatingVal) {
    let totalVal = 0;
    let hasVal = false;
    for (const o of selected) {
      if (o.notional != null) {
        totalVal += Number(o.notional || 0);
        hasVal = true;
      } else if (o.qty != null && (o.limit_price != null || o.mark_price != null)) {
        const px = Number(o.limit_price ?? o.mark_price ?? 0);
        totalVal += Number(o.qty || 0) * px;
        hasVal = true;
      }
    }
    floatingVal.textContent = hasVal ? orderMoney(totalVal) : "—";
  }
}

function setOrdSelected(orderId, checked) {
  const id = String(orderId || "");
  if (!id) return;
  if (checked) ordSelectedIds.add(id);
  else ordSelectedIds.delete(id);
  syncSelectionUi();
}

const ORD_CSV_COLUMNS = [
  ["submitted_at", (o) => o.submitted_at || ""],
  ["symbol", (o) => o.symbol || ""],
  ["side", (o) => o.side || ""],
  ["type", (o) => o.type || ""],
  ["order_class", (o) => o.order_class || ""],
  ["qty", (o) => (o.qty == null ? "" : o.qty)],
  ["filled_qty", (o) => (o.filled_qty == null ? "" : o.filled_qty)],
  ["notional", (o) => (o.notional == null ? "" : o.notional)],
  ["limit_price", (o) => (o.limit_price == null ? "" : o.limit_price)],
  ["stop_price", (o) => (o.stop_price == null ? "" : o.stop_price)],
  ["trail_percent", (o) => (o.trail_percent == null ? "" : o.trail_percent)],
  ["trail_price", (o) => (o.trail_price == null ? "" : o.trail_price)],
  ["filled_avg_price", (o) => (o.filled_avg_price == null ? "" : o.filled_avg_price)],
  ["mark_price", (o) => (o.mark_price == null ? "" : o.mark_price)],
  ["mark_change_pct", (o) =>
    o.mark_change_pct == null ? "" : Number(o.mark_change_pct).toFixed(4)],
  ["mark_age_seconds", (o) => (o.mark_age_seconds == null ? "" : o.mark_age_seconds)],
  ["trigger_distance_pct", (o) =>
    o.trigger_distance_pct == null ? "" : Number(o.trigger_distance_pct).toFixed(4)],
  ["time_in_force", (o) => o.time_in_force || ""],
  ["extended_hours", (o) => (o.extended_hours ? "true" : "false")],
  ["status", (o) => o.status || ""],
  ["filled_at", (o) => o.filled_at || ""],
  ["canceled_at", (o) => o.canceled_at || ""],
  ["order_id", (o) => o.id || ""],
  ["client_order_id", (o) => o.client_order_id || ""],
];

function csvCell(value) {
  const s = String(value ?? "");
  // A leading =, +, -, or @ makes a spreadsheet treat the cell as a formula.
  const safe = /^[=+\-@]/.test(s) ? `'${s}` : s;
  return /["\n,]/.test(safe) ? `"${safe.replaceAll('"', '""')}"` : safe;
}

function exportOrdersCsv() {
  const rows = filteredOrders();
  if (!rows.length) {
    showToast(tx("ord_export_empty", "There is nothing on the blotter to export."), "error");
    return;
  }
  const lines = [ORD_CSV_COLUMNS.map(([name]) => csvCell(name)).join(",")];
  rows.forEach((o) => {
    lines.push(ORD_CSV_COLUMNS.map(([, read]) => csvCell(read(o))).join(","));
  });
  // The BOM keeps Excel from mangling symbols and non-Latin headers.
  const blob = new Blob([`﻿${lines.join("\r\n")}\r\n`], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const stamp = new Date().toISOString().slice(0, 19).replaceAll(":", "");
  const link = document.createElement("a");
  link.href = url;
  link.download = `orders-${ordFilterStatus}-${stamp}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  showToast(tx("ord_export_done", "{count} rows exported", { count: rows.length }), "ok");
}

function detailRow(label, value) {
  // "—" is what the formatters return for absent values; a details list is
  // clearer without rows that only say "nothing here".
  if (value == null || value === "" || value === "—") return "";
  return `<div class="ord-detail-row"><dt>${escapeHtml(label)}</dt><dd class="mono">${escapeHtml(
    String(value)
  )}</dd></div>`;
}

function openDetailModal(order) {
  if (!order) return;
  ordDetailTarget = order;
  const badge = $("ord-detail-symbol");
  if (badge) badge.textContent = order.symbol || "—";
  const list = $("ord-detail-list");
  if (list) {
    const side = String(order.side || "").toLowerCase();
    const qty = qtyText(order);
    const rows = [
      detailRow(
        tx("side", "Side"),
        side === "sell" ? tx("sell", "Sell") : tx("buy", "Buy")
      ),
      detailRow(tx("order_type", "Order type"), orderTypeLabel(order.type)),
      detailRow(tx("status", "Status"), statusLabel(order.status)),
      detailRow(tx("shares_qty", "Shares / Qty"), qty.main),
      detailRow(
        tx("ord_filled_qty", "Filled"),
        Number(order.filled_qty || 0) > 0 ? orderQty(order.filled_qty) : ""
      ),
      detailRow(
        tx("ord_avg_fill", "Avg"),
        order.filled_avg_price != null ? orderMoney(order.filled_avg_price) : ""
      ),
      detailRow(tx("order_price", "Price"), orderPriceText(order).join(" · ")),
      detailRow(
        tx("current_price_label", "Current Price"),
        order.mark_price != null ? orderMoney(order.mark_price) : ""
      ),
      detailRow(
        tx("ord_mark_change", "Today"),
        Number.isFinite(Number(order.mark_change_pct))
          ? `${Number(order.mark_change_pct) >= 0 ? "+" : "−"}${Math.abs(
              Number(order.mark_change_pct)
            ).toFixed(2)}%`
          : ""
      ),
      detailRow(
        tx("ord_mark_age_label", "Price age"),
        Number.isFinite(Number(order.mark_age_seconds))
          ? formatAge(Number(order.mark_age_seconds))
          : ""
      ),
      detailRow(
        tx("ord_distance", "Distance"),
        Number.isFinite(Number(order.trigger_distance_pct))
          ? tx("ord_distance_away", "{pct}% away", {
              pct: Math.abs(Number(order.trigger_distance_pct)).toFixed(1),
            })
          : ""
      ),
      detailRow(tx("time_in_force", "Time in force"), orderTifLabel(order)),
      detailRow(
        tx("order_class", "Order class"),
        String(order.order_class || "simple").toUpperCase()
      ),
      detailRow(tx("submitted_at", "Submitted"), ordTime(order.submitted_at)),
      detailRow(tx("ord_updated_at", "Updated"), ordTime(order.updated_at)),
      detailRow(tx("ord_filled_at", "Filled at"), ordTime(order.filled_at)),
      detailRow(tx("ord_canceled_at", "Canceled at"), ordTime(order.canceled_at)),
      detailRow(tx("ord_order_id", "Order ID"), order.id),
      detailRow(tx("ord_client_order_id", "Client order ID"), order.client_order_id),
    ];
    list.innerHTML = rows.filter(Boolean).join("");
  }
  const link = $("ord-detail-position");
  if (link) {
    link.href = `${pagePath("positions")}?symbol=${encodeURIComponent(order.symbol || "")}`;
  }
  openOrdModal("ord-detail-modal");
}

/** Escalate a symbol search to the broker when — and only when — the page the
 *  blotter holds is full and the typed symbol found nothing in it. Anything the
 *  local page can already answer stays instant and unscoped. */
function scheduleSymbolScope() {
  clearTimeout(ordSymbolScopeTimer);
  ordSymbolScopeTimer = setTimeout(() => {
    const symbol = String(ordFilterSearch || "").trim().toUpperCase();
    const scopable = /^[A-Z][A-Z.\-]{0,11}$/.test(symbol);
    const capped = !!ordersPayload?.count_limited;
    const wanted =
      scopable && capped && filteredOrders().length === 0 ? symbol : "";
    if (wanted === ordSymbolScope) return;
    ordSymbolScope = wanted;
    refreshOrders({ quiet: true }).catch(() => {});
  }, 450);
}

function highlightOrder(orderId) {
  document.querySelectorAll(".is-highlighted").forEach((el) => {
    el.classList.remove("is-highlighted");
  });
  const matches = document.querySelectorAll(
    `[data-order-id="${CSS.escape(orderId)}"]`
  );
  if (!matches.length) return false;
  matches.forEach((el) => el.classList.add("is-highlighted"));
  matches[0].scrollIntoView({ block: "center", behavior: "smooth" });
  return true;
}

async function jumpToRelatedOrder(orderId) {
  if (!orderId) return;
  if (highlightOrder(orderId)) return;
  if (ordFilterKind !== "all" || ordFilterSide !== "all" || ordFilterType !== "all") {
    ordFilterKind = "all";
    ordFilterSide = "all";
    ordFilterType = "all";
    writeOrdQuery();
    renderOrdersPage();
    if (highlightOrder(orderId)) return;
  }
  if (ordFilterStatus === "open") {
    ordFilterStatus = "closed";
    writeOrdQuery();
    await refreshOrders({ quiet: true });
    if (highlightOrder(orderId)) return;
  }
  showToast(
    tx(
      "ord_related_missing",
      "That ticket is not on this blotter. Search the symbol or switch Open/Closed."
    ),
    "error"
  );
}

function cancelNoteForOrder(order) {
  const live = orderDeskLinks(order).filter((link) => link.live && link.role === "trigger");
  if (!live.length) return "";
  if (live.some((link) => link.queue === "reinvest")) {
    return tx(
      "cancel_order_has_buyback",
      "This sell has a buy-back armed. Cancelling the sell also drops the buy-back."
    );
  }
  if (live.some((link) => link.queue === "followon")) {
    return tx(
      "cancel_order_has_followon",
      "This close has a next ticket armed. Cancelling it also drops the next ticket."
    );
  }
  if (live.some((link) => link.queue === "dip_hunt")) {
    return tx(
      "cancel_order_has_dip_hunt",
      "This ticket has a dip hunt armed. Cancelling it may disarm the hunt."
    );
  }
  return "";
}

function replaceNoteForOrder(order) {
  const live = orderDeskLinks(order).filter((link) => link.live && link.role === "trigger");
  if (!live.length) return "";
  if (live.some((link) => link.queue === "reinvest")) {
    return tx(
      "edit_order_keeps_buyback",
      "This sell has a buy-back armed. Editing keeps it — it will follow the new sell."
    );
  }
  if (live.some((link) => link.queue === "followon")) {
    return tx(
      "edit_order_keeps_followon",
      "This close has a next ticket armed. Editing keeps it — it will follow the new close."
    );
  }
  if (live.some((link) => link.queue === "dip_hunt")) {
    return tx(
      "edit_order_keeps_dip_hunt",
      "This ticket has a dip hunt armed. Editing keeps the hunt watching the new ticket."
    );
  }
  return "";
}

function renderOrdersPage() {
  syncOrdFiltersUi();
  renderKpis();
  const rows = filteredOrders();
  const count = $("ord-total-count");
  if (count) {
    count.textContent =
      ordersPayload?.count_limited && !filtersActive() ? `≥${rows.length}` : String(rows.length);
    count.hidden = false;
  }
  const tbody = $("ord-table-body");
  if (tbody) tbody.innerHTML = rows.map(renderOrderRow).join("");
  const cards = $("ord-cards-list");
  if (cards) cards.innerHTML = rows.map(renderOrderCard).join("");
  renderEmpty(rows);
  renderDeskQueues();

  const loopNotice = $("ord-loop-notice");
  if (loopNotice) loopNotice.hidden = !loopRunning;

  const cancelAll = $("btn-cancel-all-orders");
  if (cancelAll) {
    const n = Number(ordersPayload?.open_count || 0);
    cancelAll.disabled = mutationsLocked() || n < 1 || ordFilterStatus !== "open";
    // The count is account-wide while the blotter below may be filtered down to
    // three rows, so the button has to say what it will really reach.
    const label = $("btn-cancel-all-text");
    if (label) {
      label.textContent =
        n > 0
          ? `${tx("cancel_all_orders", "Cancel all")} (${
              ordersPayload?.open_count_limited ? `≥${n}` : n
            })`
          : tx("cancel_all_orders", "Cancel all");
    }
    cancelAll.title =
      ordFilterStatus !== "open"
        ? tx("ord_cancel_all_closed_hint", "Switch to Open to cancel resting tickets")
        : tx("cancel_all_orders_hint", "Cancel every open order");
  }

  pruneOrdSelection();
  syncSelectionUi();
}

async function refreshOrders({ quiet = false } = {}) {
  const requestSeq = ++ordRequestSeq;
  ordActiveRequests += 1;
  ordersBusy = true;
  ordLastFetchStartedAt = Date.now();
  const refreshBtn = $("btn-refresh-orders");
  if (!quiet && refreshBtn) {
    refreshBtn.disabled = true;
    refreshBtn.setAttribute("aria-busy", "true");
  }
  try {
    if (!quiet && ordSyncState === "error") setOrdSyncState("live");
    const params = new URLSearchParams();
    params.set("status", ordFilterStatus);
    params.set("limit", "500");
    // Alpaca matches `symbol` exactly, so it can never back a prefix search.
    // It is used only as an escape hatch: when the 500-row page is full and the
    // typed symbol matched nothing in it, the match may simply be past the cap.
    // The KPI counts stay account-wide server-side, so the rail never narrows.
    if (ordSymbolScope) params.set("symbol", ordSymbolScope);
    if (ordFilterStatus === "closed") {
      if (ordFilterAfter) params.set("after", ordFilterAfter);
      if (ordFilterUntil) params.set("until", ordFilterUntil);
    }
    const [data] = await Promise.all([
      api(`/api/orders?${params.toString()}`),
      fetchPendingApprovals(),
    ]);
    if (requestSeq !== ordRequestSeq) return;
    ordersPayload = data;
    // Drop the optimistic flag as soon as the broker's own answer covers it:
    // the ticket left the open book, or it now reports the cancel itself.
    const stillPending = new Set(
      (data.orders || [])
        .filter((o) => o.is_cancelable && String(o.status || "").toLowerCase() !== "canceled")
        .map((o) => String(o.id || ""))
    );
    for (const id of Array.from(ordCancelingIds)) {
      if (!stillPending.has(id)) ordCancelingIds.delete(id);
    }
    ordLastUpdatedTime = Date.now();
    renderOrdersPage();
    // Set after the render: whether polling continues depends on the armed
    // count this payload just delivered.
    setOrdSyncState(ordAutoRefreshActive() ? "live" : "manual");
  } catch (err) {
    if (requestSeq !== ordRequestSeq) return;
    setOrdSyncState("error");
    if (!quiet) showToast(err.message, "error");
  } finally {
    ordActiveRequests = Math.max(0, ordActiveRequests - 1);
    ordersBusy = ordActiveRequests > 0;
    if (refreshBtn && !ordersBusy) {
      refreshBtn.disabled = false;
      refreshBtn.removeAttribute("aria-busy");
    }
  }
}

function openOrdModal(id) {
  const el = $(id);
  if (!el) return;
  ordModalReturnFocus = document.activeElement;
  el.hidden = false;
  const live = isLiveEnv();
  el.querySelectorAll(".confirm-live-banner").forEach((banner) => {
    banner.hidden = !live;
  });
  document.querySelector(".app")?.setAttribute("inert", "");
  const focusable = el.querySelector("button, input, select, a[href]");
  focusable?.focus();
}

function closeOrdModal(id) {
  const el = $(id);
  if (el) el.hidden = true;
  if (id === "ord-cancel-modal") {
    ordCancelTarget = null;
    const note = $("ord-cancel-plan-note");
    if (note) {
      note.hidden = true;
      note.textContent = "";
    }
  }
  if (id === "ord-replace-modal") {
    ordReplaceTarget = null;
    const err = $("ord-replace-error");
    if (err) {
      err.hidden = true;
      err.textContent = "";
    }
    const note = $("ord-replace-plan-note");
    if (note) {
      note.hidden = true;
      note.textContent = "";
    }
  }
  if (!isAnyOrdModalOpen()) document.querySelector(".app")?.removeAttribute("inert");
  if (
    ordModalReturnFocus &&
    document.contains(ordModalReturnFocus) &&
    typeof ordModalReturnFocus.focus === "function"
  ) {
    ordModalReturnFocus.focus();
  }
  ordModalReturnFocus = null;
}

function findOrder(id) {
  return (ordersPayload?.orders || []).find((o) => o.id === id) || null;
}

async function cancelDeskPlan(queue, planId) {
  if (!planId || mutationsLocked()) return;
  const routes = {
    reinvest: { url: "/api/reinvest/cancel", ok: "reinvest_cancelled", fallback: "Buy-back cancelled" },
    followon: { url: "/api/followon/cancel", ok: "followon_cancelled", fallback: "Next ticket cancelled" },
    dip_hunt: { url: "/api/dip-hunt/cancel", ok: "dip_hunt_cancelled", fallback: "Dip hunt cancelled" },
  };
  const route = routes[queue];
  if (!route) return;
  try {
    await api(route.url, {
      method: "POST",
      body: JSON.stringify({ plan_id: planId }),
    });
    showToast(tx(route.ok, route.fallback), "ok");
    await refreshOrders({ quiet: true });
  } catch (err) {
    showToast(err.message, "error");
  }
}

function openCancelOrderModal(order) {
  if (!order?.id || !order.is_cancelable || mutationsLocked()) return;
  ordCancelTarget = order;
  const summary = $("ord-cancel-summary");
  if (summary) {
    const side = String(order.side || "").toLowerCase();
    summary.textContent = `${side === "sell" ? tx("sell", "Sell") : tx("buy", "Buy")} · ${orderTypeLabel(
      order.type
    )} · ${orderQty(order.qty)} ${order.symbol || ""}`;
  }
  const note = $("ord-cancel-plan-note");
  if (note) {
    const text = cancelNoteForOrder(order);
    note.textContent = text;
    note.hidden = !text;
  }
  openOrdModal("ord-cancel-modal");
}

async function confirmCancelOne() {
  if (!ordCancelTarget?.id || mutationsLocked()) return;
  const orderId = ordCancelTarget.id;
  const btn = $("btn-cancel-confirm");
  if (btn) btn.disabled = true;
  try {
    await api("/api/order/cancel", {
      method: "POST",
      body: JSON.stringify({ order_id: orderId }),
    });
    // The broker reports pending_cancel on its own schedule; until it does, the
    // row would otherwise look exactly as untouched as before the click.
    ordCancelingIds.add(String(orderId));
    ordSelectedIds.delete(String(orderId));
    showToast(tx("order_cancelled", "Order canceled"), "ok");
    closeOrdModal("ord-cancel-modal");
    if (ordersPayload) renderOrdersPage();
    await refreshOrders({ quiet: true });
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function openCancelSelectedModal() {
  const selected = selectedOrders();
  if (!selected.length) return;
  const list = $("ord-cancel-selected-list");
  if (list) {
    list.innerHTML = selected
      .map((o) => {
        const side = String(o.side || "").toLowerCase();
        const label = `${side === "sell" ? tx("sell", "Sell") : tx("buy", "Buy")} · ${escapeHtml(
          o.symbol || ""
        )} · ${escapeHtml(orderTypeLabel(o.type))} · ${escapeHtml(orderQty(o.qty))}`;
        const note = cancelNoteForOrder(o);
        return `<li class="mono">${label}${
          note ? `<span class="ord-confirm-note">${escapeHtml(note)}</span>` : ""
        }</li>`;
      })
      .join("");
  }
  const confirm = $("btn-cancel-selected-confirm");
  if (confirm) {
    confirm.textContent = `${tx("ord_cancel_selected_confirm", "Cancel selected")} (${
      selected.length
    })`;
  }
  openOrdModal("ord-cancel-selected-modal");
}

async function confirmCancelSelected() {
  if (mutationsLocked()) return;
  const selected = selectedOrders();
  if (!selected.length) return;
  const btn = $("btn-cancel-selected-confirm");
  if (btn) btn.disabled = true;
  let ok = 0;
  const failures = [];
  try {
    // Serial, not parallel: the desk plans attached to these tickets are
    // unwound server-side per cancel, and a burst would race that unwind.
    for (const order of selected) {
      const id = String(order.id || "");
      try {
        await api("/api/order/cancel", {
          method: "POST",
          body: JSON.stringify({ order_id: id }),
        });
        ordCancelingIds.add(id);
        ordSelectedIds.delete(id);
        ok += 1;
      } catch (err) {
        failures.push(`${order.symbol || id}: ${err.message}`);
      }
    }
    if (failures.length) {
      showToast(
        tx(
          "orders_cancelled_partial",
          "{cancelled} canceled; {failed} could not be canceled. Refresh and review remaining orders.",
          { cancelled: ok, failed: failures.length }
        ),
        "error"
      );
    } else {
      showToast(tx("orders_cancelled", "{count} orders canceled", { count: ok }), "ok");
    }
    closeOrdModal("ord-cancel-selected-modal");
    if (ordersPayload) renderOrdersPage();
    await refreshOrders({ quiet: true });
  } finally {
    if (btn) btn.disabled = false;
  }
}

function openCancelAllModal() {
  const n = Number(ordersPayload?.open_count || 0);
  const count = $("ord-cancel-all-count");
  if (count) {
    count.textContent = ordersPayload?.open_count_limited
      ? tx(
          "cancel_all_count_at_least",
          "At least {count} open orders will be canceled.",
          { count: n }
        )
      : tx("cancel_all_count", "{count} open orders will be canceled.", { count: n });
  }
  // Cancel all is account-wide while the blotter may be filtered to a handful
  // of rows. Saying so at the point of confirmation is the whole guard.
  const warn = $("ord-cancel-all-filtered");
  if (warn) {
    const shown = filteredOrders().length;
    const mismatch = filtersActive() && shown < n;
    warn.textContent = mismatch
      ? tx(
          "ord_cancel_all_filtered",
          "Your filters show {shown} of {total} open orders. This cancels all {total}, not just the ones on screen.",
          { shown, total: n }
        )
      : "";
    warn.hidden = !mismatch;
  }
  openOrdModal("ord-cancel-all-modal");
}

async function confirmCancelAll() {
  if (mutationsLocked()) return;
  const btn = $("btn-cancel-all-confirm");
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/orders/cancel-all", { method: "POST" });
    const n = Number(data.cancelled || 0);
    const failed = Number(data.failed || 0);
    if (failed > 0) {
      showToast(
        tx(
          "orders_cancelled_partial",
          "{cancelled} canceled; {failed} could not be canceled. Refresh and review remaining orders.",
          { cancelled: n, failed }
        ),
        "error"
      );
    } else {
      showToast(tx("orders_cancelled", "{count} orders canceled", { count: n }), "ok");
    }
    (ordersPayload?.orders || []).forEach((o) => {
      if (o.is_cancelable) ordCancelingIds.add(String(o.id || ""));
    });
    ordSelectedIds.clear();
    closeOrdModal("ord-cancel-all-modal");
    if (ordersPayload) renderOrdersPage();
    await refreshOrders({ quiet: true });
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function openReplaceModal(order) {
  if (!order || !order.is_replaceable || mutationsLocked()) return;
  ordReplaceTarget = order;
  const badge = $("ord-replace-symbol");
  if (badge) badge.textContent = order.symbol || "—";
  const summary = $("ord-replace-summary");
  if (summary) {
    const side = String(order.side || "").toLowerCase();
    const parts = [
      side === "sell" ? tx("sell", "Sell") : tx("buy", "Buy"),
      orderTypeLabel(order.type),
      orderQty(order.qty),
    ];
    // A replace has to leave at least what already filled, so the filled slice
    // belongs in front of the operator before they retype the quantity.
    const filled = Number(order.filled_qty || 0);
    if (filled > 0) {
      parts.push(tx("ord_filled_of", "{filled} filled", { filled: orderQty(filled) }));
    }
    summary.textContent = parts.join(" · ");
  }
  const note = $("ord-replace-plan-note");
  if (note) {
    const text = replaceNoteForOrder(order);
    note.textContent = text;
    note.hidden = !text;
  }
  const title = $("ord-replace-title");
  if (title) title.textContent = tx("edit_order_title", "Edit order");
  const qty = $("ord-replace-qty");
  const limit = $("ord-replace-limit");
  const stop = $("ord-replace-stop");
  const trail = $("ord-replace-trail");
  const tif = $("ord-replace-tif");
  if (qty) {
    qty.value = order.qty != null ? String(order.qty) : "";
    const currentQty = Number(order.qty);
    qty.disabled = Number.isFinite(currentQty) && !Number.isInteger(currentQty);
    qty.setAttribute("aria-describedby", "replace-order-help");
  }
  if (limit) {
    limit.value = order.limit_price != null ? String(order.limit_price) : "";
    const limitLabel = $("ord-replace-limit-label");
    if (limitLabel) limitLabel.hidden = !["limit", "stop_limit"].includes(order.type);
  }
  if (stop) {
    stop.value = order.stop_price != null ? String(order.stop_price) : "";
    const stopLabel = $("ord-replace-stop-label");
    if (stopLabel) stopLabel.hidden = !["stop", "stop_limit"].includes(order.type);
  }
  const trailLabel = $("ord-replace-trail-label");
  if (trail) {
    const usesAmount = order.trail_percent == null && order.trail_price != null;
    const currentTrail = usesAmount ? order.trail_price : order.trail_percent;
    trail.value = currentTrail != null ? String(currentTrail) : "";
    trail.step = "0.01";
    if (usesAmount) trail.removeAttribute("max");
    else trail.max = "50";
    const trailText = $("ord-replace-trail-text");
    if (trailText) {
      trailText.dataset.i18n = usesAmount ? "trail_amount" : "trail_percent";
      trailText.textContent = usesAmount
        ? tx("trail_amount", "Trail amount")
        : tx("trail_percent", "Trail %");
    }
    if (trailLabel) trailLabel.hidden = order.type !== "trailing_stop";
  }
  if (tif) {
    tif.value = order.time_in_force || "day";
    refreshNiceSelect(tif);
  }
  openOrdModal("ord-replace-modal");
}

function numOrNull(el) {
  if (!el || el.value === "") return null;
  const n = Number(el.value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function showReplaceError(message) {
  const err = $("ord-replace-error");
  if (!err) return;
  err.hidden = false;
  err.textContent = message;
  err.focus();
}

async function confirmReplace() {
  if (!ordReplaceTarget || mutationsLocked()) return;
  const err = $("ord-replace-error");
  const form = $("ord-replace-form");
  if (form && !form.checkValidity()) {
    form.reportValidity();
    return;
  }
  const body = { order_id: ordReplaceTarget.id };
  const qty = numOrNull($("ord-replace-qty"));
  const limit = numOrNull($("ord-replace-limit"));
  const stop = numOrNull($("ord-replace-stop"));
  const trail = numOrNull($("ord-replace-trail"));
  const tif = String($("ord-replace-tif")?.value || "").toLowerCase();
  if (qty != null && qty !== Number(ordReplaceTarget.qty)) body.qty = qty;
  if (limit != null && limit !== Number(ordReplaceTarget.limit_price)) body.limit_price = limit;
  if (stop != null && stop !== Number(ordReplaceTarget.stop_price)) body.stop_price = stop;
  if (trail != null) {
    const currentTrail =
      ordReplaceTarget.trail_percent != null
        ? Number(ordReplaceTarget.trail_percent)
        : Number(ordReplaceTarget.trail_price);
    if (trail !== currentTrail) body.trail = trail;
  }
  if (tif && tif !== String(ordReplaceTarget.time_in_force || "").toLowerCase()) {
    body.time_in_force = tif;
  }
  const keys = Object.keys(body).filter((k) => k !== "order_id");
  if (!keys.length) {
    showReplaceError(
      tx("replace_no_change", "Change a quantity, price, trail, or time-in-force first.")
    );
    return;
  }
  const btn = $("btn-replace-confirm");
  if (btn) btn.disabled = true;
  try {
    await api("/api/order/replace", {
      method: "POST",
      body: JSON.stringify(body),
    });
    showToast(tx("order_replaced", "Order replaced"), "ok");
    closeOrdModal("ord-replace-modal");
    await refreshOrders({ quiet: true });
  } catch (ex) {
    if (err) {
      showReplaceError(ex.message);
    } else {
      showToast(ex.message, "error");
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function initOrdersUi() {
  startOrdSyncTimer();
  readOrdQuery();
  syncOrdFiltersUi();

  $("ord-search")?.addEventListener("input", (e) => {
    ordFilterSearch = String(e.target.value || "").toUpperCase();
    writeOrdQuery();
    renderOrdersPage();
    scheduleSymbolScope();
  });
  $("btn-clear-ord-search")?.addEventListener("click", () => {
    ordFilterSearch = "";
    writeOrdQuery();
    renderOrdersPage();
    scheduleSymbolScope();
    $("ord-search")?.focus();
  });

  document.querySelectorAll("[data-filter-status]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = btn.dataset.filterStatus;
      if (next !== "open" && next !== "closed") return;
      if (ordFilterStatus === next) return;
      ordFilterStatus = next;
      // Nothing on the Closed tab is cancelable, so a selection carried across
      // would be a bulk action aimed at rows that no longer offer it.
      ordSelectedIds.clear();
      writeOrdQuery();
      syncOrdFiltersUi();
      refreshOrders().catch(() => {});
    });
  });
  document.querySelectorAll("[data-filter-side]").forEach((btn) => {
    btn.addEventListener("click", () => {
      ordFilterSide = btn.dataset.filterSide || "all";
      writeOrdQuery();
      renderOrdersPage();
    });
  });
  document.querySelectorAll("[data-filter-kind]").forEach((btn) => {
    btn.addEventListener("click", () => {
      ordFilterKind = btn.dataset.filterKind || "all";
      writeOrdQuery();
      renderOrdersPage();
    });
  });
  $("ord-type-select")?.addEventListener("change", (e) => {
    ordFilterType = String(e.target.value || "all");
    writeOrdQuery();
    renderOrdersPage();
  });
  $("ord-sort-select")?.addEventListener("change", (e) => {
    setOrdSort(String(e.target.value || "submitted"));
  });
  document.querySelectorAll(".pos-th-sortable").forEach((th) => {
    th.querySelector(".pos-sort-btn")?.addEventListener("click", () => {
      setOrdSort(th.dataset.sortCol || "", { toggle: true });
    });
  });
  // The two split figures under Open are the Kind filter said out loud, so
  // clicking one applies it rather than being decoration.
  document.querySelectorAll("[data-kpi-kind]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const kind = btn.dataset.kpiKind || "all";
      ordFilterKind = ordFilterKind === kind ? "all" : kind;
      writeOrdQuery();
      syncOrdFiltersUi();
      renderOrdersPage();
    });
  });
  $("btn-apply-ord-window")?.addEventListener("click", () => {
    const after = String($("ord-window-after")?.value || "").trim();
    const until = String($("ord-window-until")?.value || "").trim();
    if (after && until && after > until) {
      showToast(tx("ord_window_order_error", "The From date must come before the To date."), "error");
      return;
    }
    ordFilterAfter = ORD_DATE_RE.test(after) ? after : "";
    ordFilterUntil = ORD_DATE_RE.test(until) ? until : "";
    writeOrdQuery();
    syncOrdFiltersUi();
    refreshOrders().catch(() => {});
  });
  $("btn-export-orders")?.addEventListener("click", exportOrdersCsv);
  $("ord-select-all")?.addEventListener("change", (e) => {
    const on = !!e.target.checked;
    selectableOrders().forEach((o) => {
      const id = String(o.id || "");
      if (on) ordSelectedIds.add(id);
      else ordSelectedIds.delete(id);
    });
    renderOrdersPage();
  });
  $("btn-cancel-selected-orders")?.addEventListener("click", openCancelSelectedModal);
  $("btn-cancel-selected-x")?.addEventListener("click", () =>
    closeOrdModal("ord-cancel-selected-modal")
  );
  $("btn-cancel-selected-dismiss")?.addEventListener("click", () =>
    closeOrdModal("ord-cancel-selected-modal")
  );
  $("btn-cancel-selected-confirm")?.addEventListener("click", () => {
    confirmCancelSelected().catch((err) => showToast(err.message, "error"));
  });
  $("btn-detail-x")?.addEventListener("click", () => closeOrdModal("ord-detail-modal"));
  $("btn-detail-dismiss")?.addEventListener("click", () => closeOrdModal("ord-detail-modal"));
  $("btn-reset-ord-filters")?.addEventListener("click", resetOrdFilters);
  $("btn-empty-clear-ord-filters")?.addEventListener("click", resetOrdFilters);
  $("btn-refresh-orders")?.addEventListener("click", () => refreshOrders().catch(() => {}));
  $("btn-cancel-all-orders")?.addEventListener("click", openCancelAllModal);
  const jumpToArmedPlans = () => {
    const panel = $("ord-desk-panel");
    if (panel && !panel.hidden) {
      panel.scrollIntoView({ block: "start", behavior: "smooth" });
      return;
    }
    const toggle = document.querySelector(".ord-plan-fold-toggle");
    if (toggle) {
      toggle.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    const inline = document.querySelector(".ord-inline-plan");
    if (inline) {
      inline.closest("tr, .pos-card")?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  };
  // Both rail tiles are divs carrying role="button", so each has to answer the
  // two keys a real button answers.
  const wireTile = (id, run) => {
    const tile = $(id);
    if (!tile) return;
    const fire = () => {
      if (tile.classList.contains("is-interactive")) run();
    };
    tile.addEventListener("click", fire);
    tile.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      if (!tile.classList.contains("is-interactive")) return;
      e.preventDefault();
      fire();
    });
  };
  wireTile("ord-kpi-armed-tile", jumpToArmedPlans);
  wireTile("ord-kpi-nearest-tile", () => {
    const id = ordersPayload?.nearest_trigger?.order_id;
    if (id) jumpToRelatedOrder(String(id)).catch(() => {});
  });

  $("btn-cancel-x")?.addEventListener("click", () => closeOrdModal("ord-cancel-modal"));
  $("btn-cancel-dismiss")?.addEventListener("click", () => closeOrdModal("ord-cancel-modal"));
  $("btn-cancel-confirm")?.addEventListener("click", () => confirmCancelOne());
  $("btn-cancel-all-x")?.addEventListener("click", () => closeOrdModal("ord-cancel-all-modal"));
  $("btn-cancel-all-dismiss")?.addEventListener("click", () => closeOrdModal("ord-cancel-all-modal"));
  $("btn-cancel-all-confirm")?.addEventListener("click", () => confirmCancelAll());
  $("btn-replace-x")?.addEventListener("click", () => closeOrdModal("ord-replace-modal"));
  $("btn-replace-dismiss")?.addEventListener("click", () => closeOrdModal("ord-replace-modal"));
  $("ord-replace-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    confirmReplace();
  });
  $("btn-toggle-ord-filters")?.addEventListener("click", toggleOrdFiltersBar);
  $("btn-ord-batch-deselect")?.addEventListener("click", () => {
    ordSelectedIds.clear();
    syncSelectionUi();
    renderOrdersPage();
  });
  $("btn-ord-batch-cancel")?.addEventListener("click", openCancelSelectedModal);

  document.addEventListener("click", (e) => {
    const actionsToggle = e.target.closest?.(".pos-card-actions-toggle");
    if (actionsToggle) {
      const actions = actionsToggle.nextElementSibling;
      const orderId = actionsToggle.closest(".pos-card")?.dataset.orderId;
      if (actions?.classList.contains("pos-card-actions") && orderId) {
        const open = actions.hidden;
        actions.hidden = !open;
        actionsToggle.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) ordExpandedCardActions.add(orderId);
        else ordExpandedCardActions.delete(orderId);
      }
      return;
    }
    const foldToggle = e.target.closest?.("[data-toggle-plan]");
    if (foldToggle) {
      e.preventDefault();
      const id = foldToggle.getAttribute("data-toggle-plan") || "";
      const fold = id
        ? document.querySelector(`details[data-plan-fold="${CSS.escape(id)}"]`)
        : null;
      if (fold) fold.open = !fold.open;
      return;
    }
    const cancelBtn = e.target.closest?.(".ord-act-cancel");
    if (cancelBtn && !cancelBtn.disabled) {
      const order = findOrder(cancelBtn.dataset.orderId);
      if (order) openCancelOrderModal(order);
      return;
    }
    const replaceBtn = e.target.closest?.(".ord-act-replace");
    if (replaceBtn && !replaceBtn.disabled) {
      const order = findOrder(replaceBtn.dataset.orderId);
      if (order) openReplaceModal(order);
      return;
    }
    const detailBtn = e.target.closest?.(".ord-act-detail");
    if (detailBtn) {
      const order = findOrder(detailBtn.dataset.orderId);
      if (order) openDetailModal(order);
      return;
    }
    const jumpBtn = e.target.closest?.("[data-jump-order]");
    if (jumpBtn) {
      jumpToRelatedOrder(jumpBtn.getAttribute("data-jump-order") || "").catch(() => {});
      return;
    }
    const reinvestBtn = e.target.closest?.("[data-cancel-reinvest]");
    if (reinvestBtn && !reinvestBtn.disabled) {
      cancelDeskPlan("reinvest", reinvestBtn.getAttribute("data-cancel-reinvest") || "").catch(
        () => {}
      );
      return;
    }
    const followonBtn = e.target.closest?.("[data-cancel-followon]");
    if (followonBtn && !followonBtn.disabled) {
      cancelDeskPlan("followon", followonBtn.getAttribute("data-cancel-followon") || "").catch(
        () => {}
      );
      return;
    }
    const dipBtn = e.target.closest?.("[data-cancel-dip-hunt]");
    if (dipBtn && !dipBtn.disabled) {
      cancelDeskPlan("dip_hunt", dipBtn.getAttribute("data-cancel-dip-hunt") || "").catch(
        () => {}
      );
      return;
    }
    const backdrop = e.target.closest?.(".pos-modal-backdrop");
    if (backdrop && e.target === backdrop) closeOrdModal(backdrop.id);
  });

  // Checkboxes are re-rendered on every poll, so the state has to live in the
  // set rather than on the element.
  document.addEventListener("change", (e) => {
    const check = e.target.closest?.(".ord-row-check");
    if (!check) return;
    setOrdSelected(check.dataset.orderId, check.checked);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const top = topmostOpenOrdModal();
      if (top) closeOrdModal(top.id);
      return;
    }
    trapOrdModalFocus(e);
  });

  document.addEventListener(
    "toggle",
    (e) => {
      const el = e.target;
      if (!(el instanceof HTMLDetailsElement)) return;
      const id = el.getAttribute("data-plan-fold");
      if (!id) return;
      if (el.open) ordOpenPlanFolds.add(id);
      else ordOpenPlanFolds.delete(id);
      document.querySelectorAll(`[data-toggle-plan="${CSS.escape(id)}"]`).forEach((btn) => {
        btn.setAttribute("aria-expanded", el.open ? "true" : "false");
      });
    },
    true
  );

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      setOrdSyncState(ordAutoRefreshActive() ? "live" : "manual");
      refreshOrders({ quiet: true }).catch(() => {});
    } else {
      setOrdSyncState("paused");
    }
  });
}

initOrdersUi();
refreshStatus({ forceSettings: true })
  .catch((err) => showToast(err.message, "error"))
  .finally(() => refreshOrders().catch(() => {}));

function onDeskStatusInterval() {
  if (ordersBusy) return;
  if (document.visibilityState !== "visible") return;
  if (isAnyOrdModalOpen()) return;
  const armed = armedDeskCount();
  if (ordFilterStatus !== "open" && armed < 1) return;
  const interval = armed > 0 ? ORD_ARMED_REFRESH_MS : ORD_REFRESH_MS;
  if (Date.now() - ordLastFetchStartedAt < interval) return;
  refreshOrders({ quiet: true }).catch(() => {});
}

function onDeskStatusUpdate(state) {
  const running = !!state?.loop_running;
  if (ordLastLoopRunning === running) return;
  ordLastLoopRunning = running;
  if (ordersPayload) renderOrdersPage();
}

function onDeskLanguageChange() {
  setOrdSyncState(ordSyncState);
  renderOrdersPage();
}
