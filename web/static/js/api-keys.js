/**
 * Configuration Page JavaScript for AlgoPaca
 * Alpaca & AI API key storage, Paper/Live mode switch, connection testing.
 */
let configBusy = false;
let configBusyTarget = null;
// The environment the user has picked in the radio group but not applied yet.
// Held separately from the desk's real mode so the 2s status poll cannot
// overwrite a selection mid-typing.
let pendingModeChoice = null;

// Resolved on every render rather than once at load: these labels overwrite the
// `data-i18n` text the translator put on the buttons, so a constant captured in
// English would survive every later language switch.
const savePaperLabel = () => tx("save_paper_keys_btn", "Save paper keys");
const saveLiveLabel = () => tx("save_live_keys_btn", "Save live keys");
const saveAiLabel = () => tx("save_ai_keys_btn", "Save AI keys");
const savingLabel = () => tx("saving", "Saving…");

function keysPayload() {
  const openai = String($("field-openai-key")?.value || "").trim();
  const gemini = String($("field-gemini-key")?.value || "").trim();
  const anthropic = String($("field-anthropic-key")?.value || "").trim();
  const xai = String($("field-xai-key")?.value || "").trim();
  return {
    openai_api_key: openai,
    gemini_api_key: gemini,
    anthropic_api_key: anthropic,
    xai_api_key: xai,
    save_to_env: !!$("field-save-keys")?.checked,
  };
}

function selectedTradingMode() {
  return $("field-mode-live")?.checked ? "live" : "paper";
}

function activeTradingMode(status) {
  const s = status || lastAlpacaStatus || {};
  return s.trading_mode || (s.paper === false ? "live" : "paper");
}

/** Show the credential slot for the selected environment, hide the other. */
function syncCredentialVisibility(status) {
  const selected = selectedTradingMode();
  const paperSet = $("paper-credentials");
  const liveSet = $("live-credentials");
  if (paperSet) paperSet.hidden = selected === "live";
  if (liveSet) liveSet.hidden = selected !== "live";

  // Picking a radio only reveals the matching keys — the desk does not move
  // until Apply, so say which one is on screen versus which one is running.
  const note = $("env-pending-note");
  if (!note) return;
  const active = activeTradingMode(status);
  note.hidden = selected === active;
  if (note.hidden) {
    note.textContent = "";
  } else if (selected === "live") {
    note.textContent = tx(
      "env_pending_live",
      "Showing Live credentials — the desk stays on Paper until you apply."
    );
  } else {
    note.textContent = tx(
      "env_pending_paper",
      "Showing Paper credentials — the desk stays on Live until you apply."
    );
  }
}

function syncTradingModeUi(status) {
  const s = status || lastAlpacaStatus || {};
  const mode = activeTradingMode(s);
  const paperRadio = $("field-mode-paper");
  const liveRadio = $("field-mode-live");
  // A pending choice the desk has caught up with is no longer pending.
  if (pendingModeChoice === mode) pendingModeChoice = null;
  const shown = pendingModeChoice || mode;
  if (paperRadio) paperRadio.checked = shown !== "live";
  if (liveRadio) liveRadio.checked = shown === "live";
  syncCredentialVisibility(s);

  const statusEl = $("trading-mode-status");
  if (statusEl) {
    statusEl.textContent =
      mode === "live"
        ? tx("mode_status_live", "Active: Live")
        : tx("mode_status_paper", "Active: Paper");
  }

  const hint = $("alpaca-config-hint");
  if (hint && !configBusy) {
    hint.textContent =
      mode === "live"
        ? tx("trading_env_live", "Live environment")
        : tx("trading_env_paper", "Paper environment");
    hint.dataset.state = mode === "live" ? "live" : "ready";
  }
}

