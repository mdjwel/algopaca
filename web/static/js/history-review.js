/**
 * Desk Review for the History page.
 *
 * Two layers, deliberately separated. The deterministic layer — flags,
 * calibration, post-mortems, the execution audit — is computed on the server
 * from the same fills the page already shows, costs nothing, and refreshes with
 * every filter change. The narration layer is a single explicit model call the
 * user asks for; it explains those numbers and never produces one.
 *
 * Loaded after history.js, so it reads that file's page state directly.
 */

const REVIEW_SCOPES = ["debrief", "postmortem", "audit"];
/** Flags the server can raise, each rendered through the locale files. */
const REVIEW_FLAG_KEYS = {
  small_sample: "review_flag_small_sample",
  concentrated: "review_flag_concentrated",
  losing_edge: "review_flag_losing_edge",
  thin_edge: "review_flag_thin_edge",
  payoff_upside_down: "review_flag_payoff_upside_down",
  calibration_inverted: "review_flag_calibration_inverted",
  calibration_thin: "review_flag_calibration_thin",
  wide_spread_entries: "review_flag_wide_spread_entries",
  gates_dominant: "review_flag_gates_dominant",
  market_closed_skips: "review_flag_market_closed_skips",
  unmatched_sells: "review_flag_unmatched_sells",
  window_truncated: "review_flag_window_truncated",
};

let reviewScope = "debrief";
let reviewFacts = null;
/** Narration per scope, so switching tabs never re-bills a finished call. */
const reviewNarrations = new Map();
let reviewBusy = false;
let reviewFactsSeq = 0;
let reviewFactsTimer = null;
let reviewCalibrationChart = null;
let reviewHourChart = null;
let savedLessons = [];
let queryBusy = false;
/** Whether an AI provider key is configured, per the last facts response. */
let reviewAiAvailable = true;

/* ── shared helpers ──────────────────────────────────────────────── */

function reviewOnPage() {
  return !!document.getElementById("history-review");
}

/**
 * The range the review describes. Symbol and side are deliberately absent:
 * they filter the fill list, not the FIFO walk these figures come from, so
 * sending them would only label the review with a scope it does not keep.
 */
function reviewRequestBody(extra = {}) {
  return {
    range: historyRange,
    start: historyStart,
    end: historyEnd,
    // The switcher is browser state; it only reaches the desk when someone
    // actually changes it. Say the language on every call, or a page restored
    // into Bangla gets an English review beside its Bangla flags.
    lang: reviewLang(),
    ...extra,
  };
}

function reviewSignature() {
  return [
    historyRange,
    historyStart,
    historyEnd,
    lastTradesPayload?.window_trade_count ?? "",
    lastTradesPayload?.realized_range_pnl ?? "",
  ].join("|");
}

/**
 * Pick the copy that matches the picked UI language.
 *
 * Narration and lessons are written in the desk language of the moment and
 * carry an English twin for exactly this. Switching the desk to another
 * language must not leave the old language's prose on screen, and re-running
 * the model to fix that would bill a call nobody asked for — so fall back to
 * the English copy whenever the stored language is not the one on screen.
 * (Same rule as `noteFor` on Auto Trade.)
 */
function reviewLang() {
  return typeof i18n !== "undefined" ? i18n.getCurrentLanguage() : "en";
}

function reviewText(localized, english, sourceLang) {
  const source = String(sourceLang || "").trim().toLowerCase();
  const local = String(localized || "").trim();
  const en = String(english || "").trim();
  if (source && source !== reviewLang()) return en || local;
  return local || en;
}

function reviewTextList(localized, english, sourceLang) {
  const source = String(sourceLang || "").trim().toLowerCase();
  const local = Array.isArray(localized) ? localized : [];
  const en = Array.isArray(english) ? english : [];
  if (source && source !== reviewLang() && en.length) return en;
  return local.length ? local : en;
}

function pctText(value) {
  return value == null ? "—" : `${Number(value).toFixed(1)}%`;
}

