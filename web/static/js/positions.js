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
// Cards rebuild from scratch on every 10s background poll — tracked here
// instead of read off the DOM so an expanded actions row doesn't silently
// re-collapse under the user mid-poll.
let posExpandedCardActions = new Set();
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
let posPrevTicks = new Map();

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
  "pos-exit-modal",
  "pos-autotrade-modal",
];

let activeExitPosition = null;
let activeExitMode = "stop_loss";
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
  document.body.style.overflow = "hidden";
  const focusTarget =
    modal.querySelector("input:not([type=hidden]):not([disabled])") ||
    modal.querySelector(".pos-modal-body button:not([disabled])") ||
    modal.querySelector(
      "button:not([disabled]):not(.pos-modal-close), select, [href], [tabindex]:not([tabindex='-1'])"
    ) ||
    modal.querySelector("button, [tabindex]");
  if (focusTarget) focusTarget.focus();
}

function closePosModal(id) {
  const modal = $(id);
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  if (!topmostOpenPosModal()) {
    document.body.style.overflow = "";
  }
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
    const isLive =
      data.trading_mode != null
        ? data.trading_mode === "live"
        : data.paper != null
          ? data.paper === false
          : (typeof isLiveEnv === "function"
              ? isLiveEnv()
              : Boolean(
                  document.body?.classList?.contains("is-live-env") ||
                  document.body?.dataset?.tradingMode === "live"
                ));
    if (isLive) {
      modeBadge.className = "mode-badge env-live";
      modeBadge.dataset.i18n = "live_armed";
      modeBadge.textContent = tx("live_armed", "Live · Orders on");
    } else {
      modeBadge.className = "mode-badge armed";
      modeBadge.dataset.i18n = "paper_trading";
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
  for (const s of posExpandedCardActions) {
    if (!validSymbols.has(s)) posExpandedCardActions.delete(s);
  }

  // The count beside the heading tracks what is on screen, not what is held —
  // otherwise a filtered view reads as if nothing was filtered.
  if (totalCountEl) {
    totalCountEl.textContent =
      filtered.length === positions.length
        ? String(positions.length)
        : `${filtered.length} / ${positions.length}`;
  }

  // Render Active Multi Auto-Trades Banner
  renderActiveAutoTradesBanner(data);

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

  // Mobile keeps the filters bar collapsed behind a toggle, so the badge is
  // the only hint left that a filter is narrowing the list underneath it.
  const badge = $("pos-filters-active-count");
  if (badge) {
    const count =
      (posFilterSearch ? 1 : 0) +
      (posFilterSide !== "all" ? 1 : 0) +
      (posFilterPnl !== "all" ? 1 : 0);
    badge.textContent = String(count);
    badge.hidden = count === 0;
  }
}

function togglePosFiltersBar() {
  const bar = $("pos-filters-bar");
  const btn = $("btn-toggle-pos-filters");
  if (!bar || !btn) return;
  const open = !bar.classList.contains("is-open");
  bar.classList.toggle("is-open", open);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
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
  const floatingBar = $("pos-batch-floating-bar");
  const floatingCount = $("pos-batch-count");
  const floatingMv = $("pos-batch-mv");
  const floatingPnl = $("pos-batch-pnl");
  const floatingBtnText = $("btn-batch-liquidate-text");
  const floatingAutoTradeBtn = $("btn-batch-autotrade");
  const floatingAutoTradeText = $("btn-batch-autotrade-text");

  const count = posSelectedSymbols.size;
  const isVisible = count > 0;

  if (btn) {
    btn.hidden = !isVisible || loopRunning;
    if (isVisible && text) {
      text.textContent = `${tx("liquidate_selected", "Close Selected")} (${count})`;
    }
  }

  if (floatingBar) {
    floatingBar.hidden = !isVisible;
    if (isVisible && positionsData && Array.isArray(positionsData.positions)) {
      const selectedPositions = positionsData.positions.filter((p) => posSelectedSymbols.has(p.symbol));
      const totalMv = selectedPositions.reduce((sum, p) => sum + Math.abs(Number(p.market_value || 0)), 0);
      const totalUpl = selectedPositions.reduce((sum, p) => sum + Number(p.unrealized_pl || 0), 0);

      if (floatingCount) floatingCount.textContent = String(count);
      if (floatingMv) floatingMv.textContent = money(totalMv);
      if (floatingPnl) {
        floatingPnl.textContent = formatPnl(totalUpl);
        floatingPnl.className = `pos-batch-metric mono ${totalUpl >= 0 ? "pos" : "neg"}`;
      }
      if (floatingBtnText) {
        floatingBtnText.textContent = `${tx("liquidate_selected", "Close Selected")} (${count})`;
      }
      if (floatingAutoTradeText) {
        floatingAutoTradeText.textContent = `${tx("autotrade_selected", "Auto Trade Selected")} (${count})`;
      }
      const batchLiquidateBtn = $("btn-batch-liquidate");
      if (batchLiquidateBtn) {
        batchLiquidateBtn.disabled = loopRunning;
        batchLiquidateBtn.title = loopRunning ? tx("pos_loop_locked_short", "Stop the Auto Trade loop to close manually") : "";
      }
    }
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
  if (pos.has_dip_hunt) {
    const dh = pos.dip_hunt_plan;
    const dhText = dh ? `${dh.wait_minutes}m · ${dh.dip_pct}%` : "";
    parts.push(
      `<span class="pos-prot-badge dh" title="${escapeHtml(tx("orders_dip_hunt_armed", "Dip Hunt armed") + (dhText ? ` (${dhText})` : ""))}">🎯 ${escapeHtml(tx("dip_hunt", "Dip Hunt"))}${dhText ? ` <em>${escapeHtml(dhText)}</em>` : ""}</span>`
    );
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

function getTickFlashClass(sym, currentPrice, upl) {
  const key = String(sym || "");
  const prev = posPrevTicks.get(key);
  let flash = "";
  if (prev && Number.isFinite(currentPrice) && Number.isFinite(prev.price)) {
    if (currentPrice > prev.price + 0.001) flash = "pos-flash-pos";
    else if (currentPrice < prev.price - 0.001) flash = "pos-flash-neg";
  }
  posPrevTicks.set(key, { price: Number(currentPrice || 0), upl: Number(upl || 0) });
  return flash;
}

function renderPositionsTable(positions) {
  const tbody = $("pos-table-body");
  if (!tbody) return;

  tbody.innerHTML = positions
    .map((pos) => {
      const sym = pos.symbol;
      const isSelected = posSelectedSymbols.has(sym);
      const isAutoTrading = !!pos.is_auto_trading;
      const autoStratName = pos.auto_trade ? (pos.auto_trade.engine_name || pos.auto_trade.strategy_mode?.toUpperCase() || "AUTO") : "AUTO";
      const side = String(pos.side || "long").toLowerCase();
      const upl = Number(pos.unrealized_pl || 0);
      const ipl = Number(pos.unrealized_intraday_pl || 0);
      const chg = pos.change_today;
      const qty = Number(pos.qty || 0);
      const currPx = Number(pos.current_price || 0);
      const flashClass = getTickFlashClass(sym, currPx, upl);
      const qtyAvail = Number(pos.qty_available != null ? pos.qty_available : qty);
      const closeTitle = loopRunning
        ? tx("pos_loop_locked_short", "Stop the Auto Trade loop to close manually")
        : tx("close_position", "Close");

      return `
      <tr class="pos-table-row ${isSelected ? "is-selected" : ""} ${flashClass}" data-symbol="${escapeHtml(sym)}" data-side="${escapeHtml(side)}">
        <td class="pos-cell-check">
          <input type="checkbox" class="pos-check-input pos-row-check" data-symbol="${escapeHtml(sym)}" ${isSelected ? "checked" : ""} aria-label="${escapeHtml(tx("pos_select_symbol", "Select {symbol}", { symbol: sym }))}" />
        </td>
        <td class="pos-cell-sym">
          <div class="pos-sym-group">
            ${symbolManualOrderLink(sym, "", pos.option_label || "")}
            ${pos.is_option ? `<small class="pos-option-tag">${escapeHtml(tx("option_contract", "Option"))}</small>` : ""}
            ${isAutoTrading ? `<span class="pos-row-autotrade-badge" title="${escapeHtml(tx("autotrade_badge_hint", "Auto-trade running for this ticker"))}"><span class="badge-dot"></span>${escapeHtml(autoStratName)}</span>` : ""}
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
          <div>$${currPx.toFixed(2)}</div>
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
            <button type="button" class="pos-act pos-act-autotrade btn-pos-autotrade ${isAutoTrading ? "is-running" : ""}" data-symbol="${escapeHtml(sym)}" title="${escapeHtml(isAutoTrading ? tx("autotrade_running_hint", "Auto-Trade running. Click to configure or stop.") : tx("autotrade_btn_hint", "Launch automated trading strategy for this ticker"))}">
              ${escapeHtml(isAutoTrading ? tx("autotrade_btn_manage", "Auto: On") : tx("autotrade_btn_launch", "Auto Trade"))}
            </button>
            <button type="button" class="pos-act pos-act-exit btn-pos-exit" data-symbol="${escapeHtml(sym)}" title="${escapeHtml(tx("exit_strategy_hint", "Configure Stop Loss, Breakeven, Trailing Stop, or Profit Target"))}">
              ${escapeHtml(tx("nav_exit", "Exit"))}
            </button>
            <button type="button" class="pos-act pos-act-close btn-pos-close" data-symbol="${escapeHtml(sym)}" ${loopRunning ? "disabled" : ""} title="${escapeHtml(closeTitle)}">
              ${escapeHtml(tx("close_position", "Close"))}
            </button>
            <button type="button" class="pos-act btn-pos-lots" data-symbol="${escapeHtml(sym)}" title="${escapeHtml(tx("pos_lots_hint", "See the individual share lots behind this holding"))}">
              ${escapeHtml(tx("pos_lots_short", "Lots"))}
            </button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

function updateSelectAllHeaderState(visiblePositions) {
  const selectAll = $("pos-select-all");
  if (!selectAll) return;
  if (!visiblePositions || visiblePositions.length === 0) {
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
      const isAutoTrading = !!pos.is_auto_trading;
      const autoStratName = pos.auto_trade ? (pos.auto_trade.engine_name || pos.auto_trade.strategy_mode?.toUpperCase() || "AUTO") : "AUTO";
      const actionsOpen = posExpandedCardActions.has(sym);
      const side = String(pos.side || "long").toLowerCase();
      const upl = Number(pos.unrealized_pl || 0);
      const ipl = Number(pos.unrealized_intraday_pl || 0);
      const qty = Number(pos.qty || 0);

      return `
      <div class="pos-card ${isSelected ? "is-selected" : ""}" role="listitem" data-symbol="${escapeHtml(sym)}">
        <div class="pos-card-head">
          <div class="pos-card-sym-wrap">
            <input type="checkbox" class="pos-check-input pos-row-check" data-symbol="${escapeHtml(sym)}" ${isSelected ? "checked" : ""} aria-label="${escapeHtml(tx("pos_select_symbol", "Select {symbol}", { symbol: sym }))}" />
            ${symbolManualOrderLink(sym, "pos-card-sym", pos.option_label || "")}
            ${pos.is_option ? `<small class="pos-option-tag">${escapeHtml(tx("option_contract", "Option"))}</small>` : ""}
            ${isAutoTrading ? `<span class="pos-row-autotrade-badge" title="${escapeHtml(tx("autotrade_badge_hint", "Auto-trade running for this ticker"))}"><span class="badge-dot"></span>${escapeHtml(autoStratName)}</span>` : ""}
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
            <div class="pos-kpi-valrow">
              <strong class="${upl >= 0 ? "pos" : "neg"}">${formatPnl(upl)}</strong>
              ${pos.unrealized_pct != null ? `<small class="pos-chg-pill ${Number(pos.unrealized_pct) >= 0 ? "pos" : "neg"}">${formatPnlPct(pos.unrealized_pct)}</small>` : ""}
            </div>
          </div>
          <div>
            <span>${escapeHtml(tx("intraday_pnl", "Today's P&L"))}</span>
            <div class="pos-kpi-valrow">
              <strong class="${ipl >= 0 ? "pos" : "neg"}">${formatPnl(ipl)}</strong>
              ${pos.unrealized_intraday_pct != null ? `<small class="pos-chg-pill ${Number(pos.unrealized_intraday_pct) >= 0 ? "pos" : "neg"}">${formatPnlPct(pos.unrealized_intraday_pct)}</small>` : ""}
            </div>
          </div>
          <div>
            <span>${escapeHtml(tx("protection", "Protection"))}</span>
            <span class="pos-card-prot">${protectionMarkup(pos)}</span>
          </div>
        </div>
        <button type="button" class="pos-card-actions-toggle" aria-expanded="${actionsOpen ? "true" : "false"}">
          <span>${escapeHtml(tx("actions", "Actions"))}</span>
          <svg class="pos-card-actions-chevron" viewBox="0 0 24 24" width="12" height="12" aria-hidden="true" focusable="false">
            <path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M6 9l6 6 6-6"/>
          </svg>
        </button>
        <div class="pos-card-actions" ${actionsOpen ? "" : "hidden"}>
          <button type="button" class="ghost btn-pos-autotrade ${isAutoTrading ? "is-running" : ""}" data-symbol="${escapeHtml(sym)}">
            ${escapeHtml(isAutoTrading ? tx("autotrade_btn_manage", "Auto: On") : tx("autotrade_btn_launch", "Auto Trade"))}
          </button>
          <button type="button" class="ghost btn-pos-exit" data-symbol="${escapeHtml(sym)}" title="${escapeHtml(tx("exit_strategy_hint", "Configure Stop Loss, Breakeven, Trailing Stop, or Profit Target"))}">
            ${escapeHtml(tx("exit_strategy_btn", "Exit Strategy"))}
          </button>
          <button type="button" class="ghost ghost-danger btn-pos-close" data-symbol="${escapeHtml(sym)}" ${loopRunning ? "disabled" : ""}>
            ${escapeHtml(tx("close_position", "Close"))}
          </button>
          <button type="button" class="ghost btn-pos-lots" data-symbol="${escapeHtml(sym)}">
            ${escapeHtml(tx("pos_lots_short", "Lots"))}
          </button>
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

function formatPosApiError(err, fallback = "Failed to close position") {
  if (!err) return fallback;
  let text = typeof err === "string" ? err : err.message || err.detail || fallback;
  if (typeof text !== "string") text = String(text);
  const trimmed = text.trim();
  if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
    try {
      const obj = JSON.parse(trimmed);
      if (obj && typeof obj === "object") {
        if (obj.message) {
          let msg = obj.message.charAt(0).toUpperCase() + obj.message.slice(1);
          if (obj.held_for_orders && obj.available != null) {
            msg += ` (Resting orders hold ${obj.held_for_orders} shares, ${obj.available} available). Cancel resting open orders first.`;
          }
          return msg;
        }
        if (obj.detail) return formatPosApiError(obj.detail, fallback);
      }
    } catch (_) {}
  }
  return text || fallback;
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
      errEl.textContent = formatPosApiError(err, tx("error_close_position", "Failed to close position"));
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = tx("confirm_liquidation", "Confirm liquidation");
    }
  }
}

/** ── Exit Strategy & Protection Modal Controller ────────────────── */

function openExitStrategyModal(pos, initialMode = null) {
  if (!pos) return;
  activeExitPosition = pos;

  const symBadge = $("pos-exit-symbol-badge");
  const sideBadge = $("pos-exit-side-badge");
  const heldQtyEl = $("pos-exit-held-qty");
  const avgEntryEl = $("pos-exit-avg-entry");
  const currPriceEl = $("pos-exit-curr-price");
  const unrlEl = $("pos-exit-unrealized-pnl");
  const errEl = $("pos-exit-error");

  const isShort = String(pos.side || "").toLowerCase() === "short";
  const currPx = Number(pos.current_price || 0);
  const entryPx = Number(pos.avg_entry_price || 0);
  const upl = Number(pos.unrealized_pl || 0);

  if (symBadge) symBadge.textContent = pos.symbol;
  if (sideBadge) {
    sideBadge.className = `side-badge ${isShort ? "short" : "long"}`;
    sideBadge.textContent = positionSideLabel(isShort ? "short" : "long");
  }
  if (heldQtyEl) heldQtyEl.textContent = formatPositionQty(pos.qty);
  if (avgEntryEl) avgEntryEl.textContent = `$${entryPx.toFixed(2)}`;
  if (currPriceEl) currPriceEl.textContent = `$${currPx.toFixed(2)}`;
  if (unrlEl) {
    const pctText = formatPnlPct(pos.unrealized_pct);
    unrlEl.textContent = pctText ? `${formatPnl(upl)} (${pctText})` : formatPnl(upl);
    setPnlTone(unrlEl, upl);
  }
  const isAutoTrading = !!pos.is_auto_trading;
  const submitBtn = $("btn-exit-modal-submit");
  if (isAutoTrading) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = tx(
        "err_ticker_in_autotrade",
        "Stop auto-trade for {symbol} before changing exit strategies by hand — auto trade manages its own exits.",
        { symbol: pos.symbol }
      );
    }
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.title = tx(
        "err_ticker_in_autotrade",
        "Stop auto-trade for {symbol} before changing exit strategies by hand — auto trade manages its own exits.",
        { symbol: pos.symbol }
      );
    }
  } else {
    if (errEl) {
      errEl.hidden = true;
      errEl.textContent = "";
    }
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.title = "";
    }
  }

  // Active Protection Status Card
  const statusIndicator = $("pos-exit-status-indicator");
  const currSl = $("pos-exit-curr-sl");
  const currTp = $("pos-exit-curr-tp");
  const currDist = $("pos-exit-curr-dist");

  const hasSl = !!pos.has_stop_loss;
  const hasTp = !!pos.has_take_profit;
  const hasDh = !!pos.has_dip_hunt;
  const dhPlan = pos.dip_hunt_plan;

  if (statusIndicator) {
    if (hasSl && hasTp && hasDh) {
      statusIndicator.textContent = tx("status_bracket_dip_active", "Bracket + Re-buy Active");
      statusIndicator.className = "pos-exit-status-indicator pos";
    } else if (hasSl && hasDh) {
      statusIndicator.textContent = tx("status_stop_dip_active", "Stop + Re-buy Active");
      statusIndicator.className = "pos-exit-status-indicator pos";
    } else if (hasSl && hasTp) {
      statusIndicator.textContent = tx("status_bracket_active", "Bracket Active");
      statusIndicator.className = "pos-exit-status-indicator pos";
    } else if (hasSl) {
      statusIndicator.textContent = tx("status_protected", "Protected");
      statusIndicator.className = "pos-exit-status-indicator pos";
    } else {
      statusIndicator.textContent = tx("status_unprotected", "Unprotected");
      statusIndicator.className = "pos-exit-status-indicator neg";
    }
  }

  if (currSl) {
    currSl.textContent = pos.stop_loss_price != null ? `$${Number(pos.stop_loss_price).toFixed(2)}` : tx("none", "None");
  }
  if (currTp) {
    currTp.textContent = pos.take_profit_price != null ? `$${Number(pos.take_profit_price).toFixed(2)}` : tx("none", "None");
  }
  if (currDist) {
    currDist.textContent = pos.stop_distance_pct != null ? `${Math.abs(pos.stop_distance_pct).toFixed(1)}%` : "—";
  }

  const currDhSub = $("pos-exit-stat-sub-diphunt");
  const currDhVal = $("pos-exit-curr-diphunt");
  if (currDhSub) {
    if (hasDh && dhPlan) {
      currDhSub.hidden = false;
      if (currDhVal) currDhVal.textContent = `${dhPlan.wait_minutes}m / ${dhPlan.dip_pct}%`;
    } else {
      currDhSub.hidden = true;
    }
  }

  // Default stop loss price (3% default distance, ensuring valid level)
  const hasValidSl = pos.stop_loss_price != null && (
    isShort ? Number(pos.stop_loss_price) > currPx : Number(pos.stop_loss_price) < currPx
  );
  const defaultSlPx = hasValidSl
    ? Number(pos.stop_loss_price)
    : (isShort ? currPx * 1.03 : currPx * 0.97);
  const slInput = $("pos-exit-sl-price");
  if (slInput) slInput.value = defaultSlPx.toFixed(2);

  // Default take profit price (5% default target, ensuring valid level)
  const hasValidTp = pos.take_profit_price != null && (
    isShort ? Number(pos.take_profit_price) < currPx : Number(pos.take_profit_price) > currPx
  );
  const defaultTpPx = hasValidTp
    ? Number(pos.take_profit_price)
    : (isShort ? currPx * 0.95 : currPx * 1.05);
  const tpInput = $("pos-exit-tp-price");
  if (tpInput) tpInput.value = defaultTpPx.toFixed(2);

  const trailInput = $("pos-exit-trail-pct");
  if (trailInput) trailInput.value = "3.0";

  const bracketSl = $("pos-exit-bracket-sl");
  if (bracketSl) bracketSl.value = defaultSlPx.toFixed(2);

  const bracketDefaultTpPx = hasValidTp
    ? Number(pos.take_profit_price)
    : (isShort ? currPx * 0.90 : currPx * 1.10);
  const bracketTp = $("pos-exit-bracket-tp");
  if (bracketTp) bracketTp.value = bracketDefaultTpPx.toFixed(2);

  // Breakeven target level
  const beLevel = $("pos-exit-be-level");
  const bePnl = $("pos-exit-be-pnl");
  const beTarget = isShort ? entryPx + 0.01 : Math.max(0.01, entryPx - 0.01);
  if (beLevel) beLevel.textContent = `$${beTarget.toFixed(2)}`;
  if (bePnl) {
    const isProfitable = isShort ? currPx < entryPx : currPx > entryPx;
    if (isProfitable) {
      bePnl.textContent = `$0.00 (0.0%)`;
      bePnl.className = "mono pos";
    } else {
      bePnl.textContent = tx("be_underwater_note", "Position currently underwater");
      bePnl.className = "mono neg";
    }
  }

  // Reset and sync Stop Loss distance chips, badge & custom input
  const distBadge = $("pos-exit-sl-dist-badge");
  const customWrap = $("pos-exit-sl-custom-wrap");
  const customInput = $("pos-exit-sl-custom-input");
  const customBtn = $("btn-sl-pct-custom");
  if (customWrap) {
    customWrap.hidden = false;
    customWrap.classList.remove("is-active");
  }
  if (customBtn) customBtn.classList.remove("is-active");

  const actualSlPx = pos.stop_loss_price != null ? Number(pos.stop_loss_price) : null;
  if (actualSlPx != null && currPx > 0) {
    const calculatedPct = isShort ? ((actualSlPx - currPx) / currPx) * 100 : ((currPx - actualSlPx) / currPx) * 100;
    if (distBadge) distBadge.textContent = calculatedPct > 0 ? `${calculatedPct.toFixed(1)}%` : "—";
    let matchedChip = null;
    document.querySelectorAll("[data-sl-pct]").forEach((c) => {
      const p = Number(c.dataset.slPct);
      if (Math.abs(p - calculatedPct) < 0.05) matchedChip = c;
    });
    if (matchedChip) {
      document.querySelectorAll("[data-sl-pct]").forEach((c) => c.classList.toggle("is-active", c === matchedChip));
      if (customWrap) customWrap.classList.remove("is-active");
      if (customInput) customInput.value = "";
    } else if (calculatedPct > 0) {
      document.querySelectorAll("[data-sl-pct]").forEach((c) => c.classList.remove("is-active"));
      if (customWrap) customWrap.classList.add("is-active");
      if (customInput) customInput.value = calculatedPct.toFixed(1);
    }

    let matchedBracketSl = null;
    document.querySelectorAll("[data-bracket-sl-pct]").forEach((c) => {
      if (Math.abs(Number(c.dataset.bracketSlPct) - calculatedPct) < 0.05) matchedBracketSl = c;
    });
    document.querySelectorAll("[data-bracket-sl-pct]").forEach((c) => c.classList.toggle("is-active", c === matchedBracketSl));
  } else {
    document.querySelectorAll("[data-sl-pct]").forEach((c) => c.classList.toggle("is-active", c.dataset.slPct === "3"));
    document.querySelectorAll("[data-bracket-sl-pct]").forEach((c) => c.classList.toggle("is-active", c.dataset.bracketSlPct === "3"));
    if (distBadge) distBadge.textContent = "3.0%";
    if (customWrap) customWrap.classList.remove("is-active");
    if (customInput) customInput.value = "";
  }

  // Sync Take Profit and Bracket TP chips
  const actualTpPx = pos.take_profit_price != null ? Number(pos.take_profit_price) : null;
  if (actualTpPx != null && currPx > 0) {
    const tpPct = isShort ? ((currPx - actualTpPx) / currPx) * 100 : ((actualTpPx - currPx) / currPx) * 100;
    let matchedTp = null;
    document.querySelectorAll("[data-tp-pct]").forEach((c) => {
      if (Math.abs(Number(c.dataset.tpPct) - tpPct) < 0.05) matchedTp = c;
    });
    document.querySelectorAll("[data-tp-pct]").forEach((c) => c.classList.toggle("is-active", c === matchedTp));

    let matchedBracketTp = null;
    document.querySelectorAll("[data-bracket-tp-pct]").forEach((c) => {
      if (Math.abs(Number(c.dataset.bracketTpPct) - tpPct) < 0.05) matchedBracketTp = c;
    });
    document.querySelectorAll("[data-bracket-tp-pct]").forEach((c) => c.classList.toggle("is-active", c === matchedBracketTp));
  } else {
    document.querySelectorAll("[data-tp-pct]").forEach((c) => c.classList.toggle("is-active", c.dataset.tpPct === "5"));
    document.querySelectorAll("[data-bracket-tp-pct]").forEach((c) => c.classList.toggle("is-active", c.dataset.bracketTpPct === "10"));
  }

  // Initialize Strategy Sizing (Shares / Qty)
  const heldQty = Math.abs(Number(pos.qty || 0));
  const exitQtyInput = $("pos-exit-qty-input");
  if (exitQtyInput) {
    exitQtyInput.value = heldQty;
    exitQtyInput.max = String(heldQty);
    exitQtyInput.min = "1";
    exitQtyInput.step = Number.isInteger(heldQty) ? "1" : "any";
  }
  document.querySelectorAll("[data-exit-qty-pct]").forEach((c) => {
    c.classList.toggle("is-active", c.dataset.exitQtyPct === "100");
  });
  syncExitQtyUi(heldQty, heldQty);

  // Initialize Dip Hunt accordion & inputs
  const dhGroup = $("pos-exit-dip-hunt-group");
  const dhToggle = $("pos-exit-dip-hunt-enabled");
  const dhWait = $("pos-exit-dip-hunt-wait");
  const dhPct = $("pos-exit-dip-hunt-pct");

  if (dhToggle) {
    dhToggle.checked = hasDh;
  }
  if (dhWait) {
    dhWait.value = String(dhPlan?.wait_minutes ?? 10);
  }
  if (dhPct) {
    dhPct.value = String(dhPlan?.dip_pct ?? 5);
  }
  if (dhGroup) {
    dhGroup.open = hasDh;
  }

  // Smart initial mode selection based on active exits
  let modeToOpen = initialMode;
  if (!modeToOpen) {
    if (hasSl && hasTp) modeToOpen = "bracket";
    else if (hasTp && !hasSl) modeToOpen = "take_profit";
    else modeToOpen = "stop_loss";
  }

  setExitMode(modeToOpen);
  openPosModal("pos-exit-modal");

  if (slInput && (modeToOpen === "stop_loss")) {
    slInput.focus();
    slInput.select();
  }
}

