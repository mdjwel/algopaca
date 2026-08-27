/**
 * Positions Page JavaScript for AlgoPaca
 * Portfolio allocation visualizer, positions table & cards, filters, sorting, partial/full close modal, liquidation.
 */


let positionsData = null;
let positionsBusy = false;
let posFilterSearch = "";
let posFilterSide = "all";
let posFilterPnl = "all";
let posSortKey = localStorage.getItem("desk_pos_sort_key") || "market_value";
let posSortDir = localStorage.getItem("desk_pos_sort_dir") || "desc";
let posSelectedSymbols = new Set();
// The allocation bar is a glance-at-it-occasionally panel, not a figure anyone
// watches tick — it starts collapsed so the blotter is what greets you, and
// remembers the choice for people who do want it open.
let posAllocationOpen = localStorage.getItem("desk_pos_allocation_open") === "1";
let posLastUpdatedTime = null;
let posLastUpdatedTimer = null;
let posLastFetchStartedAt = 0;
let posSyncState = "live";
let activeClosingPosition = null;
let closePercentage = 100;
let closeQty = 0;
let closeQtyRounded = false;
let posModalReturnFocus = null;

/** Background polls only ever need a blotter that is seconds-fresh. The desk
 *  status poll runs every 2s; matching it here would put four Alpaca calls a
 *  second against a 200/min budget for numbers nobody reads that fast. */
const POS_REFRESH_MS = 10000;

const POS_SORT_KEYS = new Set([
  "symbol",
  "side",
  "qty",
  "avg_entry_price",
  "current_price",
  "market_value",
  "unrealized_pl",
  "unrealized_pct",
  "unrealized_intraday_pl",
]);

// A stored preference can name a column that no longer exists — Weight was
// retired — which would leave the table sorted by an invisible key with no
// header lit to say so.
if (!POS_SORT_KEYS.has(posSortKey)) posSortKey = "market_value";

const ALLOCATION_PALETTE = [
  "#3fbf8f", "#d4894c", "#4c9bd4", "#b266d4", "#e35d5d",
  "#d4c84c", "#4cd4c3", "#d44c8c", "#8b9aab", "#80d44c",
  "#f39c12", "#1abc9c", "#9b59b6", "#34495e", "#e67e22"
];

const POS_MODAL_IDS = [
  "pos-close-modal",
  "pos-lots-modal",
  "pos-liquidate-selected-modal",
  "pos-liquidate-all-modal",
];

let activeLotsSymbol = null;
let lotsRequestSeq = 0;
let activeLotsData = null;
const activeLotsSelectedIndices = new Set();

/** True while any confirmation dialog is up. Background refreshes stand down
 *  then: re-rendering under an open dialog swaps the very numbers the user is
 *  about to act on, and rebuilds the rows behind it mid-click. */
function isAnyPosModalOpen() {
  return POS_MODAL_IDS.some((id) => {
    const el = $(id);
    return el && !el.hidden;
  });
}

function topmostOpenPosModal() {
  for (let i = POS_MODAL_IDS.length - 1; i >= 0; i -= 1) {
    const el = $(POS_MODAL_IDS[i]);
    if (el && !el.hidden) return el;
  }
  return null;
}

/** Show a dialog with focus moved inside it, remembering where to hand focus
 *  back so keyboard users do not get dropped at the top of the document. */
function openPosModal(id) {
  const modal = $(id);
  if (!modal) return;
  posModalReturnFocus = document.activeElement;
  modal.hidden = false;
  const focusTarget = modal.querySelector(
    "input:not([type=hidden]), button, select, [href], [tabindex]:not([tabindex='-1'])"
  );
  if (focusTarget) focusTarget.focus();
}

function closePosModal(id) {
  const modal = $(id);
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  if (posModalReturnFocus && document.contains(posModalReturnFocus)) {
    posModalReturnFocus.focus();
  }
  posModalReturnFocus = null;
}

/** Keep Tab inside the open dialog — the rows behind it are focusable and a
 *  stray Tab would let someone operate the blotter through the backdrop. */
