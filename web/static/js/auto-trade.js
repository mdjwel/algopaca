let formDirty = false;
let formFocused = false;
// Stop pressed, worker still draining the symbol in flight.
let loopStopping = false;
let aiPresets = [];
/** AI risk-engine inputs and their fallbacks — kept in one place so the form,
 *  the preset apply, and settings hydration cannot drift apart. */
const AI_RISK_FIELDS = [
  ["ai_risk_pct", 0.5],
  ["ai_atr_stop_mult", 1.8],
  ["ai_take_profit_r", 2],
  ["ai_trail_after_r", 1],
  ["ai_max_positions", 3],
  ["ai_daily_loss_limit_pct", 3],
  ["ai_min_hold_minutes", 15],
  ["ai_cooldown_minutes", 60],
  ["ai_max_spread_bps", 25],
  ["stop_limit_offset_pct", 0],
];
/** Preset field → form field for the risk knobs a preset owns. */
const AI_PRESET_RISK_MAP = {
  atr_stop_mult: "ai_atr_stop_mult",
  take_profit_r: "ai_take_profit_r",
  trail_after_r: "ai_trail_after_r",
  max_positions: "ai_max_positions",
  risk_pct: "ai_risk_pct",
};
/** Last cycle payload — wall binds to the featured symbol inside lastDeskWatchlist. */
let lastDeskResult = null;
let lastDeskWatchlist = [];
let lastDeskQuote = null;
/** Live marks keyed by symbol, refreshed independently of the engine cycle. */
let liveWatchQuotes = {};
let watchQuotesFetchedAt = 0;
let watchQuotesInFlight = false;
/** Matches the server-side mark cache — polling faster only re-serves it. */
const WATCH_QUOTE_INTERVAL_MS = 5000;
/** True after Stop until the server reports the loop is down. */
let suppressWatchlistUntilStop = false;
let smaPresets = [];
let dipPresets = [];
let pairPresets = [];
let aiModels = { openai: [], gemini: [], defaults: {} };
let applyingPreset = false;
let resultHistory = [];
let loopStartedAtMs = null;
let loopLastDurationSec = null;
let loopElapsedTimer = null;
let persistStatus = "ready"; // ready | editing | saving | saved | invalid
let lastPrimarySymbol = "AAPL";
let hintResetTimer = null;
let formDirtyManual = false;

const seenTradeOrderIds = new Set();
let tradeNotifyPrimed = false;
let lastTradeNotified = false;

const FALLBACK_OPENAI_MODEL = "gpt-5.6-luna";
const FALLBACK_GEMINI_MODEL = "gemini-3.5-flash-lite";


function formValue(name, fallback = "") {
  const el = $("settings")?.elements?.[name];
  if (!el) return fallback;
  // Read .value/.checked directly so disabled fields (locked while looping)
  // still round-trip — FormData omits disabled controls.
  if (el.type === "checkbox") return el.checked;
  return el.value;
}

/** Numeric field read that preserves a deliberate 0 (which means "off" for the
 *  risk knobs) while an empty or unparseable box falls back to the default —
 *  a blank "max positions" must not silently mean unlimited. */