function syncExitQtyUi(qty, heldQty) {
  const pctBadge = $("pos-exit-qty-pct-badge");
  const hintEl = $("pos-exit-qty-hint");
  const pct = heldQty > 0 ? Math.round((qty / heldQty) * 100) : 0;
  if (pctBadge) {
    pctBadge.textContent = `${pct}%`;
  }
  if (hintEl) {
    if (qty >= heldQty) {
      hintEl.textContent = tx("protecting_all_shares", "Protecting all {total} shares (100%)", {
        total: formatPositionQty(heldQty),
      });
      hintEl.classList.remove("warn");
    } else if (qty > 0) {
      hintEl.textContent = tx("protecting_shares_hint", "Protecting {qty} of {total} shares ({pct}%)", {
        qty: formatPositionQty(qty),
        total: formatPositionQty(heldQty),
        pct: String(pct),
      });
      hintEl.classList.remove("warn");
    } else {
      hintEl.textContent = tx("err_invalid_exit_qty", "Enter a valid share quantity greater than 0");
      hintEl.classList.add("warn");
    }
  }
}

function formatExitPrice(value) {
  const price = Number(value);
  if (!Number.isFinite(price)) return "—";
  const digits = Math.abs(price) < 1 ? 4 : 2;
  return `$${price.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function syncExitDipHuntUI(resolvedStopPx) {
  if (!activeExitPosition) return;
  const isShort = String(activeExitPosition.side || "").toLowerCase() === "short";
  const isSupportedMode = ["stop_loss", "breakeven", "trailing", "bracket"].includes(activeExitMode);
  const shouldShow = !isShort && isSupportedMode;

  const group = $("pos-exit-dip-hunt-group");
  if (group) {
    group.hidden = !shouldShow;
  }

  const toggle = $("pos-exit-dip-hunt-enabled");
  const isChecked = toggle ? toggle.checked : false;
  const enabled = shouldShow && isChecked;

  const fields = $("pos-exit-dip-hunt-fields");
  if (fields) {
    fields.hidden = !enabled;
  }

  const waitInput = $("pos-exit-dip-hunt-wait");
  const pctInput = $("pos-exit-dip-hunt-pct");
  const badge = $("pos-exit-dip-hunt-summary-badge");
  const summaryEl = $("pos-exit-dip-hunt-summary");

  if (badge) {
    if (!enabled) {
      badge.textContent = tx("target_off", "off");
    } else {
      const wait = Number(waitInput?.value || 10);
      const dip = Number(pctInput?.value || 5);
      badge.textContent = `${wait}m / ${dip}%`;
    }
  }

  if (summaryEl) {
    if (!enabled) {
      summaryEl.textContent = "";
    } else {
      const wait = Number(waitInput?.value || 10);
      const dip = Number(pctInput?.value || 5);
      let stopPx = resolvedStopPx;
      if (stopPx == null || stopPx <= 0) {
        const currPx = Number(activeExitPosition.current_price || 0);
        let entryPx = Number(activeExitPosition.avg_entry_price || 0);
        if (entryPx <= 0) entryPx = currPx;
        if (activeExitMode === "stop_loss") {
          stopPx = Number($("pos-exit-sl-price")?.value || 0);
        } else if (activeExitMode === "breakeven") {
          stopPx = entryPx - 0.01;
        } else if (activeExitMode === "trailing") {
          const trail = Number($("pos-exit-trail-pct")?.value || 3.0);
          stopPx = currPx * (1 - trail / 100);
        } else if (activeExitMode === "bracket") {
          stopPx = Number($("pos-exit-bracket-sl")?.value || 0);
        }
      }

      let extra = "";
      if (Number.isFinite(stopPx) && stopPx > 0) {
        const target = stopPx * (1 - dip / 100);
        extra = tx("dip_hunt_summary_price", " Example from the mark: stop ~{stop} → buy at {buy}.", {
          stop: formatExitPrice(stopPx),
          buy: formatExitPrice(target),
        });
      }
      summaryEl.textContent =
        tx(
          "dip_hunt_summary",
          "After a stop-out, wait up to {wait} minutes for a further {dip}% drop — or buy immediately if that drop hits sooner. Then repeat.",
          { wait: String(wait), dip: String(dip) }
        ) + extra;
    }
  }
}

function closeExitStrategyModal() {
  closePosModal("pos-exit-modal");
  activeExitPosition = null;
  const dhGroup = $("pos-exit-dip-hunt-group");
  if (dhGroup) {
    dhGroup.open = false;
    dhGroup.hidden = true;
  }
  const dhToggle = $("pos-exit-dip-hunt-enabled");
  if (dhToggle) {
    dhToggle.checked = false;
  }
  const dhFields = $("pos-exit-dip-hunt-fields");
  if (dhFields) {
    dhFields.hidden = true;
  }
  const dhSummary = $("pos-exit-dip-hunt-summary");
  if (dhSummary) {
    dhSummary.textContent = "";
  }
}

function setExitMode(mode) {
  activeExitMode = mode;

  // Update tabs
  document.querySelectorAll(".pos-exit-tab").forEach((tab) => {
    const on = tab.dataset.exitMode === mode;
    tab.classList.toggle("is-active", on);
    tab.setAttribute("aria-selected", on ? "true" : "false");
  });

  // Update panes
  const panes = {
    stop_loss: $("pos-exit-pane-stop-loss"),
    breakeven: $("pos-exit-pane-breakeven"),
    trailing: $("pos-exit-pane-trailing"),
    take_profit: $("pos-exit-pane-take-profit"),
    bracket: $("pos-exit-pane-bracket"),
    clear: $("pos-exit-pane-clear"),
  };

  Object.entries(panes).forEach(([key, el]) => {
    if (el) el.hidden = key !== mode;
  });

  // Toggle sizing card visibility & update label based on exit mode
  const sizingCard = $("pos-exit-sizing-card");
  const sizingLabel = $("pos-exit-qty-label");
  if (sizingCard) {
    sizingCard.hidden = mode === "clear";
  }
  if (sizingLabel) {
    if (mode === "take_profit") {
      sizingLabel.textContent = tx("shares_to_exit", "Shares to Exit");
    } else if (mode === "bracket") {
      sizingLabel.textContent = tx("shares_to_bracket", "Shares to Protect & Exit");
    } else {
      sizingLabel.textContent = tx("shares_to_protect", "Shares to Protect");
    }
  }

  // Update submit button text
  const submitBtn = $("btn-exit-modal-submit");
  if (submitBtn) {
    if (mode === "stop_loss") {
      submitBtn.textContent = tx("apply_stop_loss", "Arm Stop Loss");
      submitBtn.className = "primary";
    } else if (mode === "breakeven") {
      submitBtn.textContent = tx("apply_breakeven", "Move to Breakeven");
      submitBtn.className = "primary";
      if (activeExitPosition) {
        const curr = Number(activeExitPosition.current_price || 0);
        const entry = Number(activeExitPosition.avg_entry_price || 0);
        const short = String(activeExitPosition.side || "").toLowerCase() === "short";
        const underwater = short ? curr >= entry : curr <= entry;
        if (underwater) {
          submitBtn.disabled = true;
          submitBtn.title = tx("be_underwater_note", "Position currently underwater");
        }
      }
    } else if (mode === "trailing") {
      submitBtn.textContent = tx("apply_trailing_stop", "Arm Trailing Stop");
      submitBtn.className = "primary";
    } else if (mode === "take_profit") {
      submitBtn.textContent = tx("apply_take_profit", "Set Take Profit");
      submitBtn.className = "primary";
    } else if (mode === "bracket") {
      submitBtn.textContent = tx("apply_bracket_exit", "Arm Bracket Exit");
      submitBtn.className = "primary";
    } else if (mode === "clear") {
      submitBtn.textContent = tx("apply_clear_exits", "Cancel Exit Orders");
      submitBtn.className = "primary primary-danger";
    }
    if (activeExitPosition && activeExitPosition.is_auto_trading) {
      submitBtn.disabled = true;
    }
  }

  syncExitDipHuntUI();
  updateExitCalculations();
}

function updateExitCalculations() {
  if (!activeExitPosition) return;
  const pos = activeExitPosition;
  const currPx = Number(pos.current_price || 0);
  let entryPx = Number(pos.avg_entry_price || 0);
  if (entryPx <= 0) entryPx = currPx;
  const isShort = String(pos.side || "").toLowerCase() === "short";
  const heldQty = Math.abs(Number(pos.qty || 0));
  const exitQtyInput = $("pos-exit-qty-input");
  const parsedQty = parseFloat(exitQtyInput?.value);
  const qty = (Number.isFinite(parsedQty) && parsedQty > 0 && parsedQty <= heldQty) ? parsedQty : heldQty;

  const riskRow = $("pos-exit-risk-row");
  const rewardRow = $("pos-exit-reward-row");
  const rrRow = $("pos-exit-rr-row");
  const riskEl = $("pos-exit-preview-risk");
  const rewardEl = $("pos-exit-preview-reward");
  const rrEl = $("pos-exit-preview-rr");
  const previewBox = $("pos-exit-preview-box");

  if (activeExitMode === "clear") {
    if (previewBox) previewBox.hidden = true;
    syncExitDipHuntUI();
    return;
  }
  if (previewBox) previewBox.hidden = false;

  let stopPx = null;
  let targetPx = null;

  if (activeExitMode === "stop_loss") {
    stopPx = Number($("pos-exit-sl-price")?.value || 0);
  } else if (activeExitMode === "breakeven") {
    stopPx = isShort ? entryPx + 0.01 : entryPx - 0.01;
  } else if (activeExitMode === "trailing") {
    const trailPct = Number($("pos-exit-trail-pct")?.value || 3.0);
    stopPx = isShort ? currPx * (1 + trailPct / 100) : currPx * (1 - trailPct / 100);
  } else if (activeExitMode === "take_profit") {
    targetPx = Number($("pos-exit-tp-price")?.value || 0);
  } else if (activeExitMode === "bracket") {
    stopPx = Number($("pos-exit-bracket-sl")?.value || 0);
    targetPx = Number($("pos-exit-bracket-tp")?.value || 0);
  }

  let riskAmount = null;
  let riskPct = null;
  if (stopPx != null && stopPx > 0) {
    const diff = isShort ? stopPx - entryPx : entryPx - stopPx;
    riskAmount = diff * qty;
    riskPct = entryPx > 0 ? (diff / entryPx) * 100 : 0;
  }

  let rewardAmount = null;
  let rewardPct = null;
  if (targetPx != null && targetPx > 0) {
    const diff = isShort ? entryPx - targetPx : targetPx - entryPx;
    rewardAmount = diff * qty;
    rewardPct = entryPx > 0 ? (diff / entryPx) * 100 : 0;
  }

  if (riskRow) riskRow.hidden = riskAmount == null;
  if (rewardRow) rewardRow.hidden = rewardAmount == null;

  if (riskEl && riskAmount != null) {
    const sign = riskAmount > 0 ? "-" : "+";
    riskEl.textContent = `${sign}$${Math.abs(riskAmount).toFixed(2)} (${sign}${Math.abs(riskPct).toFixed(1)}%)`;
    riskEl.className = `mono ${riskAmount > 0 ? "neg" : "pos"}`;
  }

  if (rewardEl && rewardAmount != null) {
    const sign = rewardAmount >= 0 ? "+" : "-";
    rewardEl.textContent = `${sign}$${Math.abs(rewardAmount).toFixed(2)} (${sign}${Math.abs(rewardPct).toFixed(1)}%)`;
    rewardEl.className = `mono ${rewardAmount >= 0 ? "pos" : "neg"}`;
  }

  if (rrRow) {
    if (riskAmount != null && rewardAmount != null && riskAmount > 0 && rewardAmount > 0) {
      rrRow.hidden = false;
      const ratio = (rewardAmount / riskAmount).toFixed(2);
      if (rrEl) rrEl.textContent = `1 : ${ratio}`;
    } else {
      rrRow.hidden = true;
    }
  }

  syncExitDipHuntUI(stopPx);
}

async function submitExitStrategy() {
  if (!activeExitPosition) return;
  const pos = activeExitPosition;
  const submitBtn = $("btn-exit-modal-submit");
  const errEl = $("pos-exit-error");

  if (pos.is_auto_trading) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = tx(
        "err_ticker_in_autotrade",
        "Stop auto-trade for {symbol} before changing exit strategies by hand — auto trade manages its own exits.",
        { symbol: pos.symbol }
      );
    }
    if (submitBtn) {
      submitBtn.disabled = true;
    }
    return;
  }

  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = tx("applying", "Applying…");
  }

  try {
    const isShort = String(pos.side || "").toLowerCase() === "short";
    const currPx = Number(pos.current_price || 0);
    const heldQty = Math.abs(Number(pos.qty || 0));

    const payload = { symbol: pos.symbol };

    if (activeExitMode !== "clear") {
      const exitQtyInput = $("pos-exit-qty-input");
      const chosenQty = Number(exitQtyInput?.value || 0);
      if (chosenQty <= 0) {
        throw new Error(tx("err_invalid_exit_qty", "Enter a valid share quantity greater than 0"));
      }
      if (chosenQty > heldQty) {
        throw new Error(
          tx("err_exit_qty_exceeds", "Quantity cannot exceed held position ({total} shares)", {
            total: formatPositionQty(heldQty),
          })
        );
      }
      if ((activeExitMode !== "take_profit" || isShort) && (chosenQty < 1 || !Number.isInteger(chosenQty))) {
        throw new Error(tx("err_exit_whole_shares", "Protective stops and bracket exits require whole shares (minimum 1)"));
      }
      payload.qty = chosenQty;
    }

    if (activeExitMode === "stop_loss") {
      const slPx = Number($("pos-exit-sl-price")?.value || 0);
      if (slPx <= 0) throw new Error(tx("err_invalid_stop_price", "Enter a valid stop price greater than 0"));
      if (!isShort && slPx >= currPx) {
        throw new Error(tx("err_stop_above_market", "Stop loss for a long position must sit below current price (${price})", { price: currPx.toFixed(2) }));
      }
      if (isShort && slPx <= currPx) {
        throw new Error(tx("err_stop_below_market", "Stop loss for a short position must sit above current price (${price})", { price: currPx.toFixed(2) }));
      }
      payload.action = "price";
      payload.stop_price = slPx;
    } else if (activeExitMode === "breakeven") {
      const isUnderwater = isShort ? currPx >= entryPx : currPx <= entryPx;
      if (isUnderwater) {
        throw new Error(tx("err_be_underwater", "Cannot move to breakeven: Position is currently underwater"));
      }
      payload.action = "breakeven";
    } else if (activeExitMode === "trailing") {
      const trail = Number($("pos-exit-trail-pct")?.value || 0);
      if (trail <= 0 || trail > 50) throw new Error(tx("err_invalid_trail_pct", "Enter a valid trail percentage between 0.1% and 50%"));
      payload.action = "trail";
      payload.trail_percent = trail;
    } else if (activeExitMode === "take_profit") {
      const tpPx = Number($("pos-exit-tp-price")?.value || 0);
      if (tpPx <= 0) throw new Error(tx("err_invalid_target_price", "Enter a valid take profit price greater than 0"));
      if (!isShort && tpPx <= currPx) {
        throw new Error(tx("err_target_below_market", "Take profit for a long position must sit above current price (${price})", { price: currPx.toFixed(2) }));
      }
      if (isShort && tpPx >= currPx) {
        throw new Error(tx("err_target_above_market", "Take profit for a short position must sit below current price (${price})", { price: currPx.toFixed(2) }));
      }
      payload.action = "take_profit";
      payload.take_profit_price = tpPx;
    } else if (activeExitMode === "bracket") {
      const slPx = Number($("pos-exit-bracket-sl")?.value || 0);
      const tpPx = Number($("pos-exit-bracket-tp")?.value || 0);
      if (slPx <= 0 && tpPx <= 0) throw new Error(tx("err_bracket_needs_levels", "Enter at least a stop loss or take profit price"));
      if (slPx > 0) {
        if (!isShort && slPx >= currPx) {
          throw new Error(tx("err_stop_above_market", "Stop loss for a long position must sit below current price (${price})", { price: currPx.toFixed(2) }));
        }
        if (isShort && slPx <= currPx) {
          throw new Error(tx("err_stop_below_market", "Stop loss for a short position must sit above current price (${price})", { price: currPx.toFixed(2) }));
        }
      }
      if (tpPx > 0) {
        if (!isShort && tpPx <= currPx) {
          throw new Error(tx("err_target_below_market", "Take profit for a long position must sit above current price (${price})", { price: currPx.toFixed(2) }));
        }
        if (isShort && tpPx >= currPx) {
          throw new Error(tx("err_target_above_market", "Take profit for a short position must sit below current price (${price})", { price: currPx.toFixed(2) }));
        }
      }
      if (slPx > 0 && tpPx > 0) {
        if (!isShort && slPx >= tpPx) {
          throw new Error(tx("err_bracket_cross", "Stop loss must sit below take profit target"));
        }
        if (isShort && slPx <= tpPx) {
          throw new Error(tx("err_bracket_cross_short", "Stop loss must sit above take profit target for a short position"));
        }
      }
      payload.action = "bracket";
      if (slPx > 0) payload.stop_price = slPx;
      if (tpPx > 0) payload.take_profit_price = tpPx;
    } else if (activeExitMode === "clear") {
      const clearStops = !!$("pos-clear-stops-check")?.checked;
      const clearTp = !!$("pos-clear-tp-check")?.checked;
      if (clearStops && clearTp) payload.action = "cancel_all";
      else if (clearStops) payload.action = "cancel_stops";
      else if (clearTp) payload.action = "cancel_take_profit";
      else throw new Error(tx("err_select_cancellation", "Select at least one order type to cancel"));
    }

    if (activeExitMode !== "clear") {
      const dhToggle = $("pos-exit-dip-hunt-enabled");
      const isDipHuntSupported = !isShort && ["stop_loss", "breakeven", "trailing", "bracket"].includes(activeExitMode);

      if (isDipHuntSupported && dhToggle && dhToggle.checked) {
        if (activeExitMode === "bracket" && (!(Number($("pos-exit-bracket-sl")?.value) > 0))) {
          throw new Error(tx("err_dip_hunt_requires_stop", "Re-buy after dip requires a protective stop loss order"));
        }
        const waitMinutes = Number($("pos-exit-dip-hunt-wait")?.value);
        const dipPct = Number($("pos-exit-dip-hunt-pct")?.value);

        if (!Number.isFinite(waitMinutes) || waitMinutes < 1 || waitMinutes > 1440) {
          throw new Error(tx("err_dip_hunt_wait", "Wait time must be between 1 and 1440 minutes (24h)"));
        }
        if (!Number.isFinite(dipPct) || dipPct <= 0 || dipPct > 50) {
          throw new Error(tx("err_dip_hunt_pct", "Dip percentage must sit between 0.1% and 50%"));
        }
        payload.dip_hunt = {
          enabled: true,
          wait_minutes: waitMinutes,
          dip_pct: dipPct,
        };
      } else if (pos.has_dip_hunt) {
        payload.dip_hunt = {
          enabled: false,
        };
      }
    }

    const res = await fetch("/api/position/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    closeExitStrategyModal();
    showToast(
      tx("exit_strategy_success", "Exit strategy updated for {symbol}", { symbol: pos.symbol }),
      "ok"
    );
    await refreshPositions({ quiet: false });
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = formatPosApiError(err, tx("error_exit_strategy", "Failed to update exit strategy"));
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = !!(activeExitPosition && activeExitPosition.is_auto_trading);
      setExitMode(activeExitMode || "stop_loss");
    }
  }
}

/** Stamped in the user's active desk timezone. */
function formatLotDate(iso) {
  if (!iso) return tx("pos_lots_carried_short", "Earlier");
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return String(iso).slice(0, 10);
  try {
    const effectiveTz = getEffectiveDeskTimezone();
    const opts = {
      day: "numeric",
      month: "short",
      year: "numeric",
    };
    if (effectiveTz) opts.timeZone = effectiveTz;
    return new Intl.DateTimeFormat(document.documentElement.lang || undefined, opts).format(new Date(t));
  } catch (err) {
    return String(iso).slice(0, 10);
  }
}

/** The clock half of the stamp in the user's active desk timezone. */
function formatLotTime(iso) {
  if (!iso) return "";
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return "";
  try {
    const effectiveTz = getEffectiveDeskTimezone();
    const useHour12 = typeof isDeskHour12 === "function" ? isDeskHour12() : true;
    const opts = {
      hour: useHour12 ? "numeric" : "2-digit",
      minute: "2-digit",
      hour12: useHour12,
      timeZoneName: "short",
    };
    if (effectiveTz) opts.timeZone = effectiveTz;
    return new Intl.DateTimeFormat(document.documentElement.lang || undefined, opts).format(new Date(t));
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
    const count = lots.length;
    const countLabel = count === 1 ? tx("pos_lots_total_1", "1 lot") : tx("pos_lots_total", "{count} lots", { count });
    totals.innerHTML = `
      <span>${escapeHtml(countLabel)} · ${formatPositionQty(data.total_qty)} ${escapeHtml(tx("shares_unit", "shares"))}</span>
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
      errEl.textContent = formatPosApiError(err, tx("error_close_position", "Failed to close position"));
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
    avg_entry_price: pos?.desk_avg_entry_price ?? pos?.avg_entry_price,
    desk_avg_entry_price: pos?.desk_avg_entry_price,
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

    // Sync active positionsData holding with the desk's verified lot metrics
    const deskAvg = data.desk_avg_entry_price ?? data.weighted_avg_price ?? (
      data.total_cost_basis != null && Number(data.total_qty || data.qty || 0) > 0
        ? Number(data.total_cost_basis) / Number(data.total_qty || data.qty)
        : (data.avg_entry_price != null ? Number(data.avg_entry_price) : null)
    );
    if (deskAvg != null && positionsData && Array.isArray(positionsData.positions)) {
      const posObj = findPositionBySymbol(sym);
      if (posObj) {
        posObj.avg_entry_price = deskAvg;
        posObj.desk_avg_entry_price = deskAvg;
        if (data.total_cost_basis != null) {
          posObj.cost_basis = Number(data.total_cost_basis);
        }
        if (data.total_unrealized_pl != null) {
          posObj.unrealized_pl = Number(data.total_unrealized_pl);
          if (Number(data.total_cost_basis) > 0) {
            posObj.unrealized_pct = (Number(data.total_unrealized_pl) / Number(data.total_cost_basis)) * 100;
          }
        }
        const filtered = filterAndSortPositions(positionsData.positions);
        renderPositionsTable(filtered);
        renderPositionsCards(filtered);
      }
    }
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

  // Compute actual average entry from the desk's lots
  let avgPrice = null;
  const totalCost = data.total_cost_basis != null ? Number(data.total_cost_basis) : null;
  const totalQty = Number(data.total_qty != null ? data.total_qty : data.qty || 0);
  if (totalCost != null && totalQty > 0) {
    avgPrice = totalCost / totalQty;
  } else if (data.desk_avg_entry_price != null) {
    avgPrice = Number(data.desk_avg_entry_price);
  } else if (data.weighted_avg_price != null) {
    avgPrice = Number(data.weighted_avg_price);
  } else if (data.avg_entry_price != null) {
    avgPrice = Number(data.avg_entry_price);
  }

  if (avgEl) {
    avgEl.textContent = avgPrice != null ? `$${Number(avgPrice).toFixed(2)}` : "—";
    if (
      avgPrice != null &&
      data.alpaca_avg_entry_price != null &&
      Math.abs(avgPrice - Number(data.alpaca_avg_entry_price)) >= 0.01
    ) {
      avgEl.title = tx("pos_lots_avg_discrepancy_hint", "Desk average: {desk} (Alpaca reported: {alpaca})", {
        desk: `$${Number(avgPrice).toFixed(2)}`,
        alpaca: `$${Number(data.alpaca_avg_entry_price).toFixed(2)}`,
      });
    } else {
      avgEl.removeAttribute("title");
    }
  }

  if (pxEl) pxEl.textContent = data.current_price != null ? `$${Number(data.current_price).toFixed(2)}` : "—";
  if (pnlEl) {
    // Absent until the lots are actually back — a bare $0.00 on a live holding
    // reads as "flat", which is a claim this dialog has not earned yet.
    if (data.total_unrealized_pl == null) {
      pnlEl.textContent = "—";
      setPnlTone(pnlEl, null);
    } else {
      const total = Number(data.total_unrealized_pl);
      const pct = data.total_unrealized_pct;
      const pctFormatted = pct != null && Number.isFinite(Number(pct)) ? formatPnlPct(pct) : "";
      pnlEl.innerHTML = `${formatPnl(total)}${pctFormatted ? ` <small class="pos-chg-pill ${Number(pct) >= 0 ? "pos" : "neg"}">${pctFormatted}</small>` : ""}`;
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
      errEl.textContent = formatPosApiError(err, tx("error_liquidate_selected", "Failed to liquidate selected positions"));
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
      errEl.textContent = formatPosApiError(err, tx("error_liquidate_all", "Failed to liquidate all positions"));
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = tx("liquidate_all", "Liquidate all");
    }
  }
}