function trapPosModalFocus(event) {
  if (event.key !== "Tab") return;
  const modal = topmostOpenPosModal();
  if (!modal) return;
  const focusables = Array.from(
    modal.querySelectorAll(
      "input:not([type=hidden]):not([disabled]), button:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex='-1'])"
    )
  ).filter((el) => el.offsetParent !== null);
  if (focusables.length === 0) return;
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

async function refreshPositions(options = {}) {
  const quiet = !!options.quiet;
  const refreshBtn = $("btn-refresh-positions");
  if (!quiet && refreshBtn) {
    refreshBtn.classList.add("is-loading");
  }
  positionsBusy = true;
  posLastFetchStartedAt = Date.now();
  try {
    const res = await fetch("/api/positions");
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    positionsData = data;
    posLastUpdatedTime = Date.now();
    setPosSyncState("live");
    // Render after marking live so a paint bug cannot flip a good fetch into
    // "Sync error" — the badge should only track transport health.
    try {
      renderPositionsPage();
    } catch (renderErr) {
      console.error("positions render failed", renderErr);
      if (!quiet) {
        showToast(
          renderErr.message || tx("error_refresh_positions", "Could not load positions"),
          "error"
        );
      }
    }
  } catch (err) {
    setPosSyncState("error");
    if (!quiet) {
      showToast(err.message || tx("error_refresh_positions", "Could not load positions"), "error");
    }
  } finally {
    positionsBusy = false;
    if (refreshBtn) {
      refreshBtn.classList.remove("is-loading");
    }
  }
}

/** The pulsing dot claimed "Live" even when every poll was failing. */
function setPosSyncState(state) {
  const indicator = $("pos-sync-indicator");
  posSyncState = state;
  if (!indicator) return;
  indicator.classList.toggle("is-error", state === "error");
  indicator.classList.toggle("is-paused", state === "paused");
  updatePosSyncStatus();
}

function updatePosSyncStatus() {
  const textEl = $("pos-last-updated-text");
  if (!textEl) return;
  if (posSyncState === "error") {
    textEl.textContent = tx("positions_sync_error", "Sync error");
    return;
  }
  if (posSyncState === "paused") {
    textEl.textContent = tx("positions_sync_paused", "Paused");
    return;
  }
  if (!posLastUpdatedTime) {
    textEl.textContent = tx("live_status", "Live");
    return;
  }
  const diffSec = Math.floor((Date.now() - posLastUpdatedTime) / 1000);
  if (diffSec < 15) {
    textEl.textContent = tx("live_status", "Live");
  } else if (diffSec < 60) {
    textEl.textContent = tx("seconds_ago", "{n}s ago", { n: diffSec });
  } else {
    const mins = Math.floor(diffSec / 60);
    textEl.textContent = tx("minutes_ago", "{n}m ago", { n: mins });
  }
}

function startPosSyncTimer() {
  if (posLastUpdatedTimer) clearInterval(posLastUpdatedTimer);
  posLastUpdatedTimer = setInterval(updatePosSyncStatus, 1000);
}

/** Fill a KPI change pill, or hide it when the percentage is unavailable —
 *  an em dash in a tinted pill reads as a value, which it is not. */
function setChangePill(el, pct) {
  if (!el) return;
  const val = Number(pct);
  if (pct == null || !Number.isFinite(val)) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = formatPnlPct(val);
  setPnlTone(el, val);
}

function renderPositionsPage() {
  if (!positionsData) return;
  const data = positionsData;
  const positions = Array.isArray(data.positions) ? data.positions : [];
  const account = data.account || {};

  // Top KPI Summary
  const equityEl = $("pos-kpi-equity");
  const equitySub = $("pos-kpi-equity-sub");
  const mvEl = $("pos-kpi-market-value");
  const costEl = $("pos-kpi-cost-basis");
  const unrlEl = $("pos-kpi-unrealized");
  const unrlPct = $("pos-kpi-unrealized-pct");
  const intraEl = $("pos-kpi-intraday");
  const intraPct = $("pos-kpi-intraday-pct");
  const investedFill = $("pos-invested-fill");
  const bpEl = $("pos-kpi-buying-power");
  const cashEl = $("pos-kpi-cash");
  const countEl = $("pos-kpi-count");
  const totalCountEl = $("pos-total-count");
  const winlossPill = $("pos-kpi-winloss-pill");
  const winsEl = $("pos-kpi-wins");
  const lossesEl = $("pos-kpi-losses");
  const modeBadge = $("pos-mode-badge");

  if (modeBadge) {
    const env = data.trading_mode || (data.paper === false ? "live" : "paper");
    if (env === "live") {
      modeBadge.className = "mode-badge env-live";
      modeBadge.textContent = tx("live_armed", "Live · Orders on");
    } else {
      modeBadge.className = "mode-badge armed";
      modeBadge.textContent = tx("paper_trading", "Paper trading");
    }
  }

  if (equityEl) equityEl.textContent = money(account.equity || 0);

  // The payload carries invested_pct, but it reads 0 whenever the account
  // snapshot lands before the position marks do; derive it from the two
  // figures already on screen so the meter can never contradict them.
  const equityVal = Number(account.equity || 0);
  const marketVal = Number(data.total_market_value || 0);
  let investedPct = Number(data.invested_pct);
  if (!Number.isFinite(investedPct) || (investedPct === 0 && marketVal > 0)) {
    investedPct = equityVal > 0 ? (marketVal / equityVal) * 100 : 0;
  }
  if (equitySub) {
    equitySub.textContent = tx("pos_invested_pct", "{pct}% invested", {
      pct: investedPct.toFixed(1),
    });
  }
  if (investedFill) {
    investedFill.style.width = `${Math.min(100, Math.max(0, investedPct))}%`;
  }

  if (mvEl) mvEl.textContent = money(data.total_market_value || 0);
  if (costEl) costEl.textContent = `${tx("total_cost_basis", "Cost Basis")} ${money(data.total_cost_basis || 0)}`;

  if (unrlEl) {
    const upl = Number(data.total_unrealized_pl || 0);
    unrlEl.textContent = formatPnl(upl);
    setPnlTone(unrlEl, upl);
  }
  // The percentage rides beside its figure as a tinted pill; the sub-line
  // below it names the yardstick (`vs cost basis` / `vs prior close`), which
  // is static markup the i18n pass owns.
  setChangePill(unrlPct, data.total_unrealized_pct);

  if (intraEl) {
    const ipl = Number(data.total_intraday_pl || 0);
    intraEl.textContent = formatPnl(ipl);
    setPnlTone(intraEl, ipl);
  }
  setChangePill(intraPct, data.total_intraday_pct);

  if (bpEl) bpEl.textContent = money(account.buying_power || 0);
  if (cashEl) cashEl.textContent = `${tx("cash", "Cash")} ${money(account.cash || 0)}`;

  if (countEl) countEl.textContent = String(positions.length);

  if (winlossPill) {
    winlossPill.hidden = positions.length === 0;
    if (winsEl) {
      winsEl.textContent = `${data.winners_count || 0}W`;
      winsEl.classList.toggle("is-active", posFilterPnl === "winners");
    }
    if (lossesEl) {
      lossesEl.textContent = `${data.losers_count || 0}L`;
      lossesEl.classList.toggle("is-active", posFilterPnl === "losers");
    }
  }

  // Auto Trade owning the book is the reason every close control is dead —
  // say so, instead of leaving a row of silently disabled buttons.
  const loopNotice = $("pos-loop-notice");
  if (loopNotice) loopNotice.hidden = !(loopRunning && positions.length > 0);
  if (loopRunning && posSelectedSymbols.size > 0) {
    posSelectedSymbols.clear();
  }

  // Liquidate all button
  const liquidateAllBtn = $("btn-liquidate-all");
  if (liquidateAllBtn) {
    liquidateAllBtn.disabled = positions.length === 0 || loopRunning;
  }

  // Portfolio Allocation Section
  renderPortfolioAllocation(positions, account);

  // Filter & Sort positions
  const filtered = filterAndSortPositions(positions);

  // Clean selected symbols set against available positions
  const validSymbols = new Set(positions.map((p) => p.symbol));
  for (const s of posSelectedSymbols) {
    if (!validSymbols.has(s)) posSelectedSymbols.delete(s);
  }

  // The count beside the heading tracks what is on screen, not what is held —
  // otherwise a filtered view reads as if nothing was filtered.
  if (totalCountEl) {
    totalCountEl.textContent =
      filtered.length === positions.length
        ? String(positions.length)
        : `${filtered.length} / ${positions.length}`;
  }

  // Render Table & Cards
  renderPositionsTable(filtered);
  renderPositionsCards(filtered);
  updateSelectAllHeaderState(filtered);
  updateSortHeadersUi();
  updateSelectedBatchButton();
  updateResetFiltersButton();

  // Empty state handling
  const tableWrap = $("pos-table-wrap");
  const cardsWrap = $("pos-cards-list");
  const emptyState = $("pos-empty-state");
  const emptyTitle = $("pos-empty-title");
  const emptyDesc = $("pos-empty-desc");
  const emptyActions = $("pos-empty-actions");
  const emptyClear = $("btn-empty-clear-filters");

  const hasHoldings = positions.length > 0;
  const hasMatches = filtered.length > 0;

  if (tableWrap) tableWrap.hidden = !hasMatches;
  if (cardsWrap) cardsWrap.hidden = !hasMatches;
  if (emptyState) emptyState.hidden = hasMatches;

  if (!hasMatches && emptyState) {
    const filtered_out = hasHoldings;
    if (emptyTitle) {
      emptyTitle.textContent = filtered_out
        ? tx("no_positions_found", "No positions matching filter")
        : tx("no_open_positions", "No open holdings");
    }
    if (emptyDesc) {
      emptyDesc.textContent = filtered_out
        ? tx("pos_no_match_hint", "No holding matches the current search, side, or status filter.")
        : tx("no_open_positions_desc", "You currently have no open holdings. Start a strategy loop or place an advanced order to open positions.");
    }
    // A filtered-empty blotter needs a way back, not a pitch to open trades.
    if (emptyActions) emptyActions.hidden = filtered_out;
    if (emptyClear) emptyClear.hidden = !filtered_out;
  }
}

/** Mirror the collapse state onto the disclosure button and the panel it
 *  opens. Runs on every render too, so a language switch relabels the button
 *  and an emptied book takes the control away with it. */
function syncAllocationToggle() {
  const section = $("pos-allocation-section");
  const btn = $("btn-toggle-allocation");
  const label = $("btn-toggle-allocation-text");

  // Nothing to break down on an empty book — the toggle would open a panel
  // with no bar in it, so it goes away along with the holdings.
  const hasPositions = (positionsData?.positions?.length || 0) > 0;
  const open = hasPositions && posAllocationOpen;

  const text = open
    ? tx("hide_allocation", "Hide allocation")
    : tx("show_allocation", "Show allocation");

  if (section) section.hidden = !open;
  if (btn) {
    btn.hidden = !hasPositions;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.title = text;
  }
  if (label) label.textContent = text;
}

function toggleAllocation() {
  posAllocationOpen = !posAllocationOpen;
  localStorage.setItem("desk_pos_allocation_open", posAllocationOpen ? "1" : "0");
  // Re-render rather than just unhide: the bar is only built while open, so
  // opening it has to fill it first.
  renderPortfolioAllocation(positionsData?.positions, positionsData?.account);
}

function renderPortfolioAllocation(positions, account) {
  const bar = $("pos-allocation-bar");
  const legend = $("pos-allocation-legend");
  const countBadge = $("pos-allocation-count");

  if (!bar || !legend) return;

  syncAllocationToggle();

  // Collapsed or empty: skip the DOM build entirely. This runs on every 10s
  // poll, and there is no reason to rebuild segments nobody can see —
  // toggleAllocation() calls back in when the panel opens.
  if (!posAllocationOpen || !positions || positions.length === 0) return;

  if (countBadge) countBadge.textContent = String(positions.length);

  bar.innerHTML = "";
  legend.innerHTML = "";

  let usedPct = 0;

  positions.forEach((pos, idx) => {
    const color = ALLOCATION_PALETTE[idx % ALLOCATION_PALETTE.length];
    // Show the true weight. Clamping tiny positions up to 1% made the bar lie
    // about concentration, which is the one thing this bar exists to show.
    const weight = Math.max(0, Math.min(100, Number(pos.allocation_pct) || 0));
    const isHighRisk = weight >= 30;
    usedPct += weight;

    const segment = document.createElement("div");
    segment.className = `pos-alloc-segment ${isHighRisk ? "high-risk" : ""}`;
    segment.style.width = `${weight}%`;
    segment.style.backgroundColor = color;
    segment.title = `${pos.symbol}: ${weight}% (${money(pos.market_value || 0)})${isHighRisk ? " — " + tx("high_concentration_hint", "Concentration >30%") : ""}`;
    segment.dataset.symbol = pos.symbol;
    bar.appendChild(segment);

    const legItem = document.createElement("button");
    legItem.type = "button";
    legItem.className = "pos-alloc-legend-item";
    legItem.dataset.symbol = pos.symbol;
    legItem.title = tx("pos_filter_to_symbol", "Filter the table to {symbol}", { symbol: pos.symbol });
    legItem.innerHTML = `
      <span class="pos-alloc-dot" style="background-color: ${color}"></span>
      <strong class="pos-alloc-sym">${escapeHtml(pos.symbol)}</strong>
      <span class="pos-alloc-weight ${isHighRisk ? "pos-weight-warn" : ""}">${weight}%${isHighRisk ? " ⚠️" : ""}</span>
    `;
    legend.appendChild(legItem);
  });

  // The remainder is uninvested equity, not necessarily account cash: margin,
  // unsettled funds, and shorts can make those figures diverge.
  const uninvestedPct = Math.max(0, 100 - usedPct);
  if (uninvestedPct > 0.01) {
    const uninvestedValue = Math.max(
      0,
      Number((account || {}).equity || 0) -
        positions.reduce((sum, pos) => sum + Math.abs(Number(pos.market_value) || 0), 0)
    );
    const uninvestedSeg = document.createElement("div");
    uninvestedSeg.className = "pos-alloc-segment is-cash";
    uninvestedSeg.style.width = `${uninvestedPct}%`;
    uninvestedSeg.title = `${tx("uninvested_equity", "Uninvested equity")}: ${uninvestedPct.toFixed(1)}% (${money(uninvestedValue)})`;
    bar.appendChild(uninvestedSeg);

    const uninvestedLegend = document.createElement("span");
    uninvestedLegend.className = "pos-alloc-legend-item is-cash";
    uninvestedLegend.innerHTML = `
      <span class="pos-alloc-dot is-cash"></span>
      <strong class="pos-alloc-sym">${escapeHtml(tx("uninvested_equity", "Uninvested equity"))}</strong>
      <span class="pos-alloc-weight">${uninvestedPct.toFixed(1)}%</span>
    `;
    legend.appendChild(uninvestedLegend);
  }
}

function filterAndSortPositions(positions) {
  if (!Array.isArray(positions)) return [];
  const q = String(posFilterSearch || "").trim().toUpperCase();

  const res = positions.filter((p) => {
    // Symbol only. Matching asset_class let "US_EQUITY" pull up every row,
    // which is never what someone typing in a symbol box wants.
    if (q && !String(p.symbol || "").toUpperCase().includes(q)) return false;
    if (posFilterSide !== "all" && p.side !== posFilterSide) return false;
    if (posFilterPnl === "winners" && Number(p.unrealized_pl || 0) <= 0) return false;
    if (posFilterPnl === "losers" && Number(p.unrealized_pl || 0) >= 0) return false;
    return true;
  });

  const dirMul = posSortDir === "asc" ? 1 : -1;

  res.sort((a, b) => {
    if (posSortKey === "symbol" || posSortKey === "side") {
      return String(a[posSortKey] || "").localeCompare(String(b[posSortKey] || "")) * dirMul;
    }
    const key = POS_SORT_KEYS.has(posSortKey) ? posSortKey : "market_value";
    const va = key === "market_value" ? Math.abs(Number(a[key] || 0)) : Number(a[key] || 0);
    const vb = key === "market_value" ? Math.abs(Number(b[key] || 0)) : Number(b[key] || 0);
    return (va - vb) * dirMul;
  });

  return res;
}

function setPosSort(key, { toggle = false } = {}) {
  if (!POS_SORT_KEYS.has(key)) return;
  if (toggle && posSortKey === key) {
    posSortDir = posSortDir === "asc" ? "desc" : "asc";
  } else {
    posSortKey = key;
    posSortDir = key === "symbol" || key === "side" ? "asc" : "desc";
  }
  // Density used to persist while sort did not; persist the one that actually
  // survives a reload as a preference.
  try {
    localStorage.setItem("desk_pos_sort_key", posSortKey);
    localStorage.setItem("desk_pos_sort_dir", posSortDir);
  } catch (e) {}
  if (positionsData) renderPositionsPage();
}

function updateSortHeadersUi() {
  document.querySelectorAll(".pos-th-sortable").forEach((th) => {
    const col = th.dataset.sortCol;
    const icon = th.querySelector(".pos-sort-icon");
    if (col === posSortKey) {
      const isAsc = posSortDir === "asc";
      th.setAttribute("aria-sort", isAsc ? "ascending" : "descending");
      if (icon) icon.textContent = isAsc ? "▲" : "▼";
    } else {
      th.setAttribute("aria-sort", "none");
      if (icon) icon.textContent = "↕";
    }
  });

  const sortSelect = $("pos-sort-select");
  if (sortSelect && sortSelect.value !== posSortKey) {
    if (Array.from(sortSelect.options).some((o) => o.value === posSortKey)) {
      sortSelect.value = posSortKey;
      if (typeof refreshNiceSelect === "function") refreshNiceSelect(sortSelect);
    }
  }
}

function posFiltersAreDefault() {
  return !posFilterSearch && posFilterSide === "all" && posFilterPnl === "all";
}

function updateResetFiltersButton() {
  const btn = $("btn-reset-pos-filters");
  if (btn) btn.disabled = posFiltersAreDefault();
}

function resetPosFilters() {
  posFilterSearch = "";
  posFilterSide = "all";
  posFilterPnl = "all";
  const searchInput = $("pos-search");
  if (searchInput) searchInput.value = "";
  const clearBtn = $("btn-clear-pos-search");
  if (clearBtn) clearBtn.hidden = true;
  syncFilterButtons("data-filter-side", "all");
  syncFilterButtons("data-filter-pnl", "all");
  if (positionsData) renderPositionsPage();
}

function syncFilterButtons(attr, value) {
  document.querySelectorAll(`[${attr}]`).forEach((b) => {
    const isOn = b.getAttribute(attr) === value;
    b.classList.toggle("is-active", isOn);
    b.setAttribute("aria-pressed", isOn ? "true" : "false");
  });
}

function updateSelectedBatchButton() {
  const btn = $("btn-liquidate-selected");
  const text = $("btn-liquidate-selected-text");
  const count = posSelectedSymbols.size;
  if (!btn) return;
  if (count > 0 && !loopRunning) {
    btn.hidden = false;
    if (text) text.textContent = `${tx("liquidate_selected", "Close Selected")} (${count})`;
  } else {
    btn.hidden = true;
  }
}

/** Shared protection cell: a stop price alone does not say whether the stop is
 *  a breath away or a crash away, so lead with the distance. */
function protectionMarkup(pos) {
  const parts = [];
  if (pos.has_stop_loss) {
    const px = pos.stop_loss_price != null ? `$${Number(pos.stop_loss_price).toFixed(2)}` : tx("protection", "SL");
    const dist = pos.stop_distance_pct;
    const distText = dist != null ? tx("pos_stop_distance", "{pct}% away", { pct: Math.abs(dist).toFixed(1) }) : "";
    const tone = dist != null && dist < 2 ? "sl-tight" : "sl";
    parts.push(
      `<span class="pos-prot-badge ${tone}" title="${escapeHtml(`${tx("protection", "Protection")} ${px} ${distText}`)}">🛡️ ${escapeHtml(px)}${distText ? `<em>${escapeHtml(distText)}</em>` : ""}</span>`
    );
  } else {
    parts.push(
      `<span class="pos-prot-badge no-sl" title="${escapeHtml(tx("no_protection_hint", "No stop loss active"))}">⚠️ ${escapeHtml(tx("no_stop_loss_short", "No SL"))}</span>`
    );
  }
  if (pos.has_take_profit && pos.take_profit_price != null) {
    const tp = `$${Number(pos.take_profit_price).toFixed(2)}`;
    parts.push(`<span class="pos-prot-badge tp" title="${escapeHtml(tx("take_profit", "Take profit"))} ${tp}">🎯 ${tp}</span>`);
  }
  // Resting orders hold shares back; a close that ignores them gets rejected.
  const openCount = Number(pos.open_orders_count || 0);
  if (openCount > 0) {
    const sym = String(pos.symbol || "");
    parts.push(
      `<a href="/orders?symbol=${encodeURIComponent(sym)}" class="pos-prot-badge orders" title="${escapeHtml(tx("pos_open_orders_hint", "Resting orders can hold shares back from a close"))}">${escapeHtml(tx("pos_open_orders", "{count} open", { count: openCount }))}</a>`
    );
  }
  return parts.join(" ");
}

function positionSideLabel(side) {
  return side === "short"
    ? tx("filter_short", "Short")
    : tx("filter_long", "Long");
}

/** Alpaca position quantities can carry up to 9 decimal places. */
function formatPositionQty(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString(undefined, { maximumFractionDigits: 9 });
}

function pnlPctSuffix(pct) {
  // formatPnlPct returns "" for null, which rendered as a bare "()".
  const text = formatPnlPct(pct);
  return text ? ` <small class="${Number(pct) >= 0 ? "pos" : "neg"}">(${text})</small>` : "";
}

function symbolManualOrderLink(symbol, extraClass = "", label = "") {
  const sym = String(symbol || "");
  const text = String(label || sym);
  const classes = ["pos-sym-link", extraClass].filter(Boolean).join(" ");
  return `<a href="${pagePath("manual-order")}?symbol=${encodeURIComponent(sym)}" class="${classes}" title="${escapeHtml(tx("trade_symbol", "Trade this symbol"))}">${escapeHtml(text)}</a>`;
}

function renderPositionsTable(positions) {
  const tbody = $("pos-table-body");
  if (!tbody) return;

  tbody.innerHTML = positions
    .map((pos) => {
      const sym = pos.symbol;
      const isSelected = posSelectedSymbols.has(sym);
      const side = String(pos.side || "long").toLowerCase();
      const upl = Number(pos.unrealized_pl || 0);
      const ipl = Number(pos.unrealized_intraday_pl || 0);
      const chg = pos.change_today;
      const qty = Number(pos.qty || 0);
      const qtyAvail = Number(pos.qty_available != null ? pos.qty_available : qty);
      const closeTitle = loopRunning
        ? tx("pos_loop_locked_short", "Stop the Auto Trade loop to close manually")
        : tx("close_position", "Close");

      return `
      <tr class="pos-table-row ${isSelected ? "is-selected" : ""}" data-symbol="${escapeHtml(sym)}" data-side="${escapeHtml(side)}">
        <td class="pos-cell-check">
          <input type="checkbox" class="pos-check-input pos-row-check" data-symbol="${escapeHtml(sym)}" ${isSelected ? "checked" : ""} ${loopRunning ? "disabled" : ""} aria-label="${escapeHtml(tx("pos_select_symbol", "Select {symbol}", { symbol: sym }))}" />
        </td>
        <td class="pos-cell-sym">
          <div class="pos-sym-group">
            ${symbolManualOrderLink(sym, "", pos.option_label || "")}
            ${pos.is_option ? `<small class="pos-option-tag">${escapeHtml(tx("option_contract", "Option"))}</small>` : ""}
          </div>
        </td>
        <td>
          <span class="side-badge ${side}">${escapeHtml(positionSideLabel(side))}</span>
        </td>
        <td class="pos-cell-qty pos-num mono">
          <strong>${formatPositionQty(qty)}</strong>
          ${qtyAvail < qty ? `<small class="pos-avail-note">${escapeHtml(tx("qty_available_short", "{qty} avail", { qty: formatPositionQty(qtyAvail) }))}</small>` : ""}
        </td>
        <td class="pos-cell-price pos-num mono">
          <div>$${Number(pos.current_price || 0).toFixed(2)}</div>
          ${chg != null ? `<small class="pos-chg-pill ${chg >= 0 ? "pos" : "neg"}">${formatPnlPct(chg)}</small>` : ""}
        </td>
        <td class="pos-cell-price pos-num mono">$${Number(pos.avg_entry_price || 0).toFixed(2)}</td>
        <td class="pos-cell-mv pos-num mono">
          <strong>${money(pos.market_value || 0)}</strong>
          <small class="pos-cost-sub">${escapeHtml(tx("cost_short", "Cost {value}", { value: money(pos.cost_basis || 0) }))}</small>
        </td>
        <td class="pos-cell-pnl pos-num mono">
          <strong class="${upl >= 0 ? "pos" : "neg"}">${formatPnl(upl)}</strong>
          ${pnlPctSuffix(pos.unrealized_pct)}
        </td>
        <td class="pos-cell-pnl pos-num mono">
          <strong class="${ipl >= 0 ? "pos" : "neg"}">${formatPnl(ipl)}</strong>
          ${pnlPctSuffix(pos.unrealized_intraday_pct)}
        </td>
        <!-- Flex lives on an inner wrapper, never on the td: display:flex on a
             table cell drops it out of the table layout and the row collapses. -->
        <td class="pos-cell-prot"><div class="pos-prot-stack">${protectionMarkup(pos)}</div></td>
        <td class="pos-cell-actions">
          <div class="pos-action-row">
            <button type="button" class="pos-act pos-act-close btn-pos-close" data-symbol="${escapeHtml(sym)}" ${loopRunning ? "disabled" : ""} title="${escapeHtml(closeTitle)}">
              ${escapeHtml(tx("close_position", "Close"))}
            </button>
            ${
              side === "short"
                ? `<a href="/manual-order?symbol=${encodeURIComponent(sym)}&side=cover" class="pos-act btn-pos-cover" title="${escapeHtml(tx("cover_position_hint", "Buy part of this short back"))}">
              ${escapeHtml(tx("cover_side", "Cover"))}
            </a>`
                : ""
            }
            <button type="button" class="pos-act btn-pos-lots" data-symbol="${escapeHtml(sym)}" title="${escapeHtml(tx("pos_lots_hint", "See the individual share lots behind this holding"))}">
              ${escapeHtml(tx("pos_lots_short", "Lots"))}
            </button>
            <a href="${historyHref({ symbol: sym })}" class="pos-act btn-pos-trade" title="${escapeHtml(tx("pos_view_fills", "View fills for this symbol"))}">
              ${escapeHtml(tx("nav_history", "History"))}
            </a>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

function updateSelectAllHeaderState(visiblePositions) {
  const selectAll = $("pos-select-all");
  if (!selectAll) return;
  if (!visiblePositions || visiblePositions.length === 0 || loopRunning) {
    selectAll.checked = false;
    selectAll.indeterminate = false;
    selectAll.disabled = true;
    return;
  }
  selectAll.disabled = false;
  const totalVisible = visiblePositions.length;
  const selectedVisible = visiblePositions.filter((p) => posSelectedSymbols.has(p.symbol)).length;

  selectAll.checked = selectedVisible === totalVisible;
  selectAll.indeterminate = selectedVisible > 0 && selectedVisible < totalVisible;
}

function renderPositionsCards(positions) {
  const container = $("pos-cards-list");
  if (!container) return;

  container.innerHTML = positions
    .map((pos) => {
      const sym = pos.symbol;
      const isSelected = posSelectedSymbols.has(sym);
      const side = String(pos.side || "long").toLowerCase();
      const upl = Number(pos.unrealized_pl || 0);
      const ipl = Number(pos.unrealized_intraday_pl || 0);
      const qty = Number(pos.qty || 0);

      return `
      <div class="pos-card ${isSelected ? "is-selected" : ""}" role="listitem" data-symbol="${escapeHtml(sym)}">
        <div class="pos-card-head">
          <div class="pos-card-sym-wrap">
            <input type="checkbox" class="pos-check-input pos-row-check" data-symbol="${escapeHtml(sym)}" ${isSelected ? "checked" : ""} ${loopRunning ? "disabled" : ""} aria-label="${escapeHtml(tx("pos_select_symbol", "Select {symbol}", { symbol: sym }))}" />
            ${symbolManualOrderLink(sym, "pos-card-sym", pos.option_label || "")}
            ${pos.is_option ? `<small class="pos-option-tag">${escapeHtml(tx("option_contract", "Option"))}</small>` : ""}
            <span class="side-badge ${side}">${escapeHtml(positionSideLabel(side))}</span>
          </div>
          <div class="pos-card-mv mono">
            <strong>${money(pos.market_value || 0)}</strong>
          </div>
        </div>
        <div class="pos-card-grid mono">
          <div>
            <span>${escapeHtml(tx("shares_qty", "Shares / Qty"))}</span>
            <strong>${formatPositionQty(qty)}</strong>
          </div>
          <div>
            <span>${escapeHtml(tx("current_price_label", "Current Price"))}</span>
            <strong>$${Number(pos.current_price || 0).toFixed(2)}</strong>
          </div>
          <div>
            <span>${escapeHtml(tx("avg_entry_label", "Avg Entry"))}</span>
            <strong>$${Number(pos.avg_entry_price || 0).toFixed(2)}</strong>
          </div>
          <div>
            <span>${escapeHtml(tx("unrealized_pnl", "Unrealized P&L"))}</span>
            <strong class="${upl >= 0 ? "pos" : "neg"}">${formatPnl(upl)}${formatPnlPct(pos.unrealized_pct) ? ` (${formatPnlPct(pos.unrealized_pct)})` : ""}</strong>
          </div>
          <div>
            <span>${escapeHtml(tx("intraday_pnl", "Today's P&L"))}</span>
            <strong class="${ipl >= 0 ? "pos" : "neg"}">${formatPnl(ipl)}</strong>
          </div>
          <div>
            <span>${escapeHtml(tx("protection", "Protection"))}</span>
            <span class="pos-card-prot">${protectionMarkup(pos)}</span>
          </div>
        </div>
        <div class="pos-card-actions">
          <button type="button" class="ghost ghost-danger btn-pos-close" data-symbol="${escapeHtml(sym)}" ${loopRunning ? "disabled" : ""}>
            ${escapeHtml(tx("close_position", "Close"))}
          </button>
          ${
            side === "short"
              ? `<a href="/manual-order?symbol=${encodeURIComponent(sym)}&side=cover" class="ghost">
            ${escapeHtml(tx("cover_side", "Cover"))}
          </a>`
              : ""
          }
          <button type="button" class="ghost btn-pos-lots" data-symbol="${escapeHtml(sym)}">
            ${escapeHtml(tx("pos_lots_short", "Lots"))}
          </button>
          <a href="${historyHref({ symbol: sym })}" class="ghost btn-pos-trade" title="${escapeHtml(tx("pos_view_fills", "View fills for this symbol"))}">
            ${escapeHtml(tx("nav_history", "History"))}
          </a>
        </div>
      </div>`;
    })
    .join("");
}

function findPositionBySymbol(symbol) {
  if (!positionsData || !Array.isArray(positionsData.positions)) return null;
  return positionsData.positions.find((p) => p.symbol === symbol) || null;
}

function openClosePositionModal(pos) {
  if (!pos) return;
  if (loopRunning) {
    showToast(tx("pos_loop_locked_short", "Stop the Auto Trade loop to close manually"), "error");
    return;
  }
  activeClosingPosition = pos;
  closePercentage = 100;
  closeQty = Number(pos.qty || 0);
  closeQtyRounded = false;

  const modal = $("pos-close-modal");
  if (!modal) return;

  const symBadge = $("pos-modal-symbol-badge");
  const sideBadge = $("pos-modal-side-badge");
  const heldQtyEl = $("pos-modal-held-qty");
  const avgEntryEl = $("pos-modal-avg-entry");
  const currPriceEl = $("pos-modal-curr-price");
  const unrlEl = $("pos-modal-unrealized-pnl");
  const qtyInput = $("pos-close-qty-input");
  const slider = $("pos-close-slider");
  const errEl = $("pos-modal-error");
  const cancelCheck = $("pos-cancel-single-orders-check");

  if (symBadge) symBadge.textContent = pos.symbol;
  if (sideBadge) {
    const side = String(pos.side || "long").toLowerCase();
    sideBadge.className = `side-badge ${side}`;
    sideBadge.textContent = positionSideLabel(side);
  }
  if (heldQtyEl) heldQtyEl.textContent = formatPositionQty(pos.qty);
  if (avgEntryEl) avgEntryEl.textContent = `$${Number(pos.avg_entry_price || 0).toFixed(2)}`;
  if (currPriceEl) currPriceEl.textContent = `$${Number(pos.current_price || 0).toFixed(2)}`;
  if (unrlEl) {
    const upl = Number(pos.unrealized_pl || 0);
    const pctText = formatPnlPct(pos.unrealized_pct);
    unrlEl.textContent = pctText ? `${formatPnl(upl)} (${pctText})` : formatPnl(upl);
    setPnlTone(unrlEl, upl);
  }

  if (slider) slider.value = "100";
  if (qtyInput) {
    qtyInput.max = String(pos.qty);
    qtyInput.value = String(pos.qty);
  }
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }
  if (cancelCheck) cancelCheck.checked = true;

  document.querySelectorAll(".pos-pct-chips .chip").forEach((c) => {
    const on = c.dataset.pct === "100";
    c.classList.toggle("is-active", on);
    c.setAttribute("aria-pressed", on ? "true" : "false");
  });

  updateCloseModalCalculations();
  openPosModal("pos-close-modal");
}

function closeClosePositionModal() {
  closePosModal("pos-close-modal");
  activeClosingPosition = null;
}

/** Close qty accepts up to 9 decimal places. Percentage controls request whole
 *  shares for a whole-share holding; a custom qty remains exact because an
 *  integer holding does not prove that the asset is non-fractionable. */
function normalizeCloseQty(rawQty, heldQty, { roundWhole = false } = {}) {
  const held = Number(heldQty || 0);
  let val = Number(rawQty);
  if (!Number.isFinite(val) || val < 0) val = 0;
  if (val > held) val = held;
  if (roundWhole && Number.isInteger(held)) {
    val = Math.floor(val);
    if (val < 1 && held >= 1) val = 1;
  } else {
    val = Number(val.toFixed(9));
  }
  return val;
}

function setCloseQty(rawQty, { fromInput = false, roundWhole = false } = {}) {
  if (!activeClosingPosition) return;
  const held = Number(activeClosingPosition.qty || 0);
  const requested = Math.min(held, Math.max(0, Number(rawQty) || 0));
  closeQty = normalizeCloseQty(rawQty, held, { roundWhole });
  closeQtyRounded = roundWhole && Math.abs(requested - closeQty) > 1e-9;
  closePercentage = held > 0 ? (closeQty / held) * 100 : 0;

  const qtyInput = $("pos-close-qty-input");
  const slider = $("pos-close-slider");
  if (qtyInput && (!fromInput || Number(qtyInput.value) !== closeQty)) {
    qtyInput.value = String(closeQty);
  }
  if (slider) slider.value = String(Math.max(1, Math.round(closePercentage)));

  document.querySelectorAll(".pos-pct-chips .chip").forEach((c) => {
    const on = Math.round(closePercentage) === Number(c.dataset.pct);
    c.classList.toggle("is-active", on);
    c.setAttribute("aria-pressed", on ? "true" : "false");
  });

  updateCloseModalCalculations();
}

function updateCloseModalCalculations() {
  if (!activeClosingPosition) return;
  const pos = activeClosingPosition;
  const currPx = Number(pos.current_price || 0);
  const entryPx = Number(pos.avg_entry_price || 0);
  const isShort = String(pos.side || "").toLowerCase() === "short";
  const held = Number(pos.qty || 0);
  const qtyAvail = Number(pos.qty_available != null ? pos.qty_available : held);

  const estProceeds = closeQty * currPx;
  const estPnl = isShort
    ? closeQty * (entryPx - currPx)
    : closeQty * (currPx - entryPx);

  const pctLabel = $("pos-modal-pct-label");
  const estProceedsEl = $("pos-modal-est-proceeds");
  const estPnlEl = $("pos-modal-est-pnl");
  const remainingEl = $("pos-modal-remaining");
  const hintEl = $("pos-modal-size-hint");
  const submitBtn = $("btn-close-modal-submit");
  const cancelCheck = $("pos-cancel-single-orders-check");
  const cancelDesc = $("pos-cancel-single-orders-desc");

  if (pctLabel) pctLabel.textContent = `${Math.round(closePercentage)}%`;
  if (estProceedsEl) estProceedsEl.textContent = money(estProceeds);
  if (estPnlEl) {
    estPnlEl.textContent = formatPnl(estPnl);
    setPnlTone(estPnlEl, estPnl);
  }

  // "How much am I left holding" is the question a partial close raises, and
  // the old modal never answered it.
  if (remainingEl) {
    const left = Math.max(0, held - closeQty);
    remainingEl.textContent =
      left > 0 ? tx("pos_remaining_after", "{qty} left after", { qty: formatPositionQty(left) }) : tx("pos_closes_fully", "closes the position");
  }

  const hints = [];
  if (closeQtyRounded) {
    hints.push(tx("pos_whole_shares_hint", "Percentage selection rounded down to whole shares."));
  }
  if (closeQty > qtyAvail && cancelCheck && !cancelCheck.checked) {
    hints.push(
      tx("pos_qty_held_hint", "Only {qty} shares are free; resting orders hold the rest. Keep 'Cancel resting open orders' on.", {
        qty: formatPositionQty(qtyAvail),
      })
    );
  }
  if (hintEl) {
    hintEl.hidden = hints.length === 0;
    hintEl.textContent = hints.join(" ");
  }

  // A partial close leaves the remainder exposed if its stop is cancelled too.
  if (cancelDesc) {
    cancelDesc.textContent =
      closePercentage < 100
        ? tx("cancel_resting_orders_partial_desc", "Cancels the stop on the remaining shares too — re-arm protection afterwards.")
        : tx("cancel_resting_orders_desc", "Also cancel any active stop loss or limit orders for these symbols.");
  }

  if (submitBtn) submitBtn.disabled = closeQty <= 0;
}

function closeResultFailed(result) {
  if (!result || typeof result !== "object") return false;
  if (result.ok === false) return true;
  const status = String(result.status || "").toLowerCase();
  if (["failed", "rejected", "canceled", "cancelled", "expired"].includes(status)) return true;
  const code = Number(status);
  return Number.isFinite(code) && code >= 400;
}

async function submitClosePosition() {
  if (!activeClosingPosition) return;
  const pos = activeClosingPosition;
  const submitBtn = $("btn-close-modal-submit");
  const errEl = $("pos-modal-error");
  const cancelOrders = !!$("pos-cancel-single-orders-check")?.checked;

  if (errEl) errEl.hidden = true;
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = tx("liquidating", "Liquidating…");
  }

  try {
    const payload = { cancel_orders: cancelOrders };
    if (closeQty > 0 && closeQty < Number(pos.qty || 0)) {
      payload.qty = closeQty;
    }

    const res = await fetch(`/api/positions/${encodeURIComponent(pos.symbol)}/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();
    if (closeResultFailed(data.result)) {
      const status = String(data.result?.status || "rejected");
      throw new Error(
        tx(
          "position_close_not_accepted",
          "Close order was not accepted ({status}). Refresh the position and try again.",
          { status }
        )
      );
    }
    closeClosePositionModal();
    posSelectedSymbols.delete(pos.symbol);
    showToast(
      tx("position_closed_toast", "{symbol} close order submitted", { symbol: pos.symbol }),
      "ok"
    );
    positionsData = data.overview;
    renderPositionsPage();
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = err.message || tx("error_close_position", "Failed to close position");
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = tx("confirm_liquidation", "Confirm liquidation");
    }
  }
}

/** A fill is a market event, so a lot is stamped in ET like every other
 *  timestamp on the desk. Dated by the viewer's own clock, the same fill would
 *  land on a different day here than it does on the History page. */
function formatLotDate(iso) {
  if (!iso) return tx("pos_lots_carried_short", "Earlier");
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return String(iso).slice(0, 10);
  try {
    return new Intl.DateTimeFormat(document.documentElement.lang || undefined, {
      timeZone: "America/New_York",
      day: "numeric",
      month: "short",
      year: "numeric",
    }).format(new Date(t));
  } catch (err) {
    return String(iso).slice(0, 10);
  }
}

/** The clock half of the stamp, kept separate so the date can lead the row and
 *  the time can ride the sub-line with the holding age. */
function formatLotTime(iso) {
  if (!iso) return "";
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return "";
  try {
    return new Intl.DateTimeFormat(document.documentElement.lang || undefined, {
      timeZone: "America/New_York",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short",
    }).format(new Date(t));
  } catch (err) {
    return "";
  }
}

function formatLotAge(days) {
  if (days == null || !Number.isFinite(Number(days))) return "";
  const v = Number(days);
  if (v < 1) return tx("pos_lots_age_today", "today");
  return tx("pos_lots_age_days", "{n}d held", { n: Math.round(v) });
}

function setLotsModalState({ loading = false, hasRows = false, empty = false } = {}) {
  const loadingEl = $("pos-lots-loading");
  const listWrap = $("pos-lots-list-wrap");
  const emptyEl = $("pos-lots-empty");
  if (loadingEl) loadingEl.hidden = !loading;
  if (listWrap) listWrap.hidden = !hasRows;
  if (emptyEl) emptyEl.hidden = !empty;
}

/** One row per parcel, laid out on a grid rather than a table: the same markup
 *  then serves the desktop columns and the stacked phone layout, so the two can
 *  never drift out of step the way a table and its card mirror do. */
function renderLotsRows(data) {
  activeLotsData = data;
  const list = $("pos-lots-list");
  const totals = $("pos-lots-totals");
  const lots = Array.isArray(data.lots) ? data.lots : [];
  const currentPx = Number(data.current_price || 0);

  // The weight bar is read against the largest parcel, not against 100% — a
  // book of five even lots would otherwise render as five identical stubs.
  const maxWeight = lots.reduce((acc, lot) => Math.max(acc, Number(lot.weight_pct || 0)), 0) || 100;

  if (list) {
    list.innerHTML = lots
      .map((lot) => {
        const upl = Number(lot.unrealized_pl || 0);
        const pct = Number(lot.unrealized_pct);
        const entry = Number(lot.price || 0);
        const weight = Number(lot.weight_pct || 0);
        const isSelected = activeLotsSelectedIndices.has(lot.index);
        const subLine = [formatLotTime(lot.opened_at), formatLotAge(lot.age_days)]
          .filter(Boolean)
          .join(" · ");
        const tone = upl > 0 ? "pos" : upl < 0 ? "neg" : "flat";
        // Where this parcel's entry sits against the live mark is the whole
        // question a lot list is opened to answer, so it gets its own line.
        const drift =
          entry > 0 && currentPx > 0
            ? tx("pos_lots_vs_mark", "mark {pct}", {
                pct: formatPnlPct(((currentPx - entry) / entry) * 100 * (data.side === "short" ? -1 : 1)),
              })
            : "";

        return `
        <div class="pos-lot ${lot.estimated ? "is-estimated" : ""} is-${tone} ${isSelected ? "is-selected" : ""}" data-lot-index="${lot.index}" role="listitem">
          <div class="pos-lot-when">
            <input type="checkbox" class="pos-check-input pos-lot-check" data-lot-index="${lot.index}" ${isSelected ? "checked" : ""} aria-label="${escapeHtml(tx("pos_lots_select_lot", "Select lot {index}", { index: lot.index }))}" />
            <span class="pos-lot-num">${escapeHtml(String(lot.index))}</span>
            <span class="pos-lot-when-text">
              <strong>${escapeHtml(formatLotDate(lot.opened_at))}</strong>
              ${
                lot.estimated
                  ? `<em class="pos-lot-tag" title="${escapeHtml(
                      tx("pos_lots_carried_hint", "Opened before the lookback window — priced at the blended average entry.")
                    )}">${escapeHtml(tx("pos_lots_carried_short", "Earlier"))}</em>`
                  : subLine
                  ? `<em>${escapeHtml(subLine)}</em>`
                  : ""
              }
            </span>
          </div>

          <div class="pos-lot-entry mono">
            <span class="pos-lot-label" data-lot-label>${escapeHtml(tx("pos_lots_entry", "Entry"))}</span>
            <strong>$${entry.toFixed(2)}</strong>
            ${drift ? `<em class="${upl >= 0 ? "pos" : "neg"}">${escapeHtml(drift)}</em>` : ""}
          </div>

          <div class="pos-lot-size">
            <span class="pos-lot-label" data-lot-label>${escapeHtml(tx("pos_lots_weight", "Weight"))}</span>
            <div class="pos-lot-bar" role="img" aria-label="${escapeHtml(
              tx("pos_lots_weight_aria", "{pct}% of the position", { pct: weight.toFixed(1) })
            )}">
              <span style="width: ${Math.max(2, (weight / maxWeight) * 100)}%"></span>
            </div>
            <span class="pos-lot-size-text mono">${formatPositionQty(lot.qty)} · ${weight.toFixed(1)}%</span>
          </div>

          <div class="pos-lot-value mono">
            <span class="pos-lot-label" data-lot-label>${escapeHtml(tx("total_market_value", "Market Value"))}</span>
            <strong>${money(lot.market_value || 0)}</strong>
            <em>${escapeHtml(tx("cost_short", "Cost {value}", { value: money(lot.cost_basis || 0) }))}</em>
          </div>

          <div class="pos-lot-pnl mono">
            <span class="pos-lot-label" data-lot-label>${escapeHtml(tx("unrealized_pnl", "Unrealized P&L"))}</span>
            <strong class="${upl >= 0 ? "pos" : "neg"}">${formatPnl(upl)}</strong>
            ${formatPnlPct(pct) ? `<em class="pos-chg-pill ${pct >= 0 ? "pos" : "neg"}">${formatPnlPct(pct)}</em>` : ""}
          </div>
        </div>`;
      })
      .join("");
  }

  if (totals) {
    const totalPl = Number(data.total_unrealized_pl || 0);
    totals.innerHTML = `
      <span>${escapeHtml(tx("pos_lots_total", "{count} lots", { count: lots.length }))}</span>
      <span class="mono">${formatPositionQty(data.total_qty)} ${escapeHtml(tx("shares_unit", "shares"))}</span>
      <span class="mono">${escapeHtml(
        tx("cost_short", "Cost {value}", { value: money(data.total_cost_basis || 0) })
      )}</span>
      <strong class="mono ${totalPl >= 0 ? "pos" : "neg"}">${formatPnl(totalPl)}</strong>
    `;
  }

  updateLotsSelectionUI();
}

function updateLotsSelectionUI() {
  const lots = Array.isArray(activeLotsData?.lots) ? activeLotsData.lots : [];
  const selectedLots = lots.filter((l) => activeLotsSelectedIndices.has(l.index));
  const selectedCount = selectedLots.length;
  const selectAllCheck = $("pos-lots-select-all");
  const selBar = $("pos-lots-selection-bar");
  const closeBtn = $("btn-lots-close-selected");
  const closeText = $("btn-lots-close-text");
  const confirmPanel = $("pos-lots-confirm-panel");

  // Update select all checkbox state
  if (selectAllCheck) {
    if (lots.length === 0 || selectedCount === 0) {
      selectAllCheck.checked = false;
      selectAllCheck.indeterminate = false;
    } else if (selectedCount === lots.length) {
      selectAllCheck.checked = true;
      selectAllCheck.indeterminate = false;
    } else {
      selectAllCheck.checked = false;
      selectAllCheck.indeterminate = true;
    }
  }

  // Update rows styling and check inputs
  document.querySelectorAll(".pos-lot").forEach((row) => {
    const idx = Number(row.dataset.lotIndex);
    const isSelected = activeLotsSelectedIndices.has(idx);
    row.classList.toggle("is-selected", isSelected);
    const check = row.querySelector(".pos-lot-check");
    if (check) check.checked = isSelected;
  });

  if (selectedCount === 0) {
    if (selBar) selBar.hidden = true;
    if (closeBtn) closeBtn.hidden = true;
    if (confirmPanel) confirmPanel.hidden = true;
    return;
  }

  const selectedQty = selectedLots.reduce((acc, l) => acc + Number(l.qty || 0), 0);
  const selectedCost = selectedLots.reduce((acc, l) => acc + Number(l.cost_basis || 0), 0);
  const selectedUpl = selectedLots.reduce((acc, l) => acc + Number(l.unrealized_pl || 0), 0);
  const selectedMv = selectedLots.reduce((acc, l) => acc + Number(l.market_value || 0), 0);

  if (selBar) {
    selBar.hidden = false;
    const countEl = $("pos-lots-sel-count");
    const qtyEl = $("pos-lots-sel-qty");
    const costEl = $("pos-lots-sel-cost");
    const pnlEl = $("pos-lots-sel-pnl");
    if (countEl) countEl.textContent = String(selectedCount);
    if (qtyEl) qtyEl.textContent = `${formatPositionQty(selectedQty)} ${tx("shares_unit", "shares")}`;
    if (costEl) costEl.textContent = tx("cost_short", "Cost {value}", { value: money(selectedCost) });
    if (pnlEl) {
      pnlEl.textContent = formatPnl(selectedUpl);
      pnlEl.className = `mono ${selectedUpl >= 0 ? "pos" : "neg"}`;
    }
  }

  if (closeBtn) {
    closeBtn.hidden = false;
    if (closeText) {
      closeText.textContent = tx(
        "pos_lots_close_selected",
        "Close selected ({count} lots · {qty} shares)",
        { count: selectedCount, qty: formatPositionQty(selectedQty) }
      );
    }
  }

  // Update confirmation panel values if currently visible
  if (confirmPanel && !confirmPanel.hidden) {
    fillLotsConfirmPanel({
      count: selectedCount,
      qty: selectedQty,
      proceeds: selectedMv,
      pnl: selectedUpl,
      lots: selectedLots,
    });
  }
}

function fillLotsConfirmPanel({ count, qty, proceeds, pnl, lots }) {
  const titleEl = $("pos-lots-confirm-title");
  const descEl = $("pos-lots-confirm-desc");
  const procEl = $("pos-lots-confirm-proceeds");
  const pnlEl = $("pos-lots-confirm-pnl");
  const fifoEl = $("pos-lots-confirm-fifo-note");
  const sym = activeLotsSymbol || "";

  if (titleEl) {
    titleEl.textContent = tx(
      "pos_lots_confirm_title",
      "Close {count} selected lots ({qty} shares) of {symbol}?",
      { count, qty: formatPositionQty(qty), symbol: sym }
    );
  }
  if (descEl) {
    descEl.textContent = tx(
      "pos_lots_confirm_desc",
      "This will submit a market order to liquidate {qty} shares.",
      { qty: formatPositionQty(qty) }
    );
  }
  if (procEl) procEl.textContent = money(proceeds || 0);
  if (pnlEl) {
    pnlEl.textContent = formatPnl(pnl || 0);
    pnlEl.className = `${pnl >= 0 ? "pos" : "neg"}`;
  }
  if (fifoEl) {
    const isOldestPrefix = Array.isArray(lots) && lots.every((l, i) => l.index === i + 1);
    fifoEl.hidden = isOldestPrefix;
  }
}

function handleLotsSelectAllToggle(e) {
  const lots = Array.isArray(activeLotsData?.lots) ? activeLotsData.lots : [];
  if (e.target.checked) {
    lots.forEach((l) => activeLotsSelectedIndices.add(l.index));
  } else {
    activeLotsSelectedIndices.clear();
  }
  updateLotsSelectionUI();
}

function handleLotsClearSelection() {
  activeLotsSelectedIndices.clear();
  updateLotsSelectionUI();
}

function handleLotsListClick(e) {
  const row = e.target.closest(".pos-lot");
  if (!row) return;
  const idx = Number(row.dataset.lotIndex);
  if (!idx) return;

  if (e.target.matches(".pos-lot-check")) {
    if (e.target.checked) {
      activeLotsSelectedIndices.add(idx);
    } else {
      activeLotsSelectedIndices.delete(idx);
    }
  } else {
    // Clicking anywhere on the row toggles selection
    if (activeLotsSelectedIndices.has(idx)) {
      activeLotsSelectedIndices.delete(idx);
    } else {
      activeLotsSelectedIndices.add(idx);
    }
  }
  updateLotsSelectionUI();
}

function handleLotsCloseSelectedClick() {
  if (loopRunning) {
    showToast(tx("pos_loop_locked_short", "Stop the Auto Trade loop to close manually"), "error");
    return;
  }
  const lots = Array.isArray(activeLotsData?.lots) ? activeLotsData.lots : [];
  const selectedLots = lots.filter((l) => activeLotsSelectedIndices.has(l.index));
  if (selectedLots.length === 0) return;

  const selectedQty = selectedLots.reduce((acc, l) => acc + Number(l.qty || 0), 0);
  const selectedUpl = selectedLots.reduce((acc, l) => acc + Number(l.unrealized_pl || 0), 0);
  const selectedMv = selectedLots.reduce((acc, l) => acc + Number(l.market_value || 0), 0);

  const confirmPanel = $("pos-lots-confirm-panel");
  const errEl = $("pos-lots-confirm-error");
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }

  fillLotsConfirmPanel({
    count: selectedLots.length,
    qty: selectedQty,
    proceeds: selectedMv,
    pnl: selectedUpl,
    lots: selectedLots,
  });

  if (confirmPanel) {
    confirmPanel.hidden = false;
    confirmPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function handleLotsConfirmCancelClick() {
  const confirmPanel = $("pos-lots-confirm-panel");
  if (confirmPanel) confirmPanel.hidden = true;
}

async function submitCloseSelectedLots() {
  if (loopRunning) {
    showToast(tx("pos_loop_locked_short", "Stop the Auto Trade loop to close manually"), "error");
    return;
  }
  const sym = activeLotsSymbol;
  if (!sym) return;

  const lots = Array.isArray(activeLotsData?.lots) ? activeLotsData.lots : [];
  const selectedLots = lots.filter((l) => activeLotsSelectedIndices.has(l.index));
  if (selectedLots.length === 0) return;

  const selectedQty = selectedLots.reduce((acc, l) => acc + Number(l.qty || 0), 0);
  const totalHeldQty = Number(activeLotsData?.qty || 0);
  const cancelOrders = !!$("pos-lots-cancel-orders-check")?.checked;
  const submitBtn = $("btn-lots-confirm-submit");
  const errEl = $("pos-lots-confirm-error");

  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = tx("liquidating", "Liquidating…");
  }

  try {
    const payload = { cancel_orders: cancelOrders };
    if (selectedQty > 0 && selectedQty < totalHeldQty) {
      payload.qty = selectedQty;
    }

    const res = await fetch(`/api/positions/${encodeURIComponent(sym)}/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();
    if (closeResultFailed(data.result)) {
      const status = String(data.result?.status || "rejected");
      throw new Error(
        tx(
          "position_close_not_accepted",
          "Close order was not accepted ({status}). Refresh the position and try again.",
          { status }
        )
      );
    }

    showToast(
      tx("position_closed_toast", "{symbol} close order submitted", { symbol: sym }),
      "ok"
    );

    positionsData = data.overview;
    renderPositionsPage();

    if (selectedQty >= totalHeldQty || !findPositionBySymbol(sym)) {
      closeLotsModal();
    } else {
      activeLotsSelectedIndices.clear();
      const confirmPanel = $("pos-lots-confirm-panel");
      if (confirmPanel) confirmPanel.hidden = true;
      await openLotsModal(sym);
    }
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = err.message || tx("error_close_position", "Failed to close position");
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = tx("confirm_liquidation", "Confirm liquidation");
    }
  }
}

