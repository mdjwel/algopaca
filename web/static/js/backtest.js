/**
 * Backtest Page JavaScript for AlgoPaca
 * Backtest runner, preset management, Chart.js equity curve & drawdown analysis, multi-symbol performance table, trade logs.
 */

/** Strategy preset tables — refreshed from /api/status on every poll. */
let smaPresets = [];
let dipPresets = [];
let pairPresets = [];

function findSmaPreset(id) {
  return smaPresets.find((p) => p.id === id) || null;
}

function findDipPreset(id) {
  return dipPresets.find((p) => p.id === id) || null;
}

function findPairPreset(id) {
  return pairPresets.find((p) => p.id === id) || null;
}

function parseSymbolList(raw) {
  return [
    ...new Set(
      String(raw || "")
        .replace(/;/g, ",")
        .split(/[,\n]+/)
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean)
    ),
  ];
}

function parsePairLegsFromText(raw) {
  const uniq = parseSymbolList(raw);
  if (uniq.length < 2 || uniq[0] === uniq[1]) return null;
  return { long: uniq[0], short: uniq[1] };
}

function setBtError(message) {
  const el = $("bt-error");
  if (!el) return;
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = message;
}

function applyBtSmaPreset(presetId) {
  const form = $("backtest-form");
  const preset = findSmaPreset(presetId);
  if (!form || !preset || preset.id === "custom") return;
  if (form.elements.fast_sma) form.elements.fast_sma.value = preset.fast_sma;
  if (form.elements.slow_sma) form.elements.slow_sma.value = preset.slow_sma;
}

function applyBtDipPreset(presetId) {
  const form = $("backtest-form");
  const preset = findDipPreset(presetId);
  if (!form || !preset || preset.id === "custom") return;
  if (form.elements.dip_rsi_buy) form.elements.dip_rsi_buy.value = preset.rsi_buy;
  if (form.elements.dip_rsi_sell) form.elements.dip_rsi_sell.value = preset.rsi_sell;
  if (form.elements.dip_skip_bearish) {
    form.elements.dip_skip_bearish.checked = !!preset.skip_bearish;
  }
}

function applyBtPairPreset(presetId) {
  const form = $("backtest-form");
  const preset = findPairPreset(presetId);
  if (!form || !preset || preset.id === "custom") return;
  if (form.elements.pair_sma_period) {
    form.elements.pair_sma_period.value = preset.sma_period;
  }
  if (form.elements.pair_lookback) {
    form.elements.pair_lookback.value = preset.lookback;
  }
  if (form.elements.pair_impulse_pct) {
    form.elements.pair_impulse_pct.value = preset.impulse_pct;
  }
  if (form.elements.pair_weak_side) {
    form.elements.pair_weak_side.value = preset.weak_side || "LONG";
  }
}

const DAY_PRESET_DEFAULTS = {
  ai_vwap_momentum: { sub_mode: "vwap_trend", side: "long_only", tp_r: 1.2, stop_atr: 1.0, fast: 9, slow: 21, max_trades: 3, summary: "Intraday VWAP trend & 9/21 EMA momentum with 1.2R target and 1.0 ATR stop." },
  vwap_trend: { sub_mode: "vwap_trend", side: "long_only", tp_r: 1.2, stop_atr: 1.0, fast: 9, slow: 21, max_trades: 3, summary: "Trend following above intraday VWAP with 9/21 EMA momentum." },
  ai_orb_breakout: { sub_mode: "orb", side: "long_only", tp_r: 1.5, stop_atr: 1.2, fast: 9, slow: 21, max_trades: 3, summary: "15-minute Opening Range Breakout with 1.5R target and 1.2 ATR stop." },
  orb_breakout: { sub_mode: "orb", side: "long_only", tp_r: 1.5, stop_atr: 1.2, fast: 9, slow: 21, max_trades: 3, summary: "15-minute Opening Range Breakout with ATR stop." },
  ai_adaptive_scalp: { sub_mode: "momentum_scalp", side: "long_only", tp_r: 1.2, stop_atr: 1.0, fast: 9, slow: 21, max_trades: 4, summary: "Fast 9/21 EMA momentum scalper with ADX regime filter." },
  momentum_scalp: { sub_mode: "momentum_scalp", side: "long_only", tp_r: 1.2, stop_atr: 1.0, fast: 9, slow: 21, max_trades: 4, summary: "Fast 9/21 EMA crossovers confirmed by RSI and ADX." },
  vwap_fade: { sub_mode: "vwap_fade", side: "long_only", tp_r: 1.2, stop_atr: 1.0, fast: 9, slow: 21, max_trades: 3, summary: "Mean reversion bounces at lower VWAP band in range-bound sessions." },
};

function applyBtDayPreset(presetId) {
  const form = $("backtest-form");
  const p = DAY_PRESET_DEFAULTS[presetId];
  if (!form || !p) return;
  if (form.elements.day_sub_mode) form.elements.day_sub_mode.value = p.sub_mode;
  if (form.elements.day_side) form.elements.day_side.value = p.side;
  if (form.elements.day_profit_target_r) form.elements.day_profit_target_r.value = p.tp_r;
  if (form.elements.day_stop_atr_mult) form.elements.day_stop_atr_mult.value = p.stop_atr;
  if (form.elements.day_ema_fast) form.elements.day_ema_fast.value = p.fast;
  if (form.elements.day_ema_slow) form.elements.day_ema_slow.value = p.slow;
  if (form.elements.day_max_trades_per_day) form.elements.day_max_trades_per_day.value = p.max_trades;
}