/* ---------------------------------------------------------------------------
   Multi Auto-Trade Management (Banner, Modal & Runners API)
   --------------------------------------------------------------------------- */

let activeAutoTradeSymbols = [];
let activeAutoTradeTab = "standard"; // "standard" | "custom"
let customEnginesCache = null;

function renderActiveAutoTradesBanner(data) {
  const banner = $("pos-autotrades-section");
  const list = $("pos-autotrades-list");
  const countEl = $("pos-autotrades-count");
  if (!banner) return;

  const runners = Array.isArray(data?.active_auto_trades) ? data.active_auto_trades : [];
  if (runners.length === 0) {
    banner.hidden = true;
    if (list) list.innerHTML = "";
    if (countEl) countEl.textContent = "0";
    return;
  }

  banner.hidden = false;
  if (countEl) countEl.textContent = String(runners.length);
  if (!list) return;

  list.innerHTML = runners
    .map((r) => {
      const sym = r.symbol;
      const engine = r.engine_name || r.strategy_mode?.toUpperCase() || "Standard";
      const sig = String(r.last_signal || "hold").toLowerCase();
      const px = r.last_price != null ? `$${Number(r.last_price).toFixed(2)}` : "—";

      return `
      <div class="pos-autotrade-chip" data-symbol="${escapeHtml(sym)}">
        <span class="pos-autotrade-chip-sym">${escapeHtml(sym)}</span>
        <span class="pos-autotrade-chip-engine">${escapeHtml(engine)}</span>
        <span class="pos-autotrade-chip-sig ${sig}">${escapeHtml(sig.toUpperCase())}</span>
        <span class="pos-autotrade-chip-price">${px}</span>
        <button type="button" class="pos-autotrade-chip-stop btn-stop-chip-runner" data-symbol="${escapeHtml(sym)}" title="${escapeHtml(tx("stop_autotrade", "Stop Auto-Trade"))}">
          ${escapeHtml(tx("stop", "Stop"))}
        </button>
      </div>`;
    })
    .join("");
}