/** Reconstruction caveats belong on screen: a lot list the user cannot trust
 *  blindly is still useful, a silently incomplete one is not. */
function renderLotsNote(data) {
  const noteEl = $("pos-lots-note");
  if (!noteEl) return;
  const notes = [];
  if (Number(data.estimated_qty || 0) > 0) {
    notes.push(
      tx(
        "pos_lots_carried_note",
        "{qty} shares were bought before the {days}-day lookback; they are shown as one lot at the blended average entry.",
        { qty: formatPositionQty(data.estimated_qty), days: data.lookback_days }
      )
    );
  }
  if (data.window_truncated) {
    notes.push(tx("pos_lots_truncated_note", "Alpaca returned a full page of orders — older fills may be missing."));
  }
  noteEl.hidden = notes.length === 0;
  noteEl.textContent = notes.join(" ");
}

async function openLotsModal(symbol) {
  const sym = String(symbol || "").toUpperCase();
  if (!sym) return;
  const pos = findPositionBySymbol(sym);
  activeLotsSymbol = sym;
  activeLotsSelectedIndices.clear();
  activeLotsData = null;
  const seq = (lotsRequestSeq += 1);

  const symBadge = $("pos-lots-symbol-badge");
  const sideBadge = $("pos-lots-side-badge");
  const errEl = $("pos-lots-error");
  const noteEl = $("pos-lots-note");
  const historyLink = $("pos-lots-history-link");
  const confirmPanel = $("pos-lots-confirm-panel");
  const confirmErr = $("pos-lots-confirm-error");
  const selBar = $("pos-lots-selection-bar");
  const closeBtn = $("btn-lots-close-selected");
  const selectAllCheck = $("pos-lots-select-all");

  if (symBadge) symBadge.textContent = sym;
  if (sideBadge) {
    const side = String(pos?.side || "long").toLowerCase();
    sideBadge.className = `side-badge ${side}`;
    sideBadge.textContent = side.toUpperCase();
  }
  if (historyLink) historyLink.href = historyHref({ symbol: sym, side: "" });
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }
  if (noteEl) noteEl.hidden = true;
  if (confirmPanel) confirmPanel.hidden = true;
  if (confirmErr) {
    confirmErr.hidden = true;
    confirmErr.textContent = "";
  }
  if (selBar) selBar.hidden = true;
  if (closeBtn) closeBtn.hidden = true;
  if (selectAllCheck) {
    selectAllCheck.checked = false;
    selectAllCheck.indeterminate = false;
  }

  // Seed the header from the row that was clicked so the dialog is never blank
  // while the fill window is being walked.
  fillLotsSummary({
    lot_count: null,
    qty: pos?.qty,
    avg_entry_price: pos?.avg_entry_price,
    current_price: pos?.current_price,
  });
  setLotsModalState({ loading: true });
  openPosModal("pos-lots-modal");

  try {
    const res = await fetch(`/api/positions/${encodeURIComponent(sym)}/lots`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    // A second symbol clicked while this was in flight must win.
    if (seq !== lotsRequestSeq || activeLotsSymbol !== sym) return;
    fillLotsSummary(data);
    renderLotsRows(data);
    renderLotsNote(data);
    setLotsModalState({ hasRows: data.lots.length > 0, empty: data.lots.length === 0 });
  } catch (err) {
    if (seq !== lotsRequestSeq) return;
    setLotsModalState({});
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = err.message || tx("pos_lots_error", "Could not rebuild the share lots");
    }
  }
}