function reviewChipCount(count) {
  return tx(
    Number(count) === 1 ? "count_entry" : "count_entries",
    Number(count) === 1 ? "1 entry" : "{count} entries",
    { count: String(count) }
  );
}

/* ── deterministic facts ─────────────────────────────────────────── */

/**
 * Show the review only where it has a ledger to read, and Ask only where it
 * can answer. Ask is a bare model call with no deterministic half — without a
 * provider key it could return nothing but an error, so it stays out of the
 * way, the same rule the Explain button follows.
 */
function syncReviewVisibility() {
  const showAlpaca = historySource === "alpaca" || historySource === "both";
  const block = document.getElementById("history-review");
  const queryBox = document.getElementById("history-ai-query");
  if (block) block.hidden = !showAlpaca;
  if (queryBox) queryBox.hidden = !showAlpaca || !reviewAiAvailable;
  return showAlpaca;
}

function setReviewAiAvailable(available) {
  reviewAiAvailable = available !== false;
  syncReviewVisibility();
  syncReviewRunButton();
}

/**
 * Called by history.js after every fill render. Debounced because typing in
 * the symbol box re-renders on each keystroke, and each fetch re-walks FIFO.
 */
function onHistoryTradesRendered() {
  if (!reviewOnPage()) return;
  if (!syncReviewVisibility()) return;
  clearTimeout(reviewFactsTimer);
  reviewFactsTimer = setTimeout(() => {
    loadReviewFacts().catch(() => {});
  }, 350);
}

async function loadReviewFacts() {
  const seq = ++reviewFactsSeq;
  const signature = reviewSignature();
  let data;
  try {
    data = await api("/api/history/insights", {
      method: "POST",
      body: JSON.stringify(reviewRequestBody({ narrate: false })),
    });
  } catch (err) {
    if (seq !== reviewFactsSeq) return;
    renderReviewError(err?.message || String(err));
    return;
  }
  // A slower earlier request must never overwrite a newer answer.
  if (seq !== reviewFactsSeq) return;
  reviewFacts = data.facts || null;
  // The numbers moved, so any narration written about the old ones is stale.
  if (signature !== reviewNarrations.get("__signature")) {
    reviewNarrations.clear();
    reviewNarrations.set("__signature", signature);
    renderNarrative(null);
  }
  setReviewAiAvailable(data.ai_available);
  renderReviewFacts();
}

function renderReviewFacts() {
  renderReviewFlags();
  renderCalibrationChart();
  renderPostmortems();
  renderAudit();
  syncReviewPanes();
}

function renderReviewError(message) {
  const box = document.getElementById("review-flags");
  if (!box) return;
  box.innerHTML =
    `<p class="review-flag" data-severity="risk">${escapeHtml(
      tx("review_error", "Review unavailable: {error}", { error: message })
    )}</p>`;
}

function renderReviewFlags() {
  const box = document.getElementById("review-flags");
  if (!box) return;
  const flags = reviewFacts?.flags || [];
  if (!flags.length) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = flags
    .map((flag) => {
      const key = REVIEW_FLAG_KEYS[flag.code];
      if (!key) return "";
      const params = {};
      Object.entries(flag.values || {}).forEach(([name, value]) => {
        params[name] = typeof value === "number" ? String(value) : String(value ?? "");
      });
      const text = tx(key, flag.code, params);
      return `<p class="review-flag" data-severity="${escapeHtml(
        flag.severity || "info"
      )}">${escapeHtml(text)}</p>`;
    })
    .join("");
}

/* ── calibration ─────────────────────────────────────────────────── */

function reviewChartTheme() {
  return {
    buy: cssVar("--buy", "#3fbf8f"),
    sell: cssVar("--sell", "#e35d5d"),
    copper: cssVar("--copper", "#d4894c"),
    muted: cssVar("--muted", "#9aa8b8"),
    line: cssVar("--line", "#2a384c"),
  };
}

