/**
 * Shared Backtest-Family JavaScript for AlgoPaca
 * State, storage, result rendering, history list, and compare logic shared by
 * the Backtest, Backtest History, and Backtest Compare pages.
 */

const BT_VIEW_STORAGE_KEY = "alpaca-desk-backtest-view";

const BT_RESULT_STORAGE_KEY = "alpaca-desk-backtest-last-result";

function readBacktestViewState() {
  try {
    const raw = localStorage.getItem(BT_VIEW_STORAGE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    return data && typeof data === "object" ? data : null;
  } catch {
    return null;
  }
}

function readBacktestLastResult() {
  try {
    const raw = localStorage.getItem(BT_RESULT_STORAGE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || typeof data !== "object" || !data.result) return null;
    return data;
  } catch {
    return null;
  }
}

function saveBacktestViewState() {
  try {
    localStorage.setItem(
      BT_VIEW_STORAGE_KEY,
      JSON.stringify({
        activeHistoryId:
          btActiveHistoryId != null && Number.isFinite(Number(btActiveHistoryId))
            ? Number(btActiveHistoryId)
            : null,
        selectedHistoryIds: [...btSelectedHistoryIds]
          .map((id) => Number(id))
          .filter((id) => Number.isFinite(id)),
      })
    );
  } catch {
    /* ignore quota / private mode */
  }
}

function restoreBtSelectedHistoryIds() {
  const view = readBacktestViewState();
  const ids = Array.isArray(view?.selectedHistoryIds)
    ? view.selectedHistoryIds
    : [];
  btSelectedHistoryIds.clear();
  for (const raw of ids) {
    const id = Number(raw);
    if (Number.isFinite(id)) btSelectedHistoryIds.add(id);
  }
}

function saveBacktestLastResult(result, historyId = btActiveHistoryId) {
  if (!result || typeof result !== "object") return;
  const payload = {
    historyId:
      historyId != null && Number.isFinite(Number(historyId))
        ? Number(historyId)
        : null,
    result,
    savedAt: Date.now(),
  };
  try {
    localStorage.setItem(BT_RESULT_STORAGE_KEY, JSON.stringify(payload));
    return;
  } catch {
    /* try a lighter payload if quota is tight */
  }
  try {
    const curve = Array.isArray(result.equity_curve) ? result.equity_curve : [];
    const trades = Array.isArray(result.trade_list) ? result.trade_list : [];
    const light = {
      ...payload,
      result: {
        ...result,
        equity_curve: curve.length > 200 ? curve.filter((_, i) => i % 2 === 0) : curve,
        trade_list: trades.slice(-80),
      },
    };
    localStorage.setItem(BT_RESULT_STORAGE_KEY, JSON.stringify(light));
  } catch {
    /* ignore quota / private mode */
  }
}

function clearBacktestLastResult() {
  try {
    localStorage.removeItem(BT_RESULT_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

function clearBacktestResultsPanel() {
  btActiveHistoryId = null;
  btMultiResultCache = null;
  saveBacktestViewState();
  clearBacktestLastResult();
  const box = $("bt-results");
  if (box) box.hidden = true;
  const multiWrap = $("bt-multi-wrap");
  if (multiWrap) multiWrap.hidden = true;
  const stratBadge = $("bt-strategy-badge");
  if (stratBadge) stratBadge.textContent = "";
  syncBtHistorySelect(null);
  destroyBtEquityChart();
  btChartCache = null;
}

function restoreBacktestLastResult() {
  const cached = readBacktestLastResult();
  if (!cached?.result) return false;
  if (cached.historyId != null && Number.isFinite(Number(cached.historyId))) {
    btActiveHistoryId = Number(cached.historyId);
  }
  renderBacktestResult(cached.result, {
    historyId: btActiveHistoryId,
    scroll: false,
    quiet: true,
    skipCache: true,
  });
  return true;
}

let btEquityChart = null;

let btCompareChart = null;

let btChartCache = null;

let btHistorySummaries = [];

let btActiveHistoryId = null;

const btSelectedHistoryIds = new Set();

const BT_COMPARE_MAX = 4;

const BT_COMPARE_COLORS = ["#d4894c", "#5b9fd4", "#3fbf8f", "#c9a0dc"];

const btChartSeries = {
  strategy: true,
  hold: true,
  drawdown: false,
  trades: true,
};

/* parseBtTime, MONTHS_LONG, MONTHS_SHORT and formatDisplayDate live in common.js */

function formatBtAxisDate(iso) {
  const t = parseBtTime(iso);
  if (!Number.isFinite(t)) return String(iso || "").slice(0, 10) || "—";
  const d = new Date(t);
  return `${d.getUTCDate()} ${MONTHS_SHORT[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

function formatBtTooltipDate(iso) {
  return formatDisplayDate(iso, { withTime: true });
}

function buildBtDrawdown(equities) {
  let peak = equities[0] || 0;
  return equities.map((v) => {
    if (v > peak) peak = v;
    if (!peak) return 0;
    return -((peak - v) / peak) * 100;
  });
}

function nearestCurveIndex(pts, tradeTime) {
  const target = parseBtTime(tradeTime);
  if (!Number.isFinite(target) || !pts.length) return -1;
  let best = 0;
  let bestDiff = Infinity;
  for (let i = 0; i < pts.length; i += 1) {
    const t = parseBtTime(pts[i].t);
    if (!Number.isFinite(t)) continue;
    const diff = Math.abs(t - target);
    if (diff < bestDiff) {
      bestDiff = diff;
      best = i;
    }
  }
  return best;
}

function destroyBtEquityChart() {
  if (btEquityChart) {
    btEquityChart.destroy();
    btEquityChart = null;
  }
}

function syncBtChartToggles() {
  document.querySelectorAll("[data-bt-series]").forEach((btn) => {
    const key = btn.getAttribute("data-bt-series");
    const on = !!btChartSeries[key];
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function renderEquityChart(curve, initialCash, trades = []) {
  const canvas = $("bt-equity-chart");
  if (!canvas) return;
  if (typeof Chart === "undefined") {
    console.warn("Chart.js is not loaded");
    return;
  }

  const pts = Array.isArray(curve) ? curve : [];
  destroyBtEquityChart();
  if (pts.length < 2) {
    btChartCache = null;
    return;
  }

  const cash = Number(initialCash) || Number(pts[0]?.equity) || 1;
  const firstPx = Number(pts[0]?.price) || 0;
  const labels = pts.map((p) => String(p.t || ""));
  const strategy = pts.map((p) => Number(p.equity));
  const hasHoldEquity = pts.some(
    (p) => p.hold_equity != null && Number.isFinite(Number(p.hold_equity))
  );
  const hold = pts.map((p) => {
    if (hasHoldEquity && p.hold_equity != null) {
      return +Number(p.hold_equity).toFixed(2);
    }
    const px = Number(p.price);
    if (!firstPx || !px) return cash;
    return +(cash * (px / firstPx)).toFixed(2);
  });
  const drawdown = buildBtDrawdown(strategy);
  const prices = pts.map((p) => Number(p.price));

  const buyPoints = [];
  const sellPoints = [];
  for (const trade of Array.isArray(trades) ? trades : []) {
    const idx = nearestCurveIndex(pts, trade.time);
    if (idx < 0) continue;
    const point = {
      x: labels[idx],
      y: strategy[idx],
      trade,
    };
    if (String(trade.side || "").toLowerCase() === "buy") buyPoints.push(point);
    else if (String(trade.side || "").toLowerCase() === "sell") sellPoints.push(point);
  }

  btChartCache = {
    labels,
    strategy,
    hold,
    drawdown,
    prices,
    buyPoints,
    sellPoints,
  };

  const copper = cssVar("--copper", "#d4894c");
  const muted = cssVar("--muted", "#9aa8b8");
  const buy = cssVar("--buy", "#3fbf8f");
  const sell = cssVar("--sell", "#e35d5d");
  const text = cssVar("--text", "#f2ebe1");
  const line = cssVar("--line", "#2a384c");
  const mono = cssVar("--mono", "IBM Plex Mono, monospace");

  const tickCount = Math.min(8, Math.max(4, Math.floor(labels.length / 18) || 4));

  btEquityChart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Strategy",
          data: strategy,
          borderColor: copper,
          backgroundColor: "rgba(212, 137, 76, 0.12)",
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: copper,
          tension: 0.15,
          fill: true,
          yAxisID: "y",
          hidden: !btChartSeries.strategy,
          order: 2,
        },
        {
          label: "Buy & hold",
          data: hold,
          borderColor: muted,
          backgroundColor: "transparent",
          borderWidth: 1.75,
          borderDash: [5, 4],
          pointRadius: 0,
          pointHoverRadius: 3,
          tension: 0.15,
          fill: false,
          yAxisID: "y",
          hidden: !btChartSeries.hold,
          order: 3,
        },
        {
          label: "Drawdown %",
          data: drawdown,
          borderColor: sell,
          backgroundColor: "rgba(227, 93, 93, 0.12)",
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 3,
          tension: 0.1,
          fill: true,
          yAxisID: "y1",
          hidden: !btChartSeries.drawdown,
          order: 4,
        },
        {
          label: "Buys",
          type: "scatter",
          data: buyPoints,
          parsing: false,
          showLine: false,
          pointRadius: 5,
          pointHoverRadius: 7,
          pointStyle: "triangle",
          backgroundColor: buy,
          borderColor: buy,
          yAxisID: "y",
          hidden: !btChartSeries.trades,
          order: 1,
        },
        {
          label: "Sells",
          type: "scatter",
          data: sellPoints,
          parsing: false,
          showLine: false,
          pointRadius: 5,
          pointHoverRadius: 7,
          pointStyle: "triangle",
          rotation: 180,
          backgroundColor: sell,
          borderColor: sell,
          yAxisID: "y",
          hidden: !btChartSeries.trades,
          order: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: "index",
        intersect: false,
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "rgba(12, 18, 25, 0.94)",
          titleColor: text,
          bodyColor: muted,
          borderColor: line,
          borderWidth: 1,
          padding: 10,
          titleFont: { family: mono, size: 12, weight: "500" },
          bodyFont: { family: mono, size: 11 },
          displayColors: true,
          callbacks: {
            title(items) {
              const idx = items[0]?.dataIndex;
              const fromScatter = items[0]?.raw?.trade?.time;
              if (fromScatter) return formatBtTooltipDate(fromScatter);
              if (idx == null) return "";
              return formatBtTooltipDate(labels[idx]);
            },
            label(ctx) {
              const label = ctx.dataset.label || "";
              if (label === "Buys" || label === "Sells") {
                const trade = ctx.raw?.trade;
                if (!trade) return `${label}`;
                const pnl =
                  trade.pnl != null ? ` · ${formatPnl(trade.pnl)}` : "";
                return `${String(trade.side || label).toUpperCase()} @ ${money(trade.price)}${pnl}`;
              }
              if (label === "Drawdown %") {
                return `Drawdown ${Number(ctx.parsed.y).toFixed(2)}%`;
              }
              return `${label}: ${money(ctx.parsed.y)}`;
            },
            afterBody(items) {
              const idx = items[0]?.dataIndex;
              if (idx == null || items[0]?.raw?.trade) return [];
              const px = prices[idx];
              const lines = [];
              if (Number.isFinite(px)) lines.push(`Bar close: ${money(px)}`);
              const strat = strategy[idx];
              const bhVal = hold[idx];
              if (Number.isFinite(strat) && Number.isFinite(bhVal)) {
                const delta = strat - bhVal;
                lines.push(
                  `vs hold: ${delta >= 0 ? "+" : ""}${money(delta)}`
                );
              }
              return lines;
            },
          },
        },
      },
      scales: {
        x: {
          type: "category",
          ticks: {
            color: muted,
            font: { family: mono, size: 10 },
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: tickCount,
            callback(value) {
              const label = this.getLabelForValue(value);
              return formatBtAxisDate(label);
            },
          },
          grid: {
            color: "rgba(42, 56, 76, 0.45)",
            drawTicks: false,
          },
          border: { color: line },
        },
        y: {
          position: "left",
          ticks: {
            color: muted,
            font: { family: mono, size: 10 },
            callback: (v) => {
              const n = Number(v);
              if (!Number.isFinite(n)) return v;
              if (Math.abs(n) >= 1000) return `$${(n / 1000).toFixed(1)}k`;
              return `$${n.toFixed(0)}`;
            },
          },
          grid: {
            color: "rgba(42, 56, 76, 0.35)",
          },
          border: { color: line },
          title: {
            display: true,
            text: "Equity ($)",
            color: muted,
            font: { family: mono, size: 10 },
          },
        },
        y1: {
          position: "right",
          display: btChartSeries.drawdown,
          ticks: {
            color: sell,
            font: { family: mono, size: 10 },
            callback: (v) => `${Number(v).toFixed(0)}%`,
          },
          grid: { drawOnChartArea: false },
          border: { color: line },
          title: {
            display: true,
            text: "Drawdown %",
            color: sell,
            font: { family: mono, size: 10 },
          },
        },
      },
    },
  });

  const start = formatBtTooltipDate(labels[0]);
  const end = formatBtTooltipDate(labels[labels.length - 1]);
  const caption = $("bt-chart-caption");
  if (caption) {
    caption.textContent =
      `${start} → ${end} · hover for date & equity · toggle series above`;
  }
  syncBtChartToggles();
}

function formatBtStrategyName(result) {
  if (!result) return "";
  const mode = String(result.mode || "").toLowerCase();
  const tr = (k, fb) => (typeof window.t === "function" ? window.t(k, fb) : fb);

  let modeName = "";
  if (mode === "dip") modeName = tr("mode_dip", "Buy the dip");
  else if (mode === "sma") modeName = tr("mode_sma", "SMA crossover");
  else if (mode === "pair") modeName = tr("mode_pair", "Long & Short Pair");
  else if (mode === "ls") modeName = tr("mode_ls", "Regime Dual Momentum (L/S)");
  else if (mode === "day") modeName = tr("mode_day", "Day trading (VWAP & ORB)");
  else modeName = result.mode ? String(result.mode).toUpperCase() : "";

  const label = result.params?.label || result.day_preset_label;
  if (!label || label.toLowerCase() === modeName.toLowerCase()) {
    return modeName;
  }
  return `${modeName} · ${label}`;
}

function syncBtHistorySelect(activeId) {
  const sel = $("bt-history-select");
  if (!sel) return;
  const currentId =
    activeId != null ? Number(activeId) : Number(btActiveHistoryId);
  const runs = Array.isArray(btHistorySummaries) ? btHistorySummaries : [];

  sel.replaceChildren();

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent =
    typeof window.t === "function"
      ? window.t("select_backtest_run", "Select previous run...")
      : "Select previous run...";
  if (!currentId || !runs.some((r) => Number(r.id) === currentId)) {
    placeholder.selected = true;
  }
  sel.appendChild(placeholder);

  for (const row of runs) {
    const opt = document.createElement("option");
    opt.value = String(row.id);
    const idNum = row.id != null ? `#${row.id}` : "";
    let symStr = String(row.symbol || "—");
    if (row.run_kind === "portfolio") {
      symStr = `Portfolio (${row.symbol_count || 1} sym)`;
    } else if (Number(row.symbol_count) > 1) {
      const firstSym = symStr.split("+")[0];
      symStr = `${firstSym} (+${Number(row.symbol_count) - 1})`;
    }
    const label = row.label || row.day_preset_label || row.mode || "";
    const ret = Number(row.total_return_pct);
    const retStr = Number.isFinite(ret)
      ? `${ret >= 0 ? "+" : ""}${ret.toFixed(2)}%`
      : "";
    const parts = [idNum, symStr, label, retStr].filter(Boolean);
    opt.textContent = parts.join(" · ");
    if (currentId && Number(row.id) === currentId) {
      opt.selected = true;
    }
    sel.appendChild(opt);
  }

  ensureNiceSelect(sel);
}

function getAggregateTradeList(result) {
  if (Array.isArray(result.trade_list) && result.trade_list.length > 0) {
    return result.trade_list;
  }
  if (!Array.isArray(result.results) || !result.results.length) return [];
  const all = [];
  for (const r of result.results) {
    if (r.error || !Array.isArray(r.trade_list)) continue;
    for (const t of r.trade_list) {
      all.push({ ...t, symbol: t.symbol || r.symbol });
    }
  }
  all.sort((a, b) => {
    const ta = a.time || a.entry_time || "";
    const tb = b.time || b.entry_time || "";
    return String(tb).localeCompare(String(ta));
  });
  return all;
}

function getAggregateEquityCurve(result) {
  if (Array.isArray(result.equity_curve) && result.equity_curve.length >= 2) {
    return result.equity_curve;
  }
  if (!Array.isArray(result.results) || !result.results.length) return [];
  const validLegs = result.results.filter(
    (r) => !r.error && Array.isArray(r.equity_curve) && r.equity_curve.length >= 2
  );
  if (!validLegs.length) return [];

  const map = new Map();
  for (const leg of validLegs) {
    for (const pt of leg.equity_curve) {
      if (!pt || !pt.t) continue;
      if (!map.has(pt.t)) {
        map.set(pt.t, { equities: [], holdEquities: [] });
      }
      const entry = map.get(pt.t);
      if (pt.equity != null && Number.isFinite(Number(pt.equity))) {
        entry.equities.push(Number(pt.equity));
      }
      if (pt.hold_equity != null && Number.isFinite(Number(pt.hold_equity))) {
        entry.holdEquities.push(Number(pt.hold_equity));
      }
    }
  }

  const sortedTimes = Array.from(map.keys()).sort();
  if (sortedTimes.length < 2) return [];

  const curve = [];
  for (const t of sortedTimes) {
    const { equities, holdEquities } = map.get(t);
    const avgEq = equities.length
      ? equities.reduce((a, b) => a + b, 0) / equities.length
      : null;
    const avgHold = holdEquities.length
      ? holdEquities.reduce((a, b) => a + b, 0) / holdEquities.length
      : null;
    if (avgEq != null) {
      curve.push({
        t,
        equity: Math.round(avgEq * 100) / 100,
        hold_equity: avgHold != null ? Math.round(avgHold * 100) / 100 : avgEq,
      });
    }
  }
  return curve;
}

function renderBacktestResult(result, options = {}) {
  const box = $("bt-results");
  if (!box || !result) return;
  box.hidden = false;
  if (options.historyId != null) {
    btActiveHistoryId = Number(options.historyId);
    saveBacktestViewState();
  }
  if (!options.skipCache) {
    saveBacktestLastResult(result, btActiveHistoryId);
  }

  btMultiResultCache = result;
  const multi = isMultiBacktestResult(result);
  const multiWrap = $("bt-multi-wrap");
  if (multiWrap) multiWrap.hidden = !multi;

  const stratBadge = $("bt-strategy-badge");
  if (stratBadge) {
    stratBadge.textContent = formatBtStrategyName(result);
  }

  syncBtHistorySelect(btActiveHistoryId);

  let view = result;
  if (multi) {
    const defaultSymbol =
      result.run_kind === "portfolio" ? "__book__" : "__summary__";
    const selected = options.detailSymbol || defaultSymbol;
    renderBtSymbolTable(result, selected);
    view = resolveBtDetailView(result, selected);
  } else if (multiTable) {
    multiTable.replaceChildren();
  }

  fillBtSummaryMetrics(view, result, { multi });

  const meta = $("bt-meta");
  if (meta) {
    const label = result.params?.label || result.mode;
    const stop =
      result.stop_loss_pct > 0 ? ` · stop ${result.stop_loss_pct}%` : "";
    const open =
      Number(view.open_qty) !== 0 && Number.isFinite(Number(view.open_qty))
        ? ` · open ${formatBtShares(view.open_qty)} @ ${money(view.open_entry)}` +
          (view.open_mark != null ? ` mark ${money(view.open_mark)}` : "") +
          (view.unrealized_pnl != null
            ? ` (${formatPnl(view.unrealized_pnl)}${
                formatPnlPct(view.unrealized_pnl_pct)
                  ? ` ${formatPnlPct(view.unrealized_pnl_pct)}`
                  : ""
              })`
            : "")
        : "";
    const hist =
      btActiveHistoryId != null ? ` · history #${btActiveHistoryId}` : "";
    const kind =
      result.run_kind === "portfolio"
        ? " · portfolio"
        : multi
          ? " · per symbol"
          : "";
    const sizing =
      result.mode === "pair" || result.run_kind === "pair"
        ? "full equity"
        : formatQty(result.qty);
    const symCount = result.symbols?.length || result.results?.length || 0;
    const allLabel =
      typeof window.t === "function"
        ? window.t("all_symbols", "All symbols")
        : "All symbols";
    const symLabel =
      view === result || view.symbol === result.symbol
        ? (multi && symCount > 1
            ? `${allLabel} (${symCount})`
            : result.symbol)
        : `${view.symbol} (of ${result.symbol})`;
    meta.textContent =
      `${symLabel}${kind} · ${label} · ${result.bar_timeframe} · ` +
      `${view.start || result.start ? formatDisplayDate(view.start || result.start) : "?"} → ${view.end || result.end ? formatDisplayDate(view.end || result.end) : "?"} · ` +
      `${view.evaluated_bars ?? result.evaluated_bars ?? "—"} bars · qty ${sizing}${stop}${open}${hist}`;
  }

  const aggCurve = getAggregateEquityCurve(result);
  const aggTrades = getAggregateTradeList(result);
  const chartCurve =
    view.equity_curve && view.equity_curve.length
      ? view.equity_curve
      : aggCurve;
  const chartTrades =
    view.trade_list && (view !== result || result.run_kind === "portfolio")
      ? view.trade_list
      : aggTrades;
  const chartCash =
    view.initial_cash != null ? view.initial_cash : result.initial_cash;

  if (chartCurve && chartCurve.length >= 2) {
    renderEquityChart(chartCurve, chartCash, chartTrades);
  } else if (result.run_kind === "per_symbol" && multi && view.equity_curve) {
    renderEquityChart(view.equity_curve, view.initial_cash, view.trade_list);
  } else {
    destroyBtEquityChart();
    btChartCache = null;
  }

  renderBtTradeList(chartTrades || view.trade_list || [], view, result);

  if (options.scroll !== false) {
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function isMultiBacktestResult(result) {
  if (!result || typeof result !== "object") return false;
  if (Array.isArray(result.results) && result.results.length > 0) return true;
  if (Array.isArray(result.symbols) && result.symbols.length > 1) return true;
  return result.run_kind === "portfolio";
}

let btMultiResultCache = null;

function syncBtDetailSymbolSelect(result, selected) {
  const pick = $("bt-detail-symbol");
  if (!pick) return;
  const opts = [];
  if (result.run_kind === "portfolio") {
    opts.push({ value: "__book__", label: "Portfolio book" });
  } else if (result.run_kind === "per_symbol" && (result.results || []).length > 1) {
    opts.push({ value: "__summary__", label: "Average (all symbols)" });
  }
  for (const row of result.results || []) {
    const sym = row.symbol || "?";
    opts.push({
      value: sym,
      label: row.error ? `${sym} (error)` : sym,
    });
  }
  pick.replaceChildren();
  for (const o of opts) {
    const el = document.createElement("option");
    el.value = o.value;
    el.textContent = o.label;
    pick.appendChild(el);
  }
  const ok = opts.some((o) => o.value === selected);
  pick.value = ok ? selected : opts[0]?.value || "";
  ensureNiceSelect(pick);
}

function resolveBtDetailView(result, selected) {
  if (!selected || selected === "__book__" || selected === "__summary__") return result;
  const leg = (result.results || []).find((r) => r.symbol === selected);
  if (!leg || leg.error) return result;
  // Portfolio legs lack equity curves — keep book chart when picking a leg
  // unless the leg has its own curve (per_symbol multi).
  if (leg.equity_curve && leg.equity_curve.length >= 2) return leg;
  return {
    ...result,
    symbol: leg.symbol,
    trade_list: leg.trade_list || [],
    trades: leg.trades,
    round_trips: leg.round_trips,
    wins: leg.wins,
    losses: leg.losses,
    win_rate: leg.win_rate,
    realized_pnl: leg.realized_pnl,
    open_qty: leg.open_qty,
    open_entry: leg.open_entry,
    open_mark: leg.open_mark,
    open_symbol: leg.symbol,
    open_group_id: leg.open_group_id,
    unrealized_pnl: leg.unrealized_pnl,
    unrealized_pnl_pct: leg.unrealized_pnl_pct,
    open_legs: leg.open_legs,
    total_return_pct: leg.total_return_pct ?? result.total_return_pct,
    buy_hold_return_pct: leg.buy_hold_return_pct ?? result.buy_hold_return_pct,
    max_drawdown_pct: leg.max_drawdown_pct ?? result.max_drawdown_pct,
    final_equity: leg.final_equity ?? result.final_equity,
  };
}

function renderBtSymbolTable(result, activeSymbol) {
  const wrap = $("bt-compare-symbols");
  if (!wrap) return;
  wrap.replaceChildren();
  const rows = Array.isArray(result.summary)
    ? result.summary
    : (result.results || []).map((r) => ({
        symbol: r.symbol,
        error: r.error,
        total_return_pct: r.total_return_pct,
        buy_hold_return_pct: r.buy_hold_return_pct,
        alpha_pct:
          r.total_return_pct != null && r.buy_hold_return_pct != null
            ? Number(r.total_return_pct) - Number(r.buy_hold_return_pct)
            : null,
        max_drawdown_pct: r.max_drawdown_pct,
        trades: r.round_trips ?? r.trades,
        round_trips: r.round_trips,
        win_rate: r.win_rate,
        realized_pnl: r.realized_pnl,
      }));
  if (!rows.length && result.run_kind !== "portfolio") return;

  const table = document.createElement("table");
  table.className = "bt-symbol-table";
  table.setAttribute("role", "grid");
  table.setAttribute("aria-label", "Backtest results by symbol");
  table.innerHTML =
    "<thead><tr>" +
    "<th>Symbol</th><th>Return</th><th>Buy&nbsp;&amp;&nbsp;hold</th><th>Alpha</th>" +
    "<th>Max&nbsp;DD</th><th>Round&nbsp;trips</th><th>Win&nbsp;rate</th><th>P&amp;L</th>" +
    "</tr></thead>";
  const body = document.createElement("tbody");
  const active =
    activeSymbol ||
    $("bt-detail-symbol")?.value ||
    "";

  const appendMetricRow = (row, { book = false, pickValue = null } = {}) => {
    const tr = document.createElement("tr");
    const key = pickValue ?? row.symbol;
    tr.classList.add("is-clickable");
    if (book) tr.classList.add("is-book");
    if (active && active === key) tr.classList.add("is-active");
    tr.tabIndex = 0;
    tr.dataset.sym = key;
    tr.setAttribute("role", "row");
    tr.setAttribute("aria-selected", active === key ? "true" : "false");
    if (row.error) {
      tr.innerHTML =
        `<td>${escapeHtml(book ? "Portfolio book" : row.symbol)}</td>` +
        `<td colspan="7" class="neg">${escapeHtml(String(row.error))}</td>`;
    } else {
      // Number(null) === 0 — treat missing book metrics as blank, not 0%.
      const ret =
        row.total_return_pct != null ? Number(row.total_return_pct) : NaN;
      const hold =
        row.buy_hold_return_pct != null
          ? Number(row.buy_hold_return_pct)
          : NaN;
      const alpha =
        row.alpha_pct != null
          ? Number(row.alpha_pct)
          : Number.isFinite(ret) && Number.isFinite(hold)
            ? ret - hold
            : NaN;
      const dd =
        row.max_drawdown_pct != null ? Number(row.max_drawdown_pct) : NaN;
      const wr = row.win_rate != null ? Number(row.win_rate) : NaN;
      const label = book ? "Portfolio book" : row.symbol;
      tr.innerHTML =
        `<td>${escapeHtml(label)}</td>` +
        `<td class="${Number.isFinite(ret) ? (ret >= 0 ? "pos" : "neg") : ""}">${Number.isFinite(ret) ? escapeHtml(pctToneText(ret)) : "—"}</td>` +
        `<td class="${Number.isFinite(hold) ? (hold >= 0 ? "pos" : "neg") : ""}">${Number.isFinite(hold) ? escapeHtml(pctToneText(hold)) : "—"}</td>` +
        `<td class="${Number.isFinite(alpha) ? (alpha >= 0 ? "pos" : "neg") : ""}">${Number.isFinite(alpha) ? escapeHtml(pctToneText(alpha, { signed: true })) : "—"}</td>` +
        `<td class="${Number.isFinite(dd) && dd > 0 ? "neg" : ""}">${Number.isFinite(dd) ? escapeHtml(pctToneText(dd)) : "—"}</td>` +
        `<td>${escapeHtml(String(row.round_trips ?? row.trades ?? "—"))}</td>` +
        `<td>${Number.isFinite(wr) ? escapeHtml(`${(wr * 100).toFixed(0)}%`) : "—"}</td>` +
        `<td class="${Number(row.realized_pnl) >= 0 ? "pos" : "neg"}">${row.realized_pnl != null ? escapeHtml(formatPnl(row.realized_pnl)) : "—"}</td>`;
    }
    body.appendChild(tr);
  };

  if (result.run_kind === "portfolio") {
    appendMetricRow(
      {
        symbol: "Portfolio book",
        total_return_pct: result.total_return_pct,
        buy_hold_return_pct: result.buy_hold_return_pct,
        alpha_pct:
          result.total_return_pct != null && result.buy_hold_return_pct != null
            ? Number(result.total_return_pct) - Number(result.buy_hold_return_pct)
            : null,
        max_drawdown_pct: result.max_drawdown_pct,
        trades: result.round_trips ?? result.trades,
        round_trips: result.round_trips,
        win_rate: result.win_rate,
        realized_pnl: result.realized_pnl,
      },
      { book: true, pickValue: "__book__" }
    );
  } else if (
    result.run_kind === "per_symbol" &&
    (result.results || []).length > 1
  ) {
    const allSymLabel =
      typeof window.t === "function"
        ? window.t("all_symbols", "All symbols")
        : "All symbols";
    appendMetricRow(
      {
        symbol: allSymLabel,
        total_return_pct: result.total_return_pct,
        buy_hold_return_pct: result.buy_hold_return_pct,
        alpha_pct:
          result.total_return_pct != null && result.buy_hold_return_pct != null
            ? Number(result.total_return_pct) -
              Number(result.buy_hold_return_pct)
            : null,
        max_drawdown_pct: result.max_drawdown_pct,
        trades: result.round_trips ?? result.trades,
        round_trips: result.round_trips,
        win_rate: result.win_rate,
        realized_pnl: result.realized_pnl,
      },
      { book: true, pickValue: "__summary__" }
    );
  }

  for (const row of rows) {
    appendMetricRow(row);
  }
  table.appendChild(body);
  wrap.appendChild(table);

  const focusSym = (sym) => {
    if (!sym) return;
    renderBacktestResult(btMultiResultCache || result, {
      detailSymbol: sym,
      scroll: false,
      quiet: true,
      skipCache: true,
      historyId: btActiveHistoryId,
    });
  };

  wrap.querySelectorAll("tr.is-clickable").forEach((tr) => {
    tr.addEventListener("click", () => focusSym(tr.dataset.sym));
    tr.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      ev.preventDefault();
      focusSym(tr.dataset.sym);
    });
  });
}

function fillBtSummaryMetrics(view, root, { multi } = {}) {
  const ret = $("bt-return");
  const bh = $("bt-buyhold");
  const alphaEl = $("bt-alpha");
  const dd = $("bt-drawdown");
  const eq = $("bt-equity");
  const realized = $("bt-realized");
  const wr = $("bt-winrate");
  const tn = $("bt-trades-n");
  const alphaLine = $("bt-alpha-line");

  const stratRet = Number(view.total_return_pct);
  const holdRet = Number(view.buy_hold_return_pct);
  const alpha = stratRet - holdRet;
  const hasRet = Number.isFinite(stratRet);
  const hasHold = Number.isFinite(holdRet);

  if (ret) {
    ret.textContent = hasRet ? `${stratRet.toFixed(2)}%` : "—";
    if (hasRet) setPnlTone(ret, stratRet);
    else ret.classList.remove("pos", "neg");
  }
  if (bh) {
    bh.textContent = hasHold ? `${holdRet.toFixed(2)}%` : "—";
    if (hasHold) setPnlTone(bh, holdRet);
    else bh.classList.remove("pos", "neg");
  }
  if (alphaEl) {
    if (hasRet && hasHold) {
      alphaEl.textContent = `${alpha >= 0 ? "+" : ""}${alpha.toFixed(2)}%`;
      setPnlTone(alphaEl, alpha);
    } else {
      alphaEl.textContent = "—";
      alphaEl.classList.remove("pos", "neg");
    }
  }
  if (alphaLine) {
    if (root.run_kind === "portfolio" && view === root) {
      alphaLine.textContent = hasRet && hasHold
        ? alpha >= 0
          ? `Portfolio beat equal-weight hold by ${alpha.toFixed(2)}%`
          : `Portfolio trailed equal-weight hold by ${Math.abs(alpha).toFixed(2)}%`
        : "Shared-cash portfolio book";
    } else if (hasRet && hasHold) {
      alphaLine.textContent =
        alpha >= 0
          ? `Beat buy & hold by ${alpha.toFixed(2)}%`
          : `Trailed buy & hold by ${Math.abs(alpha).toFixed(2)}%`;
    } else {
      alphaLine.textContent = "";
    }
    alphaLine.classList.toggle("pos", hasRet && hasHold && alpha > 0);
    alphaLine.classList.toggle("neg", hasRet && hasHold && alpha < 0);
  }
  if (dd) {
    const ddv = Number(view.max_drawdown_pct);
    dd.textContent = Number.isFinite(ddv) ? `${ddv.toFixed(2)}%` : "—";
    dd.classList.remove("pos");
    dd.classList.toggle("neg", Number.isFinite(ddv) && ddv > 0);
  }
  if (eq) {
    eq.textContent =
      view.final_equity != null ? money(view.final_equity) : "—";
  }
  if (realized) {
    if (view.realized_pnl != null) {
      const u =
        view.unrealized_pnl != null &&
        Number(view.open_qty) !== 0 &&
        Number.isFinite(Number(view.unrealized_pnl))
          ? ` · unreal ${formatPnl(view.unrealized_pnl)}`
          : "";
      realized.textContent = `${formatPnl(view.realized_pnl)}${u}`;
      setPnlTone(realized, view.realized_pnl);
    } else {
      realized.textContent = "—";
      realized.classList.remove("pos", "neg");
    }
  }
  if (wr) {
    wr.textContent =
      view.round_trips > 0
        ? `${(Number(view.win_rate) * 100).toFixed(0)}% (${view.wins}W / ${view.losses}L)`
        : "—";
  }
  if (tn) {
    const rounds = view.round_trips;
    tn.textContent = String(
      rounds != null ? rounds : view.trades ?? 0
    );
  }
}

function formatBtShares(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v === 0) {
    const label = formatQty(n);
    return label === "—" ? "—" : label;
  }
  const abs = Math.abs(v);
  const qtyLabel = formatQty(abs);
  const unit = abs === 1 ? "share" : "shares";
  if (v < 0) return `short ${qtyLabel} ${unit}`;
  return `${qtyLabel} ${unit}`;
}

function formatBtAuditLine(t) {
  const parts = [];
  if (t.initial_cash != null) {
    parts.push(`start ${money(t.initial_cash)}`);
  }
  if (t.equity_before != null || t.equity_after != null) {
    const before =
      t.equity_before != null ? money(t.equity_before) : "—";
    const after = t.equity_after != null ? money(t.equity_after) : "—";
    parts.push(`equity ${before} → ${after}`);
  }
  if (t.cash_before != null || t.cash_after != null) {
    const before = t.cash_before != null ? money(t.cash_before) : "—";
    const after = t.cash_after != null ? money(t.cash_after) : "—";
    parts.push(`cash ${before} → ${after}`);
  }
  if (t.slip_cost != null && Number(t.slip_cost) > 0) {
    const bps =
      t.slip_bps != null ? ` (${Number(t.slip_bps).toFixed(0)} bps)` : "";
    parts.push(`slip −${money(t.slip_cost)}${bps}`);
  }
  return parts.join(" · ");
}

function buildBtOpenRows(view, root) {
  const rows = [];
  const legs = Array.isArray(view?.open_legs)
    ? view.open_legs
    : Array.isArray(root?.open_legs) && view === root
      ? root.open_legs
      : null;
  if (legs && legs.length) {
    for (const leg of legs) {
      if (!(Number(leg.qty) !== 0 && Number.isFinite(Number(leg.qty)))) continue;
      rows.push({
        symbol: leg.symbol,
        qty: leg.qty,
        entry: leg.entry,
        mark: leg.mark,
        reason: leg.reason || "open position",
        unrealized_pnl: leg.unrealized_pnl,
        unrealized_pnl_pct: leg.unrealized_pnl_pct,
        group_id: leg.group_id,
      });
    }
    return rows;
  }
  const qty = Number(view?.open_qty);
  if (!(qty !== 0 && Number.isFinite(qty))) return rows;
  rows.push({
    symbol: view.open_symbol || view.symbol || root?.symbol,
    qty,
    entry: view.open_entry,
    mark: view.open_mark,
    reason: view.open_reason || "open position",
    unrealized_pnl: view.unrealized_pnl,
    unrealized_pnl_pct: view.unrealized_pnl_pct,
    group_id: view.open_group_id,
  });
  return rows;
}

function groupBtTradeLegs(trades) {
  const rows = Array.isArray(trades) ? trades : [];
  const used = new Set();
  const groups = [];

  const pushGroup = (legs, meta = {}) => {
    groups.push({
      legs,
      rotationId: meta.rotationId ?? legs[0]?.rotation_id ?? null,
      groupId: meta.groupId ?? legs[0]?.group_id ?? null,
      label: meta.label || null,
    });
  };

  // Same-bar rotations (sell + buy sharing rotation_id).
  const byRotation = new Map();
  rows.forEach((t, idx) => {
    const rid = t?.rotation_id;
    if (rid == null) return;
    if (!byRotation.has(rid)) byRotation.set(rid, []);
    byRotation.get(rid).push(idx);
  });
  for (const [rid, idxs] of byRotation) {
    if (idxs.length < 2) continue;
    const legSet = new Set(idxs);
    const sellIdx = idxs.find((i) => {
      const s = String(rows[i].side).toLowerCase();
      return s === "sell" || s === "cover";
    });
    // Pull the entry buy/short that this exit closes (same group_id), if still free.
    if (sellIdx != null) {
      const sellGid = rows[sellIdx]?.group_id;
      if (sellGid != null) {
        rows.forEach((t, idx) => {
          if (legSet.has(idx) || used.has(idx)) return;
          const s = String(t?.side || "").toLowerCase();
          if (
            t?.group_id === sellGid &&
            (s === "buy" || s === "short")
          ) {
            legSet.add(idx);
          }
        });
      }
    }
    const orderedIdxs = [...legSet].sort((a, b) => a - b);
    const legs = orderedIdxs.map((i) => rows[i]);
    orderedIdxs.forEach((i) => used.add(i));
    const sell = legs.find((l) => {
      const s = String(l.side).toLowerCase();
      return s === "sell" || s === "cover";
    });
    const buy = legs.find(
      (l) =>
        (String(l.side).toLowerCase() === "buy" ||
          String(l.side).toLowerCase() === "short") &&
        l.rotation_id === rid
    );
    const label =
      sell?.symbol && buy?.symbol
        ? `Rotated from ${sell.symbol} to ${buy.symbol}`
        : sell?.reason || buy?.reason || `Rotation #${rid}`;
    pushGroup(legs, { rotationId: rid, label });
  }

  // Round-trip groups by group_id (buy + later sell).
  const byGroup = new Map();
  rows.forEach((t, idx) => {
    if (used.has(idx)) return;
    const gid = t?.group_id;
    if (gid == null) return;
    if (!byGroup.has(gid)) byGroup.set(gid, []);
    byGroup.get(gid).push(idx);
  });
  for (const [gid, idxs] of byGroup) {
    const legs = idxs.map((i) => rows[i]);
    idxs.forEach((i) => used.add(i));
    const buy = legs.find((l) => {
      const s = String(l.side).toLowerCase();
      return s === "buy" || s === "short";
    });
    const sell = legs.find((l) => {
      const s = String(l.side).toLowerCase();
      return s === "sell" || s === "cover";
    });
    const sym = buy?.symbol || sell?.symbol || "";
    const label =
      buy && sell
        ? `Round trip${sym ? ` · ${sym}` : ""}`
        : buy
          ? `Open entry${sym ? ` · ${sym}` : ""}`
          : null;
    pushGroup(legs, { groupId: gid, label });
  }

  rows.forEach((t, idx) => {
    if (used.has(idx)) return;
    pushGroup([t]);
  });

  // Newest-first for the trade log.
  groups.sort((a, b) => {
    const ta = a.legs[a.legs.length - 1]?.time || a.legs[0]?.time || "";
    const tb = b.legs[b.legs.length - 1]?.time || b.legs[0]?.time || "";
    return String(tb).localeCompare(String(ta));
  });
  return groups;
}

function renderBtTradeLegRow(t) {
  const side = String(t.side || "").toLowerCase();
  const when = escapeHtml(formatDisplayDate(t.time, { withTime: true }));
  const px = money(t.price);
  const qtyLabel = escapeHtml(formatBtShares(t.qty));
  let pnlHtml = "";
  if (t.pnl != null) {
    const tone = Number(t.pnl) >= 0 ? "pos" : "neg";
    const pct = formatPnlPct(t.pnl_pct);
    const label = pct
      ? `${formatPnl(t.pnl)} (${pct})`
      : formatPnl(t.pnl);
    pnlHtml = `<span class="history-pnl ${tone}">${escapeHtml(label)}</span>`;
  }
  const sym = t.symbol
    ? `<span class="history-symbol">${escapeHtml(String(t.symbol))}</span>`
    : "";
  const audit = formatBtAuditLine(t);
  const row = document.createElement("div");
  row.className = "history-row";
  row.dataset.signal = side;
  row.setAttribute("role", "listitem");
  row.innerHTML =
    `<div class="history-row-top">` +
    `<span class="history-time">${when}</span>` +
    `<span class="history-signal ${escapeHtml(side)}">${escapeHtml(side.toUpperCase() || "—")}</span>` +
    (sym || `<span class="history-price">${escapeHtml(px)}</span>`) +
    (sym ? `<span class="history-price">${escapeHtml(px)}</span>` : "") +
    `</div>` +
    `<div class="history-row-meta">` +
    `qty ${qtyLabel}` +
    `${pnlHtml ? ` · ${pnlHtml}` : ""}` +
    (t.reason ? ` · ${escapeHtml(t.reason)}` : "") +
    `</div>` +
    (audit
      ? `<div class="history-row-audit">${escapeHtml(audit)}</div>`
      : "");
  return row;
}

function renderBtOpenPositionRow(open) {
  const when = "OPEN / MARK-TO-MARKET";
  const isShort = Number(open.qty) < 0;
  const sym = open.symbol
    ? `<span class="history-symbol">${escapeHtml(String(open.symbol))}</span>`
    : `<span class="history-symbol">OPEN</span>`;
  const entry = open.entry != null ? money(open.entry) : "—";
  const mark = open.mark != null ? money(open.mark) : "—";
  const qtyLabel = escapeHtml(formatBtShares(open.qty));
  let pnlHtml = "";
  if (open.unrealized_pnl != null && Number.isFinite(Number(open.unrealized_pnl))) {
    const tone = Number(open.unrealized_pnl) >= 0 ? "pos" : "neg";
    const pct = formatPnlPct(open.unrealized_pnl_pct);
    const label = pct
      ? `${formatPnl(open.unrealized_pnl)} (${pct})`
      : formatPnl(open.unrealized_pnl);
    pnlHtml = `<span class="history-pnl ${tone}">${escapeHtml(label)}</span>`;
  }
  const row = document.createElement("div");
  row.className = "history-row bt-open-row";
  row.dataset.signal = isShort ? "short" : "hold";
  row.setAttribute("role", "listitem");
  row.innerHTML =
    `<div class="history-row-top">` +
    `<span class="history-time">${when}</span>` +
    `<span class="history-signal ${isShort ? "short" : "hold"}">${isShort ? "SHORT" : "OPEN"}</span>` +
    sym +
    `<span class="history-price">${escapeHtml(mark)}</span>` +
    `</div>` +
    `<div class="history-row-meta">` +
    `entry ${escapeHtml(entry)} · mark ${escapeHtml(mark)} · qty ${qtyLabel}` +
    `${pnlHtml ? ` · unrealized ${pnlHtml}` : ""}` +
    (open.reason ? ` · ${escapeHtml(String(open.reason))}` : "") +
    `</div>`;
  return row;
}

function renderBtTradeList(trades, view = null, root = null) {
  const list = $("bt-trades");
  if (!list) return;
  list.replaceChildren();
  const rows = Array.isArray(trades) ? trades : [];
  const openRows = buildBtOpenRows(view || root, root || view);

  if (!rows.length && !openRows.length) {
    const empty = document.createElement("p");
    empty.className = "field-help";
    empty.textContent =
      "No trades in this window — try a longer lookback, milder dip preset, or shorter SMA windows.";
    list.appendChild(empty);
    return;
  }

  for (const open of openRows) {
    list.appendChild(renderBtOpenPositionRow(open));
  }

  const groups = groupBtTradeLegs(rows);
  for (const group of groups) {
    if (group.legs.length > 1 || group.label) {
      const wrap = document.createElement("div");
      wrap.className = "bt-trade-group";
      wrap.setAttribute("role", "group");
      if (group.label) {
        const head = document.createElement("div");
        head.className = "bt-trade-group-label";
        head.textContent = group.label;
        wrap.appendChild(head);
      }
      // Narrative order inside a group: entry → exit → (optional re-entry).
      const ordered = group.legs.slice().sort((a, b) => {
        const ta = String(a.time || "");
        const tb = String(b.time || "");
        if (ta !== tb) return ta.localeCompare(tb);
        // Same timestamp rotation: prior entry buy → sell → new buy.
        const rank = (leg) => {
          const s = String(leg.side || "").toLowerCase();
          if (s === "sell" || s === "cover") return 1;
          if (
            (s === "buy" || s === "short") &&
            leg.rotation_id != null
          ) {
            return 2;
          }
          return 0;
        };
        return rank(a) - rank(b);
      });
      for (const leg of ordered) {
        wrap.appendChild(renderBtTradeLegRow(leg));
      }
      list.appendChild(wrap);
    } else {
      list.appendChild(renderBtTradeLegRow(group.legs[0]));
    }
  }
}

function pctToneText(value, { signed = false, digits = 2 } = {}) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const sign = signed && n > 0 ? "+" : "";
  return `${sign}${n.toFixed(digits)}%`;
}

function formatBtRsiSummary(row) {
  const mode = String(row?.mode || "").toLowerCase();
  if (mode === "dip") {
    const buy = Number(row.dip_rsi_buy);
    const sell = Number(row.dip_rsi_sell);
    if (!Number.isFinite(buy) || !Number.isFinite(sell)) return "—";
    return `≤${buy} / ≥${sell}`;
  }
  if (mode === "pair") {
    const sma = Number(row.pair_sma_period);
    const lb = Number(row.pair_lookback);
    if (!Number.isFinite(sma) || !Number.isFinite(lb)) return "—";
    return `SMA${sma}/${lb}d`;
  }
  if (mode === "ls") {
    const fast = Number(row.ls_ema_fast);
    const slow = Number(row.ls_ema_slow);
    if (!Number.isFinite(fast) || !Number.isFinite(slow)) return "—";
    const rr = Number(row.ls_rr);
    return Number.isFinite(rr) ? `EMA${fast}/${slow} ${rr}R` : `EMA${fast}/${slow}`;
  }
  return "—";
}

function formatBtHistoryWhen(iso) {
  return formatAge(iso);
}

function syncBtCompareButton() {
  const btn = $("btn-bt-compare");
  if (!btn) return;
  const n = btSelectedHistoryIds.size;
  if (currentPage === "backtest-compare") {
    btn.disabled = n < 2 || n > BT_COMPARE_MAX;
    btn.textContent = "Refresh compare";
    return;
  }
  btn.disabled = n < 2 || n > BT_COMPARE_MAX;
  btn.textContent =
    n >= 2 ? `Compare selected (${n})` : "Compare selected";
}

function renderBacktestHistory(history) {
  btHistorySummaries = Array.isArray(history) ? history : [];
  const valid = new Set(btHistorySummaries.map((h) => Number(h.id)));
  for (const id of [...btSelectedHistoryIds]) {
    if (!valid.has(id)) btSelectedHistoryIds.delete(id);
  }
  if (btActiveHistoryId != null && !valid.has(btActiveHistoryId)) {
    btActiveHistoryId = null;
  }
  syncBtHistorySelect(btActiveHistoryId);

  const list = $("bt-history-list");
  if (!list) return;
  list.replaceChildren();
  if (!btHistorySummaries.length) {
    const empty = document.createElement("p");
    empty.className = "bt-history-empty";
    empty.textContent =
      "No saved runs yet — run a backtest to start building history.";
    list.appendChild(empty);
    syncBtCompareButton();
    return;
  }

  for (const row of btHistorySummaries) {
    const id = Number(row.id);
    const selected = btSelectedHistoryIds.has(id);
    const active = btActiveHistoryId === id;
    const alpha = Number(row.alpha_pct);
    const ret = Number(row.total_return_pct);
    const dd = Number(row.max_drawdown_pct);
    const item = document.createElement("div");
    item.className =
      "bt-history-row" +
      (selected ? " is-selected" : "") +
      (active ? " is-active" : "");
    item.setAttribute("role", "listitem");
    item.dataset.historyId = String(id);
    item.tabIndex = 0;

    const check = document.createElement("label");
    check.className = "bt-history-check";
    check.title = "Select for compare";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = selected;
    input.setAttribute("aria-label", `Select run ${id} for compare`);
    input.addEventListener("click", (ev) => ev.stopPropagation());
    input.addEventListener("change", (ev) => {
      ev.stopPropagation();
      if (input.checked) {
        if (btSelectedHistoryIds.size >= BT_COMPARE_MAX) {
          input.checked = false;
          showToast(`Compare up to ${BT_COMPARE_MAX} runs.`, "error");
          return;
        }
        btSelectedHistoryIds.add(id);
      } else {
        btSelectedHistoryIds.delete(id);
      }
      saveBacktestViewState();
      renderBacktestHistory(btHistorySummaries);
    });
    check.appendChild(input);

    const main = document.createElement("div");
    main.className = "bt-history-main";
    const title = document.createElement("div");
    title.className = "bt-history-title";
    const rsiLine = formatBtRsiSummary(row);
    const kind =
      row.run_kind === "portfolio"
        ? "portfolio"
        : Number(row.symbol_count) > 1
          ? "multi"
          : "";
    title.innerHTML =
      `<span class="bt-sym">${escapeHtml(String(row.symbol || "—"))}</span>` +
      (kind
        ? `<span class="bt-rsi" title="Run mode">${escapeHtml(kind)}</span>`
        : "") +
      `<span class="bt-label">${escapeHtml(String(row.label || row.mode || "—"))}</span>` +
      (row.day_preset_label
        ? `<span class="bt-rsi" title="Preset">${escapeHtml(String(row.day_preset_label))}</span>`
        : "") +
      (rsiLine !== "—"
        ? `<span class="bt-rsi" title="Strategy parameters">${escapeHtml(
            String(row.mode || "").toLowerCase() === "dip"
              ? `RSI ${rsiLine}`
              : rsiLine
          )}</span>`
        : "");
    const meta = document.createElement("div");
    meta.className = "bt-history-meta";
    const window =
      row.start || row.end
        ? `${formatDisplayDate(row.start || "")} → ${formatDisplayDate(row.end || "")}`
        : `${row.days || "—"}d`;
    meta.textContent =
      `${formatBtHistoryWhen(row.created_at)} · ${row.bar_timeframe || "—"} · ${window}` +
      ` · ${row.trades ?? 0} trades`;
    main.append(title, meta);

    const metrics = document.createElement("div");
    metrics.className = "bt-history-metrics";
    metrics.innerHTML =
      `<div><span>Return</span><strong class="${ret >= 0 ? "pos" : "neg"}">${escapeHtml(pctToneText(ret))}</strong></div>` +
      `<div><span>vs hold</span><strong class="${alpha >= 0 ? "pos" : "neg"}">${escapeHtml(pctToneText(alpha, { signed: true }))}</strong></div>` +
      `<div><span>Max DD</span><strong class="${dd > 0 ? "neg" : ""}">${escapeHtml(pctToneText(dd))}</strong></div>` +
      `<div><span>Equity</span><strong>${escapeHtml(money(row.final_equity))}</strong></div>`;

    const actions = document.createElement("div");
    actions.className = "bt-history-row-actions";
    const viewBtn = document.createElement("button");
    viewBtn.type = "button";
    viewBtn.className = "ghost";
    viewBtn.textContent = "View";
    viewBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      loadBacktestHistoryEntry(id);
    });
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "ghost";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      try {
        const data = await api(`/api/backtest/history/${id}`, { method: "DELETE" });
        if (btActiveHistoryId === id) {
          clearBacktestResultsPanel();
        }
        btSelectedHistoryIds.delete(id);
        renderBacktestHistory(data.history || []);
        showToast("Removed from history.", "ok");
      } catch (err) {
        showToast(err.message || "Delete failed", "error");
      }
    });
    actions.append(viewBtn, delBtn);

    item.append(check, main, metrics, actions);
    item.addEventListener("click", () => loadBacktestHistoryEntry(id));
    item.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        loadBacktestHistoryEntry(id);
      }
    });
    list.appendChild(item);
  }
  syncBtCompareButton();
}