function syncConfigConnection() {
  const alpacaEl = $("config-conn-alpaca");
  const aiEl = $("config-conn-ai");
  const statusHint = $("config-status-hint");
  const s = lastAlpacaStatus || {};
  const keys = lastKeyStatus || {};
  const account = lastAccount;
  const accountError = s.account_error;
  const mode = s.trading_mode || (s.paper === false ? "live" : "paper");
  const modeLabel =
    mode === "live" ? tx("live_status", "Live") : tx("paper_status", "Paper");

  if (alpacaEl) {
    if (accountError) {
      alpacaEl.textContent = `${tx("status_failed", "Failed")} — ${accountError}`;
      alpacaEl.dataset.state = "invalid";
    } else if (s.set && account) {
      const equity =
        account.equity != null
          ? ` · ${tx("equity", "Equity")} ${money(account.equity)}`
          : "";
      alpacaEl.textContent = `${tx("connected", "Connected")} · ${modeLabel}${equity}`;
      alpacaEl.dataset.state = mode === "live" ? "live" : "ok";
    } else if (s.set) {
      alpacaEl.textContent = tx(
        "keys_saved_verify",
        "Keys saved · {mode} — refresh account to verify",
        { mode: modeLabel }
      );
      alpacaEl.dataset.state = "ok";
    } else if (s.api_key_set || s.secret_set) {
      alpacaEl.textContent = tx(
        "incomplete_pair",
        "Incomplete pair — need both key and secret"
      );
      alpacaEl.dataset.state = "invalid";
    } else {
      alpacaEl.textContent = tx(
        "not_connected_keys",
        "Not connected — paste paper and/or live keys below"
      );
      alpacaEl.dataset.state = "muted";
    }
  }

  if (aiEl) {
    const o = keys.openai || { set: false, source: "none" };
    const g = keys.gemini || { set: false, source: "none" };
    const a = keys.anthropic || { set: false, source: "none" };
    const x = keys.xai || { set: false, source: "none" };
    const fmt = (entry, name) => {
      if (!entry.set) return `${name} ✗`;
      const src = entry.source === "ui" ? "UI" : entry.source === "env" ? ".env" : "";
      return `${name} ✓${src ? ` (${src})` : ""}`;
    };
    aiEl.textContent = `${fmt(o, "OpenAI")} · ${fmt(g, "Gemini")} · ${fmt(a, "Anthropic")} · ${fmt(x, "xAI")}`;
    aiEl.dataset.state = o.set || g.set || a.set || x.set ? "ok" : "muted";
  }

  if (statusHint && !configBusy) {
    if (accountError || ((s.api_key_set || s.secret_set) && !s.set)) {
      statusHint.dataset.state = "invalid";
      statusHint.textContent = tx("needs_attention", "Needs attention");
    } else if (s.set) {
      statusHint.dataset.state = mode === "live" ? "live" : "ready";
      statusHint.textContent =
        mode === "live" ? tx("live_status", "Live") : tx("ready", "Ready");
    } else {
      statusHint.dataset.state = "ready";
      statusHint.textContent = tx("status_label", "Status");
    }
  }

  const paperKeys = s.paper_keys || {};
  const liveKeys = s.live_keys || {};
  const clearAlpaca = $("btn-clear-alpaca");
  if (clearAlpaca) {
    clearAlpaca.disabled =
      configBusy ||
      busy ||
      !(
        paperKeys.api_key_set ||
        paperKeys.secret_set ||
        liveKeys.api_key_set ||
        liveKeys.secret_set ||
        s.api_key_set ||
        s.secret_set
      );
  }
  const clearOpenAI = $("btn-clear-openai");
  if (clearOpenAI) {
    clearOpenAI.disabled = configBusy || busy || !keys.openai?.set;
  }
  const clearGemini = $("btn-clear-gemini");
  if (clearGemini) {
    clearGemini.disabled = configBusy || busy || !keys.gemini?.set;
  }
  const clearAnthropic = $("btn-clear-anthropic");
  if (clearAnthropic) {
    clearAnthropic.disabled = configBusy || busy || !keys.anthropic?.set;
  }
  const clearXai = $("btn-clear-xai");
  if (clearXai) {
    clearXai.disabled = configBusy || busy || !keys.xai?.set;
  }

  syncTradingModeUi(s);
}