function renderCalibrationChart() {
  const canvas = document.getElementById("review-calibration-chart");
  const empty = document.getElementById("review-calibration-empty");
  const wrap = canvas?.closest(".review-chart-wrap");
  if (!canvas) return;
  if (reviewCalibrationChart) {
    reviewCalibrationChart.destroy();
    reviewCalibrationChart = null;
  }
  const buckets = (reviewFacts?.calibration?.buckets || []).filter(
    (b) => b.entries > 0
  );
  const hasData = buckets.length > 0 && typeof Chart !== "undefined";
  if (empty) empty.hidden = hasData;
  if (wrap) wrap.hidden = !hasData;
  if (!hasData) return;

  const theme = reviewChartTheme();
  reviewCalibrationChart = new Chart(canvas.getContext("2d"), {
    type: "bar",
    data: {
      labels: buckets.map((b) => b.label),
      datasets: [
        {
          label: tx("stat_win_rate", "Win rate"),
          data: buckets.map((b) => b.win_rate ?? 0),
          // Green above a coin flip, red below it: the reference the reader
          // actually cares about is 50%, not zero.
          backgroundColor: buckets.map((b) =>
            (b.win_rate ?? 0) >= 50 ? theme.buy : theme.sell
          ),
          borderRadius: 3,
          maxBarThickness: 46,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const bucket = buckets[ctx.dataIndex] || {};
              return [
                `${tx("stat_win_rate", "Win rate")}: ${pctText(bucket.win_rate)}`,
                `${reviewChipCount(bucket.entries)}`,
                `${tx("realized_pnl", "Realized")}: ${formatPnl(bucket.realized)}`,
              ];
            },
          },
        },
      },
      scales: {
        x: {
          grid: { color: theme.line, drawTicks: false },
          ticks: { color: theme.muted, font: { size: 10 } },
        },
        y: {
          min: 0,
          max: 100,
          grid: { color: theme.line, drawTicks: false },
          ticks: {
            color: theme.muted,
            font: { size: 10 },
            callback: (value) => `${value}%`,
          },
        },
      },
    },
  });
}

/* ── post-mortems ────────────────────────────────────────────────── */

function reviewBiasChip(labelKey, fallback, value) {
  if (!value) return "";
  return (
    `<span class="review-cond"><span class="review-cond-key">${escapeHtml(
      tx(labelKey, fallback)
    )}</span><span class="review-cond-val">${escapeHtml(String(value))}</span></span>`
  );
}

function renderPostmortems() {
  const box = document.getElementById("review-postmortems");
  if (!box) return;
  const rows = reviewFacts?.postmortems || [];
  if (!rows.length) {
    box.innerHTML = `<p class="review-empty">${escapeHtml(
      tx(
        "review_pm_empty",
        "No closed trades in this range yet — a post-mortem needs a buy and the sell that retired it."
      )
    )}</p>`;
    return;
  }
  box.innerHTML = rows
    .map((row) => {
      const win = Number(row.realized) >= 0;
      const conditions = [
        reviewBiasChip("review_pm_regime", "Regime", row.regime),
        reviewBiasChip("review_pm_spread", "Spread", row.spread_bps != null ? `${row.spread_bps} bps` : ""),
        reviewBiasChip("review_pm_adx", "ADX", row.adx),
        reviewBiasChip("review_pm_atr", "ATR", row.atr_pct != null ? `${row.atr_pct}%` : ""),
        reviewBiasChip("review_pm_htf", "Daily trend", row.htf_bias),
        reviewBiasChip("review_pm_news", "News", row.news_bias),
      ]
        .filter(Boolean)
        .join("");
      const managed = Array.isArray(row.managed) ? row.managed : [];
      const confidence =
        row.confidence != null
          ? `<span class="review-pm-conf">${escapeHtml(
              tx("review_pm_confidence", "Confidence {value}", {
                value: Number(row.confidence).toFixed(2),
              })
            )}</span>`
          : "";
      const attribution = [row.mode ? String(row.mode).toUpperCase() : "", row.preset]
        .filter(Boolean)
        .join(" · ");
      return (
        `<article class="review-pm" data-outcome="${win ? "win" : "loss"}">` +
        `<header class="review-pm-head">` +
        `<span class="review-pm-symbol">${escapeHtml(row.symbol || "—")}</span>` +
        `<span class="review-pm-pnl ${win ? "pos" : "neg"}">${escapeHtml(
          formatPnl(row.realized)
        )}${row.realized_pct != null ? ` (${Number(row.realized_pct).toFixed(2)}%)` : ""}</span>` +
        `<span class="review-pm-meta">${escapeHtml(
          [formatEtDate(row.opened_at, { withTime: true }), attribution]
            .filter(Boolean)
            .join(" · ")
        )}</span>` +
        confidence +
        `</header>` +
        (row.thesis
          ? `<p class="review-pm-thesis"><span class="review-pm-label">${escapeHtml(
              tx("review_pm_thesis", "Thesis at entry")
            )}</span>${escapeHtml(row.thesis)}</p>`
          : "") +
        (row.risks
          ? `<p class="review-pm-risks"><span class="review-pm-label">${escapeHtml(
              tx("review_pm_risks", "Risks flagged")
            )}</span>${escapeHtml(row.risks)}</p>`
          : "") +
        (conditions ? `<div class="review-pm-conds">${conditions}</div>` : "") +
        (managed.length
          ? `<p class="review-pm-managed">${escapeHtml(
              tx("review_pm_managed", "Managed: {actions}", {
                actions: managed.join("; "),
              })
            )}</p>`
          : "") +
        `</article>`
      );
    })
    .join("");
}