async function fetchCustomEnginesList() {
  if (customEnginesCache) return customEnginesCache;
  try {
    const res = await fetch("/api/custom-engines");
    if (!res.ok) return [];
    const data = await res.json();
    customEnginesCache = Array.isArray(data.engines) ? data.engines : [];
    return customEnginesCache;
  } catch {
    return [];
  }
}

function syncSizingModeUI(mode) {
  const select = $("pos-autotrade-sizing-mode");
  const lbl = $("pos-autotrade-size-label");
  const chips = $("pos-notional-chips");
  if (select && mode) select.value = mode;
  const currentMode = select ? select.value : mode;
  if (currentMode === "notional") {
    if (lbl) lbl.textContent = tx("trade_notional", "Trade Notional ($)");
    if (chips) {
      chips.hidden = false;
      const curVal = parseFloat($("pos-autotrade-size-input")?.value || "0");
      chips.querySelectorAll(".btn-notional-chip").forEach((btn) => {
        btn.classList.toggle("is-active", parseFloat(btn.dataset.val) === curVal);
      });
    }
  } else {
    if (lbl) lbl.textContent = tx("trade_qty", "Trade Quantity");
    if (chips) chips.hidden = true;
  }
}

async function populateCustomEnginesSelect(selectedId = null) {
  const select = $("pos-custom-engine-select");
  const desc = $("pos-custom-engine-desc");
  if (!select) return;

  // Pair engines trade a long and a short leg together, which a single-ticker
  // runner cannot express — keep them out of the picker.
  const engines = (await fetchCustomEnginesList()).filter(
    (e) => (e.base_engine || e.choices?.strategy_mode || "") !== "pair"
  );
  if (!engines || engines.length === 0) {
    select.innerHTML = `<option value="">${escapeHtml(tx("no_custom_engines", "No custom engines found"))}</option>`;
    if (desc) desc.textContent = "";
    return;
  }

  select.innerHTML = engines
    .map((e) => `<option value="${escapeHtml(e.id)}">${escapeHtml(e.name || e.id)}</option>`)
    .join("");

  if (selectedId && engines.some((e) => e.id === selectedId)) {
    select.value = selectedId;
  }

  const chosen = engines.find((e) => e.id === select.value) || engines[0];
  if (desc && chosen?.description) {
    desc.textContent = chosen.description;
  }
}