function fillLotsSummary(data) {
  const countEl = $("pos-lots-count");
  const qtyEl = $("pos-lots-total-qty");
  const avgEl = $("pos-lots-avg-entry");
  const pxEl = $("pos-lots-curr-price");
  const pnlEl = $("pos-lots-total-pnl");
  if (countEl) countEl.textContent = data.lot_count == null ? "—" : String(data.lot_count);
  if (qtyEl) qtyEl.textContent = formatPositionQty(data.qty);
  if (avgEl) avgEl.textContent = data.avg_entry_price != null ? `$${Number(data.avg_entry_price).toFixed(2)}` : "—";
  if (pxEl) pxEl.textContent = data.current_price != null ? `$${Number(data.current_price).toFixed(2)}` : "—";
  if (pnlEl) {
    // Absent until the lots are actually back — a bare $0.00 on a live holding
    // reads as "flat", which is a claim this dialog has not earned yet.
    if (data.total_unrealized_pl == null) {
      pnlEl.textContent = "—";
      setPnlTone(pnlEl, null);
    } else {
      const total = Number(data.total_unrealized_pl);
      pnlEl.textContent = formatPnl(total);
      setPnlTone(pnlEl, total);
    }
  }
}

function closeLotsModal() {
  closePosModal("pos-lots-modal");
  activeLotsSymbol = null;
  activeLotsData = null;
  activeLotsSelectedIndices.clear();
  const confirmPanel = $("pos-lots-confirm-panel");
  if (confirmPanel) confirmPanel.hidden = true;
  const selBar = $("pos-lots-selection-bar");
  if (selBar) selBar.hidden = true;
  const closeBtn = $("btn-lots-close-selected");
  if (closeBtn) closeBtn.hidden = true;
  const selectAllCheck = $("pos-lots-select-all");
  if (selectAllCheck) {
    selectAllCheck.checked = false;
    selectAllCheck.indeterminate = false;
  }
}

