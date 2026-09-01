/**
 * User Settings & Preferences Controller for AlgoPaca
 * Handles profile updates, password changes, active session management,
 * theme & localization switching, audio synthesizer alerts, and data export.
 */

(function () {
  "use strict";

  // State
  let currentProfile = null;
  let currentPreferences = null;
  let activeSessions = [];
  let currentTab = "profile";

  // DOM - Tabs
  const tabButtons = document.querySelectorAll(".settings-tab-btn");
  const tabPanels = document.querySelectorAll(".settings-panel");

  // DOM - Profile Elements
  const heroPanel = $("profile-hero-panel");
  const heroError = $("profile-hero-error");
  const btnRetryProfile = $("btn-retry-profile");
  const heroAvatarImg = $("profile-avatar-img");
  const heroRolePill = $("profile-role-pill");
  const heroDisplayName = $("profile-display-name-hero");
  const heroUsernameTag = $("profile-username-tag");
  const heroIdTag = $("profile-id-tag");
  const heroStatusPill = $("profile-status-pill");
  const heroMemberSince = $("profile-member-since");
  const heroLastLogin = $("profile-last-login");
  const heroTradingMode = $("profile-trading-mode");
  const heroActiveSessionsCount = $("profile-active-sessions-count");
  const headerRoleTagText = $("header-user-role-text");

  const formProfile = $("form-user-profile");
  const inputProfileName = $("input-profile-name");
  const inputProfileEmail = $("input-profile-email");
  const inputProfileUsername = $("input-profile-username");
  const inputProfileRole = $("input-profile-role");
  const btnSaveProfile = $("btn-save-profile");
  const btnResetProfile = $("btn-reset-profile");
  const profileUnsavedFlag = $("profile-unsaved-flag");
  const profileNameCounter = $("profile-name-counter");
  const profileNameError = $("profile-name-error");
  const profileEmailError = $("profile-email-error");
  const profileEmailNotice = $("profile-email-notice");

  // DOM - Password Elements
  const formChangePassword = $("form-change-password");
  const inputPwCurrent = $("pw-current");
  const inputPwNew = $("pw-new");
  const inputPwConfirm = $("pw-confirm");
  const pwStrengthFill = $("pw-strength-fill");
  const pwStrengthLabel = $("pw-strength-label");
  const pwMatchHint = $("pw-match-hint");
  const btnChangePw = $("btn-change-pw");

  // DOM - Sessions Elements
  const sessionsContainer = $("sessions-container");
  const btnTerminateOtherSessions = $("btn-terminate-other-sessions");

  // DOM - Appearance Elements
  const themeCards = document.querySelectorAll(".theme-card");
  const selectSettingsLang = $("select-settings-lang");
  const selectSettingsTimezone = $("select-settings-timezone");
  const selectSettingsDefaultPage = $("select-settings-default-page");
  const selectSettingsRefresh = $("select-settings-refresh");
  const checkSoundAlerts = $("check-sound-alerts");
  const checkCompactMode = $("check-compact-mode");
  const btnTestSound = $("btn-test-sound");
  const btnSaveAppearance = $("btn-save-appearance");

  // DOM - Trading Defaults Elements
  const formTradingDefaults = $("form-trading-defaults");
  const selectDefaultSizeMode = $("select-default-size-mode");
  const inputDefaultTradeQty = $("input-default-trade-qty");
  const inputDefaultTradeNotional = $("input-default-trade-notional");
  const checkConfirmOrders = $("check-confirm-orders");
  const checkConfirmCloseAll = $("check-confirm-close-all");
  const checkSettingsRequireApproval = $("check-settings-require-approval");
  const checkSettingsNotifyBrowser = $("check-settings-notify-browser");
  const checkSettingsNotifyEmail = $("check-settings-notify-email");
  const wrapSettingsNotificationEmail = $("wrap-settings-notification-email");
  const inputSettingsNotificationEmail = $("input-settings-notification-email");
  const tradeQtyError = $("settings-trade-qty-error");
  const tradeNotionalError = $("settings-trade-notional-error");
  const notificationEmailError = $("settings-notification-email-error");

  // DOM - Integrations & Data
  const badgeStatusPaper = $("badge-status-paper");
  const badgeStatusLive = $("badge-status-live");
  const badgeStatusOpenai = $("badge-status-openai");
  const badgeStatusGemini = $("badge-status-gemini");
  const badgeStatusAnthropic = $("badge-status-anthropic");
  const badgeStatusXai = $("badge-status-xai");
  const btnExportData = $("btn-export-data");

  // DOM - Modal Delete
  const modalDeleteAccount = $("modal-delete-account");
  const btnOpenDeleteModal = $("btn-open-delete-modal");
  const btnCloseDeleteModal = $("btn-close-delete-modal");
  const btnCancelDelete = $("btn-cancel-delete");
  const btnConfirmDelete = $("btn-confirm-delete");
  const inputDeletePassword = $("input-delete-password");

  /* ------------------------------------------------------------------------ */
  /* Audio Synthesizer (Web Audio API)                                        */
  /* ------------------------------------------------------------------------ */

  function playDeskChime(type = "success") {
    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return;
      const ctx = new AudioContext();
      if (ctx.state === "suspended") {
        ctx.resume();
      }

      const now = ctx.currentTime;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();

      osc.connect(gain);
      gain.connect(ctx.destination);

      if (type === "success") {
        // Crisp dual-tone ascending chime
        osc.type = "sine";
        osc.frequency.setValueAtTime(523.25, now); // C5
        osc.frequency.setValueAtTime(659.25, now + 0.08); // E5
        osc.frequency.setValueAtTime(783.99, now + 0.16); // G5
        gain.gain.setValueAtTime(0.15, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.45);
        osc.start(now);
        osc.stop(now + 0.46);
      } else if (type === "alert") {
        // Warning buzz tone
        osc.type = "triangle";
        osc.frequency.setValueAtTime(320, now);
        osc.frequency.setValueAtTime(280, now + 0.1);
        gain.gain.setValueAtTime(0.2, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.3);
        osc.start(now);
        osc.stop(now + 0.31);
      }
    } catch (e) {
      console.warn("Audio chime playback not supported:", e);
    }
  }

  /* ------------------------------------------------------------------------ */
  /* Tab Switching & Deep-linking                                             */
  /* ------------------------------------------------------------------------ */

  const VALID_SETTINGS_TABS = ["profile", "security", "appearance", "trading", "data"];
  const STORAGE_KEY_SETTINGS_TAB = "algopaca_settings_active_tab";

  function switchTab(tabName, opts = {}) {
    if (!VALID_SETTINGS_TABS.includes(tabName)) {
      tabName = "profile";
    }
    if (tabName === currentTab && opts.focusPanel !== true) {
      // Still run through once on first paint so panels/ARIA are consistent.
      if (opts.initial !== true) return;
    }

    // Warn before navigating away from an edited profile form.
    if (currentTab === "profile" && tabName !== "profile" && isProfileDirty()) {
      if (!confirm(tr("settings_leave_unsaved_confirm", "You have unsaved profile changes. Discard them?"))) {
        // A hashchange may already have moved the URL ahead of the visible tab.
        if (history.replaceState) history.replaceState(null, "", `#${currentTab}`);
        return;
      }
      resetProfileForm();
    }

    currentTab = tabName;

    try {
      localStorage.setItem(STORAGE_KEY_SETTINGS_TAB, tabName);
    } catch (_) {}

    tabButtons.forEach((btn) => {
      const isTarget = btn.dataset.tab === tabName;
      btn.classList.toggle("is-active", isTarget);
      btn.setAttribute("aria-selected", isTarget ? "true" : "false");
      // Roving tabindex: only the selected tab is in the tab order, arrows move between them.
      btn.tabIndex = isTarget ? 0 : -1;
    });

    tabPanels.forEach((panel) => {
      const isTarget = panel.id === `panel-${tabName}`;
      panel.classList.toggle("is-active", isTarget);
      panel.hidden = !isTarget;
    });

    if (history.replaceState) {
      history.replaceState(null, "", `#${tabName}`);
    }

    if (opts.focusPanel) {
      $(`panel-${tabName}`)?.focus();
    }

    if (tabName === "security") {
      fetchSessions();
    }
  }

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetTab = btn.dataset.tab;
      if (targetTab) switchTab(targetTab);
    });
  });

  // WAI-ARIA tablist keyboard support: arrows, Home and End move between tabs.
  const tabList = document.querySelector(".settings-tabs-nav");
  tabList?.addEventListener("keydown", (e) => {
    const keys = ["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp", "Home", "End"];
    if (!keys.includes(e.key)) return;

    const buttons = Array.from(tabButtons);
    const currentIndex = buttons.findIndex((b) => b.dataset.tab === currentTab);
    if (currentIndex < 0) return;

    let nextIndex = currentIndex;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") nextIndex = (currentIndex + 1) % buttons.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") nextIndex = (currentIndex - 1 + buttons.length) % buttons.length;
    else if (e.key === "Home") nextIndex = 0;
    else if (e.key === "End") nextIndex = buttons.length - 1;

    e.preventDefault();
    const nextTab = buttons[nextIndex]?.dataset.tab;
    if (!nextTab) return;
    switchTab(nextTab);
    // switchTab may be cancelled by the unsaved-changes guard; only move focus if it took.
    if (currentTab === nextTab) buttons[nextIndex].focus();
  });

  function checkHashTab(initial = false) {
    const hash = window.location.hash.replace("#", "").trim();
    if (VALID_SETTINGS_TABS.includes(hash)) {
      switchTab(hash, { initial });
      return;
    }
    try {
      const savedTab = localStorage.getItem(STORAGE_KEY_SETTINGS_TAB);
      if (savedTab && VALID_SETTINGS_TABS.includes(savedTab)) {
        switchTab(savedTab, { initial });
        return;
      }
    } catch (_) {}
    switchTab("profile", { initial });
  }

  /* ------------------------------------------------------------------------ */
  /* Profile Loading & Saving                                                 */
  /* ------------------------------------------------------------------------ */

  const EM_DASH = "—";

  /** Translate with an English fallback, tolerating i18n.js not being ready yet. */
  function tr(key, fallback, params) {
    if (typeof window.t === "function") return window.t(key, fallback, params || {});
    return fallback;
  }

  /**
   * Pull a readable message out of an error response body.
   * FastAPI returns a plain string `detail` for HTTPException but a list of
   * objects for 422 validation errors, which stringified to "[object Object]".
   */
  function extractApiError(data, fallback) {
    const detail = data?.detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
      const parts = detail
        .map((d) => (typeof d === "string" ? d : d?.msg))
        .filter(Boolean);
      if (parts.length) return parts.join(" · ");
    }
    return fallback;
  }

  function formatDateTime(isoString) {
    if (!isoString) return EM_DASH;
    try {
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return isoString;
      return d.toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (e) {
      return isoString;
    }
  }

  async function fetchProfile() {
    heroPanel?.classList.add("is-loading");
    if (heroError) heroError.hidden = true;
    try {
      const res = await fetch("/api/user/profile");
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok || !data.profile) {
        throw new Error(extractApiError(data, tr("settings_profile_load_failed", "Could not load your profile.")));
      }
      currentProfile = data.profile;
      renderProfile(data.profile);
      heroPanel?.classList.remove("is-loading");
    } catch (err) {
      // Leave the skeleton up rather than showing invented placeholder identity.
      if (heroError) heroError.hidden = false;
      showToast(err.message || tr("settings_profile_load_failed", "Could not load your profile."), "error");
    }
  }

  btnRetryProfile?.addEventListener("click", fetchProfile);

  function renderProfile(user) {
    const name = user.display_name || user.username || "Trader";
    const username = user.username || "user";
    const role = (user.role || "trader").toLowerCase();
    const status = (user.status || "active").toLowerCase();

    updateHeroIdentity(name);
    if (heroUsernameTag) {
      heroUsernameTag.textContent = `@${username}`;
      heroUsernameTag.dataset.copyValue = username;
    }
    if (heroIdTag) {
      heroIdTag.textContent = `ID #${user.id}`;
      heroIdTag.dataset.copyValue = String(user.id);
    }
    if (heroMemberSince) heroMemberSince.textContent = formatDateTime(user.created_at);
    if (heroLastLogin) heroLastLogin.textContent = formatDateTime(user.last_login_at);

    // Trading mode: surfaced here because it changes what every order on the desk does.
    if (heroTradingMode) {
      const isLive = String(user.trading_mode || "paper").toLowerCase() === "live";
      heroTradingMode.textContent = isLive ? tr("live", "Live") : tr("paper", "Paper");
      heroTradingMode.className = `date-val ${isLive ? "profile-mode-live" : "profile-mode-paper"}`;
    }

    if (heroActiveSessionsCount) {
      const count = Number(user.active_sessions);
      heroActiveSessionsCount.textContent = Number.isFinite(count) ? String(count) : EM_DASH;
    }

    if (heroStatusPill) {
      heroStatusPill.hidden = false;
      heroStatusPill.textContent = tr(`settings_status_${status}`, status.toUpperCase());
      heroStatusPill.className = `profile-status-pill is-${status}`;
    }

    if (heroRolePill) {
      heroRolePill.textContent = role.toUpperCase();
      heroRolePill.className = `profile-role-pill is-${role}`;
    }
    if (headerRoleTagText) {
      headerRoleTagText.textContent = `${role.toUpperCase()} ${tr("settings_header_tag_suffix", "SETTINGS")}`;
    }

    if (inputProfileName) inputProfileName.value = name;
    if (inputProfileEmail) inputProfileEmail.value = user.email || "";
    if (inputProfileUsername) inputProfileUsername.value = username;
    if (inputProfileRole) inputProfileRole.value = tr(`settings_role_${role}`, role.toUpperCase());

    profileBaseline = { display_name: name, email: user.email || "" };
    clearFieldError(inputProfileName, profileNameError);
    clearFieldError(inputProfileEmail, profileEmailError);
    updateNameCounter();
    updateProfileDirtyState();

    // Integration badges
    const activeProvider = (typeof lastDeskSettings !== "undefined" && lastDeskSettings?.ai_provider) || "openai";
    const getAiBadgeText = (keyPresent, providerName) => {
      const isAct = providerName === activeProvider;
      if (keyPresent) return isAct ? "Active · Connected" : "Connected";
      return isAct ? "Active · Not Set" : "Not Set";
    };
    updateIntegrationBadge(badgeStatusPaper, user.has_paper_key ? "connected" : "missing", user.has_paper_key ? "Configured" : "Not Set");
    updateIntegrationBadge(badgeStatusLive, user.has_live_key ? (user.live_authorized ? "authorized" : "connected") : "missing", user.has_live_key ? (user.live_authorized ? "Authorized Live" : "Keys Set") : "Not Set");
    updateIntegrationBadge(badgeStatusOpenai, user.has_openai_key ? "connected" : (activeProvider === "openai" ? "pending" : "missing"), getAiBadgeText(user.has_openai_key, "openai"));
    updateIntegrationBadge(badgeStatusGemini, user.has_gemini_key ? "connected" : (activeProvider === "gemini" ? "pending" : "missing"), getAiBadgeText(user.has_gemini_key, "gemini"));
    updateIntegrationBadge(badgeStatusAnthropic, user.has_anthropic_key ? "connected" : (activeProvider === "anthropic" ? "pending" : "missing"), getAiBadgeText(user.has_anthropic_key, "anthropic"));
    updateIntegrationBadge(badgeStatusXai, user.has_xai_key ? "connected" : (activeProvider === "xai" ? "pending" : "missing"), getAiBadgeText(user.has_xai_key, "xai"));
  }

  /** Build a ui-avatars.com profile image URL for the given display name. */
  function buildUiAvatarUrl(name) {
    const params = new URLSearchParams({
      name: name || "Trader",
      background: "d4894c",
      color: "120b05",
      size: "152",
      bold: "true",
      rounded: "true",
      format: "png",
    });
    return `https://ui-avatars.com/api/?${params.toString()}`;
  }

  /** Hero name + ui-avatars.com photo, also driven live while the user types. */
  function updateHeroIdentity(name) {
    const shown = (name || "").trim() || currentProfile?.username || "Trader";
    if (heroDisplayName) heroDisplayName.textContent = shown;
    if (heroAvatarImg) {
      const nextSrc = buildUiAvatarUrl(shown);
      if (heroAvatarImg.getAttribute("src") !== nextSrc) {
        heroAvatarImg.src = nextSrc;
      }
      heroAvatarImg.alt = shown;
    }
  }

  function updateIntegrationBadge(el, status, text) {
    if (!el) return;
    el.setAttribute("data-status", status);
    el.textContent = text;
  }

  /* ---------------------------- Copy-to-clipboard --------------------------- */

  [heroUsernameTag, heroIdTag].forEach((chip) => {
    chip?.addEventListener("click", async () => {
      const value = chip.dataset.copyValue;
      if (!value) return;
      try {
        await navigator.clipboard.writeText(value);
        chip.classList.add("is-copied");
        showToast(tr("settings_copied_to_clipboard", "Copied to clipboard."), "success");
        setTimeout(() => chip.classList.remove("is-copied"), 1400);
      } catch (_) {
        showToast(tr("settings_copy_failed", "Could not copy to clipboard."), "error");
      }
    });
  });

  /* ------------------------- Validation & Dirty State ----------------------- */

  // Kept deliberately permissive; the server is the authority on email validity.
  const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
  let profileBaseline = { display_name: "", email: "" };

  function setButtonBusy(btn, isBusy, loadingText = null) {
    if (!btn) return;
    if (isBusy) {
      const width = btn.getBoundingClientRect().width;
      if (width > 0) {
        btn.style.minWidth = `${width}px`;
      }
      btn.classList.add("is-loading");
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      if (loadingText) {
        btn._origHtml = btn.innerHTML;
        btn.innerHTML = `<div class="spinner spinner-xs"></div><span>${escapeHtml(loadingText)}</span>`;
      }
    } else {
      btn.classList.remove("is-loading");
      btn.removeAttribute("aria-busy");
      btn.disabled = false;
      if (btn._origHtml) {
        btn.innerHTML = btn._origHtml;
        delete btn._origHtml;
      }
      btn.style.minWidth = "";
    }
  }

  function setFieldError(input, errorEl, message) {
    if (input) {
      input.classList.add("is-invalid");
      input.setAttribute("aria-invalid", "true");
    }
    if (errorEl) {
      errorEl.textContent = message;
      errorEl.hidden = false;
    }
  }

  function clearFieldError(input, errorEl) {
    if (input) {
      input.classList.remove("is-invalid");
      input.removeAttribute("aria-invalid");
    }
    if (errorEl) {
      errorEl.textContent = "";
      errorEl.hidden = true;
    }
  }

  function validateDisplayName(showError = true) {
    const value = (inputProfileName?.value || "").trim();
    if (!value) {
      if (showError) setFieldError(inputProfileName, profileNameError, tr("settings_err_name_required", "Display name is required."));
      return false;
    }
    if (value.length > 50) {
      if (showError) setFieldError(inputProfileName, profileNameError, tr("settings_err_name_too_long", "Display name cannot exceed 50 characters."));
      return false;
    }
    clearFieldError(inputProfileName, profileNameError);
    return true;
  }

  function validateEmail(showError = true) {
    const value = (inputProfileEmail?.value || "").trim();
    if (!value) {
      if (showError) setFieldError(inputProfileEmail, profileEmailError, tr("settings_err_email_required", "Email address is required."));
      return false;
    }
    if (!EMAIL_PATTERN.test(value)) {
      if (showError) setFieldError(inputProfileEmail, profileEmailError, tr("settings_err_email_invalid", "Please enter a valid email address."));
      return false;
    }
    clearFieldError(inputProfileEmail, profileEmailError);
    return true;
  }

  function isProfileDirty() {
    if (!currentProfile) return false;
    return (
      (inputProfileName?.value || "").trim() !== profileBaseline.display_name ||
      (inputProfileEmail?.value || "").trim().toLowerCase() !== profileBaseline.email.toLowerCase()
    );
  }

  function updateNameCounter() {
    if (!profileNameCounter || !inputProfileName) return;
    const len = inputProfileName.value.length;
    profileNameCounter.textContent = `${len}/50`;
    profileNameCounter.classList.toggle("is-near-limit", len >= 40 && len < 50);
    profileNameCounter.classList.toggle("is-at-limit", len >= 50);
  }

  function updateProfileDirtyState() {
    const dirty = isProfileDirty();
    const valid = validateDisplayName(false) && validateEmail(false);

    if (btnSaveProfile) btnSaveProfile.disabled = !dirty || !valid;
    if (btnResetProfile) btnResetProfile.hidden = !dirty;
    if (profileUnsavedFlag) profileUnsavedFlag.hidden = !dirty;

    if (profileEmailNotice) {
      const emailChanged =
        (inputProfileEmail?.value || "").trim().toLowerCase() !== profileBaseline.email.toLowerCase();
      profileEmailNotice.hidden = !emailChanged;
    }
  }

  function resetProfileForm() {
    if (inputProfileName) inputProfileName.value = profileBaseline.display_name;
    if (inputProfileEmail) inputProfileEmail.value = profileBaseline.email;
    clearFieldError(inputProfileName, profileNameError);
    clearFieldError(inputProfileEmail, profileEmailError);
    updateHeroIdentity(profileBaseline.display_name);
    updateNameCounter();
    updateProfileDirtyState();
  }

  inputProfileName?.addEventListener("input", () => {
    updateHeroIdentity(inputProfileName.value);
    updateNameCounter();
    if (inputProfileName.classList.contains("is-invalid")) validateDisplayName(true);
    updateProfileDirtyState();
  });
  inputProfileName?.addEventListener("blur", () => {
    validateDisplayName(true);
    updateProfileDirtyState();
  });

  inputProfileEmail?.addEventListener("input", () => {
    if (inputProfileEmail.classList.contains("is-invalid")) validateEmail(true);
    updateProfileDirtyState();
  });
  inputProfileEmail?.addEventListener("blur", () => {
    validateEmail(true);
    updateProfileDirtyState();
  });

  btnResetProfile?.addEventListener("click", resetProfileForm);

  // Guard against closing the tab mid-edit.
  window.addEventListener("beforeunload", (e) => {
    if (!isProfileDirty()) return;
    e.preventDefault();
    e.returnValue = "";
  });

  formProfile?.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!btnSaveProfile) return;

    const nameOk = validateDisplayName(true);
    const emailOk = validateEmail(true);
    if (!nameOk || !emailOk) {
      (nameOk ? inputProfileEmail : inputProfileName)?.focus();
      showToast(tr("settings_fix_errors_first", "Please correct the highlighted fields before saving."), "error");
      return;
    }
    if (!isProfileDirty()) return;

    setButtonBusy(btnSaveProfile, true, tr("saving", "Saving…"));

    try {
      const payload = {
        display_name: (inputProfileName?.value || "").trim(),
        email: (inputProfileEmail?.value || "").trim(),
      };

      const res = await fetch("/api/user/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(extractApiError(data, tr("settings_profile_save_failed", "Failed to update profile.")));

      currentProfile = data.profile;
      renderProfile(data.profile);
      showToast(data.message || tr("settings_profile_saved", "Profile updated successfully."), "success");
      syncMastheadIdentity(data.profile);
    } catch (err) {
      // Server-side rejections are field-specific often enough to be worth anchoring.
      const message = String(err.message || "");
      if (/email/i.test(message)) {
        setFieldError(inputProfileEmail, profileEmailError, message);
      } else if (/display name/i.test(message)) {
        setFieldError(inputProfileName, profileNameError, message);
      }
      showToast(message, "error");
    } finally {
      setButtonBusy(btnSaveProfile, false);
      updateProfileDirtyState();
    }
  });

  /** Keep the masthead chip and its dropdown in step with a saved profile. */
  function syncMastheadIdentity(profile) {
    const name = profile?.display_name || profile?.username || "";
    if (!name) return;
    const initials = computeInitials(name);
    document.querySelectorAll(".masthead-user-name, .masthead-info-name").forEach((el) => {
      el.textContent = name;
    });
    document.querySelectorAll(".masthead-user-avatar").forEach((el) => {
      el.textContent = initials;
    });
  }

  /* ------------------------------------------------------------------------ */
  /* Password Strength & Change Form                                          */
  /* ------------------------------------------------------------------------ */

  function evaluatePasswordStrength(password) {
    if (!password) return { score: 0, label: "Too weak", color: "var(--sell)" };
    let score = 0;
    if (password.length >= 8) score += 1;
    if (password.length >= 12) score += 1;
    if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score += 1;
    if (/\d/.test(password)) score += 1;
    if (/[^A-Za-z0-9]/.test(password)) score += 1;

    if (score <= 1) return { score: 20, label: "Too weak", color: "var(--sell)" };
    if (score === 2) return { score: 40, label: "Weak", color: "#f87171" };
    if (score === 3) return { score: 65, label: "Medium", color: "var(--warn)" };
    if (score === 4) return { score: 85, label: "Strong", color: "#6ee7b7" };
    return { score: 100, label: "Very Strong", color: "var(--buy)" };
  }

  inputPwNew?.addEventListener("input", () => {
    const val = inputPwNew.value;
    const { score, label, color } = evaluatePasswordStrength(val);
    if (pwStrengthFill) {
      pwStrengthFill.style.width = `${score}%`;
      pwStrengthFill.style.backgroundColor = color;
    }
    if (pwStrengthLabel) {
      pwStrengthLabel.textContent = label;
      pwStrengthLabel.style.color = color;
    }
  });

  inputPwConfirm?.addEventListener("input", () => {
    if (!pwMatchHint) return;
    if (inputPwNew.value && inputPwConfirm.value) {
      if (inputPwNew.value === inputPwConfirm.value) {
        pwMatchHint.textContent = "✓ Passwords match";
        pwMatchHint.style.color = "var(--buy)";
      } else {
        pwMatchHint.textContent = "✗ Passwords do not match";
        pwMatchHint.style.color = "var(--sell)";
      }
    } else {
      pwMatchHint.textContent = "Must be at least 8 characters long.";
      pwMatchHint.style.color = "var(--muted)";
    }
  });

  // Password Show / Hide Toggles
  document.querySelectorAll(".btn-toggle-pw").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetId = btn.dataset.target;
      const targetInput = $(targetId);
      if (!targetInput) return;

      const isPassword = targetInput.type === "password";
      targetInput.type = isPassword ? "text" : "password";

      const eyeOpen = btn.querySelector(".eye-open");
      const eyeClosed = btn.querySelector(".eye-closed");
      if (eyeOpen && eyeClosed) {
        eyeOpen.hidden = isPassword;
        eyeClosed.hidden = !isPassword;
      }
    });
  });

  formChangePassword?.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!btnChangePw) return;

    const currentPassword = (inputPwCurrent?.value || "").trim();
    const newPassword = (inputPwNew?.value || "").trim();
    const confirmPassword = (inputPwConfirm?.value || "").trim();

    if (!currentPassword) {
      showToast("Please enter your current password", "error");
      return;
    }
    if (newPassword.length < 8) {
      showToast("New password must be at least 8 characters long", "error");
      return;
    }
    if (newPassword !== confirmPassword) {
      showToast("New passwords do not match", "error");
      return;
    }

    const originalBtnHtml = btnChangePw.innerHTML;
    btnChangePw.disabled = true;
    btnChangePw.innerHTML = `<div class="spinner spinner-xs"></div> Updating…`;

    try {
      const res = await fetch("/api/user/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to update password");

      formChangePassword.reset();
      if (pwStrengthFill) pwStrengthFill.style.width = "0%";
      if (pwStrengthLabel) pwStrengthLabel.textContent = "Too weak";
      if (pwMatchHint) {
        pwMatchHint.textContent = "Must be at least 8 characters long.";
        pwMatchHint.style.color = "var(--muted)";
      }

      showToast(data.message || "Password updated successfully!", "success");
      playDeskChime("success");
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btnChangePw.disabled = false;
      btnChangePw.innerHTML = originalBtnHtml;
    }
  });

  /* ------------------------------------------------------------------------ */
  /* Active Sessions Management                                               */
  /* ------------------------------------------------------------------------ */

  async function fetchSessions() {
    if (!sessionsContainer) return;
    try {
      const res = await fetch("/api/user/sessions");
      if (!res.ok) throw new Error("Failed to load sessions");
      const data = await res.json();
      if (data.ok && data.sessions) {
        activeSessions = data.sessions;
        renderSessions(data.sessions);
      }
    } catch (err) {
      sessionsContainer.innerHTML = `<div class="panel-empty-state"><p class="text-danger">${escapeHtml(err.message)}</p></div>`;
    }
  }

  function renderSessions(sessions) {
    if (!sessionsContainer) return;
    if (!sessions || sessions.length === 0) {
      sessionsContainer.innerHTML = `<div class="panel-empty-state"><p>No active sessions found.</p></div>`;
      return;
    }

    sessionsContainer.innerHTML = sessions
      .map((s) => {
        const isCurrent = s.is_current;
        return `
        <div class="session-card ${isCurrent ? "is-current" : ""}">
          <div class="session-icon-box">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18">
              <rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect>
              <line x1="8" y1="21" x2="16" y2="21"></line>
              <line x1="12" y1="17" x2="12" y2="21"></line>
            </svg>
          </div>
          <div class="session-info">
            <div class="session-title-row">
              <span class="session-device-name">${escapeHtml(s.client_name)} on ${escapeHtml(s.os_name)}</span>
              ${
                isCurrent
                  ? `<span class="session-current-badge"><span class="pulse-dot"></span> This Device</span>`
                  : ""
              }
            </div>
            <div class="session-meta-row">
              <span>Logged in: ${formatDateTime(s.created_at)}</span>
              <span>•</span>
              <span>Expires: ${formatDateTime(s.expires_at)}</span>
            </div>
          </div>
          <div class="session-actions">
            ${
              !isCurrent
                ? `<button type="button" class="btn btn-secondary btn-xs btn-terminate-session" data-token="${escapeHtml(s.token)}">
                     <span data-i18n="settings_revoke">Revoke</span>
                   </button>`
                : `<span class="tag-status-pill tag-ok">Active Now</span>`
            }
          </div>
        </div>
      `;
      })
      .join("");

    // Attach revoke buttons
    sessionsContainer.querySelectorAll(".btn-terminate-session").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const token = btn.dataset.token;
        if (!token) return;

        btn.disabled = true;
        btn.innerHTML = `<div class="spinner spinner-xs"></div>`;

        try {
          const res = await fetch("/api/user/sessions/terminate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || "Failed to terminate session");

          showToast("Session revoked.", "success");
          fetchSessions();
        } catch (err) {
          showToast(err.message, "error");
          btn.disabled = false;
          btn.textContent = "Revoke";
        }
      });
    });
  }

  btnTerminateOtherSessions?.addEventListener("click", async () => {
    if (!confirm("Are you sure you want to sign out of all other active browsers and devices?")) {
      return;
    }

    const origHtml = btnTerminateOtherSessions.innerHTML;
    btnTerminateOtherSessions.disabled = true;
    btnTerminateOtherSessions.innerHTML = `<div class="spinner spinner-xs"></div> Revoking…`;

    try {
      const res = await fetch("/api/user/sessions/terminate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ terminate_others: true }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to revoke other sessions");

      showToast(data.message || "All other sessions signed out.", "success");
      fetchSessions();
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btnTerminateOtherSessions.disabled = false;
      btnTerminateOtherSessions.innerHTML = origHtml;
    }
  });

  /* ------------------------------------------------------------------------ */
  /* Preferences: Theme, Appearance, Locale, Audio                            */
  /* ------------------------------------------------------------------------ */

  function applyTheme(themeName) {
    const validThemes = ["obsidian", "midnight", "emerald", "daylight"];
    if (!validThemes.includes(themeName)) themeName = "obsidian";

    const root = document.documentElement;
    root.setAttribute("data-theme", themeName);
    try {
      localStorage.setItem("algopaca_theme", themeName);
      document.cookie = `algopaca_theme=${encodeURIComponent(themeName)}; path=/; max-age=31536000; SameSite=Lax`;
    } catch (e) {}

    themeCards.forEach((card) => {
      const isSelected = card.dataset.themeVal === themeName;
      card.classList.toggle("is-active", isSelected);
      const radio = card.querySelector("input[type='radio']");
      if (radio) radio.checked = isSelected;
    });

    window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: themeName } }));
  }

  // Synchronize theme cards immediately with active page theme
  const initialDeskTheme = document.documentElement.getAttribute("data-theme") || localStorage.getItem("algopaca_theme") || "obsidian";
  applyTheme(initialDeskTheme);

  themeCards.forEach((card) => {
    card.addEventListener("click", () => {
      const themeVal = card.dataset.themeVal;
      if (themeVal) applyTheme(themeVal);
    });
  });

  btnTestSound?.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    playDeskChime("success");
  });

  async function fetchPreferences() {
    try {
      const res = await fetch("/api/user/preferences");
      if (!res.ok) throw new Error("Failed to load preferences");
      const data = await res.json();
      if (data.ok && data.preferences) {
        currentPreferences = data.preferences;
        renderPreferences(data.preferences);
      }
    } catch (err) {
      console.warn("Preferences load error:", err);
    }
  }

  function renderPreferences(prefs) {
    if (prefs.theme) {
      applyTheme(prefs.theme);
    }
    if (selectSettingsLang && prefs.language) {
      selectSettingsLang.value = prefs.language;
    }
    if (selectSettingsTimezone && prefs.timezone_display) {
      selectSettingsTimezone.value = prefs.timezone_display;
    }
    if (selectSettingsDefaultPage && prefs.default_page) {
      selectSettingsDefaultPage.value = prefs.default_page;
    }
    if (selectSettingsRefresh && prefs.chart_refresh_interval) {
      selectSettingsRefresh.value = String(prefs.chart_refresh_interval);
    }
    if (checkSoundAlerts) {
      checkSoundAlerts.checked = !!prefs.sound_alerts;
    }
    if (checkCompactMode) {
      checkCompactMode.checked = !!prefs.compact_mode;
    }

    // Trading defaults
    if (selectDefaultSizeMode && prefs.default_size_mode) {
      selectDefaultSizeMode.value = prefs.default_size_mode;
    }
    if (inputDefaultTradeQty && prefs.default_trade_qty !== undefined) {
      inputDefaultTradeQty.value = prefs.default_trade_qty;
    }
    if (inputDefaultTradeNotional && prefs.default_trade_notional !== undefined) {
      inputDefaultTradeNotional.value = prefs.default_trade_notional;
    }
    if (checkConfirmOrders) {
      checkConfirmOrders.checked = !!prefs.confirm_orders;
    }
    if (checkConfirmCloseAll) {
      checkConfirmCloseAll.checked = !!prefs.confirm_close_all;
    }
    if (checkSettingsRequireApproval) {
      checkSettingsRequireApproval.checked = !!prefs.require_approval;
    }
    if (checkSettingsNotifyBrowser) {
      checkSettingsNotifyBrowser.checked = prefs.notify_browser !== undefined ? !!prefs.notify_browser : true;
    }
    if (checkSettingsNotifyEmail) {
      checkSettingsNotifyEmail.checked = !!prefs.notify_email;
      if (wrapSettingsNotificationEmail) {
        wrapSettingsNotificationEmail.hidden = !prefs.notify_email;
      }
    }
    if (inputSettingsNotificationEmail) {
      inputSettingsNotificationEmail.value = prefs.notification_email || "";
    }

    if (typeof refreshNiceSelects === "function") {
      refreshNiceSelects();
    }
  }

  async function savePreferencesPayload(partialPayload, successMsg = "Preferences saved.") {
    try {
      const res = await fetch("/api/user/preferences", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(partialPayload),
      });
      const data = await res.json();
      if (!res.ok) {
        let msg = "Failed to save preferences";
        if (typeof data.detail === "string") {
          msg = data.detail;
        } else if (Array.isArray(data.detail) && data.detail[0]?.msg) {
          msg = data.detail[0].msg;
        } else if (data.message) {
          msg = data.message;
        }
        throw new Error(msg);
      }

      currentPreferences = data.preferences;
      showToast(successMsg, "success");
      return data.preferences;
    } catch (err) {
      showToast(err.message, "error");
      throw err;
    }
  }

  function validateTradeQty(showError = true) {
    if (!inputDefaultTradeQty) return true;
    const val = parseFloat(inputDefaultTradeQty.value);
    if (isNaN(val) || val <= 0) {
      if (showError) {
        setFieldError(
          inputDefaultTradeQty,
          tradeQtyError,
          tr("settings_err_trade_qty_invalid", "Default share quantity must be greater than 0.")
        );
      }
      return false;
    }
    clearFieldError(inputDefaultTradeQty, tradeQtyError);
    return true;
  }

  function validateTradeNotional(showError = true) {
    if (!inputDefaultTradeNotional) return true;
    const val = parseFloat(inputDefaultTradeNotional.value);
    if (isNaN(val) || val <= 0) {
      if (showError) {
        setFieldError(
          inputDefaultTradeNotional,
          tradeNotionalError,
          tr("settings_err_trade_notional_invalid", "Default dollar notional must be greater than 0.")
        );
      }
      return false;
    }
    clearFieldError(inputDefaultTradeNotional, tradeNotionalError);
    return true;
  }

  function validateNotificationEmail(showError = true) {
    if (!inputSettingsNotificationEmail) return true;
    const val = (inputSettingsNotificationEmail.value || "").trim();
    if (val && !EMAIL_PATTERN.test(val)) {
      if (showError) {
        setFieldError(
          inputSettingsNotificationEmail,
          notificationEmailError,
          tr("settings_err_notify_email_invalid", "Please enter a valid notification email address.")
        );
      }
      return false;
    }
    clearFieldError(inputSettingsNotificationEmail, notificationEmailError);
    return true;
  }

  inputDefaultTradeQty?.addEventListener("input", () => {
    if (inputDefaultTradeQty.classList.contains("is-invalid")) validateTradeQty(true);
  });
  inputDefaultTradeQty?.addEventListener("blur", () => {
    validateTradeQty(true);
  });

  inputDefaultTradeNotional?.addEventListener("input", () => {
    if (inputDefaultTradeNotional.classList.contains("is-invalid")) validateTradeNotional(true);
  });
  inputDefaultTradeNotional?.addEventListener("blur", () => {
    validateTradeNotional(true);
  });

  inputSettingsNotificationEmail?.addEventListener("input", () => {
    if (inputSettingsNotificationEmail.classList.contains("is-invalid")) validateNotificationEmail(true);
  });
  inputSettingsNotificationEmail?.addEventListener("blur", () => {
    validateNotificationEmail(true);
  });

  btnSaveAppearance?.addEventListener("click", async () => {
    setButtonBusy(btnSaveAppearance, true, tr("saving", "Saving…"));

    try {
      const selectedTheme = document.querySelector(".theme-card.is-active")?.dataset.themeVal || "obsidian";
      const lang = selectSettingsLang?.value || "en";
      const tz = selectSettingsTimezone?.value || "local";
      const defPage = selectSettingsDefaultPage?.value || "auto-trade";
      const pollRate = parseInt(selectSettingsRefresh?.value || "20", 10);
      const sound = checkSoundAlerts ? checkSoundAlerts.checked : true;
      const compact = checkCompactMode ? checkCompactMode.checked : false;

      await savePreferencesPayload({
        theme: selectedTheme,
        language: lang,
        timezone_display: tz,
        default_page: defPage,
        chart_refresh_interval: pollRate,
        sound_alerts: sound,
        compact_mode: compact,
      });

      // Switch language in i18n engine if changed
      if (typeof i18n !== "undefined" && i18n.setLanguage && lang !== i18n.getCurrentLanguage()) {
        await i18n.setLanguage(lang);
      }
    } catch (e) {
      // toast shown
    } finally {
      setButtonBusy(btnSaveAppearance, false);
    }
  });

  formTradingDefaults?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("btn-save-trading-defaults");
    if (!btn) return;

    const validQty = validateTradeQty(true);
    const validNotional = validateTradeNotional(true);
    const validEmail = validateNotificationEmail(true);

    if (!validQty || !validNotional || !validEmail) {
      if (!validQty) inputDefaultTradeQty?.focus();
      else if (!validNotional) inputDefaultTradeNotional?.focus();
      else if (!validEmail) inputSettingsNotificationEmail?.focus();
      return;
    }

    setButtonBusy(btn, true, tr("saving", "Saving…"));

    try {
      const sizeMode = selectDefaultSizeMode?.value || "qty";
      const qty = parseFloat(inputDefaultTradeQty?.value || "1.0");
      const notional = parseFloat(inputDefaultTradeNotional?.value || "100.0");
      const confirmOrders = checkConfirmOrders ? checkConfirmOrders.checked : true;
      const confirmCloseAll = checkConfirmCloseAll ? checkConfirmCloseAll.checked : true;
      const requireApproval = checkSettingsRequireApproval ? checkSettingsRequireApproval.checked : false;
      const notifyBrowser = checkSettingsNotifyBrowser ? checkSettingsNotifyBrowser.checked : true;
      const notifyEmail = checkSettingsNotifyEmail ? checkSettingsNotifyEmail.checked : false;
      const notificationEmail = inputSettingsNotificationEmail?.value?.trim() || "";

      await savePreferencesPayload({
        default_size_mode: sizeMode,
        default_trade_qty: qty,
        default_trade_notional: notional,
        confirm_orders: confirmOrders,
        confirm_close_all: confirmCloseAll,
        require_approval: requireApproval,
        notify_browser: notifyBrowser,
        notify_email: notifyEmail,
        notification_email: notificationEmail,
      }, tr("settings_trading_defaults_saved", "Trading defaults saved successfully."));
    } catch (err) {
      // toast shown
    } finally {
      setButtonBusy(btn, false);
    }
  });

  checkSettingsNotifyEmail?.addEventListener("change", (ev) => {
    if (wrapSettingsNotificationEmail) {
      wrapSettingsNotificationEmail.hidden = !ev.target.checked;
    }
    if (!ev.target.checked && inputSettingsNotificationEmail) {
      clearFieldError(inputSettingsNotificationEmail, notificationEmailError);
    }
  });

  /* ------------------------------------------------------------------------ */
  /* Data Export & Account Deletion                                           */
  /* ------------------------------------------------------------------------ */

  btnExportData?.addEventListener("click", async () => {
    const origHtml = btnExportData.innerHTML;
    btnExportData.disabled = true;
    btnExportData.innerHTML = `<div class="spinner spinner-xs"></div> Preparing Export…`;

    try {
      const res = await fetch("/api/user/export");
      if (!res.ok) throw new Error("Failed to generate account export");
      const data = await res.json();

      const blob = new Blob([JSON.stringify(data.data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const username = currentProfile?.username || "trader";
      a.href = url;
      a.download = `algopaca_export_${username}_${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      showToast("Account data exported successfully!", "success");
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btnExportData.disabled = false;
      btnExportData.innerHTML = origHtml;
    }
  });

  // Account Deletion Modal
  btnOpenDeleteModal?.addEventListener("click", () => {
    if (modalDeleteAccount) {
      modalDeleteAccount.hidden = false;
      if (inputDeletePassword) {
        inputDeletePassword.value = "";
        inputDeletePassword.focus();
      }
    }
  });

  function closeDeleteModal() {
    if (modalDeleteAccount) {
      modalDeleteAccount.hidden = true;
      if (inputDeletePassword) inputDeletePassword.value = "";
    }
  }

  btnCloseDeleteModal?.addEventListener("click", closeDeleteModal);
  btnCancelDelete?.addEventListener("click", closeDeleteModal);

  modalDeleteAccount?.addEventListener("click", (e) => {
    if (e.target === modalDeleteAccount) closeDeleteModal();
  });

  btnConfirmDelete?.addEventListener("click", async () => {
    const password = (inputDeletePassword?.value || "").trim();
    if (!password) {
      showToast("Password is required to confirm account deletion.", "error");
      return;
    }

    const origHtml = btnConfirmDelete.innerHTML;
    btnConfirmDelete.disabled = true;
    btnConfirmDelete.innerHTML = `<div class="spinner spinner-xs"></div> Deleting…`;

    try {
      const res = await fetch("/api/user/account", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to delete account");

      showToast("Account deleted. Redirecting…", "success");
      setTimeout(() => {
        window.location.href = "/login";
      }, 1200);
    } catch (err) {
      showToast(err.message, "error");
      btnConfirmDelete.disabled = false;
      btnConfirmDelete.innerHTML = origHtml;
    }
  });

  /* ------------------------------------------------------------------------ */
  /* Initialization                                                           */
  /* ------------------------------------------------------------------------ */

  document.addEventListener("DOMContentLoaded", () => {
    checkHashTab(true);
    updateNameCounter();
    fetchProfile();
    fetchPreferences();

    window.addEventListener("hashchange", () => checkHashTab(false));
  });
})();