function setConfigPanelHint(target, state, label) {
  const el =
    target === "ai" ? $("ai-config-hint") : $("alpaca-config-hint");
  if (!el) return;
  el.dataset.state = state;
  if (label != null) el.textContent = label;
}

function syncConfigBusyUi() {
  const saving = configBusy || busy;
  const alpacaForm = $("alpaca-config");
  const aiForm = $("ai-config");
  const saveAlpaca = $("btn-save-alpaca");
  const saveLive = $("btn-save-live");
  const saveAi = $("btn-save-keys");
  const applyMode = $("btn-apply-mode");

  if (alpacaForm) {
    [...alpacaForm.elements].forEach((el) => {
      if (el.id === "btn-clear-alpaca") return;
      el.disabled = saving;
    });
  }
  if (aiForm) {
    [...aiForm.elements].forEach((el) => {
      if (
        el.id === "btn-clear-openai" ||
        el.id === "btn-clear-gemini" ||
        el.id === "btn-clear-anthropic" ||
        el.id === "btn-clear-xai"
      )
        return;
      el.disabled = saving;
    });
  }

  if (saveAlpaca) {
    saveAlpaca.disabled = saving;
    saveAlpaca.textContent =
      configBusy && configBusyTarget === "alpaca"
        ? savingLabel()
        : savePaperLabel();
  }
  if (saveLive) {
    saveLive.disabled = saving;
    saveLive.textContent =
      configBusy && configBusyTarget === "live" ? savingLabel() : saveLiveLabel();
  }
  if (applyMode) applyMode.disabled = saving;
  if (saveAi) {
    saveAi.disabled = saving;
    saveAi.textContent =
      configBusy && configBusyTarget === "ai" ? savingLabel() : saveAiLabel();
  }

  // Paper and live credentials share the one Alpaca panel, so both targets
  // light up the same hint.
  if (configBusy && (configBusyTarget === "alpaca" || configBusyTarget === "live")) {
    setConfigPanelHint("alpaca", "saving", savingLabel());
  } else if (configBusy && configBusyTarget === "ai") {
    setConfigPanelHint("ai", "saving", savingLabel());
  }

  syncConfigConnection();
}

function setConfigBusy(isBusy, target = null) {
  configBusy = !!isBusy;
  configBusyTarget = isBusy ? target : null;
  syncConfigBusyUi();
}

async function onSaveKeys(ev) {
  ev?.preventDefault?.();
  const payload = keysPayload();
  const errEl = $("ai-config-error");
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }
  if (
    !payload.openai_api_key &&
    !payload.gemini_api_key &&
    !payload.anthropic_api_key &&
    !payload.xai_api_key
  ) {
    const msg = "Paste at least one AI API key first.";
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = msg;
    }
    setConfigPanelHint("ai", "invalid", tx("status_invalid", "Invalid"));
    showToast(msg, "error");
    return;
  }
  try {
    setBusy(true, "Saving AI keys…");
    setConfigBusy(true, "ai");
    const data = await api("/api/keys", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const openaiEl = $("field-openai-key");
    const geminiEl = $("field-gemini-key");
    const anthropicEl = $("field-anthropic-key");
    const xaiEl = $("field-xai-key");
    if (openaiEl) openaiEl.value = "";
    if (geminiEl) geminiEl.value = "";
    if (anthropicEl) anthropicEl.value = "";
    if (xaiEl) xaiEl.value = "";
    applyAiKeys(data.state?.ai_ready, data.ai_key_status || data.state?.ai_key_status);
    await refreshStatus({ forceSettings: false });
    const saved = [
      payload.openai_api_key ? "OpenAI" : null,
      payload.gemini_api_key ? "Gemini" : null,
      payload.anthropic_api_key ? "Anthropic" : null,
      payload.xai_api_key ? "xAI" : null,
    ]
      .filter(Boolean)
      .join(" + ");
    setConfigPanelHint("ai", "saved", tx("status_saved", "Saved"));
    showToast(
      payload.save_to_env
        ? `${saved} key saved to .env.`
        : `${saved} key saved for this session.`,
      "ok"
    );
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = err.message;
    }
    setConfigPanelHint("ai", "invalid", tx("status_failed", "Failed"));
    showToast(err.message, "error");
  } finally {
    setConfigBusy(false);
    setBusy(false);
  }
}

