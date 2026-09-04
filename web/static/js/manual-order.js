/**
 * Advanced Order Page JavaScript for AlgoPaca
 * Order form validation, symbol quote/position context, cost estimation, and order submission.
 *
 * Sizing has two layers on purpose. `calculateSizeEstimate` is a local mirror
 * of the server risk engine, fast enough to answer every keystroke; a debounced
 * server preview (`refreshServerPreview`) then overwrites it with the real
 * numbers. The mirror alone used to be the whole preview, which meant any
 * change to the server's rounding or clamping silently made the page lie —
 * whatever the server last said is what the page shows.
 */

let formDirtyManual = false;
let manualContext = null;
let manualContextError = null;
let manualContextTimer = null;
let lastManualTicket = null;
let manualBusyLabel = null;
let manualLastEstimate = null;
let manualModalReturnFocus = null;
let manualContextFetchedAt = 0;
/** Bumped on every `refreshManualContext()` call so a slow response for a
 *  symbol the user has since typed past cannot overwrite newer context. */
let manualContextRequestId = 0;
/** { key, result } — the last server preview, and the form it described. */
let manualServerPreview = null;
let manualPreviewTimer = null;
let manualPreviewInFlight = false;
let manualPreviewPendingRerun = false;
/** Breaches attached to the ticket the confirm modal is currently showing. */
let manualPendingBreaches = [];
/** Keep one broker id across an uncertain retry; replace it when terms change. */
let manualPendingTicket = null;
/** Last share count we wrote into the exit qty box (so a later position
 *  refresh can keep All/Half in sync without stomping a number the user typed). */
let lastAutoSellQty = null;
/** "all" | "half" | null — which fill button last wrote the box. */
let lastAutoSellFill = null;
/** Restored All/Half drafts wait for position size before filling the box. */
let pendingSellFill = null;
/** True once the user types in the exit qty box, so backspacing to empty
 *  does not immediately rewrite All over the number they were about to enter. */
let sellQtyTouched = false;
/** Open positions from `/api/positions`, for the quick-symbol chips. Kept
 *  separate from `manualContext` — that is one symbol's context, this is the
 *  whole book. */
let manualOpenPositions = [];

/** True while the sell limit is pinned to the stop, so it follows the stop as
 *  the ticket is re-sized instead of going stale at the price it was filled at. */
let stopLimitPinnedToStop = false;

/** Auto-refresh cadence for the ticket context, in ms. */
const MANUAL_CONTEXT_REFRESH_MS = 15000;
/** How long to sit still before asking the server to size the ticket. */
const MANUAL_PREVIEW_DEBOUNCE_MS = 450;
/** Mirrors MIN/MAX_ATR_STOP_MULT on the desk (`bot/config.py`). A multiple
 *  below the floor prices a near-zero stop, and risk sizing divides the risk
 *  budget by that distance — so 0.01 buys ~180× the intended position. This
 *  page has no flat stop-% field to fall back on, so 0 is not offered here. */
const MIN_ATR_STOP_MULT = 0.1;
const MAX_ATR_STOP_MULT = 10;