async function setAutoTradeTab(tab, selectedCustomId = null) {
  activeAutoTradeTab = tab;
  const tabStd = $("tab-strategy-standard");
  const tabCust = $("tab-strategy-custom");
  const paneStd = $("pane-strategy-standard");
  const paneCust = $("pane-strategy-custom");

  if (tab === "custom") {
    tabStd?.classList.remove("is-active");
    tabStd?.setAttribute("aria-selected", "false");
    tabCust?.classList.add("is-active");
    tabCust?.setAttribute("aria-selected", "true");
    if (paneStd) paneStd.hidden = true;
    if (paneCust) {
      paneCust.hidden = false;
      await populateCustomEnginesSelect(selectedCustomId);
    }
  } else {
    tabCust?.classList.remove("is-active");
    tabCust?.setAttribute("aria-selected", "false");
    tabStd?.classList.add("is-active");
    tabStd?.setAttribute("aria-selected", "true");
    if (paneCust) paneCust.hidden = true;
    if (paneStd) paneStd.hidden = false;
  }
}

async function openAutoTradeModal(symbols) {
  const list = Array.isArray(symbols) ? symbols : [symbols];
  activeAutoTradeSymbols = list.filter(Boolean);
  if (activeAutoTradeSymbols.length === 0) return;

  const titleEl = $("pos-autotrade-modal-title");
  const symBadge = $("pos-autotrade-symbol-badge");
  const statusBadge = $("pos-autotrade-status-badge");
  const chipsEl = $("pos-autotrade-targets-chips");
  const errEl = $("pos-autotrade-modal-error");
  const activeCard = $("pos-autotrade-active-card");
  const submitBtn = $("btn-autotrade-modal-submit");

  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }

  if (chipsEl) {
    chipsEl.innerHTML = activeAutoTradeSymbols
      .map((s) => `<span class="pos-autotrade-target-pill">${escapeHtml(s)}</span>`)
      .join("");
  }

  const isSingle = activeAutoTradeSymbols.length === 1;
  const firstSym = activeAutoTradeSymbols[0];

  if (symBadge) {
    symBadge.textContent = isSingle ? firstSym : `${activeAutoTradeSymbols.length} Holdings`;
  }

  // Check if runner is currently active for this symbol
  const activeRunners = (positionsData?.active_auto_trades || []).filter((r) =>
    activeAutoTradeSymbols.includes(r.symbol)
  );
  const isRunning = activeRunners.length > 0;

  if (statusBadge) {
    statusBadge.hidden = !isRunning;
    statusBadge.textContent = isRunning ? tx("autotrade_running_badge", "Running") : "";
  }

  if (activeCard) {
    if (isRunning) {
      activeCard.hidden = false;
      const runner = activeRunners[0];
      const engineEl = $("pos-autotrade-active-engine");
      const sigEl = $("pos-autotrade-active-signal");
      const pxEl = $("pos-autotrade-active-price");
      const cyclesEl = $("pos-autotrade-active-cycles");
      const uptimeEl = $("pos-autotrade-active-uptime");
      const reasonEl = $("pos-autotrade-active-reason");

      if (engineEl) engineEl.textContent = runner.engine_name || runner.strategy_mode?.toUpperCase() || "Standard";
      if (sigEl) {
        const s = String(runner.last_signal || "hold").toLowerCase();
        sigEl.className = `pos-signal-badge ${s}`;
        sigEl.textContent = s.toUpperCase();
      }
      if (pxEl) pxEl.textContent = runner.last_price != null ? `$${Number(runner.last_price).toFixed(2)}` : "—";
      if (cyclesEl) cyclesEl.textContent = String(runner.cycles_count || 0);
      if (uptimeEl) {
        const sec = runner.uptime_seconds || 0;
        const mins = Math.floor(sec / 60);
        uptimeEl.textContent = mins > 0 ? `${mins}m ${sec % 60}s` : `${sec}s`;
      }
      if (reasonEl) reasonEl.textContent = runner.last_reason || runner.error || "—";
    } else {
      activeCard.hidden = true;
    }
  }

  // Pre-populate form fields from running configuration or position defaults
  if (isRunning) {
    const runner = activeRunners[0];
    if (runner.custom_engine_id) {
      await setAutoTradeTab("custom", runner.custom_engine_id);
    } else {
      await setAutoTradeTab("standard");
      const stdSelect = $("pos-standard-strategy-select");
      if (stdSelect && runner.strategy_mode) {
        stdSelect.value = runner.strategy_mode;
      }
    }

    const tfSelect = $("pos-autotrade-timeframe");
    if (tfSelect && runner.timeframe) {
      tfSelect.value = runner.timeframe;
    }

    const pollSelect = $("pos-autotrade-poll");
    if (pollSelect && runner.poll_seconds) {
      pollSelect.value = String(runner.poll_seconds);
    }

    const sizeMode = runner.settings?.size_mode || (runner.settings?.trade_notional != null ? "notional" : "qty");
    syncSizingModeUI(sizeMode);
    const sizeInp = $("pos-autotrade-size-input");
    if (sizeInp) {
      if (sizeMode === "notional" && runner.settings?.trade_notional != null) {
        sizeInp.value = runner.settings.trade_notional;
      } else if (runner.settings?.trade_qty != null) {
        sizeInp.value = runner.settings.trade_qty;
      }
    }
  } else {
    await setAutoTradeTab("standard");
    const stdSelect = $("pos-standard-strategy-select");
    if (stdSelect) stdSelect.value = "sma";

    const tfSelect = $("pos-autotrade-timeframe");
    if (tfSelect) tfSelect.value = "15Min";

    const pollSelect = $("pos-autotrade-poll");
    if (pollSelect) pollSelect.value = "30";

    syncSizingModeUI("qty");

    const pos = (positionsData?.positions || []).find((p) => p.symbol === firstSym);
    const defQty = pos && pos.qty && Math.abs(parseFloat(pos.qty)) > 0
      ? Math.abs(parseFloat(pos.qty))
      : 1;
    const sizeInp = $("pos-autotrade-size-input");
    if (sizeInp) sizeInp.value = defQty;
  }

  if (submitBtn) {
    submitBtn.disabled = false;
    submitBtn.textContent = isRunning
      ? tx("update_autotrade", "Update Auto-Trade")
      : tx("start_autotrade", "Start Auto-Trade");
  }

  openPosModal("pos-autotrade-modal");
}