function renderLiquidatePreview(container, list) {
  if (!container) return;
  container.innerHTML = list
    .map(
      (pos) => `
      <div class="pos-liquidate-row">
        <strong>${escapeHtml(pos.symbol)}</strong>
        <span>${formatPositionQty(pos.qty)} ${escapeHtml(tx("shares_unit", "shares"))}</span>
        <span class="mono">${money(pos.market_value || 0)}</span>
        <span class="mono ${Number(pos.unrealized_pl || 0) >= 0 ? "pos" : "neg"}">${formatPnl(pos.unrealized_pl || 0)}</span>
      </div>`
    )
    .join("");
}

/** Both bulk dialogs answer the same question — "what am I about to give up" —
 *  so they share one summary line instead of drifting apart. */
function renderLiquidateTotals(el, list) {
  if (!el) return;
  const mv = list.reduce((acc, p) => acc + Math.abs(Number(p.market_value || 0)), 0);
  const pl = list.reduce((acc, p) => acc + Number(p.unrealized_pl || 0), 0);
  el.innerHTML = `
    <span>${escapeHtml(tx("pos_liquidate_total", "{count} positions", { count: list.length }))}</span>
    <strong class="mono">${money(mv)}</strong>
    <strong class="mono ${pl >= 0 ? "pos" : "neg"}">${formatPnl(pl)}</strong>
  `;
}