function stockPrice(value) {
  const price = Number(value);
  if (!Number.isFinite(price)) return "—";
  const digits = Math.abs(price) < 1 ? 4 : 2;
  return `$${price.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function normalizeStockPrice(value) {
  const price = Number(value);
  if (!Number.isFinite(price)) return null;
  const scale = Math.abs(price) < 1 ? 10000 : 100;
  return Math.round((price + Number.EPSILON) * scale) / scale;
}

function manualFormValue(name, fallback = "") {
  const form = $("manual-order");
  if (!form) return fallback;
  const safe = String(name).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  const radios = form.querySelectorAll(`input[type="radio"][name="${safe}"]`);
  if (radios.length) {
    const checked = form.querySelector(
      `input[type="radio"][name="${safe}"]:checked`
    );
    return checked?.value || fallback;
  }
  const el = form.elements?.[name];
  if (!el) return fallback;
  if (el.type === "checkbox") return el.checked;
  return el.value;
}

function setManualFormValue(name, value) {
  const form = $("manual-order");
  if (!form || value == null) return;
  const safe = String(name).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  const radios = form.querySelectorAll(`input[type="radio"][name="${safe}"]`);
  if (radios.length) {
    radios.forEach((input) => {
      input.checked = String(input.value) === String(value);
    });
    return;
  }
  const el = form.elements?.[name];
  if (!el) return;
  if (el.type === "checkbox") {
    el.checked = value === true || value === "true";
    return;
  }
  el.value = String(value);
  if (el.tagName === "SELECT") refreshNiceSelect(el);
}

/** Symbol, shares, and other typed fields must stay native inputs — never a dropdown. */
function stripNiceSelectFromManualInputs(form) {
  if (!form?.querySelectorAll) return;
  form.querySelectorAll("input").forEach((el) => {
    const wrap = el.parentElement?.querySelector(":scope > .nice-select");
    if (wrap) wrap.remove();
    el.classList.remove("hidden-select", "wide");
    if (el._niceSelect) delete el._niceSelect;
  });
}

/**
 * Normalize a stored side value onto the Buy / Sell control.
 *
 * Recent tickets and saved drafts carry the desk action, so a short that came
 * back from the broker has to fold onto the button it was placed from —
 * otherwise reusing a short ticket left the side control untouched.
 */
function visibleTicketSide(side) {
  const raw = String(side || "").toLowerCase();
  if (raw === "sell" || raw === "short") return "sell";
  if (raw === "buy" || raw === "cover") return "buy";
  return "";
}

/** Which button the ticket is standing on — buy or sell. */
function manualSide() {
  const raw = String(manualFormValue("side", "buy") || "buy").toLowerCase();
  return visibleTicketSide(raw) || "buy";
}

/**
 * The action the desk is actually asked to perform.
 *
 * `place_manual_order` takes four actions, not two broker sides, because
 * "sell" is ambiguous the moment shorting exists: with shares in hand it
 * closes a long, and flat it opens a short. The form shows two buttons, so
 * the live position is what resolves them — and the confirm dialog names the
 * result before anything leaves the page.
 *
 * Buy always stays `buy`. Covering a short would have to be sized against the
 * borrow rather than by the risk engine that owns the Buy sizing block, so
 * that action is left to Positions and a Buy over a short is still refused —
 * see `err_buy_on_short`.
 */
function manualDeskAction() {
  if (manualSide() !== "sell") return "buy";
  return manualSignedPosition() > 0 ? "sell" : "short";
}

/** Is this Sell opening a short rather than closing a long? */
function manualOpensShort() {
  return manualDeskAction() === "short";
}

function manualSymbol() {
  return String(manualFormValue("symbol", "") || "").trim().toUpperCase();
}

/**
 * Which half of the form is on screen — the Buy block or the Sell block.
 *
 * Deliberately the *button*, not the desk action: a short entry stands on the
 * Sell button and sizes from the Sell quantity box, so the layout follows the
 * button while the ticket that goes out follows `manualDeskAction`.
 */
function manualIsEntry() {
  return manualSide() === "buy";
}

function manualIsExit() {
  return manualSide() === "sell";
}

/** Does this ticket open risk (buy, short) or close it (sell)? */
function manualOpensRisk() {
  return manualDeskAction() !== "sell";
}

function manualOrderType() {
  const raw = String(manualFormValue("order_type", "market") || "market").toLowerCase();
  return ["market", "limit", "stop", "stop_limit", "trailing_stop"].includes(raw)
    ? raw
    : "market";
}

function manualOrderTypeLabel(otype) {
  const key = {
    market: ["market", "Market"],
    limit: ["limit", "Limit"],
    stop: ["type_stop", "Stop"],
    stop_limit: ["type_stop_limit", "Stop limit"],
    trailing_stop: ["type_trailing_stop", "Trailing stop"],
  }[otype];
  return key ? tx(key[0], key[1]) : String(otype || "");
}

/** Alpaca only fills these in regular hours; Limit can opt into the 24h market. */
function manualOrderTypeIsRthOnly(otype = manualOrderType()) {
  return otype !== "limit";
}

function manualTicketQueuesForRth() {
  return Boolean(manualContext && manualContext.is_open === false) && !manualExtendedHours();
}

function manualTimeInForce() {
  const raw = String(manualFormValue("time_in_force", "day") || "day").toLowerCase();
  return ["day", "gtc", "ioc", "fok", "opg", "cls"].includes(raw) ? raw : "day";
}

/** Order types that carry a limit price field. */
function manualNeedsLimit() {
  return ["limit", "stop_limit"].includes(manualOrderType());
}

/** Order types that carry a stop trigger field. */
function manualNeedsTrigger() {
  return ["stop", "stop_limit"].includes(manualOrderType());
}

function manualNeedsTrail() {
  return manualOrderType() === "trailing_stop";
}

function manualTradingSession() {
  const raw = String(manualFormValue("trading_session", "regular") || "regular").toLowerCase();
  return raw === "24h" ? "24h" : "regular";
}

function manualExtendedHours() {
  return manualTradingSession() === "24h";
}

function manualTrailPercent() {
  const raw = Number(manualFormValue("trail_percent", ""));
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

function manualTriggerPrice() {
  const raw = Number(manualFormValue("stop_price", ""));
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

/** How a buy is sized: risk budget, a cash amount, or an exact share count. */
function manualBuySizeMode() {
  const raw = String(manualFormValue("buy_size_mode", "risk") || "risk").toLowerCase();
  return ["risk", "notional", "qty"].includes(raw) ? raw : "risk";
}

function manualTakeProfitR() {
  const raw = Number(manualFormValue("take_profit_r", 0));
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

/** Cushion past the stop for a stop-limit exit. 0 = sell at market (unless absolute limit). */
function manualStopLimitOffsetPct() {
  const raw = Number(manualFormValue("stop_limit_offset_pct", 0));
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

/** Absolute sell limit after the stop. Empty = use cushion or market. */
function manualStopLimitPrice() {
  const raw = Number(manualFormValue("stop_limit_price", ""));
  return Number.isFinite(raw) && raw > 0 ? raw : null;
}

/**
 * Hold a pinned sell limit on the stop, and show the button's state.
 *
 * "At stop" used to fill the box once and let go. Nothing said so, and nothing
 * moved afterwards — so any later edit to the ATR multiple, the symbol, or the
 * size slid the stop out from under a number that still looked authoritative,
 * and the ticket only complained at Preview. Pinned, the box tracks the stop;
 * typing in it releases the pin.
 *
 * Returns true when the value actually moved, so the caller can re-price.
 */
function syncStopLimitPin(calc) {
  const btn = $("btn-stop-limit-at-stop");
  if (btn) {
    btn.classList.toggle("is-active", stopLimitPinnedToStop);
    btn.setAttribute("aria-pressed", stopLimitPinnedToStop ? "true" : "false");
  }
  // Exits hide the whole bracket group, so the pin goes dormant there rather
  // than writing into a field nobody can see. Switching back wakes it up.
  if (!stopLimitPinnedToStop || !manualIsEntry()) return false;
  const stop = normalizeStockPrice(Number(calc?.stopPrice));
  if (!(stop > 0)) return false;
  const field = $("manual-order")?.elements?.stop_limit_price;
  // Written straight onto the element: `setManualFormValue` fires no `input`
  // event either, but going through the field here keeps the no-op check exact.
  if (!field || Number(field.value) === stop) return false;
  field.value = String(stop);
  return true;
}

/**
 * Limit fill after the stop triggers. Mirrors desk `limit_price_for_stop` /
 * `normalize_stop_exit_limit` — long may sit at the stop, never above it.
 */
function stopLimitFromStop(stopPrice, offsetPct, absoluteLimit) {
  const stop = Number(stopPrice);
  if (!(stop > 0)) return null;
  if (absoluteLimit != null && Number(absoluteLimit) > 0) {
    let limit = normalizeStockPrice(Number(absoluteLimit));
    if (limit > stop) return null; // invalid — sell above stop
    return limit > 0 ? limit : null;
  }
  const offset = Number(offsetPct);
  if (!(offset > 0)) return null;
  const raw = stop * (1 - offset / 100);
  if (!(raw > 0)) return null;
  let limit = normalizeStockPrice(raw);
  if (limit > stop) limit = normalizeStockPrice(stop);
  return limit > 0 ? limit : null;
}

function manualSellMode() {
  const raw = String(manualFormValue("sell_mode", "custom") || "custom").toLowerCase();
  return raw === "dollars" ? "dollars" : "custom";
}

function formatSellQtyValue(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return "";
  return String(Math.round(v * 10000) / 10000);
}

/** Price this exit is expected to fill at — limit/trigger if set, else the mark. */
function manualExitFillPrice() {
  const mark = Number(manualContext?.quote?.price);
  const limit = Number(manualFormValue("limit_price", ""));
  const trigger = manualTriggerPrice();
  if (manualNeedsLimit() && limit > 0) return limit;
  if (trigger > 0) return trigger;
  return Number.isFinite(mark) && mark > 0 ? mark : 0;
}

/** Signed position: >0 long, <0 short, 0 flat. */
function manualSignedPosition() {
  const qty = Number(manualContext?.position);
  return Number.isFinite(qty) ? qty : 0;
}

/** Shares a Sell has to work with — a short position has none to sell here. */
function manualPositionQty() {
  const qty = manualSignedPosition();
  return qty > 0 ? qty : 0;
}

function manualSellQty() {
  const mode = manualSellMode();
  if (mode === "dollars") {
    const dollars = Number(manualFormValue("sell_notional", ""));
    const px = manualExitFillPrice();
    if (!(dollars > 0) || !(px > 0)) return 0;
    return dollars / px;
  }
  const raw = Number(manualFormValue("sell_qty", ""));
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

/** Shares All/Half should write. Non-fractionable names cannot close half a share. */
function sellFillShares(which) {
  const held = manualPositionQty();
  if (!(held > 0)) return 0;
  if (which !== "half") return held;
  if (manualContext?.asset?.fractionable === false) return Math.floor(held / 2);
  return held / 2;
}

/**
 * Keep All/Half fills tracking the live position until the user types
 * something else — an empty box on Sell is almost never what they meant.
 */
function maybeAutofillSellQty() {
  if (!manualIsExit()) return;
  const held = manualPositionQty();
  if (!(held > 0)) return;

  if (pendingSellFill) {
    const result = applySellFill(pendingSellFill);
    if (result !== false) pendingSellFill = null;
    return;
  }

  const mode = manualSellMode();
  const px = manualExitFillPrice();
  const fraction = lastAutoSellFill;

  if (mode === "dollars") {
    const input = $("manual-sell-notional");
    if (!input || !(px > 0)) return;
    const currentRaw = String(input.value || "").trim();
    const intent = fraction || (currentRaw === "" && !sellQtyTouched ? "all" : null);
    if (!intent) return;
    const expectedShares = sellFillShares(intent);
    if (!(expectedShares > 0)) return;
    const expectedDollars = Math.round(expectedShares * px * 100) / 100;
    const current = Number(input.value);
    const lastWritten =
      lastAutoSellQty != null ? Math.round(lastAutoSellQty * px * 100) / 100 : null;
    const stillAuto =
      currentRaw === "" ||
      (lastWritten != null && Math.abs(current - lastWritten) < 0.011);
    if (!stillAuto) return;
    if (Math.abs(current - expectedDollars) > 0.011) {
      input.value = expectedDollars.toFixed(2);
    }
    lastAutoSellFill = intent;
    lastAutoSellQty = expectedShares;
    return;
  }

  const qtyInput = $("manual-sell-qty");
  if (!qtyInput) return;
  const currentRaw = String(qtyInput.value || "").trim();
  const current = Number(currentRaw);
  const matchesLastAuto =
    lastAutoSellQty != null &&
    Number.isFinite(current) &&
    Math.abs(current - lastAutoSellQty) < 1e-9;
  const intent = fraction || (currentRaw === "" && !sellQtyTouched ? "all" : null);
  if (!intent) return;
  if (fraction && !matchesLastAuto && currentRaw !== "") return;
  const fill = sellFillShares(intent);
  if (!(fill > 0)) return;
  const formatted = formatSellQtyValue(fill);
  if (qtyInput.value !== formatted) qtyInput.value = formatted;
  lastAutoSellFill = intent;
  lastAutoSellQty = fill;
}

function applySellFill(which) {
  const shares = sellFillShares(which);
  if (!(shares > 0)) return false;
  lastAutoSellFill = which === "half" ? "half" : "all";
  lastAutoSellQty = shares;
  pendingSellFill = null;
  if (manualSellMode() === "dollars") {
    const px = manualExitFillPrice();
    if (!(px > 0)) return "no_price";
    setManualFormValue("sell_notional", (Math.round(shares * px * 100) / 100).toFixed(2));
  } else {
    setManualFormValue("sell_qty", formatSellQtyValue(shares));
  }
  return true;
}

function fillSellQty(which) {
  const result = applySellFill(which);
  if (result === false) {
    showToast(
      tx("manual_sell_flat", "No long position in this symbol — nothing to sell."),
      "error"
    );
    return;
  }
  formDirtyManual = true;
  setManualError(null);
  if (result === "no_price") {
    showToast(
      tx("err_sell_needs_mark", "Need a mark to size this exit in dollars."),
      "error"
    );
    saveManualFormDraft();
    syncManualUi();
    return;
  }
  validateManualField(manualSellMode() === "dollars" ? "sell_notional" : "sell_qty");
  saveManualFormDraft();
  syncManualUi();
  scheduleServerPreview();
}

function syncSellFillButtons() {
  const held = manualPositionQty();
  const qty = manualSellQty();
  const near = (a, b) => b > 0 && Math.abs(a - b) <= Math.max(1e-6, b * 1e-6);
  let isAll = lastAutoSellFill === "all";
  let isHalf = lastAutoSellFill === "half";
  if (!isAll && !isHalf && held > 0 && qty > 0) {
    if (near(qty, held)) isAll = true;
    else if (near(qty, held / 2) || near(qty, Math.floor(held / 2))) isHalf = true;
  }
  const locked = loopRunning || busy || !(held > 0);
  const halfShares = sellFillShares("half");

  const fillGroups = $("manual-sell-group")?.querySelectorAll(".qty-fill-group");
  fillGroups?.forEach((fg) => {
    fg.hidden = !(held > 0);
  });

  $("manual-sell-group")?.querySelectorAll("[data-sell-fill]").forEach((btn) => {
    const on = btn.dataset.sellFill === "half" ? isHalf : isAll;
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.disabled = locked || (btn.dataset.sellFill === "half" && !(halfShares > 0));
  });
}

function syncSellUnitToggle() {
  const form = $("manual-order");
  const dollarRadio = form?.querySelector('input[name="sell_mode"][value="dollars"]');
  const shareRadio = form?.querySelector('input[name="sell_mode"][value="custom"]');
  const locked = loopRunning || busy;
  const canDollars = manualExitFillPrice() > 0;
  if (shareRadio) shareRadio.disabled = locked;
  if (!dollarRadio) return;
  const standingOnDollars = dollarRadio.checked;
  dollarRadio.disabled = locked || (!canDollars && !standingOnDollars);
  const unit = dollarRadio.closest(".qty-unit");
  unit?.classList.toggle("is-disabled", !canDollars && !standingOnDollars);
  if (!canDollars && !standingOnDollars) {
    unit?.setAttribute(
      "data-tooltip",
      tx("err_sell_needs_mark", "Need a mark to size this exit in dollars.")
    );
  } else {
    unit?.setAttribute("data-tooltip", tx("dollars_title", "Dollars"));
  }
}

/** Sync quote fill pills (Bid, Mid, Mark, Ask, At stop, BP %) conditionally */
function syncQuoteFillButtons() {
  const quote = manualContext?.quote || {};
  const hasBid = Number(quote.bid) > 0;
  const hasAsk = Number(quote.ask) > 0;
  const hasPrice = Number(quote.price) > 0;
  const hasMark = hasPrice || hasBid || hasAsk;
  const hasMid = (hasBid && hasAsk) || hasMark;
  const locked = loopRunning || busy;

  const setBtn = (id, available) => {
    const btn = $(id);
    if (btn) {
      btn.disabled = !available || locked;
      btn.classList.toggle("is-disabled", !available || locked);
    }
  };

  // Limit price quote helpers
  setBtn("btn-limit-bid", hasBid);
  setBtn("btn-limit-mid", hasMid);
  setBtn("btn-limit-mark", hasMark);
  setBtn("btn-limit-ask", hasAsk);

  // Stop trigger price quote helpers
  setBtn("btn-stop-bid", hasBid);
  setBtn("btn-stop-mid", hasMid);
  setBtn("btn-stop-mark", hasMark);
  setBtn("btn-stop-ask", hasAsk);

  // Re-invest buy-back quote helpers
  setBtn("btn-reinvest-bid", hasBid);
  setBtn("btn-reinvest-mid", hasMid);
  setBtn("btn-reinvest-mark", hasMark);
  setBtn("btn-reinvest-ask", hasAsk);

  // Follow-on target limit quote helpers
  setBtn("btn-followon-bid", hasBid);
  setBtn("btn-followon-mid", hasMid);
  setBtn("btn-followon-mark", hasMark);
  setBtn("btn-followon-ask", hasAsk);

  // Stop-limit at-stop button
  const stopPx = Number(currentEstimate()?.stopPrice);
  const hasStop = Number.isFinite(stopPx) && stopPx > 0;
  const btnStopLimitAtStop = $("btn-stop-limit-at-stop");
  if (btnStopLimitAtStop) {
    btnStopLimitAtStop.disabled = !hasStop || locked;
    btnStopLimitAtStop.classList.toggle("is-active", stopLimitPinnedToStop && hasStop);
    btnStopLimitAtStop.setAttribute("aria-pressed", stopLimitPinnedToStop && hasStop ? "true" : "false");
  }

  // Notional BP fill buttons (25%, 50%, Max)
  const bp = Number(manualContext?.buying_power);
  const hasBp = Number.isFinite(bp) && bp > 0;
  document.querySelectorAll("[data-notional-fill]").forEach((btn) => {
    btn.disabled = !hasBp || locked;
  });
}

/** Switching # ↔ $ keeps the same economic size when a mark is known. */
function convertSellQtyOnUnitToggle(nextMode) {
  const px = manualExitFillPrice();
  if (!(px > 0)) return;
  if (nextMode === "dollars") {
    const qty = Number($("manual-sell-qty")?.value);
    if (qty > 0) {
      setManualFormValue("sell_notional", (Math.round(qty * px * 100) / 100).toFixed(2));
    }
  } else if (nextMode === "custom") {
    const dollars = Number($("manual-sell-notional")?.value);
    if (dollars > 0) {
      setManualFormValue("sell_qty", formatSellQtyValue(dollars / px));
      if (!lastAutoSellFill) lastAutoSellQty = null;
    }
  }
}

/** Is a buy-back attached to this sell? Only a long sell can carry one. */
function manualReinvestEnabled() {
  return (
    manualSide() === "sell" &&
    // A short opens a position; there is no sale to buy back.
    !manualOpensShort() &&
    manualFormValue("reinvest_enabled", false) === true
  );
}

function manualReinvestQtyMode() {
  const raw = String(manualFormValue("reinvest_qty_mode", "match") || "match").toLowerCase();
  return raw === "custom" ? "custom" : "match";
}

/**
 * Shares the buy-back would send. "Match" is an estimate until the sell
 * reports its fill — the server re-reads the filled qty before it buys.
 */
function manualReinvestQty() {
  if (manualReinvestQtyMode() === "match") return manualSellQty();
  const raw = Number(manualFormValue("reinvest_qty", ""));
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

function manualReinvestLimit() {
  const raw = Number(manualFormValue("reinvest_limit_price", ""));
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

function manualReinvestExpireMinutes() {
  const raw = Number(manualFormValue("reinvest_expire_minutes", 120));
  if (!Number.isFinite(raw) || raw <= 0) return 120;
  return Math.min(1440, raw);
}

/** The price the sell is expected to leave at — a limit, else the mark. */
function manualSellReference() {
  const limit = Number(manualFormValue("limit_price", ""));
  if (manualOrderType() === "limit" && limit > 0) return limit;
  const mark = Number(manualContext?.quote?.price);
  return mark > 0 ? mark : 0;
}

function manualReinvestPayload() {
  if (!manualReinvestEnabled()) return null;
  const payload = {
    enabled: true,
    qty_mode: manualReinvestQtyMode(),
    limit_price: manualReinvestLimit(),
    expire_minutes: manualReinvestExpireMinutes(),
    // Inherit the main ticket's trading session so a 24h-market parent
    // produces a 24h-market buy-back.
    time_in_force: manualTimeInForce(),
    extended_hours: manualExtendedHours(),
  };
  if (payload.qty_mode === "custom") payload.qty = manualReinvestQty();
  return payload;
}

function manualFollowOnEnabled() {
  return (
    manualIsExit() &&
    // A next ticket fires off a close; a short entry never closes anything.
    !manualOpensShort() &&
    manualFormValue("followon_enabled", false) === true
  );
}

function manualFollowOnKind() {
  const raw = String(manualFormValue("followon_kind", "reverse") || "reverse").toLowerCase();
  return raw === "rotate" ? "rotate" : "reverse";
}

function manualFollowOnQtyMode() {
  const raw = String(manualFormValue("followon_qty_mode", "match") || "match").toLowerCase();
  return raw === "custom" ? "custom" : "match";
}

function manualFollowOnOrderType() {
  const raw = String(manualFormValue("followon_order_type", "limit") || "limit").toLowerCase();
  return raw === "market" ? "market" : "limit";
}

function followonIsMarket(plan) {
  if (plan?.market === true) return true;
  return String(plan?.order_type || plan?.ticket_type || "limit").toLowerCase() === "market";
}

function followonPriceLabel(plan) {
  return followonIsMarket(plan) ? tx("market", "Market") : stockPrice(plan?.limit_price);
}

function manualFollowOnQty() {
  if (manualFollowOnQtyMode() === "match") return manualSellQty();
  const raw = Number(manualFormValue("followon_qty", ""));
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

function manualFollowOnLimit() {
  const raw = Number(manualFormValue("followon_limit_price", ""));
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

function manualFollowOnTargetSymbol() {
  const closeSymbol = String(manualFormValue("symbol", "") || "")
    .trim()
    .toUpperCase();
  if (manualFollowOnKind() !== "rotate") return closeSymbol;
  const raw = String(manualFormValue("followon_target_symbol", "") || "")
    .trim()
    .toUpperCase();
  return raw;
}

function manualFollowOnPayload() {
  if (!manualFollowOnEnabled()) return null;
  const orderType = manualFollowOnOrderType();
  const payload = {
    enabled: true,
    kind: manualFollowOnKind(),
    qty_mode: manualFollowOnQtyMode(),
    order_type: orderType,
    ticket_type: orderType,
    market: orderType === "market",
    // Inherit the main ticket's trading session so "24 hour market" parent
    // produces a 24 hour market next-ticket.
    time_in_force: manualTimeInForce(),
    extended_hours: manualExtendedHours(),
  };
  if (orderType === "limit") payload.limit_price = manualFollowOnLimit();
  if (payload.kind === "rotate") payload.target_symbol = manualFollowOnTargetSymbol();
  if (payload.qty_mode === "custom") payload.qty = manualFollowOnQty();
  return payload;
}

function manualBracketEnabled() {
  const form = $("manual-order");
  const checked = form?.elements?.bracket_enabled ? form.elements.bracket_enabled.checked : true;
  return (
    manualSide() === "buy" &&
    ["market", "limit"].includes(manualOrderType()) &&
    checked === true
  );
}

/**
 * Will this entry actually carry an OTO/bracket stop?
 *
 * Mirrors `attaches_stop` in `place_manual_order`: a stop only rides along on a
 * Market or Limit parent, in regular hours, with the bracket switched on.
 */
function manualAttachesStop() {
  return (
    manualIsEntry() &&
    manualBracketEnabled() &&
    ["market", "limit"].includes(manualOrderType()) &&
    !manualExtendedHours()
  );
}

/**
 * Does the desk floor this ticket to whole shares?
 *
 * Two things force it on this form: an attached stop (Alpaca refuses fractional
 * OTO/bracket orders) and a name that is not fractionable. (The desk also floors
 * a short borrow, but the ticket only offers Buy and Sell.)
 * Flooring anywhere else made the panel promise a smaller ticket than the one
 * `place_manual_order` sends — a $1,000 buy of a $150 stock showed "6 shares ·
 * $900" while 6.6667 shares went to the broker.
 */
function manualQtyIsWholeOnly() {
  if (manualAttachesStop()) return true;
  if (manualContext?.asset?.fractionable === false) return true;
  return false;
}

function manualDipHuntEnabled() {
  return manualSide() === "buy" && manualFormValue("dip_hunt_enabled", false) === true;
}

function manualDipHuntWaitMinutes() {
  const raw = Number(manualFormValue("dip_hunt_wait_minutes", 10));
  if (!Number.isFinite(raw) || raw <= 0) return 10;
  return Math.min(1440, raw);
}

function manualDipHuntPct() {
  const raw = Number(manualFormValue("dip_hunt_pct", 5));
  if (!Number.isFinite(raw) || raw <= 0) return 5;
  return Math.min(50, raw);
}

function manualDipHuntPayload() {
  if (!manualDipHuntEnabled() || !manualBracketEnabled()) return null;
  return {
    enabled: true,
    wait_minutes: manualDipHuntWaitMinutes(),
    dip_pct: manualDipHuntPct(),
  };
}

function manualPayload() {
  const symbol = manualSymbol();
  const action = manualDeskAction();
  const orderType = manualOrderType();
  const payload = {
    symbol,
    side: action,
    order_type: orderType,
    time_in_force: manualTimeInForce(),
    extended_hours: manualExtendedHours(),
  };
  if (action === "short") {
    // A short is an entry that stands on the Sell button: it sizes from the
    // Sell quantity box, and the borrow is always whole shares.
    payload.size_mode = "qty";
    payload.qty = Math.floor(manualSellQty());
    // The Protective Bracket accordion is a Buy affordance, so a short carries
    // no stop. Spelling the zeros out matters: omit them and the desk falls
    // back to its own stop % and ATR multiple, attaching an OTO the form never
    // showed and the user never asked for.
    payload.ai_risk_pct = null;
    payload.ai_atr_stop_mult = 0;
    payload.stop_loss_pct = 0;
    payload.take_profit_r = 0;
    payload.stop_limit_offset_pct = 0;
    payload.stop_limit_price = null;
  } else if (action === "sell") {
    // An exit closes what is already there: the user picks how much. Risk
    // sizing has no meaning here — it would only get clamped anyway.
    payload.size_mode = "qty";
    payload.qty = manualSellQty();
    const reinvest = manualReinvestPayload();
    if (reinvest) payload.reinvest = reinvest;
    const followon = manualFollowOnPayload();
    if (followon) payload.followon = followon;
  } else {
    payload.size_mode = manualBuySizeMode();
    const bracketOn = manualBracketEnabled();
    if (bracketOn) {
      payload.ai_risk_pct = Number(manualFormValue("ai_risk_pct", 0.5) || 0);
      payload.ai_atr_stop_mult = Number(manualFormValue("ai_atr_stop_mult", 1.8) || 0);
      payload.take_profit_r = manualTakeProfitR();
      payload.stop_limit_offset_pct = manualStopLimitOffsetPct();
      const stopLimitPx = manualStopLimitPrice();
      if (stopLimitPx != null) payload.stop_limit_price = stopLimitPx;
      const dipHunt = manualDipHuntPayload();
      if (dipHunt) payload.dip_hunt = dipHunt;
    } else {
      payload.ai_risk_pct = null;
      payload.ai_atr_stop_mult = 0;
      payload.stop_loss_pct = 0;
      payload.take_profit_r = 0;
      payload.stop_limit_offset_pct = 0;
      payload.stop_limit_price = null;
    }
    if (payload.size_mode === "notional") {
      payload.notional = Number(manualFormValue("notional", 0) || 0);
    } else if (payload.size_mode === "qty") {
      payload.qty = Number(manualFormValue("buy_qty", 0) || 0);
    }
  }
  if (manualNeedsLimit()) {
    const limit = Number(manualFormValue("limit_price", ""));
    payload.limit_price = Number.isFinite(limit) && limit > 0 ? limit : null;
  }
  if (manualNeedsTrigger()) {
    payload.stop_price = manualTriggerPrice() || null;
  }
  if (manualNeedsTrail()) {
    payload.trail_percent = manualTrailPercent() || null;
  }
  return payload;
}

/**
 * A stable string for the ticket's sizing inputs.
 *
 * Used to decide whether the last server preview still describes what is on
 * screen. Deliberately excludes `preview` and the client ticket id, which
 * change per request without changing the numbers.
 *
 * All three attached plans belong here: the preview echoes each one back, so
 * leaving `followon` and `dip_hunt` out meant editing them left a stale
 * preview on screen describing terms that were no longer on the form.
 */
function manualPreviewKey() {
  const p = manualPayload();
  return JSON.stringify([
    p.symbol,
    p.side,
    p.order_type,
    p.time_in_force,
    p.extended_hours,
    p.size_mode,
    p.qty ?? null,
    p.notional ?? null,
    p.limit_price ?? null,
    p.stop_price ?? null,
    p.trail_percent ?? null,
    p.ai_risk_pct ?? null,
    p.ai_atr_stop_mult ?? null,
    p.take_profit_r ?? null,
    p.stop_limit_offset_pct ?? null,
    p.stop_limit_price ?? null,
    p.reinvest ?? null,
    p.followon ?? null,
    p.dip_hunt ?? null,
  ]);
}

/** A compact client id accepted by Alpaca's REST Trading API. */
function newTicketId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID().slice(0, 32);
  return `t-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function ticketIdForCurrentTerms() {
  const key = JSON.stringify(manualPayload());
  if (!manualPendingTicket || manualPendingTicket.key !== key) {
    manualPendingTicket = { key, id: newTicketId() };
  }
  return manualPendingTicket.id;
}

function validateManualLocal() {
  const p = manualPayload();
  if (!p.symbol) return tx("err_symbol_required", "Enter a stock symbol (e.g., AAPL, MSFT).");
  if (p.extended_hours && p.order_type !== "limit") {
    return tx(
      "err_type_24h",
      "The 24-hour market only accepts Limit orders. Switch to Regular hours to queue this ticket for the open."
    );
  }
  if (manualNeedsLimit() && !(p.limit_price > 0)) {
    return tx("err_limit_price", "Limit price must be greater than $0.00.");
  }
  if (manualNeedsTrigger() && !(p.stop_price > 0)) {
    return tx("err_trigger_price", "Trigger price must be greater than $0.00.");
  }
  if (manualNeedsTrail() && !(p.trail_percent > 0)) {
    return tx("err_trail_percent", "Enter a trail percentage greater than 0.");
  }
  if (p.extended_hours && !["day", "gtc"].includes(p.time_in_force)) {
    return tx(
      "err_extended_tif",
      "24-hour market orders must use Day or GTC time in force."
    );
  }
  // `qty` only carries the share count on an exit or a Shares-mode buy. Sizing
  // in Dollars leaves it unset, so this used to wave a fractional Dollars
  // ticket through and let the desk answer with a 400 at submit — after the
  // confirm dialog, and after every silent preview had swallowed the same
  // error. The estimate knows the share count in every mode; ask it.
  const sizedShares = Number(p.qty) > 0 ? Number(p.qty) : Number(currentEstimate()?.shares);
  const sizedIsFractional = sizedShares > 0 && !Number.isInteger(sizedShares);
  if (sizedIsFractional && p.time_in_force !== "day") {
    return tx(
      "err_fractional_tif",
      "Fractional stock orders must use Day time in force."
    );
  }
  if (sizedIsFractional && manualContext?.asset?.fractionable === false) {
    return tx(
      "err_not_fractionable",
      "{symbol} does not support fractional shares at Alpaca — size this ticket in whole shares.",
      { symbol: p.symbol }
    );
  }
  const bracketOn = manualBracketEnabled();
  if (manualIsEntry() && bracketOn && !["market", "limit"].includes(p.order_type)) {
    return tx(
      "err_protected_entry_type",
      "Protected entries must use Market or Limit; Alpaca cannot attach an OTO/bracket stop to this order type."
    );
  }
  if (manualIsEntry() && bracketOn && !["day", "gtc"].includes(p.time_in_force)) {
    return tx(
      "err_protected_entry_tif",
      "Protected entries must use Day or GTC time in force."
    );
  }
  const asset = manualContext?.asset;
  const opensShort =
    p.side === "short" || (p.side === "sell" && p.followon?.kind === "reverse");
  if (opensShort && asset && asset.shortable === false) {
    return tx(
      "err_not_shortable",
      "This symbol is not shortable at Alpaca — the borrow is unavailable, so the ticket would be rejected."
    );
  }
  const signed = manualSignedPosition();
  // The desk action is never `cover`, so a Buy over a short still has to be
  // refused here rather than quietly buying the borrow back at risk-engine size.
  if (manualSide() === "buy" && signed < 0) {
    return tx(
      "err_buy_on_short",
      "This symbol is held short — close it from Positions before buying."
    );
  }
  if (p.side === "short") {
    if (!(manualSellQty() > 0)) {
      return manualSellMode() === "dollars"
        ? tx("err_sell_notional", "Enter a dollar amount greater than $0.00.")
        : tx("err_short_qty", "Enter how many shares to short.");
    }
    // Alpaca never borrows a fraction of a share, in any session. The payload
    // already floors, so an under-one-share ticket would otherwise go out as 0.
    if (!(p.qty > 0)) {
      return tx(
        "err_short_fractional",
        "Alpaca does not short fractional shares — this ticket needs at least 1 whole share."
      );
    }
    const calc = currentEstimate();
    if (calc?.blocked) return calc.blockedMessage;
    return null;
  }
  if (manualIsExit()) {
    const held = manualPositionQty();
    if (held <= 0) {
      return tx("err_sell_flat", "No long position to sell in this symbol.");
    }
    if (!(p.qty > 0)) {
      return manualSellMode() === "dollars"
        ? tx("err_sell_notional", "Enter a dollar amount greater than $0.00.")
        : tx("err_sell_qty", "Enter how many shares to sell.");
    }
    if (p.qty > held + 1e-9) {
      if (manualSellMode() === "dollars") {
        const px = manualExitFillPrice();
        return tx(
          "err_sell_notional_too_much",
          "That is more than this position is worth ({value}).",
          { value: money(held * px) }
        );
      }
      return tx("err_sell_too_many", "You cannot sell more shares than you hold.");
    }
    if (p.reinvest) {
      if (!(p.reinvest.limit_price > 0)) {
        return tx(
          "err_reinvest_limit",
          "Enter the price the buy-back should pay — it must be greater than $0.00."
        );
      }
      if (p.reinvest.qty_mode === "custom" && !(p.reinvest.qty > 0)) {
        return tx("err_reinvest_qty", "Enter how many shares to buy back.");
      }
      if (!(p.reinvest.expire_minutes > 0) || p.reinvest.expire_minutes > 1440) {
        return tx(
          "err_reinvest_expire",
          "The buy-back wait must be between 1 and 1440 minutes."
        );
      }
    }
    if (p.followon) {
      if (p.reinvest) {
        return tx(
          "err_followon_with_reinvest",
          "Choose either a buy-back or a next ticket, not both."
        );
      }
      if (!followonIsMarket(p.followon) && !(p.followon.limit_price > 0)) {
        return tx(
          "err_followon_limit",
          "Enter the price the next ticket should rest at — it must be greater than $0.00."
        );
      }
      if (p.followon.qty_mode === "custom" && !(p.followon.qty > 0)) {
        return tx("err_followon_qty", "Enter how many shares the next ticket should send.");
      }
      if (p.followon.kind === "rotate") {
        const target = String(p.followon.target_symbol || "").trim().toUpperCase();
        if (!/^[A-Z.\-]{1,12}$/.test(target)) {
          return tx("err_followon_symbol", "Enter the symbol the next ticket should buy.");
        }
        if (target === p.symbol) {
          return tx(
            "err_followon_same_symbol",
            "Buy another stock needs a different symbol than the one you are closing."
          );
        }
      } else {
        const held = manualPositionQty();
        if (held > 0 && p.qty + 1e-9 < held) {
          return tx(
            "followon_partial_warn",
            "Reverse needs the whole position closed. Enter the full available quantity, or the next ticket cannot fire."
          );
        }
      }
    }
    return null;
  }
  if (p.size_mode === "risk" && !bracketOn) {
    return tx(
      "err_risk_needs_bracket",
      "Risk sizing requires a protective stop. Enable Protective Bracket or switch to Shares (#) or Dollars ($)."
    );
  }
  if (p.size_mode === "risk" && (!(p.ai_risk_pct > 0) || p.ai_risk_pct > 10)) {
    return tx("err_risk_pct", "Risk per trade must be greater than 0% and at most 10%.");
  }
  if (p.size_mode === "notional" && !(p.notional > 0)) {
    return tx("err_notional", "Enter a dollar amount greater than $0.00.");
  }
  if (p.size_mode === "qty" && !(p.qty > 0)) {
    return tx("err_buy_qty", "Enter how many shares to buy.");
  }
  if (bracketOn) {
    if (
      !(p.ai_atr_stop_mult >= MIN_ATR_STOP_MULT) ||
      p.ai_atr_stop_mult > MAX_ATR_STOP_MULT
    ) {
      return tx("err_atr_mult", "Stop = ATR × must be between 0.1 and 10.");
    }
    if (p.take_profit_r > 20) {
      return tx("err_take_profit_r", "Take profit = R × must be 20 or less.");
    }
    if (p.stop_limit_offset_pct != null && (p.stop_limit_offset_pct < 0 || p.stop_limit_offset_pct > 50)) {
      return tx(
        "err_stop_limit_offset",
        "Stop-limit cushion must be between 0% and 50%."
      );
    }
    if (p.stop_limit_price != null) {
      if (!(p.stop_limit_price > 0)) {
        return tx("err_stop_limit_price", "Sell limit must be greater than $0.00.");
      }
      // The same estimate the "At stop" button and the preview panel read —
      // validating against the local mirror while the button fills from the
      // server preview meant the two could disagree about where the stop is.
      const calc = currentEstimate();
      const stopPx = calc?.stopPrice;
      if (stopPx != null && Number.isFinite(stopPx)) {
        if (p.stop_limit_price > stopPx) {
          return tx(
            "err_stop_limit_below_stop",
            "Sell limit must be at or below the stop price."
          );
        }
      }
    }
  }
  if (p.dip_hunt) {
    if (!(p.dip_hunt.wait_minutes > 0) || p.dip_hunt.wait_minutes > 1440) {
      return tx(
        "err_dip_hunt_wait",
        "The dip-hunt wait must be between 1 and 1440 minutes."
      );
    }
    if (!(p.dip_hunt.dip_pct > 0) || p.dip_hunt.dip_pct > 50) {
      return tx(
        "err_dip_hunt_pct",
        "The further drop must be greater than 0% and at most 50%."
      );
    }
  }
  const calc = currentEstimate();
  if (calc?.blocked) return calc.blockedMessage;
  return null;
}

/**
 * The ticket's single error surface.
 *
 * It sits directly above the submit button, so mirroring it into the toast at
 * the top of the page only prints the same sentence twice — the inline copy is
 * the one the user is already looking at.
 */
function setManualError(message) {
  const el = $("manual-error");
  if (!el) return;
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = message;

  const firstInvalid = document.querySelector(
    '.manual-form input[aria-invalid="true"], .manual-form select[aria-invalid="true"]'
  );
  if (firstInvalid) {
    firstInvalid.scrollIntoView({ block: "center", behavior: "smooth" });
    try {
      firstInvalid.focus({ preventScroll: true });
    } catch {
      firstInvalid.focus();
    }
  } else {
    el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

function syncManualBusyHint() {
  const hint = $("manual-hint");
  if (!hint) return;
  hint.hidden = false;
  if (busy && manualBusyLabel) {
    hint.textContent = manualBusyLabel;
    hint.dataset.state = "saving";
    return;
  }
  if (loopRunning) {
    hint.textContent = tx("locked", "Locked");
    hint.dataset.state = "locked";
  } else {
    const env =
      lastAlpacaStatus?.trading_mode ||
      lastAccount?.trading_mode ||
      (lastAccount?.paper === false ? "live" : "paper");
    hint.textContent =
      env === "live"
        ? tx("live_real", "Live account")
        : tx("live_paper", "Paper account");
    hint.dataset.state = env === "live" ? "live" : "ready";
  }
}

/** Resting orders for the symbol, each cancellable in place. */
function renderContextOrders(orders) {
  const el = $("manual-ctx-orders");
  if (!el) return;
  const rows = Array.isArray(orders) ? orders : [];
  if (!rows.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = rows
    .map((o) => {
      const px = o.stop_price ?? o.limit_price;
      const label = o.is_stop ? tx("order_stop", "stop") : String(o.type || "order");
      const text = `${String(o.side || "").toUpperCase()} ${formatQty(o.qty)} ${label}${
        px != null ? ` @ ${stockPrice(px)}` : ""
      }`;
      const id = String(o.id || "");
      const safeId = escapeHtml(id);
      // Cancel-then-replace leaves the position naked in between, which is the
      // one thing a resting stop exists to prevent. Alpaca replaces in a
      // single call, so a price move stays a price move.
      const editable = !!id && px != null;
      const editRow = editable
        ? `<div class="manual-ctx-order-edit" hidden data-edit-row="${safeId}">
            <label class="sr-only" for="manual-edit-${safeId}">${escapeHtml(
              tx("new_price", "New price")
            )}</label>
            <input id="manual-edit-${safeId}" type="number" min="0.0001" step="0.0001"
                   inputmode="decimal" value="${escapeHtml(String(px))}"
                   data-edit-input="${safeId}" data-is-stop="${o.is_stop ? "1" : "0"}" />
            <button type="button" class="ghost small" data-save-order="${safeId}">${escapeHtml(
              tx("save", "Save")
            )}</button>
          </div>`
        : "";
      return `<li class="manual-ctx-order">
          <div class="manual-ctx-order-row">
            <span>${escapeHtml(text)}</span>
            <span class="manual-ctx-order-actions">
              ${
                editable
                  ? `<button type="button" class="ghost small" data-edit-order="${safeId}">${escapeHtml(
                      tx("modify", "Modify")
                    )}</button>`
                  : ""
              }
              <button type="button" class="ghost small" data-cancel-order="${safeId}" ${
                id ? "" : "disabled"
              }>${escapeHtml(tx("cancel", "Cancel"))}</button>
            </span>
          </div>
          ${editRow}
        </li>`;
    })
    .join("");
}

/**
 * Move a resting order's price without cancelling it.
 *
 * The list could only ever cancel, and a cancelled stop is an unprotected
 * position until the replacement lands.
 */
async function replaceRestingOrder(orderId) {
  if (!orderId || busy) return;
  const input = document.querySelector(
    `[data-edit-input="${window.CSS?.escape ? CSS.escape(orderId) : orderId}"]`
  );
  const price = Number(input?.value);
  if (!(price > 0)) {
    showToast(tx("err_stop_price", "Enter a stop price greater than $0.00."), "error");
    return;
  }
  const body = { order_id: orderId };
  if (input?.dataset?.isStop === "1") body.stop_price = price;
  else body.limit_price = price;
  try {
    setBusy(true, tx("moving_stop", "Moving stop…"));
    await api("/api/order/replace", {
      method: "POST",
      body: JSON.stringify(body),
    });
    showToast(
      tx("manual_order_moved_to", "Order moved to {price}", { price: stockPrice(price) }),
      "ok"
    );
  } catch (err) {
    setManualError(err.message);
  } finally {
    setBusy(false);
    await refreshManualContext().catch(() => {});
  }
}

/** Cancel one resting order, then re-read the context so the list is truthful. */
async function cancelRestingOrder(orderId) {
  if (!orderId || busy) return;
  try {
    setBusy(true, tx("cancelling", "Cancelling…"));
    await api("/api/order/cancel", {
      method: "POST",
      body: JSON.stringify({ order_id: orderId }),
    });
    showToast(tx("order_cancelled", "Order cancelled"), "ok");
  } catch (err) {
    setManualError(err.message);
  } finally {
    setBusy(false);
    await refreshManualContext().catch(() => {});
  }
}

/**
 * Place default sizing and pricing values based on the stock price, ATR, and account equity
 * so the ticket sizes to at least 1 whole share and prevents "less than one whole share" errors.
 */
function applyStockPriceDefaults(data) {
  if (!data) return;
  const quote = data.quote || {};
  const mark = Number(quote.price);
  if (!(mark > 0)) return;

  const equity = Number(data.equity ?? data.account?.equity ?? manualContext?.equity ?? manualContext?.account?.equity ?? 0);
  const atr = Number(data.atr ?? manualContext?.atr ?? 0);
  const fallbackStopPct = Number(data.stop_loss_pct ?? manualContext?.stop_loss_pct ?? 5);
  let atrMult = Number(manualFormValue("ai_atr_stop_mult", 1.8) || 1.8);
  if (!(atrMult >= MIN_ATR_STOP_MULT && atrMult <= MAX_ATR_STOP_MULT)) {
    atrMult = 1.8;
  }

  let stopDistance = atr > 0 ? atr * atrMult : mark * (fallbackStopPct / 100);
  if (!(stopDistance > 0)) stopDistance = mark * 0.05;

  // 1. Risk mode sizing: Ensure risk % is large enough to size to at least 1 whole share
  if (equity > 0 && stopDistance > 0) {
    const currentRisk = Number(manualFormValue("ai_risk_pct", 0.5) || 0.5);
    const potentialShares = Math.floor(
      Math.min((equity * (currentRisk / 100)) / stopDistance, equity / mark)
    );

    if (potentialShares < 1) {
      let neededRiskPct = (stopDistance / equity) * 100;

      // If needed risk exceeds the 10% maximum limit and symbol has ATR, lower multiplier
      if (neededRiskPct > 10 && atr > 0) {
        const maxAffordableStop = equity * 0.095;
        const safeMult = Math.max(
          MIN_ATR_STOP_MULT,
          Math.floor((maxAffordableStop / atr) * 10) / 10
        );
        setManualFormValue("ai_atr_stop_mult", safeMult);
        atrMult = safeMult;
        stopDistance = atr * atrMult;
        neededRiskPct = (stopDistance / equity) * 100;
      }

      // Round up to nearest 0.05% step so it's clean and guarantees >= 1 whole share
      const cleanRisk = Math.min(10, Math.max(0.5, Math.ceil(neededRiskPct * 20) / 20));
      setManualFormValue("ai_risk_pct", cleanRisk);
      const riskInput = $("manual-ai-risk-pct");
      if (riskInput) riskInput.placeholder = String(cleanRisk);
    }
  }

  // 2. Notional mode sizing: Ensure default dollar amount covers at least 1 whole share
  const currentNotional = Number(manualFormValue("notional", 0) || 0);
  const defaultNotional = Math.max(Math.ceil(mark), Math.min(1000, Math.ceil(mark * 5)));
  if (!(currentNotional >= mark)) {
    setManualFormValue("notional", defaultNotional);
  }
  const notionalInput = $("manual-notional");
  if (notionalInput) notionalInput.placeholder = String(defaultNotional);

  // 3. Qty mode sizing: Ensure at least 1 whole share
  const currentQty = Number(manualFormValue("buy_qty", 0) || 0);
  if (!(currentQty >= 1)) {
    setManualFormValue("buy_qty", 1);
  }
  const qtyInput = $("manual-buy-qty");
  if (qtyInput) qtyInput.placeholder = "1";

  // 4. Limit price: Default to live mark price if empty
  const currentLimit = Number(manualFormValue("limit_price", 0) || 0);
  if (!(currentLimit > 0)) {
    setManualFormValue("limit_price", normalizeStockPrice(mark));
  }
  const limitInput = $("manual-limit");
  if (limitInput) limitInput.placeholder = String(normalizeStockPrice(mark));

  // 5. Trigger / Stop price: Default to sensible stop below mark if empty
  const currentStop = Number(manualFormValue("stop_price", 0) || 0);
  if (!(currentStop > 0)) {
    const defaultStop = mark > stopDistance ? mark - stopDistance : mark * 0.95;
    setManualFormValue("stop_price", normalizeStockPrice(defaultStop));
    const stopInput = $("manual-stop-price");
    if (stopInput) stopInput.placeholder = String(normalizeStockPrice(defaultStop));
  }

  // 6. Trailing stop percent: Default based on volatility or 3%
  const currentTrail = Number(manualFormValue("trail_percent", 0) || 0);
  const trailInput = $("manual-trail-percent");
  const stats = data.stats || {};
  const atrPct = Number(stats.atr_pct);
  const defaultTrail = Number.isFinite(atrPct) && atrPct > 0 ? Math.min(50, Math.max(1, Math.round(atrPct * 1.5 * 10) / 10)) : 3;
  if (!(currentTrail > 0)) {
    setManualFormValue("trail_percent", defaultTrail);
  }
  if (trailInput) trailInput.placeholder = String(defaultTrail);

  // 7. Sell / Short defaults: Ensure at least 1 share
  const currentSellQty = Number(manualFormValue("sell_qty", 0) || 0);
  if (!(currentSellQty >= 1)) {
    setManualFormValue("sell_qty", 1);
  }
  const currentSellNotional = Number(manualFormValue("sell_notional", 0) || 0);
  if (!(currentSellNotional >= mark)) {
    setManualFormValue("sell_notional", Math.ceil(mark).toFixed(2));
  }

  // 8. Protective Bracket options defaults based on stock price & ATR
  const currentAtrMult = Number(manualFormValue("ai_atr_stop_mult", 0) || 0);
  if (!(currentAtrMult >= MIN_ATR_STOP_MULT && currentAtrMult <= MAX_ATR_STOP_MULT)) {
    setManualFormValue("ai_atr_stop_mult", atrMult);
  }
  const atrMultInput = $("manual-ai-atr-mult");
  if (atrMultInput) atrMultInput.placeholder = String(atrMult);

  const currentTpR = Number(manualFormValue("take_profit_r", 0) || 0);
  if (!(currentTpR > 0)) {
    setManualFormValue("take_profit_r", 2);
  }
  const tpRInput = $("manual-take-profit-r");
  if (tpRInput) tpRInput.placeholder = "2";

  const calculatedStopPx = mark > stopDistance ? mark - stopDistance : mark * 0.95;
  const currentStopLimitPx = Number(manualFormValue("stop_limit_price", 0) || 0);
  const stopLimitInput = $("manual-stop-limit-price");
  if (stopLimitInput) {
    stopLimitInput.placeholder = String(normalizeStockPrice(calculatedStopPx));
  }

  const currentStopLimitOffset = Number(manualFormValue("stop_limit_offset_pct", -1));
  if (currentStopLimitOffset < 0 || isNaN(currentStopLimitOffset)) {
    setManualFormValue("stop_limit_offset_pct", 0);
  }
  const stopLimitOffsetInput = $("manual-stop-limit-offset");
  if (stopLimitOffsetInput) stopLimitOffsetInput.placeholder = "0";

  // Clear any existing manual error
  setManualError(null);
}

function applyManualContext(data, errorMsg = null) {
  manualContext = data || null;
  manualContextFetchedAt = data ? Date.now() : 0;
  const markEl = $("manual-ctx-mark");
  const spreadEl = $("manual-ctx-spread");
  const sessionEl = $("manual-ctx-session");
  const posEl = $("manual-ctx-position");
  const atrEl = $("manual-ctx-atr");
  const bpEl = $("manual-ctx-bp");
  const metaEl = $("manual-ctx-meta");
  if (!data) {
    [markEl, spreadEl, sessionEl, posEl, atrEl, bpEl].forEach((el) => {
      if (el) el.textContent = "—";
    });
    ["manual-ctx-day", "manual-ctx-pnl", "manual-ctx-52w", "manual-ctx-rvol", "manual-ctx-earnings"].forEach(
      (id) => {
        const el = $(id);
        if (el) el.textContent = "—";
      }
    );
    if (metaEl) {
      if (errorMsg) {
        metaEl.className = "manual-ctx-meta is-error";
        if (
          errorMsg.includes("API credentials are not configured") ||
          errorMsg.includes("API Keys") ||
          errorMsg.includes("API keys")
        ) {
          metaEl.innerHTML = `<span class="ctx-err-text">⚠️ ${escapeHtml(errorMsg)}</span> <a href="/api-keys" class="ctx-err-link">${escapeHtml(tx("nav_api_keys", "API Keys"))} →</a>`;
        } else {
          metaEl.textContent = `⚠️ ${errorMsg}`;
        }
      } else {
        metaEl.className = "manual-ctx-meta";
        metaEl.textContent = tx(
          "enter_symbol_hint",
          "Enter a symbol to load mark and position."
        );
      }
    }
    renderContextOrders([]);
    renderDayRange(null);
    renderAssetFlags(null);
    renderPortfolioHeat(null);
    renderManagePanel(null);
    renderBreaches([]);
    announceContext(null);
    syncManualUi();
    return;
  }
  const quote = data.quote || {};
  const mark = Number(quote.price);
  if (markEl) markEl.textContent = Number.isFinite(mark) ? stockPrice(mark) : "—";
  if (spreadEl) {
    const bid = Number(quote.bid);
    const ask = Number(quote.ask);
    if (bid > 0 && ask > 0) {
      const diff = ask - bid;
      const pct = mark > 0 ? (diff / mark) * 100 : 0;
      const bps = mark > 0 ? ((ask - bid) / mark) * 10000 : null;
      spreadEl.textContent =
        `${stockPrice(bid)} / ${stockPrice(ask)} · $${diff.toFixed(2)} (${pct.toFixed(2)}%)`;
      spreadEl.classList.toggle("is-wide", bps != null && bps > 25);
    } else {
      spreadEl.textContent = "—";
      spreadEl.classList.remove("is-wide");
    }
  }
  if (sessionEl) sessionEl.textContent = formatSession(data.session || quote.session);
  if (posEl) {
    const qty = Number(data.position);
    if (!Number.isFinite(qty) || qty === 0) {
      posEl.textContent = tx("position_flat", "Flat");
      posEl.className = "";
    } else {
      const dir = qty > 0 ? tx("long", "long") : tx("short", "short");
      posEl.textContent = `${formatQty(Math.abs(qty))} ${tx("shares", "shares")} ${dir}`;
      posEl.className = qty > 0 ? "pos" : "neg";
    }
  }
  if (atrEl) {
    const atr = Number(data.atr);
    atrEl.textContent = Number.isFinite(atr) && atr > 0 ? stockPrice(atr) : "—";
  }
  if (bpEl) bpEl.textContent = data.buying_power != null ? money(data.buying_power) : "—";

  const stats = data.stats || {};
  const dayEl = $("manual-ctx-day");
  if (dayEl) {
    const change = Number(stats.day_change_pct);
    if (Number.isFinite(change)) {
      dayEl.textContent = `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`;
      dayEl.className = change > 0 ? "pos" : change < 0 ? "neg" : "";
    } else {
      dayEl.textContent = "—";
      dayEl.className = "";
    }
  }

  // Open P&L is the number that decides whether an add is averaging up or
  // doubling down, and the ticket used to show only the share count.
  const pnlEl = $("manual-ctx-pnl");
  if (pnlEl) {
    const detail = data.position_detail || {};
    const pl = Number(detail.unrealized_pl);
    const pct = Number(detail.unrealized_pct);
    const entry = Number(detail.avg_entry);
    if (Number.isFinite(pl) && Number(data.position) !== 0) {
      const pctText = Number.isFinite(pct) ? ` (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)` : "";
      const entryText = Number.isFinite(entry)
        ? ` · ${tx("from_entry", "from")} ${stockPrice(entry)}`
        : "";
      pnlEl.textContent = `${formatPnl(pl)}${pctText}${entryText}`;
      setPnlTone(pnlEl, pl);
    } else {
      pnlEl.textContent = "—";
      pnlEl.className = "";
    }
  }

  const rangeEl = $("manual-ctx-52w");
  if (rangeEl) {
    const r = stats.range_52w;
    if (r && r.low > 0 && r.high > 0) {
      const off = Number(stats.pct_from_52w_high);
      const offText = Number.isFinite(off) ? ` · ${off.toFixed(1)}%` : "";
      rangeEl.textContent = `${stockPrice(r.low)} – ${stockPrice(r.high)}${offText}`;
    } else {
      rangeEl.textContent = "—";
    }
  }

  const rvolEl = $("manual-ctx-rvol");
  if (rvolEl) {
    const rvol = Number(stats.volume_ratio);
    if (Number.isFinite(rvol)) {
      rvolEl.textContent = `${rvol.toFixed(2)}×`;
      // Thin volume is where a market order gets a price nobody quoted.
      rvolEl.classList.toggle("is-wide", rvol < 0.5);
    } else {
      rvolEl.textContent = "—";
      rvolEl.classList.remove("is-wide");
    }
  }

  const earnEl = $("manual-ctx-earnings");
  if (earnEl) {
    const e = data.earnings || {};
    if (e.next_when_et || e.next_date) {
      const hours = Number(e.hours_until);
      const soon = Number.isFinite(hours) && hours > 0 && hours <= 48;
      earnEl.textContent = String(e.next_when_et || e.next_date);
      earnEl.classList.toggle("neg", !!e.blackout || soon);
    } else {
      earnEl.textContent = e.last_result
        ? tx("earnings_reported", "reported · {result}", { result: String(e.last_result) })
        : "—";
      earnEl.classList.remove("neg");
    }
  }

  if (metaEl) {
    if (errorMsg) {
      metaEl.className = "manual-ctx-meta is-error";
      metaEl.textContent = `⚠️ ${errorMsg}`;
    } else {
      metaEl.className = "manual-ctx-meta";
      const source = (quote.source || "quote").replaceAll("_", " ");
      const age =
        typeof quote.age_seconds === "number" ? ` · ${formatAge(quote.age_seconds)}` : "";
      const atrPct = Number(stats.atr_pct);
      const atrText = Number.isFinite(atrPct)
        ? ` · ATR ${atrPct.toFixed(2)}%`
        : "";
      metaEl.textContent = `${data.symbol || ""} · ${source}${age}${atrText}`.trim();
    }
  }
  renderContextOrders(data.open_orders);
  renderDayRange(stats);
  renderAssetFlags(data.asset);
  renderPortfolioHeat(data.heat);
  renderManagePanel(data);
  // The context's breaches are sized without a ticket; a live preview knows
  // this ticket's own risk too, so it wins whenever it is still current.
  if (!hasFreshServerPreview()) {
    renderBreaches(manualOpensRisk() ? data.breaches : []);
  }
  announceContext(data);
  renderQuickChips();
  applyStockPriceDefaults(data);
  syncManualUi();
}

/**
 * Mark and position, in one line, for assistive tech.
 *
 * The context list was itself `aria-live`, which meant the 15-second auto
 * refresh re-announced eleven rows over whatever the user was doing.
 */
function announceContext(data) {
  const el = $("manual-context-live");
  if (!el) return;
  if (!data) {
    el.textContent = "";
    return;
  }
  const mark = Number(data.quote?.price);
  const qty = Number(data.position);
  const position =
    Number.isFinite(qty) && qty !== 0
      ? `${formatQty(Math.abs(qty))} ${tx("shares", "shares")} ${
          qty > 0 ? tx("long", "long") : tx("short", "short")
        }`
      : tx("position_flat", "Flat");
  const next = `${data.symbol || ""} ${
    Number.isFinite(mark) ? stockPrice(mark) : "—"
  } · ${position}`;
  if (el.textContent !== next) el.textContent = next;
}

/** Where the last trade sits between the day's low and high. */
function renderDayRange(stats) {
  const wrap = $("manual-day-range");
  if (!wrap) return;
  const pct = Number(stats?.day_range_pct);
  const range = stats?.day_range;
  if (!Number.isFinite(pct) || !range) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  const clampedPct = Math.max(0, Math.min(100, pct));
  const marker = $("manual-day-range-marker");
  if (marker) marker.style.left = `${clampedPct}%`;
  const low = $("manual-day-low");
  const high = $("manual-day-high");
  const label = $("manual-day-range-label");
  if (low) low.textContent = stockPrice(range.low);
  if (high) high.textContent = stockPrice(range.high);
  const labelText = tx("day_range_position", "{pct}% of day's range", {
    pct: pct.toFixed(0),
  });
  if (label) label.textContent = labelText;
  // min/max/now share one unit (price), so a screen reader reads a coherent
  // range instead of a percentage sandwiched between two dollar bounds.
  const mark = Number(manualContext?.quote?.price);
  if (range.low != null) wrap.setAttribute("aria-valuemin", String(range.low));
  if (range.high != null) wrap.setAttribute("aria-valuemax", String(range.high));
  wrap.setAttribute(
    "aria-valuenow",
    Number.isFinite(mark) ? String(mark) : String(range.low ?? 0)
  );
  wrap.setAttribute("aria-valuetext", `${stockPrice(mark)} · ${labelText}`);
}

/** Broker facts that decide whether a ticket can exist — shown, not guessed. */
function renderAssetFlags(asset) {
  const el = $("manual-asset-flags");
  const warn = $("manual-asset-warn");
  if (!el) return;
  if (!asset || !asset.symbol) {
    el.hidden = true;
    el.innerHTML = "";
    if (warn) warn.hidden = true;
    return;
  }
  const flags = [
    { on: asset.shortable, label: tx("flag_shortable", "shortable") },
    { on: asset.fractionable, label: tx("flag_fractionable", "fractionable") },
    { on: asset.marginable, label: tx("flag_marginable", "marginable") },
    { on: asset.easy_to_borrow, label: tx("flag_etb", "easy to borrow") },
  ];
  el.hidden = false;
  el.innerHTML = flags
    .map(
      (f) =>
        `<li class="manual-flag" data-on="${f.on ? "yes" : "no"}">${escapeHtml(
          f.label
        )}</li>`
    )
    .join("");

  if (warn) {
    if (asset.tradable === false) {
      warn.hidden = false;
      warn.textContent = tx(
        "asset_not_tradable",
        "Alpaca does not accept orders in this symbol — it is not tradable."
      );
    } else {
      warn.hidden = true;
    }
  }
}

/**
 * Book-wide open risk.
 *
 * The bar is scaled against the desk's own stated appetite (risk per trade ×
 * max positions) rather than an arbitrary ceiling, so "full" means "at the
 * limit you configured", not "at some number the page invented".
 */
function renderPortfolioHeat(heat) {
  const panel = $("manual-heat-panel");
  if (!panel) return;
  if (!heat || !heat.positions) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const valueEl = $("manual-heat-value");
  const noteEl = $("manual-heat-note");
  const fill = $("manual-heat-fill");
  const nakedEl = $("manual-heat-unprotected");

  const pct = Number(heat.open_risk_pct);
  if (valueEl) {
    valueEl.textContent = Number.isFinite(pct)
      ? `${money(heat.open_risk)} · ${pct.toFixed(2)}%`
      : money(heat.open_risk);
  }

  const perTrade = Number(manualFormValue("ai_risk_pct", 0.5)) || 0;
  const maxPositions = Number(lastDeskSettings?.ai_max_positions) || 0;
  const budget = perTrade > 0 && maxPositions > 0 ? perTrade * maxPositions : null;
  const track = panel.querySelector(".manual-heat-track");
  if (track) {
    track.setAttribute("aria-valuenow", Number.isFinite(pct) ? pct.toFixed(2) : "0");
    if (budget) track.setAttribute("aria-valuemax", budget.toFixed(2));
  }
  if (fill) {
    const ratio =
      budget && Number.isFinite(pct) ? Math.max(0, Math.min(1, pct / budget)) : 0;
    fill.style.width = `${(ratio * 100).toFixed(1)}%`;
    fill.dataset.level = ratio >= 1 ? "over" : ratio >= 0.75 ? "high" : "ok";
  }
  if (noteEl) {
    noteEl.textContent = budget
      ? tx("heat_note_budget", "{count} positions · desk budget {budget}%", {
          count: String(heat.positions),
          budget: budget.toFixed(2),
        })
      : tx("heat_note_plain", "{count} positions with a resting stop", {
          count: String(heat.protected ?? heat.positions),
        });
  }
  if (nakedEl) {
    const naked = Array.isArray(heat.unprotected) ? heat.unprotected : [];
    if (naked.length) {
      nakedEl.hidden = false;
      nakedEl.textContent = tx(
        "heat_unprotected",
        "No stop on: {symbols} ({value} exposed)",
        { symbols: naked.join(", "), value: money(heat.unprotected_value) }
      );
    } else {
      nakedEl.hidden = true;
    }
  }
}

/** The manage-stop controls, shown only when there is a position to protect. */
function renderManagePanel(data) {
  const panel = $("manual-manage-panel");
  if (!panel) return;
  const qty = Number(data?.position);
  if (!Number.isFinite(qty) || qty === 0) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const summary = $("manual-manage-summary");
  const detail = data.position_detail || {};
  const stop = Number(data.current_stop);
  const entry = Number(detail.avg_entry);
  if (summary) {
    const parts = [
      `${formatQty(Math.abs(qty))} ${tx("shares", "shares")} ${
        qty > 0 ? tx("long", "long") : tx("short", "short")
      }`,
    ];
    if (Number.isFinite(entry)) {
      parts.push(`${tx("entry_price", "Entry")} ${stockPrice(entry)}`);
    }
    parts.push(
      Number.isFinite(stop) && stop > 0
        ? `${tx("stop_price", "Stop")} ${stockPrice(stop)}`
        : tx("no_stop_resting", "no stop resting")
    );
    summary.textContent = parts.join(" · ");
    summary.classList.toggle("warn", !(stop > 0));
  }
  const stopInput = $("manual-manage-stop");
  if (stopInput && !stopInput.value && Number.isFinite(stop) && stop > 0) {
    stopInput.value = stop.toFixed(2);
  }
  const beBtn = $("btn-stop-breakeven");
  if (beBtn) {
    beBtn.disabled = !Number.isFinite(entry) || busy || loopRunning;
    beBtn.title = Number.isFinite(entry)
      ? tx("stop_to_breakeven_hint", "Move the stop to your average entry so the trade cannot lose")
      : tx("breakeven_needs_entry", "Alpaca has no average entry price for this position");
  }
  const closeBtn = $("btn-manage-close");
  if (closeBtn) closeBtn.disabled = busy || loopRunning;
}

/**
 * Flatten the whole position at market from the Manage Position card.
 *
 * The form already knows how to build a full Sell ticket, but that means
 * switching side, clearing the sizing block, and hitting Preview just to
 * close out what the panel is already showing — this is the one-click path.
 */
async function closeManagedPosition() {
  if (busy || loopRunning) return;
  const symbol = String(manualContext?.symbol || manualSymbol() || "").trim().toUpperCase();
  const qty = Math.abs(Number(manualContext?.position) || 0);
  if (!symbol || !(qty > 0)) return;
  const confirmed = await askInlineConfirm(
    tx(
      "manual_confirm_close_position",
      "Close {qty} shares of {symbol} at market? Any resting protective stop is cancelled first.",
      { qty: formatQty(qty), symbol }
    ),
    { confirmLabel: tx("close_position_btn", "Close position") }
  );
  if (!confirmed) return;
  try {
    setBusy(true, tx("closing_position", "Closing position…"));
    const data = await api(`/api/positions/${encodeURIComponent(symbol)}/close`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    // A 200 here only means the request was well-formed — the broker can
    // still refuse the order itself, and that comes back inside `result`.
    const result = data.result;
    const status = String(result?.status || "").toLowerCase();
    const rejected =
      result?.ok === false ||
      ["failed", "rejected", "canceled", "cancelled", "expired"].includes(status);
    if (rejected) {
      throw new Error(
        tx(
          "position_close_not_accepted",
          "Close order was not accepted ({status}). Refresh the position and try again.",
          { status: result?.status || "rejected" }
        )
      );
    }
    showToast(
      tx("position_closed_toast", "{symbol} close order submitted", { symbol }),
      "ok"
    );
  } catch (err) {
    showToast(err.message || tx("error_close_position", "Failed to close position"), "error");
  } finally {
    setBusy(false);
    await refreshManualContext().catch(() => {});
    await refreshManualPositions().catch(() => {});
  }
}

/**
 * Render quick ticker chips above the symbol input for open positions and
 * the desk's own watchlist.
 */
function renderQuickChips() {
  const wrap = $("manual-quick-chips");
  if (!wrap) return;

  const currentSym = manualSymbol();
  const positions = Array.isArray(manualOpenPositions) ? manualOpenPositions : [];
  const chips = [];

  // Add all open positions first
  positions.forEach((pos) => {
    const sym = String(pos.symbol || "").toUpperCase();
    if (!sym) return;
    const qty = Number(pos.qty || 0);
    const isShort = String(pos.side || "long").toLowerCase() === "short";
    const plPct = Number(pos.unrealized_pct);
    const plStr = Number.isFinite(plPct) ? `${plPct >= 0 ? "+" : ""}${plPct.toFixed(1)}%` : "";
    const isNegative = Number.isFinite(plPct) ? plPct < 0 : isShort;
    chips.push({
      symbol: sym,
      isPosition: true,
      qty,
      badge: plStr,
      isShort,
      isNegative,
    });
  });

  // Fill out with the desk's own watchlist, not a fixed guess.
  const raw = lastDeskSettings?.symbols || lastDeskSettings?.symbol || "";
  const watch = String(raw)
    .split(",")
    .map((s) => s.trim().toUpperCase())
    .filter((s) => /^[A-Z.\-]{1,12}$/.test(s));
  watch.forEach((sym) => {
    if (!chips.some((c) => c.symbol === sym)) {
      chips.push({ symbol: sym, isPosition: false });
    }
  });

  wrap.innerHTML = chips
    .map((c) => {
      const active = c.symbol === currentSym ? " is-active" : "";
      const badgeClass = c.isNegative ? "neg" : "pos";
      const badgeHtml = c.badge
        ? `<span class="quick-chip-pos ${badgeClass}">${escapeHtml(c.badge)}</span>`
        : "";
      return `<button type="button" class="quick-chip${active}" data-chip-symbol="${escapeHtml(c.symbol)}"><strong>${escapeHtml(c.symbol)}</strong>${badgeHtml}</button>`;
    })
    .join("");
  wrap.hidden = chips.length === 0;
}

/** Open positions for the quick-chip rail. A failed fetch keeps the last
 *  known list rather than blanking the chips. */
async function refreshManualPositions() {
  try {
    const data = await api("/api/positions");
    manualOpenPositions = Array.isArray(data.positions) ? data.positions : [];
  } catch {
    /* keep the last known list */
  }
  renderQuickChips();
}

async function refreshManualContext() {
  const symbol = manualSymbol();
  if (!symbol) {
    manualContextRequestId += 1;
    manualContextError = null;
    applyManualContext(null);
    updateSizeEstimate();
    return null;
  }
  const requestId = ++manualContextRequestId;
  const metaEl = $("manual-ctx-meta");
  const refreshBtn = $("btn-manual-refresh");
  if (refreshBtn) refreshBtn.classList.add("is-loading");
  if (metaEl && (!manualContext || manualContext.symbol !== symbol)) {
    metaEl.className = "manual-ctx-meta";
    metaEl.textContent = tx("loading_symbol", "Loading {symbol}…", { symbol });
  }
  try {
    const data = await api(`/api/manual-context?symbol=${encodeURIComponent(symbol)}`);
    if (requestId !== manualContextRequestId) return null; // superseded by a newer request
    manualContextError = null;
    applyManualContext(data);
    if (data.account) applyAccount(data.account);
    updateSizeEstimate();
    scheduleServerPreview();
    return data;
  } catch (err) {
    if (requestId !== manualContextRequestId) return null; // superseded by a newer request
    const errMsg = err.message || tx("could_not_load", "Could not load data");
    const suggestion = err.message?.includes("not found")
      ? ` • ${tx("check_symbol_spelling", "Check symbol spelling")}`
      : err.message?.includes("network")
        ? ` • ${tx("check_connection", "Check your connection")}`
        : "";
    manualContextError = `${errMsg}${suggestion}`;
    if (manualContext && manualContext.symbol === symbol) {
      applyManualContext(manualContext, manualContextError);
    } else {
      applyManualContext(null, manualContextError);
    }
    // Keep prior context values but refresh session-aware controls.
    syncManualUi();
    updateSizeEstimate();
    throw err;
  } finally {
    if (refreshBtn) refreshBtn.classList.remove("is-loading");
  }
}

function scheduleManualContextRefresh() {
  clearTimeout(manualContextTimer);
  manualContextTimer = setTimeout(() => {
    refreshManualContext().catch(() => {});
  }, 320);
}

/** Order types Alpaca will accept in the session the ticket is being typed in. */
function syncManualTypeUi() {
  const typeSelect = $("manual-order-type");
  const warn = $("manual-session-warn");
  const currentType = manualOrderType();
  const isMarket = currentType === "market";

  const allowedTypes = ["market", "limit", "stop", "stop_limit", "trailing_stop"];
  if (typeSelect) {
    [...typeSelect.options].forEach((opt) => {
      opt.disabled = !allowedTypes.includes(opt.value);
    });
    typeSelect.disabled = loopRunning || busy;
    ensureNiceSelect(typeSelect);
    decorateOrderTypeSelect(typeSelect);
  }

  // Non-limit types cannot fill in the 24-hour market.
  if (manualOrderTypeIsRthOnly() && manualExtendedHours()) {
    setManualFormValue("trading_session", "regular");
  }

  const tifSelect = $("manual-tif");
  if (tifSelect) {
    const exitQty = manualIsExit() && !manualOpensShort() ? manualSellQty() : 0;
    const fractionalExit = exitQty > 0 && !Number.isInteger(exitQty);
    const protectedEntry = manualIsEntry() && manualBracketEnabled() && ["market", "limit"].includes(currentType);

    // Alpaca strictly requires DAY for Market orders:
    let allowedTifs;
    if (isMarket) {
      allowedTifs = ["day"];
    } else if (fractionalExit) {
      allowedTifs = ["day"];
    } else if (manualExtendedHours() || protectedEntry || ["stop", "stop_limit", "trailing_stop"].includes(currentType)) {
      allowedTifs = ["day", "gtc"];
    } else {
      allowedTifs = ["day", "gtc", "ioc", "fok", "opg", "cls"];
    }

    if (!allowedTifs.includes(manualTimeInForce())) {
      setManualFormValue("time_in_force", "day");
    }
    [...tifSelect.options].forEach((opt) => {
      const allowed = allowedTifs.includes(opt.value);
      opt.disabled = !allowed;
      opt.hidden = !allowed;
    });
    tifSelect.closest(".manual-tif-field")?.classList.toggle(
      "tif-day-gtc-only",
      manualExtendedHours() || isMarket
    );
    tifSelect.disabled = isMarket || loopRunning || busy;
    ensureNiceSelect(tifSelect);
    decorateTifSelect(tifSelect);
  }

  const tifCol = tifSelect?.closest(".manual-tif-col");
  if (tifCol) {
    tifCol.hidden = isMarket;
  }
  const tifLimitRow = tifSelect?.closest(".manual-tif-limit-row");
  if (tifLimitRow) {
    tifLimitRow.hidden = isMarket;
  }

  syncTradingSessionUi();

  const rows = {
    "manual-limit-row": manualNeedsLimit(),
    "manual-stop-row": manualNeedsTrigger(),
    "manual-trail-row": manualNeedsTrail(),
  };
  Object.entries(rows).forEach(([id, visible]) => {
    const row = $(id);
    if (row) row.hidden = !visible;
    row?.querySelectorAll("input").forEach((input) => {
      input.disabled = !visible || loopRunning || busy;
    });
  });

  if (warn) {
    const queued = manualTicketQueuesForRth();
    warn.hidden = !queued;
    if (queued) {
      warn.textContent = tx(
        "order_activates_rth",
        "{session}: this {type} order will rest until regular hours.",
        {
          session: formatSession(manualContext?.session),
          type: manualOrderTypeLabel(manualOrderType()),
        }
      );
    }
  }
  syncTifHelp();
  syncLimitOffset();
  syncTriggerOffset();
  syncQuoteFillButtons();
}

function etWeekdayUtc(year, month, day) {
  return new Date(Date.UTC(year, month, day, 17, 0, 0)).getUTCDay();
}

function etYmdNow() {
  const parts = etParts(Date.now());
  if (!parts) return null;
  return {
    y: parts.year,
    m: parts.month,
    d: parts.day,
    min: Number(parts.hour) * 60 + Number(parts.minute),
  };
}

/** "today" / "tomorrow" / weekday for the next weekday cutoff in ET. */
function dayExpireWhen(cutoffMin) {
  const now = etYmdNow();
  if (!now) return tx("tif_when_today", "today");
  let y = now.y;
  let m = now.m;
  let d = now.d;
  for (let i = 0; i < 8; i++) {
    const weekday = etWeekdayUtc(y, m, d);
    const isWeekday = weekday >= 1 && weekday <= 5;
    const beforeCutoff = i === 0 ? now.min < cutoffMin : true;
    if (isWeekday && beforeCutoff) {
      if (i === 0) return tx("tif_when_today", "today");
      if (i === 1) return tx("tif_when_tomorrow", "tomorrow");
      const lang = window.i18n?.getCurrentLanguage?.() || "en";
      return new Intl.DateTimeFormat(lang, {
        weekday: "short",
        timeZone: "UTC",
      }).format(new Date(Date.UTC(y, m, d, 17, 0, 0)));
    }
    const next = new Date(Date.UTC(y, m, d + 1, 17, 0, 0));
    y = next.getUTCFullYear();
    m = next.getUTCMonth();
    d = next.getUTCDate();
  }
  return tx("tif_when_today", "today");
}

function tifExpireHint(tif) {
  const overnight = manualExtendedHours();
  switch (tif) {
    case "day": {
      const time = overnight
        ? tx("tif_time_8pm", "8:00 pm ET")
        : tx("tif_time_4pm", "4:00 pm ET");
      const when = dayExpireWhen(overnight ? 20 * 60 : 16 * 60);
      return tx("tif_hint_expires_at", "Expires at {time} {when}", { time, when });
    }
    case "gtc":
      return overnight
        ? tx("tif_hint_gtc_24h", "Expires in 90 days")
        : tx("tif_hint_gtc", "Until cancelled");
    case "ioc":
      return tx("tif_hint_ioc", "Fills now or cancels");
    case "fok":
      return tx("tif_hint_fok", "All now or none");
    case "opg":
      return tx("tif_hint_opg", "At the 9:30 am ET open");
    case "cls":
      return tx("tif_hint_cls", "At the 4:00 pm ET close");
    default:
      return "";
  }
}

function orderTypeActivationHint(otype) {
  if (otype === "limit") {
    return tx("session_all", "All sessions");
  }
  return tx("session_regular", "Regular hours");
}

/** Name on the left, activation session on the right — every type stays
 *  selectable; the hint is when Alpaca will actually work the ticket. */
function decorateOrderTypeSelect(select) {
  const dropdown = select?._niceSelect?.dropdown;
  if (!dropdown) return;
  const fill = (el, otype) => {
    if (!el) return;
    const name = manualOrderTypeLabel(otype);
    const hint = orderTypeActivationHint(otype);
    el.innerHTML =
      `<span class="type-name">${escapeHtml(name)}</span>` +
      (hint ? `<span class="type-hint">${escapeHtml(hint)}</span>` : "");
  };
  fill(dropdown.querySelector(".current"), select.value);
  dropdown.querySelectorAll(".option").forEach((opt) => {
    const value = opt.dataset.value || opt.getAttribute("data-value");
    fill(opt, value);
  });
}

/** Name on the left, expiration on the right — the closed control has to
 *  show when this ticket dies, not just the TIF acronym. */
function decorateTifSelect(select) {
  const dropdown = select?._niceSelect?.dropdown;
  if (!dropdown) return;
  const overnight = manualExtendedHours();
  const fill = (el, tif) => {
    if (!el) return;
    const code = String(tif || "day").toUpperCase();
    const hint = tifExpireHint(tif);
    el.innerHTML =
      `<span class="tif-name">${escapeHtml(code)}</span>` +
      (hint ? `<span class="tif-hint">${escapeHtml(hint)}</span>` : "");
  };
  fill(dropdown.querySelector(".current"), select.value);
  dropdown.querySelectorAll(".option").forEach((opt) => {
    const value = opt.dataset.value || opt.getAttribute("data-value");
    if (overnight && !["day", "gtc"].includes(value)) {
      opt.hidden = true;
      return;
    }
    opt.hidden = false;
    fill(opt, value);
  });
}

function syncTradingSessionUi() {
  const form = $("manual-order");
  const sessionField = $("manual-session-field");
  const currentType = manualOrderType();
  const isLimit = currentType === "limit";

  // 24-hour market is exclusively supported for Limit orders on Alpaca.
  // When placing Market, Stop, Stop Limit, or Trailing Stop, hide session choice.
  if (sessionField) {
    sessionField.hidden = !isLimit;
  }

  const overnight = form?.querySelector('input[name="trading_session"][value="24h"]');
  const regular = form?.querySelector('input[name="trading_session"][value="regular"]');
  const locked = loopRunning || busy;
  const rthOnlyType = manualOrderTypeIsRthOnly();
  const lock24h = rthOnlyType || !isLimit;
  if (overnight) {
    if (lock24h && overnight.checked) {
      setManualFormValue("trading_session", "regular");
      if (regular) regular.checked = true;
    }
    overnight.disabled = lock24h || locked;
    const segment = overnight.closest(".segment");
    const reason = rthOnlyType
      ? tx(
          "help_session_rth_type",
          "This order type fills in regular hours only. Sent now, it rests until the open."
        )
      : "";
    const tooltip = $("manual-session-24h-disabled-reason");
    if (tooltip) {
      tooltip.hidden = !reason;
      tooltip.textContent = reason;
      if (reason) overnight.setAttribute("aria-describedby", tooltip.id);
      else overnight.removeAttribute("aria-describedby");
    }
    if (segment) {
      segment.classList.toggle("is-disabled", lock24h);
      if (reason && overnight.disabled) {
        segment.tabIndex = 0;
        segment.setAttribute("aria-disabled", "true");
      } else {
        segment.removeAttribute("tabindex");
        segment.removeAttribute("aria-disabled");
      }
    }
  }
  if (regular) regular.disabled = locked;

  const help = $("manual-session-help");
  if (help) {
    if (!isLimit) {
      help.hidden = true;
    } else {
      help.hidden = false;
      if (manualExtendedHours()) {
        help.textContent = manualIsEntry()
          ? tx(
              "help_session_24h_entry",
              "Limit Day or GTC. Alpaca cannot attach a stop to a 24-hour order — the desk arms the stop after this ticket fills."
            )
          : tx(
              "help_session_24h",
              "Limit Day or GTC. Day orders stay live through overnight, pre-market, regular, and after-hours, then cancel at 8:00 pm ET."
            );
      } else {
        help.textContent = tx(
          "help_session_regular",
          "Regular hours fill 9:30 am–4:00 pm ET. Day orders cancel at the 4:00 pm close. Choose 24 hour market to fill overnight, pre-market, or after hours."
        );
      }
    }
  }

  // Next-ticket market orders are not supported in the 24-hour market:
  // force the UI to stay on `limit` so the server does not reject the plan.
  const followonMarket = form?.querySelector(
    'input[name="followon_order_type"][value="market"]'
  );
  if (followonMarket) {
    const overnightEnabled = manualExtendedHours();
    followonMarket.disabled = overnightEnabled || locked;
    if (overnightEnabled && followonMarket.checked) {
      setManualFormValue("followon_order_type", "limit");
    }
  }
}

/** One sentence per time-in-force — the acronyms mean nothing on their own. */
function syncTifHelp() {
  const el = $("help-tif");
  if (!el) return;
  const overnight = manualExtendedHours();
  const map = {
    day: overnight
      ? tx(
          "help_tif_day_24h",
          "A Day order in the 24-hour market is cancelled at 8:00 pm ET if it has not filled."
        )
      : tx(
          "help_tif_day_rth",
          "A Day order is cancelled at the 4:00 pm ET close if it has not filled."
        ),
    gtc: overnight
      ? tx(
          "help_tif_gtc_24h",
          "Good till cancelled — the order keeps resting across 24-hour sessions for up to 90 days."
        )
      : tx(
          "help_tif_gtc",
          "Good till cancelled — the order keeps resting across sessions until it fills or you cancel it."
        ),
    ioc: tx(
      "help_tif_ioc",
      "Immediate or cancel — whatever fills right now fills; the rest is dropped."
    ),
    fok: tx("help_tif_fok", "Fill or kill — the whole size fills at once, or nothing does."),
    opg: tx("help_tif_opg", "At the open — the order only participates in the opening auction."),
    cls: tx("help_tif_cls", "At the close — the order only participates in the closing auction."),
  };
  el.textContent = map[manualTimeInForce()] || map.day;
}

/** Show how far a price sits from the mark — a fat-finger catcher. */
function priceOffsetText(el, price, { hintKey, hint }) {
  if (!el) return;
  const mark = Number(manualContext?.quote?.price);
  if (!(price > 0) || !(mark > 0)) {
    el.textContent = tx(hintKey, hint);
    el.classList.remove("warn");
    return;
  }
  const pct = ((price - mark) / mark) * 100;
  const away = pct >= 0 ? tx("above_mark", "above mark") : tx("below_mark", "below mark");
  el.textContent = `${Math.abs(pct).toFixed(2)}% ${away} (${stockPrice(mark)})`;
  el.classList.toggle("warn", Math.abs(pct) >= 10);
}

function syncLimitOffset() {
  const el = $("manual-limit-offset");
  if (!el) return;
  if (!manualNeedsLimit()) {
    el.textContent = "";
    el.classList.remove("warn");
    return;
  }
  priceOffsetText(el, Number(manualFormValue("limit_price", "")), {
    hintKey: "limit_offset_hint",
    hint: "Limit orders rest until price trades there.",
  });
}

function syncTriggerOffset() {
  const el = $("manual-stop-offset");
  if (!el) return;
  if (!manualNeedsTrigger()) {
    el.textContent = "";
    el.classList.remove("warn");
    return;
  }
  priceOffsetText(el, manualTriggerPrice(), {
    hintKey: "trigger_offset_hint",
    hint: "The order stays dormant until price reaches this trigger.",
  });
}

function syncManualLoopBanner() {
  const banner = $("manual-loop-banner");
  if (!banner) return;
  banner.hidden = !loopRunning;
}

/**
 * Mirror of the server risk engine (`Config.ai_stop_distance` +
 * `ai_qty_for_risk`), including the equity cap and the whole-share quantity
 * required by protected entries.
 */
function calculateSizeEstimate() {
  const side = manualSide();
  const symbol = manualSymbol();
  const mark = Number(manualContext?.quote?.price);
  const limit = Number(manualFormValue("limit_price", ""));
  const trigger = manualTriggerPrice();
  // Whichever price this ticket would actually fill at.
  const entry =
    manualNeedsLimit() && limit > 0 ? limit : trigger > 0 ? trigger : mark;
  const buyingPower = Number(manualContext?.buying_power);

  if (!symbol) return null;

  if (!manualContext || manualContext.symbol !== symbol || !(mark > 0)) {
    const isMissingKeys =
      manualContextError?.includes("credentials") ||
      manualContextError?.includes("API Keys") ||
      manualContextError?.includes("API keys") ||
      lastAlpacaStatus?.set === false;
    const msg = isMissingKeys
      ? tx(
          "err_alpaca_keys_required",
          "Alpaca API credentials are not configured. Connect your API keys on API Keys to preview and size orders."
        )
      : manualContextError && (!manualContext || manualContext.symbol === symbol)
        ? manualContextError
        : tx("err_waiting_quote", "Fetching live quote for {symbol}…", { symbol });
    return {
      side,
      blocked: true,
      blockedMessage: msg,
    };
  }

  if (manualOpensShort()) {
    const exact = manualSellQty();
    if (!(exact > 0)) return null;
    // Alpaca never borrows a fraction of a share, so the desk floors a short
    // in every session — the panel has to show the ticket that goes out.
    const shares = Math.floor(exact);
    if (!(shares > 0)) {
      return {
        side,
        blocked: true,
        blockedMessage: tx(
          "err_short_fractional",
          "Alpaca does not short fractional shares — this ticket needs at least 1 whole share."
        ),
      };
    }
    if (manualContext?.asset?.shortable === false) {
      return {
        side,
        blocked: true,
        blockedMessage: tx(
          "err_not_shortable",
          "This symbol is not shortable at Alpaca — the borrow is unavailable, so the ticket would be rejected."
        ),
      };
    }
    const proceeds = shares * entry;
    return {
      side,
      isShortEntry: true,
      shares,
      entry,
      proceeds,
      truncated: shares !== exact,
      // A short is opened, not closed, so it consumes buying power the way a
      // buy does — the "Remaining" cell an exit shows means nothing here.
      bpPct: buyingPower > 0 ? (proceeds / buyingPower) * 100 : null,
      exceedsBp: buyingPower > 0 && proceeds > buyingPower,
    };
  }

  if (manualIsExit()) {
    const held = manualPositionQty();
    const qty = manualSellQty();
    if (!(qty > 0)) return null;
    const remaining = Math.max(0, held - qty);
    return {
      side,
      isExit: true,
      shares: qty,
      entry,
      proceeds: qty * entry,
      held,
      remaining,
      // The partial-exit fix: the leftover gets a fresh stop, and the user
      // should know that before they press the button, not after.
      rearms: remaining > 1e-9,
    };
  }

  const equity = Number(manualContext?.equity ?? manualContext?.account?.equity);
  const atr = Number(manualContext?.atr);
  const bracketOn = manualBracketEnabled();
  const atrMult = bracketOn ? Number(manualFormValue("ai_atr_stop_mult", 1.8) || 0) : 0;
  const riskPct = Number(manualFormValue("ai_risk_pct", 0.5) || 0);
  const fallbackStopPct = Number(manualContext?.stop_loss_pct || 0);

  if (!(equity > 0)) {
    return {
      side,
      blocked: true,
      blockedMessage: tx(
        "err_no_equity",
        "Account equity is unavailable — connect Alpaca on API Keys before sizing a ticket."
      ),
    };
  }

  if (!bracketOn) {
    const sizeMode = manualBuySizeMode();
    if (sizeMode === "risk") {
      return {
        side,
        blocked: true,
        blockedMessage: tx(
          "err_risk_needs_bracket",
          "Risk sizing requires a protective stop. Enable Protective Bracket or switch to Shares (#) or Dollars ($)."
        ),
      };
    }
    let shares = 0;
    let truncated = false;
    if (sizeMode === "notional") {
      const dollars = Number(manualFormValue("notional", 0) || 0);
      if (!(dollars > 0)) return null;
      const exact = dollars / mark;
      // Without a bracket nothing forces whole shares, and the desk sends the
      // fraction — so the panel has to show it too.
      shares = manualQtyIsWholeOnly() ? Math.floor(exact) : exact;
      truncated = shares !== exact;
    } else if (sizeMode === "qty") {
      shares = Number(manualFormValue("buy_qty", 0) || 0);
    }
    if (!(shares > 0)) return null;
    const cost = shares * entry;
    return {
      side,
      sizeMode,
      shares,
      entry,
      stopDistance: null,
      riskDollars: null,
      riskPct: null,
      stopPrice: null,
      stopLimitPrice: null,
      stopLimitOffset: 0,
      targetPrice: null,
      takeProfitR: 0,
      cost,
      truncated,
      usesAtr: false,
      equity,
      projectedRiskPct: null,
      riskReward: null,
      attachesStop: false,
      bpPct: buyingPower > 0 ? (cost / buyingPower) * 100 : null,
      exceedsBp: buyingPower > 0 && cost > buyingPower,
    };
  }

  if (!(atrMult > 0)) return null;
  if (manualBuySizeMode() === "risk" && !(riskPct > 0)) return null;

  // Server: ATR × multiple when both are usable, else the flat stop-loss %.
  // Both branches size off the *mark*, not the limit — `place_manual_order`
  // passes `price` (the quote) into ai_stop_distance and ai_qty_for_risk.
  const usesAtr = atr > 0;
  const stopDistance = usesAtr ? atr * atrMult : mark * (fallbackStopPct / 100);
  if (!(stopDistance > 0)) {
    return {
      side,
      blocked: true,
      blockedMessage: tx(
        "err_no_stop_distance",
        "No ATR for this symbol and no flat stop % set on Auto Trade — the risk engine cannot size this ticket."
      ),
    };
  }

  const sizeMode = manualBuySizeMode();
  let exactShares;
  if (sizeMode === "notional") {
    const dollars = Number(manualFormValue("notional", 0) || 0);
    if (!(dollars > 0)) return null;
    exactShares = dollars / mark;
  } else if (sizeMode === "qty") {
    exactShares = Number(manualFormValue("buy_qty", 0) || 0);
    if (!(exactShares > 0)) return null;
  } else {
    // `ai_qty_for_risk` never lets one position exceed the account.
    exactShares = Math.min((equity * (riskPct / 100)) / stopDistance, equity / mark);
  }
  const wholeOnly = manualQtyIsWholeOnly();
  if (wholeOnly && sizeMode === "risk" && equity >= mark && exactShares < 1) {
    const minNeededRisk = (stopDistance / equity) * 100;
    if (minNeededRisk <= 10) {
      exactShares = 1;
    }
  }
  const shares = wholeOnly ? Math.floor(exactShares) : exactShares;
  const truncated = shares !== exactShares;
  if (!(shares > 0)) {
    return {
      side,
      blocked: true,
      blockedMessage: wholeOnly
        ? tx(
            "err_size_zero",
            "This ticket sizes to less than one whole share, and a protective stop needs at least one. Raise Risk per trade % or lower Stop = ATR ×."
          )
        : tx(
            "err_size_zero_fractional",
            "This ticket sizes to nothing. Raise Risk per trade % or the dollar amount."
          ),
    };
  }

  // The desk converts the distance to a percent off the mark, then applies it
  // to the entry reference (the limit price on a limit ticket).
  const stopPct = stopDistance / mark;
  const stopPrice = normalizeStockPrice(entry * (1 - stopPct));
  const cost = shares * entry;
  const riskPerShare = entry - stopPrice;
  // Server: target = entry + R × risk-per-share, the bracket's other leg.
  const takeProfitR = manualTakeProfitR();
  const targetPrice =
    takeProfitR > 0 && riskPerShare > 0
      ? normalizeStockPrice(entry + riskPerShare * takeProfitR)
      : null;
  const stopLimitOffset = manualStopLimitOffsetPct();
  const stopLimitPrice = stopLimitFromStop(
    stopPrice,
    stopLimitOffset,
    manualStopLimitPrice()
  );
  const riskDollars = shares * riskPerShare;
  // Heat is the book's number, so the ticket's own risk has to be added to it
  // to answer "what would I be risking in total if I send this?".
  const openRisk = Number(manualContext?.heat?.open_risk);
  const projectedRiskPct =
    equity > 0 && Number.isFinite(openRisk)
      ? ((openRisk + riskDollars) / equity) * 100
      : null;
  return {
    side,
    sizeMode,
    shares,
    entry,
    stopDistance,
    riskDollars,
    riskPct,
    stopPrice,
    stopLimitPrice,
    stopLimitOffset,
    targetPrice,
    takeProfitR,
    cost,
    truncated,
    usesAtr,
    equity,
    projectedRiskPct,
    riskReward: manualRiskReward(riskDollars, targetPrice, shares, entry),
    attachesStop: manualAttachesStop(),
    bpPct: buyingPower > 0 ? (cost / buyingPower) * 100 : null,
    exceedsBp: buyingPower > 0 && cost > buyingPower,
  };
}

/**
 * Reward ÷ risk for the bracket.
 *
 * Both legs were already on the panel and the ratio between them — the number
 * that actually says whether the trade is worth taking — was computed into
 * `rewardDollars` and then thrown away.
 */
function manualRiskReward(riskDollars, targetPrice, shares, entry) {
  const risk = Number(riskDollars);
  const target = Number(targetPrice);
  const qty = Number(shares);
  const px = Number(entry);
  if (!(risk > 0) || !(qty > 0) || !(px > 0) || !Number.isFinite(target) || !(target > 0)) {
    return null;
  }
  const reward = qty * Math.abs(target - px);
  if (!(reward > 0)) return null;
  return { reward, ratio: reward / risk };
}

/**
 * Fold a server preview into the same shape `calculateSizeEstimate` returns.
 *
 * The two paths render through one function so the panel cannot drift between
 * "what the browser guessed" and "what the desk said" — only the numbers
 * differ, never the layout.
 */
function estimateFromServer(result) {
  if (!result || typeof result !== "object") return null;
  const side = String(result.side || "buy");
  const shares = Number(result.order_qty);
  if (!(shares > 0)) return null;
  const entry = Number(result.limit_price || result.stop_price || result.price);
  if (side === "short") {
    const proceeds = shares * entry;
    const bp = Number(manualContext?.buying_power) || 0;
    return {
      side,
      isShortEntry: true,
      fromServer: true,
      shares,
      entry,
      proceeds,
      truncated: !!result.qty_whole_for_short,
      warnings: Array.isArray(result.warnings) ? result.warnings : [],
      breaches: Array.isArray(result.breaches) ? result.breaches : [],
      bpPct: bp > 0 ? (proceeds / bp) * 100 : null,
      exceedsBp: bp > 0 && proceeds > bp,
    };
  }
  if (side === "sell") {
    const held = Math.abs(Number(result.position) || 0);
    return {
      side,
      isExit: true,
      fromServer: true,
      shares,
      entry,
      proceeds: shares * entry,
      held,
      remaining: Math.max(0, held - shares),
      rearms: held - shares > 1e-9,
    };
  }
  const stopPrice = Number(result.stop_preview);
  const equity = Number(manualContext?.equity) || 0;
  const buyingPower = Number(manualContext?.buying_power) || 0;
  const cost = shares * entry;
  const riskDollars = Number(result.ticket_risk);
  const openRisk = Number(manualContext?.heat?.open_risk);
  return {
    side,
    fromServer: true,
    sizeMode: result.size_mode,
    shares,
    entry,
    stopDistance: Number(result.stop_distance) || null,
    riskDollars: Number.isFinite(riskDollars) ? riskDollars : null,
    riskPct: Number(result.ai_risk_pct),
    stopPrice: Number.isFinite(stopPrice) ? stopPrice : null,
    stopLimitPrice:
      result.stop_limit_preview != null ? Number(result.stop_limit_preview) : null,
    stopLimitOffset: Number(result.stop_limit_offset_pct) || 0,
    targetPrice:
      result.take_profit_price != null ? Number(result.take_profit_price) : null,
    takeProfitR: Number(result.take_profit_r) || 0,
    cost,
    truncated: !!(result.qty_truncated || result.qty_whole_for_stop),
    usesAtr: Number(result.stop_distance) > 0,
    equity,
    projectedRiskPct:
      equity > 0 && Number.isFinite(openRisk) && Number.isFinite(riskDollars)
        ? ((openRisk + riskDollars) / equity) * 100
        : null,
    riskReward: manualRiskReward(
      riskDollars,
      result.take_profit_price != null ? Number(result.take_profit_price) : null,
      shares,
      entry
    ),
    // `attaches_stop` on the server also needs a stop to attach — a ticket
    // sent with the bracket off is a bare Market/Limit parent.
    attachesStop:
      ["market", "limit"].includes(String(result.order_type || "")) &&
      !result.extended_hours &&
      result.stop_preview != null,
    warnings: Array.isArray(result.warnings) ? result.warnings : [],
    breaches: Array.isArray(result.breaches) ? result.breaches : [],
    bpPct: buyingPower > 0 ? (cost / buyingPower) * 100 : null,
    exceedsBp: buyingPower > 0 && cost > buyingPower,
  };
}

/**
 * The estimate the panel should show right now.
 *
 * Prefers the server's answer when it still describes the current form; falls
 * back to the local mirror while a preview is in flight so the numbers never
 * blank out mid-typing.
 */
function currentEstimate() {
  if (manualServerPreview && manualServerPreview.key === manualPreviewKey()) {
    // A preview that came back without a usable size (a refused ticket, say)
    // must not blank the panel — the local mirror still has something to say.
    if (manualServerPreview.estimate) return manualServerPreview.estimate;
  }
  return calculateSizeEstimate();
}

/** True when the last server preview still describes what is on the form. */
function hasFreshServerPreview() {
  return !!manualServerPreview && manualServerPreview.key === manualPreviewKey();
}

/**
 * Ask the desk to size the ticket for real, debounced.
 *
 * `preview` never touches the broker, so this is safe to fire on typing. It is
 * skipped whenever the form is not yet valid — a 400 per keystroke would be
 * noise, and the local mirror is already showing something sensible.
 */
function scheduleServerPreview() {
  clearTimeout(manualPreviewTimer);
  manualPreviewTimer = setTimeout(() => {
    refreshServerPreview().catch(() => {});
  }, MANUAL_PREVIEW_DEBOUNCE_MS);
}

async function refreshServerPreview() {
  if (loopRunning || !manualContext) return;
  if (manualPreviewInFlight) {
    manualPreviewPendingRerun = true;
    return;
  }
  if (validateManualLocal()) return;
  const key = manualPreviewKey();
  if (manualServerPreview?.key === key) return;
  manualPreviewInFlight = true;
  manualPreviewPendingRerun = false;
  try {
    const payload = { ...manualPayload(), preview: true, confirm_adjusted_qty: true };
    const data = await api("/api/order", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const result = data.result || {};
    manualServerPreview = { key, result, estimate: estimateFromServer(result) };
    updateSizeEstimate();
    renderBreaches(result.breaches);
  } catch {
    // A preview that fails leaves the local mirror in place: it is an estimate
    // either way, and an error banner on every keystroke helps nobody.
  } finally {
    manualPreviewInFlight = false;
    if (manualPreviewPendingRerun) {
      manualPreviewPendingRerun = false;
      scheduleServerPreview();
    }
  }
}

function updateSizeEstimate() {
  const labelEl = $("manual-estimate-label");
  const valueEl = $("manual-estimate-value");
  const grid = $("manual-estimate-grid");
  const note = $("manual-estimate-note");
  if (!valueEl || !grid || !note) return;

  const calc = currentEstimate();
  manualLastEstimate = calc;
  // A pinned sell limit rides the stop wherever the latest sizing put it. Only
  // a real move re-prices, so this settles after one round rather than looping.
  if (syncStopLimitPin(calc)) {
    validateManualField("stop_limit_price");
    saveManualFormDraft();
    scheduleServerPreview();
  }
  const setCell = (id, text) => {
    const el = $(id);
    if (el) el.textContent = text;
  };
  // A visible marker for which layer produced these numbers: the desk's own
  // sizing, or the browser's estimate of it while a preview is in flight.
  const panel = $("manual-size-estimate");
  if (panel) panel.dataset.source = calc?.fromServer ? "desk" : "local";
  if (labelEl) {
    labelEl.textContent =
      tx("order_preview", "Order preview") +
      (calc && !calc.fromServer ? ` · ${tx("estimate_local_badge", "est.")}` : "");
  }

  if (!calc || calc.blocked) {
    valueEl.textContent = "—";
    grid.hidden = true;
    const bracketVisualizer = $("manual-bracket-visualizer");
    if (bracketVisualizer) bracketVisualizer.hidden = true;
    const inlineBadge = $("manual-inline-sizing-badge");
    if (inlineBadge) inlineBadge.hidden = true;
    const blockedMsg = calc?.blockedMessage;
    if (blockedMsg) {
      if (
        blockedMsg.includes("credentials") ||
        blockedMsg.includes("API Keys") ||
        blockedMsg.includes("API keys")
      ) {
        note.innerHTML = `<span class="estimate-warn-text">⚠️ ${escapeHtml(blockedMsg)}</span> <a href="/api-keys" class="estimate-link">${escapeHtml(tx("nav_api_keys", "API Keys"))} →</a>`;
      } else {
        note.textContent = blockedMsg;
      }
    } else {
      note.textContent = tx("estimate_pending", "Enter a symbol and the desk will size the ticket.");
    }
    note.classList.toggle("warn", !!calc?.blocked);
    announceEstimate(valueEl.textContent, note.textContent);
    syncManualPlaceButtons();
    return;
  }

  note.classList.remove("warn");
  grid.hidden = false;
  applyEstimateGridMode(
    calc.isShortEntry ? "short" : calc.isExit ? "exit" : "entry",
    calc
  );

  if (calc.isShortEntry) {
    valueEl.textContent = tx("estimate_short_value", "Short {shares} shares ≈ {credit}", {
      shares: formatQty(calc.shares),
      credit: money(calc.proceeds),
    });
    setCell("est-cost", money(calc.proceeds));
    setCell("est-bp", calc.bpPct != null ? `${calc.bpPct.toFixed(1)}%` : "—");
    const bracketVisualizer = $("manual-bracket-visualizer");
    if (bracketVisualizer) bracketVisualizer.hidden = true;
    const inlineBadge = $("manual-inline-sizing-badge");
    if (inlineBadge) inlineBadge.hidden = true;
    const shortNotes = [
      tx(
        "estimate_short_open",
        "Opens a new short — you are borrowing {shares} shares to sell. Losses are open-ended until you buy them back.",
        { shares: formatQty(calc.shares) }
      ),
    ];
    if (calc.truncated) {
      shortNotes.push(
        tx(
          "estimate_whole_shares_short",
          "Rounded down to whole shares — Alpaca does not short fractions."
        )
      );
    }
    if (calc.exceedsBp) {
      shortNotes.push(
        tx("estimate_over_bp", "Estimated cost exceeds buying power — Alpaca may reject this ticket.")
      );
    }
    note.textContent = shortNotes.join(" ");
    note.classList.add("warn");
    announceEstimate(valueEl.textContent, note.textContent);
    syncManualPlaceButtons();
    return;
  }

  if (calc.isExit) {
    valueEl.textContent = tx("estimate_sell_value", "Sell {shares} shares ≈ {proceeds}", {
      shares: formatQty(calc.shares),
      proceeds: money(calc.proceeds),
    });
    // An exit has no stop, target or risk of its own; the four cells that used
    // to read "—" are hidden, and the two that remain say what they mean —
    // this grid used to label proceeds "Est. cost" and a share count
    // "% of buying power".
    setCell("est-cost", money(calc.proceeds));
    setCell("est-bp", `${formatQty(calc.remaining)} ${tx("shares", "shares")}`);
    const bracketVisualizer = $("manual-bracket-visualizer");
    if (bracketVisualizer) bracketVisualizer.hidden = true;
    const inlineBadge = $("manual-inline-sizing-badge");
    if (inlineBadge) inlineBadge.hidden = true;
    note.textContent = calc.rearms
      ? tx(
          "estimate_exit_partial",
          "Closes {qty} of {held} shares. The resting stop is cancelled first, then re-armed over the {left} you keep.",
          {
            qty: formatQty(calc.shares),
            held: formatQty(calc.held),
            left: formatQty(calc.remaining),
          }
        )
      : tx(
          "estimate_exit_full",
          "Closes the whole {held}-share position and cancels the resting stop with it.",
          { held: formatQty(calc.held) }
        );
    announceEstimate(valueEl.textContent, note.textContent);
    syncManualPlaceButtons();
    return;
  }

  valueEl.textContent = tx("estimate_buy_value", "≈ {shares} shares @ {price}", {
    shares: formatQty(calc.shares),
    price: stockPrice(calc.entry),
  });
  setCell("est-cost", money(calc.cost));
  // The stop price alone hides how tight the stop is — the distance is what
  // decides whether market noise takes the trade out.
  const stopAway =
    calc.entry > 0 && calc.stopPrice != null
      ? (Math.abs(calc.stopPrice - calc.entry) / calc.entry) * 100
      : null;
  setCell(
    "est-stop",
    calc.stopPrice == null
      ? "—"
      : stopAway != null
        ? `${stockPrice(calc.stopPrice)} · −${stopAway.toFixed(2)}%`
        : stockPrice(calc.stopPrice)
  );
  setCell(
    "est-stop-limit",
    calc.stopLimitPrice != null
      ? `${stockPrice(calc.stopLimitPrice)}${
          calc.stopLimitOffset > 0
            ? ` · −${Number(calc.stopLimitOffset).toFixed(1)}%`
            : ""
        }`
      : calc.stopPrice != null
        ? tx("stop_limit_market", "market")
        : "—"
  );
  const riskOfEquity =
    calc.equity > 0 && calc.riskDollars != null
      ? (calc.riskDollars / calc.equity) * 100
      : null;
  setCell(
    "est-risk",
    calc.riskDollars == null
      ? "—"
      : riskOfEquity != null
        ? `${money(calc.riskDollars)} (${riskOfEquity.toFixed(2)}%)`
        : money(calc.riskDollars)
  );
  setCell(
    "est-target",
    calc.targetPrice != null
      ? `${stockPrice(calc.targetPrice)} · ${calc.takeProfitR}R`
      : tx("target_off", "off")
  );
  setCell(
    "est-rr",
    calc.riskReward
      ? tx("risk_reward_value", "{ratio}:1 · {reward} up", {
          ratio: calc.riskReward.ratio.toFixed(2),
          reward: money(calc.riskReward.reward),
        })
      : tx("target_off", "off")
  );
  setCell("est-bp", calc.bpPct != null ? `${calc.bpPct.toFixed(1)}%` : "—");

  // Update Visual Bracket Diagram
  const bracketVisualizer = $("manual-bracket-visualizer");
  if (bracketVisualizer) {
    // A take-profit of 0 sends a stop-only bracket — a real, common choice,
    // not an incomplete one, so entry + stop still earn a diagram.
    if (!calc.isExit && !calc.isShortEntry && manualBracketEnabled() && calc.entry > 0 && calc.stopPrice > 0) {
      const hasTarget = calc.targetPrice > 0;
      bracketVisualizer.hidden = false;
      const targetTier = bracketVisualizer.querySelector(".target-tier");
      if (targetTier) targetTier.hidden = !hasTarget;
      const bTargetVal = $("bracket-target-val");
      const bTargetPct = $("bracket-target-pct");
      const bEntryVal = $("bracket-entry-val");
      const bStopVal = $("bracket-stop-val");
      const bStopPct = $("bracket-stop-pct");
      const bBarStop = $("bracket-bar-stop");
      const bBarTarget = $("bracket-bar-target");
      const bRiskDollars = $("bracket-risk-dollars");
      const bRewardDollars = $("bracket-reward-dollars");
      const bRrRatio = $("bracket-rr-ratio");

      const targetGainPct = hasTarget
        ? ((calc.targetPrice - calc.entry) / calc.entry) * 100
        : null;
      const stopLossPct = ((calc.stopPrice - calc.entry) / calc.entry) * 100;

      if (hasTarget) {
        if (bTargetVal) bTargetVal.textContent = stockPrice(calc.targetPrice);
        if (bTargetPct) {
          bTargetPct.textContent = `${targetGainPct >= 0 ? "+" : ""}${targetGainPct.toFixed(1)}%`;
          bTargetPct.className = `bracket-tier-pct ${targetGainPct >= 0 ? "pos" : "neg"}`;
        }
      }
      if (bEntryVal) bEntryVal.textContent = stockPrice(calc.entry);
      if (bStopVal) bStopVal.textContent = stockPrice(calc.stopPrice);
      if (bStopPct) {
        bStopPct.textContent = `${stopLossPct >= 0 ? "+" : ""}${stopLossPct.toFixed(1)}%`;
        bStopPct.className = `bracket-tier-pct ${stopLossPct >= 0 ? "pos" : "neg"}`;
      }

      // Prefer the ratio the risk engine actually computed; the geometric
      // fallback still reads off the same percentages drawn above it, so the
      // bar never shows a proportion nothing on screen supports.
      const derivedRatio = hasTarget
        ? (calc.riskReward?.ratio ??
          (Math.abs(stopLossPct) > 0 ? Math.abs(targetGainPct) / Math.abs(stopLossPct) : null))
        : null;
      const ratio = Number.isFinite(derivedRatio) && derivedRatio > 0 ? derivedRatio : null;
      const stopRatioPct = !hasTarget ? 100 : ratio ? Math.max(15, Math.min(60, Math.round(100 / (1 + ratio)))) : 50;
      const targetRatioPct = 100 - stopRatioPct;

      if (bBarStop) bBarStop.style.width = `${stopRatioPct}%`;
      if (bBarTarget) bBarTarget.style.width = `${targetRatioPct}%`;

      if (bRiskDollars) bRiskDollars.textContent = calc.riskDollars != null ? `-${money(calc.riskDollars)}` : "—";
      if (bRewardDollars) {
        bRewardDollars.textContent = hasTarget && calc.riskReward?.reward != null
          ? `+${money(calc.riskReward.reward)}`
          : "";
        bRewardDollars.hidden = !hasTarget;
      }
      if (bRrRatio) {
        bRrRatio.textContent = ratio
          ? tx("bracket_rr_label", "1 : {ratio} R:R", { ratio: ratio.toFixed(1) })
          : hasTarget
            ? ""
            : tx("stop_only_bracket", "Stop only");
      }
    } else {
      bracketVisualizer.hidden = true;
    }
  }

  // Update Inline Sizing Badge
  const inlineBadge = $("manual-inline-sizing-badge");
  if (inlineBadge) {
    if (!calc.isExit && calc.shares > 0 && calc.cost > 0) {
      inlineBadge.hidden = false;
      const bracketOn = manualBracketEnabled();
      const stopDist = bracketOn && calc.stopPrice != null && calc.entry > 0
        ? ` · ${escapeHtml(tx("stop_price", "Stop"))}: <strong>${stockPrice(calc.stopPrice)}</strong>`
        : "";
      const riskDist = bracketOn && calc.riskDollars != null
        ? ` · ${escapeHtml(tx("max_risk", "Max risk"))}: <strong>${money(calc.riskDollars)}</strong>`
        : "";
      inlineBadge.innerHTML = `
        <span class="badge-stat"><strong>${formatQty(calc.shares)}</strong> ${tx("shares", "shares")} (${money(calc.cost)})</span>
        <span class="badge-stat">${riskDist}${stopDist}</span>
      `;
    } else {
      inlineBadge.hidden = true;
    }
  }

  const notes = [];
  // Only a ticket that is being sized off a stop can be missing an ATR. With
  // the Protective Bracket switched off there is no stop distance to derive,
  // so this warned — and painted the whole panel orange — about a number the
  // ticket never wanted.
  if (!calc.usesAtr && calc.stopPrice != null && manualBracketEnabled()) {
    notes.push(
      tx(
        "estimate_no_atr",
        "No ATR available — sized from the flat stop % instead of volatility."
      )
    );
  }
  if (calc.truncated) {
    notes.push(
      calc.attachesStop
        ? tx(
            "estimate_whole_shares",
            "Rounded down to whole shares — a protective stop cannot attach to a fractional order."
          )
        : tx(
            "estimate_whole_shares_asset",
            "Rounded down to whole shares — this symbol is not fractionable at Alpaca."
          )
    );
  }
  if (calc.exceedsBp) {
    notes.push(
      tx("estimate_over_bp", "Estimated cost exceeds buying power — Alpaca may reject this ticket.")
    );
  }
  // The single most useful line on the panel for someone holding a book: what
  // this ticket does to total exposure, not just to itself.
  if (calc.projectedRiskPct != null) {
    notes.push(
      tx("estimate_total_heat", "Open risk after this ticket: {pct}% of equity.", {
        pct: calc.projectedRiskPct.toFixed(2),
      })
    );
  }
  note.textContent = notes.join(" ");
  note.classList.toggle(
    "warn",
    calc.exceedsBp || (!calc.usesAtr && calc.stopPrice != null)
  );
  announceEstimate(valueEl.textContent, note.textContent);
  syncManualPlaceButtons();
}

/** Cells that only mean something for an entry. */
const MANUAL_ENTRY_ONLY_ROWS = [
  "est-row-stop",
  "est-row-stop-limit",
  "est-row-risk",
  "est-row-target",
  "est-row-rr",
];

/**
 * Relabel the preview grid for what this ticket actually does.
 *
 * The labels were static while only the values swapped, so a sell reported its
 * proceeds under "Est. cost" and its remaining share count under
 * "% of buying power". A short is a third case: it takes in a credit like an
 * exit, but consumes buying power like an entry, and it leaves no "Remaining".
 *
 * ``mode`` is one of "entry", "exit", "short".
 */
function applyEstimateGridMode(mode, calc) {
  const isExit = mode === "exit";
  const isShort = mode === "short";
  const hasBracket = !isExit && !isShort && manualBracketEnabled();
  const hasTarget = hasBracket && Number(calc?.targetPrice) > 0;
  const hasStopLimit = hasBracket && Number(calc?.stopLimitPrice) > 0;

  const rowStop = $("est-row-stop");
  if (rowStop) rowStop.hidden = isExit || isShort || !hasBracket;

  const rowStopLimit = $("est-row-stop-limit");
  if (rowStopLimit) rowStopLimit.hidden = isExit || isShort || !hasBracket || !hasStopLimit;

  const rowRisk = $("est-row-risk");
  if (rowRisk) rowRisk.hidden = isExit || isShort || !hasBracket;

  const rowTarget = $("est-row-target");
  if (rowTarget) rowTarget.hidden = isExit || isShort || !hasBracket || !hasTarget;

  const rowRr = $("est-row-rr");
  if (rowRr) rowRr.hidden = isExit || isShort || !hasBracket || !hasTarget;

  const costLabel = $("est-cost-label");
  if (costLabel) {
    costLabel.textContent = isShort
      ? tx("est_credit", "Est. credit")
      : isExit
        ? tx("est_proceeds", "Est. proceeds")
        : tx("est_cost", "Est. cost");
  }
  const bpLabel = $("est-bp-label");
  if (bpLabel) {
    bpLabel.textContent = isExit
      ? tx("remaining_position", "Remaining")
      : tx("pct_buying_power", "% of buying power");
  }
}

/**
 * One sentence for assistive tech.
 *
 * The panel itself used to be `aria-live`, so every keystroke re-read the whole
 * six-cell grid. Only the headline and its note are worth announcing.
 */
function announceEstimate(value, noteText) {
  const el = $("manual-estimate-live");
  if (!el) return;
  const next = [value, noteText].filter(Boolean).join(". ");
  if (el.textContent !== next) el.textContent = next;
}

function formatBreachMessage(breach) {
  const params = breach?.params || {};
  const messages = {
    daily_loss: [
      "breach_daily_loss",
      "Daily loss limit hit ({actual}% ≤ −{limit}%) — the desk would stop taking new risk today.",
    ],
    max_positions: [
      "breach_max_positions",
      "Max concurrent positions reached ({current}/{limit}).",
    ],
    spread: [
      "breach_spread",
      "Spread {actual} bps is above the desk limit of {limit} bps.",
    ],
    cooldown: [
      "breach_cooldown",
      "Stopped out {age}m ago — the desk cooldown is {limit}m.",
    ],
    portfolio_heat: [
      "breach_portfolio_heat",
      "Open risk would reach {projected}% of equity, past the desk budget of {budget}%.",
    ],
  };
  const [key, fallback] = messages[String(breach?.code || "")] || [];
  return key ? tx(key, fallback, params) : String(breach?.message || "");
}

/** Desk limits this ticket would cross, listed above the submit button. */
function renderBreaches(breaches) {
  const el = $("manual-breaches");
  if (!el) return;
  const rows = Array.isArray(breaches) ? breaches : [];
  manualPendingBreaches = rows;
  if (!rows.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = rows
    .map(
      (b) =>
        `<li data-code="${escapeHtml(String(b.code || ""))}">${escapeHtml(
          formatBreachMessage(b)
        )}</li>`
    )
    .join("");
}

function validateManualField(fieldName) {
  const form = $("manual-order");
  const field = form?.elements?.[fieldName];
  if (!field || field instanceof RadioNodeList) return;

  let error = null;
  const raw = String(field.value ?? "").trim();
  const val = Number(field.value);

  if (fieldName === "ai_risk_pct") {
    if (!(val > 0)) error = tx("err_field_gt_zero_pct", "Must be greater than 0%");
    else if (val > 10) error = tx("err_field_max_10_pct", "Max 10%");
  } else if (fieldName === "ai_atr_stop_mult") {
    if (!(val >= MIN_ATR_STOP_MULT)) error = tx("err_field_min_atr", "Min 0.1");
    else if (val > MAX_ATR_STOP_MULT) error = tx("err_field_max_10", "Max 10");
  } else if (fieldName === "take_profit_r") {
    if (val < 0) error = tx("err_field_gte_zero", "Cannot be negative");
    else if (val > 20) error = tx("err_field_max_20", "Max 20");
  } else if (fieldName === "stop_limit_offset_pct") {
    if (val < 0) error = tx("err_field_gte_zero", "Cannot be negative");
    else if (val > 50) error = tx("err_field_max_50", "Max 50%");
  } else if (fieldName === "stop_limit_price") {
    // `val` is a Number, so the old `val !== ""` was always true and an empty
    // box — the documented way to say "no absolute limit" — read as invalid.
    if (raw !== "" && !(val > 0)) {
      error = tx("err_field_gt_zero", "Must be greater than 0");
    } else if (raw !== "") {
      // The same rule the form validator and the desk enforce, checked here so
      // an above-the-stop limit is caught while it is being typed rather than
      // at Preview — and so a pinned value that went stale says so.
      const stopPx = Number(currentEstimate()?.stopPrice);
      if (stopPx > 0 && val > stopPx) {
        error = tx("err_field_at_or_below_stop", "At or below {stop}", {
          stop: stockPrice(stopPx),
        });
      }
    }
  } else if (fieldName === "notional") {
    if (!(val > 0)) error = tx("err_field_gt_zero", "Must be greater than 0");
  } else if (fieldName === "buy_qty") {
    if (!(val > 0)) error = tx("err_field_gt_zero", "Must be greater than 0");
  } else if (fieldName === "sell_qty") {
    const held = manualPositionQty();
    if (!(val > 0)) error = tx("err_field_gt_zero", "Must be greater than 0");
    // Flat, this box sizes a short, not a close — there is no holding to
    // measure it against, only the whole-share borrow.
    else if (manualOpensShort()) {
      if (val < 1) error = tx("err_field_min_one_share", "At least 1 whole share");
    } else if (val > held + 1e-9) {
      error = tx("err_field_max_held", "You hold {held}", { held: formatQty(held) });
    }
  } else if (fieldName === "sell_notional") {
    const held = manualPositionQty();
    const px = manualExitFillPrice();
    if (!(val > 0)) error = tx("err_field_gt_zero", "Must be greater than 0");
    else if (manualOpensShort()) {
      if (px > 0 && val / px < 1) {
        error = tx("err_field_min_one_share", "At least 1 whole share");
      }
    } else if (px > 0 && val / px > held + 1e-9) {
      error = tx("err_sell_notional_too_much", "That is more than this position is worth ({value}).", {
        value: money(held * px),
      });
    }
  } else if (fieldName === "reinvest_qty" || fieldName === "reinvest_limit_price") {
    // Only meaningful once the buy-back is armed — an empty box is not an error.
    if (manualReinvestEnabled() && !(val > 0)) {
      error = tx("err_field_gt_zero", "Must be greater than 0");
    }
  } else if (fieldName === "reinvest_expire_minutes") {
    if (manualReinvestEnabled()) {
      if (!(val >= 1)) error = tx("err_field_min_1", "Minimum 1 minute");
      else if (val > 1440) error = tx("err_field_max_1440", "Max 1440 (24h)");
    }
  } else if (fieldName === "followon_qty") {
    if (manualFollowOnEnabled() && !(val > 0)) {
      error = tx("err_field_gt_zero", "Must be greater than 0");
    }
  } else if (fieldName === "followon_limit_price") {
    if (
      manualFollowOnEnabled() &&
      manualFollowOnOrderType() !== "market" &&
      !(val > 0)
    ) {
      error = tx("err_field_gt_zero", "Must be greater than 0");
    }
  } else if (fieldName === "followon_target_symbol") {
    if (manualFollowOnEnabled() && manualFollowOnKind() === "rotate") {
      const target = String(field.value || "").trim().toUpperCase();
      const closeSymbol = String(manualFormValue("symbol", "") || "").trim().toUpperCase();
      if (!/^[A-Z.\-]{1,12}$/.test(target)) {
        error = tx("err_followon_symbol", "Enter the symbol the next ticket should buy.");
      } else if (target === closeSymbol) {
        error = tx(
          "err_followon_same_symbol",
          "Buy another stock needs a different symbol than the one you are closing."
        );
      }
    }
  } else if (fieldName === "dip_hunt_wait_minutes") {
    if (manualDipHuntEnabled()) {
      if (!(val >= 1)) error = tx("err_field_min_1", "Minimum 1 minute");
      else if (val > 1440) error = tx("err_field_max_1440", "Max 1440 (24h)");
    }
  } else if (fieldName === "dip_hunt_pct") {
    if (manualDipHuntEnabled()) {
      if (!(val > 0)) error = tx("err_field_gt_zero_pct", "Must be greater than 0%");
      else if (val > 50) error = tx("err_field_max_50", "Max 50%");
    }
  } else if (fieldName === "trail_percent") {
    if (manualNeedsTrail()) {
      if (!(val > 0)) error = tx("err_field_gt_zero", "Must be greater than 0");
      else if (val > 50) error = tx("err_field_max_50", "Max 50%");
    }
  } else if (fieldName === "stop_price") {
    if (manualNeedsTrigger() && !(val > 0)) {
      error = tx("err_field_gt_zero", "Must be greater than 0");
    }
  } else if (fieldName === "limit_price") {
    if (manualNeedsLimit() && !(val > 0)) {
      error = tx("err_field_gt_zero", "Must be greater than 0");
    }
  }

  // The label is where every field keeps its error line, but four price fields
  // kept theirs one level up in the row — the message was computed and then
  // rendered nowhere. Falling back to the row makes that unable to recur.
  // (Only `.manual-limit-row` — a `.field-pair-row` holds two fields and would
  // hand back the neighbour's error line.)
  const errorEl =
    field.parentElement?.querySelector(".field-error") ||
    field.closest(".manual-limit-row")?.querySelector(".field-error") ||
    field.closest(".qty-field-input")?.querySelector(".field-error");
  if (errorEl) {
    if (!errorEl.id) errorEl.id = `manual-field-error-${fieldName.replaceAll("_", "-")}`;
    const describedBy = new Set(
      String(field.getAttribute("aria-describedby") || "")
        .split(/\s+/)
        .filter(Boolean)
    );
    describedBy.add(errorEl.id);
    field.setAttribute("aria-describedby", [...describedBy].join(" "));
    errorEl.hidden = !error;
    errorEl.textContent = error || "";
  }
  field.setAttribute("aria-invalid", error ? "true" : "false");
}

function syncManualSideUi() {
  const side = manualSide();
  const isExit = manualIsExit();
  const form = $("manual-order");
  if (form) form.dataset.side = side;
  // The preview lives in the rail, outside the form, so the side has to be
  // published on the layout for the buy/sell colouring to reach it.
  const layout = $("page-manual-order");
  if (layout) layout.dataset.side = side;
  const sellGroup = $("manual-sell-group");
  const buySizingBlock = $("manual-buy-sizing-block");
  const riskGroup = $("manual-risk-group");
  const dipHuntGroup = $("manual-dip-hunt-group");
  const isBuy = side === "buy";
  const typeAllowed = ["market", "limit"].includes(manualOrderType());
  if (sellGroup) sellGroup.hidden = !isExit;
  if (buySizingBlock) buySizingBlock.hidden = isExit;
  if (riskGroup) riskGroup.hidden = !isBuy || !typeAllowed;
  if (dipHuntGroup) dipHuntGroup.hidden = !isBuy || !typeAllowed || !manualBracketEnabled();

  const sideInputs = form?.elements?.side;
  if (sideInputs instanceof RadioNodeList) {
    [...sideInputs].forEach((input) => {
      input.disabled = loopRunning || busy;
      const segment = input.closest(".segment");
      segment?.classList.remove("is-disabled");
      segment?.removeAttribute("tabindex");
      segment?.removeAttribute("aria-disabled");
    });
  }

  maybeAutofillSellQty();

  const sellMode = manualSellMode();
  const sharesMode = sellMode !== "dollars";
  const sellLegend = $("manual-sell-legend");
  if (sellLegend) {
    sellLegend.setAttribute("for", sharesMode ? "manual-sell-qty" : "manual-sell-notional");
  }
  const qtyLabel = $("manual-sell-qty-label");
  if (qtyLabel) qtyLabel.hidden = !sharesMode;
  // Flat, the Sell box sizes a short: there is no holding to cap it against,
  // and capping at 0 made the field reject every number the user typed.
  const opensShort = manualOpensShort();
  const qtyInput = $("manual-sell-qty");
  if (qtyInput) {
    qtyInput.disabled = !sharesMode || loopRunning || busy;
    qtyInput.max = opensShort ? "" : String(manualPositionQty() || 0);
  }
  const dollarLabel = $("manual-sell-notional-label");
  if (dollarLabel) dollarLabel.hidden = sharesMode;
  const dollarInput = $("manual-sell-notional");
  if (dollarInput) {
    dollarInput.disabled = sharesMode || loopRunning || busy;
    const held = manualPositionQty();
    const px = manualExitFillPrice();
    dollarInput.max = !opensShort && held > 0 && px > 0 ? String(held * px) : "";
  }

  const sellAvail = $("manual-sell-avail");
  if (sellAvail && isExit && opensShort) {
    const shortable = manualContext?.asset?.shortable;
    sellAvail.textContent =
      shortable === false
        ? tx("qty_not_shortable", "not shortable")
        : tx("qty_opens_short", "opens a short");
    sellAvail.classList.toggle("is-empty", shortable === false);
  } else if (sellAvail && isExit) {
    const held = manualPositionQty();
    const px = manualExitFillPrice();
    const shown =
      sellMode === "dollars" && held > 0 && px > 0
        ? money(held * px)
        : formatQty(held);
    sellAvail.textContent = tx("qty_available", "{qty} available", { qty: shown });
    sellAvail.classList.toggle("is-empty", !(held > 0));
  } else if (sellAvail) {
    sellAvail.textContent = "";
    sellAvail.classList.remove("is-empty");
  }
  syncSellFillButtons();
  syncSellUnitToggle();

  // The partial-exit note only applies when something is actually left over.
  const partialNote = $("manual-partial-note");
  if (partialNote) {
    const held = manualPositionQty();
    const qty = manualSellQty();
    partialNote.hidden = !isExit || !(held > 0) || !(qty > 0) || qty >= held - 1e-9;
  }

  // Sizing mode swaps the input; the ATR stop column never moves.
  const buyMode = manualBuySizeMode();
  const buyLegend = $("manual-buy-legend");
  if (buyLegend) {
    buyLegend.setAttribute(
      "for",
      buyMode === "notional"
        ? "manual-notional"
        : buyMode === "qty"
          ? "manual-buy-qty"
          : "manual-ai-risk-pct"
    );
  }

  const buyAvail = $("manual-buy-avail");
  if (buyAvail && !isExit) {
    const bp = Number(manualContext?.buying_power);
    if (bp > 0) {
      buyAvail.textContent = tx("qty_available", "{qty} available", { qty: money(bp) });
      buyAvail.classList.remove("is-empty");
    } else {
      buyAvail.textContent = "";
      buyAvail.classList.remove("is-empty");
    }
  } else if (buyAvail) {
    buyAvail.textContent = "";
    buyAvail.classList.remove("is-empty");
  }

  const buyModeInputs = form?.elements?.buy_size_mode;
  if (buyModeInputs instanceof RadioNodeList) {
    [...buyModeInputs].forEach((input) => {
      input.disabled = isExit || loopRunning || busy;
    });
  }

  const modeFields = {
    risk: $("manual-risk-pct-label"),
    notional: $("manual-notional-label"),
    qty: $("manual-buy-qty-label"),
  };
  Object.entries(modeFields).forEach(([mode, label]) => {
    if (!label) return;
    label.hidden = isExit || mode !== buyMode;
    const input = label.querySelector("input");
    if (input) input.disabled = label.hidden || loopRunning || busy;
    label.querySelectorAll("[data-notional-fill]").forEach((btn) => {
      btn.disabled = label.hidden || loopRunning || busy || !(Number(manualContext?.buying_power) > 0);
    });
  });
  const modeHelp = $("manual-size-mode-help");
  if (modeHelp && !isExit) {
    modeHelp.textContent =
      buyMode === "notional"
        ? tx(
            "size_help_dollars",
            "Shares = your dollar amount ÷ mark, rounded down. The ATR stop still sets where the trade is wrong."
          )
        : buyMode === "qty"
          ? tx(
              "size_help_shares",
              "You choose the share count; the ATR stop still sets where the trade is wrong, so watch Max risk."
            )
          : tx(
              "manual_risk_help_short",
              "Shares are set so a stop-out costs your risk budget: equity × risk % ÷ (ATR × multiplier). More volatility means fewer shares for the same dollar risk."
            );
  }

  // One sentence per action, so the button's meaning is never inferred.
  const sideHelp = $("manual-side-help");
  if (sideHelp) {
    const helps = {
      buy: tx(
        "manual_side_help_buy",
        "Buy opens or adds to a long and sizes from your risk budget."
      ),
      sell: tx(
        "manual_side_help_sell",
        "Sell closes part or all of a long. A partial sell keeps a stop over what is left."
      ),
      short: tx(
        "manual_side_help_short",
        "With no long to close, Sell opens a short — borrowed shares sold now and bought back later."
      ),
    };
    sideHelp.textContent = helps[manualDeskAction()] || helps.buy;
  }

  syncManualBracketUi();
  syncManualReinvestUi();
  syncManualFollowOnUi();
  syncManualDipHuntUi();

  const sellHelp = $("manual-sell-help");
  if (sellHelp && isExit) {
    const held = manualPositionQty();
    if (opensShort) {
      // Flat, Sell is not a refusal — it is the short entry. Say which one it
      // is, and whether the borrow is actually available.
      let text =
        manualContext?.asset?.shortable === false
          ? tx(
              "manual_short_unavailable",
              "No long position here, and {symbol} is not shortable at Alpaca — the borrow is unavailable.",
              { symbol: manualSymbol() }
            )
          : tx(
              "manual_short_hint",
              "No long position here, so Sell opens a short: you borrow the shares and sell them, then buy them back to close. Whole shares only, and no protective stop is attached.",
              { symbol: manualSymbol() }
            );
      if (sellMode === "dollars") {
        const sizedQty = Math.floor(manualSellQty());
        const px = manualExitFillPrice();
        if (sizedQty > 0 && px > 0) {
          text +=
            " " +
            tx("help_sell_dollars_preview_short", "≈ {shares} shares at {price}.", {
              shares: formatQty(sizedQty),
              price: stockPrice(px),
            });
        }
      }
      sellHelp.textContent = text;
    } else {
      // Anything not opening a short is a real long to close: `opensShort`
      // already covers flat and already-short, so `held` is positive here.
      let text = tx(
        "manual_sell_hint",
        "Choose how much of the position to close. Any resting protective stop is cancelled first."
      );
      if (sellMode === "dollars") {
        const sizedQty = manualSellQty();
        const px = manualExitFillPrice();
        const dollars = Number(manualFormValue("sell_notional", ""));
        if (sizedQty > 0 && px > 0 && dollars > 0) {
          text +=
            " " +
            tx("help_sell_dollars_preview_short", "≈ {shares} shares at {price}.", {
              shares: formatQty(sizedQty),
              price: stockPrice(px),
            });
        }
      }
      sellHelp.textContent = text;
    }
    sellHelp.classList.toggle("warn", !held);
  }
}

/**
 * The buy-back block: visible on sells, expanded once armed.
 *
 * The summary line is the whole point of the block — it states the two prices
 * side by side, because a buy-back priced *above* the sell is a real order
 * that loses money on purpose and the user should see that before submitting.
 */
function syncManualReinvestUi() {
  const group = $("manual-reinvest-group");
  // A buy-back is armed against a sell that closes a long. A Sell that opens
  // a short has nothing to buy back, and the payload drops the plan — so the
  // card must not stay on screen inviting one.
  const isSell = manualSide() === "sell" && !manualOpensShort();
  if (group) group.hidden = !isSell;

  const enabled = manualReinvestEnabled();
  const fields = $("manual-reinvest-fields");
  if (fields) fields.hidden = !enabled;
  const toggle = $("manual-reinvest-enabled");
  if (toggle) toggle.disabled = !isSell || loopRunning || busy;

  const qtyLabel = $("manual-reinvest-qty-label");
  const custom = manualReinvestQtyMode() === "custom";
  if (qtyLabel) qtyLabel.hidden = !enabled || !custom;
  const qtyInput = $("manual-reinvest-qty");
  if (qtyInput) qtyInput.disabled = !enabled || !custom || loopRunning || busy;
  const limitInput = $("manual-reinvest-limit");
  if (limitInput) limitInput.disabled = !enabled || loopRunning || busy;
  const expireInput = $("manual-reinvest-expire");
  if (expireInput) expireInput.disabled = !enabled || loopRunning || busy;

  const offsetEl = $("manual-reinvest-offset");
  const summaryEl = $("manual-reinvest-summary");
  if (!enabled) {
    if (offsetEl) {
      offsetEl.textContent = "";
      offsetEl.classList.remove("warn");
    }
    if (summaryEl) {
      summaryEl.textContent = "";
      summaryEl.classList.remove("warn");
    }
    return;
  }

  const buyPrice = manualReinvestLimit();
  const sellPrice = manualSellReference();
  if (offsetEl) {
    if (buyPrice > 0 && sellPrice > 0) {
      const pct = ((buyPrice - sellPrice) / sellPrice) * 100;
      const above = pct >= 0;
      offsetEl.textContent = above
        ? tx("reinvest_offset_above", "{pct}% above the sell price ({price})", {
            pct: Math.abs(pct).toFixed(2),
            price: stockPrice(sellPrice),
          })
        : tx("reinvest_offset_below", "{pct}% below the sell price ({price})", {
            pct: Math.abs(pct).toFixed(2),
            price: stockPrice(sellPrice),
          });
      offsetEl.classList.toggle("warn", above);
    } else {
      offsetEl.textContent = tx(
        "reinvest_price_hint",
        "The price the buy-back rests at once the sell fills."
      );
      offsetEl.classList.remove("warn");
    }
  }

  if (summaryEl) {
    const qty = manualReinvestQty();
    if (qty > 0 && buyPrice > 0) {
      summaryEl.textContent = tx(
        "reinvest_summary",
        "Sell {sellQty} → then buy {buyQty} back at {buyPrice} (≈ {cost}).",
        {
          sellQty: formatQty(manualSellQty()),
          buyQty: formatQty(qty),
          buyPrice: stockPrice(buyPrice),
          cost: money(qty * buyPrice),
        }
      );
      summaryEl.classList.toggle("warn", sellPrice > 0 && buyPrice > sellPrice);
    } else {
      summaryEl.textContent = "";
      summaryEl.classList.remove("warn");
    }
  }
}

function syncManualFollowOnUi() {
  const group = $("manual-followon-group");
  // Same as the buy-back: a next ticket fires when a close fills, and a short
  // entry is not a close.
  const isExit = manualIsExit() && !manualOpensShort();
  if (group) group.hidden = !isExit;

  const enabled = manualFollowOnEnabled();
  const fields = $("manual-followon-fields");
  if (fields) fields.hidden = !enabled;
  const toggle = $("manual-followon-enabled");
  if (toggle) toggle.disabled = !isExit || loopRunning || busy;

  const kind = manualFollowOnKind();
  const rotate = kind === "rotate";
  const symbolLabel = $("manual-followon-symbol-label");
  if (symbolLabel) symbolLabel.hidden = !enabled || !rotate;
  const symbolInput = $("manual-followon-symbol");
  if (symbolInput) symbolInput.disabled = !enabled || !rotate || loopRunning || busy;

  const custom = manualFollowOnQtyMode() === "custom";
  const qtyLabel = $("manual-followon-qty-label");
  if (qtyLabel) qtyLabel.hidden = !enabled || !custom;
  const qtyInput = $("manual-followon-qty");
  if (qtyInput) qtyInput.disabled = !enabled || !custom || loopRunning || busy;
  const market = manualFollowOnOrderType() === "market";
  const limitLabel = $("manual-followon-limit-label");
  if (limitLabel) limitLabel.hidden = !enabled || market;
  const priceRow = $("manual-followon-price-row");
  if (priceRow) {
    priceRow.hidden = !enabled || market;
    priceRow.classList.toggle("is-market", market);
  }
  const limitInput = $("manual-followon-limit");
  if (limitInput) limitInput.disabled = !enabled || market || loopRunning || busy;
  const marketHint = $("manual-followon-market-hint");
  if (marketHint) marketHint.hidden = !enabled || !market;

  const helpEl = $("manual-followon-help");
  if (helpEl && isExit) {
    helpEl.textContent = !enabled
      ? tx(
          "followon_help",
          "When the close fills, the desk sends the next order at your price. Nothing is sent if the close never fills."
        )
      : rotate
        ? market
          ? tx(
              "followon_help_rotate_market",
              "When the close fills, the desk sends a market buy in a different symbol."
            )
          : tx(
              "followon_help_rotate",
              "When the close fills, the desk sends a limit buy in a different symbol."
            )
        : market
          ? tx(
              "followon_help_reverse_sell_market",
              "When the sell fills, the desk shorts this stock at the then-current price. The whole long must close first — Alpaca will not short while you are still long."
            )
          : tx(
              "followon_help_reverse_sell",
              "When the sell fills, the desk shorts this stock at your price. The whole long must close first — Alpaca will not short while you are still long."
            );
  }

  const held = manualPositionQty();
  const closeQty = manualSellQty();
  const partial = $("manual-followon-partial");
  if (partial) {
    partial.hidden =
      !enabled || rotate || !(held > 0) || !(closeQty > 0) || closeQty >= held - 1e-9;
  }

  const offsetEl = $("manual-followon-offset");
  const summaryEl = $("manual-followon-summary");
  if (!enabled) {
    if (offsetEl) {
      offsetEl.textContent = "";
      offsetEl.classList.remove("warn");
    }
    if (summaryEl) {
      summaryEl.textContent = "";
      summaryEl.classList.remove("warn");
    }
    return;
  }

  const nextPrice = manualFollowOnLimit();
  const closePrice = manualSellReference();
  if (offsetEl) {
    if (market) {
      offsetEl.textContent = "";
      offsetEl.classList.remove("warn");
    } else if (nextPrice > 0 && closePrice > 0) {
      const pct = ((nextPrice - closePrice) / closePrice) * 100;
      const above = pct >= 0;
      offsetEl.textContent = above
        ? tx("followon_offset_above", "{pct}% above the close price ({price})", {
            pct: Math.abs(pct).toFixed(2),
            price: stockPrice(closePrice),
          })
        : tx("followon_offset_below", "{pct}% below the close price ({price})", {
            pct: Math.abs(pct).toFixed(2),
            price: stockPrice(closePrice),
          });
      offsetEl.classList.toggle("warn", false);
    } else {
      offsetEl.textContent = tx(
        "followon_price_hint",
        "The price the next ticket rests at once the close fills."
      );
      offsetEl.classList.remove("warn");
    }
  }

  if (summaryEl) {
    const qty = manualFollowOnQty();
    const closeSymbol = String(manualFormValue("symbol", "") || "")
      .trim()
      .toUpperCase();
    const target = manualFollowOnTargetSymbol() || closeSymbol;
    const priced = market || nextPrice > 0;
    if (qty > 0 && priced && (!rotate || target)) {
      summaryEl.textContent = rotate
        ? market
          ? tx(
              "followon_summary_rotate_market",
              "Close {closeQty} {symbol} → then buy {nextQty} {target} at market.",
              {
                closeQty: formatQty(closeQty),
                symbol: closeSymbol || "—",
                nextQty: formatQty(qty),
                target: target || "—",
              }
            )
          : tx(
              "followon_summary_rotate",
              "Close {closeQty} {symbol} → then buy {nextQty} {target} at {price} (≈ {cost}).",
              {
                closeQty: formatQty(closeQty),
                symbol: closeSymbol || "—",
                nextQty: formatQty(qty),
                target: target || "—",
                price: stockPrice(nextPrice),
                cost: money(qty * nextPrice),
              }
            )
        : market
          ? tx(
              "followon_summary_reverse_sell_market",
              "Sell {closeQty} {symbol} → then short {nextQty} at market.",
              {
                closeQty: formatQty(closeQty),
                symbol: closeSymbol || "—",
                nextQty: formatQty(qty),
              }
            )
          : tx(
              "followon_summary_reverse_sell",
              "Sell {closeQty} {symbol} → then short {nextQty} at {price}.",
              {
                closeQty: formatQty(closeQty),
                symbol: closeSymbol || "—",
                nextQty: formatQty(qty),
                price: stockPrice(nextPrice),
              }
            );
      summaryEl.classList.remove("warn");
    } else {
      summaryEl.textContent = "";
      summaryEl.classList.remove("warn");
    }
  }
}

function syncManualBracketUi() {
  const group = $("manual-risk-group");
  const isBuy = manualSide() === "buy";
  const typeAllowed = ["market", "limit"].includes(manualOrderType());
  const shouldShow = isBuy && typeAllowed;
  if (group) group.hidden = !shouldShow;

  const form = $("manual-order");
  const toggle = form?.elements?.bracket_enabled;
  const isChecked = toggle ? toggle.checked : true;
  const enabled = shouldShow && isChecked;

  const fields = $("manual-bracket-fields");
  if (fields) fields.hidden = !enabled;
  if (toggle) toggle.disabled = !shouldShow || loopRunning || busy;

  const atrInput = $("manual-ai-atr-mult");
  if (atrInput) atrInput.disabled = !enabled || loopRunning || busy;
  const tpInput = $("manual-take-profit-r");
  if (tpInput) tpInput.disabled = !enabled || loopRunning || busy;
  const explicitStopLimit = Number(manualFormValue("stop_limit_price", "")) > 0;
  const stopLimitOffset = $("manual-stop-limit-offset");
  if (stopLimitOffset) {
    stopLimitOffset.disabled = !enabled || explicitStopLimit || loopRunning || busy;
    const offsetLabel = $("manual-stop-limit-label");
    if (offsetLabel) {
      offsetLabel.classList.toggle("is-disabled", explicitStopLimit);
    }
  }
  const stopLimitPrice = $("manual-stop-limit-price");
  if (stopLimitPrice) stopLimitPrice.disabled = !enabled || loopRunning || busy;
  const btnStopLimitAtStop = $("btn-stop-limit-at-stop");
  if (btnStopLimitAtStop) {
    const hasStop = Number(currentEstimate()?.stopPrice) > 0;
    btnStopLimitAtStop.disabled = !enabled || !hasStop || loopRunning || busy;
    btnStopLimitAtStop.classList.toggle("is-active", stopLimitPinnedToStop && hasStop);
    btnStopLimitAtStop.setAttribute("aria-pressed", stopLimitPinnedToStop && hasStop ? "true" : "false");
  }

  const badge = $("manual-bracket-summary-badge");
  if (badge) {
    if (!enabled) {
      badge.textContent = tx("bracket_off", "off");
    } else {
      const atrMult = Number(manualFormValue("ai_atr_stop_mult", 1.8) || 0);
      const tpR = Number(manualFormValue("take_profit_r", 2) || 0);
      const tpText = tpR > 0 ? `${tpR}R` : tx("stop_only_bracket", "Stop only");
      badge.textContent = `${atrMult > 0 ? `${atrMult}× ATR` : "Stop"} · ${tpText}`;
    }
  }

  syncBuyUnitToggle(enabled);
}

function syncBuyUnitToggle(bracketActive) {
  const form = $("manual-order");
  const riskUnit = form?.querySelector('.qty-unit input[value="risk"]')?.closest(".qty-unit");
  const riskInput = form?.querySelector('input[name="buy_size_mode"][value="risk"]');
  if (riskUnit && riskInput) {
    riskUnit.hidden = !bracketActive;
    riskInput.disabled = !bracketActive || loopRunning || busy;
  }
  if (!bracketActive && manualBuySizeMode() === "risk") {
    setManualFormValue("buy_size_mode", "notional");
    const notionalInput = form?.querySelector('input[name="buy_size_mode"][value="notional"]');
    if (notionalInput) notionalInput.checked = true;
  }
}

function syncManualDipHuntUi() {
  const group = $("manual-dip-hunt-group");
  const isBuy = manualSide() === "buy";
  const typeAllowed = ["market", "limit"].includes(manualOrderType());
  const bracketOn = manualBracketEnabled();
  const shouldShow = isBuy && typeAllowed && bracketOn;
  if (group) group.hidden = !shouldShow;

  const form = $("manual-order");
  const toggle = form?.elements?.dip_hunt_enabled;
  if (!shouldShow && toggle && toggle.checked) {
    toggle.checked = false;
  }
  const isChecked = toggle ? toggle.checked : false;
  const enabled = shouldShow && isChecked;

  const fields = $("manual-dip-hunt-fields");
  if (fields) fields.hidden = !enabled;
  if (toggle) toggle.disabled = !shouldShow || loopRunning || busy;

  const waitInput = $("manual-dip-hunt-wait");
  if (waitInput) waitInput.disabled = !enabled || loopRunning || busy;
  const pctInput = $("manual-dip-hunt-pct");
  if (pctInput) pctInput.disabled = !enabled || loopRunning || busy;

  const badge = $("manual-dip-hunt-summary-badge");
  if (badge) {
    if (!enabled) {
      badge.textContent = tx("target_off", "off");
    } else {
      const wait = manualDipHuntWaitMinutes();
      const dip = manualDipHuntPct();
      badge.textContent = `${wait}m / ${dip}%`;
    }
  }

  const summaryEl = $("manual-dip-hunt-summary");
  if (!summaryEl) return;
  if (!enabled) {
    summaryEl.textContent = "";
    return;
  }
  const wait = manualDipHuntWaitMinutes();
  const dip = manualDipHuntPct();
  const estimate = calculateSizeEstimate();
  const stopPx = Number(estimate?.stopPrice);
  let extra = "";
  if (Number.isFinite(stopPx) && stopPx > 0) {
    const target = stopPx * (1 - dip / 100);
    extra = tx("dip_hunt_summary_price", " Example from the mark: stop ~{stop} → buy at {buy}.", {
      stop: stockPrice(stopPx),
      buy: stockPrice(target),
    });
  }
  summaryEl.textContent =
    tx(
      "dip_hunt_summary",
      "After a stop-out, wait up to {wait} minutes for a further {dip}% drop — or buy immediately if that drop hits sooner. Then repeat.",
      { wait: String(wait), dip: String(dip) }
    ) + extra;
}

function selectManualSide(side) {
  const next = visibleTicketSide(side);
  if (!next || loopRunning || busy) return false;
  if (manualSide() === next) return false;
  setManualFormValue("side", next);
  formDirtyManual = true;
  if (manualContext) applyStockPriceDefaults(manualContext);
  saveManualFormDraft();
  syncManualUi();
  scheduleServerPreview();
  return true;
}

function syncManualPlaceButtons() {
  const side = manualSide();
  const locked = loopRunning || busy;
  const submitBtn = $("btn-manual-submit");
  const submitText = $("btn-submit-text");
  const submitPill = $("btn-submit-pill");
  const calc = currentEstimate();

  if (submitBtn) {
    submitBtn.disabled = locked;
    submitBtn.dataset.side = side;
    submitBtn.classList.toggle("is-buy", side === "buy");
    submitBtn.classList.toggle("is-sell", side === "sell");

    if (busy && manualBusyLabel) {
      if (submitText) submitText.textContent = manualBusyLabel;
      if (submitPill) submitPill.textContent = "";
    } else {
      if (submitText) {
        // The button names the action the desk will run, not the button the
        // ticket is standing on — "Place Sell Order" over a flat position was
        // about to open a short.
        submitText.textContent =
          {
            buy: tx("place_buy", "Place Buy Order"),
            sell: tx("place_sell", "Place Sell Order"),
            short: tx("place_short", "Place Short Order"),
          }[manualDeskAction()] || tx("place_buy", "Place Buy Order");
      }
      if (submitPill) {
        submitPill.textContent = "";
      }
    }
  }
}

function syncManualHelp() {
  const help = $("manual-help");
  syncManualPlaceButtons();
  syncManualBusyHint();
  if (!help) return;

  const queued = manualTicketQueuesForRth();
  const session = formatSession(manualContext?.session);
  const tif = manualTimeInForce().toUpperCase();
  const otype = manualOrderType();
  const typeLabel = manualOrderTypeLabel(otype);
  let text;
  let isWarn = false;
  if (manualExtendedHours()) {
    text = tx(
      "manual_help_offrth",
      "{session}: extended-hours Limit {tif} order.",
      { session, tif }
    );
    isWarn = true;
  } else if (queued) {
    text = tx(
      "manual_help_queued",
      "{session}: {type} {tif} order queued for regular hours.",
      { session, type: typeLabel, tif }
    );
    isWarn = true;
  } else if (otype === "trailing_stop") {
    text = tx(
      "manual_help_trailing",
      "Trailing {tif} order — the trigger follows the best price and never moves back.",
      { tif }
    );
  } else if (otype === "stop" || otype === "stop_limit") {
    text = tx(
      "manual_help_stop",
      "{tif} order that stays dormant until price reaches the trigger.",
      { tif }
    );
  } else if (otype === "limit") {
    text = tx("manual_help_limit_tif", "Limit {tif} order — it rests until price trades there.", {
      tif,
    });
  } else {
    text = tx("manual_help_market_tif", "Market {tif} order in regular hours.", { tif });
  }
  if (manualOpensShort()) {
    text += ` ${tx(
      "manual_help_short",
      "Opens a short — no protective stop is attached, and whole shares only."
    )}`;
    isWarn = true;
  } else if (manualIsExit()) {
    text += ` ${tx("manual_help_sell", "Protective stops are cancelled before a sell.")}`;
  } else if (manualAttachesStop()) {
    // Promised on every Market/Limit buy, including the ones with the
    // Protective Bracket switched off — which go out completely unprotected.
    text += ` ${tx("manual_help_buy", "A protective stop is attached to the fill.")}`;
  } else if (manualIsEntry() && !manualBracketEnabled()) {
    text += ` ${tx(
      "manual_help_buy_unprotected",
      "No protective stop — this buy goes out unbracketed."
    )}`;
  }
  help.textContent = text;
  // Info vs warning are different states — never let the page show both.
  help.classList.toggle("warn", isWarn);
  help.classList.toggle("info", !isWarn);
}

const MANUAL_FORM_STORAGE_KEY = "alpaca-desk-manual-order-form";

function readManualFormDraft() {
  try {
    const raw = localStorage.getItem(MANUAL_FORM_STORAGE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    return data && typeof data === "object" ? data : null;
  } catch {
    return null;
  }
}

/** Fields a draft or a preset is made of, and how to read each back. */
const MANUAL_SAVED_FIELDS = {
  side: ["buy", "sell"],
  order_type: ["market", "limit", "stop", "stop_limit", "trailing_stop"],
  time_in_force: ["day", "gtc", "ioc", "fok", "opg", "cls"],
  trading_session: ["24h", "regular"],
  sell_mode: ["custom", "dollars"],
  buy_size_mode: ["risk", "notional", "qty"],
  reinvest_qty_mode: ["match", "custom"],
  followon_kind: ["reverse", "rotate"],
  followon_qty_mode: ["match", "custom"],
  followon_order_type: ["limit", "market"],
};
/** Free-form numeric fields, restored as-is. */
const MANUAL_SAVED_NUMBERS = [
  "limit_price",
  "stop_price",
  "trail_percent",
  "sell_qty",
  "sell_notional",
  "notional",
  "buy_qty",
  "take_profit_r",
  "ai_risk_pct",
  "ai_atr_stop_mult",
  "stop_limit_offset_pct",
  "stop_limit_price",
  "reinvest_qty",
  "reinvest_limit_price",
  "reinvest_expire_minutes",
  "followon_qty",
  "followon_limit_price",
  "followon_target_symbol",
  "dip_hunt_wait_minutes",
  "dip_hunt_pct",
];

function collectManualForm() {
  const form = $("manual-order");
  if (!form) return null;
  const out = {
    symbol: manualSymbol(),
    side: manualSide(),
    order_type: manualOrderType(),
    time_in_force: manualTimeInForce(),
    trading_session: manualTradingSession(),
    extended_hours: manualExtendedHours(),
    sell_mode: manualSellMode(),
    buy_size_mode: manualBuySizeMode(),
    bracket_enabled: form.elements.bracket_enabled?.checked !== false,
    reinvest_enabled: form.elements.reinvest_enabled?.checked === true,
    reinvest_qty_mode: manualReinvestQtyMode(),
    followon_enabled: form.elements.followon_enabled?.checked === true,
    followon_kind: manualFollowOnKind(),
    followon_qty_mode: manualFollowOnQtyMode(),
    followon_order_type: manualFollowOnOrderType(),
    dip_hunt_enabled: form.elements.dip_hunt_enabled?.checked === true,
    stop_limit_pinned: stopLimitPinnedToStop,
  };
  MANUAL_SAVED_NUMBERS.forEach((name) => {
    out[name] = form.elements[name]?.value ?? "";
  });
  return out;
}

function applyManualForm(saved) {
  if (!saved || typeof saved !== "object") return false;
  if (saved.symbol) {
    const symbol = String(saved.symbol).trim().toUpperCase();
    if (/^[A-Z.\-]{1,12}$/.test(symbol)) setManualFormValue("symbol", symbol);
  }
  Object.entries(MANUAL_SAVED_FIELDS).forEach(([name, allowed]) => {
    if (name === "side") {
      const side = visibleTicketSide(saved.side);
      if (side) setManualFormValue("side", side);
      return;
    }
    if (allowed.includes(saved[name])) setManualFormValue(name, saved[name]);
  });
  if (saved.bracket_enabled != null) {
    setManualFormValue("bracket_enabled", saved.bracket_enabled === true);
  }
  // All / Half were retired from the ticket; map them onto the share box.
  if (saved.sell_mode === "all" || saved.sell_mode === "half") {
    pendingSellFill = saved.sell_mode;
    lastAutoSellQty = null;
    lastAutoSellFill = null;
    setManualFormValue("sell_mode", "custom");
  }
  MANUAL_SAVED_NUMBERS.forEach((name) => {
    if (saved[name] != null && saved[name] !== "") {
      setManualFormValue(name, saved[name]);
    }
  });
  // Safe to restore: the pin only ever tracks the stop this ticket is sized
  // against, so a stale saved price is corrected on the next estimate.
  stopLimitPinnedToStop = saved.stop_limit_pinned === true;
  setManualFormValue(
    "trading_session",
    saved.trading_session === "24h" || saved.extended_hours === true ? "24h" : "regular"
  );
  // A buy-back, next-ticket, or dip hunt arms a second order that spends real
  // cash. Restoring any of them pre-armed from a saved draft or preset would
  // mean a ticket the user never typed could go out on the next submit.
  const droppedAutomation = [
    [saved.reinvest_enabled === true, "reinvest_legend", "Re-invest after the sell fills"],
    [saved.followon_enabled === true, "followon_legend", "Next ticket after this close fills"],
    [saved.dip_hunt_enabled === true, "dip_hunt_legend", "Buy from the lowest price"],
  ]
    .filter(([dropped]) => dropped)
    .map(([, key, fallback]) => tx(key, fallback));
  setManualFormValue("reinvest_enabled", false);
  setManualFormValue("followon_enabled", false);
  setManualFormValue("dip_hunt_enabled", false);
  if (droppedAutomation.length) {
    showToast(
      tx(
        "manual_automation_not_restored",
        "Turned off for safety, re-enable if needed: {list}.",
        { list: droppedAutomation.join(", ") }
      ),
      "error"
    );
  }
  formDirtyManual = true;
  return true;
}

function saveManualFormDraft() {
  const draft = collectManualForm();
  if (!draft) return;
  try {
    localStorage.setItem(MANUAL_FORM_STORAGE_KEY, JSON.stringify(draft));
  } catch {
    /* ignore quota / private mode */
  }
}

function restoreManualFormDraft() {
  return applyManualForm(readManualFormDraft());
}

/* ---------------------------------------------------------------- presets --
 * A saved preset is the same shape as a draft, minus the symbol: "how I trade"
 * rather than "what I was about to trade". Stored per browser, like the draft.
 */

const MANUAL_PRESET_STORAGE_KEY = "alpaca-desk-manual-order-presets";
const MANUAL_MAX_PRESETS = 12;

function readManualPresets() {
  try {
    const raw = JSON.parse(localStorage.getItem(MANUAL_PRESET_STORAGE_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((p) => p && p.name) : [];
  } catch {
    return [];
  }
}

function writeManualPresets(list) {
  try {
    localStorage.setItem(
      MANUAL_PRESET_STORAGE_KEY,
      JSON.stringify(list.slice(0, MANUAL_MAX_PRESETS))
    );
  } catch {
    /* ignore quota / private mode */
  }
}

function renderManualPresets() {
  const select = $("manual-preset");
  if (!select) return;
  const presets = readManualPresets();
  const current = select.value;
  select.innerHTML =
    `<option value="">${escapeHtml(tx("preset_none", "— none —"))}</option>` +
    presets
      .map(
        (p) =>
          `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`
      )
      .join("");
  if (presets.some((p) => p.name === current)) select.value = current;
  // The option list is rebuilt from storage, and this runs before the shared
  // DOMContentLoaded pass on first load — `ensureNiceSelect` covers both the
  // "not bound yet" and the "bound, needs a re-read" cases.
  ensureNiceSelect(select);
  syncPresetDeleteButton();
}

async function saveCurrentAsPreset() {
  const values = collectManualForm();
  if (!values) return;
  const suggested = `${values.side} ${values.order_type} ${values.buy_size_mode}`;
  const name = await askInlinePrompt(
    tx("preset_name_prompt", "Name this ticket preset"),
    { initial: suggested }
  );
  if (!name) return;
  const existing = readManualPresets();
  // The list is capped, and a silent `slice()` used to drop the oldest preset
  // with no word about it. Say so, and let the user delete one instead.
  if (
    existing.length >= MANUAL_MAX_PRESETS &&
    !existing.some((p) => p.name === name)
  ) {
    showToast(
      tx(
        "preset_limit_reached",
        "Preset list is full ({max}). Delete one before saving another.",
        { max: String(MANUAL_MAX_PRESETS) }
      ),
      "error"
    );
    return;
  }
  // A preset describes the sizing, not the instrument — the symbol comes from
  // whatever you are looking at when you apply it.
  const { symbol, ...rest } = values;
  const presets = existing.filter((p) => p.name !== name);
  presets.unshift({ name, values: rest });
  writeManualPresets(presets);
  renderManualPresets();
  const select = $("manual-preset");
  if (select) {
    select.value = name;
    ensureNiceSelect(select);
  }
  syncPresetDeleteButton();
  showToast(tx("preset_saved", "Preset saved"), "ok");
}

/** Delete only makes sense once a saved preset is actually selected. */
function syncPresetDeleteButton() {
  const btn = $("btn-manual-delete-preset");
  const select = $("manual-preset");
  if (!btn) return;
  btn.hidden = !select?.value;
  btn.disabled = loopRunning || busy;
}

/** Twelve presets with no way to remove one is a list you get stuck in. */
async function deleteSelectedPreset() {
  const select = $("manual-preset");
  const name = String(select?.value || "");
  if (!name) return;
  const ok = await askInlineConfirm(
    tx("preset_delete_confirm", "Delete the preset “{name}”?", { name }),
    { confirmLabel: tx("delete_preset", "Delete") }
  );
  if (!ok) return;
  writeManualPresets(readManualPresets().filter((p) => p.name !== name));
  renderManualPresets();
  if (select) {
    select.value = "";
    ensureNiceSelect(select);
  }
  syncPresetDeleteButton();
  showToast(tx("preset_deleted", "Preset deleted"), "ok");
}

function applyPresetByName(name) {
  if (!name) return;
  const preset = readManualPresets().find((p) => p.name === name);
  if (!preset) return;
  applyManualForm(preset.values);
  saveManualFormDraft();
  syncManualUi();
  scheduleServerPreview();
  showToast(tx("preset_applied", "Preset applied: {name}", { name }), "ok");
}

/** Honour ?symbol=&side= so Positions "Add" / "Trade" land on a prefilled ticket. */
function applyManualTicketFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const symbol = String(params.get("symbol") || "")
    .trim()
    .toUpperCase();
  let touched = false;
  if (/^[A-Z.\-]{1,12}$/.test(symbol)) {
    setManualFormValue("symbol", symbol);
    formDirtyManual = true;
    touched = true;
  }
  const rawSide = String(params.get("side") || "").trim().toLowerCase();
  const side = visibleTicketSide(rawSide);
  if (side) {
    setManualFormValue("side", side);
    formDirtyManual = true;
    touched = true;
  }
  return touched;
}

/** Watchlist plus open positions — holdings first, so selling one is one pick. */
function syncSymbolSuggestions() {
  const list = $("symbol-suggestions");
  if (!list) return;
  const raw = lastDeskSettings?.symbols || lastDeskSettings?.symbol || "";
  const watch = String(raw)
    .split(",")
    .map((s) => s.trim().toUpperCase())
    .filter((s) => /^[A-Z.\-]{1,12}$/.test(s));
  const held = (
    Array.isArray(manualContext?.heat?.symbols) ? manualContext.heat.symbols : []
  )
    .map((s) => String(s || "").trim().toUpperCase())
    .filter((s) => /^[A-Z.\-]{1,12}$/.test(s));
  const unique = [...new Set([...held, ...watch])];
  const current = [...list.options].map((o) => o.value).join(",");
  if (current === unique.join(",")) return;
  list.innerHTML = unique.map((s) => `<option value="${escapeHtml(s)}"></option>`).join("");
}

/**
 * One pass over the whole ticket.
 *
 * Order matters and is the reason this reads oddly: the blanket disable loop
 * below would otherwise re-enable fields that the type and side passes had
 * just decided to lock, so those two run *after* it. They used to run on both
 * sides of it — every keystroke did the work twice, and rebuilt every
 * nice-select in between.
 */
function syncManualUi() {
  syncManualLoopBanner();
  syncSymbolSuggestions();
  const locked = loopRunning || busy;
  const form = $("manual-order");

  if (form) {
    // Baseline pass: lock everything the loop or an in-flight request should
    // own. It runs *before* the type and side passes because it would
    // otherwise re-lock the fields they had just decided to open — which is
    // why both of those used to be called a second time afterwards, doing all
    // their work twice on every keystroke.
    const SELF_MANAGED = new Set([
      "limit_price",
      "stop_price",
      "trail_percent",
      "sell_qty",
      "sell_notional",
      "notional",
      "buy_qty",
      "side",
    ]);
    [...form.elements].forEach((el) => {
      if (el.type === "hidden") return;
      if (SELF_MANAGED.has(el.name)) return;
      el.disabled = locked;
    });
    stripNiceSelectFromManualInputs(form);
    // Idempotent, and it has to happen before `decorateTifSelect` runs inside
    // `syncManualTypeUi` — that needs a bound dropdown to write into.
    initNiceSelects(form);
  }

  // These own the per-field enabled state, and may correct the order type or
  // the time in force, so everything that reads either runs after them.
  syncManualSideUi();
  syncManualTypeUi();
  if (form) syncNiceSelectDisabled(form);

  syncManualHelp();
  updateSizeEstimate();

  const preview = $("btn-manual-preview");
  if (preview) preview.disabled = locked;
  const refresh = $("btn-manual-refresh");
  if (refresh) refresh.disabled = busy;
  syncPresetDeleteButton();
}

/** Helper to render a structured queue item in advanced order panels */
function renderManualPlanItem(queue, plan, view, cancelBtnHtml) {
  const queueKey = queue === "followon" ? "followon" : queue === "reinvest" ? "reinvest" : "dip_hunt";
  const queueTitleMap = {
    followon: tx("followon_queue", "Next-ticket queue"),
    reinvest: tx("reinvest_queue", "Re-investment queue"),
    dip_hunt: tx("dip_hunt_queue", "Dip-hunt queue"),
  };
  const queueBadgeLabel = queueTitleMap[queueKey] || "";
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
        ${escapeHtml(queueBadgeLabel)}
      </span>
      <span class="side-badge ${escapeHtml(side)}">${escapeHtml(sideLabel)}</span>
      <strong class="ord-plan-sym-text">${escapeHtml(view.symbol)}</strong>
      <span class="ord-plan-dot-sep">·</span>
      <span class="ord-plan-spec-val">${escapeHtml(view.qty || "—")}</span>
      <span class="ord-plan-spec-muted">@</span>
      <span class="ord-plan-spec-val ${view.priceLabel === tx("market", "Market") ? "is-market" : ""}">${escapeHtml(view.priceLabel || "—")}</span>`
    : `<span class="manual-reinvest-head">${escapeHtml(view.head)}</span>`;

  return `<li class="manual-reinvest-item ord-queue-item" data-kind="${escapeHtml(view.kind)}">
      <div class="ord-queue-row">
        <div class="ord-plan-chips">${chipContent}</div>
        <div class="ord-queue-actions">${cancelBtnHtml}</div>
      </div>
      <div class="ord-plan-status-row">
        <div class="ord-plan-status-pill kind-${escapeHtml(view.kind)}">
          ${statusIconSvg}
          <span class="ord-plan-note-text">${escapeHtml(view.note)}</span>
        </div>
      </div>
    </li>`;
}

/** One human sentence per plan state, for the queue panel. */
function formatReinvestPlan(plan) {
  const status = String(plan?.status || "").toLowerCase();
  const qty =
    plan.buy_qty != null
      ? formatQty(plan.buy_qty)
      : plan.qty_mode === "custom"
        ? formatQty(plan.qty)
        : formatQty(plan.sell_qty);
  const priceLabel = stockPrice(plan.limit_price);
  const head = `${plan.symbol} · ${qty} @ ${priceLabel}`;
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
  // "placing" means the buy is already on the wire — past the point of cancel.
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
  // The desk died mid-send: it cannot know whether the buy landed, and
  // guessing either way would be worse than saying so.
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

/**
 * Reveal a rail queue, opening it the first time it appears with live work.
 *
 * The three queues are the same kind of card and now fold the same way — but a
 * plan that has just been armed is money the desk is about to move, so it
 * should not arrive collapsed behind a caret. Collapse it once and it stays
 * collapsed: this only fires on the hidden → visible transition.
 */
function showPlanFold(panel, hasLiveWork) {
  if (!panel) return;
  const wasHidden = panel.hidden;
  panel.hidden = false;
  if (wasHidden && hasLiveWork) panel.open = true;
}

function renderReinvestPlans(plans) {
  const panel = $("manual-reinvest-panel");
  const list = $("manual-reinvest-list");
  const countEl = $("manual-reinvest-count");
  if (!panel || !list) return;
  const rows = Array.isArray(plans) ? plans : [];
  if (countEl) {
    countEl.hidden = !rows.length;
    countEl.textContent = String(rows.length);
  }
  if (!rows.length) {
    panel.hidden = true;
    list.innerHTML = "";
    return;
  }
  showPlanFold(
    panel,
    rows.some((p) => p.status === "waiting" || p.status === "placing" || p.status === "awaiting_fill")
  );
  list.innerHTML = rows
    .map((plan) => {
      const view = formatReinvestPlan(plan);
      const cancel = view.cancellable
        ? `<button type="button" class="ghost ghost-danger ord-plan-btn-cancel" data-cancel-reinvest="${escapeHtml(
            String(plan.id || "")
          )}"><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="4" x2="4" y2="12"></line><line x1="4" y1="4" x2="12" y2="12"></line></svg>${escapeHtml(tx("cancel", "Cancel"))}</button>`
        : "";
      return renderManualPlanItem("reinvest", plan, view, cancel);
    })
    .join("");
}

/**
 * Poll the queue while any plan is still waiting.
 *
 * The buy-back fires on the server, so the page is only a viewer — but a
 * waiting plan that silently becomes a filled order is exactly the thing the
 * user opened this page to watch.
 */
let reinvestPollTimer = null;
let lastReinvestPlans = [];
let followOnPollTimer = null;
let lastFollowOnPlans = [];

async function refreshReinvestPlans() {
  try {
    const data = await api("/api/reinvest");
    const plans = data.plans || [];
    lastReinvestPlans = plans;
    renderReinvestPlans(plans);
    scheduleReinvestPoll(
      plans.some((p) => p.status === "waiting" || p.status === "placing" || p.status === "awaiting_fill")
    );
    return plans;
  } catch {
    return null;
  }
}

function scheduleReinvestPoll(active) {
  clearTimeout(reinvestPollTimer);
  if (!active) return;
  reinvestPollTimer = setTimeout(() => {
    if (document.hidden) {
      scheduleReinvestPoll(true);
      return;
    }
    refreshReinvestPlans().catch(() => {});
  }, 5000);
}

async function cancelReinvestPlan(planId) {
  if (!planId || busy) return;
  try {
    setBusy(true, tx("cancelling", "Cancelling…"));
    const data = await api("/api/reinvest/cancel", {
      method: "POST",
      body: JSON.stringify({ plan_id: planId }),
    });
    lastReinvestPlans = data.plans || [];
    renderReinvestPlans(lastReinvestPlans);
    showToast(tx("reinvest_cancelled", "Buy-back cancelled"), "ok");
  } catch (err) {
    setManualError(err.message);
  } finally {
    setBusy(false);
    syncManualUi();
  }
}

function formatFollowOnPlan(plan) {
  const status = String(plan?.status || "").toLowerCase();
  const nextSide = String(plan.next_side || "buy").toLowerCase();
  const target = String(plan.target_symbol || plan.symbol || "");
  const qty = formatQty(plan.next_qty ?? plan.qty ?? plan.close_qty);
  const sideLabel =
    nextSide === "short" ? tx("short_side", "Short") : tx("buy", "Buy");
  const isMarket = String(plan.order_type || "limit").toLowerCase() === "market";
  const priceLabel = followonPriceLabel(plan);
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
    const started = plan.wait_started === true || plan.seconds_left != null;
    if (!started) {
      return {
        ...base,
        kind: "waiting",
        note: tx("followon_state_waiting", "Waiting for the close to fill"),
        cancellable: true,
      };
    }
    const mins = Math.max(0, Math.ceil(Number(plan.seconds_left || 0) / 60));
    return {
      ...base,
      kind: "waiting",
      note: tx(
        "followon_state_wait_after_fill",
        "Close filled · {mins}m left to send",
        { mins: String(mins) }
      ),
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

function renderFollowOnPlans(plans) {
  const panel = $("manual-followon-panel");
  const list = $("manual-followon-list");
  const countEl = $("manual-followon-count");
  if (!panel || !list) return;
  const rows = Array.isArray(plans) ? plans : [];
  if (countEl) {
    countEl.hidden = !rows.length;
    countEl.textContent = String(rows.length);
  }
  if (!rows.length) {
    panel.hidden = true;
    list.innerHTML = "";
    return;
  }
  showPlanFold(
    panel,
    rows.some((p) => p.status === "waiting" || p.status === "placing")
  );
  list.innerHTML = rows
    .map((plan) => {
      const view = formatFollowOnPlan(plan);
      const cancel = view.cancellable
        ? `<button type="button" class="ghost ghost-danger ord-plan-btn-cancel" data-cancel-followon="${escapeHtml(
            String(plan.id || "")
          )}"><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="4" x2="4" y2="12"></line><line x1="4" y1="4" x2="12" y2="12"></line></svg>${escapeHtml(tx("cancel", "Cancel"))}</button>`
        : "";
      return renderManualPlanItem("followon", plan, view, cancel);
    })
    .join("");
}

async function refreshFollowOnPlans() {
  try {
    const data = await api("/api/followon");
    const plans = data.plans || [];
    lastFollowOnPlans = plans;
    renderFollowOnPlans(plans);
    scheduleFollowOnPoll(plans.some((p) => p.status === "waiting" || p.status === "placing"));
    return plans;
  } catch {
    return null;
  }
}

function scheduleFollowOnPoll(active) {
  clearTimeout(followOnPollTimer);
  if (!active) return;
  followOnPollTimer = setTimeout(() => {
    if (document.hidden) {
      scheduleFollowOnPoll(true);
      return;
    }
    refreshFollowOnPlans().catch(() => {});
  }, 5000);
}

async function cancelFollowOnPlan(planId) {
  if (!planId || busy) return;
  try {
    setBusy(true, tx("cancelling", "Cancelling…"));
    const data = await api("/api/followon/cancel", {
      method: "POST",
      body: JSON.stringify({ plan_id: planId }),
    });
    lastFollowOnPlans = data.plans || [];
    renderFollowOnPlans(lastFollowOnPlans);
    showToast(tx("followon_cancelled", "Next ticket cancelled"), "ok");
  } catch (err) {
    setManualError(err.message);
  } finally {
    setBusy(false);
    syncManualUi();
  }
}

function formatDipHuntPlan(plan) {
  const status = String(plan?.status || "").toLowerCase();
  const cycle = Number(plan.cycle) > 1 ? ` · ${tx("dip_hunt_cycle", "cycle {n}", { n: String(plan.cycle) })}` : "";
  const head = `${plan.symbol}${cycle} · ${tx("dip_hunt_head", "{wait}m / {dip}%", {
    wait: String(plan.wait_minutes ?? "—"),
    dip: String(plan.dip_pct ?? "—"),
  })}`;
  const cancellable = [
    "watching_entry",
    "watching_stop",
    "hunting",
    "awaiting_fill",
  ].includes(status);
  const target = plan.target_price != null ? stockPrice(plan.target_price) : "—";
  const base = {
    side: "buy",
    symbol: (plan.symbol || "—") + cycle,
    qty: formatQty(plan.buy_qty ?? plan.qty),
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
          ? tx(
              "dip_hunt_state_hunting",
              "Hunting {price} · {mins}m of the wait left",
              { price: target, mins: String(mins) }
            )
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
      cancellable: false,
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

function renderDipHuntPlans(plans) {
  const panel = $("manual-dip-hunt-panel");
  const list = $("manual-dip-hunt-list");
  const countEl = $("manual-dip-hunt-count");
  if (!panel || !list) return;
  const rows = Array.isArray(plans) ? plans : [];
  if (countEl) {
    countEl.hidden = !rows.length;
    countEl.textContent = String(rows.length);
  }
  if (!rows.length) {
    panel.hidden = true;
    list.innerHTML = "";
    return;
  }
  showPlanFold(
    panel,
    rows.some((p) => DIP_HUNT_LIVE.has(p.status))
  );
  list.innerHTML = rows
    .map((plan) => {
      const view = formatDipHuntPlan(plan);
      const cancel = view.cancellable
        ? `<button type="button" class="ghost ghost-danger ord-plan-btn-cancel" data-cancel-dip-hunt="${escapeHtml(
            String(plan.id || "")
          )}"><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="4" x2="4" y2="12"></line><line x1="4" y1="4" x2="12" y2="12"></line></svg>${escapeHtml(tx("cancel", "Cancel"))}</button>`
        : "";
      return renderManualPlanItem("dip_hunt", plan, view, cancel);
    })
    .join("");
}

let dipHuntPollTimer = null;
let lastDipHuntPlans = [];

const DIP_HUNT_LIVE = new Set([
  "watching_entry",
  "watching_stop",
  "hunting",
  "awaiting_fill",
  "placing",
]);

async function refreshDipHuntPlans() {
  try {
    const data = await api("/api/dip-hunt");
    const plans = data.plans || [];
    lastDipHuntPlans = plans;
    renderDipHuntPlans(plans);
    scheduleDipHuntPoll(plans.some((p) => DIP_HUNT_LIVE.has(p.status)));
    return plans;
  } catch {
    return null;
  }
}

function scheduleDipHuntPoll(active) {
  clearTimeout(dipHuntPollTimer);
  if (!active) return;
  dipHuntPollTimer = setTimeout(() => {
    if (document.hidden) {
      scheduleDipHuntPoll(true);
      return;
    }
    refreshDipHuntPlans().catch(() => {});
  }, 5000);
}

async function cancelDipHuntPlan(planId) {
  if (!planId || busy) return;
  try {
    setBusy(true, tx("cancelling", "Cancelling…"));
    const data = await api("/api/dip-hunt/cancel", {
      method: "POST",
      body: JSON.stringify({ plan_id: planId }),
    });
    lastDipHuntPlans = data.plans || [];
    renderDipHuntPlans(lastDipHuntPlans);
    showToast(tx("dip_hunt_cancelled", "Dip hunt cancelled"), "ok");
  } catch (err) {
    setManualError(err.message);
  } finally {
    setBusy(false);
    syncManualUi();
  }
}

function renderManualLastTicket(result) {
  const panel = $("manual-last-ticket");
  const summary = $("manual-last-summary");
  const details = $("manual-last-details");
  const warnList = $("manual-last-warnings");
  if (!panel || !summary || !details) return;
  if (!result) {
    panel.hidden = true;
    return;
  }
  lastManualTicket = result;
  panel.hidden = false;
  const side = String(result.side || "").toUpperCase();
  const qty = result.order_qty != null ? formatQty(result.order_qty) : "—";
  const sym = result.symbol || "—";
  const histLink = $("manual-history-link");
  if (histLink && result.symbol) {
    histLink.href = historyHref({
      symbol: result.symbol,
      source: "alpaca",
      range: "month",
      side: "",
    });
  }
  const posLink = $("manual-positions-link");
  if (posLink && result.symbol) {
    posLink.href = `/positions?symbol=${encodeURIComponent(result.symbol)}`;
  }

  let state;
  if (result.needs_confirm) state = tx("ticket_needs_confirm", "Needs confirm");
  else if (result.preview) state = tx("ticket_preview", "Preview (not sent)");
  else state = tx("ticket_submitted", "Submitted");
  summary.textContent = `${state} · ${side} ${qty} ${sym}`;
  summary.dataset.side = String(result.side || "").toLowerCase();
  // A fresh ticket has no outcome yet — `trackOrderFill` fills this in.
  renderOrderOutcome(null);

  // The desk returns the adjustments it made — showing them beats a silent fill.
  const warnings = Array.isArray(result.warnings) ? result.warnings : [];
  if (warnList) {
    warnList.hidden = warnings.length === 0;
    warnList.innerHTML = warnings.map((w) => `<li>${escapeHtml(String(w))}</li>`).join("");
  }

  const rows = [
    [
      tx("order_type", "Order type"),
      `${String(result.order_type || "—").replaceAll("_", " ")}${
        result.time_in_force ? ` · ${String(result.time_in_force).toUpperCase()}` : ""
      }${
        result.extended_hours
          ? ` · ${tx("session_24h", "24 hour market")}`
          : result.is_open === false
            ? ` · ${tx("activates_rth_queued", "Regular hours")}`
            : ` · ${tx("session_regular", "Regular hours")}`
      }`,
    ],
    [tx("session", "Session"), formatSession(result.session)],
    [tx("mark", "Mark"), result.price != null ? stockPrice(result.price) : "—"],
    [tx("limit_price", "Limit"), result.limit_price != null ? stockPrice(result.limit_price) : "—"],
  ];
  if (result.stop_price != null) {
    rows.push([tx("trigger_price", "Trigger price"), stockPrice(result.stop_price)]);
  }
  if (result.trail_percent != null) {
    rows.push([tx("trail_percent", "Trail %"), `${result.trail_percent}%`]);
  }
  rows.push(
    [
      tx("order_id", "Order id"),
      result.order_id ? String(result.order_id).slice(0, 12) + "…" : "—",
    ],
    [
      tx("target_price", "Target"),
      result.stop_loss?.take_profit_price != null
        ? stockPrice(result.stop_loss.take_profit_price)
        : result.take_profit_price != null
          ? `~${stockPrice(result.take_profit_price)}`
          : tx("target_off", "off"),
    ],
    [
      tx("stop_price", "Stop"),
      result.stop_loss?.stop_price != null
        ? stockPrice(result.stop_loss.stop_price)
        : result.stop_preview != null
          ? `~${stockPrice(result.stop_preview)}`
          : "—",
    ],
    [
      tx("stop_limit_price", "Sell limit"),
      result.stop_loss?.limit_price != null
        ? stockPrice(result.stop_loss.limit_price)
        : result.stop_limit_preview != null
          ? `~${stockPrice(result.stop_limit_preview)}`
          : tx("stop_limit_market", "market"),
    ],
    [tx("position", "Position"), result.position != null ? formatQty(result.position) : "—"]
  );
  if (result.qty_truncated || result.qty_clamped) {
    rows.push([
      tx("adjusted", "Adjusted"),
      `${formatQty(result.requested_qty)} → ${formatQty(result.order_qty)}`,
    ]);
  }
  if (result.stop_rearmed) {
    rows.push([
      tx("stop_rearmed", "Stop re-armed"),
      tx("stop_rearmed_value", "over the {qty} shares kept", {
        qty: formatQty(Math.abs(Number(result.position) || 0)),
      }),
    ]);
  }
  if (Array.isArray(result.overridden) && result.overridden.length) {
    rows.push([
      tx("limits_overridden", "Limits overridden"),
      result.overridden.join(", "),
    ]);
  }
  if (result.reinvest) {
    const ri = result.reinvest;
    rows.push([
      tx("reinvest_confirm_row", "Buy-back"),
      tx("reinvest_ticket_value", "{qty} @ {price} · {state}", {
        qty: formatQty(ri.planned_qty ?? ri.qty ?? ri.sell_qty),
        price: stockPrice(ri.limit_price),
        state:
          ri.status === "preview"
            ? tx("ticket_preview", "Preview (not sent)")
            : tx("reinvest_state_armed", "armed"),
      }),
    ]);
  }
  if (result.followon) {
    const fo = result.followon;
    const nextSide = String(fo.next_side || "buy").toLowerCase();
    const sideLabel =
      nextSide === "short" ? tx("short_side", "Short") : tx("buy", "Buy");
    rows.push([
      tx("followon_confirm_row", "Next ticket"),
      tx("followon_ticket_value", "{side} {qty} {symbol} @ {price} · {state}", {
        side: sideLabel,
        qty: formatQty(fo.planned_qty ?? fo.qty ?? fo.close_qty),
        symbol: fo.target_symbol || fo.symbol || "",
        price: followonPriceLabel(fo),
        state:
          fo.status === "preview"
            ? tx("ticket_preview", "Preview (not sent)")
            : tx("followon_state_armed", "armed"),
      }),
    ]);
  }
  if (result.dip_hunt) {
    const dh = result.dip_hunt;
    rows.push([
      tx("dip_hunt_confirm_row", "Dip hunt"),
      tx("dip_hunt_ticket_value", "{wait}m wait · {dip}% drop · {state}", {
        wait: String(dh.wait_minutes ?? "—"),
        dip: String(dh.dip_pct ?? "—"),
        state:
          dh.status === "preview"
            ? tx("ticket_preview", "Preview (not sent)")
            : tx("dip_hunt_state_armed", "armed"),
      }),
    ]);
  }
  details.innerHTML = rows
    .map(
      ([dt, dd]) =>
        `<div><dt>${escapeHtml(String(dt))}</dt><dd>${escapeHtml(String(dd))}</dd></div>`
    )
    .join("");
}

/** Human sentence for what the broker did with the ticket. */
function formatOrderOutcome(order) {
  const status = String(order?.status || "").toLowerCase();
  const filled = Number(order?.filled_qty || 0);
  const price = Number(order?.filled_avg_price);
  if (status === "filled") {
    return {
      kind: "ok",
      text: tx("ticket_filled", "Filled {qty} @ {price}", {
        qty: formatQty(filled),
        price: Number.isFinite(price) ? stockPrice(price) : "—",
      }),
    };
  }
  if (filled > 0) {
    return {
      kind: "warn",
      text: tx("ticket_partial", "Partially filled: {qty} of {total}", {
        qty: formatQty(filled),
        total: formatQty(order?.qty),
      }),
    };
  }
  if (status === "rejected") {
    return { kind: "error", text: tx("ticket_rejected", "Rejected by the broker") };
  }
  if (status === "canceled" || status === "cancelled" || status === "expired") {
    return { kind: "error", text: tx("ticket_canceled", "Canceled before it filled") };
  }
  return {
    kind: "warn",
    text: tx("ticket_working", "Working — accepted, not filled yet ({status})", {
      status: status || "…",
    }),
  };
}

/**
 * Acceptance is not a fill, so the ticket follows its own order until the
 * broker reaches a terminal state.
 *
 * The delay widens as it goes: a market order settles in seconds, but a
 * resting limit can sit all session and used to be abandoned after six
 * seconds, leaving "Working" on screen forever. Backing off means the page can
 * keep watching a GTC order for an hour at the cost of a handful of requests.
 */
const MANUAL_TRACK_DELAYS_MS = [
  1200, 1200, 1500, 2000, 3000, 5000, 8000, 15000, 30000, 60000,
];

async function trackOrderFill(orderId, { attempts = 40 } = {}) {
  if (!orderId) return null;
  let last = null;
  for (let i = 0; i < attempts; i += 1) {
    try {
      const data = await api(`/api/order/status?order_id=${encodeURIComponent(orderId)}`);
      last = data.order || null;
    } catch {
      return last;
    }
    if (last) {
      renderOrderOutcome(last);
      if (last.is_terminal) return last;
    }
    // Stop following an order the user has navigated away from — the ticket it
    // belongs to is no longer the one on screen.
    if (lastManualTicket?.order_id !== orderId) return last;
    const delay =
      MANUAL_TRACK_DELAYS_MS[Math.min(i, MANUAL_TRACK_DELAYS_MS.length - 1)];
    await new Promise((resolve) => setTimeout(resolve, delay));
  }
  return last;
}

function renderOrderOutcome(order) {
  const el = $("manual-last-status");
  if (!el) return;
  if (!order) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  const outcome = formatOrderOutcome(order);
  el.hidden = false;
  el.textContent = outcome.text;
  el.dataset.kind = outcome.kind;
}

function hydrateManualFromSettings(settings, { force = false } = {}) {
  if (!settings) return;
  const form = $("manual-order");
  if (!form) return;
  if (!force && formDirtyManual) {
    syncManualUi();
    return;
  }
  if (settings.ai_risk_pct != null) setManualFormValue("ai_risk_pct", settings.ai_risk_pct);
  if (settings.ai_atr_stop_mult != null) {
    // Auto Trade allows 0 ("no ATR stop, use the flat percent") but this page
    // has no flat-percent field, so 0 would load a ticket that can never
    // validate. Lift it to the floor and let the user widen from there.
    const deskMult = Number(settings.ai_atr_stop_mult);
    setManualFormValue(
      "ai_atr_stop_mult",
      Number.isFinite(deskMult) && deskMult >= MIN_ATR_STOP_MULT
        ? deskMult
        : MIN_ATR_STOP_MULT
    );
  }
  if (settings.stop_limit_offset_pct != null) {
    setManualFormValue("stop_limit_offset_pct", settings.stop_limit_offset_pct);
  }
  syncManualUi();
}

function formatQtyAdjustmentConfirm(result) {
  const parts = [];
  if (result.qty_clamped) {
    parts.push(
      tx(
        "confirm_qty_clamped",
        "Sell qty will be clamped to your position: {from} → {to} (held {held}).",
        {
          from: formatQty(result.requested_qty),
          to: formatQty(result.order_qty),
          held: formatQty(result.position),
        }
      )
    );
  }
  if (result.qty_truncated) {
    parts.push(
      tx(
        "confirm_qty_truncated",
        "Outside regular hours, qty truncates to whole shares: {from} → {to}.",
        { from: formatQty(result.requested_qty), to: formatQty(result.order_qty) }
      )
    );
  }
  return parts.join(" ");
}

/** Modal plumbing — focus is trapped inside and returned on close. */
function manualModalNodes() {
  const modal = $("manual-confirm-modal");
  if (!modal) return [];
  return [
    ...modal.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
    ),
  ].filter((node) => !node.closest("[hidden]"));
}

function openConfirmModal({ focus = "cancel" } = {}) {
  const modal = $("manual-confirm-modal");
  if (!modal) return;
  manualModalReturnFocus = document.activeElement;
  modal.hidden = false;
  document.body.classList.add("modal-open");
  document.querySelector(".masthead")?.setAttribute("inert", "");
  document.querySelector("main")?.setAttribute("inert", "");
  document.querySelector(".desk-footer")?.setAttribute("inert", "");
  // Focus lands on the way out, not the way through. This dialog is the last
  // gate before real money and it used to open with Submit focused, so a
  // stray Enter — the key that opened it — sent the order.
  if (focus === "prompt") $("manual-confirm-prompt-input")?.focus();
  else if (focus === "submit") $("btn-confirm-submit")?.focus();
  else $("btn-confirm-cancel")?.focus();
}

function closeConfirmModal() {
  const modal = $("manual-confirm-modal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  const prompt = $("manual-confirm-prompt");
  if (prompt) prompt.hidden = true;
  document.body.classList.remove("modal-open");
  document.querySelector(".masthead")?.removeAttribute("inert");
  document.querySelector("main")?.removeAttribute("inert");
  document.querySelector(".desk-footer")?.removeAttribute("inert");
  window.manualOrderPayload = null;
  if (manualModalReturnFocus?.focus) manualModalReturnFocus.focus();
  manualModalReturnFocus = null;
  modal.dispatchEvent(new Event("manualmodalclose"));
}

/** Is the desk pointed at a real brokerage account right now? */
function manualIsLiveAccount() {
  const env =
    lastAlpacaStatus?.trading_mode ||
    lastAccount?.trading_mode ||
    (lastAccount?.paper === false ? "live" : "paper");
  return env === "live";
}

function renderConfirmationModal(payload) {
  const modal = $("manual-confirm-modal");
  const summary = $("manual-confirm-summary");
  const noteEl = $("manual-confirm-note");
  if (!modal || !summary) return;

  const isExit = payload.side === "sell";
  const isShortEntry = payload.side === "short";
  const calc = currentEstimate();
  const session = manualContext?.session || manualContext?.quote?.session;

  // The dialog is the last place the ticket can still be read before it goes
  // out, so it names the desk action — "Short", never a "Sell" that is really
  // opening a borrow.
  const SIDE_LABELS = {
    buy: tx("buy", "Buy"),
    sell: tx("sell", "Sell"),
    // Not the existing `short` key — that one is the lowercase noun the
    // position rail uses ("held short"), not an action label.
    short: tx("action_short", "Short"),
  };
  const TYPE_LABELS = {
    market: manualOrderTypeLabel("market"),
    limit: manualOrderTypeLabel("limit"),
    stop: manualOrderTypeLabel("stop"),
    stop_limit: manualOrderTypeLabel("stop_limit"),
    trailing_stop: manualOrderTypeLabel("trailing_stop"),
  };

  // Rows are [label, value, tone?] — tone drives the value colour.
  const rows = [
    [tx("symbol", "Symbol"), payload.symbol],
    [
      tx("action", "Action"),
      SIDE_LABELS[payload.side] || payload.side,
      isExit ? "sell" : "buy",
    ],
    [
      tx("order_type", "Order type"),
      payload.order_type === "market"
        ? `${TYPE_LABELS[payload.order_type] || payload.order_type} · DAY`
        : `${TYPE_LABELS[payload.order_type] || payload.order_type} · ${String(
            payload.time_in_force || "day"
          ).toUpperCase()} · ${tifExpireHint(payload.time_in_force || "day")}`,
    ],
  ];

  if (payload.limit_price) {
    rows.push([tx("limit_price", "Limit price"), stockPrice(payload.limit_price)]);
  }
  if (payload.stop_price) {
    rows.push([tx("trigger_price", "Trigger price"), stockPrice(payload.stop_price)]);
  }
  if (payload.trail_percent) {
    rows.push([tx("trail_percent", "Trail %"), `${payload.trail_percent}%`]);
  }
  if (payload.order_type === "limit") {
    if (payload.extended_hours) {
      rows.push([tx("trading_session", "Trading session"), tx("session_24h", "24 hour market")]);
    } else {
      rows.push([tx("trading_session", "Trading session"), tx("session_regular", "Regular hours")]);
    }
  } else if (manualContext && manualContext.is_open === false) {
    rows.push([
      tx("activates", "Activates"),
      tx("activates_rth_queued", "Regular hours"),
    ]);
  }

  if (calc && !calc.blocked) {
    if (isShortEntry) {
      rows.push(
        [tx("shares", "Shares"), formatQty(calc.shares)],
        [tx("entry_price", "Entry price"), stockPrice(calc.entry)],
        [tx("est_credit", "Est. credit"), money(calc.proceeds)],
        [
          tx("stop_price", "Stop"),
          tx("confirm_short_no_stop", "None — this short is unprotected"),
          "warn",
        ],
        [
          tx("pct_buying_power", "% of buying power"),
          calc.bpPct != null ? `${calc.bpPct.toFixed(1)}%` : "—",
        ]
      );
    } else if (isExit) {
      rows.push(
        [tx("shares", "Shares"), formatQty(calc.shares)],
        [tx("est_proceeds", "Est. proceeds"), money(calc.proceeds)],
        [tx("remaining_position", "Remaining"), formatQty(calc.remaining)]
      );
      if (calc.rearms) {
        rows.push([
          tx("stop_price", "Stop"),
          tx("confirm_stop_rearm", "Re-armed over the shares you keep"),
        ]);
      }
    } else {
      rows.push(
        [tx("est_position_size", "Position size"), `${formatQty(calc.shares)} ${tx("shares", "shares")}`],
        [tx("entry_price", "Entry price"), stockPrice(calc.entry)],
        [tx("est_cost", "Est. cost"), money(calc.cost)]
      );

      const hasBracket = manualBracketEnabled();
      if (hasBracket) {
        rows.push([
          tx("stop_price", "Stop"),
          calc.stopPrice != null ? stockPrice(calc.stopPrice) : tx("target_off", "off"),
        ]);
        if (calc.stopLimitPrice != null) {
          rows.push([
            tx("stop_limit_price", "Sell limit"),
            stockPrice(calc.stopLimitPrice),
          ]);
        }
        if (calc.targetPrice != null) {
          rows.push([
            tx("target_price", "Target"),
            `${stockPrice(calc.targetPrice)} · ${calc.takeProfitR}R`,
          ]);
        }
        if (calc.riskDollars != null) {
          rows.push([
            tx("max_risk", "Max risk"),
            `${money(calc.riskDollars)}${
              calc.equity > 0
                ? ` (${((calc.riskDollars / calc.equity) * 100).toFixed(2)}% ${tx(
                    "of_equity",
                    "of equity"
                  )})`
                : ""
            }`,
            "warn",
          ]);
        }
        if (calc.riskReward) {
          rows.push([
            tx("risk_reward", "Risk / reward"),
            tx("risk_reward_value", "{ratio}:1 · {reward} up", {
              ratio: calc.riskReward.ratio.toFixed(2),
              reward: money(calc.riskReward.reward),
            }),
          ]);
        }
      } else {
        rows.push([
          tx("bracket_legend", "Protective Bracket"),
          tx("bracket_off", "off"),
        ]);
      }
      if (calc.projectedRiskPct != null) {
        rows.push([
          tx("portfolio_heat", "Portfolio heat"),
          `${calc.projectedRiskPct.toFixed(2)}% ${tx("of_equity", "of equity")}`,
        ]);
      }
    }
  }

  // A second order is about to be armed — it belongs in the confirm dialog
  // beside the sell, not buried in a help line on the form.
  if (payload.reinvest) {
    const buyQty = manualReinvestQty();
    rows.push(
      [
        tx("reinvest_confirm_row", "Buy-back"),
        tx("reinvest_confirm_value", "{qty} @ {price} after the sell fills", {
          qty:
            payload.reinvest.qty_mode === "match"
              ? tx("reinvest_qty_match", "Same as sold")
              : formatQty(buyQty),
          price: stockPrice(payload.reinvest.limit_price),
        }),
      ],
      [
        tx("reinvest_confirm_cost", "Buy-back cost"),
        buyQty > 0 ? `≈ ${money(buyQty * payload.reinvest.limit_price)}` : "—",
      ],
      [
        tx("reinvest_expire", "Wait up to (minutes)"),
        String(payload.reinvest.expire_minutes),
      ]
    );
  }
  if (payload.followon) {
    const nextQty = manualFollowOnQty();
    const nextSide = String(payload.followon.kind === "reverse" && payload.side === "sell" ? "short" : "buy");
    const target =
      payload.followon.kind === "rotate"
        ? manualFollowOnTargetSymbol()
        : payload.symbol;
    const qtyLabel =
      payload.followon.qty_mode === "match"
        ? tx("followon_qty_match", "Same as closed")
        : formatQty(nextQty);
    const market = followonIsMarket(payload.followon);
    const confirmValue = market
      ? payload.followon.kind === "rotate"
        ? tx(
            "followon_confirm_rotate_market",
            "Buy {qty} {symbol} at market after the close fills",
            { qty: qtyLabel, symbol: target || "—" }
          )
        : tx(
            "followon_confirm_reverse_sell_market",
            "Short {qty} at market after the sell fills",
            { qty: qtyLabel }
          )
      : payload.followon.kind === "rotate"
        ? tx(
            "followon_confirm_rotate",
            "Buy {qty} {symbol} @ {price} after the close fills",
            {
              qty: qtyLabel,
              symbol: target || "—",
              price: stockPrice(payload.followon.limit_price),
            }
          )
        : tx(
            "followon_confirm_reverse_sell",
            "Short {qty} @ {price} after the sell fills",
            {
              qty: qtyLabel,
              price: stockPrice(payload.followon.limit_price),
            }
          );
    rows.push([tx("followon_confirm_row", "Next ticket"), confirmValue]);
    if (!market) {
      rows.push([
        tx("followon_confirm_cost", "Next-ticket cost"),
        nextSide === "short" || !(nextQty > 0)
          ? "—"
          : `≈ ${money(nextQty * payload.followon.limit_price)}`,
      ]);
    }
  }

  if (payload.dip_hunt) {
    rows.push(
      [
        tx("dip_hunt_confirm_row", "Dip hunt"),
        tx(
          "dip_hunt_confirm_value",
          "After stop-out: wait {wait}m for a {dip}% drop, or buy immediately if it prints first",
          {
            wait: String(payload.dip_hunt.wait_minutes),
            dip: String(payload.dip_hunt.dip_pct),
          }
        ),
      ]
    );
  }

  rows.push([tx("session", "Session"), formatSession(session) || "—"]);

  // Tone comes from the row that was pushed, not from comparing the rendered
  // label back against a translated string — which silently stopped colouring
  // anything the moment two labels happened to translate alike.
  summary.innerHTML = rows
    .map((entry) => {
      const [label, value, tone] = entry;
      const ddClass = tone ? `value-${tone}` : "";
      return `<div class="confirm-row">
           <dt>${escapeHtml(String(label))}</dt>
           <dd class="${ddClass}">${escapeHtml(String(value))}</dd>
         </div>`;
    })
    .join("");

  // Paper and live rendered identically here, which is exactly the moment the
  // difference matters most.
  const liveBanner = $("manual-confirm-live");
  const isLive = manualIsLiveAccount();
  if (liveBanner) liveBanner.hidden = !isLive;
  const content = $("manual-confirm-content");
  if (content) content.dataset.env = isLive ? "live" : "paper";

  // Desk limits. The server runs them for every action that opens risk, and a
  // short is one — gating on the Buy button alone hid them.
  const breachList = $("manual-confirm-breaches");
  const breaches = manualOpensRisk() ? manualPendingBreaches : [];
  if (breachList) {
    breachList.hidden = breaches.length === 0;
    breachList.innerHTML = breaches
      .map((b) => `<li>${escapeHtml(formatBreachMessage(b))}</li>`)
      .join("");
  }

  if (noteEl) {
    const notes = [];
    if (!calc || calc.blocked) {
      notes.push(
        tx(
          "confirm_no_estimate",
          "The desk will size this ticket on submit — the numbers above are incomplete."
        )
      );
    } else if (!calc.isExit && !calc.usesAtr && calc.stopPrice != null) {
      notes.push(
        tx("estimate_no_atr", "No ATR available — sized from the flat stop % instead of volatility.")
      );
    }
    if (calc && !calc.blocked && !calc.fromServer) {
      notes.push(
        tx(
          "confirm_local_estimate",
          "These numbers are the browser's estimate — the desk sizes the ticket again on submit."
        )
      );
    }
    if (calc && !calc.blocked && calc.exceedsBp) {
      notes.push(
        tx("estimate_over_bp", "Estimated cost exceeds buying power — Alpaca may reject this ticket.")
      );
    }
    if (isShortEntry) {
      notes.push(
        tx(
          "confirm_short_note",
          "This opens a short position: losses run without a ceiling until you buy the shares back, and no protective stop is attached. Close it from Positions."
        )
      );
    }
    if (payload.reinvest) {
      const sellPrice = manualSellReference();
      if (sellPrice > 0 && payload.reinvest.limit_price > sellPrice) {
        notes.push(
          tx(
            "reinvest_note_above",
            "The buy-back price is above the sell price — you would be buying the shares back for more than you sold them."
          )
        );
      }
      notes.push(
        tx(
          "reinvest_note_runtime",
          "The wait starts after the sell fills. If the buy-back does not fill in time, it is cancelled. Waiting plans resume after a restart."
        )
      );
    }
    if (payload.followon) {
      notes.push(
        tx(
          "followon_note_runtime",
          "The desk sends the next ticket after the close fills; waiting plans resume after a restart until they expire."
        )
      );
      if (followonIsMarket(payload.followon)) {
        notes.push(
          tx(
            "followon_note_market",
            "Market next tickets only send during regular hours."
          )
        );
      }
    }
    if (payload.dip_hunt) {
      notes.push(
        tx(
          "dip_hunt_note_runtime",
          "After a stop-out the desk hunts a cheaper re-entry; the cycle repeats until you cancel it or the take-profit fills. Live hunts resume after a restart."
        )
      );
    }
    noteEl.textContent = notes.join(" ");
    noteEl.hidden = notes.length === 0;
  }

  openConfirmModal();
}

function onManualSubmit(ev) {
  ev?.preventDefault?.();
  if (busy || loopRunning) return;
  const err = validateManualLocal();
  if (err) {
    setManualError(err);
    return;
  }
  setManualError(null);
  // Reuse the id after an uncertain failure so a network retry cannot become
  // another broker order. A changed form gets a different id automatically.
  const payload = { ...manualPayload(), ticket_id: ticketIdForCurrentTerms() };
  window.manualOrderPayload = payload;
  renderConfirmationModal(payload);
}

/** Ask the desk to size the ticket without sending it. */
async function onManualPreview() {
  if (busy || loopRunning) return;
  const err = validateManualLocal();
  if (err) {
    setManualError(err);
    return;
  }
  setManualError(null);
  const key = manualPreviewKey();
  const payload = { ...manualPayload(), preview: true, confirm_adjusted_qty: true };
  try {
    setBusy(true, tx("previewing", "Previewing…"));
    const data = await api("/api/order", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const result = data.result || {};
    // Feed the same cache the debounced preview fills, so the panel and the
    // "Last ticket" block are describing one calculation, not two.
    manualServerPreview = { key, result, estimate: estimateFromServer(result) };
    renderManualLastTicket(result);
    renderBreaches(result.breaches);
    updateSizeEstimate();
    showToast(tx("preview_ready", "Preview ready — nothing was sent."), "ok");
  } catch (e) {
    setManualError(e.message);
  } finally {
    setBusy(false);
    syncManualUi();
  }
}

const SUBMIT_BUSY_LABELS = {
  buy: ["submitting_buy", "Submitting buy…"],
  sell: ["submitting_sell", "Submitting sell…"],
  short: ["submitting_short", "Submitting short…"],
};

async function submitConfirmedOrder() {
  const payload = window.manualOrderPayload;
  if (!payload) return;
  const hasKnownBreaches =
    Array.isArray(manualPendingBreaches) && manualPendingBreaches.length > 0;
  closeConfirmModal();

  const [busyKey, busyFallback] =
    SUBMIT_BUSY_LABELS[payload.side] || SUBMIT_BUSY_LABELS.buy;
  const busyLabel = tx(busyKey, busyFallback);
  const sent = { ...payload };
  if (hasKnownBreaches) sent.override_breaches = true;

  try {
    setBusy(true, busyLabel);
    let data = await api("/api/order", {
      method: "POST",
      body: JSON.stringify(sent),
    });
    let r = data.result || {};

    // The desk can come back asking about an adjusted quantity. It used to be
    // a native window.confirm — a second, differently-styled dialog on top of
    // the one the user was already answering.
    if (r.needs_confirm && r.confirm_kind !== "breach") {
      renderManualLastTicket(r);
      const ok = await askInlineConfirm(
        `${formatQtyAdjustmentConfirm(r)} ${tx(
          "confirm_adjusted_qty",
          "Submit with the adjusted quantity?"
        )}`
      );
      if (!ok) {
        setManualError(tx("order_cancelled_qty", "Order cancelled — qty adjustment not confirmed."));
        showToast(tx("order_cancelled", "Order cancelled"), "error");
        return;
      }
      setBusy(true, busyLabel);
      data = await api("/api/order", {
        method: "POST",
        body: JSON.stringify({ ...sent, confirm_adjusted_qty: true }),
      });
      r = data.result || {};
    }

    // If a new breach appeared after preview, modal confirmation still counts
    // as acknowledgement: resend once with breach override.
    if (r.needs_confirm && r.confirm_kind === "breach") {
      setBusy(true, busyLabel);
      data = await api("/api/order", {
        method: "POST",
        body: JSON.stringify({ ...sent, override_breaches: true }),
      });
      r = data.result || {};
    }

    formDirtyManual = true;
    saveManualFormDraft();
    if (data.state?.account) applyAccount(data.state.account);
    if (data.state?.settings) hydrateManualFromSettings(data.state.settings, { force: false });
    renderManualLastTicket(r);
    rememberRecentTicket(r);
    // The book just changed, so the cached preview no longer describes it.
    manualServerPreview = null;

    if (r.duplicate) {
      showToast(
        tx(
          "order_duplicate_toast",
          "Identical to the ticket just sent — nothing was placed twice."
        ),
        "error"
      );
    } else {
      const otype = String(
        r.order_type || payload?.order_type || sent?.order_type || manualOrderType() || ""
      ).toLowerCase();
      const isMarket = otype === "market";
      const targetPage = isMarket ? "positions" : "orders";
      const targetLabel = isMarket
        ? tx("nav_positions", "Positions")
        : tx("nav_orders", "Orders");
      const toastActionLink = ` <a class="toast-link-btn" href="${pagePath(targetPage)}">${escapeHtml(
        targetLabel
      )}</a>`;
      const timeStr = typeof formatTradeExecutionTime === "function" ? formatTradeExecutionTime(r.submitted_at || r.ts || r.iso || Date.now()) : "";
      const timePart = timeStr ? ` · ${timeStr}` : "";
      const reasonLabel = tx("order_reason", "Reason");
      const cleanReason = typeof cleanTradeReason === "function" ? cleanTradeReason(r.reason || payload.intent || "") : String(r.reason || payload.intent || "").trim();
      const reasonPart = cleanReason ? ` · ${reasonLabel}: ${cleanReason}` : "";
      showToast(
        tx("order_submitted_toast", "{side} submitted", {
          side: String(r.side || payload.side).toUpperCase(),
        }) +
          timePart +
          reasonPart +
          (r.order_id ? ` · ${String(r.order_id).slice(0, 8)}…` : "") +
          (r.stop_loss?.stop_price != null
            ? ` · ${tx("stop_price", "Stop")} ${stockPrice(r.stop_loss.stop_price)}`
            : ""),
        "ok",
        toastActionLink
      );
    }
    // A definitive broker result ends this attempt. An intentional second
    // order with identical terms must receive a fresh client id.
    manualPendingTicket = null;
    await refreshManualContext().catch(() => {});
    // An armed buy-back is now the desk's business, not the form's — show it
    // in the queue straight away so it can be cancelled.
    if (r.reinvest) await refreshReinvestPlans();
    if (r.followon) await refreshFollowOnPlans();
    if (r.dip_hunt) await refreshDipHuntPlans();
  } catch (e) {
    setManualError(e.message);
  } finally {
    setBusy(false);
    await refreshStatus();
  }

  // Outside the busy gate: following the order must not lock the form, and a
  // fill changes the position, so refresh the context once it settles.
  const orderId = lastManualTicket?.order_id;
  if (orderId) {
    const final = await trackOrderFill(orderId);
    if (final?.is_terminal) {
      await refreshManualContext().catch(() => {});
      await refreshManualPositions().catch(() => {});
    }
  }
}

/**
 * A yes/no question answered inside the confirm modal.
 *
 * Replaces `window.confirm`, which blocked the page, ignored the desk's
 * styling, and could not be translated.
 */
function askInlineConfirm(
  message,
  {
    confirmLabel = tx("confirm_yes", "Yes, continue"),
    cancelLabel = tx("cancel", "Cancel"),
  } = {}
) {
  return new Promise((resolve) => {
    const modal = $("manual-confirm-modal");
    const summary = $("manual-confirm-summary");
    const noteEl = $("manual-confirm-note");
    const breachList = $("manual-confirm-breaches");
    const submit = $("btn-confirm-submit");
    const cancel = $("btn-confirm-cancel");
    if (!modal || !summary || !submit || !cancel) {
      resolve(false);
      return;
    }
    const originalLabels = {
      submitText: submit.textContent,
      submitKey: submit.getAttribute("data-i18n"),
      cancelText: cancel.textContent,
      cancelKey: cancel.getAttribute("data-i18n"),
    };
    if (breachList) breachList.hidden = true;
    summary.innerHTML = `<p class="confirm-question">${escapeHtml(message)}</p>`;
    if (noteEl) noteEl.hidden = true;
    submit.disabled = false;
    submit.removeAttribute("data-i18n");
    cancel.removeAttribute("data-i18n");
    submit.textContent = confirmLabel;
    cancel.textContent = cancelLabel;

    let settled = false;
    const finish = (answer, { close = true } = {}) => {
      if (settled) return;
      settled = true;
      submit.removeEventListener("click", onYes, true);
      cancel.removeEventListener("click", onNo, true);
      modal.removeEventListener("manualmodalclose", onDismiss);
      submit.textContent = originalLabels.submitText;
      cancel.textContent = originalLabels.cancelText;
      if (originalLabels.submitKey) submit.setAttribute("data-i18n", originalLabels.submitKey);
      if (originalLabels.cancelKey) cancel.setAttribute("data-i18n", originalLabels.cancelKey);
      if (close) closeConfirmModal();
      resolve(answer);
    };
    const onYes = (ev) => {
      ev.stopPropagation();
      finish(true);
    };
    const onNo = (ev) => {
      ev.stopPropagation();
      finish(false);
    };
    const onDismiss = () => finish(false, { close: false });
    // Capture phase so these run before the page's own submit handler.
    submit.addEventListener("click", onYes, { capture: true, once: true });
    cancel.addEventListener("click", onNo, { capture: true, once: true });
    modal.addEventListener("manualmodalclose", onDismiss, { once: true });
    openConfirmModal({ focus: "cancel" });
  });
}

/**
 * A one-line text answer, collected inside the same modal.
 *
 * `window.prompt` was the last native dialog on the page, and it failed the
 * same test that removed `window.confirm`: unstyled, unlocalisable, and it
 * froze the tab. Resolves to the trimmed string, or null if dismissed.
 */
function askInlinePrompt(
  message,
  {
    initial = "",
    confirmLabel = tx("save", "Save"),
    cancelLabel = tx("cancel", "Cancel"),
    maxLength = 40,
  } = {}
) {
  return new Promise((resolve) => {
    const modal = $("manual-confirm-modal");
    const summary = $("manual-confirm-summary");
    const noteEl = $("manual-confirm-note");
    const breachList = $("manual-confirm-breaches");
    const promptWrap = $("manual-confirm-prompt");
    const promptLabel = $("manual-confirm-prompt-label");
    const input = $("manual-confirm-prompt-input");
    const submit = $("btn-confirm-submit");
    const cancel = $("btn-confirm-cancel");
    if (!modal || !summary || !submit || !cancel || !promptWrap || !input) {
      resolve(null);
      return;
    }
    const originalLabels = {
      submitText: submit.textContent,
      submitKey: submit.getAttribute("data-i18n"),
      cancelText: cancel.textContent,
      cancelKey: cancel.getAttribute("data-i18n"),
    };
    if (breachList) breachList.hidden = true;
    if (noteEl) noteEl.hidden = true;
    summary.innerHTML = "";
    promptWrap.hidden = false;
    if (promptLabel) promptLabel.textContent = message;
    input.maxLength = maxLength;
    input.value = String(initial || "");
    submit.disabled = false;
    submit.removeAttribute("data-i18n");
    cancel.removeAttribute("data-i18n");
    submit.textContent = confirmLabel;
    cancel.textContent = cancelLabel;

    let settled = false;
    const finish = (answer, { close = true } = {}) => {
      if (settled) return;
      settled = true;
      submit.removeEventListener("click", onYes, true);
      cancel.removeEventListener("click", onNo, true);
      input.removeEventListener("keydown", onKey);
      modal.removeEventListener("manualmodalclose", onDismiss);
      submit.textContent = originalLabels.submitText;
      cancel.textContent = originalLabels.cancelText;
      if (originalLabels.submitKey) submit.setAttribute("data-i18n", originalLabels.submitKey);
      if (originalLabels.cancelKey) cancel.setAttribute("data-i18n", originalLabels.cancelKey);
      promptWrap.hidden = true;
      if (close) closeConfirmModal();
      resolve(answer);
    };
    const value = () => String(input.value || "").trim().slice(0, maxLength) || null;
    const onYes = (ev) => {
      ev.stopPropagation();
      finish(value());
    };
    const onNo = (ev) => {
      ev.stopPropagation();
      finish(null);
    };
    // Enter is what someone presses after typing a name; the modal is not a
    // form, so nothing would happen without this.
    const onKey = (ev) => {
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      ev.stopPropagation();
      finish(value());
    };
    const onDismiss = () => finish(null, { close: false });
    submit.addEventListener("click", onYes, { capture: true, once: true });
    cancel.addEventListener("click", onNo, { capture: true, once: true });
    input.addEventListener("keydown", onKey);
    modal.addEventListener("manualmodalclose", onDismiss, { once: true });
    openConfirmModal({ focus: "prompt" });
    input.select();
  });
}

/* -------------------------------------------------------- recent tickets --
 * "Last ticket" vanished on reload, which is precisely when someone wants to
 * check what they sent this morning against what Positions now shows.
 */

const MANUAL_RECENT_STORAGE_KEY = "alpaca-desk-manual-recent";
const MANUAL_MAX_RECENT = 8;

function readRecentTickets() {
  try {
    const raw = JSON.parse(localStorage.getItem(MANUAL_RECENT_STORAGE_KEY) || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

function rememberRecentTicket(result) {
  // Previews and suppressed duplicates never reached the broker, so they are
  // not part of the record of what was actually sent.
  if (!result || result.preview || result.needs_confirm || result.duplicate) return;
  if (!result.order_id) return;
  const row = {
    at: Date.now(),
    symbol: result.symbol,
    side: result.side,
    qty: result.order_qty,
    order_type: result.order_type,
    price: result.limit_price ?? result.stop_price ?? result.price,
    order_id: result.order_id,
  };
  const rows = [row, ...readRecentTickets()].slice(0, MANUAL_MAX_RECENT);
  try {
    localStorage.setItem(MANUAL_RECENT_STORAGE_KEY, JSON.stringify(rows));
  } catch {
    /* ignore quota / private mode */
  }
  renderRecentTickets();
}

function renderRecentTickets() {
  const panel = $("manual-recent-panel");
  const list = $("manual-recent-list");
  if (!panel || !list) return;
  const rows = readRecentTickets();
  if (!rows.length) {
    panel.hidden = true;
    list.innerHTML = "";
    return;
  }
  panel.hidden = false;
  list.innerHTML = rows
    .map((r) => {
      const when = new Date(Number(r.at) || 0);
      const time = Number.isFinite(when.getTime())
        ? formatDeskTime(when)
        : "";
      const price = Number(r.price) > 0 ? ` @ ${stockPrice(r.price)}` : "";
      const type = {
        market: tx("market", "Market"),
        limit: tx("limit", "Limit"),
        stop: tx("type_stop", "Stop"),
        stop_limit: tx("type_stop_limit", "Stop limit"),
        trailing_stop: tx("type_trailing_stop", "Trailing stop"),
      }[String(r.order_type || "")] || String(r.order_type || "");
      const head = `${String(r.side || "").toUpperCase()} ${formatQty(r.qty)} ${
        r.symbol || ""
      }${price}`;
      // Re-sending a ticket you just sent is the commonest action on a manual
      // desk, and this list was inert text. It loads the terms back into the
      // form — it never sends anything.
      return `<li class="manual-recent-item" data-side="${escapeHtml(
        String(r.side || "")
      )}">
          <button type="button" class="manual-recent-btn" data-recent="${escapeHtml(
            String(r.order_id || "")
          )}" title="${escapeHtml(tx("recent_reuse_hint", "Load these terms into the ticket"))}">
            <span class="manual-recent-head">${escapeHtml(head)}</span>
            <span class="manual-recent-meta">${escapeHtml(`${time} · ${type}`)}</span>
          </button>
        </li>`;
    })
    .join("");
}

/**
 * Load a previously sent ticket's terms back onto the form.
 *
 * Symbol, side, order type and price only — never the size, which has to be
 * re-derived from the risk engine against today's ATR and equity.
 */
function reuseRecentTicket(orderId) {
  const row = readRecentTickets().find((r) => String(r.order_id) === String(orderId));
  if (!row) return;
  if (loopRunning || busy) return;
  const symbol = String(row.symbol || "").trim().toUpperCase();
  if (/^[A-Z.\-]{1,12}$/.test(symbol)) setManualFormValue("symbol", symbol);
  const reusedSide = visibleTicketSide(row.side);
  if (reusedSide) setManualFormValue("side", reusedSide);
  if (
    ["market", "limit", "stop", "stop_limit", "trailing_stop"].includes(
      String(row.order_type)
    )
  ) {
    setManualFormValue("order_type", row.order_type);
  }
  const px = Number(row.price);
  if (px > 0) {
    if (manualNeedsLimit()) setManualFormValue("limit_price", px);
    if (manualNeedsTrigger()) setManualFormValue("stop_price", px);
  }
  formDirtyManual = true;
  saveManualFormDraft();
  syncManualUi();
  scheduleManualContextRefresh();
  scheduleServerPreview();
  showToast(tx("recent_reused", "Ticket terms loaded — nothing was sent."), "ok");
}

/* ------------------------------------------------------ position stop mgmt */

async function sendStopAction(body, busyKey, busyFallback) {
  if (busy || loopRunning) return;
  const note = $("manual-manage-note");
  try {
    setBusy(true, tx(busyKey, busyFallback));
    const data = await api("/api/position/stop", {
      method: "POST",
      body: JSON.stringify(body),
    });
    const stop = data.stop || {};
    const text =
      stop.type === "trailing_stop"
        ? tx("stop_trailing_armed", "Trailing stop armed at {pct}%", {
            pct: String(stop.trail_percent ?? ""),
          })
        : tx("stop_moved_to", "Stop moved to {price}", {
            price: stockPrice(stop.stop_price),
          });
    if (note) {
      note.textContent = text;
      note.classList.remove("warn");
    }
    showToast(text, "ok");
    await refreshManualContext().catch(() => {});
  } catch (err) {
    if (note) {
      note.textContent = err.message;
      note.classList.add("warn");
    }
    showToast(err.message, "error");
  } finally {
    setBusy(false);
    syncManualUi();
  }
}

/** Wiring */
$("manual-ctx-orders")?.addEventListener("click", async (ev) => {
  // Cancel buttons carry an inline SVG icon, so a click can land on the
  // <svg>/<path> instead of the button that owns the data attribute.
  const target = ev.target instanceof Element ? ev.target : null;
  const editBtn = target?.closest("[data-edit-order]");
  if (editBtn) {
    const list = $("manual-ctx-orders");
    list?.querySelectorAll("[data-edit-row]").forEach((row) => {
      const mine = row.dataset.editRow === editBtn.dataset.editOrder;
      row.hidden = mine ? !row.hidden : true;
      if (mine && !row.hidden) row.querySelector("input")?.focus();
    });
    return;
  }

  const saveBtn = target?.closest("[data-save-order]");
  if (saveBtn) {
    replaceRestingOrder(saveBtn.dataset.saveOrder).catch((err) =>
      showToast(err.message, "error")
    );
    return;
  }

  const cancelBtn = target?.closest("[data-cancel-order]");
  const id = cancelBtn?.dataset.cancelOrder;
  if (!id) return;
  const confirmed = await askInlineConfirm(
    tx(
      "confirm_cancel_resting",
      "Cancel this resting order? Protective coverage may change immediately."
    ),
    { confirmLabel: tx("cancel_order_action", "Cancel order") }
  );
  if (!confirmed) return;
  cancelRestingOrder(id).catch((err) => showToast(err.message, "error"));
});

$("manual-reinvest-list")?.addEventListener("click", (ev) => {
  const id = ev.target?.closest?.("[data-cancel-reinvest]")?.dataset?.cancelReinvest;
  if (!id) return;
  cancelReinvestPlan(id).catch((err) => showToast(err.message, "error"));
});

$("manual-followon-list")?.addEventListener("click", (ev) => {
  const id = ev.target?.closest?.("[data-cancel-followon]")?.dataset?.cancelFollowon;
  if (!id) return;
  cancelFollowOnPlan(id).catch((err) => showToast(err.message, "error"));
});

$("manual-dip-hunt-list")?.addEventListener("click", (ev) => {
  const id = ev.target?.closest?.("[data-cancel-dip-hunt]")?.dataset?.cancelDipHunt;
  if (!id) return;
  cancelDipHuntPlan(id).catch((err) => showToast(err.message, "error"));
});

$("btn-confirm-cancel")?.addEventListener("click", closeConfirmModal);

$("btn-confirm-submit")?.addEventListener("click", () => {
  submitConfirmedOrder().catch((err) => {
    showToast(err.message || tx("order_failed", "Order submission failed"), "error");
  });
});

// The backdrop sits above the modal box in hit-testing, so match either.
$("manual-confirm-modal")?.addEventListener("click", (ev) => {
  if (ev.target === ev.currentTarget || ev.target?.dataset?.modalDismiss) {
    closeConfirmModal();
  }
});

document.addEventListener("keydown", (ev) => {
  const modal = $("manual-confirm-modal");
  if (!modal || modal.hidden) return;
  if (ev.key === "Escape") {
    closeConfirmModal();
    return;
  }
  if (ev.key !== "Tab") return;
  const nodes = manualModalNodes();
  if (!nodes.length) return;
  const first = nodes[0];
  const last = nodes[nodes.length - 1];
  if (ev.shiftKey && document.activeElement === first) {
    ev.preventDefault();
    last.focus();
  } else if (!ev.shiftKey && document.activeElement === last) {
    ev.preventDefault();
    first.focus();
  }
});

const manualForm = $("manual-order");
manualForm?.addEventListener("submit", onManualSubmit);
$("btn-manual-preview")?.addEventListener("click", () => {
  onManualPreview().catch(() => {});
});
$("btn-manual-refresh")?.addEventListener("click", () => {
  refreshManualContext().catch(() => {});
});

/**
 * Copy a quote into a price field — the commonest keystroke on the page.
 *
 * Mark alone was the only choice, which puts a limit on the wrong side of a
 * wide spread about half the time. Both sides of the book are already on the
 * rail, so a buyer can rest at the bid and a seller at the ask.
 */
function fillFromQuote(field, which = "mark") {
  const quote = manualContext?.quote || {};
  let price = 0;
  if (which === "bid") {
    price = Number(quote.bid);
  } else if (which === "ask") {
    price = Number(quote.ask);
  } else if (which === "mid") {
    const bid = Number(quote.bid);
    const ask = Number(quote.ask);
    if (bid > 0 && ask > 0) {
      price = (bid + ask) / 2;
    } else {
      price = Number(quote.price);
    }
  } else {
    price = Number(quote.price);
  }
  if (!(price > 0)) {
    showToast(
      tx("err_no_quote_side", "That side of the quote is not available for this symbol."),
      "error"
    );
    return;
  }
  setManualFormValue(field, normalizeStockPrice(price));
  formDirtyManual = true;
  validateManualField(field);
  saveManualFormDraft();
  syncManualUi();
  scheduleServerPreview();
}

/** Fill the Dollar-amount box from a share of buying power — the Shares
 *  sizing mode has All/Half; this is its Dollars-mode equivalent. */
function fillNotionalFromBp(pct) {
  const bp = Number(manualContext?.buying_power);
  if (!(bp > 0)) {
    showToast(
      tx("err_no_equity", "Account equity is unavailable — connect Alpaca on API Keys before sizing a ticket."),
      "error"
    );
    return;
  }
  const amount = Math.floor((bp * pct) / 100);
  if (!(amount > 0)) return;
  setManualFormValue("notional", amount);
  formDirtyManual = true;
  validateManualField("notional");
  saveManualFormDraft();
  syncManualUi();
  scheduleServerPreview();
}
$("manual-notional-label")?.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-notional-fill]");
  if (!btn || btn.disabled) return;
  ev.preventDefault();
  fillNotionalFromBp(Number(btn.dataset.notionalFill));
});

$("manual-quick-chips")?.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-chip-symbol]");
  if (!btn) return;
  const sym = btn.dataset.chipSymbol;
  if (!sym) return;
  const el = $("manual-symbol");
  if (el) {
    el.value = sym;
    formDirtyManual = true;
    // A new symbol invalidates whatever share count the last one left behind
    // — otherwise a typed sell qty for the old symbol survives onto this one.
    sellQtyTouched = false;
    pendingSellFill = null;
    lastAutoSellQty = null;
    lastAutoSellFill = null;
    saveManualFormDraft();
    syncManualUi();
    scheduleManualContextRefresh();
    scheduleServerPreview();
    renderQuickChips();
  }
});
$("manual-sell-group")?.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-sell-fill]");
  if (!btn || btn.disabled) return;
  ev.preventDefault();
  fillSellQty(btn.dataset.sellFill);
});
$("btn-limit-bid")?.addEventListener("click", () => fillFromQuote("limit_price", "bid"));
$("btn-limit-mid")?.addEventListener("click", () => fillFromQuote("limit_price", "mid"));
$("btn-limit-mark")?.addEventListener("click", () => fillFromQuote("limit_price", "mark"));
$("btn-limit-ask")?.addEventListener("click", () => fillFromQuote("limit_price", "ask"));
$("btn-stop-bid")?.addEventListener("click", () => fillFromQuote("stop_price", "bid"));
$("btn-stop-mid")?.addEventListener("click", () => fillFromQuote("stop_price", "mid"));
$("btn-stop-mark")?.addEventListener("click", () => fillFromQuote("stop_price", "mark"));
$("btn-stop-ask")?.addEventListener("click", () => fillFromQuote("stop_price", "ask"));
$("btn-reinvest-bid")?.addEventListener("click", () => fillFromQuote("reinvest_limit_price", "bid"));
$("btn-reinvest-mid")?.addEventListener("click", () => fillFromQuote("reinvest_limit_price", "mid"));
$("btn-reinvest-mark")?.addEventListener("click", () => fillFromQuote("reinvest_limit_price", "mark"));
$("btn-reinvest-ask")?.addEventListener("click", () => fillFromQuote("reinvest_limit_price", "ask"));
$("btn-followon-bid")?.addEventListener("click", () => fillFromQuote("followon_limit_price", "bid"));
$("btn-followon-mid")?.addEventListener("click", () => fillFromQuote("followon_limit_price", "mid"));
$("btn-followon-mark")?.addEventListener("click", () => fillFromQuote("followon_limit_price", "mark"));
$("btn-followon-ask")?.addEventListener("click", () => fillFromQuote("followon_limit_price", "ask"));
$("btn-stop-limit-at-stop")?.addEventListener("click", () => {
  if (stopLimitPinnedToStop) {
    // Unpinning empties the box rather than leaving an absolute limit behind
    // that no longer looks pinned — empty is the documented "cushion or
    // market" state, and it is the only honest inverse of pinning.
    stopLimitPinnedToStop = false;
    const field = $("manual-order")?.elements?.stop_limit_price;
    if (field) field.value = "";
  } else {
    const stop = normalizeStockPrice(Number(currentEstimate()?.stopPrice));
    if (!(stop > 0)) {
      showToast(
        tx("err_no_stop_yet", "Size the ticket first so the stop price is known."),
        "error"
      );
      return;
    }
    stopLimitPinnedToStop = true;
    setManualFormValue("stop_limit_price", stop);
  }
  formDirtyManual = true;
  validateManualField("stop_limit_price");
  saveManualFormDraft();
  syncManualUi();
  scheduleServerPreview();
});

$("manual-preset")?.addEventListener("change", (ev) => {
  applyPresetByName(ev.target.value);
  syncPresetDeleteButton();
});
$("btn-manual-save-preset")?.addEventListener("click", () => {
  saveCurrentAsPreset().catch((err) => showToast(err.message, "error"));
});
$("btn-manual-save-preset-inline")?.addEventListener("click", () => {
  saveCurrentAsPreset().catch((err) => showToast(err.message, "error"));
});
$("btn-manual-delete-preset")?.addEventListener("click", () => {
  deleteSelectedPreset().catch((err) => showToast(err.message, "error"));
});

$("manual-recent-list")?.addEventListener("click", (ev) => {
  const id = ev.target?.closest?.("[data-recent]")?.dataset?.recent;
  if (id) reuseRecentTicket(id);
});

// The pinned mobile preview shows the headline only; the six-cell breakdown
// is one tap away rather than covering half the viewport.
$("btn-estimate-expand")?.addEventListener("click", () => {
  const panel = $("manual-size-estimate");
  const btn = $("btn-estimate-expand");
  if (!panel || !btn) return;
  const open = panel.classList.toggle("is-expanded");
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  btn.textContent = open ? tx("hide_details", "Hide") : tx("details", "Details");
});

$("btn-clear-recent")?.addEventListener("click", (ev) => {
  ev.stopPropagation();
  ev.preventDefault();
  try {
    localStorage.removeItem(MANUAL_RECENT_STORAGE_KEY);
  } catch {
    /* ignore private mode */
  }
  renderRecentTickets();
});

$("btn-stop-breakeven")?.addEventListener("click", () => {
  sendStopAction(
    { symbol: manualContext?.symbol, action: "breakeven" },
    "moving_stop",
    "Moving stop…"
  );
});
$("btn-stop-price")?.addEventListener("click", () => {
  const price = Number($("manual-manage-stop")?.value);
  if (!(price > 0)) {
    const note = $("manual-manage-note");
    if (note) {
      note.textContent = tx("err_stop_price", "Enter a stop price greater than $0.00.");
      note.classList.add("warn");
    }
    return;
  }
  sendStopAction(
    { symbol: manualContext?.symbol, action: "price", stop_price: price },
    "moving_stop",
    "Moving stop…"
  );
});
$("btn-stop-trail")?.addEventListener("click", () => {
  const pct = Number($("manual-manage-trail")?.value);
  if (!(pct > 0)) {
    const note = $("manual-manage-note");
    if (note) {
      note.textContent = tx("err_trail_percent", "Enter a trail percentage greater than 0.");
      note.classList.add("warn");
    }
    return;
  }
  sendStopAction(
    { symbol: manualContext?.symbol, action: "trail", trail_percent: pct },
    "arming_trail",
    "Arming trailing stop…"
  );
});
$("btn-manage-close")?.addEventListener("click", () => {
  closeManagedPosition().catch((err) => showToast(err.message, "error"));
});

/** Fields whose value changes what the desk would size, so re-price on edit. */
const MANUAL_SIZING_FIELDS = [
  "ai_risk_pct",
  "ai_atr_stop_mult",
  "sell_qty",
  "sell_notional",
  "notional",
  "buy_qty",
  "take_profit_r",
  "stop_limit_offset_pct",
  "stop_limit_price",
  "limit_price",
  "stop_price",
  "trail_percent",
  "bracket_enabled",
  "dip_hunt_enabled",
];

manualForm?.addEventListener("input", (ev) => {
  formDirtyManual = true;
  setManualError(null);
  const name = ev.target?.name;
  if (name === "sell_qty" || name === "sell_notional") {
    sellQtyTouched = true;
    pendingSellFill = null;
    lastAutoSellQty = null;
    lastAutoSellFill = null;
  }
  // Typing a limit of your own releases the pin. `syncStopLimitPin` writes the
  // element directly and fires no `input`, so it cannot unpin itself here.
  if (name === "stop_limit_price") stopLimitPinnedToStop = false;
  if (
    [
      ...MANUAL_SIZING_FIELDS,
      "reinvest_qty",
      "reinvest_limit_price",
      "reinvest_expire_minutes",
      "followon_qty",
      "followon_limit_price",
      "followon_target_symbol",
      "dip_hunt_wait_minutes",
      "dip_hunt_pct",
    ].includes(name)
  ) {
    validateManualField(name);
  }
  syncManualUi();
  saveManualFormDraft();
  // Only the debounce fetches on typing — `change` and `blur` would otherwise
  // fire two more requests for the same symbol.
  if (name === "symbol") scheduleManualContextRefresh();
  else if (MANUAL_SIZING_FIELDS.includes(name)) scheduleServerPreview();
});

manualForm?.addEventListener("change", (ev) => {
  formDirtyManual = true;
  setManualError(null);
  const name = ev.target?.name;
  if (name === "followon_enabled" && ev.target?.checked) {
    setManualFormValue("reinvest_enabled", false);
  } else if (name === "reinvest_enabled" && ev.target?.checked) {
    setManualFormValue("followon_enabled", false);
  }
  if (name === "followon_target_symbol") {
    const el = ev.target;
    if (el) el.value = String(el.value || "").trim().toUpperCase();
  }
  if (name === "followon_order_type") {
    validateManualField("followon_limit_price");
  }
  if (name === "sell_mode") {
    convertSellQtyOnUnitToggle(ev.target?.value);
  }
  if (name === "buy_size_mode" && manualContext) {
    applyStockPriceDefaults(manualContext);
  }
  syncManualUi();
  saveManualFormDraft();
  if (name === "symbol") scheduleManualContextRefresh();
  else scheduleServerPreview();
});

// A wheel over a focused number input silently re-prices the ticket while the
// user is only trying to scroll the form.
manualForm?.addEventListener(
  "wheel",
  (ev) => {
    const el = ev.target;
    if (
      el instanceof HTMLInputElement &&
      el.type === "number" &&
      document.activeElement === el
    ) {
      ev.preventDefault();
    }
  },
  { passive: false }
);

$("manual-symbol")?.addEventListener("blur", () => {
  const el = $("manual-symbol");
  if (!el) return;
  const next = String(el.value || "").trim().toUpperCase();
  const changed = next !== el.value;
  el.value = next;
  saveManualFormDraft();
  if (changed || next !== manualContext?.symbol) scheduleManualContextRefresh();
});

/**
 * Keyboard shortcuts.
 *
 * Suppressed while typing into a field or with the modal open, so they can
 * never fire an order the user was in the middle of composing.
 */
document.addEventListener("keydown", (ev) => {
  if (ev.altKey) return;
  const modal = $("manual-confirm-modal");
  if (modal && !modal.hidden) return;
  const target = ev.target;
  // nice-select2 swaps the real <select> for a focusable, non-<select> div —
  // Tab lands there, and without this a lone "s" while browsing Order type
  // options flips the ticket to Sell instead of picking an option.
  const typing =
    target instanceof HTMLElement &&
    (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) ||
      target.isContentEditable ||
      target.closest(".nice-select"));

  // Submit works from inside a field — it is the one shortcut you want while
  // your hands are still on the size box.
  if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
    ev.preventDefault();
    onManualSubmit(ev);
    return;
  }
  if (typing || ev.metaKey || ev.ctrlKey) return;

  const pick = (side) => {
    ev.preventDefault();
    selectManualSide(side);
  };
  switch (ev.key.toLowerCase()) {
    case "b":
      pick("buy");
      break;
    case "s":
      pick("sell");
      break;
    case "p":
      ev.preventDefault();
      onManualPreview().catch(() => {});
      break;
    default:
      break;
  }
});

restoreManualFormDraft();
if (applyManualTicketFromUrl()) saveManualFormDraft();
renderManualPresets();
renderRecentTickets();
renderQuickChips();
syncManualUi();
refreshStatus({ forceSettings: true })
  .then(() => refreshManualContext().catch(() => {}))
  .then(() => scheduleServerPreview())
  .catch((err) => showToast(err.message, "error"));
// Plans outlive the page: one armed on a previous visit is still running.
refreshReinvestPlans().catch(() => {});
refreshFollowOnPlans().catch(() => {});
refreshDipHuntPlans().catch(() => {});
refreshManualPositions().catch(() => {});

function onDeskStatusUpdate(state) {
  if (state.settings) hydrateManualFromSettings(state.settings);
  renderQuickChips();
  syncManualUi();
}

/** Keep the mark honest — a stale quote makes every number on the page a lie. */
function onDeskStatusInterval() {
  if (busy || loopRunning || document.hidden) return;
  const modal = $("manual-confirm-modal");
  if (modal && !modal.hidden) return;
  if (Date.now() - manualContextFetchedAt < MANUAL_CONTEXT_REFRESH_MS) return;
  refreshManualContext().catch(() => {});
}

function onDeskLanguageChange() {
  // Strings rendered from JS are not covered by translateDOM.
  document.title = `${tx("manual_order_heading", "Advanced Order")} · ${tx(
    "app_title",
    "AlgoPaca"
  )}`;
  syncManualUi();
  applyManualContext(manualContext);
  renderBreaches(manualPendingBreaches);
  if (lastManualTicket) renderManualLastTicket(lastManualTicket);
  renderReinvestPlans(lastReinvestPlans);
  renderFollowOnPlans(lastFollowOnPlans);
  renderManualPresets();
  renderRecentTickets();
}