async function saveAlpacaSlot(environment) {
  const errEl = $("alpaca-config-error");
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }
  const isLive = environment === "live";
  const apiKey = String(
    $(isLive ? "field-live-key" : "field-alpaca-key")?.value || ""
  ).trim();
  const secret = String(
    $(isLive ? "field-live-secret" : "field-alpaca-secret")?.value || ""
  ).trim();
  const current = lastAlpacaStatus || {};
  const slot = (isLive ? current.live_keys : current.paper_keys) || current;
  const willHaveKey = !!apiKey || !!slot.api_key_set;
  const willHaveSecret = !!secret || !!slot.secret_set;
  const label = isLive ? "live" : "paper";

  if (!apiKey && !secret) {
    const msg = slot.set
      ? `Paste a new ${label} API key and/or secret to update.`
      : `Paste Alpaca ${label} API key and secret to save.`;
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = msg;
    }
    setConfigPanelHint("alpaca", "invalid", tx("status_invalid", "Invalid"));
    showToast(msg, "error");
    return;
  }
  if (!willHaveKey || !willHaveSecret) {
    const missing = [
      !willHaveKey ? "API key" : null,
      !willHaveSecret ? "secret key" : null,
    ]
      .filter(Boolean)
      .join(" and ");
    const msg = `Paste both Alpaca ${label} API key and secret to connect (${missing} missing).`;
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = msg;
    }
    setConfigPanelHint("alpaca", "invalid", tx("status_invalid", "Invalid"));
    showToast(msg, "error");
    return;
  }
  try {
    setBusy(true, `Saving ${label} keys…`);
    setConfigBusy(true, isLive ? "live" : "alpaca");
    const data = await api("/api/alpaca-keys", {
      method: "POST",
      body: JSON.stringify({
        alpaca_api_key: apiKey,
        alpaca_secret_key: secret,
        environment,
        save_to_env: true,
      }),
    });
    const status = data.alpaca_key_status || data.state?.alpaca_key_status || {};
    const keyEl = $(isLive ? "field-live-key" : "field-alpaca-key");
    const secretEl = $(isLive ? "field-live-secret" : "field-alpaca-secret");
    if (keyEl) keyEl.value = "";
    if (secretEl) secretEl.value = "";
    applyAlpacaKeys(status);
    if (data.state) applyDeskState(data.state);
    else await refreshStatus({ forceSettings: false });

    const savedSlot = (isLive ? status.live_keys : status.paper_keys) || status;
    const connected = !!savedSlot.set && !status.account_error;
    if (!connected && status.account_error) {
      const msg = status.account_error;
      if (errEl) {
        errEl.hidden = false;
        errEl.textContent = msg;
      }
      setConfigPanelHint("alpaca", "invalid", tx("status_failed", "Failed"));
      showToast(msg, "error");
      return;
    }
    if (!savedSlot.set) {
      const msg = "Keys incomplete — both API key and secret are required.";
      if (errEl) {
        errEl.hidden = false;
        errEl.textContent = msg;
      }
      setConfigPanelHint("alpaca", "invalid", tx("status_failed", "Failed"));
      showToast(msg, "error");
      return;
    }

    const equity =
      status.account?.equity != null
        ? ` Equity ${money(status.account.equity)}.`
        : "";
    setConfigPanelHint("alpaca", "saved", tx("status_saved", "Saved"));
    showToast(
      isLive
        ? `Live keys saved.${equity || " Switch environment to use them."}`
        : `Paper keys saved.${equity}`,
      "ok"
    );
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = err.message;
    }
    setConfigPanelHint("alpaca", "invalid", tx("status_failed", "Failed"));
    showToast(err.message, "error");
  } finally {
    setConfigBusy(false);
    setBusy(false);
  }
}