async function refreshBacktestHistory({ restoreResult = false } = {}) {
  try {
    const data = await api("/api/backtest/history");
    renderBacktestHistory(data.history || []);
    if (restoreResult) {
      await restoreBacktestResultFromHistory();
    }
  } catch (err) {
    console.warn("Backtest history load failed", err);
  }
}

async function restoreBacktestResultFromHistory() {
  const view = readBacktestViewState();
  const cached = readBacktestLastResult();
  let id =
    view?.activeHistoryId != null
      ? Number(view.activeHistoryId)
      : cached?.historyId != null
        ? Number(cached.historyId)
        : NaN;
  if (!Number.isFinite(id)) {
    id = Number(btHistorySummaries[0]?.id);
  }
  if (!Number.isFinite(id)) {
    // Keep any locally cached result; only clear when nothing to show.
    if (!cached?.result) clearBacktestResultsPanel();
    return;
  }
  if (!btHistorySummaries.some((h) => Number(h.id) === id)) {
    id = Number(btHistorySummaries[0]?.id);
  }
  if (!Number.isFinite(id)) {
    if (!cached?.result) clearBacktestResultsPanel();
    return;
  }
  try {
    const data = await api(`/api/backtest/history/${id}`);
    const result = data.entry?.result;
    if (!result) {
      if (!cached?.result) return;
      return;
    }
    btActiveHistoryId = id;
    renderBacktestResult(result, {
      historyId: id,
      scroll: false,
      quiet: true,
    });
    renderBacktestHistory(btHistorySummaries);
    saveBacktestViewState();
  } catch (err) {
    console.warn("Backtest result restore failed", err);
    // Fall back to local cache if server history is unavailable.
    if (!$("bt-results") || $("bt-results").hidden) {
      restoreBacktestLastResult();
    }
  }
}