/* ── execution audit ─────────────────────────────────────────────── */

function reviewStatRow(labelKey, fallback, value, params) {
  return (
    `<div class="review-stat"><span>${escapeHtml(
      tx(labelKey, fallback, params)
    )}</span><strong>${escapeHtml(String(value))}</strong></div>`
  );
}

function renderAudit() {
  const box = document.getElementById("review-audit");
  if (!box) return;
  const execution = reviewFacts?.execution || {};
  const spread = execution.spread_bps || {};
  const hasAny =
    execution.blocked_total ||
    execution.skipped_total ||
    execution.stop_moves ||
    execution.scale_outs ||
    spread.samples;
  if (!hasAny) {
    box.innerHTML = `<p class="review-empty">${escapeHtml(
      tx(
        "review_audit_empty",
        "Nothing to audit yet. Risk-gate blocks, skipped cycles and stop moves are recorded as the desk runs."
      )
    )}</p>`;
    renderHourChart();
    return;
  }

  const reasonList = (rows, titleKey, titleFallback) => {
    if (!rows || !rows.length) return "";
    return (
      `<div class="review-audit-group">` +
      `<h5 class="review-audit-title">${escapeHtml(tx(titleKey, titleFallback))}</h5>` +
      `<div class="symbol-pnl">` +
      rows
        .map(
          (row) =>
            `<span class="symbol-pnl-chip is-static">` +
            `<span class="symbol-pnl-sym">${escapeHtml(row.reason)}</span>` +
            `<span class="symbol-pnl-count">${escapeHtml(
              tx("review_times", "{count}×", { count: String(row.count) })
            )}</span></span>`
        )
        .join("") +
      `</div></div>`
    );
  };

  box.innerHTML =
    `<div class="history-stats review-stats">` +
    reviewStatRow("review_audit_blocked", "Blocked by risk gates", execution.blocked_total || 0) +
    reviewStatRow("review_audit_skipped", "Cycles skipped", execution.skipped_total || 0) +
    reviewStatRow("review_audit_stop_moves", "Stop moves", execution.stop_moves || 0) +
    reviewStatRow("review_audit_scale_outs", "Scale-outs", execution.scale_outs || 0) +
    reviewStatRow(
      "review_audit_avg_spread",
      "Avg entry spread",
      spread.avg != null ? `${spread.avg} bps` : "—"
    ) +
    reviewStatRow(
      "review_audit_wide",
      "Wide-spread entries",
      `${spread.wide_entries || 0}${
        spread.wide_losses ? ` (${spread.wide_losses} ${tx("stat_losses", "losses")})` : ""
      }`
    ) +
    `</div>` +
    reasonList(execution.blocked, "review_audit_block_reasons", "Why entries were blocked") +
    reasonList(execution.skipped, "review_audit_skip_reasons", "Why cycles were skipped");

  renderHourChart();
}

