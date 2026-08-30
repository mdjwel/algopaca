/**
 * AlgoPaca - Setup Wizard Controller (Administration Onboarding)
 * Manages the 4-step administrative setup flow:
 * 1. Primary Owner Account Profile
 * 2. Outbound SMTP Mail Server & Live Diagnostics
 * 3. Platform Options, Themes & Regional Preferences
 * 4. Launchpad Review & Desk Initialization
 */

(function () {
  "use strict";

  const TOTAL_STEPS = 4;
  let currentStep = 1;

  const wizardData = {
    needsSetup: true,
    // Step 1: Owner Profile
    username: "",
    email: "",
    displayName: "",
    password: "",
    confirmPassword: "",
    // Step 2: SMTP Mail Server
    smtpHost: "",
    smtpPort: 587,
    smtpUser: "",
    smtpPass: "",
    smtpFrom: "",
    smtpSenderName: "AlgoPaca",
    smtpUseSsl: false,
    smtpTestEmail: "",
    smtpVerified: false,
    // Step 3: Platform Preferences
    theme: "obsidian",
    lang: "en",
    defaultPage: "auto-trade",
    timezoneDisplay: "local",
    soundAlerts: true,
    notifyBrowser: true,
    notifyEmail: false,
  };

  const $ = (id) => document.getElementById(id);

  // DOM Elements
  const progressCounter = $("wizard-progress-counter");
  const btnPrev = $("btn-wizard-prev");
  const btnNext = $("btn-wizard-next");
  const btnNextLabel = $("btn-next-label");
  const btnSkip = $("btn-wizard-skip");
  const stepperTrack = $("wizard-stepper-track");

  // Step 1 Elements
  const inputUsername = $("wizard-field-username");
  const inputEmail = $("wizard-field-email");
  const inputDisplayName = $("wizard-field-display-name");
  const inputPassword = $("wizard-field-password");
  const inputConfirmPassword = $("wizard-field-confirm-password");
  const btnToggleOwnerPw = $("btn-toggle-owner-pw");
  const btnToggleConfirmPw = $("btn-toggle-confirm-pw");
  const pwMeterBar = $("pw-meter-bar");
  const pwMeterLabel = $("pw-meter-label");
  const confirmHint = $("wizard-confirm-hint");
  const errUsername = $("wizard-err-username");
  const errEmail = $("wizard-err-email");
  const errPassword = $("wizard-err-password");
  const errConfirmPassword = $("wizard-err-confirm-password");

  // Step 2 Elements (SMTP)
  const inputSmtpHost = $("wizard-smtp-host");
  const selectSmtpPort = $("wizard-smtp-port");
  const inputSmtpUser = $("wizard-smtp-user");
  const inputSmtpPass = $("wizard-smtp-pass");
  const inputSmtpFrom = $("wizard-smtp-from");
  const inputSmtpSenderName = $("wizard-smtp-sender-name");
  const checkSmtpSsl = $("check-wizard-smtp-ssl");
  const inputSmtpTestEmail = $("wizard-smtp-test-email");
  const btnTestSmtp = $("btn-wizard-test-smtp");
  const smtpTestMsg = $("wizard-smtp-test-msg");
  const btnToggleSmtpPw = $("btn-toggle-smtp-pw");

  // Step 3 Elements (Platform)
  const selectLang = $("wizard-select-lang");
  const selectDefaultPage = $("wizard-default-page");
  const selectTimezoneDisplay = $("wizard-timezone-display");
  const checkSound = $("check-wizard-sound");
  const checkBrowserNotify = $("check-wizard-browser-notify");
  const checkEmailNotify = $("check-wizard-email-notify");

  // Step 4 Summary Elements
  const summaryOwnerName = $("summary-owner-name");
  const summaryOwnerEmail = $("summary-owner-email");
  const summarySmtpStatus = $("summary-smtp-status");
  const summarySmtpDetail = $("summary-smtp-detail");
  const summaryTheme = $("summary-theme");
  const summaryLang = $("summary-lang");
  const summaryDefaultPage = $("summary-default-page");
  const summaryTimezone = $("summary-timezone");

  function tx(key, fallback = "", params = {}) {
    if (window.i18n && typeof window.i18n.t === "function") {
      return window.i18n.t(key, fallback, params);
    }
    return fallback || key;
  }

  function setFieldError(inputEl, errEl, message) {
    if (inputEl) {
      inputEl.classList.toggle("has-error", Boolean(message));
    }
    if (errEl) {
      errEl.hidden = !message;
      errEl.textContent = message || "";
    }
  }

  function clearAllFieldErrors() {
    setFieldError(inputUsername, errUsername, "");
    setFieldError(inputEmail, errEmail, "");
    setFieldError(inputPassword, errPassword, "");
    setFieldError(inputConfirmPassword, errConfirmPassword, "");
  }

  function evaluatePasswordStrength(pw) {
    if (!pw) return { score: 0, label: "" };
    let score = 0;
    if (pw.length >= 8) score += 1;
    if (pw.length >= 12) score += 1;
    if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) score += 1;
    if (/[0-9]/.test(pw)) score += 1;
    if (/[^A-Za-z0-9]/.test(pw)) score += 1;

    if (score <= 1) return { score: 1, label: tx("auth_pw_weak", "Weak"), cls: "is-weak" };
    if (score === 2) return { score: 2, label: tx("auth_pw_fair", "Fair"), cls: "is-fair" };
    if (score === 3 || score === 4) return { score: 3, label: tx("auth_pw_good", "Good"), cls: "is-good" };
    return { score: 4, label: tx("auth_pw_strong", "Strong"), cls: "is-strong" };
  }

  function updatePasswordMeter() {
    const pw = inputPassword?.value || "";
    const res = evaluatePasswordStrength(pw);
    if (pwMeterBar) {
      pwMeterBar.className = "pw-meter-bar " + (res.cls || "");
    }
    if (pwMeterLabel) {
      pwMeterLabel.textContent = res.label;
    }
    updatePasswordMatch();
  }

  function updatePasswordMatch() {
    if (!confirmHint) return;
    const pw = inputPassword?.value || "";
    const cpw = inputConfirmPassword?.value || "";
    if (!cpw) {
      confirmHint.hidden = true;
      confirmHint.textContent = "";
      return;
    }
    confirmHint.hidden = false;
    if (pw === cpw) {
      confirmHint.className = "wizard-match-hint is-match";
      confirmHint.textContent = "✓ " + tx("auth_pw_match_success", "Passwords match");
      setFieldError(inputConfirmPassword, errConfirmPassword, "");
    } else {
      confirmHint.className = "wizard-match-hint is-mismatch";
      confirmHint.textContent = "✕ " + tx("auth_pw_match_error", "Passwords do not match");
    }
  }

  function setupPasswordToggle(btn, input) {
    if (!btn || !input) return;
    btn.addEventListener("click", () => {
      const isPw = input.type === "password";
      input.type = isPw ? "text" : "password";
      const iconOpen = btn.querySelector(".icon-eye-open");
      const iconClosed = btn.querySelector(".icon-eye-closed");
      if (iconOpen) iconOpen.hidden = isPw;
      if (iconClosed) iconClosed.hidden = !isPw;
    });
  }

  function highlightSelectedCard(radioEl) {
    if (!radioEl) return;
    const name = radioEl.getAttribute("name");
    document.querySelectorAll(`input[name="${name}"]`).forEach((input) => {
      const card = input.closest(".theme-card");
      if (card) card.classList.toggle("is-selected", input.checked);
    });
  }

  async function loadExistingState() {
    try {
      const res = await api("/api/setup/status");
      if (res && res.ok) {
        wizardData.needsSetup = Boolean(res.needs_setup);

        if (res.smtp && typeof res.smtp === "object") {
          wizardData.smtpHost = res.smtp.host || "";
          wizardData.smtpPort = res.smtp.port || 587;
          wizardData.smtpUser = res.smtp.username || "";
          wizardData.smtpFrom = res.smtp.from_email || "";
          wizardData.smtpSenderName = res.smtp.sender_name || "AlgoPaca";
          wizardData.smtpUseSsl = Boolean(res.smtp.use_ssl);
          wizardData.smtpVerified = Boolean(res.smtp.configured);
        }

        if (res.user) {
          wizardData.username = res.user.username || "";
          wizardData.email = res.user.email || "";
        }
      }
    } catch (err) {
      console.warn("Could not load setup status:", err);
    }
    populateFields();
  }

  function populateFields() {
    if (inputUsername && wizardData.username) inputUsername.value = wizardData.username;
    if (inputEmail && wizardData.email) inputEmail.value = wizardData.email;
    if (inputDisplayName && wizardData.displayName) inputDisplayName.value = wizardData.displayName;

    if (inputSmtpHost) inputSmtpHost.value = wizardData.smtpHost;
    if (selectSmtpPort) selectSmtpPort.value = String(wizardData.smtpPort);
    if (inputSmtpUser) inputSmtpUser.value = wizardData.smtpUser;
    if (inputSmtpFrom) inputSmtpFrom.value = wizardData.smtpFrom;
    if (inputSmtpSenderName) inputSmtpSenderName.value = wizardData.smtpSenderName;
    if (checkSmtpSsl) checkSmtpSsl.checked = wizardData.smtpUseSsl;
    if (inputSmtpTestEmail) inputSmtpTestEmail.value = wizardData.email || "";

    const themeRadio = document.querySelector(`input[name="wizard_theme"][value="${wizardData.theme}"]`);
    if (themeRadio) {
      themeRadio.checked = true;
      highlightSelectedCard(themeRadio);
    }

    if (selectLang) selectLang.value = wizardData.lang;
    if (selectDefaultPage) selectDefaultPage.value = wizardData.defaultPage;
    if (selectTimezoneDisplay) selectTimezoneDisplay.value = wizardData.timezoneDisplay;
    if (checkSound) checkSound.checked = wizardData.soundAlerts;
    if (checkBrowserNotify) checkBrowserNotify.checked = wizardData.notifyBrowser;
    if (checkEmailNotify) checkEmailNotify.checked = wizardData.notifyEmail;

    if (typeof refreshNiceSelects === "function") {
      refreshNiceSelects();
    }
  }

  function validateCurrentStep() {
    clearAllFieldErrors();

    if (currentStep === 1) {
      if (wizardData.needsSetup) {
        const u = inputUsername?.value.trim() || "";
        const em = inputEmail?.value.trim() || "";
        const pw = inputPassword?.value || "";
        const cpw = inputConfirmPassword?.value || "";

        if (!u) {
          setFieldError(inputUsername, errUsername, tx("wizard_val_username_req", "Username is required."));
          inputUsername?.focus();
          return false;
        }
        if (!/^[a-zA-Z0-9_.-]{3,30}$/.test(u)) {
          setFieldError(inputUsername, errUsername, tx("wizard_val_username_fmt", "Username must be 3-30 characters (letters, numbers, underscores, dots, dashes)."));
          inputUsername?.focus();
          return false;
        }
        if (!em) {
          setFieldError(inputEmail, errEmail, tx("wizard_val_email_req", "Email address is required."));
          inputEmail?.focus();
          return false;
        }
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(em)) {
          setFieldError(inputEmail, errEmail, tx("wizard_val_email_fmt", "Please enter a valid email address."));
          inputEmail?.focus();
          return false;
        }
        if (!pw || pw.length < 8) {
          setFieldError(inputPassword, errPassword, tx("wizard_val_pw_len", "Password must be at least 8 characters long."));
          inputPassword?.focus();
          return false;
        }
        if (pw !== cpw) {
          setFieldError(inputConfirmPassword, errConfirmPassword, tx("auth_pw_match_error", "Passwords do not match."));
          inputConfirmPassword?.focus();
          return false;
        }

        wizardData.username = u;
        wizardData.email = em;
        wizardData.displayName = inputDisplayName?.value.trim() || u;
        wizardData.password = pw;
        wizardData.confirmPassword = cpw;

        if (inputSmtpTestEmail && !inputSmtpTestEmail.value) {
          inputSmtpTestEmail.value = em;
        }
      }
      return true;
    }

    if (currentStep === 2) {
      wizardData.smtpHost = inputSmtpHost?.value.trim() || "";
      wizardData.smtpPort = parseInt(selectSmtpPort?.value || "587", 10);
      wizardData.smtpUser = inputSmtpUser?.value.trim() || "";
      wizardData.smtpPass = inputSmtpPass?.value || "";
      wizardData.smtpFrom = inputSmtpFrom?.value.trim() || "";
      wizardData.smtpSenderName = inputSmtpSenderName?.value.trim() || "AlgoPaca";
      wizardData.smtpUseSsl = Boolean(checkSmtpSsl?.checked);
      return true;
    }

    if (currentStep === 3) {
      const selectedTheme = document.querySelector('input[name="wizard_theme"]:checked');
      if (selectedTheme) wizardData.theme = selectedTheme.value;
      if (selectLang) wizardData.lang = selectLang.value;
      if (selectDefaultPage) wizardData.defaultPage = selectDefaultPage.value;
      if (selectTimezoneDisplay) wizardData.timezoneDisplay = selectTimezoneDisplay.value;
      if (checkSound) wizardData.soundAlerts = checkSound.checked;
      if (checkBrowserNotify) wizardData.notifyBrowser = checkBrowserNotify.checked;
      if (checkEmailNotify) wizardData.notifyEmail = checkEmailNotify.checked;
      return true;
    }

    return true;
  }

  function updateSummaryView() {
    if (summaryOwnerName) {
      summaryOwnerName.textContent = `${wizardData.username || "admin"} (${tx("role_owner", "Owner")})`;
    }
    if (summaryOwnerEmail) {
      summaryOwnerEmail.textContent = wizardData.email || "—";
    }

    if (summarySmtpStatus) {
      if (wizardData.smtpHost) {
        summarySmtpStatus.textContent = wizardData.smtpVerified
          ? "✓ " + tx("wizard_smtp_verified", "Verified")
          : tx("wizard_smtp_configured", "Configured");
      } else {
        summarySmtpStatus.textContent = tx("wizard_smtp_skipped", "Skipped / Optional");
      }
    }
    if (summarySmtpDetail) {
      summarySmtpDetail.textContent = wizardData.smtpHost
        ? `${wizardData.smtpHost}:${wizardData.smtpPort}`
        : tx("wizard_smtp_none_desc", "Configure later in Admin Console");
    }

    if (summaryTheme) {
      const themeNames = {
        obsidian: "Obsidian (Dark)",
        midnight: "Midnight (Deep Blue)",
        emerald: "Emerald (Quant Green)",
        daylight: "Daylight (Clean Light)",
      };
      summaryTheme.textContent = themeNames[wizardData.theme] || wizardData.theme;
    }
    if (summaryLang) {
      const langNames = { en: "English (US)", bn: "বাংলা (Bengali)", es: "Español", fr: "Français", hi: "हिन्दी" };
      summaryLang.textContent = langNames[wizardData.lang] || wizardData.lang;
    }
    if (summaryDefaultPage) {
      summaryDefaultPage.textContent = tx(`nav_${wizardData.defaultPage.replace("-", "_")}`, wizardData.defaultPage);
    }
    if (summaryTimezone) {
      const tzNames = {
        local: tx("tz_local", "Local Browser Time"),
        exchange: tx("tz_exchange", "US Eastern Market Time"),
        utc: tx("tz_utc", "UTC / GMT"),
      };
      summaryTimezone.textContent = tzNames[wizardData.timezoneDisplay] || wizardData.timezoneDisplay;
    }
  }

  function navigateToStep(step, { scroll = true } = {}) {
    if (step < 1) step = 1;
    if (step > TOTAL_STEPS) step = TOTAL_STEPS;
    currentStep = step;

    // Update Progress Counter
    if (progressCounter) {
      progressCounter.textContent = tx("wizard_progress_step_of", `Step {step} of {total}`, {
        step: currentStep,
        total: TOTAL_STEPS,
      });
    }

    // Toggle Panels
    document.querySelectorAll(".wizard-step-panel").forEach((panel) => {
      const panelStep = parseInt(panel.getAttribute("data-step") || "1", 10);
      const isActive = panelStep === currentStep;
      panel.classList.toggle("is-active", isActive);
      panel.hidden = !isActive;
    });

    // Update Stepper Navigation
    document.querySelectorAll(".stepper-step").forEach((item) => {
      const s = parseInt(item.getAttribute("data-step") || "1", 10);
      item.classList.toggle("is-active", s === currentStep);
      item.classList.toggle("is-completed", s < currentStep);
      const btn = item.querySelector(".stepper-step-btn");
      if (btn) {
        if (s === currentStep) {
          btn.setAttribute("aria-current", "step");
        } else {
          btn.removeAttribute("aria-current");
        }
      }
    });

    // Update Footer Buttons
    if (btnPrev) {
      btnPrev.disabled = currentStep === 1;
    }

    if (btnSkip) {
      btnSkip.hidden = currentStep !== 2;
    }

    if (btnNextLabel) {
      if (currentStep === TOTAL_STEPS) {
        btnNextLabel.textContent = tx("wizard_btn_finish", "Complete Setup & Open Desk");
      } else {
        btnNextLabel.textContent = tx("wizard_btn_next", "Continue");
      }
    }

    if (currentStep === 4) {
      updateSummaryView();
    }

    if (scroll) {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  async function testSmtpConnection() {
    if (!btnTestSmtp) return;
    const recipient = (inputSmtpTestEmail?.value || wizardData.email || "").trim();
    if (!recipient) {
      if (smtpTestMsg) {
        smtpTestMsg.hidden = false;
        smtpTestMsg.className = "wizard-smtp-test-feedback is-error";
        smtpTestMsg.textContent = tx("smtp_recipient_required", "Recipient email is required for the connection test.");
      }
      inputSmtpTestEmail?.focus();
      return;
    }

    const host = inputSmtpHost?.value.trim() || "";
    if (!host) {
      if (smtpTestMsg) {
        smtpTestMsg.hidden = false;
        smtpTestMsg.className = "wizard-smtp-test-feedback is-error";
        smtpTestMsg.textContent = tx("smtp_host_required", "Please enter an SMTP host before running test.");
      }
      inputSmtpHost?.focus();
      return;
    }

    btnTestSmtp.disabled = true;
    const origHtml = btnTestSmtp.innerHTML;
    btnTestSmtp.innerHTML = `<span class="loading-spinner"></span> ${tx("wizard_verifying", "Testing Connection…")}`;

    if (smtpTestMsg) {
      smtpTestMsg.hidden = false;
      smtpTestMsg.className = "wizard-smtp-test-feedback";
      smtpTestMsg.textContent = tx("smtp_testing_in_progress", "Connecting to SMTP server and verifying credentials…");
    }

    try {
      const res = await api("/api/setup/test-smtp", {
        method: "POST",
        body: JSON.stringify({
          to_email: recipient,
          host: host,
          port: parseInt(selectSmtpPort?.value || "587", 10),
          username: inputSmtpUser?.value.trim() || "",
          password: inputSmtpPass?.value || "",
          from_email: inputSmtpFrom?.value.trim() || "",
          sender_name: inputSmtpSenderName?.value.trim() || "AlgoPaca",
          use_ssl: Boolean(checkSmtpSsl?.checked),
        }),
      });

      if (res && res.ok) {
        wizardData.smtpVerified = true;
        if (smtpTestMsg) {
          smtpTestMsg.className = "wizard-smtp-test-feedback is-success";
          const logLines = Array.isArray(res.logs) ? res.logs.map((l) => `[${l.step}] ${l.detail || l.message || ""}`).join("\n") : "";
          smtpTestMsg.textContent = `✓ ${res.message || tx("smtp_test_success", "SMTP Test Email Sent Successfully!")}\n${logLines}`;
        }
        if (typeof showToast === "function") {
          showToast(tx("smtp_test_success", "SMTP Connected Successfully!"), "success");
        }
      } else {
        wizardData.smtpVerified = false;
        if (smtpTestMsg) {
          smtpTestMsg.className = "wizard-smtp-test-feedback is-error";
          const errDetail = res?.error || res?.detail || tx("smtp_test_failed", "SMTP Test Failed.");
          const logLines = Array.isArray(res?.logs) ? "\n" + res.logs.map((l) => `[${l.step}] ${l.detail || l.message || ""}`).join("\n") : "";
          smtpTestMsg.textContent = `✕ ${errDetail}${logLines}`;
        }
      }
    } catch (err) {
      wizardData.smtpVerified = false;
      if (smtpTestMsg) {
        smtpTestMsg.className = "wizard-smtp-test-feedback is-error";
        smtpTestMsg.textContent = `✕ ${err.message || String(err)}`;
      }
    } finally {
      btnTestSmtp.disabled = false;
      btnTestSmtp.innerHTML = origHtml;
    }
  }

  async function completeSetupWizard() {
    if (btnNext) {
      btnNext.disabled = true;
      btnNext.innerHTML = `<span class="loading-spinner"></span> ${tx("wizard_saving", "Initializing Desk…")}`;
    }

    const payload = {
      username: wizardData.username,
      email: wizardData.email,
      display_name: wizardData.displayName || wizardData.username,
      password: wizardData.password,
      smtp_host: wizardData.smtpHost,
      smtp_port: wizardData.smtpPort,
      smtp_username: wizardData.smtpUser,
      smtp_password: wizardData.smtpPass,
      smtp_from_email: wizardData.smtpFrom,
      smtp_sender_name: wizardData.smtpSenderName,
      smtp_use_ssl: wizardData.smtpUseSsl,
      theme: wizardData.theme,
      lang: wizardData.lang,
      default_page: wizardData.defaultPage,
      timezone_display: wizardData.timezoneDisplay,
      sound_alerts: wizardData.soundAlerts,
      notify_browser: wizardData.notifyBrowser,
      notify_email: wizardData.notifyEmail,
    };

    try {
      const res = await api("/api/setup/complete", {
        method: "POST",
        body: JSON.stringify(payload),
      });

      if (res && res.ok) {
        if (typeof showToast === "function") {
          showToast(tx("wizard_setup_complete", "Setup completed! Opening AlgoPaca…"), "success");
        }
        setTimeout(() => {
          window.location.href = `/${wizardData.defaultPage || "auto-trade"}`;
        }, 800);
      } else {
        throw new Error(res?.detail || res?.error || tx("wizard_save_err", "Failed to complete setup."));
      }
    } catch (err) {
      if (typeof showToast === "function") {
        showToast(err.message || String(err), "error");
      }
      if (btnNext) {
        btnNext.disabled = false;
        btnNext.innerHTML = `<span id="btn-next-label">${tx("wizard_btn_finish", "Complete Setup & Open Desk")}</span>`;
      }
    }
  }

  function initWizardEvents() {
    // Stepper Button Clicks
    stepperTrack?.addEventListener("click", (e) => {
      const btn = e.target.closest(".stepper-step-btn");
      if (!btn) return;
      const targetStep = parseInt(btn.getAttribute("data-step-target") || "1", 10);
      if (targetStep < currentStep) {
        navigateToStep(targetStep);
      } else if (targetStep > currentStep) {
        if (validateCurrentStep()) {
          navigateToStep(targetStep);
        }
      }
    });

    // Jump buttons from Summary
    document.querySelectorAll(".btn-summary-jump").forEach((btn) => {
      btn.addEventListener("click", () => {
        const targetStep = parseInt(btn.getAttribute("data-step-target") || "1", 10);
        navigateToStep(targetStep);
      });
    });

    // Back Button
    btnPrev?.addEventListener("click", () => {
      if (currentStep > 1) {
        navigateToStep(currentStep - 1);
      }
    });

    // Skip Button (on Step 2 SMTP)
    btnSkip?.addEventListener("click", () => {
      if (currentStep === 2) {
        navigateToStep(3);
      }
    });

    // Next / Complete Button
    btnNext?.addEventListener("click", () => {
      if (!validateCurrentStep()) return;

      if (currentStep < TOTAL_STEPS) {
        navigateToStep(currentStep + 1);
      } else {
        completeSetupWizard();
      }
    });

    // Step 1: Password strength & match listeners
    inputPassword?.addEventListener("input", updatePasswordMeter);
    inputConfirmPassword?.addEventListener("input", updatePasswordMatch);
    setupPasswordToggle(btnToggleOwnerPw, inputPassword);
    setupPasswordToggle(btnToggleConfirmPw, inputConfirmPassword);
    setupPasswordToggle(btnToggleSmtpPw, inputSmtpPass);

    // Step 2: SMTP Test button & Port / SSL synchronization
    btnTestSmtp?.addEventListener("click", testSmtpConnection);

    selectSmtpPort?.addEventListener("change", () => {
      const port = parseInt(selectSmtpPort.value || "587", 10);
      if (port === 465) {
        if (checkSmtpSsl) checkSmtpSsl.checked = true;
      } else if (port === 587 || port === 25 || port === 2525) {
        if (checkSmtpSsl) checkSmtpSsl.checked = false;
      }
    });

    checkSmtpSsl?.addEventListener("change", () => {
      if (checkSmtpSsl.checked) {
        if (selectSmtpPort && selectSmtpPort.value !== "465") {
          selectSmtpPort.value = "465";
          if (typeof refreshNiceSelects === "function") refreshNiceSelects();
        }
      } else {
        if (selectSmtpPort && selectSmtpPort.value === "465") {
          selectSmtpPort.value = "587";
          if (typeof refreshNiceSelects === "function") refreshNiceSelects();
        }
      }
    });

    // Step 3: Theme radio change
    document.querySelectorAll('input[name="wizard_theme"]').forEach((radio) => {
      radio.addEventListener("change", () => {
        highlightSelectedCard(radio);
        wizardData.theme = radio.value;
        document.documentElement.setAttribute("data-theme", radio.value);
      });
    });

    // Language select change
    selectLang?.addEventListener("change", async (e) => {
      const newLang = e.target.value;
      wizardData.lang = newLang;
      if (window.i18n && typeof window.i18n.setLanguage === "function") {
        await window.i18n.setLanguage(newLang);
      }
    });

    // Enter key submits current step if inside input
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.isComposing) return;
      const el = e.target;
      if (!(el instanceof HTMLInputElement)) return;
      e.preventDefault();
      btnNext?.click();
    });

    // NiceSelect initialization
    if (typeof initNiceSelects === "function") {
      initNiceSelects();
    }
  }

  // Bootstrap On DOM Ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      initWizardEvents();
      loadExistingState();
    });
  } else {
    initWizardEvents();
    loadExistingState();
  }
})();