async function loadBacktestHistoryEntry(entryId, { scroll = true, quiet = false } = {}) {
  try {
    const data = await api(`/api/backtest/history/${entryId}`);
    const result = data.entry?.result;
    if (!result) throw new Error("Run has no result payload");
    btActiveHistoryId = Number(entryId);
    saveBacktestLastResult(result, btActiveHistoryId);
    saveBacktestViewState();
    // History subpage — open the result on the Run page.
    if (!$("bt-results")) {
      location.href = pagePath("backtest");
      return;
    }
    renderBacktestResult(result, {
      historyId: btActiveHistoryId,
      scroll,
      quiet,
    });
    renderBacktestHistory(btHistorySummaries);
    if (!quiet) showToast("Loaded saved backtest.", "ok");
  } catch (err) {
    showToast(err.message || "Could not load run", "error");
  }
}

function destroyBtCompareChart() {
  if (btCompareChart) {
    btCompareChart.destroy();
    btCompareChart = null;
  }
}

function closeBtCompare() {
  destroyBtCompareChart();
  if (currentPage === "backtest-compare") {
    location.href = pagePath("backtest-history");
    return;
  }
  const panel = $("bt-compare");
  if (panel) panel.hidden = true;
}

function syncBtCompareEmpty(showEmpty) {
  const empty = $("bt-compare-empty");
  const panel = $("bt-compare");
  if (empty) empty.hidden = !showEmpty;
  if (panel) panel.hidden = !!showEmpty;
}