function closeAutoTradeModal() {
  closePosModal("pos-autotrade-modal");
  activeAutoTradeSymbols = [];
}

async function submitAutoTradeModal() {
  if (activeAutoTradeSymbols.length === 0) return;

  const errEl = $("pos-autotrade-modal-error");
  const submitBtn = $("btn-autotrade-modal-submit");

  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }

  const isCustom = activeAutoTradeTab === "custom";
  const customEngineId = isCustom ? $("pos-custom-engine-select")?.value : null;
  const strategyMode = isCustom ? "" : ($("pos-standard-strategy-select")?.value || "sma");

  if (isCustom && !customEngineId) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = tx("select_custom_engine_error", "Please select a custom engine.");
    }
    return;
  }

  const timeframe = $("pos-autotrade-timeframe")?.value || "15Min";
  const pollSeconds = parseInt($("pos-autotrade-poll")?.value || "30", 10);
  const sizeMode = $("pos-autotrade-sizing-mode")?.value || "qty";
  const sizeVal = parseFloat($("pos-autotrade-size-input")?.value || "1");

  if (!Number.isFinite(sizeVal) || sizeVal <= 0) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = tx("invalid_size_value", "Please enter a valid positive trade size.");
    }
    return;
  }

  const payload = {
    // A custom engine brings its own base strategy, so leave the mode empty
    // and let the backend resolve it from the engine.
    symbols: activeAutoTradeSymbols,
    strategy_mode: isCustom ? "" : strategyMode || "sma",
    custom_engine_id: customEngineId || null,
    bar_timeframe: timeframe,
    poll_seconds: pollSeconds,
    size_mode: sizeMode,
    trade_qty: sizeMode === "qty" ? sizeVal : null,
    trade_notional: sizeMode === "notional" ? sizeVal : null,
  };

  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = tx("starting_autotrade", "Starting…");
  }

  try {
    const res = await fetch("/api/auto-trade/multi/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    closeAutoTradeModal();
    showToast(tx("autotrade_started_toast", "Auto-Trade started successfully"), "ok");
    await refreshPositions({ quiet: false });
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = formatPosApiError(err, tx("error_start_autotrade", "Failed to start auto-trade"));
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = tx("start_autotrade", "Start Auto-Trade");
    }
  }
}