function renderHourChart() {
  const canvas = document.getElementById("review-hour-chart");
  const heading = document.getElementById("review-hour-heading");
  const wrap = canvas?.closest(".review-chart-wrap");
  if (!canvas) return;
  if (reviewHourChart) {
    reviewHourChart.destroy();
    reviewHourChart = null;
  }
  const rows = (reviewFacts?.execution?.by_hour || []).filter((r) => r.entries > 0);
  const hasData = rows.length > 1 && typeof Chart !== "undefined";
  if (heading) heading.hidden = !hasData;
  if (wrap) wrap.hidden = !hasData;
  if (!hasData) return;

  const theme = reviewChartTheme();
  reviewHourChart = new Chart(canvas.getContext("2d"), {
    type: "bar",
    data: {
      labels: rows.map((r) => `${String(r.hour).padStart(2, "0")}:00`),
      datasets: [
        {
          label: tx("realized_pnl", "Realized"),
          data: rows.map((r) => Number(r.realized) || 0),
          backgroundColor: rows.map((r) =>
            Number(r.realized) >= 0 ? theme.buy : theme.sell
          ),
          borderRadius: 3,
          maxBarThickness: 40,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const row = rows[ctx.dataIndex] || {};
              return [
                `${tx("realized_pnl", "Realized")}: ${formatPnl(row.realized)}`,
                reviewChipCount(row.entries),
                `${tx("stat_win_rate", "Win rate")}: ${pctText(row.win_rate)}`,
              ];
            },
          },
        },
      },
      scales: {
        x: {
          grid: { color: theme.line, drawTicks: false },
          ticks: { color: theme.muted, font: { size: 10 } },
        },
        y: {
          grid: { color: theme.line, drawTicks: false },
          ticks: {
            color: theme.muted,
            font: { size: 10 },
            callback: (value) => money(value),
          },
        },
      },
    },
  });
}

/* ── narration ───────────────────────────────────────────────────── */

function syncReviewRunButton() {
  const btn = document.getElementById("btn-review-run");
  if (!btn) return;
  btn.disabled = reviewBusy || !reviewAiAvailable;
  btn.textContent = reviewBusy
    ? tx("review_running", "Reading the range…")
    : tx("review_run", "Explain this range");
  btn.title = reviewAiAvailable
    ? ""
    : tx("review_no_key", "Add an AI provider key on Configuration to get a written review.");
}

function renderNarrative(narration, meta = {}) {
  const box = document.getElementById("review-narrative");
  const model = document.getElementById("review-model");
  if (!box) return;
  if (!narration) {
    box.innerHTML = "";
    box.classList.remove("is-filled");
    if (model) model.hidden = true;
    renderSuggestedLessons([]);
    return;
  }
  box.classList.add("is-filled");
  const headline = reviewText(
    narration.headline,
    narration.headline_en,
    narration.lang
  );
  const bullets = reviewTextList(
    narration.bullets,
    narration.bullets_en,
    narration.lang
  );
  box.innerHTML =
    (headline ? `<p class="review-headline">${escapeHtml(headline)}</p>` : "") +
    bullets.map((text) => `<p>${escapeHtml(text)}</p>`).join("") +
    `<p class="review-disclaimer">${escapeHtml(
      tx(
        "review_disclaimer",
        "A read of past paper trades, not advice and not a forecast."
      )
    )}</p>`;
  if (model) {
    model.hidden = false;
    model.textContent = [
      narration.provider,
      narration.model,
      meta.cached ? tx("review_cached", "cached") : "",
    ]
      .filter(Boolean)
      .join(" · ");
  }
  renderSuggestedLessons(narration.lessons || [], narration.lang);
}