async function onSaveAlpacaKeys(ev) {
  ev?.preventDefault?.();
  await saveAlpacaSlot("paper");
}

async function onSaveLiveKeys(ev) {
  ev?.preventDefault?.();
  await saveAlpacaSlot("live");
}

async function applyTradingMode(mode) {
  const errEl = $("alpaca-config-error");
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }
  try {
    setBusy(true, mode === "live" ? "Switching to Live…" : "Switching to Paper…");
    setConfigBusy(true, "alpaca");
    const data = await api("/api/trading-mode", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    if (data.state) applyDeskState(data.state);
    else {
      applyAlpacaKeys(data.alpaca_key_status);
      await refreshStatus({ forceSettings: true });
    }
    setConfigPanelHint(
      "alpaca",
      mode === "live" ? "live" : "saved",
      mode === "live"
        ? tx("live_status", "Live")
        : tx("paper_status", "Paper")
    );
    showToast(
      mode === "live"
        ? tx(
            "switched_to_live",
            "Live environment active."
          )
        : tx("switched_to_paper", "Paper environment active."),
      "ok"
    );
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = err.message;
    }
    setConfigPanelHint("alpaca", "invalid", tx("status_failed", "Failed"));
    showToast(err.message, "error");
    pendingModeChoice = null;
    syncTradingModeUi(lastAlpacaStatus);
  } finally {
    setConfigBusy(false);
    setBusy(false);
  }
}

async function onApplyTradingMode() {
  const mode = selectedTradingMode();
  const current = lastAlpacaStatus || {};
  const currentMode = current.trading_mode || (current.paper === false ? "live" : "paper");
  if (mode === currentMode) {
    showToast(
      mode === "live"
        ? tx("already_live", "Already on Live.")
        : tx("already_paper", "Already on Paper."),
      "ok"
    );
    return;
  }
  const ok = window.confirm(
    mode === "live"
      ? tx(
          "switch_to_live_confirm",
          "Switch to Live trading? Any running loop will stop."
        )
      : tx(
          "switch_to_paper_confirm",
          "Switch back to Paper trading? Any running loop will stop."
        )
  );
  if (!ok) {
    pendingModeChoice = null;
    syncTradingModeUi(current);
    return;
  }
  await applyTradingMode(mode);
}

async function onClearAlpacaKeys() {
  const current = lastAlpacaStatus || {};
  const paperKeys = current.paper_keys || {};
  const liveKeys = current.live_keys || {};
  if (
    !(
      paperKeys.api_key_set ||
      paperKeys.secret_set ||
      liveKeys.api_key_set ||
      liveKeys.secret_set ||
      current.api_key_set ||
      current.secret_set
    )
  ) {
    showToast("No Alpaca keys to clear.", "error");
    return;
  }
  const ok = window.confirm(
    tx(
      "clear_alpaca_confirm",
      "Clear saved paper and live Alpaca keys from .env? You will need to paste them again to reconnect."
    )
  );
  if (!ok) return;
  const errEl = $("alpaca-config-error");
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }
  try {
    setBusy(true, "Clearing Alpaca keys…");
    setConfigBusy(true, "alpaca");
    const data = await api("/api/alpaca-keys/clear", {
      method: "POST",
      body: JSON.stringify({ environment: "all" }),
    });
    applyAlpacaKeys(data.alpaca_key_status || data.state?.alpaca_key_status);
    lastAccount = null;
    applyAccount(null);
    if (data.state) applyDeskState(data.state);
    setConfigPanelHint("alpaca", "ready", tx("status_cleared", "Cleared"));
    showToast("Alpaca keys cleared.", "ok");
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = err.message;
    }
    setConfigPanelHint("alpaca", "invalid", tx("status_failed", "Failed"));
    showToast(err.message, "error");
  } finally {
    setConfigBusy(false);
    setBusy(false);
  }
}