function openLiquidateSelectedModal() {
  if (!positionsData || !Array.isArray(positionsData.positions) || posSelectedSymbols.size === 0) {
    return;
  }
  const errEl = $("pos-liquidate-selected-error");
  const titleEl = $("pos-liquidate-selected-title");

  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }

  const selectedPositions = positionsData.positions.filter((p) => posSelectedSymbols.has(p.symbol));
  if (titleEl) {
    titleEl.textContent = `${tx("confirm_close_selected_title", "Liquidate selected positions")} (${selectedPositions.length})`;
  }

  renderLiquidatePreview($("pos-liquidate-selected-preview"), selectedPositions);
  renderLiquidateTotals($("pos-liquidate-selected-totals"), selectedPositions);

  openPosModal("pos-liquidate-selected-modal");
}

function closeLiquidateSelectedModal() {
  closePosModal("pos-liquidate-selected-modal");
}

async function submitLiquidateSelected() {
  const submitBtn = $("btn-liquidate-selected-confirm");
  const errEl = $("pos-liquidate-selected-error");
  const cancelOrders = !!$("pos-cancel-selected-orders-check")?.checked;
  const symbols = Array.from(posSelectedSymbols);

  if (symbols.length === 0) return;

  if (errEl) errEl.hidden = true;
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = tx("liquidating", "Liquidating…");
  }

  try {
    const res = await fetch("/api/positions/close-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols, cancel_orders: cancelOrders }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();
    closeLiquidateSelectedModal();
    posSelectedSymbols.clear();
    const results = data.result?.results || [];
    const failed = results.filter(closeResultFailed);
    const submitted = Math.max(0, results.length - failed.length);
    positionsData = data.overview;
    renderPositionsPage();
    // A batch can partly fail; reporting a flat success hid the survivors.
    if (failed.length > 0) {
      showToast(
        tx("pos_liquidate_partial", "{ok} submitted, {failed} failed — check the remaining rows", {
          ok: submitted,
          failed: failed.length,
        }),
        "error"
      );
    } else {
      showToast(
        tx("selected_positions_closed_toast", "{count} selected close order(s) submitted", { count: submitted }),
        "ok"
      );
    }
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = err.message || tx("error_liquidate_selected", "Failed to liquidate selected positions");
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = tx("liquidate_selected", "Close Selected");
    }
  }
}