function syncBtRunKindChips(runKind) {
  const kind = runKind === "portfolio" ? "portfolio" : "per_symbol";
  const hidden = $("bt-run-kind");
  if (hidden) hidden.value = kind;
  document.querySelectorAll("[data-bt-run-kind]").forEach((btn) => {
    const on = btn.getAttribute("data-bt-run-kind") === kind;
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function syncBacktestUi() {
  const form = $("backtest-form");
  if (!form) return;
  const mode = form.elements.mode?.value || "dip";
  const sma = mode === "sma";
  const dip = mode === "dip";
  const pair = mode === "pair";
  const ls = mode === "ls";
  const day = mode === "day";

  form.querySelectorAll(".bt-sma-only").forEach((el) => {
    el.hidden = !sma;
  });
  form.querySelectorAll(".bt-dip-only").forEach((el) => {
    el.hidden = !dip;
  });
  form.querySelectorAll(".bt-pair-only").forEach((el) => {
    el.hidden = !pair;
  });
  form.querySelectorAll(".bt-ls-only").forEach((el) => {
    el.hidden = !ls;
  });
  form.querySelectorAll(".bt-day-only").forEach((el) => {
    el.hidden = !day;
  });
  form.querySelectorAll(".bt-shares-only").forEach((el) => {
    // Day Trading sizes from the desk risk engine, like pair and ls.
    el.hidden = !!pair || !!ls || !!day;
  });

  const smaPreset = form.elements.sma_preset?.value || "classic";
  const dipPreset = form.elements.dip_preset?.value || "deep";
  const pairPreset = form.elements.pair_preset?.value || "research_max";
  const dayPreset = form.elements.day_preset?.value || "ai_vwap_momentum";
  if (sma && smaPreset !== "custom") applyBtSmaPreset(smaPreset);
  if (dip && dipPreset !== "custom") applyBtDipPreset(dipPreset);
  if (pair && pairPreset !== "custom") applyBtPairPreset(pairPreset);
  if (day && dayPreset !== "custom") applyBtDayPreset(dayPreset);

  const modeHint = $("bt-mode-hint");
  if (modeHint) {
    modeHint.textContent = sma
      ? "Buy when fast SMA crosses above slow; sell on cross below. Fills at bar close."
      : pair
        ? "Full-capital long/short rotator — long in bull regime, short only on crash impulses."
        : ls
          ? (() => {
              const rr = Number(form.elements.ls_rr?.value || 2);
              const nice = Number.isFinite(rr) ? String(rr) : "2";
              return `Per-ticker long or short from EMA/ADX regime + MACD hist. ATR stops, ${nice}R targets, frictions.`;
            })()
        : day
          ? "Replays Day Trading rules on intraday bars — VWAP, opening range, ATR stops, R targets and the end-of-day square-off."
        : "Oversold washes via RSI / Bollinger — pick a dip preset below.";
  }
  const smaHint = $("bt-sma-hint");
  if (smaHint) {
    const preset = findSmaPreset(smaPreset);
    smaHint.textContent = preset?.summary || "Choose an SMA window pair.";
  }
  const dipHint = $("bt-dip-hint");
  if (dipHint) {
    const preset = findDipPreset(dipPreset);
    dipHint.textContent = preset?.summary || "Choose how deep a wash to buy.";
  }
  const pairHint = $("bt-pair-hint");
  if (pairHint) {
    const preset = findPairPreset(pairPreset);
    pairHint.textContent =
      preset?.summary || "Long leg by default; short leg only on confirmed bear impulses.";
  }
  const dayHint = $("bt-day-hint");
  if (dayHint) {
    const p = DAY_PRESET_DEFAULTS[dayPreset];
    dayHint.textContent =
      p?.summary || "Intraday VWAP & 9/21 EMA trend following with 1.2R target, 1.0 ATR stop, and EOD square-off.";
  }

  // Pair / LS force daily bars; symbols stay user-editable.
  if ((pair || ls) && form.elements.bar_timeframe) {
    form.elements.bar_timeframe.value = "1Day";
  }
  if (form.elements.symbols) form.elements.symbols.disabled = false;
  if (form.elements.bar_timeframe) form.elements.bar_timeframe.disabled = !!pair || !!ls;

  const stopOn = !!form.elements.stop_loss_enabled?.checked;
  const stopLabel = $("bt-stop-pct-label");
  if (stopLabel) stopLabel.hidden = !stopOn || pair || ls;

  const stopEnabled = form.elements.stop_loss_enabled;
  if (stopEnabled) {
    const wrap = stopEnabled.closest("label") || stopEnabled.closest(".bt-stop-toggle");
    if (wrap) wrap.hidden = !!pair || !!ls;
  }

  const tf = form.elements.bar_timeframe?.value || "1Day";
  const daysEl = form.elements.days;
  const marketHint = $("bt-market-hint");
  const kindHint = $("bt-run-kind-hint");
  const kindWrap = $("bt-run-kind-wrap");
  const symbolsRaw = String(
    form.elements.symbols?.value || form.elements.symbol?.value || ""
  );
  const symbolList = parseBtSymbolsInput(symbolsRaw);
  const multi = symbolList.length > 1;
  let runKind = String(form.elements.run_kind?.value || "per_symbol")
    .trim()
    .toLowerCase()
    .replace(/-/g, "_");
  if (runKind !== "portfolio") runKind = "per_symbol";
  if (form.elements.run_kind) form.elements.run_kind.value = runKind;
  syncBtRunKindChips(runKind);
  if (kindWrap) kindWrap.hidden = !multi || pair;
  const countEl = $("bt-symbols-count");
  if (countEl) {
    const n = symbolList.length;
    countEl.textContent = `${n} / 10`;
    countEl.classList.toggle("is-over", n > 10);
  }
  const symbolsHint = $("bt-symbols-hint");
  if (symbolsHint) {
    symbolsHint.textContent = pair
      ? "Enter exactly two symbols: long leg first, short leg second (any stocks/ETFs)."
      : ls
        ? "One or more tickers. Each can go long or short from regime signals (daily bars)."
      : multi
        ? "Same strategy settings apply to every ticker in the list."
        : "Add more tickers (comma, space, or newline) to compare or run a shared portfolio.";
  }
  if (kindHint) {
    kindHint.textContent =
      runKind === "portfolio"
        ? "One shared cash pool. Symbols compete for cash; buy & hold splits cash equally."
        : "Each symbol gets its own cash and equity curve for side-by-side comparison.";
  }
  const cashLabel = $("bt-cash-label");
  const cashHint = $("bt-cash-hint");
  const qtyLabel = $("bt-qty-label");
  const qtyHint = $("bt-qty-hint");
  const portfolioMode = multi && runKind === "portfolio" && !pair;
  if (cashLabel) {
    cashLabel.textContent = portfolioMode
      ? "Shared cash ($)"
      : multi && !pair
        ? "Cash per symbol ($)"
        : "Initial cash ($)";
  }
  if (cashHint) {
    cashHint.textContent = portfolioMode
      ? "One pool for all symbols — buys spend from the same cash."
      : multi && !pair
        ? "Each ticker is simulated with this starting cash independently."
        : pair
          ? "Starting capital for the full-equity pair rotator."
          : "Starting cash for this simulation.";
  }
  if (!pair) {
    if (qtyLabel) {
      qtyLabel.textContent = portfolioMode
        ? "Shares per symbol"
        : "Shares per trade";
    }
    if (qtyHint) {
      qtyHint.textContent = portfolioMode
        ? "Max long size per symbol when shared cash allows."
        : "Fixed share size on each buy signal.";
    }
  }
  const multiHint = $("bt-multi-hint");
  if (multiHint) {
    multiHint.textContent = portfolioMode
      ? "Book row is the shared book. Pick a symbol for its fills and stats."
      : "Select a row to focus the summary, chart, and trades.";
  }
  const intraday = tf !== "1Day";
  if (daysEl) {
    let selected = Number(daysEl.value);
    [...daysEl.options].forEach((opt) => {
      const days = Number(opt.value);
      const invalid = intraday && days > 60;
      opt.disabled = invalid;
      if (invalid && daysEl.value === opt.value) {
        selected = 60;
      }
    });
    if (intraday && selected > 60) selected = 60;
    if (![...daysEl.options].some((o) => Number(o.value) === selected && !o.disabled)) {
      selected = intraday ? 60 : 365;
    }
    daysEl.value = String(selected);
  }
  if (marketHint) {
    marketHint.textContent = intraday
      ? "Intraday lookbacks are capped at 60 days. Longer options are disabled."
      : "Daily bars support up to 2 years. Intraday is capped at 60 days.";
  }
  refreshNiceSelects(form);
  syncNiceSelectDisabled(form);
}

function applyDeskSettingsToBacktest(settings) {
  const form = $("backtest-form");
  if (!form || !settings) return false;
  const mode = String(settings.strategy_mode || "sma");
  form.elements.mode.value = mode === "ai" ? "sma" : mode;
  const deskSymbols = String(settings.symbols || settings.symbol || "AAPL")
    .trim()
    .toUpperCase();
  if (form.elements.symbols) {
    form.elements.symbols.value = deskSymbols || "AAPL";
  }
  if (settings.bar_timeframe) {
    const tf = settings.bar_timeframe;
    const allowed = new Set(["1Day", "1Hour", "15Min", "5Min"]);
    form.elements.bar_timeframe.value = allowed.has(tf) ? tf : "1Day";
  }
  if (settings.trade_qty != null && form.elements.qty && mode !== "pair" && mode !== "ls") {
    form.elements.qty.value = settings.trade_qty;
  }
  if (settings.sma_preset && form.elements.sma_preset) {
    form.elements.sma_preset.value = settings.sma_preset;
  }
  if (settings.fast_sma != null && form.elements.fast_sma) {
    form.elements.fast_sma.value = settings.fast_sma;
  }
  if (settings.slow_sma != null && form.elements.slow_sma) {
    form.elements.slow_sma.value = settings.slow_sma;
  }
  if (settings.dip_preset && form.elements.dip_preset) {
    form.elements.dip_preset.value = settings.dip_preset;
  }
  if (settings.dip_rsi_buy != null && form.elements.dip_rsi_buy) {
    form.elements.dip_rsi_buy.value = settings.dip_rsi_buy;
  }
  if (settings.dip_rsi_sell != null && form.elements.dip_rsi_sell) {
    form.elements.dip_rsi_sell.value = settings.dip_rsi_sell;
  }
  if (settings.dip_skip_bearish !== undefined && form.elements.dip_skip_bearish) {
    form.elements.dip_skip_bearish.checked = settings.dip_skip_bearish !== false;
  }
  if (settings.pair_preset && form.elements.pair_preset) {
    form.elements.pair_preset.value = settings.pair_preset;
  }
  if (settings.pair_sma_period != null && form.elements.pair_sma_period) {
    form.elements.pair_sma_period.value = settings.pair_sma_period;
  }
  if (settings.pair_lookback != null && form.elements.pair_lookback) {
    form.elements.pair_lookback.value = settings.pair_lookback;
  }
  if (settings.pair_impulse_pct != null && form.elements.pair_impulse_pct) {
    form.elements.pair_impulse_pct.value = settings.pair_impulse_pct;
  }
  if (settings.pair_weak_side && form.elements.pair_weak_side) {
    form.elements.pair_weak_side.value = settings.pair_weak_side;
  }
  if (settings.ls_ema_fast != null && form.elements.ls_ema_fast) {
    form.elements.ls_ema_fast.value = settings.ls_ema_fast;
  }
  if (settings.ls_ema_slow != null && form.elements.ls_ema_slow) {
    form.elements.ls_ema_slow.value = settings.ls_ema_slow;
  }
  if (settings.ls_adx_min != null && form.elements.ls_adx_min) {
    form.elements.ls_adx_min.value = settings.ls_adx_min;
  }
  if (settings.ls_atr_stop_mult != null && form.elements.ls_atr_stop_mult) {
    form.elements.ls_atr_stop_mult.value = settings.ls_atr_stop_mult;
  }
  if (settings.ls_risk_pct != null && form.elements.ls_risk_pct) {
    form.elements.ls_risk_pct.value = settings.ls_risk_pct;
  }
  if (settings.ls_rr != null && form.elements.ls_rr) {
    form.elements.ls_rr.value = settings.ls_rr;
  }
  if (settings.ls_time_stop_bars != null && form.elements.ls_time_stop_bars) {
    form.elements.ls_time_stop_bars.value = settings.ls_time_stop_bars;
  }
  const stopPct = Number(settings.stop_loss_pct ?? 0);
  if (form.elements.stop_loss_enabled) {
    form.elements.stop_loss_enabled.checked = stopPct > 0;
  }
  if (form.elements.stop_loss_pct && stopPct > 0) {
    form.elements.stop_loss_pct.value = stopPct;
  }
  syncBacktestUi();
  saveBacktestFormDraft();
  return mode === "ai";
}

function loadBacktestFromDesk() {
  const form = $("backtest-form");
  if (!form) return;
  const desk = $("settings");
  if (desk) {
    const mode = String(desk.elements.strategy_mode?.value || "sma");
    form.elements.mode.value = mode === "ai" ? "sma" : mode;
    const deskSymbols =
      String(desk.elements.symbols?.value || desk.elements.symbol?.value || "AAPL")
        .trim()
        .toUpperCase();
    if (form.elements.symbols) {
      form.elements.symbols.value = deskSymbols || "AAPL";
    }
    if (desk.elements.bar_timeframe) {
      const tf = desk.elements.bar_timeframe.value;
      const allowed = new Set(["1Day", "1Hour", "15Min", "5Min"]);
      form.elements.bar_timeframe.value = allowed.has(tf) ? tf : "1Day";
    }
    if (desk.elements.trade_qty && mode !== "pair" && mode !== "ls") {
      form.elements.qty.value = desk.elements.trade_qty.value;
    }
    if (desk.elements.sma_preset) form.elements.sma_preset.value = desk.elements.sma_preset.value;
    if (desk.elements.fast_sma) form.elements.fast_sma.value = desk.elements.fast_sma.value;
    if (desk.elements.slow_sma) form.elements.slow_sma.value = desk.elements.slow_sma.value;
    if (desk.elements.dip_preset) form.elements.dip_preset.value = desk.elements.dip_preset.value;
    if (desk.elements.dip_rsi_buy) form.elements.dip_rsi_buy.value = desk.elements.dip_rsi_buy.value;
    if (desk.elements.dip_rsi_sell) form.elements.dip_rsi_sell.value = desk.elements.dip_rsi_sell.value;
    if (desk.elements.dip_skip_bearish) {
      form.elements.dip_skip_bearish.checked = !!desk.elements.dip_skip_bearish.checked;
    }
    if (desk.elements.pair_preset) {
      form.elements.pair_preset.value = desk.elements.pair_preset.value;
    }
    if (desk.elements.pair_sma_period) {
      form.elements.pair_sma_period.value = desk.elements.pair_sma_period.value;
    }
    if (desk.elements.pair_lookback) {
      form.elements.pair_lookback.value = desk.elements.pair_lookback.value;
    }
    if (desk.elements.pair_impulse_pct) {
      form.elements.pair_impulse_pct.value = desk.elements.pair_impulse_pct.value;
    }
    if (desk.elements.pair_weak_side) {
      form.elements.pair_weak_side.value = desk.elements.pair_weak_side.value;
    }
    if (desk.elements.ls_ema_fast) {
      form.elements.ls_ema_fast.value = desk.elements.ls_ema_fast.value;
    }
    if (desk.elements.ls_ema_slow) {
      form.elements.ls_ema_slow.value = desk.elements.ls_ema_slow.value;
    }
    if (desk.elements.ls_adx_min) {
      form.elements.ls_adx_min.value = desk.elements.ls_adx_min.value;
    }
    if (desk.elements.ls_atr_stop_mult) {
      form.elements.ls_atr_stop_mult.value = desk.elements.ls_atr_stop_mult.value;
    }
    if (desk.elements.ls_risk_pct) {
      form.elements.ls_risk_pct.value = desk.elements.ls_risk_pct.value;
    }
    if (desk.elements.ls_rr) {
      form.elements.ls_rr.value = desk.elements.ls_rr.value;
    }
    if (desk.elements.ls_time_stop_bars) {
      form.elements.ls_time_stop_bars.value = desk.elements.ls_time_stop_bars.value;
    }
    if (desk.elements.stop_loss_enabled) {
      form.elements.stop_loss_enabled.checked = !!desk.elements.stop_loss_enabled.checked;
    }
    if (desk.elements.stop_loss_pct) {
      form.elements.stop_loss_pct.value = desk.elements.stop_loss_pct.value;
    }
    syncBacktestUi();
    saveBacktestFormDraft();
    showToast(
      mode === "ai"
        ? "Loaded desk watchlist & size (AI isn’t backtestable — using SMA)."
        : "Loaded desk strategy into Backtest.",
      "ok"
    );
    return;
  }
  if (lastDeskSettings) {
    const wasAi = applyDeskSettingsToBacktest(lastDeskSettings);
    showToast(
      wasAi
        ? "Loaded desk watchlist & size (AI isn’t backtestable — using SMA)."
        : "Loaded desk strategy into Backtest.",
      "ok"
    );
    return;
  }
  refreshStatus({ forceSettings: true })
    .then(() => {
      if (!lastDeskSettings) {
        showToast("Desk settings unavailable — open Auto Trade first.", "error");
        return;
      }
      const wasAi = applyDeskSettingsToBacktest(lastDeskSettings);
      showToast(
        wasAi
          ? "Loaded desk watchlist & size (AI isn’t backtestable — using SMA)."
          : "Loaded desk strategy into Backtest.",
        "ok"
      );
    })
    .catch((err) => showToast(err.message || "Could not load desk settings", "error"));
}

function parseBtSymbolsInput(raw) {
  const parts = String(raw || "")
    .toUpperCase()
    .replace(/;/g, ",")
    .replace(/[\n\r\t]+/g, ",")
    .split(",")
    .flatMap((p) => p.trim().split(/\s+/))
    .map((p) => p.trim())
    .filter(Boolean);
  return [...new Set(parts)];
}

function backtestPayload() {
  const form = $("backtest-form");
  const mode = String(form.elements.mode.value || "sma");
  let days = Number(form.elements.days.value || 365);
  const tf = String(form.elements.bar_timeframe.value || "1Day");
  if (tf !== "1Day" && days > 60) days = 60;
  const stopOn = !!form.elements.stop_loss_enabled?.checked;
  const symbolsRaw = String(
    form.elements.symbols?.value || form.elements.symbol?.value || "AAPL"
  ).trim();
  const symbols = parseBtSymbolsInput(symbolsRaw);
  const runKind = String(form.elements.run_kind?.value || "per_symbol")
    .trim()
    .toLowerCase()
    .replace(/-/g, "_");
  const payload = {
    mode,
    symbols: symbols.join(", ") || "AAPL",
    symbol: symbols[0] || "AAPL",
    run_kind: runKind === "portfolio" ? "portfolio" : "per_symbol",
    days,
    bar_timeframe: tf,
    qty: Number(form.elements.qty.value || 1),
    initial_cash: Number(form.elements.initial_cash.value || 10000),
    stop_loss_pct: stopOn ? Number(form.elements.stop_loss_pct.value || 0) : 0,
  };
  if (mode === "sma") {
    payload.sma_preset = form.elements.sma_preset.value || "classic";
    payload.fast_sma = Number(form.elements.fast_sma.value || 10);
    payload.slow_sma = Number(form.elements.slow_sma.value || 30);
  } else if (mode === "pair") {
    payload.pair_preset = form.elements.pair_preset?.value || "research_max";
    payload.pair_sma_period = Number(form.elements.pair_sma_period?.value || 50);
    payload.pair_lookback = Number(form.elements.pair_lookback?.value || 7);
    payload.pair_impulse_pct = Number(form.elements.pair_impulse_pct?.value || 5);
    payload.pair_weak_side = form.elements.pair_weak_side?.value || "LONG";
    const legs = parsePairLegsFromText(symbolsRaw);
    if (!legs) {
      throw new Error(
        "Long & Short Pair needs two different symbols (long first, short second)."
      );
    }
    payload.pair_long_symbol = legs.long;
    payload.pair_short_symbol = legs.short;
    payload.slip_bps = Number(form.elements.slip_bps?.value || 5);
    payload.symbols = `${legs.long}, ${legs.short}`;
    payload.symbol = legs.long;
    payload.bar_timeframe = "1Day";
  } else if (mode === "day") {
    if (payload.bar_timeframe === "1Day") payload.bar_timeframe = "15Min";
    if (payload.days > 60) payload.days = 60;
    payload.slip_bps = Number(form.elements.slip_bps?.value || 1);
    payload.day_preset = form.elements.day_preset?.value || "ai_vwap_momentum";
    payload.day_sub_mode = form.elements.day_sub_mode?.value || "vwap_trend";
    payload.day_side = form.elements.day_side?.value || "long_only";
    payload.day_profit_target_r = Number(form.elements.day_profit_target_r?.value || 1.2);
    payload.day_stop_atr_mult = Number(form.elements.day_stop_atr_mult?.value || 1.0);
    payload.day_ema_fast = Number(form.elements.day_ema_fast?.value || 9);
    payload.day_ema_slow = Number(form.elements.day_ema_slow?.value || 21);
    payload.day_max_trades_per_day = Number(form.elements.day_max_trades_per_day?.value || 3);
  } else if (mode === "ls") {
    payload.ls_ema_fast = Number(form.elements.ls_ema_fast?.value || 21);
    payload.ls_ema_slow = Number(form.elements.ls_ema_slow?.value || 55);
    payload.ls_adx_min = Number(form.elements.ls_adx_min?.value || 20);
    payload.ls_atr_stop_mult = Number(form.elements.ls_atr_stop_mult?.value || 1.5);
    payload.ls_risk_pct = Number(form.elements.ls_risk_pct?.value || 1);
    payload.ls_rr = Number(form.elements.ls_rr?.value || 2);
    payload.ls_time_stop_bars = Number(form.elements.ls_time_stop_bars?.value || 15);
    payload.ls_slippage_pct = Number(form.elements.ls_slippage_pct?.value || 0.02);
    payload.ls_commission_pct = Number(form.elements.ls_commission_pct?.value || 0.05);
    payload.bar_timeframe = "1Day";
    if (!(payload.ls_ema_fast < payload.ls_ema_slow)) {
      throw new Error("LS EMA fast must be smaller than EMA slow.");
    }
  } else {
    payload.dip_preset = form.elements.dip_preset.value || "deep";
    payload.dip_rsi_buy = Number(form.elements.dip_rsi_buy.value || 30);
    payload.dip_rsi_sell = Number(form.elements.dip_rsi_sell.value || 60);
    payload.dip_skip_bearish = !!form.elements.dip_skip_bearish?.checked;
  }
  return payload;
}

const BT_FORM_STORAGE_KEY = "alpaca-desk-backtest-form";

function readBacktestFormDraft() {
  try {
    const raw = localStorage.getItem(BT_FORM_STORAGE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    return data && typeof data === "object" ? data : null;
  } catch {
    return null;
  }
}

function saveBacktestFormDraft() {
  const form = $("backtest-form");
  if (!form) return;
  const stopOn = !!form.elements.stop_loss_enabled?.checked;
  const draft = {
    mode: form.elements.mode?.value || "dip",
    symbols: String(form.elements.symbols?.value || form.elements.symbol?.value || "")
      .trim()
      .toUpperCase(),
    run_kind: form.elements.run_kind?.value || "per_symbol",
    days: form.elements.days?.value || "365",
    bar_timeframe: form.elements.bar_timeframe?.value || "1Day",
    initial_cash: form.elements.initial_cash?.value || "10000",
    qty: form.elements.qty?.value || "1",
    stop_loss_enabled: stopOn,
    stop_loss_pct: form.elements.stop_loss_pct?.value || "2",
    sma_preset: form.elements.sma_preset?.value || "classic",
    fast_sma: form.elements.fast_sma?.value || "10",
    slow_sma: form.elements.slow_sma?.value || "30",
    dip_preset: form.elements.dip_preset?.value || "deep",
    dip_rsi_buy: form.elements.dip_rsi_buy?.value || "30",
    dip_rsi_sell: form.elements.dip_rsi_sell?.value || "60",
    dip_skip_bearish: !!form.elements.dip_skip_bearish?.checked,
    pair_preset: form.elements.pair_preset?.value || "research_max",
    pair_sma_period: form.elements.pair_sma_period?.value || "50",
    pair_lookback: form.elements.pair_lookback?.value || "7",
    pair_impulse_pct: form.elements.pair_impulse_pct?.value || "5",
    pair_weak_side: form.elements.pair_weak_side?.value || "LONG",
    slip_bps: form.elements.slip_bps?.value || "5",
    ls_ema_fast: form.elements.ls_ema_fast?.value || "21",
    ls_ema_slow: form.elements.ls_ema_slow?.value || "55",
    ls_adx_min: form.elements.ls_adx_min?.value || "20",
    ls_atr_stop_mult: form.elements.ls_atr_stop_mult?.value || "1.5",
    ls_risk_pct: form.elements.ls_risk_pct?.value || "1",
    ls_rr: form.elements.ls_rr?.value || "2",
    ls_time_stop_bars: form.elements.ls_time_stop_bars?.value || "15",
    ls_slippage_pct: form.elements.ls_slippage_pct?.value || "0.02",
    ls_commission_pct: form.elements.ls_commission_pct?.value || "0.05",
  };
  try {
    localStorage.setItem(BT_FORM_STORAGE_KEY, JSON.stringify(draft));
  } catch {
    /* ignore quota / private mode */
  }
}

function restoreBacktestFormDraft() {
  const form = $("backtest-form");
  const draft = readBacktestFormDraft();
  if (!form || !draft) return false;

  const setVal = (name, value) => {
    if (value == null || !form.elements[name]) return;
    form.elements[name].value = String(value);
  };
  const setCheck = (name, value) => {
    if (!form.elements[name]) return;
    form.elements[name].checked = !!value;
  };

  const mode = String(draft.mode || "").toLowerCase();
  if (["sma", "dip", "pair", "ls", "day"].includes(mode)) setVal("mode", mode);
  const symbols =
    draft.symbols ||
    draft.symbol ||
    "";
  if (symbols) setVal("symbols", String(symbols).trim().toUpperCase());
  if (draft.run_kind) {
    const rk = String(draft.run_kind).toLowerCase().replace(/-/g, "_");
    setVal("run_kind", rk === "portfolio" ? "portfolio" : "per_symbol");
  }
  setVal("days", draft.days);
  setVal("bar_timeframe", draft.bar_timeframe);
  setVal("initial_cash", draft.initial_cash);
  setVal("qty", draft.qty);
  setCheck("stop_loss_enabled", draft.stop_loss_enabled);
  setVal("stop_loss_pct", draft.stop_loss_pct);
  setVal("sma_preset", draft.sma_preset);
  setVal("fast_sma", draft.fast_sma);
  setVal("slow_sma", draft.slow_sma);
  setVal("dip_preset", draft.dip_preset);
  setVal("dip_rsi_buy", draft.dip_rsi_buy);
  setVal("dip_rsi_sell", draft.dip_rsi_sell);
  setCheck("dip_skip_bearish", draft.dip_skip_bearish);
  setVal("pair_preset", draft.pair_preset);
  setVal("pair_sma_period", draft.pair_sma_period);
  setVal("pair_lookback", draft.pair_lookback);
  setVal("pair_impulse_pct", draft.pair_impulse_pct);
  setVal("pair_weak_side", draft.pair_weak_side);
  setVal("slip_bps", draft.slip_bps);
  setVal("ls_ema_fast", draft.ls_ema_fast);
  setVal("ls_ema_slow", draft.ls_ema_slow);
  setVal("ls_adx_min", draft.ls_adx_min);
  setVal("ls_atr_stop_mult", draft.ls_atr_stop_mult);
  setVal("ls_risk_pct", draft.ls_risk_pct);
  setVal("ls_rr", draft.ls_rr);
  setVal("ls_time_stop_bars", draft.ls_time_stop_bars);
  setVal("ls_slippage_pct", draft.ls_slippage_pct);
  setVal("ls_commission_pct", draft.ls_commission_pct);
  return true;
}

function applyBtChartSeriesVisibility() {
  if (!btEquityChart) return;
  const map = {
    Strategy: "strategy",
    "Buy & hold": "hold",
    "Drawdown %": "drawdown",
    Buys: "trades",
    Sells: "trades",
  };
  btEquityChart.data.datasets.forEach((ds) => {
    const key = map[ds.label];
    if (key) ds.hidden = !btChartSeries[key];
  });
  if (btEquityChart.options.scales?.y1) {
    btEquityChart.options.scales.y1.display = btChartSeries.drawdown;
  }
  btEquityChart.update();
  syncBtChartToggles();
}

function isBtChartFullscreen() {
  return !!$("bt-chart-block")?.classList.contains("is-fullscreen");
}

function syncBtChartFullscreenUi(on) {
  const btn = $("btn-bt-chart-fs");
  const enter = btn?.querySelector(".bt-chart-fs-enter");
  const exit = btn?.querySelector(".bt-chart-fs-exit");
  if (btn) {
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.setAttribute("aria-label", on ? "Exit fullscreen" : "Fullscreen equity curve");
    btn.title = on ? "Exit fullscreen (Esc)" : "Fullscreen chart";
  }
  if (enter) enter.hidden = !!on;
  if (exit) exit.hidden = !on;
  document.body.classList.toggle("bt-chart-fs-open", !!on);
}

function setBtChartFullscreen(on) {
  const block = $("bt-chart-block");
  if (!block) return;
  const next = !!on;
  block.classList.toggle("is-fullscreen", next);
  syncBtChartFullscreenUi(next);
  requestAnimationFrame(() => {
    btEquityChart?.resize();
  });
}

function toggleBtChartFullscreen() {
  setBtChartFullscreen(!isBtChartFullscreen());
}

$("backtest-form")?.addEventListener("change", (ev) => {
  setBtError(null);
  const name = ev.target?.name;
  if (name === "sma_preset") applyBtSmaPreset(ev.target.value);
  if (name === "dip_preset") applyBtDipPreset(ev.target.value);
  if (name === "pair_preset") applyBtPairPreset(ev.target.value);
  if (name === "day_preset") applyBtDayPreset(ev.target.value);
  if (name === "mode" && ev.target.value === "pair") {
    const form = $("backtest-form");
    if (form?.elements.symbols) form.elements.symbols.value = "";
    if (form?.elements.bar_timeframe) form.elements.bar_timeframe.value = "1Day";
  }
  if (name === "mode" && ev.target.value === "ls") {
    const form = $("backtest-form");
    if (form?.elements.bar_timeframe) form.elements.bar_timeframe.value = "1Day";
  }
  if (name === "mode" && ev.target.value === "day") {
    const form = $("backtest-form");
    // Intraday engine: daily bars have no VWAP, opening range or closing bell.
    if (form?.elements.bar_timeframe) form.elements.bar_timeframe.value = "15Min";
    if (form?.elements.days && Number(form.elements.days.value) > 60) {
      form.elements.days.value = 60;
    }
  }
  if (name === "fast_sma" || name === "slow_sma") {
    const form = $("backtest-form");
    if (form?.elements.sma_preset && form.elements.sma_preset.value !== "custom") {
      form.elements.sma_preset.value = "custom";
    }
  }
  if (
    name === "dip_rsi_buy" ||
    name === "dip_rsi_sell" ||
    name === "dip_skip_bearish"
  ) {
    const form = $("backtest-form");
    if (form?.elements.dip_preset && form.elements.dip_preset.value !== "custom") {
      form.elements.dip_preset.value = "custom";
    }
  }
  if (
    name === "pair_sma_period" ||
    name === "pair_lookback" ||
    name === "pair_impulse_pct" ||
    name === "pair_weak_side"
  ) {
    const form = $("backtest-form");
    if (form?.elements.pair_preset && form.elements.pair_preset.value !== "custom") {
      form.elements.pair_preset.value = "custom";
    }
  }
  if (
    name === "day_sub_mode" ||
    name === "day_side" ||
    name === "day_profit_target_r" ||
    name === "day_stop_atr_mult" ||
    name === "day_ema_fast" ||
    name === "day_ema_slow" ||
    name === "day_max_trades_per_day"
  ) {
    const form = $("backtest-form");
    if (form?.elements.day_preset && form.elements.day_preset.value !== "custom") {
      form.elements.day_preset.value = "custom";
    }
  }
  syncBacktestUi();
  saveBacktestFormDraft();
});
$("backtest-form")?.addEventListener("input", (ev) => {
  const name = ev.target?.name;
  if (
    name === "pair_sma_period" ||
    name === "pair_lookback" ||
    name === "pair_impulse_pct"
  ) {
    const form = $("backtest-form");
    if (form?.elements.pair_preset && form.elements.pair_preset.value !== "custom") {
      form.elements.pair_preset.value = "custom";
    }
  }
  if (name === "fast_sma" || name === "slow_sma") {
    const form = $("backtest-form");
    if (form?.elements.sma_preset && form.elements.sma_preset.value !== "custom") {
      form.elements.sma_preset.value = "custom";
    }
  }
  if (name === "dip_rsi_buy" || name === "dip_rsi_sell") {
    const form = $("backtest-form");
    if (form?.elements.dip_preset && form.elements.dip_preset.value !== "custom") {
      form.elements.dip_preset.value = "custom";
    }
  }
  saveBacktestFormDraft();
});
$("backtest-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  setBtError(null);
  const btn = $("btn-backtest");
  let payload;
  try {
    payload = backtestPayload();
  } catch (err) {
    setBtError(err.message || String(err));
    return;
  }
  const symbolList = parseBtSymbolsInput(payload.symbols);
  if (!symbolList.length) {
    setBtError("Add at least one symbol.");
    return;
  }
  if (payload.mode === "pair" && symbolList.length !== 2) {
    setBtError("Long & Short Pair needs exactly two symbols (long, short).");
    return;
  }
  if (symbolList.length > 10) {
    setBtError("At most 10 symbols allowed.");
    return;
  }
  if (payload.mode === "sma" && !(payload.fast_sma < payload.slow_sma)) {
    setBtError("Fast SMA must be less than slow SMA.");
    return;
  }
  if (
    payload.mode === "dip" &&
    !(payload.dip_rsi_buy < payload.dip_rsi_sell)
  ) {
    setBtError("RSI buy threshold must be less than RSI sell.");
    return;
  }
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Running…";
    }
    saveBacktestFormDraft();
    const data = await api("/api/backtest", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    btActiveHistoryId =
      data.history_id != null ? Number(data.history_id) : null;
    renderBacktestResult(data.result, { historyId: btActiveHistoryId });
    saveBacktestViewState();
    if (Array.isArray(data.history)) {
      renderBacktestHistory(data.history);
    } else {
      await refreshBacktestHistory();
    }
    showToast("Backtest complete.", "ok");
  } catch (err) {
    setBtError(err.message || String(err));
    showToast(err.message || "Backtest failed", "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Run backtest";
    }
  }
});
$("btn-bt-from-desk")?.addEventListener("click", () => {
  loadBacktestFromDesk();
});
$("btn-bt-compare")?.addEventListener("click", () => {
  runBacktestCompare();
});
$("btn-bt-compare-close")?.addEventListener("click", () => {
  closeBtCompare();
});
$("btn-bt-clear-history")?.addEventListener("click", async () => {
  if (!btHistorySummaries.length) {
    showToast("History is already empty.", "ok");
    return;
  }
  if (!window.confirm("Clear all saved backtest runs?")) return;
  try {
    await api("/api/backtest/history", { method: "DELETE" });
    btSelectedHistoryIds.clear();
    saveBacktestViewState();
    closeBtCompare();
    clearBacktestResultsPanel();
    renderBacktestHistory([]);
    showToast("Backtest history cleared.", "ok");
  } catch (err) {
    showToast(err.message || "Clear failed", "error");
  }
});
document.querySelector(".bt-chart-toggles")?.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-bt-series]");
  if (!btn) return;
  const key = btn.getAttribute("data-bt-series");
  if (!key || !(key in btChartSeries)) return;
  btChartSeries[key] = !btChartSeries[key];
  // Keep at least one equity series visible.
  if (
    !btChartSeries.strategy &&
    !btChartSeries.hold &&
    !btChartSeries.drawdown
  ) {
    btChartSeries[key] = true;
  }
  applyBtChartSeriesVisibility();
});
$("btn-bt-chart-fs")?.addEventListener("click", () => {
  toggleBtChartFullscreen();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && isBtChartFullscreen()) {
    ev.preventDefault();
    setBtChartFullscreen(false);
  }
});
$("bt-symbols")?.addEventListener("blur", () => {
  const el = $("bt-symbols");
  if (!el) return;
  const list = parseBtSymbolsInput(el.value);
  el.value = list.join(", ") || "AAPL";
  syncBacktestUi();
  saveBacktestFormDraft();
});
$("bt-symbols")?.addEventListener("input", () => {
  syncBacktestUi();
});
document.querySelector(".bt-run-kind-chips")?.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-bt-run-kind]");
  if (!btn) return;
  const kind =
    btn.getAttribute("data-bt-run-kind") === "portfolio"
      ? "portfolio"
      : "per_symbol";
  const hidden = $("bt-run-kind");
  if (hidden) hidden.value = kind;
  syncBtRunKindChips(kind);
  syncBacktestUi();
  saveBacktestFormDraft();
});
$("bt-detail-symbol")?.addEventListener("change", () => {
  if (!btMultiResultCache) return;
  renderBacktestResult(btMultiResultCache, {
    detailSymbol: $("bt-detail-symbol")?.value,
    scroll: false,
    quiet: true,
    skipCache: true,
    historyId: btActiveHistoryId,
  });
});

restoreBacktestFormDraft();

// Initialization
restoreBacktestFormDraft();
syncBacktestUi();
restoreBacktestLastResult();
refreshStatus().catch(() => {});

function onDeskStatusUpdate(state, { forceSettings } = {}) {
  if (Array.isArray(state.sma_presets) && state.sma_presets.length) {
    smaPresets = state.sma_presets;
  }
  if (Array.isArray(state.dip_presets) && state.dip_presets.length) {
    dipPresets = state.dip_presets;
  }
  if (Array.isArray(state.pair_presets)) {
    pairPresets = state.pair_presets;
  }
  if (forceSettings) {
    syncBacktestUi();
  }
}