async function runReviewNarration() {
  if (reviewBusy) return;
  const cached = reviewNarrations.get(reviewScope);
  // A narration written in another language is still shown (via its English
  // twin) but must not satisfy a fresh Explain — that click is how the reader
  // asks for this range in the language now on screen.
  if (cached && String(cached.lang || "").toLowerCase() === reviewLang()) {
    renderNarrative(cached, { cached: true });
    return;
  }
  reviewBusy = true;
  syncReviewRunButton();
  try {
    const data = await api("/api/history/insights", {
      method: "POST",
      body: JSON.stringify(reviewRequestBody({ scope: reviewScope, narrate: true })),
    });
    reviewFacts = data.facts || reviewFacts;
    // Latch the key state from this same answer; the `finally` below re-syncs
    // from it, so a "no key" reply can no longer hand the button straight back.
    setReviewAiAvailable(data.ai_available);
    renderReviewFacts();
    if (data.narration) {
      reviewNarrations.set(reviewScope, data.narration);
      renderNarrative(data.narration, { cached: data.cached });
    } else if (data.narration_error === "no_ai_key") {
      showToast(
        tx(
          "review_no_key",
          "Add an AI provider key on Configuration to get a written review."
        ),
        "error"
      );
      return;
    } else {
      showToast(
        tx("review_error", "Review unavailable: {error}", {
          error: data.narration_error || "unknown",
        }),
        "error"
      );
    }
  } catch (err) {
    showToast(
      tx("review_error", "Review unavailable: {error}", {
        error: err?.message || String(err),
      }),
      "error"
    );
  } finally {
    reviewBusy = false;
    syncReviewVisibility();
    syncReviewRunButton();
  }
}

/* ── scope tabs ──────────────────────────────────────────────────── */