function openLiquidateAllModal() {
  if (!positionsData || !Array.isArray(positionsData.positions) || positionsData.positions.length === 0) {
    return;
  }
  const errEl = $("pos-liquidate-all-error");

  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }

  renderLiquidatePreview($("pos-liquidate-list-preview"), positionsData.positions);
  renderLiquidateTotals($("pos-liquidate-all-totals"), positionsData.positions);

  openPosModal("pos-liquidate-all-modal");
}

function closeLiquidateAllModal() {
  closePosModal("pos-liquidate-all-modal");
}

async function submitLiquidateAll() {
  const submitBtn = $("btn-liquidate-all-confirm");
  const errEl = $("pos-liquidate-all-error");
  const cancelOrders = !!$("pos-cancel-orders-check")?.checked;

  if (errEl) errEl.hidden = true;
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = tx("liquidating", "Liquidating…");
  }

  try {
    const res = await fetch("/api/positions/close-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cancel_orders: cancelOrders }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();
    closeLiquidateAllModal();
    posSelectedSymbols.clear();
    const results = data.result?.results || [];
    const failed = results.filter(closeResultFailed);
    const submitted = Math.max(0, results.length - failed.length);
    positionsData = data.overview;
    renderPositionsPage();
    if (failed.length > 0) {
      showToast(
        tx("pos_liquidate_partial", "{ok} submitted, {failed} failed — check the remaining rows", {
          ok: submitted,
          failed: failed.length,
        }),
        "error"
      );
    } else {
      showToast(
        tx("all_positions_closed_toast", "{count} close orders submitted", { count: submitted }),
        "ok"
      );
    }
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = err.message || tx("error_liquidate_all", "Failed to liquidate all positions");
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = tx("liquidate_all", "Liquidate all");
    }
  }
}