const AI_PROVIDER_LABELS = {
  openai: "OpenAI",
  gemini: "Gemini",
  anthropic: "Anthropic",
  xai: "xAI",
};

async function onClearAiKey(provider) {
  const status = lastKeyStatus || {};
  const entry = status[provider];
  const label = AI_PROVIDER_LABELS[provider] || provider;
  if (!entry?.set) {
    showToast(`No ${label} key to clear.`, "error");
    return;
  }
  const ok = window.confirm(
    `Clear saved ${label} API key from this session and .env?`
  );
  if (!ok) return;
  const errEl = $("ai-config-error");
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }
  try {
    setBusy(true, `Clearing ${label} key…`);
    setConfigBusy(true, "ai");
    const data = await api("/api/keys/clear", {
      method: "POST",
      body: JSON.stringify({
        openai: provider === "openai",
        gemini: provider === "gemini",
        anthropic: provider === "anthropic",
        xai: provider === "xai",
        clear_env: true,
      }),
    });
    applyAiKeys(data.state?.ai_ready, data.ai_key_status || data.state?.ai_key_status);
    if (data.state) applyDeskState(data.state);
    setConfigPanelHint("ai", "ready", tx("status_cleared", "Cleared"));
    showToast(`${label} key cleared.`, "ok");
  } catch (err) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = err.message;
    }
    setConfigPanelHint("ai", "invalid", tx("status_failed", "Failed"));
    showToast(err.message, "error");
  } finally {
    setConfigBusy(false);
    setBusy(false);
  }
}

/** Apply a fresh status payload after a save/clear without waiting for the poll. */
function applyDeskState(state) {
  if (!state || typeof state !== "object") return;
  if (state.account) applyAccount(state.account);
  applyAiKeys(state.ai_ready, state.ai_key_status);
  applyAlpacaKeys(state.alpaca_key_status);
  applyTradingEnv(state.trading_mode || state.alpaca_key_status);
  syncConfigConnection();
}

$("ai-config")?.addEventListener("submit", onSaveKeys);
$("btn-save-keys")?.addEventListener("click", onSaveKeys);
$("alpaca-config")?.addEventListener("submit", onSaveAlpacaKeys);
$("btn-save-live")?.addEventListener("click", onSaveLiveKeys);
// Both credential slots live in one <form>, whose only submit button is the
// paper one — so Enter inside a live field would fire an empty paper save.
// Route it to the slot the caret is actually in.
["field-live-key", "field-live-secret"].forEach((id) => {
  $(id)?.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    onSaveLiveKeys(ev);
  });
});
$("btn-apply-mode")?.addEventListener("click", onApplyTradingMode);

// Selecting an environment only swaps which credential slot is on screen.
["field-mode-paper", "field-mode-live"].forEach((id) => {
  $(id)?.addEventListener("change", () => {
    pendingModeChoice = selectedTradingMode();
    syncTradingModeUi(lastAlpacaStatus);
  });
});
$("btn-clear-alpaca")?.addEventListener("click", onClearAlpacaKeys);
$("btn-clear-openai")?.addEventListener("click", () => onClearAiKey("openai"));
$("btn-clear-gemini")?.addEventListener("click", () => onClearAiKey("gemini"));
$("btn-clear-anthropic")?.addEventListener("click", () => onClearAiKey("anthropic"));
$("btn-clear-xai")?.addEventListener("click", () => onClearAiKey("xai"));

// Initialization
refreshStatus({ forceSettings: true }).catch((err) => showToast(err.message, "error"));

// The save buttons and panel hints are rewritten from JS, so `translateDOM()`
// alone leaves whichever label was rendered last in the old language.
function onDeskLanguageChange() {
  syncConfigBusyUi();
}

function onDeskStatusUpdate(state) {
  if (state.alpaca_key_status) {
    applyAlpacaKeys(state.alpaca_key_status);
  }
  if (state.ai_key_status) {
    applyAiKeys(state.ai_ready, state.ai_key_status);
  }
  applyTradingEnv(state.trading_mode || state.alpaca_key_status);
  syncConfigConnection();
}