async function runBacktestCompare({ navigate = true } = {}) {
  const ids = [...btSelectedHistoryIds];
  if (ids.length < 2) {
    if (currentPage === "backtest-compare") {
      syncBtCompareEmpty(true);
      showToast("Select at least 2 runs on History to compare.", "error");
      return;
    }
    showToast("Select at least 2 runs to compare.", "error");
    return;
  }
  if (ids.length > BT_COMPARE_MAX) {
    showToast(`Compare up to ${BT_COMPARE_MAX} runs.`, "error");
    return;
  }
  saveBacktestViewState();
  // From History (or elsewhere without compare panel), open the Compare subpage.
  if (navigate && (!$("bt-compare-chart") || currentPage === "backtest-history")) {
    location.href = pagePath("backtest-compare");
    return;
  }
  const btn = $("btn-bt-compare");
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent =
        currentPage === "backtest-compare" ? "Refreshing…" : "Comparing…";
    }
    const data = await api("/api/backtest/compare", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    const runs = Array.isArray(data.runs) ? data.runs : [];
    syncBtCompareEmpty(false);
    const panel = $("bt-compare");
    if (panel) panel.hidden = false;
    renderCompareTable(runs);
    renderCompareEquityChart(runs);
    if (currentPage !== "backtest-compare") {
      panel?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    showToast(`Comparing ${runs.length} runs.`, "ok");
  } catch (err) {
    showToast(err.message || "Compare failed", "error");
  } finally {
    syncBtCompareButton();
    if (btn && currentPage === "backtest-compare") {
      btn.textContent = "Refresh compare";
    }
  }
}