function syncReviewPanes() {
  REVIEW_SCOPES.forEach((scope) => {
    const pane = document.getElementById(`review-pane-${scope}`);
    if (pane) pane.hidden = scope !== reviewScope;
  });
  document.querySelectorAll("[data-review-scope]").forEach((btn) => {
    const active = btn.dataset.reviewScope === reviewScope;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
}

function setReviewScope(scope) {
  if (!REVIEW_SCOPES.includes(scope) || scope === reviewScope) return;
  reviewScope = scope;
  syncReviewPanes();
  // Each scope gets its own narration; show the one already paid for, if any.
  renderNarrative(reviewNarrations.get(scope) || null, { cached: true });
}

/* ── lessons ─────────────────────────────────────────────────────── */

function lessonScopeLabel(lesson) {
  const scope = String(lesson.scope || "global");
  if (scope === "symbol" && lesson.target) {
    return tx("review_lesson_scope_symbol", "For {target}", { target: lesson.target });
  }
  if (scope === "preset" && lesson.target) {
    return tx("review_lesson_scope_preset", "Preset {target}", { target: lesson.target });
  }
  return tx("review_lesson_scope_global", "All symbols");
}

function renderSuggestedLessons(lessons, sourceLang) {
  const block = document.getElementById("review-lessons-block");
  const box = document.getElementById("review-lessons");
  if (!block || !box) return;
  const rows = Array.isArray(lessons) ? lessons : [];
  block.hidden = !rows.length;
  if (!rows.length) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = rows
    .map((lesson, index) => {
      return (
        `<div class="review-lesson" data-lesson-index="${index}">` +
        `<div class="review-lesson-body">` +
        `<span class="review-lesson-scope">${escapeHtml(lessonScopeLabel(lesson))}</span>` +
        `<p>${escapeHtml(
          reviewText(lesson.text, lesson.text_en, sourceLang)
        )}</p>` +
        `</div>` +
        `<button type="button" class="ghost review-lesson-save" data-save-lesson="${index}">${escapeHtml(
          tx("review_lesson_save", "Save to playbook")
        )}</button>` +
        `</div>`
      );
    })
    .join("");
  // Stash the objects; the buttons only carry an index.
  box.dataset.lessons = JSON.stringify(rows);
}

function renderSavedLessons() {
  const block = document.getElementById("review-saved-block");
  const box = document.getElementById("review-saved");
  if (!block || !box) return;
  block.hidden = !savedLessons.length;
  if (!savedLessons.length) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = savedLessons
    .map((lesson) => {
      const enabled = lesson.enabled !== false;
      return (
        `<div class="review-lesson is-saved" data-lesson-id="${lesson.id}" data-enabled="${enabled}">` +
        `<div class="review-lesson-body">` +
        `<span class="review-lesson-scope">${escapeHtml(lessonScopeLabel(lesson))}</span>` +
        `<p>${escapeHtml(
          reviewText(lesson.text, lesson.text_en, lesson.lang)
        )}</p>` +
        `</div>` +
        `<div class="review-lesson-actions">` +
        `<button type="button" class="ghost" data-toggle-lesson="${lesson.id}">${escapeHtml(
          enabled
            ? tx("review_lesson_disable", "Pause")
            : tx("review_lesson_enable", "Resume")
        )}</button>` +
        `<button type="button" class="ghost ghost-danger" data-delete-lesson="${lesson.id}">${escapeHtml(
          tx("review_lesson_delete", "Remove")
        )}</button>` +
        `</div></div>`
      );
    })
    .join("");
}

async function loadSavedLessons() {
  try {
    const data = await api("/api/history/lessons");
    savedLessons = Array.isArray(data.lessons) ? data.lessons : [];
  } catch {
    savedLessons = [];
  }
  renderSavedLessons();
}

async function saveLesson(index) {
  const box = document.getElementById("review-lessons");
  if (!box) return;
  let rows = [];
  try {
    rows = JSON.parse(box.dataset.lessons || "[]");
  } catch {
    rows = [];
  }
  const lesson = rows[index];
  if (!lesson) return;
  try {
    const data = await api("/api/history/lessons", {
      method: "POST",
      body: JSON.stringify({
        text: lesson.text || lesson.text_en || "",
        text_en: lesson.text_en || "",
        scope: lesson.scope || "global",
        target: lesson.target || "",
        lang: reviewNarrations.get(reviewScope)?.lang || "en",
        source_range: historyRange,
      }),
    });
    savedLessons = Array.isArray(data.lessons) ? data.lessons : savedLessons;
    renderSavedLessons();
    showToast(tx("review_lesson_saved", "Lesson added to the playbook."), "ok");
    const btn = box.querySelector(`[data-save-lesson="${index}"]`);
    if (btn) {
      btn.disabled = true;
      btn.textContent = tx("review_lesson_in_playbook", "In playbook");
    }
  } catch (err) {
    showToast(err?.message || String(err), "error");
  }
}

/* ── natural-language query ──────────────────────────────────────── */

function setQueryNote(text, kind) {
  const note = document.getElementById("history-query-note");
  if (!note) return;
  note.textContent = text;
  note.classList.toggle("is-active", kind === "active");
  note.classList.toggle("is-error", kind === "error");
}

async function runHistoryQuery() {
  const input = document.getElementById("history-query");
  const btn = document.getElementById("btn-history-query");
  const text = String(input?.value || "").trim();
  if (!text || queryBusy) return;
  queryBusy = true;
  if (btn) {
    btn.disabled = true;
    btn.textContent = tx("ai_query_running", "Reading…");
  }
  try {
    const symbols = [
      ...new Set(
        (lastTradesPayload?.window_trades || [])
          .map((row) => String(row.symbol || "").toUpperCase())
          .filter(Boolean)
      ),
    ];
    const data = await api("/api/history/query", {
      method: "POST",
      body: JSON.stringify({ text, symbols, lang: reviewLang() }),
    });
    applyQueryFilter(data.filter || {});
  } catch (err) {
    setQueryNote(
      tx("ai_query_failed", "Could not read that: {error}", {
        error: err?.message || String(err),
      }),
      "error"
    );
  } finally {
    queryBusy = false;
    if (btn) {
      btn.disabled = false;
      btn.textContent = tx("ai_query_run", "Filter");
    }
  }
}

function applyQueryFilter(spec) {
  if (spec.unsupported) {
    setQueryNote(
      spec.explain ||
        tx(
          "ai_query_unsupported",
          "That is not something this page can filter for. Try naming a symbol, a side, or a P&L threshold."
        ),
      "error"
    );
    return;
  }
  // Symbol, side and range map onto the real controls so the change is visible
  // and undoable; only outcome and P&L bounds need the extra filter.
  const symbolEl = document.getElementById("history-symbol");
  const sideEl = document.getElementById("history-side");
  if (symbolEl) symbolEl.value = spec.symbol || "";
  if (sideEl) {
    sideEl.value = spec.side || "";
    refreshNiceSelect(sideEl);
  }
  setHistoryQueryFilter(spec);
  const clearBtn = document.getElementById("btn-history-query-clear");
  if (clearBtn) clearBtn.hidden = false;
  setQueryNote(
    spec.explain || tx("ai_query_applied", "Filter applied."),
    "active"
  );

  const rangeChanged = spec.range && spec.range !== historyRange;
  if (rangeChanged) {
    historyRange = spec.range;
    if (historyRange !== "custom") {
      historyStart = "";
      historyEnd = "";
    }
    syncHistoryFiltersUi();
    writeHistoryFiltersToUrl();
    refreshTrades().catch(() => {});
    return;
  }
  syncHistoryFiltersUi();
  applyHistoryView();
}

/* ── wiring ──────────────────────────────────────────────────────── */

if (reviewOnPage()) {
  document.getElementById("btn-review-run")?.addEventListener("click", () => {
    runReviewNarration();
  });

  document.querySelectorAll("[data-review-scope]").forEach((btn) => {
    btn.addEventListener("click", () => setReviewScope(btn.dataset.reviewScope));
  });

  document.getElementById("btn-history-query")?.addEventListener("click", () => {
    runHistoryQuery();
  });
  document.getElementById("history-query")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      runHistoryQuery();
    }
  });
  document
    .getElementById("btn-history-query-clear")
    ?.addEventListener("click", () => {
      clearHistoryQueryFilter();
      applyHistoryView();
    });

  document.addEventListener("click", (ev) => {
    const save = ev.target.closest("[data-save-lesson]");
    if (save) {
      ev.preventDefault();
      saveLesson(Number(save.dataset.saveLesson));
      return;
    }
    const toggle = ev.target.closest("[data-toggle-lesson]");
    if (toggle) {
      ev.preventDefault();
      const id = Number(toggle.dataset.toggleLesson);
      const current = savedLessons.find((l) => Number(l.id) === id);
      api(`/api/history/lessons/${id}`, {
        method: "POST",
        body: JSON.stringify({ enabled: current?.enabled === false }),
      })
        .then((data) => {
          savedLessons = data.lessons || savedLessons;
          renderSavedLessons();
        })
        .catch((err) => showToast(err?.message || String(err), "error"));
      return;
    }
    const remove = ev.target.closest("[data-delete-lesson]");
    if (remove) {
      ev.preventDefault();
      const id = Number(remove.dataset.deleteLesson);
      if (
        !window.confirm(
          tx("review_lesson_delete_confirm", "Remove this lesson from the playbook?")
        )
      ) {
        return;
      }
      api(`/api/history/lessons/${id}`, { method: "DELETE" })
        .then((data) => {
          savedLessons = data.lessons || [];
          renderSavedLessons();
        })
        .catch((err) => showToast(err?.message || String(err), "error"));
    }
  });

  syncReviewPanes();
  loadSavedLessons();
}

/** Re-render everything language-dependent when the desk language changes. */
function onReviewLanguageChange() {
  if (!reviewOnPage()) return;
  renderReviewFacts();
  renderSavedLessons();
  renderNarrative(reviewNarrations.get(reviewScope) || null, { cached: true });
  syncReviewRunButton();
}