async function stopAutoTradeForCurrentModal() {
  if (activeAutoTradeSymbols.length === 0) return;
  const stopBtn = $("btn-autotrade-modal-stop");
  if (stopBtn) {
    stopBtn.disabled = true;
    stopBtn.textContent = tx("stopping_autotrade", "Stopping…");
  }

  try {
    await Promise.all(
      activeAutoTradeSymbols.map((sym) =>
        fetch("/api/auto-trade/multi/stop", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol: sym }),
        })
      )
    );
    closeAutoTradeModal();
    showToast(tx("autotrade_stopped_toast", "Auto-Trade stopped"), "ok");
    await refreshPositions({ quiet: false });
  } catch (err) {
    showToast(formatPosApiError(err, tx("error_stop_autotrade", "Failed to stop auto-trade")), "error");
  } finally {
    if (stopBtn) {
      stopBtn.disabled = false;
      stopBtn.textContent = tx("stop_autotrade", "Stop Auto-Trade");
    }
  }
}

async function stopSingleAutoTrade(symbol) {
  try {
    const res = await fetch("/api/auto-trade/multi/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    showToast(tx("autotrade_stopped_toast", "Auto-Trade stopped"), "ok");
    await refreshPositions({ quiet: false });
  } catch (err) {
    showToast(formatPosApiError(err, tx("error_stop_autotrade", "Failed to stop auto-trade")), "error");
  }
}

async function stopAllAutoTrades() {
  try {
    const res = await fetch("/api/auto-trade/multi/stop-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    showToast(tx("all_autotrades_stopped_toast", "All active auto-trades stopped"), "ok");
    await refreshPositions({ quiet: false });
  } catch (err) {
    showToast(formatPosApiError(err, tx("error_stop_autotrade", "Failed to stop auto-trades")), "error");
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
    const actionsToggle = e.target.closest(".pos-card-actions-toggle");
    if (actionsToggle) {
      const actions = actionsToggle.nextElementSibling;
      const sym = actionsToggle.closest(".pos-card")?.dataset.symbol;
      if (actions?.classList.contains("pos-card-actions") && sym) {
        const open = actions.hidden;
        actions.hidden = !open;
        actionsToggle.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) posExpandedCardActions.add(sym);
        else posExpandedCardActions.delete(sym);
      }
      return;
    }
    const exitBtn = e.target.closest(".btn-pos-exit");
    if (exitBtn) {
      const sym = exitBtn.dataset.symbol || exitBtn.closest(".pos-table-row, .pos-card")?.dataset.symbol;
      if (sym) openExitStrategyModal(findPositionBySymbol(sym));
      return;
    }
    const protClick = e.target.closest(".pos-cell-prot, .pos-card-prot, .pos-prot-badge");
    if (protClick && !e.target.closest("a.orders")) {
      const rowOrCard = protClick.closest(".pos-table-row, .pos-card");
      const sym = rowOrCard?.dataset.symbol;
      if (sym) {
        const pObj = findPositionBySymbol(sym);
        const hasBoth = !!(pObj?.has_stop_loss && pObj?.has_take_profit);
        const initialMode = hasBoth
          ? "bracket"
          : (protClick.closest(".pos-prot-badge")?.classList.contains("tp") ? "take_profit" : "stop_loss");
        openExitStrategyModal(pObj, initialMode);
        return;
      }
    }
    const lotsBtn = e.target.closest(".btn-pos-lots");
    if (lotsBtn) {
      openLotsModal(lotsBtn.dataset.symbol).catch(() => {});
      return;
    }
    const autoTradeBtn = e.target.closest(".btn-pos-autotrade");
    if (autoTradeBtn) {
      const sym = autoTradeBtn.dataset.symbol || autoTradeBtn.closest(".pos-table-row, .pos-card")?.dataset.symbol;
      if (sym) openAutoTradeModal(sym);
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
  else if (id === "pos-exit-modal") closeExitStrategyModal();
  else if (id === "pos-autotrade-modal") closeAutoTradeModal();
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
  $("pos-kpi-green-tile")?.addEventListener("click", () => applyPnlFilter("winners"));
  $("pos-kpi-red-tile")?.addEventListener("click", () => applyPnlFilter("losers"));

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

  // Mobile filters toggle — desktop never shows this button, so the handler
  // is harmless dead weight there rather than something that needs gating.
  $("btn-toggle-pos-filters")?.addEventListener("click", togglePosFiltersBar);

  // The legend buttons and allocation bar segments filter the table.
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
  $("pos-allocation-bar")?.addEventListener("click", (e) => {
    const seg = e.target.closest(".pos-alloc-segment:not(.is-cash)");
    if (seg?.dataset.symbol) focusSymbol(seg.dataset.symbol);
  });

  $("btn-batch-deselect")?.addEventListener("click", () => {
    posSelectedSymbols.clear();
    renderPositionsPage();
  });
  $("btn-batch-autotrade")?.addEventListener("click", () => {
    if (posSelectedSymbols.size === 0) return;
    openAutoTradeModal(Array.from(posSelectedSymbols));
  });
  $("btn-batch-liquidate")?.addEventListener("click", openLiquidateSelectedModal);

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

  // Exit Strategy Modal: Tabs
  document.querySelectorAll(".pos-exit-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      setExitMode(tab.dataset.exitMode);
    });
  });

  // Exit Strategy Modal: Inputs live calculations
  $("pos-exit-sl-price")?.addEventListener("input", () => {
    if (activeExitPosition) {
      const currPx = Number(activeExitPosition.current_price || 0);
      const slPx = Number($("pos-exit-sl-price")?.value || 0);
      const isShort = String(activeExitPosition.side || "").toLowerCase() === "short";
      const distBadge = $("pos-exit-sl-dist-badge");
      const customWrap = $("pos-exit-sl-custom-wrap");
      const customInput = $("pos-exit-sl-custom-input");
      if (slPx > 0 && currPx > 0) {
        const diffPct = isShort ? ((slPx - currPx) / currPx) * 100 : ((currPx - slPx) / currPx) * 100;
        if (distBadge) distBadge.textContent = diffPct > 0 ? `${diffPct.toFixed(1)}%` : "—";
        let matchedChip = null;
        document.querySelectorAll("[data-sl-pct]").forEach((c) => {
          const p = Number(c.dataset.slPct);
          if (Math.abs(p - diffPct) < 0.05) matchedChip = c;
        });
        if (matchedChip) {
          document.querySelectorAll("[data-sl-pct]").forEach((c) => c.classList.toggle("is-active", c === matchedChip));
          if (customWrap) customWrap.classList.remove("is-active");
          if (customInput && document.activeElement !== customInput) customInput.value = "";
        } else if (diffPct > 0) {
          document.querySelectorAll("[data-sl-pct]").forEach((c) => c.classList.remove("is-active"));
          if (customWrap) customWrap.classList.add("is-active");
          if (customInput && document.activeElement !== customInput) {
            customInput.value = diffPct.toFixed(1);
          }
        } else {
          document.querySelectorAll("[data-sl-pct]").forEach((c) => c.classList.remove("is-active"));
          if (customWrap) customWrap.classList.remove("is-active");
        }
      } else {
        if (distBadge) distBadge.textContent = "—";
        document.querySelectorAll("[data-sl-pct]").forEach((c) => c.classList.remove("is-active"));
        if (customWrap) customWrap.classList.remove("is-active");
      }
    }
    updateExitCalculations();
  });
  $("pos-exit-trail-pct")?.addEventListener("input", (e) => {
    const trailVal = Number(e.target.value || 0);
    let matched = null;
    document.querySelectorAll("[data-trail-pct]").forEach((c) => {
      if (Math.abs(Number(c.dataset.trailPct) - trailVal) < 0.05) matched = c;
    });
    document.querySelectorAll("[data-trail-pct]").forEach((c) => c.classList.toggle("is-active", c === matched));
    updateExitCalculations();
  });

  $("pos-exit-tp-price")?.addEventListener("input", (e) => {
    if (!activeExitPosition) return;
    const tpPx = Number(e.target.value || 0);
    const currPx = Number(activeExitPosition.current_price || 0);
    const isShort = String(activeExitPosition.side || "").toLowerCase() === "short";
    if (tpPx > 0 && currPx > 0) {
      const diffPct = isShort ? ((currPx - tpPx) / currPx) * 100 : ((tpPx - currPx) / currPx) * 100;
      let matched = null;
      document.querySelectorAll("[data-tp-pct]").forEach((c) => {
        if (Math.abs(Number(c.dataset.tpPct) - diffPct) < 0.05) matched = c;
      });
      document.querySelectorAll("[data-tp-pct]").forEach((c) => c.classList.toggle("is-active", c === matched));
    } else {
      document.querySelectorAll("[data-tp-pct]").forEach((c) => c.classList.remove("is-active"));
    }
    updateExitCalculations();
  });

  $("pos-exit-bracket-sl")?.addEventListener("input", (e) => {
    if (!activeExitPosition) return;
    const slPx = Number(e.target.value || 0);
    const currPx = Number(activeExitPosition.current_price || 0);
    const isShort = String(activeExitPosition.side || "").toLowerCase() === "short";
    if (slPx > 0 && currPx > 0) {
      const diffPct = isShort ? ((slPx - currPx) / currPx) * 100 : ((currPx - slPx) / currPx) * 100;
      let matched = null;
      document.querySelectorAll("[data-bracket-sl-pct]").forEach((c) => {
        if (Math.abs(Number(c.dataset.bracketSlPct) - diffPct) < 0.05) matched = c;
      });
      document.querySelectorAll("[data-bracket-sl-pct]").forEach((c) => c.classList.toggle("is-active", c === matched));
    } else {
      document.querySelectorAll("[data-bracket-sl-pct]").forEach((c) => c.classList.remove("is-active"));
    }
    updateExitCalculations();
  });

  $("pos-exit-bracket-tp")?.addEventListener("input", (e) => {
    if (!activeExitPosition) return;
    const tpPx = Number(e.target.value || 0);
    const currPx = Number(activeExitPosition.current_price || 0);
    const isShort = String(activeExitPosition.side || "").toLowerCase() === "short";
    if (tpPx > 0 && currPx > 0) {
      const diffPct = isShort ? ((currPx - tpPx) / currPx) * 100 : ((tpPx - currPx) / currPx) * 100;
      let matched = null;
      document.querySelectorAll("[data-bracket-tp-pct]").forEach((c) => {
        if (Math.abs(Number(c.dataset.bracketTpPct) - diffPct) < 0.05) matched = c;
      });
      document.querySelectorAll("[data-bracket-tp-pct]").forEach((c) => c.classList.toggle("is-active", c === matched));
    } else {
      document.querySelectorAll("[data-bracket-tp-pct]").forEach((c) => c.classList.remove("is-active"));
    }
    updateExitCalculations();
  });

  // Exit Strategy Modal: Quick percentage chips
  document.querySelectorAll("[data-sl-pct]").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!activeExitPosition) return;
      const pct = Number(chip.dataset.slPct || 3);
      const currPx = Number(activeExitPosition.current_price || 0);
      const isShort = String(activeExitPosition.side || "").toLowerCase() === "short";
      const target = isShort ? currPx * (1 + pct / 100) : currPx * (1 - pct / 100);
      const slInput = $("pos-exit-sl-price");
      if (slInput) slInput.value = target.toFixed(2);
      document.querySelectorAll("[data-sl-pct]").forEach((c) => c.classList.toggle("is-active", c === chip));
      const customWrap = $("pos-exit-sl-custom-wrap");
      if (customWrap) customWrap.classList.remove("is-active");
      const customInput = $("pos-exit-sl-custom-input");
      if (customInput) customInput.value = "";
      const distBadge = $("pos-exit-sl-dist-badge");
      if (distBadge) distBadge.textContent = `${pct.toFixed(1)}%`;
      updateExitCalculations();
    });
  });

  // Custom Risk Distance Inline Input
  $("pos-exit-sl-custom-input")?.addEventListener("focus", () => {
    document.querySelectorAll("[data-sl-pct]").forEach((c) => c.classList.remove("is-active"));
    $("pos-exit-sl-custom-wrap")?.classList.add("is-active");
  });

  $("pos-exit-sl-custom-input")?.addEventListener("input", (e) => {
    if (!activeExitPosition) return;
    document.querySelectorAll("[data-sl-pct]").forEach((c) => c.classList.remove("is-active"));
    $("pos-exit-sl-custom-wrap")?.classList.add("is-active");
    const pct = Number(e.target.value);
    const currPx = Number(activeExitPosition.current_price || 0);
    const isShort = String(activeExitPosition.side || "").toLowerCase() === "short";
    const distBadge = $("pos-exit-sl-dist-badge");
    if (pct > 0 && currPx > 0) {
      const target = isShort ? currPx * (1 + pct / 100) : currPx * (1 - pct / 100);
      const slInput = $("pos-exit-sl-price");
      if (slInput) slInput.value = target.toFixed(2);
      if (distBadge) distBadge.textContent = `${pct.toFixed(1)}%`;
      updateExitCalculations();
    } else if (distBadge) {
      distBadge.textContent = "—";
    }
  });

  // Exit Strategy Modal: Quantity percentage chips
  document.querySelectorAll("[data-exit-qty-pct]").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!activeExitPosition) return;
      const pct = Number(chip.dataset.exitQtyPct || 100);
      const heldQty = Math.abs(Number(activeExitPosition.qty || 0));
      let q = heldQty;
      if (pct < 100) {
        q = Number.isInteger(heldQty)
          ? Math.max(1, Math.round((heldQty * pct) / 100))
          : Number(((heldQty * pct) / 100).toFixed(4));
      }
      const qtyInput = $("pos-exit-qty-input");
      if (qtyInput) qtyInput.value = q;
      document.querySelectorAll("[data-exit-qty-pct]").forEach((c) => c.classList.toggle("is-active", c === chip));
      syncExitQtyUi(q, heldQty);
      updateExitCalculations();
    });
  });

  // Exit Strategy Modal: Quantity input live sync
  $("pos-exit-qty-input")?.addEventListener("input", (e) => {
    if (!activeExitPosition) return;
    const heldQty = Math.abs(Number(activeExitPosition.qty || 0));
    const val = parseFloat(e.target.value) || 0;
    const pct = heldQty > 0 ? (val / heldQty) * 100 : 0;
    document.querySelectorAll("[data-exit-qty-pct]").forEach((c) => {
      const chipPct = Number(c.dataset.exitQtyPct);
      c.classList.toggle("is-active", Math.abs(chipPct - pct) < 0.1);
    });
    syncExitQtyUi(val, heldQty);
    updateExitCalculations();
  });

  document.querySelectorAll("[data-trail-pct]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const pct = Number(chip.dataset.trailPct || 3);
      const trailInput = $("pos-exit-trail-pct");
      if (trailInput) trailInput.value = pct.toFixed(1);
      document.querySelectorAll("[data-trail-pct]").forEach((c) => c.classList.toggle("is-active", c === chip));
      updateExitCalculations();
    });
  });

  document.querySelectorAll("[data-tp-pct]").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!activeExitPosition) return;
      const pct = Number(chip.dataset.tpPct || 5);
      const currPx = Number(activeExitPosition.current_price || 0);
      const isShort = String(activeExitPosition.side || "").toLowerCase() === "short";
      const target = isShort ? currPx * (1 - pct / 100) : currPx * (1 + pct / 100);
      const tpInput = $("pos-exit-tp-price");
      if (tpInput) tpInput.value = target.toFixed(2);
      document.querySelectorAll("[data-tp-pct]").forEach((c) => c.classList.toggle("is-active", c === chip));
      updateExitCalculations();
    });
  });

  document.querySelectorAll("[data-bracket-sl-pct]").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!activeExitPosition) return;
      const pct = Number(chip.dataset.bracketSlPct || 3);
      const currPx = Number(activeExitPosition.current_price || 0);
      const isShort = String(activeExitPosition.side || "").toLowerCase() === "short";
      const target = isShort ? currPx * (1 + pct / 100) : currPx * (1 - pct / 100);
      const slInput = $("pos-exit-bracket-sl");
      if (slInput) slInput.value = target.toFixed(2);
      document.querySelectorAll("[data-bracket-sl-pct]").forEach((c) => c.classList.toggle("is-active", c === chip));
      updateExitCalculations();
    });
  });

  document.querySelectorAll("[data-bracket-tp-pct]").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!activeExitPosition) return;
      const pct = Number(chip.dataset.bracketTpPct || 10);
      const currPx = Number(activeExitPosition.current_price || 0);
      const isShort = String(activeExitPosition.side || "").toLowerCase() === "short";
      const target = isShort ? currPx * (1 - pct / 100) : currPx * (1 + pct / 100);
      const tpInput = $("pos-exit-bracket-tp");
      if (tpInput) tpInput.value = target.toFixed(2);
      document.querySelectorAll("[data-bracket-tp-pct]").forEach((c) => c.classList.toggle("is-active", c === chip));
      updateExitCalculations();
    });
  });

  // Exit Strategy Modal: Dip Hunt live inputs
  $("pos-exit-dip-hunt-enabled")?.addEventListener("change", () => {
    syncExitDipHuntUI();
  });
  $("pos-exit-dip-hunt-wait")?.addEventListener("input", () => {
    syncExitDipHuntUI();
  });
  $("pos-exit-dip-hunt-pct")?.addEventListener("input", () => {
    syncExitDipHuntUI();
  });

  // Modal Actions
  $("btn-close-modal-x")?.addEventListener("click", closeClosePositionModal);
  $("btn-close-modal-cancel")?.addEventListener("click", closeClosePositionModal);
  $("btn-close-modal-submit")?.addEventListener("click", submitClosePosition);

  $("btn-exit-modal-x")?.addEventListener("click", closeExitStrategyModal);
  $("btn-exit-modal-cancel")?.addEventListener("click", closeExitStrategyModal);
  $("btn-exit-modal-submit")?.addEventListener("click", submitExitStrategy);

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

  // Multi Auto-Trade Modal & Banner Actions
  $("btn-autotrade-modal-x")?.addEventListener("click", closeAutoTradeModal);
  $("btn-autotrade-modal-cancel")?.addEventListener("click", closeAutoTradeModal);
  $("btn-autotrade-modal-submit")?.addEventListener("click", submitAutoTradeModal);
  $("btn-autotrade-modal-stop")?.addEventListener("click", stopAutoTradeForCurrentModal);
  $("tab-strategy-standard")?.addEventListener("click", () => setAutoTradeTab("standard"));
  $("tab-strategy-custom")?.addEventListener("click", () => setAutoTradeTab("custom"));
  $("pos-custom-engine-select")?.addEventListener("change", (e) => {
    const desc = $("pos-custom-engine-desc");
    if (!desc || !customEnginesCache) return;
    const selected = customEnginesCache.find((eng) => eng.id === e.target.value);
    desc.textContent = selected?.description || "";
  });
  $("pos-autotrade-sizing-mode")?.addEventListener("change", (e) => {
    const inp = $("pos-autotrade-size-input");
    if (e.target.value === "notional") {
      if (inp && Number(inp.value) === 1) inp.value = "500";
    } else {
      if (inp && Number(inp.value) === 500) inp.value = "1";
    }
    syncSizingModeUI(e.target.value);
  });
  $("pos-notional-chips")?.addEventListener("click", (e) => {
    const chip = e.target.closest(".btn-notional-chip");
    if (!chip) return;
    const val = chip.dataset.val;
    const inp = $("pos-autotrade-size-input");
    if (inp && val) {
      inp.value = val;
      syncSizingModeUI("notional");
    }
  });
  $("pos-autotrade-size-input")?.addEventListener("input", () => {
    const mode = $("pos-autotrade-sizing-mode")?.value || "qty";
    syncSizingModeUI(mode);
  });
  $("btn-stop-all-autotrades")?.addEventListener("click", stopAllAutoTrades);
  $("pos-autotrades-list")?.addEventListener("click", (e) => {
    const stopBtn = e.target.closest(".btn-stop-chip-runner");
    if (stopBtn && stopBtn.dataset.symbol) {
      stopSingleAutoTrade(stopBtn.dataset.symbol);
      return;
    }
    const chip = e.target.closest(".pos-autotrade-chip");
    if (chip && chip.dataset.symbol) {
      openAutoTradeModal(chip.dataset.symbol);
    }
  });

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