function numField(name, fallback) {
  const raw = formValue(name, "");
  if (raw === "" || raw === null || raw === undefined) return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

function deskSizeMode(strategyMode) {
  const raw = String(formValue("size_mode", "qty") || "qty").toLowerCase();
  if (raw === "ai" && strategyMode === "ai") return "ai";
  if (raw === "notional") return "notional";
  return "qty";
}

function formPayload() {
  const mode = String(formValue("strategy_mode", "sma") || "sma");
  const symbolRaw = String(formValue("symbol", "") || "").trim().toUpperCase();
  const symbolsRaw = String(formValue("symbols", "") || "").trim().toUpperCase();
  const provider = String(formValue("ai_provider", "openai") || "openai");
  const openaiKey = String($("field-openai-key")?.value || "").trim();
  const geminiKey = String($("field-gemini-key")?.value || "").trim();
  const pairLegs = mode === "pair" ? parsePairLegsFromText(symbolsRaw || symbolRaw) : null;
  const symbol = pairLegs
    ? pairLegs.long
    : symbolRaw || "AAPL";
  const symbols = pairLegs
    ? `${pairLegs.long},${pairLegs.short}`
    : symbolsRaw || symbol;
  return {
    symbol,
    symbols,
    fast_sma: Number(formValue("fast_sma", 10) || 10),
    slow_sma: Number(formValue("slow_sma", 30) || 30),
    sma_preset: String(formValue("sma_preset", "classic") || "classic"),
    dip_preset: String(formValue("dip_preset", "deep") || "deep"),
    dip_rsi_buy: Number(formValue("dip_rsi_buy", 30) || 30),
    dip_rsi_sell: Number(formValue("dip_rsi_sell", 60) || 60),
    dip_skip_bearish: !!formValue("dip_skip_bearish", true),
    trade_qty: Number(formValue("trade_qty", 1) || 1),
    size_mode: deskSizeMode(mode),
    trade_notional: Number(formValue("trade_notional", 100) || 100),
    bar_timeframe: mode === "pair" || mode === "ls"
      ? "1Day"
      : String(formValue("bar_timeframe", "15Min") || "15Min"),
    poll_seconds: Number(formValue("poll_seconds", 20) || 20),
    strategy_mode: mode,
    pair_preset: String(formValue("pair_preset", "research_max") || "research_max"),
    pair_sma_period: Number(formValue("pair_sma_period", 50) || 50),
    pair_lookback: Number(formValue("pair_lookback", 7) || 7),
    pair_impulse_pct: Number(formValue("pair_impulse_pct", 5) || 5),
    pair_weak_side: String(formValue("pair_weak_side", "LONG") || "LONG"),
    pair_long_symbol: pairLegs ? pairLegs.long : "",
    pair_short_symbol: pairLegs ? pairLegs.short : "",
    ls_ema_fast: Number(formValue("ls_ema_fast", 21) || 21),
    ls_ema_slow: Number(formValue("ls_ema_slow", 55) || 55),
    ls_adx_min: Number(formValue("ls_adx_min", 20) || 20),
    ls_atr_stop_mult: Number(formValue("ls_atr_stop_mult", 1.5) || 1.5),
    ls_risk_pct: Number(formValue("ls_risk_pct", 1) || 1),
    ls_rr: Number(formValue("ls_rr", 2) || 2),
    ls_time_stop_bars: Number(formValue("ls_time_stop_bars", 15) || 15),
    ai_provider: provider,
    ai_preset: String(formValue("ai_preset", "balanced") || "balanced"),
    ai_instructions: String(formValue("ai_instructions", "") || ""),
    ai_min_confidence: Number(formValue("ai_min_confidence", 0.55) || 0.55),
    ai_risk_pct: numField("ai_risk_pct", 0.5),
    ai_atr_stop_mult: numField("ai_atr_stop_mult", 1.8),
    ai_take_profit_r: numField("ai_take_profit_r", 2),
    ai_trail_after_r: numField("ai_trail_after_r", 1),
    ai_max_positions: numField("ai_max_positions", 3),
    ai_daily_loss_limit_pct: numField("ai_daily_loss_limit_pct", 3),
    ai_min_hold_minutes: numField("ai_min_hold_minutes", 15),
    ai_cooldown_minutes: numField("ai_cooldown_minutes", 60),
    ai_max_spread_bps: numField("ai_max_spread_bps", 25),
    stop_limit_offset_pct: numField("stop_limit_offset_pct", 0),
    openai_model: String(
      formValue("openai_model", aiModels.defaults?.openai || FALLBACK_OPENAI_MODEL) ||
        FALLBACK_OPENAI_MODEL
    ).trim(),
    gemini_model: String(
      formValue("gemini_model", aiModels.defaults?.gemini || FALLBACK_GEMINI_MODEL) ||
        FALLBACK_GEMINI_MODEL
    ).trim(),
    openai_api_key: provider === "openai" ? openaiKey : "",
    gemini_api_key: provider === "gemini" ? geminiKey : "",
    lang: typeof i18n !== "undefined" ? i18n.getCurrentLanguage() : "en",
    // Only the dedicated Save keys button persists to disk.
    save_keys_to_env: false,
  };
}

function keysPayload() {
  const openai = String($("field-openai-key")?.value || "").trim();
  const gemini = String($("field-gemini-key")?.value || "").trim();
  return {
    openai_api_key: openai,
    gemini_api_key: gemini,
    save_to_env: !!$("field-save-keys")?.checked,
  };
}

function collectExecutedTrades(state) {
  const items = [];
  const seen = new Set();
  const visit = (item) => {
    if (!item || typeof item !== "object") return;
    if (Array.isArray(item.actions)) item.actions.forEach(visit);
    const id = item.order_id;
    if (id == null || id === "") return;
    const key = String(id);
    if (seen.has(key)) return;
    const signal = String(item.signal || item.side || "").toLowerCase();
    if (signal !== "buy" && signal !== "sell") return;
    seen.add(key);
    items.push({
      order_id: key,
      symbol: item.symbol,
      signal,
      qty: item.order_qty ?? item.qty,
      stop: item.stop_loss,
    });
  };
  visit(state?.last_result);
  (state?.last_ai_results || []).forEach(visit);
  (state?.result_history || []).forEach(visit);
  return items;
}

function formatTradeLine(trade) {
  const side = String(trade.signal || "").toUpperCase();
  const qty = trade.qty != null && trade.qty !== "" ? ` ${trade.qty}` : "";
  const symbol = trade.symbol ? ` ${String(trade.symbol).toUpperCase()}` : "";
  return `${side}${qty}${symbol}`.trim();
}

function formatTradeToast(trades) {
  if (trades.length === 1) {
    const trade = trades[0];
    const oid = trade.order_id ? ` · ${String(trade.order_id).slice(0, 8)}…` : "";
    const stop =
      trade.stop?.stop_price != null
        ? ` · stop $${Number(trade.stop.stop_price).toFixed(2)}`
        : "";
    return `${formatTradeLine(trade)} submitted${oid}${stop}`;
  }
  return `${trades.length} paper orders submitted: ${trades
    .map(formatTradeLine)
    .join(" · ")}`;
}

function notifyExecutedTrades(state) {
  lastTradeNotified = false;
  if (currentPage !== "auto-trade") return false;
  const trades = collectExecutedTrades(state);
  if (!tradeNotifyPrimed) {
    trades.forEach((trade) => seenTradeOrderIds.add(trade.order_id));
    tradeNotifyPrimed = true;
    return false;
  }
  const fresh = trades.filter((trade) => !seenTradeOrderIds.has(trade.order_id));
  fresh.forEach((trade) => seenTradeOrderIds.add(trade.order_id));
  if (!fresh.length) return false;
  showToast(
    formatTradeToast(fresh),
    "ok",
    ` <a href="${historyHref({ symbol: fresh[0]?.symbol || "" })}">${escapeHtml(tx("view_in_history", "View in History"))}</a>`
  );
  lastTradeNotified = true;
  return true;
}

function setFormError(message) {
  const el = $("form-error");
  if (!el) return;
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = message;
}

function validateLocal() {
  const payload = formPayload();
  if (payload.strategy_mode === "pair") {
    const legs = parsePairLegsFromText(payload.symbols || payload.symbol);
    if (!legs) {
      return "Long & Short Pair needs two different symbols (long first, short second).";
    }
  } else if (!payload.symbol) {
    return "Enter a symbol (e.g. AAPL).";
  }
  if (payload.strategy_mode === "sma" && !(payload.fast_sma < payload.slow_sma)) {
    return "Fast SMA must be smaller than Slow SMA.";
  }
  if (
    payload.strategy_mode === "dip" &&
    !(payload.dip_rsi_buy > 0 && payload.dip_rsi_buy < payload.dip_rsi_sell && payload.dip_rsi_sell < 100)
  ) {
    return "Dip RSI buy must be less than RSI sell (both between 0 and 100).";
  }
  if (
    payload.strategy_mode === "ls" &&
    !(payload.ls_ema_fast < payload.ls_ema_slow)
  ) {
    return "LS EMA fast must be smaller than EMA slow.";
  }
  if (payload.size_mode === "notional") {
    if (!(payload.trade_notional > 0)) {
      return "Dollar amount must be greater than 0.";
    }
  } else if (payload.size_mode !== "ai" && !(payload.trade_qty > 0)) {
    return "Shares / qty must be greater than 0.";
  }
  if (payload.poll_seconds < 10) {
    return "Poll interval must be at least 10 seconds.";
  }
  if (
    payload.strategy_mode === "ai" &&
    (payload.ai_min_confidence < 0 || payload.ai_min_confidence > 1)
  ) {
    return "Min confidence must be between 0 and 1.";
  }
  if (payload.strategy_mode === "ai") {
    if (payload.ai_risk_pct < 0 || payload.ai_risk_pct > 10) {
      return "Risk per trade must be between 0 and 10%.";
    }
    if (payload.ai_atr_stop_mult < 0 || payload.ai_atr_stop_mult > 10) {
      return "ATR stop multiple must be between 0 and 10.";
    }
    if (payload.stop_limit_offset_pct < 0 || payload.stop_limit_offset_pct > 50) {
      return "Stop-limit cushion must be between 0 and 50%.";
    }
    if (payload.ai_risk_pct > 0 && !(payload.ai_atr_stop_mult > 0)) {
      return "Risk sizing needs a stop: set Stop = ATR × greater than 0.";
    }
    if (payload.ai_max_positions < 0 || payload.ai_max_positions > 50) {
      return "Max positions must be between 0 and 50.";
    }
    if (payload.ai_daily_loss_limit_pct < 0 || payload.ai_daily_loss_limit_pct > 100) {
      return "Daily loss limit must be between 0 and 100%.";
    }
  }
  if (
    payload.strategy_mode === "sma" ||
    payload.strategy_mode === "dip" ||
    payload.strategy_mode === "pair"
  ) {
    if (payload.ai_risk_pct < 0 || payload.ai_risk_pct > 10) {
      return "Risk per trade must be between 0 and 10%.";
    }
    if (payload.ai_atr_stop_mult < 0 || payload.ai_atr_stop_mult > 10) {
      return "ATR stop multiple must be between 0 and 10.";
    }
    if (payload.stop_limit_offset_pct < 0 || payload.stop_limit_offset_pct > 50) {
      return "Stop-limit cushion must be between 0 and 50%.";
    }
    if (payload.ai_risk_pct > 0 && !(payload.ai_atr_stop_mult > 0)) {
      return "Risk sizing needs a stop: set Stop = ATR × greater than 0.";
    }
  }
  if (payload.strategy_mode !== "pair" && !String(payload.symbols || "").trim()) {
    return "Add at least one symbol to the evaluate list.";
  }
  return null;
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

function validateReadyToRun() {
  const localError = validateLocal();
  if (localError) return localError;
  const payload = formPayload();
  if (payload.strategy_mode === "ai" && !providerKeyReady(payload.ai_provider)) {
    return payload.ai_provider === "gemini"
      ? "Paste a Gemini API key on Configuration and click Save AI keys before running AI mode."
      : "Paste an OpenAI API key on Configuration and click Save AI keys before running AI mode.";
  }
  return null;
}

function syncSizeModeUi() {
  const strategyMode = formPayload().strategy_mode;
  let mode = deskSizeMode(strategyMode);
  if (strategyMode !== "ai" && mode === "ai") {
    mode = "qty";
  }
  const hidden = $("field-size-mode");
  if (hidden) hidden.value = mode;
  const shell = document.querySelector("#settings .size-input-shell");
  if (shell) shell.dataset.sizeMode = mode;
  const aiBtn = $("size-mode-ai");
  if (aiBtn) aiBtn.hidden = strategyMode !== "ai";
  document.querySelectorAll("#settings .size-toggle-btn").forEach((btn) => {
    const active = btn.dataset.sizeMode === mode;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.disabled = loopRunning;
  });
  const qtyEl = $("field-qty");
  const notionalEl = $("field-notional");
  const aiPlaceholder = $("size-ai-placeholder");
  const label = $("size-field-label");
  if (qtyEl) {
    qtyEl.hidden = mode !== "qty";
    qtyEl.disabled = loopRunning || mode !== "qty";
  }
  if (notionalEl) {
    notionalEl.hidden = mode !== "notional";
    notionalEl.disabled = loopRunning || mode !== "notional";
  }
  if (aiPlaceholder) {
    aiPlaceholder.hidden = mode !== "ai";
  }
  if (label) {
    if (mode === "ai") {
      label.htmlFor = "";
      label.textContent = tx("size_ai_label", "AI decides");
      label.setAttribute("data-i18n", "size_ai_label");
    } else if (mode === "notional") {
      label.htmlFor = "field-notional";
      label.textContent = tx("size_notional_label", "Dollar amount");
      label.setAttribute("data-i18n", "size_notional_label");
    } else {
      label.htmlFor = "field-qty";
      label.textContent = tx("shares_qty", "Shares / qty");
      label.setAttribute("data-i18n", "shares_qty");
    }
  }
  const hint = $("size-hint");
  if (!hint) return;
  if (strategyMode === "pair") {
    hint.textContent =
      "Pair mode deploys available cash into the active leg (desk size is only a fallback).";
    return;
  }
  if (strategyMode === "ls") {
    hint.textContent =
      "LS sizes by equity × risk% / ATR stop distance. Desk size is only a fallback if equity is unavailable.";
    return;
  }
  const riskPct = Number(formValue("ai_risk_pct", 0.5) || 0);
  if (riskPct > 0 && (strategyMode === "sma" || strategyMode === "dip" || strategyMode === "ai")) {
    hint.textContent = tx(
      "size_hint_risk",
      "Risk engine sizes shares so a stop-out costs that % of equity (desk qty/dollars are the fallback when risk % is 0)."
    );
    return;
  }
  if (mode === "ai") {
    hint.textContent = tx(
      "size_hint_ai",
      "The model picks shares each cycle from volatility, conviction, and liquidity — never above the risk-engine cap."
    );
    return;
  }
  if (mode === "notional") {
    const dollars = Number(formValue("trade_notional", 100) || 0);
    hint.textContent = dollars > 0
      ? `Orders size to about ${money(dollars)} at the live mark (converted to shares each cycle).`
      : "Enter a dollar amount greater than 0 to size orders in USD.";
    return;
  }
  const qty = Number(formValue("trade_qty", 1) || 0);
  hint.textContent = qty > 0
    ? `Orders use ${qty} share${qty === 1 ? "" : "s"} per signal.`
    : "Enter shares / qty greater than 0.";
}

function selectSizeMode(mode) {
  if (loopRunning) return;
  const strategyMode = String(formValue("strategy_mode", "sma") || "sma");
  let next = "qty";
  if (mode === "notional") next = "notional";
  else if (mode === "ai" && strategyMode === "ai") next = "ai";
  const hidden = $("field-size-mode");
  if (hidden) hidden.value = next;
  syncSizeModeUi();
  formDirty = true;
  schedulePersistSettings();
}

function syncRunZone() {
  const zone = $("run-zone");
  const notes = $("run-notes");
  const env = ordersEnvPhraseEn();
  if (zone) zone.classList.add("is-paper");
  if (notes) notes.classList.add("is-paper");
  const onceBtn = $("btn-once");
  if (onceBtn) {
    onceBtn.title = `Evaluate once — ${env} if the signal is buy or sell`;
  }
  const loopBtn = $("btn-loop");
  if (loopBtn && !loopRunning) {
    loopBtn.title = `Poll on an interval and place ${env} on buy/sell`;
  }
}

function idleLoopState() {
  const env = ordersEnvPhraseEn();
  if (loopLastDurationSec != null) {
    return `Idle · last run ${formatDuration(loopLastDurationSec)}. The next run can place ${env}.`;
  }
  return `Idle — edit strategy, then run once. Run once or Start loop will place ${env} on buy/sell.`;
}

function engineSideCopy(mode) {
  if (mode === "ai") {
    return {
      pill: tx("long_or_short", "Long or short"),
      side: "ai",
      note: tx("engine_side_note_ai", "Can open a long or a short on each symbol from TA, news, calendar, and earnings."),
    };
  }
  if (mode === "ls") {
    return {
      pill: tx("long_or_short", "Long or short"),
      side: "ls",
      note: tx("engine_side_note_ls", "Opens long in bull regimes and short in bear regimes on the same symbol."),
    };
  }
  if (mode === "pair") {
    return {
      pill: tx("two_leg_rotator", "Two-leg rotator"),
      side: "pair",
      note: tx("engine_side_note_pair", "Buys the long-leg or short-leg symbol — does not short-sell a stock."),
    };
  }
  return {
    pill: tx("long_only", "Long only"),
    side: "long",
    note: tx("engine_side_note_long_full", "Buy opens a long. Sell closes it. Flat accounts stay flat on a sell."),
  };
}

function syncEngineSide() {
  const copy = engineSideCopy(formPayload().strategy_mode);
  const pill = $("engine-side-pill");
  const note = $("engine-side-note");
  const wall = $("side-badge");
  if (pill) {
    pill.textContent = copy.pill;
    pill.dataset.side = copy.side;
  }
  if (note) note.textContent = copy.note;
  if (wall) {
    wall.textContent = copy.pill;
    wall.dataset.side = copy.side;
  }
}

function syncModeHint() {
  const hint = $("mode-hint");
  if (!hint) return;
  const mode = formPayload().strategy_mode;
  if (mode === "ai") {
    hint.textContent = tx(
      "mode_hint_ai",
      "Model reads TA, news, earnings, and the calendar — then can go long or short on each evaluate-list symbol."
    );
  } else if (mode === "dip") {
    hint.textContent = tx(
      "mode_hint_dip",
      "Buys oversold washes (RSI or lower Bollinger); sells into RSI recovery or upper band."
    );
  } else if (mode === "pair") {
    hint.textContent = tx(
      "mode_hint_pair",
      "Rotates full cash between a long and short leg — long in bull regime, short only on crash impulses."
    );
  } else if (mode === "ls") {
    hint.textContent = tx(
      "mode_hint_ls",
      "Takes long or short on each evaluate-list symbol from EMA/ADX regime + MACD momentum (daily bars)."
    );
  } else {
    hint.textContent = tx(
      "mode_hint_sma",
      "Classic crossover on the evaluate list — buy/sell when fast SMA crosses slow."
    );
  }
}

function syncWatchlistComposer() {
  const chips = $("watch-chips");
  const warn = $("featured-warn");
  const warnText = $("featured-warn-text");
  const includeBtn = $("btn-include-featured");
  if (!chips) return;
  const mode = formPayload().strategy_mode;
  const list = parseSymbolList(formValue("symbols", ""));
  const featured = String(formValue("symbol", "") || "").trim().toUpperCase();
  if (!list.length) {
    chips.hidden = true;
    chips.innerHTML = "";
  } else {
    chips.hidden = false;
    chips.innerHTML = list
      .map((sym, i) => {
        const role =
          mode === "pair" ? (i === 0 ? "Long" : i === 1 ? "Short" : "") : "";
        const isFeat = sym === featured;
        return (
          `<button type="button" class="watch-chip${isFeat ? " is-featured" : ""}" data-symbol="${escapeHtml(sym)}" aria-pressed="${isFeat ? "true" : "false"}" title="${escapeHtml(tx("feature_symbol_title", `Click to feature ${sym}`, { symbol: sym }))}">` +
          (role ? `<span class="watch-chip-role">${escapeHtml(role)}</span>` : "") +
          `${escapeHtml(sym)}</button>`
        );
      })
      .join("");
  }
  const missing = mode !== "pair" && featured && !list.includes(featured);
  if (warn) {
    warn.hidden = !missing;
    if (warnText && missing) {
      warnText.textContent = `${featured} is featured on the wall but not on the evaluate list — it will not be traded.`;
    }
    if (includeBtn) {
      includeBtn.textContent = featured ? `Include ${featured}` : "Include";
      includeBtn.disabled = loopRunning;
    }
  }
}

function includeFeaturedInWatchlist() {
  const form = $("settings");
  if (!form?.symbols || loopRunning) return;
  const featured = String(form.symbol?.value || "").trim().toUpperCase();
  if (!featured) return;
  const list = parseSymbolList(form.symbols.value);
  if (!list.includes(featured)) {
    form.symbols.value = [featured, ...list].join(", ");
  }
  formDirty = true;
  setFormError(null);
  schedulePersistSettings();
  syncModeUi();
}

function setFeaturedSymbol(sym) {
  const form = $("settings");
  if (!form?.symbol) return;
  const next = String(sym || "").trim().toUpperCase();
  if (!next) return;
  form.symbol.value = next;
  lastPrimarySymbol = next;
  formDirty = true;
  if (!loopRunning) {
    schedulePersistSettings();
  }
  syncModeUi();
  applyAiWatchlist(lastDeskWatchlist);
  syncFeaturedWall();
}

function syncPollHint() {
  const hint = $("poll-hint");
  if (!hint) return;
  const sec = Number(formValue("poll_seconds", 20) || 0);
  if (!(sec >= 10)) {
    hint.textContent = "Minimum 10 seconds between loop cycles.";
    hint.classList.add("invalid");
    return;
  }
  hint.classList.remove("invalid");
  hint.textContent =
    sec === 20
      ? "Loop checks the market every 20 seconds."
      : `Loop checks the market every ${sec} seconds.`;
}

function syncMarketHints() {
  const symbolHint = $("symbol-hint");
  const watchHint = $("watchlist-hint");
  const mode = formPayload().strategy_mode;
  const ai = mode === "ai";
  const pair = mode === "pair";
  const ls = mode === "ls";
  if (symbolHint) {
    symbolHint.textContent = pair
      ? "Featured = long leg. Pair mode uses daily bars."
      : ls
        ? "Quote and last signal on the wall. LS uses daily bars and can go long or short."
      : ai
        ? "Quote and last signal on the wall. Include it on the evaluate list if you want AI to trade it."
        : "Quote and last signal on the wall. Include it on the evaluate list to trade it.";
  }
  if (watchHint) {
    if (pair) {
      watchHint.textContent =
        "Enter exactly two symbols: long leg first, short leg second.";
      return;
    }
    const n = parseSymbolList(formValue("symbols", "")).length || 1;
    const who =
      mode === "ai"
        ? "AI evaluates"
        : mode === "dip"
          ? "Dip evaluates"
          : mode === "ls"
            ? "LS evaluates"
          : "SMA evaluates";
    watchHint.textContent =
      `${who} ${n} symbol${n === 1 ? "" : "s"} each cycle. Featured is highlighted on the wall.`;
  }
}

function syncStrategyHint(forceState) {
  const hint = $("strategy-hint");
  if (!hint) return;
  if (forceState) persistStatus = forceState;
  let state = persistStatus;
  let label = "Ready";
  if (loopRunning) {
    state = "locked";
    label = "Locked";
  } else if (state === "invalid") {
    label = "Fix errors";
  } else if (state === "saving") {
    label = "Saving…";
  } else if (state === "editing") {
    label = "Editing…";
  } else if (state === "saved") {
    label = "Saved";
  } else {
    state = "ready";
    label = "Ready";
  }
  hint.textContent = label;
  hint.dataset.state = state;
  hint.title =
    state === "locked"
      ? "Stop the loop to edit strategy"
      : state === "invalid"
        ? "Fix validation errors before running"
        : "Strategy settings auto-save; API keys live on Configuration";
}

function maybeSyncWatchlistFromPrimary(ev) {
  if (ev?.target?.name !== "symbol") return;
  const form = $("settings");
  if (!form?.symbols) return;
  const next = String(form.symbol.value || "")
    .trim()
    .toUpperCase();
  const prev = String(lastPrimarySymbol || "")
    .trim()
    .toUpperCase();
  const watch = String(form.symbols.value || "")
    .trim()
    .toUpperCase();
  // Keep a solo watchlist in lockstep with primary; leave multi-lists alone.
  if (!watch || watch === prev) {
    form.symbols.value = next || watch;
  }
  lastPrimarySymbol = next || prev;
}

function populateModelOptions(models) {
  if (!models) return;
  aiModels = {
    openai: Array.isArray(models.openai) ? models.openai : [],
    gemini: Array.isArray(models.gemini) ? models.gemini : [],
    defaults: models.defaults || {},
  };
  fillModelSelect(
    $("field-openai-model"),
    aiModels.openai,
    aiModels.defaults.openai || FALLBACK_OPENAI_MODEL
  );
  fillModelSelect(
    $("field-gemini-model"),
    aiModels.gemini,
    aiModels.defaults.gemini || FALLBACK_GEMINI_MODEL
  );
}

function fillModelSelect(select, options, preferred) {
  if (!select || !options?.length) return;
  const wanted = preferred || select.value || options[0].id;
  const ids = options.map((m) => m.id);
  // Preserve a saved custom / legacy model so refresh doesn't silently change it.
  const extra =
    wanted && !ids.includes(wanted)
      ? [{ id: wanted, label: `${wanted} (saved)` }]
      : [];
  const all = [...extra, ...options];
  const same =
    select.options.length === all.length &&
    [...select.options].every((opt, i) => opt.value === all[i].id);
  const targetVal = all.some((m) => m.id === wanted) ? wanted : all[0].id;
  if (!same) {
    select.innerHTML = all
      .map(
        (m) =>
          `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label || m.id)}</option>`
      )
      .join("");
    select.value = targetVal;
    refreshNiceSelect(select);
  } else if (select.value !== targetVal) {
    select.value = targetVal;
    refreshNiceSelect(select);
  }
}

function syncModeUi() {
  if (!$("settings")) return;
  const payload = formPayload();
  const mode = payload.strategy_mode;
  const ai = mode === "ai";
  const sma = mode === "sma";
  const dip = mode === "dip";
  const pair = mode === "pair";
  const ls = mode === "ls";
  const provider = payload.ai_provider === "gemini" ? "gemini" : "openai";
  document.body.classList.toggle("mode-ai", ai);
  // Provider panels first, then ai-only — so SMA/dip mode re-hides model fields
  // that also carry provider-* classes.
  document.querySelectorAll(".provider-openai").forEach((el) => {
    el.hidden = provider !== "openai";
  });
  document.querySelectorAll(".provider-gemini").forEach((el) => {
    el.hidden = provider !== "gemini";
  });
  document.querySelectorAll(".ai-only").forEach((el) => {
    el.hidden = !ai;
  });
  document.querySelectorAll(".risk-engine-panel").forEach((el) => {
    // LS keeps its own ATR / risk % fields under .ls-only.
    el.hidden = ls;
  });
  document.querySelectorAll(".sma-only").forEach((el) => {
    el.hidden = !sma;
  });
  document.querySelectorAll(".dip-only").forEach((el) => {
    el.hidden = !dip;
  });
  document.querySelectorAll(".pair-only").forEach((el) => {
    el.hidden = !pair;
  });
  document.querySelectorAll(".ls-only").forEach((el) => {
    el.hidden = !ls;
  });
  const tf = $("field-timeframe");
  if (pair || ls) {
    const form = $("settings");
    if (form?.bar_timeframe) form.bar_timeframe.value = "1Day";
  }
  if (tf) tf.disabled = loopRunning || pair || ls;
  if (pair) {
    const form = $("settings");
    // Clear legacy SOXL/SOXS lock once; leave user-entered pairs alone.
    const locked =
      String(form?.symbols?.value || "").replace(/\s+/g, "").toUpperCase() ===
      "SOXL,SOXS";
    if (locked) {
      if (form.symbol) form.symbol.value = "";
      if (form.symbols) form.symbols.value = "";
    }
  }
  // Model dropdown: only the active provider's select while in AI mode.
  const openaiModel = $("field-openai-model")?.closest("label");
  const geminiModel = $("field-gemini-model")?.closest("label");
  if (openaiModel) openaiModel.hidden = !ai || provider !== "openai";
  if (geminiModel) geminiModel.hidden = !ai || provider !== "gemini";
  const metricA = $("metric-a-label");
  const metricB = $("metric-b-label");
  if (metricA && metricB) {
    if (ai) {
      metricA.textContent = "RSI (14)";
      metricB.textContent = "Trend";
    } else if (dip) {
      metricA.textContent = "RSI (14)";
      metricB.textContent = "BB %b";
    } else if (pair) {
      metricA.textContent = "Lookback %";
      metricB.textContent = "SMA";
    } else if (ls) {
      metricA.textContent = "EMA fast";
      metricB.textContent = "EMA slow";
    } else {
      metricA.textContent = "Fast SMA";
      metricB.textContent = "Slow SMA";
    }
  }
  syncPresetHint();
  syncSmaPresetHint();
  syncDipPresetHint();
  syncPairPresetHint();
  syncModeHint();
  syncEngineSide();
  syncMarketHints();
  syncWatchlistComposer();
  syncPollHint();
  syncSizeModeUi();
  syncRunZone();
  syncStrategyHint();
}

function findPreset(id) {
  return aiPresets.find((p) => p.id === id) || null;
}

function findSmaPreset(id) {
  return smaPresets.find((p) => p.id === id) || null;
}

function findDipPreset(id) {
  return dipPresets.find((p) => p.id === id) || null;
}

function findPairPreset(id) {
  return pairPresets.find((p) => p.id === id) || null;
}

function syncPresetHint() {
  const hint = $("preset-hint");
  if (!hint) return;
  const preset = findPreset(formPayload().ai_preset);
  const key = "preset_summary_" + (preset?.id || "custom");
  const fallback = preset?.summary || "Choose how the AI should trade.";
  hint.textContent = tx(key, fallback);
}

function syncSmaPresetHint() {
  const hint = $("sma-preset-hint");
  if (!hint) return;
  const preset = findSmaPreset(formPayload().sma_preset);
  const key = "sma_preset_summary_" + (preset?.id || "custom");
  const fallback = preset?.summary || "Choose an SMA window pair.";
  hint.textContent = tx(key, fallback);
}

function syncDipPresetHint() {
  const hint = $("dip-preset-hint");
  if (!hint) return;
  const preset = findDipPreset(formPayload().dip_preset);
  const key = "dip_preset_summary_" + (preset?.id || "custom");
  const fallback = preset?.summary || "Choose how deep a wash to buy.";
  hint.textContent = tx(key, fallback);
}

function syncPairPresetHint() {
  const hint = $("pair-preset-hint");
  if (!hint) return;
  const preset = findPairPreset(formPayload().pair_preset);
  const key = "pair_preset_summary_" + (preset?.id || "custom");
  const fallback = preset?.summary || "Long leg by default; short leg only on confirmed bear impulses.";
  hint.textContent = tx(key, fallback);
}

function applyPairPreset(presetId) {
  const preset = findPairPreset(presetId);
  const form = $("settings");
  if (!preset || !form?.pair_preset) return;
  applyingPreset = true;
  form.pair_preset.value = preset.id;
  refreshNiceSelect(form.pair_preset);
  if (preset.id !== "custom") {
    if (form.pair_sma_period) form.pair_sma_period.value = preset.sma_period;
    if (form.pair_lookback) form.pair_lookback.value = preset.lookback;
    if (form.pair_impulse_pct) form.pair_impulse_pct.value = preset.impulse_pct;
    if (form.pair_weak_side) form.pair_weak_side.value = preset.weak_side || "LONG";
  }
  syncPairPresetHint();
  applyingPreset = false;
}

function populatePresetOptions(presets) {
  const select = $("field-preset");
  if (!select || !presets?.length) return;
  const current = select.value || "balanced";
  const targetVal = presets.some((p) => p.id === current) ? current : presets[0].id;
  select.innerHTML = presets
    .map((p) => {
      const label = tx("preset_" + p.id, p.label);
      return `<option value="${escapeHtml(p.id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  select.value = targetVal;
  refreshNiceSelect(select);
}

function populateSmaPresetOptions(presets) {
  const selects = [$("field-sma-preset"), $("bt-sma-preset")].filter(Boolean);
  if (!selects.length || !presets?.length) return;
  for (const select of selects) {
    const current = select.value || "classic";
    const targetVal = presets.some((p) => p.id === current) ? current : presets[0].id;
    select.innerHTML = presets
      .map((p) => {
        const label = tx("preset_sma_" + p.id, tx("preset_" + p.id, p.label));
        return `<option value="${escapeHtml(p.id)}">${escapeHtml(label)}</option>`;
      })
      .join("");
    select.value = targetVal;
    refreshNiceSelect(select);
  }
}

function populateDipPresetOptions(presets) {
  const selects = [$("field-dip-preset"), $("bt-dip-preset")].filter(Boolean);
  if (!selects.length || !presets?.length) return;
  for (const select of selects) {
    const current = select.value || "deep";
    const targetVal = presets.some((p) => p.id === current) ? current : presets[0].id;
    select.innerHTML = presets
      .map((p) => {
        const label = tx("preset_dip_" + p.id, tx("preset_" + p.id, p.label));
        return `<option value="${escapeHtml(p.id)}">${escapeHtml(label)}</option>`;
      })
      .join("");
    select.value = targetVal;
    refreshNiceSelect(select);
  }
}

function applyAiPreset(presetId, { forceInstructions = true } = {}) {
  const preset = findPreset(presetId);
  const form = $("settings");
  if (!preset || !form?.ai_preset) return;
  applyingPreset = true;
  form.ai_preset.value = preset.id;
  refreshNiceSelect(form.ai_preset);
  if (preset.id !== "custom") {
    if (forceInstructions && form.ai_instructions) {
      form.ai_instructions.value = preset.instructions || "";
    }
    if (form.ai_min_confidence) {
      form.ai_min_confidence.value = preset.min_confidence;
    }
    // A trend book and a mean-reversion book need opposite stop/target geometry,
    // so the preset carries its own — mirror it into the visible inputs.
    for (const [presetKey, fieldName] of Object.entries(AI_PRESET_RISK_MAP)) {
      const value = preset[presetKey];
      if (value !== null && value !== undefined && form[fieldName]) {
        form[fieldName].value = value;
      }
    }
  }
  syncPresetHint();
  applyingPreset = false;
}

function applySmaPreset(presetId) {
  const preset = findSmaPreset(presetId);
  const form = $("settings");
  if (!preset || !form?.sma_preset) return;
  applyingPreset = true;
  form.sma_preset.value = preset.id;
  refreshNiceSelect(form.sma_preset);
  if (preset.id !== "custom") {
    if (form.fast_sma) form.fast_sma.value = preset.fast_sma;
    if (form.slow_sma) form.slow_sma.value = preset.slow_sma;
  }
  syncSmaPresetHint();
  syncSmaHint();
  applyingPreset = false;
}

function applyDipPreset(presetId) {
  const preset = findDipPreset(presetId);
  const form = $("settings");
  if (!preset || !form?.dip_preset) return;
  applyingPreset = true;
  form.dip_preset.value = preset.id;
  refreshNiceSelect(form.dip_preset);
  if (preset.id !== "custom") {
    if (form.dip_rsi_buy) form.dip_rsi_buy.value = preset.rsi_buy;
    if (form.dip_rsi_sell) form.dip_rsi_sell.value = preset.rsi_sell;
    if (form.dip_skip_bearish) form.dip_skip_bearish.checked = !!preset.skip_bearish;
  }
  syncDipPresetHint();
  syncDipHint();
  applyingPreset = false;
}

function markPresetCustomIfEdited(ev) {
  if (applyingPreset) return;
  const form = $("settings");
  const name = ev?.target?.name;
  if (name === "ai_instructions") {
    if (!form?.ai_preset || form.ai_preset.value === "custom") return;
    form.ai_preset.value = "custom";
    refreshNiceSelect(form.ai_preset);
    syncPresetHint();
    return;
  }
  if (name === "fast_sma" || name === "slow_sma") {
    if (!form?.sma_preset || form.sma_preset.value === "custom") return;
    form.sma_preset.value = "custom";
    refreshNiceSelect(form.sma_preset);
    syncSmaPresetHint();
    return;
  }
  if (
    name === "dip_rsi_buy" ||
    name === "dip_rsi_sell" ||
    name === "dip_skip_bearish"
  ) {
    if (!form?.dip_preset || form.dip_preset.value === "custom") return;
    form.dip_preset.value = "custom";
    refreshNiceSelect(form.dip_preset);
    syncDipPresetHint();
    return;
  }
  if (
    name === "pair_sma_period" ||
    name === "pair_lookback" ||
    name === "pair_impulse_pct" ||
    name === "pair_weak_side"
  ) {
    if (!form?.pair_preset || form.pair_preset.value === "custom") return;
    form.pair_preset.value = "custom";
    refreshNiceSelect(form.pair_preset);
    syncPairPresetHint();
  }
}

function syncDipHint() {
  const hint = $("dip-hint");
  if (!hint) return;
  const err = validateLocal();
  const dipIssue = err && err.includes("Dip RSI");
  hint.classList.toggle("invalid", !!dipIssue);
  if (dipIssue) {
    hint.textContent = "RSI buy must be less than RSI sell before you can run.";
  } else {
    const p = formPayload();
    const skip = p.dip_skip_bearish ? "skip bearish" : "allow bearish";
    const preset = findDipPreset(p.dip_preset);
    const band =
      preset && preset.use_lower_band === false
        ? "RSI only (no BB-only)"
        : "or lower BB";
    hint.textContent = `Buy RSI ≤${p.dip_rsi_buy} ${band}; sell RSI ≥${p.dip_rsi_sell} or upper band · ${skip}.`;
  }
}

function syncSmaHint() {
  const hint = $("sma-hint");
  if (!hint) return;
  const err = validateLocal();
  const smaIssue = err && err.includes("Fast SMA");
  hint.classList.toggle("invalid", !!smaIssue);
  if (smaIssue) {
    hint.textContent = "Fast must be smaller than Slow before you can run.";
  } else {
    const p = formPayload();
    hint.textContent = `Windows: ${p.fast_sma} / ${p.slow_sma} on ${p.bar_timeframe} bars.`;
  }
}

function applySettings(settings, { force = false } = {}) {
  if (!settings) return;
  lastDeskSettings = settings;
  if (!force && (formDirty || formFocused)) return;
  const form = $("settings");
  if (!form) {
    // Manual-order fields live on their own page — hydrate only when present.
    if (typeof hydrateManualFromSettings === "function") {
      hydrateManualFromSettings(settings, { force });
    }
    return;
  }
  form.symbol.value = settings.symbol;
  if (form.symbols) form.symbols.value = settings.symbols || settings.symbol;
  form.fast_sma.value = settings.fast_sma;
  form.slow_sma.value = settings.slow_sma;
  if (form.sma_preset) form.sma_preset.value = settings.sma_preset || "classic";
  if (form.dip_preset) form.dip_preset.value = settings.dip_preset || "deep";
  if (form.dip_rsi_buy) form.dip_rsi_buy.value = settings.dip_rsi_buy ?? 30;
  if (form.dip_rsi_sell) form.dip_rsi_sell.value = settings.dip_rsi_sell ?? 60;
  if (form.dip_skip_bearish) {
    form.dip_skip_bearish.checked = settings.dip_skip_bearish !== false;
  }
  if (form.pair_preset) form.pair_preset.value = settings.pair_preset || "research_max";
  if (form.pair_sma_period) form.pair_sma_period.value = settings.pair_sma_period ?? 50;
  if (form.pair_lookback) form.pair_lookback.value = settings.pair_lookback ?? 7;
  if (form.pair_impulse_pct) form.pair_impulse_pct.value = settings.pair_impulse_pct ?? 5;
  if (form.pair_weak_side) {
    const weak = String(settings.pair_weak_side || "LONG").toUpperCase();
    form.pair_weak_side.value = weak === "SOXL" ? "LONG" : weak === "CASH" ? "CASH" : "LONG";
  }
  if (form.ls_ema_fast) form.ls_ema_fast.value = settings.ls_ema_fast ?? 21;
  if (form.ls_ema_slow) form.ls_ema_slow.value = settings.ls_ema_slow ?? 55;
  if (form.ls_adx_min) form.ls_adx_min.value = settings.ls_adx_min ?? 20;
  if (form.ls_atr_stop_mult) form.ls_atr_stop_mult.value = settings.ls_atr_stop_mult ?? 1.5;
  if (form.ls_risk_pct) form.ls_risk_pct.value = settings.ls_risk_pct ?? 1;
  if (form.ls_rr) form.ls_rr.value = settings.ls_rr ?? 2;
  if (form.ls_time_stop_bars) form.ls_time_stop_bars.value = settings.ls_time_stop_bars ?? 15;
  form.trade_qty.value = settings.trade_qty;
  if (form.trade_notional) {
    form.trade_notional.value =
      settings.trade_notional ?? (form.trade_notional.value || 100);
  }
  const savedMode = String(settings.size_mode || "qty").toLowerCase();
  const resolvedSize =
    savedMode === "ai" && (settings.strategy_mode || "sma") === "ai"
      ? "ai"
      : savedMode === "notional"
        ? "notional"
        : "qty";
  if (form.size_mode) form.size_mode.value = resolvedSize;
  form.bar_timeframe.value = settings.bar_timeframe || "15Min";
  form.poll_seconds.value = settings.poll_seconds ?? 20;
  if (form.strategy_mode) form.strategy_mode.value = settings.strategy_mode || "sma";
  if (form.ai_provider) form.ai_provider.value = settings.ai_provider || "openai";
  if (form.ai_preset) form.ai_preset.value = settings.ai_preset || "balanced";
  if (form.ai_instructions) {
    let text = settings.ai_instructions || "";
    if (!text.trim()) {
      const preset = findPreset(form.ai_preset.value);
      if (preset && preset.id !== "custom") {
        text = preset.instructions || "";
      }
    }
    form.ai_instructions.value = text;
  }
  if (form.ai_min_confidence) {
    form.ai_min_confidence.value = settings.ai_min_confidence ?? 0.55;
  }
  for (const [name, fallback] of AI_RISK_FIELDS) {
    if (form[name]) form[name].value = settings[name] ?? fallback;
  }
  if (form.openai_model) {
    fillModelSelect(
      form.openai_model,
      aiModels.openai,
      settings.openai_model || aiModels.defaults?.openai || FALLBACK_OPENAI_MODEL
    );
  }
  if (form.gemini_model) {
    fillModelSelect(
      form.gemini_model,
      aiModels.gemini,
      settings.gemini_model || aiModels.defaults?.gemini || FALLBACK_GEMINI_MODEL
    );
  }
  formDirty = false;
  lastPrimarySymbol = String(settings.symbol || "AAPL").trim().toUpperCase();
  persistStatus = "ready";
  syncModeBadge(
    settings.strategy_mode,
    settings.ai_preset,
    settings.sma_preset,
    settings.dip_preset,
    settings.pair_preset
  );
  syncModeUi();
  syncSmaHint();
  syncDipHint();
  syncSizeModeUi();
  refreshNiceSelects($("settings"));
  if (typeof hydrateManualFromSettings === "function") {
    hydrateManualFromSettings(settings, { force });
  }
}

function deskEnvLabel() {
  const mode =
    lastAlpacaStatus?.trading_mode ||
    lastAccount?.trading_mode ||
    (lastAccount?.paper === false ? "live" : "paper");
  return mode === "live" ? "live" : "paper";
}

/** Localized "paper orders" / "live orders", for strings that go through tx(). */
function ordersEnvPhrase() {
  return deskEnvLabel() === "live"
    ? tx("live_orders", "live orders")
    : tx("paper_orders", "paper orders");
}

/** The same phrase in plain English, for the button titles and idle line that
 *  are not translated yet — a localized fragment inside an English sentence
 *  reads worse than either language on its own. */
function ordersEnvPhraseEn() {
  return deskEnvLabel() === "live" ? "live orders" : "paper orders";
}

function syncModeBadge(strategyMode, aiPresetId, smaPresetId, dipPresetId, pairPresetId) {
  const badge = $("mode-badge");
  if (!badge) return;
  const env = deskEnvLabel();
  const orderLabel =
    env === "live"
      ? tx("live_orders_short", "Live orders")
      : tx("paper_orders_short", "Paper orders");
  const mode = strategyMode || formPayload().strategy_mode;
  if (mode === "ai") {
    const preset = findPreset(aiPresetId || formPayload().ai_preset);
    const presetLabel = preset ? `${preset.label} · ` : "";
    badge.textContent = `AI · ${presetLabel}${orderLabel}`;
  } else if (mode === "dip") {
    const preset = findDipPreset(dipPresetId || formPayload().dip_preset);
    const presetLabel = preset ? `${preset.label} · ` : "";
    badge.textContent = `Dip · ${presetLabel}${orderLabel}`;
  } else if (mode === "pair") {
    const preset = findPairPreset(pairPresetId || formPayload().pair_preset);
    const presetLabel = preset ? `${preset.label} · ` : "";
    badge.textContent = `Pair · ${presetLabel}${orderLabel}`;
  } else if (mode === "ls") {
    badge.textContent = `LS · Regime Dual Momentum · ${orderLabel}`;
  } else {
    const preset = findSmaPreset(smaPresetId || formPayload().sma_preset);
    const presetLabel = preset ? `${preset.label} · ` : "";
    badge.textContent = `SMA · ${presetLabel}${orderLabel}`;
  }
  badge.className = env === "live" ? "mode-badge env-live" : "mode-badge armed";
}

function featuredSymbol() {
  return String(formValue("symbol", "") || lastDeskSettings?.symbol || "")
    .trim()
    .toUpperCase();
}

function resultForFeatured() {
  const feat = featuredSymbol();
  const list = Array.isArray(lastDeskWatchlist) ? lastDeskWatchlist : [];
  const last = lastDeskResult;
  if (formPayload().strategy_mode === "pair") return last;
  if (feat) {
    const hit = list.find((r) => String(r?.symbol || "").toUpperCase() === feat);
    if (hit) return hit;
    if (last && String(last.symbol || "").toUpperCase() === feat) return last;
    if (last && !list.length && !last.symbol) return last;
    return null;
  }
  return last;
}

function quoteForFeatured() {
  const feat = featuredSymbol();
  // The watchlist poll runs on a shorter cycle than /api/status, so prefer it
  // when it has the featured name — otherwise the wall and its own row can show
  // two different prices for the same symbol.
  const live = feat ? liveWatchQuotes[feat] : null;
  if (live) return live;

  const quote = lastDeskQuote;
  if (!quote) return null;
  const qSym = String(quote.symbol || "").toUpperCase();
  if (feat && qSym && qSym !== feat) return null;
  return quote;
}

function syncFeaturedWall() {
  if (!$("signal")) return;
  const result = resultForFeatured();
  applyResult(result);
  applyQuote(quoteForFeatured(), result);
}

/** Pick the note copy that matches the picked UI language.
 *
 *  AI rows carry `thesis` in `note_lang` plus an English `thesis_en`. Rendering
 *  the localized copy only when its language matches is what lets a switch back
 *  to English clear a Bangla note without re-running the model. Rows from before
 *  this (no `note_lang`) still render their single stored copy. */
function noteFor(row) {
  if (!row) return "";
  const lang = typeof i18n !== "undefined" ? i18n.getCurrentLanguage() : "en";
  const noteLang = String(row.note_lang || "").trim().toLowerCase();
  const localized = String(row.thesis || "").trim();
  const english = String(row.thesis_en || "").trim();

  let raw = localized;
  if (noteLang && noteLang !== lang) raw = english || localized;
  if (!raw) raw = english || String(row.reason || "").trim();
  if (!raw) return "";

  return typeof window.translateNote === "function"
    ? window.translateNote(raw, lang)
    : raw;
}

function applyQuote(quote, result) {
  const priceEl = $("price");
  const metaEl = $("price-meta");
  if (!priceEl || !metaEl) return;
  const mark = quote?.price ?? result?.price;
  if (mark == null || Number.isNaN(Number(mark))) {
    priceEl.textContent = "$—";
    metaEl.textContent = "Waiting for live quote…";
    return;
  }
  priceEl.textContent = money(mark);
  const session = formatSession(quote?.session || result?.session);
  const source = quote?.source || result?.price_source || "quote";
  const barClose = quote?.bar_close ?? result?.bar_close;
  const ageSec = quote?.age_seconds;
  let meta = `${session} · ${source.replaceAll("_", " ")}`;
  if (typeof ageSec === "number") {
    meta += ` · ${formatAge(ageSec)}`;
  }
  if (barClose != null && Math.abs(Number(barClose) - Number(mark)) > 0.005) {
    meta += ` · bar close ${money(barClose)}`;
  }
  metaEl.textContent = meta;
}

function stopLoopElapsedTicker() {
  if (loopElapsedTimer) {
    clearInterval(loopElapsedTimer);
    loopElapsedTimer = null;
  }
}

function tickLoopElapsed() {
  const el = $("loop-elapsed");
  if (!el) return;
  if (loopRunning && loopStartedAtMs) {
    const sec = (Date.now() - loopStartedAtMs) / 1000;
    const label = formatDuration(sec);
    el.hidden = false;
    el.textContent = loopStopping
      ? tx("stopping_label", `Stopping ${label}`, { label })
      : `Running ${label}`;
    el.dataset.kind = loopStopping ? "stopping" : "running";
    if (!busy) {
      const loopState = $("loop-state");
      if (loopState) {
        loopState.textContent =
          `Loop running · ${label} — Stop to edit strategy again.`;
      }
    }
    const live = document.querySelector(
      '.history-session[data-status="running"] .history-session-time'
    );
    if (live) {
      const started = live.textContent.split(" · ")[0] || "—";
      live.textContent = `${started} · ${label}`;
    }
  } else if (!loopRunning && loopLastDurationSec != null) {
    el.hidden = false;
    el.textContent = `Last run ${formatDuration(loopLastDurationSec)}`;
    el.dataset.kind = "last";
  } else {
    el.hidden = true;
    el.textContent = "";
    delete el.dataset.kind;
  }
}

function syncLoopElapsedTicker() {
  stopLoopElapsedTicker();
  tickLoopElapsed();
  if (loopRunning && loopStartedAtMs) {
    loopElapsedTimer = setInterval(tickLoopElapsed, 1000);
  }
}

function applyResult(result) {
  const signalEl = $("signal");
  if (!signalEl) return;
  const aiMeta = $("ai-meta");
  if (!result) {
    signalEl.textContent = "—";
    signalEl.className = "signal hold";
    $("fast").textContent = "—";
    $("slow").textContent = "—";
    $("position").textContent = "—";
    const feat = String(formValue("symbol", "") || "").trim().toUpperCase();
    $("reason").textContent = feat
      ? `No signal yet for ${feat}. Configure the strategy, then Run once.`
      : "Configure the strategy, then Run once to see a signal.";
    if (aiMeta) aiMeta.hidden = true;
    return;
  }
  const signal = (result.signal || "hold").toLowerCase();
  const el = signalEl;
  el.textContent = signal.toUpperCase();
  el.className = `signal ${signal}`;

  const isAi = !!result.provider;
  const isDip = !isAi && (result.engine === "dip" || result.rsi != null);
  if (isAi) {
    const rsi = result.rsi ?? result.context_summary?.rsi;
    const trend = result.trend_bias || result.context_summary?.trend || result.ta_bias;
    $("fast").textContent =
      rsi != null && rsi !== "" ? Number(rsi).toFixed(1) : "—";
    $("slow").textContent = trend ? String(trend) : "—";
  } else if (isDip) {
    const rsi = result.rsi ?? result.fast_sma;
    const pct = result.bb_pct_b != null ? Number(result.bb_pct_b) * 100 : result.slow_sma;
    $("fast").textContent =
      rsi != null && rsi !== "" ? Number(rsi).toFixed(1) : "—";
    $("slow").textContent =
      pct != null && pct !== "" ? `${Number(pct).toFixed(0)}` : "—";
  } else {
    $("fast").textContent =
      result.fast_sma != null && result.fast_sma !== ""
        ? Number(result.fast_sma).toFixed(2)
        : "—";
    $("slow").textContent =
      result.slow_sma != null && result.slow_sma !== ""
        ? Number(result.slow_sma).toFixed(2)
        : "—";
  }
  const pos = Number(result.position);
  if (Number.isFinite(pos) && pos < 0) {
    $("position").textContent = `short ${Math.abs(pos)}`;
  } else {
    $("position").textContent = String(result.position ?? "—");
  }
  $("reason").textContent = noteFor(result);

  if (!aiMeta) return;
  if (isAi) {
    aiMeta.hidden = false;
    const bits = [
      `${result.provider}/${result.model || "?"}`,
      `conf ${(Number(result.confidence) || 0).toFixed(2)}`,
      `TA ${result.ta_bias || "—"}`,
      `news ${result.news_bias || "—"}`,
    ];
    if (result.regime) bits.push(result.regime);
    if (result.adx != null) bits.push(`ADX ${Number(result.adx).toFixed(0)}`);
    if (result.atr_pct != null) bits.push(`ATR ${Number(result.atr_pct).toFixed(1)}%`);
    // Open risk in R is the number that decides an exit — keep it prominent.
    if (result.r_multiple != null) {
      const r = Number(result.r_multiple);
      bits.push(`${r >= 0 ? "+" : ""}${r.toFixed(2)}R`);
    }
    if (result.spread_bps != null) {
      bits.push(`spread ${Number(result.spread_bps).toFixed(0)}bps`);
    }
    if (result.news_count != null) bits.push(`${result.news_count} headlines`);
    if (result.calendar_count != null) bits.push(`${result.calendar_count} events`);
    if (result.managed?.actions?.length) bits.push(...result.managed.actions);
    if (result.risk_blocked) bits.push("risk guard");
    if (result.earnings_stance) {
      const earn = result.earnings_blackout
        ? "earn blackout"
        : result.earnings_result
          ? `earn ${result.earnings_stance}/${result.earnings_result}`
          : `earn ${result.earnings_stance}`;
      bits.push(earn);
    }
    if (result.intent === "open_short") bits.push("short");
    else if (result.intent === "cover") bits.push("cover");
    if (result.order_id) bits.push("submitted");
    if (result.stop_loss?.stop_price != null) {
      bits.push(`stop $${Number(result.stop_loss.stop_price).toFixed(2)}`);
    } else if (result.stop_loss_pct > 0) {
      bits.push(`SL ${Number(result.stop_loss_pct).toFixed(1)}%`);
    }
    aiMeta.textContent = bits.join(" · ");
  } else if (result.engine === "manual" || result.mode === "manual") {
    aiMeta.hidden = false;
    const bits = [
      `Manual ${String(result.order_type || "order")}`,
      String(result.side || result.signal || "").toUpperCase(),
    ];
    if (result.order_qty != null) bits.push(`qty ${result.order_qty}`);
    if (result.limit_price != null) bits.push(`limit $${Number(result.limit_price).toFixed(2)}`);
    if (result.stop_loss?.stop_price != null) {
      bits.push(`stop $${Number(result.stop_loss.stop_price).toFixed(2)}`);
    } else if (result.stop_preview != null) {
      bits.push(`stop ~$${Number(result.stop_preview).toFixed(2)}`);
    } else if (result.stop_loss_pct > 0) {
      bits.push(`SL ${Number(result.stop_loss_pct).toFixed(1)}%`);
    }
    if (result.preview) bits.push("preview");
    aiMeta.textContent = bits.filter(Boolean).join(" · ");
  } else if (result.order_id) {
    aiMeta.hidden = false;
    const bits = [`${String(result.signal || "").toUpperCase()} submitted`];
    if (result.order_qty != null) bits.push(`qty ${result.order_qty}`);
    bits.push(`order ${String(result.order_id).slice(0, 8)}…`);
    if (result.stop_loss?.stop_price != null) {
      bits.push(`stop $${Number(result.stop_loss.stop_price).toFixed(2)}`);
    }
    aiMeta.textContent = bits.join(" · ");
  } else {
    aiMeta.hidden = true;
  }
}

function clearDeskWatchlist() {
  lastDeskWatchlist = [];
  applyAiWatchlist([]);
  if (typeof syncFeaturedWall === "function") syncFeaturedWall();
}

function watchlistIsLive() {
  return loopRunning && !suppressWatchlistUntilStop;
}

function applyAiWatchlist(results) {
  const box = $("ai-watchlist");
  if (!box) return;
  if (!watchlistIsLive() || !results || !results.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  const isAi = results.some((r) => r.confidence != null || r.provider);
  const isDip = !isAi && results.some((r) => r.engine === "dip" || r.rsi != null);
  const isPair = !isAi && results.some((r) => r.engine === "pair");
  const isLs = !isAi && results.some((r) => r.engine === "ls");
  const metricLabel = isAi
    ? "Conf"
    : isDip
      ? "RSI / %b"
      : isPair
        ? "Lookback / SMA"
        : isLs
          ? "EMA"
          : "Fast / Slow";
  const featured = String(formValue("symbol", "") || "").trim().toUpperCase();
  box.hidden = false;
  box.innerHTML =
    `<div class="watch-head" role="row">` +
    `<span role="columnheader">${escapeHtml(tx("symbol", "Symbol"))}</span><span role="columnheader">${escapeHtml(tx("signal", "Signal"))}</span><span role="columnheader">${escapeHtml(metricLabel)}</span><span role="columnheader">${escapeHtml(tx("note", "Note"))}</span>` +
    `</div>` +
    results
      .map((r) => {
        const signal = String(r.signal || "hold").toLowerCase();
        const sig = signal.toUpperCase();
        const sym = String(r.symbol || "?").toUpperCase();
        const isFeat = !!featured && sym === featured;
        let meta = "—";
        if (r.confidence != null) {
          meta = Number(r.confidence).toFixed(2);
        } else if (r.engine === "dip" || r.rsi != null) {
          const rsi = r.rsi ?? r.fast_sma;
          const pct =
            r.bb_pct_b != null ? Number(r.bb_pct_b) * 100 : r.slow_sma;
          meta = `${Number(rsi || 0).toFixed(0)} / ${Number(pct || 0).toFixed(0)}`;
        } else if (r.fast_sma != null && r.slow_sma != null) {
          meta = `${Number(r.fast_sma).toFixed(1)} / ${Number(r.slow_sma).toFixed(1)}`;
        }
        const note = noteFor(r) || "—";
        // Price is filled by renderWatchPrices from the live quote poll — the
        // row's own price is only the mark captured when the engine last ran.
        return (
          `<div class="watch-row is-clickable${isFeat ? " is-featured" : ""}" role="button" tabindex="0" data-signal="${escapeHtml(signal)}" data-symbol="${escapeHtml(sym)}" data-signal-price="${escapeHtml(String(r.price ?? ""))}" aria-label="${escapeHtml(tx("feature_symbol", `Feature ${sym}`, { symbol: sym }))}" title="${escapeHtml(tx("feature_symbol_title", `Click to feature ${sym}`, { symbol: sym }))}"${isFeat ? ' aria-current="true"' : ""}>` +
          `<div class="watch-sym" role="cell">` +
          `<strong>${escapeHtml(sym)}</strong>` +
          `<span class="watch-px"></span>` +
          `</div>` +
          `<span class="sig ${escapeHtml(signal)}" role="cell">${escapeHtml(sig)}</span>` +
          `<span class="watch-meta" role="cell">${escapeHtml(meta)}</span>` +
          `<span class="watch-note" role="cell" title="${escapeHtml(note)}">${escapeHtml(note)}</span>` +
          `</div>`
        );
      })
      .join("");

  renderWatchPrices();
  refreshWatchQuotes();
}

/** Paint the live mark onto every watchlist row, with its drift since the signal.
 *
 *  Done in place rather than by re-rendering the list so the poll never steals
 *  focus from a row the reader is about to click. */
function renderWatchPrices() {
  const box = $("ai-watchlist");
  if (!box || box.hidden) return;
  box.querySelectorAll(".watch-row[data-symbol]").forEach((row) => {
    const el = row.querySelector(".watch-px");
    if (!el) return;
    const sym = row.dataset.symbol;
    const live = liveWatchQuotes[sym];
    // An absent price is an empty attribute, and Number("") is 0 — parse it as
    // NaN instead so a missing mark never renders as $0.00.
    const rawSignalPx = row.dataset.signalPrice;
    const signalPx = rawSignalPx ? Number(rawSignalPx) : NaN;
    const mark = live?.price ?? (Number.isFinite(signalPx) ? signalPx : null);
    if (mark == null || !Number.isFinite(Number(mark))) {
      el.textContent = "";
      el.removeAttribute("title");
      el.classList.remove("is-up", "is-down", "is-live");
      return;
    }

    let text = `$${Number(mark).toFixed(2)}`;
    let direction = "";
    if (live?.price != null && Number.isFinite(signalPx) && signalPx > 0) {
      const deltaPct = ((Number(live.price) - signalPx) / signalPx) * 100;
      if (Math.abs(deltaPct) >= 0.01) {
        text += ` ${deltaPct > 0 ? "+" : ""}${deltaPct.toFixed(2)}%`;
        direction = deltaPct > 0 ? "is-up" : "is-down";
      }
    }
    el.textContent = text;
    el.classList.toggle("is-live", !!live);
    el.classList.toggle("is-up", direction === "is-up");
    el.classList.toggle("is-down", direction === "is-down");
    el.title = live
      ? tx("watch_px_live", "Live mark — signal was at ${price}", {
          price: Number.isFinite(signalPx) ? `$${signalPx.toFixed(2)}` : "—",
        })
      : tx("watch_px_stale", "Price when the signal was taken");
  });
}

/** Pull live marks for the visible watchlist, throttled to the server cache. */
async function refreshWatchQuotes({ force = false } = {}) {
  const box = $("ai-watchlist");
  if (!box || box.hidden) return;
  if (watchQuotesInFlight) return;
  if (!force && Date.now() - watchQuotesFetchedAt < WATCH_QUOTE_INTERVAL_MS) return;

  const symbols = [...box.querySelectorAll(".watch-row[data-symbol]")]
    .map((row) => row.dataset.symbol)
    .filter(Boolean);
  if (!symbols.length) return;

  watchQuotesInFlight = true;
  try {
    const data = await api(`/api/quotes?symbols=${encodeURIComponent(symbols.join(","))}`);
    liveWatchQuotes = data.quotes || {};
    watchQuotesFetchedAt = Date.now();
    renderWatchPrices();
    syncFeaturedWall();
  } catch {
    // A missed poll just leaves the last mark on screen — never toast for it.
  } finally {
    watchQuotesInFlight = false;
  }
}

function applyLoop(running, meta = {}) {
  loopRunning = !!running;
  if (meta.stopping != null) loopStopping = !!meta.stopping;
  if (!loopRunning) loopStopping = false;

  if (meta.startedAtMs != null && Number.isFinite(Number(meta.startedAtMs))) {
    loopStartedAtMs = Number(meta.startedAtMs);
  } else if (meta.startedAt) {
    const parsed = Date.parse(meta.startedAt);
    loopStartedAtMs = Number.isFinite(parsed) ? parsed : null;
  } else if (
    loopRunning &&
    loopStartedAtMs == null &&
    typeof meta.elapsedSec === "number"
  ) {
    loopStartedAtMs = Date.now() - Math.max(0, meta.elapsedSec) * 1000;
  } else if (loopRunning && loopStartedAtMs == null) {
    // Fallback when the server omitted start time (stale process).
    loopStartedAtMs = Date.now();
  } else if (!loopRunning) {
    loopStartedAtMs = null;
  }

  if (meta.lastDurationSec != null) {
    loopLastDurationSec = Number(meta.lastDurationSec);
  } else if (meta.lastDurationSec === null) {
    loopLastDurationSec = null;
  }

  const loopBtn = $("btn-loop");
  if (!loopBtn) {
    if (typeof syncManualUi === "function") syncManualUi();
    return;
  }
  loopBtn.disabled = busy;
  loopBtn.textContent = loopRunning
    ? (typeof window.t === "function" ? window.t("stop_loop", "Stop loop") : "Stop loop")
    : (typeof window.t === "function" ? window.t("start_loop", "Start loop") : "Start loop");
  loopBtn.classList.toggle("copper", !loopRunning);
  loopBtn.classList.toggle("danger", loopRunning);
  loopBtn.setAttribute("aria-pressed", loopRunning ? "true" : "false");
  loopBtn.title = loopRunning ? "Stop the loop" : "Start the polling loop";
  const onceBtn = $("btn-once");
  if (onceBtn) onceBtn.disabled = loopRunning || busy;
  syncStrategyHint(loopRunning ? "locked" : persistStatus === "locked" ? "ready" : persistStatus);
  const settings = $("settings");
  if (settings) {
    [...settings.elements].forEach((el) => {
      if (el.name) el.disabled = loopRunning;
    });
    const tf = $("field-timeframe");
    if (tf && !loopRunning) {
      const mode = formPayload().strategy_mode;
      tf.disabled = mode === "pair" || mode === "ls";
    }
    syncNiceSelectDisabled(settings);
  }
  // Keep size field visibility correct after unlock or while looping.
  syncSizeModeUi();
  syncRunZone();
  if (typeof syncManualUi === "function") syncManualUi();
  const loopState = $("loop-state");
  if (loopRunning) {
    const elapsed =
      loopStartedAtMs != null
        ? formatDuration((Date.now() - loopStartedAtMs) / 1000)
        : "0:00";
    if (loopState) {
      loopState.textContent = `Loop running · ${elapsed} — Stop to edit strategy again.`;
    }
  } else if (!busy && loopState) {
    loopState.textContent = idleLoopState();
  }
  syncLoopElapsedTicker();
  applyAiWatchlist(lastDeskWatchlist);
}

function render(state, { forceSettings = false } = {}) {
  if (state.ai_models) {
    populateModelOptions(state.ai_models);
  }
  if (Array.isArray(state.ai_presets) && state.ai_presets.length) {
    aiPresets = state.ai_presets;
    populatePresetOptions(aiPresets);
  }
  if (Array.isArray(state.sma_presets) && state.sma_presets.length) {
    smaPresets = state.sma_presets;
    populateSmaPresetOptions(smaPresets);
  }
  if (Array.isArray(state.dip_presets) && state.dip_presets.length) {
    dipPresets = state.dip_presets;
    populateDipPresetOptions(dipPresets);
  }
  if (Array.isArray(state.pair_presets)) {
    pairPresets = state.pair_presets;
  }
  applySettings(state.settings, { force: forceSettings });
  applyAccount(state.account);
  applyAiKeys(state.ai_ready, state.ai_key_status);
  applyAlpacaKeys(state.alpaca_key_status);
  lastDeskResult = state.last_result || null;
  lastDeskWatchlist = Array.isArray(state.last_ai_results)
    ? state.last_ai_results
    : [];
  lastDeskQuote = state.quote || null;
  // Desk-session history renders on the History page.
  if (typeof applyHistory === "function") {
    applyHistory(state.loop_history, state.result_history);
  }
  applyLoop(!!state.loop_running, {
    startedAt: state.loop_started_at,
    startedAtMs: state.loop_started_at_ms,
    elapsedSec: state.loop_elapsed_seconds,
    lastDurationSec: state.loop_last_duration_seconds,
    stopping: state.loop_stopping,
  });
  if (suppressWatchlistUntilStop && !state.loop_running) {
    suppressWatchlistUntilStop = false;
  }
  applyAiWatchlist(lastDeskWatchlist);
  syncFeaturedWall();
  if (forceSettings) {
    if (typeof syncBacktestUi === "function") syncBacktestUi();
  } else if (
    isBacktestFamily() &&
    (Array.isArray(state.sma_presets) || Array.isArray(state.dip_presets))
  ) {
    const dipHint = $("bt-dip-hint");
    const smaHint = $("bt-sma-hint");
    const form = $("backtest-form");
    if (form && dipHint && form.elements.mode?.value === "dip") {
      const preset = findDipPreset(form.elements.dip_preset?.value);
      if (preset?.summary) dipHint.textContent = preset.summary;
    }
    if (form && smaHint && form.elements.mode?.value === "sma") {
      const preset = findSmaPreset(form.elements.sma_preset?.value);
      if (preset?.summary) smaHint.textContent = preset.summary;
    }
  }
  if (formDirty || formFocused) {
    syncModeBadge(
      formPayload().strategy_mode,
      formPayload().ai_preset,
      formPayload().sma_preset,
      formPayload().dip_preset,
      formPayload().pair_preset
    );
    syncModeUi();
    syncSmaHint();
    syncDipHint();
  } else if (state.settings) {
    syncModeBadge(
      state.settings.strategy_mode,
      state.settings.ai_preset,
      state.settings.sma_preset,
      state.settings.dip_preset,
      state.settings.pair_preset
    );
  }
  const traded = notifyExecutedTrades(state);
  if (state.error && !traded) {
    showToast(state.error, "error");
  } else if (!state.error) {
    const toast = $("toast");
    if (toast?.dataset.kind === "error" && !toast.hidden) {
      showToast(null);
    }
  }
}



async function onRefresh() {
  const localError = validateLocal();
  if (localError) {
    setFormError(localError);
    showToast(localError, "error");
    return;
  }
  setFormError(null);
  try {
    setBusy(true, "Refreshing account…");
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify(formPayload()),
    });
    formDirty = false;
    const data = await api("/api/account", { method: "POST", body: "{}" });
    applyAccount(data.account);
    await refreshStatus({ forceSettings: true });
    showToast("Account refreshed.", "ok");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    setBusy(false);
    await refreshStatus();
  }
}

async function onOnce() {
  const localError = validateReadyToRun();
  if (localError) {
    setFormError(localError);
    showToast(localError, "error");
    syncStrategyHint("invalid");
    return;
  }
  setFormError(null);
  const payload = formPayload();
  try {
    setBusy(
      true,
      payload.strategy_mode === "ai"
        ? `Asking ${payload.ai_provider}…`
        : "Evaluating signal…"
    );
    const data = await api("/api/run-once", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    formDirty = false;
    persistStatus = "ready";
    render(data.state, { forceSettings: true });
    if (!lastTradeNotified) {
      showToast(
        payload.strategy_mode === "ai" ? "AI decision updated." : "Signal updated.",
        "ok"
      );
    }
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    setBusy(false);
    await refreshStatus({ forceSettings: true });
  }
}

async function waitUntilLoopStopped({ timeoutMs = 120000 } = {}) {
  const started = Date.now();
  // Poll the loop-only endpoint: /api/status refreshes the quote and rebuilds
  // presets, model catalogs and history on every hit — far too heavy here.
  while (Date.now() - started < timeoutMs) {
    const light = await api("/api/loop/state");
    if (!light.loop_running) return await api("/api/status");
    $("loop-state").textContent = tx(
      "loop_stopping",
      "Stopping… finishing the symbol in flight."
    );
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error("Timed out waiting for the loop to stop.");
}

async function onStart() {
  const localError = validateReadyToRun();
  if (localError) {
    setFormError(localError);
    showToast(localError, "error");
    syncStrategyHint("invalid");
    return;
  }
  setFormError(null);
  const payload = formPayload();
  const ok = window.confirm(
    deskEnvLabel() === "live"
      ? tx(
          "loop_live_confirm",
          "Starting the loop will place real orders on buy/sell signals. Continue?"
        )
      : tx(
          "loop_paper_confirm",
          "Starting the loop will place paper orders on buy/sell signals. Continue?"
        )
  );
  if (!ok) return;
  // Cancel any pending auto-save before the form locks; a delayed persist
  // used to fire with FormData missing disabled fields and reset mode to SMA.
  clearTimeout(persistTimer);
  try {
    setBusy(true, "Starting loop…");
    const data = await api("/api/loop/start", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    formDirty = false;
    render(data.state, { forceSettings: true });
    showToast(
      deskEnvLabel() === "live"
        ? "Loop started — LIVE orders enabled."
        : "Loop started — paper orders enabled.",
      "ok"
    );
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    setBusy(false);
    await refreshStatus({ forceSettings: true });
  }
}

async function onStop() {
  try {
    // Flip the badge before the round trip so Stop feels instant even while
    // the worker finishes the symbol it is on.
    loopStopping = true;
    tickLoopElapsed();
    setBusy(true, tx("loop_stopping", "Stopping… finishing the symbol in flight."));
    statusGen += 1;
    suppressWatchlistUntilStop = true;
    clearDeskWatchlist();
    const data = await api("/api/loop/stop", { method: "POST", body: "{}" });
    render(data.state, { forceSettings: true });
    // Keep busy until the worker exits — stop only interrupts between cycles /
    // during the poll sleep, so loop_running can stay true briefly.
    if (data.state?.loop_running) {
      const state = await waitUntilLoopStopped();
      render(state, { forceSettings: true });
    }
    showToast("Loop stopped.", "ok");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    setBusy(false);
    await refreshStatus({ forceSettings: true });
  }
}

async function onLoopToggle() {
  if (busy) return;
  if (loopRunning) {
    await onStop();
  } else {
    await onStart();
  }
}

async function onClearHistory() {
  if (!resultHistory.length) {
    showToast(tx("desk_history_empty_toast", "Desk history is already empty."), "ok");
    return;
  }
  const ok = window.confirm(
    tx(
      "clear_desk_confirm",
      "Clear all desk session history? This cannot be undone.\n\nAlpaca fills are unchanged."
    )
  );
  if (!ok) return;
  try {
    setBusy(true, tx("clearing_history", "Clearing history…"));
    const data = await api("/api/history/clear", { method: "POST", body: "{}" });
    render(data.state);
    showToast(tx("desk_history_cleared", "Desk history cleared."), "ok");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    setBusy(false);
    await refreshStatus();
  }
}

const form = $("settings");
let persistTimer = null;

function schedulePersistSettings() {
  clearTimeout(persistTimer);
  clearTimeout(hintResetTimer);
  if (loopRunning) return;
  const err = validateLocal();
  if (err) {
    syncStrategyHint("invalid");
    return;
  }
  syncStrategyHint("editing");
  persistTimer = setTimeout(async () => {
    if (loopRunning) return;
    const err = validateLocal();
    if (err) {
      syncStrategyHint("invalid");
      return;
    }
    // Don't ship password fields on auto-save; keys use Save API key.
    const payload = formPayload();
    payload.openai_api_key = "";
    payload.gemini_api_key = "";
    payload.save_keys_to_env = false;
    syncStrategyHint("saving");
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      formDirty = false;
      syncStrategyHint("saved");
      hintResetTimer = setTimeout(() => {
        if (!formDirty && !loopRunning) syncStrategyHint("ready");
      }, 1600);
    } catch (_) {
      // Keep formDirty so a later run/save can retry.
      syncStrategyHint("editing");
    }
  }, 450);
}

if (form) {
  form.addEventListener("input", (ev) => {
    formDirty = true;
    const name = ev.target?.name;
    markPresetCustomIfEdited(ev);
    maybeSyncWatchlistFromPrimary(ev);
    syncModeBadge(
      form.elements.strategy_mode.value,
      form.elements.ai_preset?.value,
      form.elements.sma_preset?.value,
      form.elements.dip_preset?.value,
      form.elements.pair_preset?.value
    );
    syncModeUi();
    syncSmaHint();
    syncDipHint();
    if (name === "symbol") {
      applyAiWatchlist(lastDeskWatchlist);
      syncFeaturedWall();
    }
    setFormError(null);
    if (name === "openai_api_key" || name === "gemini_api_key") {
      syncStrategyHint("editing");
      return;
    }
    schedulePersistSettings();
  });
  form.addEventListener("change", (ev) => {
    formDirty = true;
    const name = ev.target?.name;
    if (name === "ai_preset") {
      applyAiPreset(ev.target.value, { forceInstructions: true });
    }
    if (name === "sma_preset") {
      applySmaPreset(ev.target.value);
    }
    if (name === "dip_preset") {
      applyDipPreset(ev.target.value);
    }
    if (name === "pair_preset") {
      applyPairPreset(ev.target.value);
    }
    if (name === "strategy_mode" && (ev.target.value === "pair" || ev.target.value === "ls")) {
      if (form.elements.bar_timeframe) form.elements.bar_timeframe.value = "1Day";
    }
    if (name === "symbol") {
      maybeSyncWatchlistFromPrimary(ev);
      applyAiWatchlist(lastDeskWatchlist);
      syncFeaturedWall();
    }
    syncModeBadge(
      form.elements.strategy_mode.value,
      form.elements.ai_preset?.value,
      form.elements.sma_preset?.value,
      form.elements.dip_preset?.value,
      form.elements.pair_preset?.value
    );
    syncModeUi();
    syncSmaHint();
    syncDipHint();
    if (name === "openai_api_key" || name === "gemini_api_key" || name === "save_keys_to_env") {
      return;
    }
    schedulePersistSettings();
  });
  form.addEventListener("focusin", () => {
    formFocused = true;
  });
  form.querySelectorAll(".size-toggle-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      selectSizeMode(btn.dataset.sizeMode || "qty");
    });
  });
  form.addEventListener("focusout", () => {
    setTimeout(() => {
      formFocused = form.contains(document.activeElement);
    }, 0);
  });
}

$("btn-refresh")?.addEventListener("click", onRefresh);
$("btn-once")?.addEventListener("click", onOnce);
$("btn-loop")?.addEventListener("click", onLoopToggle);
$("btn-include-featured")?.addEventListener("click", includeFeaturedInWatchlist);
$("watch-chips")?.addEventListener("click", (ev) => {
  const chip = ev.target.closest(".watch-chip[data-symbol]");
  if (chip) setFeaturedSymbol(chip.dataset.symbol);
});
$("ai-watchlist")?.addEventListener("click", (ev) => {
  const row = ev.target.closest(".watch-row[data-symbol]");
  if (row) setFeaturedSymbol(row.dataset.symbol);
});
$("ai-watchlist")?.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" || ev.key === " ") {
    const row = ev.target.closest(".watch-row[data-symbol]");
    if (row) {
      ev.preventDefault();
      setFeaturedSymbol(row.dataset.symbol);
    }
  }
});
$("btn-clear-history")?.addEventListener("click", onClearHistory);

// Auto-trade event listeners and initialization
document.addEventListener("DOMContentLoaded", () => {
  syncModeUi();
  refreshStatus({ forceSettings: true }).catch((err) => showToast(err.message, "error"));
});
if (document.readyState === "interactive" || document.readyState === "complete") {
  syncModeUi();
  refreshStatus({ forceSettings: true }).catch((err) => showToast(err.message, "error"));
}

function onDeskStatusUpdate(state, { forceSettings } = {}) {
  render(state, { forceSettings });
}

/** Runs on the shared 2s desk tick; the quote fetch throttles itself. */
function onDeskStatusInterval() {
  refreshWatchQuotes();
}

function onDeskLanguageChange() {
  syncEngineSide();
  syncModeHint();
  syncPresetHint();
  syncSmaPresetHint();
  syncDipPresetHint();
  syncPairPresetHint();
  syncSizeModeUi();

  if (Array.isArray(aiPresets) && aiPresets.length) {
    populatePresetOptions(aiPresets);
  }
  if (Array.isArray(smaPresets) && smaPresets.length) {
    populateSmaPresetOptions(smaPresets);
  }
  if (Array.isArray(dipPresets) && dipPresets.length) {
    populateDipPresetOptions(dipPresets);
  }

  const loopBtn = $("btn-loop");
  if (loopBtn) {
    loopBtn.textContent = loopRunning
      ? tx("stop_loop", "Stop loop")
      : tx("start_loop", "Start loop");
  }
  if (Array.isArray(lastDeskWatchlist)) {
    applyAiWatchlist(lastDeskWatchlist);
  }
  syncFeaturedWall();
}