function renderCompareTable(runs) {
  const wrap = $("bt-compare-table-wrap");
  if (!wrap) return;
  wrap.replaceChildren();
  const table = document.createElement("table");
  table.className = "bt-compare-table";
  const metrics = [
    { key: "label", label: "Strategy", format: (s) => s.label || s.mode || "—" },
    { key: "symbol", label: "Symbol", format: (s) => s.symbol || "—" },
    {
      key: "window",
      label: "Window",
      format: (s) =>
        `${s.bar_timeframe || "—"} · ${s.days ?? "—"}d`,
    },
    {
      key: "rsi",
      label: "RSI buy / sell",
      format: (s) => formatBtRsiSummary(s),
    },
    {
      key: "return",
      label: "Strategy return",
      format: (s) => pctToneText(s.total_return_pct),
      tone: (s) => Number(s.total_return_pct),
    },
    {
      key: "hold",
      label: "Buy & hold",
      format: (s) => pctToneText(s.buy_hold_return_pct),
      tone: (s) => Number(s.buy_hold_return_pct),
    },
    {
      key: "alpha",
      label: "vs buy & hold",
      format: (s) => pctToneText(s.alpha_pct, { signed: true }),
      tone: (s) => Number(s.alpha_pct),
    },
    {
      key: "dd",
      label: "Max drawdown",
      format: (s) => pctToneText(s.max_drawdown_pct),
      tone: (s) => (Number(s.max_drawdown_pct) > 0 ? -1 : 0),
    },
    {
      key: "equity",
      label: "Final equity",
      format: (s) => money(s.final_equity),
    },
    {
      key: "pnl",
      label: "Realized P&L",
      format: (s) => formatPnl(s.realized_pnl),
      tone: (s) => Number(s.realized_pnl),
    },
    {
      key: "wr",
      label: "Win rate",
      format: (s) =>
        s.round_trips > 0
          ? `${(Number(s.win_rate) * 100).toFixed(0)}% (${s.wins}W / ${s.losses}L)`
          : "—",
    },
    {
      key: "trades",
      label: "Trades",
      format: (s) => String(s.trades ?? 0),
    },
  ];

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headRow.appendChild(document.createElement("th")).textContent = "Metric";
  runs.forEach((run, i) => {
    const th = document.createElement("th");
    const color = BT_COMPARE_COLORS[i % BT_COMPARE_COLORS.length];
    th.innerHTML =
      `<span class="bt-compare-swatch" style="background:${color}"></span>` +
      escapeHtml(`#${run.id} · ${run.summary?.symbol || "—"}`);
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const metric of metrics) {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.scope = "row";
    th.textContent = metric.label;
    tr.appendChild(th);
    for (const run of runs) {
      const s = run.summary || {};
      const td = document.createElement("td");
      td.textContent = metric.format(s);
      if (metric.tone) {
        const tone = metric.tone(s);
        if (Number.isFinite(tone) && tone > 0) td.classList.add("pos");
        if (Number.isFinite(tone) && tone < 0) td.classList.add("neg");
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
}

function renderCompareEquityChart(runs) {
  const canvas = $("bt-compare-chart");
  if (!canvas || typeof Chart === "undefined") return;
  destroyBtCompareChart();

  const series = runs
    .map((run, i) => {
      const curve = Array.isArray(run.result?.equity_curve)
        ? run.result.equity_curve
        : [];
      if (curve.length < 2) return null;
      const cash = Number(run.result?.initial_cash) || Number(curve[0]?.equity) || 1;
      const firstEq = Number(curve[0]?.equity) || cash;
      return {
        id: run.id,
        label:
          `${run.summary?.symbol || "?"} · ${run.summary?.label || run.summary?.mode || "run"}`,
        color: BT_COMPARE_COLORS[i % BT_COMPARE_COLORS.length],
        points: curve.map((p) => {
          const eq = Number(p.equity);
          const ret = firstEq ? ((eq - firstEq) / firstEq) * 100 : 0;
          return { t: parseBtTime(p.t), iso: String(p.t || ""), ret };
        }).filter((p) => Number.isFinite(p.t)),
      };
    })
    .filter(Boolean);

  const caption = $("bt-compare-caption");
  if (!series.length) {
    if (caption) {
      caption.textContent = "Not enough equity points to overlay.";
    }
    return;
  }

  // Normalize each curve to % return from its own start so different cash/windows compare.
  const datasets = series.map((s) => ({
    label: s.label,
    data: s.points.map((p) => ({ x: p.t, y: +p.ret.toFixed(3), iso: p.iso })),
    borderColor: s.color,
    backgroundColor: "transparent",
    borderWidth: 2,
    pointRadius: 0,
    pointHoverRadius: 3,
    tension: 0.15,
    parsing: false,
  }));

  const muted = cssVar("--muted", "#9aa8b8");
  const text = cssVar("--text", "#f2ebe1");
  const line = cssVar("--line", "#2a384c");
  const mono = cssVar("--mono", "IBM Plex Mono, monospace");

  btCompareChart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false, axis: "x" },
      plugins: {
        legend: {
          display: true,
          labels: {
            color: muted,
            font: { family: mono, size: 11 },
            boxWidth: 12,
            boxHeight: 2,
          },
        },
        tooltip: {
          backgroundColor: "rgba(12, 18, 25, 0.94)",
          titleColor: text,
          bodyColor: muted,
          borderColor: line,
          borderWidth: 1,
          padding: 10,
          titleFont: { family: mono, size: 12, weight: "500" },
          bodyFont: { family: mono, size: 11 },
          callbacks: {
            title(items) {
              const iso = items[0]?.raw?.iso;
              return iso ? formatBtTooltipDate(iso) : "";
            },
            label(ctx) {
              const y = Number(ctx.parsed.y);
              const sign = y > 0 ? "+" : "";
              return `${ctx.dataset.label}: ${sign}${y.toFixed(2)}%`;
            },
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          ticks: {
            color: muted,
            font: { family: mono, size: 10 },
            maxTicksLimit: 7,
            callback(value) {
              return formatBtAxisDate(new Date(value).toISOString());
            },
          },
          grid: { color: "rgba(42, 56, 76, 0.45)", drawTicks: false },
          border: { color: line },
        },
        y: {
          ticks: {
            color: muted,
            font: { family: mono, size: 10 },
            callback: (v) => `${Number(v).toFixed(0)}%`,
          },
          grid: { color: "rgba(42, 56, 76, 0.35)" },
          border: { color: line },
          title: {
            display: true,
            text: "Return from start (%)",
            color: muted,
            font: { family: mono, size: 10 },
          },
        },
      },
    },
  });

  if (caption) {
    caption.textContent =
      "Normalized % return from each run’s start — useful across different cash or windows.";
  }
}