function toggleSymbolSelection(symbol, checked) {
  if (checked) posSelectedSymbols.add(symbol);
  else posSelectedSymbols.delete(symbol);
  document
    .querySelectorAll(`.pos-row-check[data-symbol="${CSS.escape(symbol)}"]`)
    .forEach((box) => {
      box.checked = checked;
      box.closest(".pos-table-row, .pos-card")?.classList.toggle("is-selected", checked);
    });
  if (positionsData) {
    updateSelectAllHeaderState(filterAndSortPositions(positionsData.positions));
  }
  updateSelectedBatchButton();
}

/** One delegated handler per container. The old code re-bound a listener to
 *  every checkbox and button on every render — which, with a poll behind it,
 *  meant tearing down and rebuilding the listener graph continuously. */
function bindRowDelegation(container) {
  if (!container) return;
  container.addEventListener("change", (e) => {
    const check = e.target.closest(".pos-row-check");
    if (!check) return;
    toggleSymbolSelection(check.dataset.symbol, check.checked);
  });
  container.addEventListener("click", (e) => {
    const lotsBtn = e.target.closest(".btn-pos-lots");
    if (lotsBtn) {
      openLotsModal(lotsBtn.dataset.symbol).catch(() => {});
      return;
    }
    const btn = e.target.closest(".btn-pos-close");
    if (!btn || btn.disabled) return;
    openClosePositionModal(findPositionBySymbol(btn.dataset.symbol));
  });
}

/** Backdrop and Escape share one exit path per dialog, so each one still gets
 *  its own teardown instead of being hidden with its state left behind. */
function dismissPosModal(id) {
  if (id === "pos-close-modal") closeClosePositionModal();
  else if (id === "pos-lots-modal") closeLotsModal();
  else closePosModal(id);
}

function initPositionsUi() {
  startPosSyncTimer();

  // Search input
  const searchInput = $("pos-search");
  const clearSearchBtn = $("btn-clear-pos-search");
  if (searchInput) {
    searchInput.addEventListener("input", () => {
      posFilterSearch = searchInput.value;
      if (clearSearchBtn) clearSearchBtn.hidden = !posFilterSearch;
      if (positionsData) renderPositionsPage();
    });
  }
  if (clearSearchBtn) {
    clearSearchBtn.addEventListener("click", () => {
      if (searchInput) searchInput.value = "";
      posFilterSearch = "";
      clearSearchBtn.hidden = true;
      if (positionsData) renderPositionsPage();
      searchInput?.focus();
    });
  }

  // Side Filter
  document.querySelectorAll("[data-filter-side]").forEach((btn) => {
    btn.addEventListener("click", () => {
      posFilterSide = btn.dataset.filterSide;
      syncFilterButtons("data-filter-side", posFilterSide);
      if (positionsData) renderPositionsPage();
    });
  });

  // PnL Status Filter
  document.querySelectorAll("[data-filter-pnl]").forEach((btn) => {
    btn.addEventListener("click", () => {
      posFilterPnl = btn.dataset.filterPnl;
      syncFilterButtons("data-filter-pnl", posFilterPnl);
      if (positionsData) renderPositionsPage();
    });
  });

  // The W/L counters read as a summary but sit next to a Winners/Losers
  // filter — wire them to it so the number is also the way in.
  const applyPnlFilter = (value) => {
    posFilterPnl = posFilterPnl === value ? "all" : value;
    syncFilterButtons("data-filter-pnl", posFilterPnl);
    if (positionsData) renderPositionsPage();
  };
  $("pos-kpi-wins")?.addEventListener("click", () => applyPnlFilter("winners"));
  $("pos-kpi-losses")?.addEventListener("click", () => applyPnlFilter("losers"));

  $("btn-reset-pos-filters")?.addEventListener("click", resetPosFilters);
  $("btn-empty-clear-filters")?.addEventListener("click", resetPosFilters);

  // Clickable Sort Table Headers
  document.querySelectorAll(".pos-th-sortable").forEach((th) => {
    th.addEventListener("click", () => setPosSort(th.dataset.sortCol, { toggle: true }));
  });

  // Sort Select — the only sort control once the table collapses to cards
  const sortSelect = $("pos-sort-select");
  if (sortSelect) {
    sortSelect.value = posSortKey;
    sortSelect.addEventListener("change", () => setPosSort(sortSelect.value));
  }

  // Select All Header Checkbox
  const selectAll = $("pos-select-all");
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      if (!positionsData || !Array.isArray(positionsData.positions)) return;
      const filtered = filterAndSortPositions(positionsData.positions);
      filtered.forEach((p) => {
        if (selectAll.checked) posSelectedSymbols.add(p.symbol);
        else posSelectedSymbols.delete(p.symbol);
      });
      renderPositionsPage();
    });
  }

  bindRowDelegation($("pos-table-body"));
  bindRowDelegation($("pos-cards-list"));

  // Allocation disclosure, which lives on the equity card beside the
  // `% invested` line it expands.
  $("btn-toggle-allocation")?.addEventListener("click", toggleAllocation);
  syncAllocationToggle();

  // The legend buttons filter the table. The bar stays a read-only image; its
  // tiny segments are not viable keyboard or touch targets.
  const focusSymbol = (symbol) => {
    if (!symbol || !searchInput) return;
    searchInput.value = symbol;
    posFilterSearch = symbol;
    if (clearSearchBtn) clearSearchBtn.hidden = false;
    renderPositionsPage();
    $("positions-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  $("pos-allocation-legend")?.addEventListener("click", (e) => {
    focusSymbol(e.target.closest(".pos-alloc-legend-item")?.dataset.symbol);
  });

  $("btn-refresh-positions")?.addEventListener("click", () => {
    refreshPositions({ quiet: false }).catch(() => {});
  });
  $("btn-liquidate-selected")?.addEventListener("click", openLiquidateSelectedModal);
  $("btn-liquidate-all")?.addEventListener("click", openLiquidateAllModal);

  // Close Modal Percentage Chips
  document.querySelectorAll(".pos-pct-chips .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!activeClosingPosition) return;
      const pct = Number(chip.dataset.pct || 100);
      setCloseQty((Number(activeClosingPosition.qty || 0) * pct) / 100, {
        roundWhole: true,
      });
    });
  });

  $("pos-close-slider")?.addEventListener("input", (e) => {
    if (!activeClosingPosition) return;
    const pct = Number(e.target.value);
    setCloseQty((Number(activeClosingPosition.qty || 0) * pct) / 100, {
      roundWhole: true,
    });
  });

  $("pos-close-qty-input")?.addEventListener("input", (e) => {
    setCloseQty(e.target.value, { fromInput: true });
  });
  // Snap the field to the value that will actually be sent once editing stops.
  $("pos-close-qty-input")?.addEventListener("blur", () => setCloseQty(closeQty));

  $("pos-cancel-single-orders-check")?.addEventListener("change", updateCloseModalCalculations);

  // Modal Actions
  $("btn-close-modal-x")?.addEventListener("click", closeClosePositionModal);
  $("btn-close-modal-cancel")?.addEventListener("click", closeClosePositionModal);
  $("btn-close-modal-submit")?.addEventListener("click", submitClosePosition);

  $("btn-lots-modal-x")?.addEventListener("click", closeLotsModal);
  $("btn-lots-modal-close")?.addEventListener("click", closeLotsModal);
  $("pos-lots-select-all")?.addEventListener("change", handleLotsSelectAllToggle);
  $("btn-lots-clear-selection")?.addEventListener("click", handleLotsClearSelection);
  $("pos-lots-list")?.addEventListener("click", handleLotsListClick);
  $("btn-lots-close-selected")?.addEventListener("click", handleLotsCloseSelectedClick);
  $("btn-lots-confirm-cancel")?.addEventListener("click", handleLotsConfirmCancelClick);
  $("btn-lots-confirm-submit")?.addEventListener("click", submitCloseSelectedLots);

  $("btn-liquidate-selected-x")?.addEventListener("click", closeLiquidateSelectedModal);
  $("btn-liquidate-selected-cancel")?.addEventListener("click", closeLiquidateSelectedModal);
  $("btn-liquidate-selected-confirm")?.addEventListener("click", submitLiquidateSelected);

  $("btn-liquidate-all-x")?.addEventListener("click", closeLiquidateAllModal);
  $("btn-liquidate-all-cancel")?.addEventListener("click", closeLiquidateAllModal);
  $("btn-liquidate-all-confirm")?.addEventListener("click", submitLiquidateAll);

  // Clicking the backdrop dismisses, matching every other dialog on the desk.
  POS_MODAL_IDS.forEach((id) => {
    $(id)?.addEventListener("mousedown", (e) => {
      if (e.target === e.currentTarget) dismissPosModal(id);
    });
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      // Close only the dialog on top; blanket-closing all of them dismissed a
      // confirmation the user never saw.
      const top = topmostOpenPosModal();
      if (!top) return;
      dismissPosModal(top.id);
      return;
    }
    trapPosModalFocus(e);
  });

  // A background tab does not need live quotes; resume on return.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      setPosSyncState("live");
      refreshPositions({ quiet: true }).catch(() => {});
    } else {
      setPosSyncState("paused");
    }
  });
}


// Page Initialization
initPositionsUi();
refreshStatus({ forceSettings: true })
  .catch((err) => showToast(err.message, "error"))
  .finally(() => refreshPositions().catch(() => {}));

function onDeskStatusInterval() {
  // The desk status poll fires every 2s. Positions is a three-call Alpaca
  // round trip, so throttle it, and stand down while a dialog is open or the
  // tab is hidden.
  if (positionsBusy) return;
  if (document.visibilityState !== "visible") return;
  if (isAnyPosModalOpen()) return;
  if (Date.now() - posLastFetchStartedAt < POS_REFRESH_MS) return;
  refreshPositions({ quiet: true }).catch(() => {});
}

let posLastLoopRunning = null;

function onDeskStatusUpdate(state) {
  // loopRunning gates every close control, so a loop started or stopped on
  // another page has to reach this one. Only re-render on an actual change —
  // this fires every 2s, and rebuilding the table that often would tear the
  // rows out from under the pointer.
  const running = !!state?.loop_running;
  if (posLastLoopRunning === running) return;
  posLastLoopRunning = running;
  if (positionsData) renderPositionsPage();
}

function onDeskLanguageChange() {
  renderPositionsPage();
}
