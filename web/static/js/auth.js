/**
 * Authentication JavaScript for AlgoPaca (Login & Sign Up)
 * Handles client-side validation, i18n localization, session flow, and security checks.
 */

(function () {
  const $ = (id) => document.getElementById(id);

  const USERNAME_REGEX = /^[a-zA-Z0-9_.-]{3,30}$/;
  const EMAIL_REGEX = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;
  const MIN_PASSWORD_LENGTH = 8;
  const MAX_PASSWORD_LENGTH = 128;

  const FIELD_ERROR_IDS = {
    "field-identifier": "err-identifier",
    "field-password": "err-password",
    "field-username": "err-username",
    "field-email": "err-email",
    "field-confirm-password": "err-confirm",
  };

  // The API always answers in English. Map the messages it can return onto
  // translation keys so a visitor reading the desk in another language does not
  // get an English banner back.
  const SERVER_MESSAGE_KEYS = [
    { test: /invalid username\/email or password/i, key: "auth_err_generic_login", fallback: "Invalid username/email or password." },
    { test: /username or email is required/i, key: "auth_err_missing_identifier", fallback: "Username or email is required." },
    { test: /password is required/i, key: "auth_err_enter_password", fallback: "Enter your password." },
    { test: /username is required/i, key: "auth_err_choose_username", fallback: "Choose a username." },
    { test: /username must be 3-30 characters/i, key: "auth_err_username_format", fallback: "Use 3–30 characters: letters, numbers, underscores, dots, or hyphens." },
    { test: /email address is required/i, key: "auth_err_enter_email", fallback: "Enter your email address." },
    { test: /provide a valid email address/i, key: "auth_err_invalid_email", fallback: "Enter a valid email address." },
    { test: /password must be at least \d+ characters/i, key: "auth_err_pw_min_length", fallback: "Password must be at least 8 characters." },
    { test: /password (?:must be|cannot exceed) \d+ characters/i, key: "auth_err_pw_too_long", fallback: "Password must be 128 characters or fewer." },
    { test: /reset link has expired/i, key: "auth_err_reset_expired", fallback: "This reset link has expired. Please request a new one." },
    { test: /reset link has already been used/i, key: "auth_err_reset_used", fallback: "This reset link has already been used. Please request a new one." },
    { test: /invalid (?:or missing )?reset token/i, key: "auth_err_invalid_reset_token", fallback: "Invalid or missing password reset link. Please request a new one." },
    { test: /registration failed|login failed|failed to reset password/i, key: "auth_err_server", fallback: "The desk could not complete that request. Please try again." },
  ];

  /** Translate a raw API `detail` when it is one we recognise. */
  function localizeServerMessage(rawMessage) {
    if (!rawMessage) return null;
    for (const rule of SERVER_MESSAGE_KEYS) {
      if (rule.test.test(rawMessage)) {
        return { text: t(rule.key, rule.fallback), key: rule.key, fallback: rule.fallback };
      }
    }
    return null;
  }

  /** Show a server rejection in the banner, translated when possible. */
  function showServerAlert(rawMessage, fallbackKey, fallbackText) {
    const localized = localizeServerMessage(rawMessage);
    if (localized) {
      showAlertKey(localized.key, localized.fallback);
      return;
    }
    showAlert(rawMessage || t(fallbackKey, fallbackText));
  }

  // Server-side failures that belong on a specific field rather than in the
  // page-level banner. Matched against the English `detail` the API returns.
  const SERVER_FIELD_ERRORS = [
    {
      test: /username already exists|username.*already in use/i,
      inputId: "field-username",
      key: "auth_err_username_exists",
      fallback: "That username is taken. Try another one.",
    },
    {
      test: /email address already exists|email.*already in use/i,
      inputId: "field-email",
      key: "auth_err_email_exists",
      fallback: "An account already uses this email. Sign in instead.",
    },
  ];

  let formBusy = false;
  // Remembered so the banner can be re-rendered when the language changes.
  let lastAlert = null;

  function t(key, fallback = "", params = {}) {
    if (window.i18n && typeof window.i18n.t === "function") {
      return window.i18n.t(key, fallback, params);
    }
    let text = fallback || key;
    if (typeof text === "string" && params) {
      Object.keys(params).forEach((paramKey) => {
        text = text.replace(new RegExp(`\\{${paramKey}\\}`, "g"), params[paramKey]);
      });
    }
    return text;
  }

  function getQueryParam(name) {
    const params = new URLSearchParams(window.location.search);
    return params.get(name);
  }

  function getRedirectUrl() {
    const next = getQueryParam("next");
    if (
      next &&
      next.startsWith("/") &&
      !next.startsWith("//") &&
      !next.includes("\\") &&
      !next.includes(":") &&
      // Bouncing back to an auth page would just re-run this redirect.
      !/^\/(login|signup)(\/|\?|$)/.test(next)
    ) {
      return next;
    }
    return "/auto-trade";
  }

  function syncSwitchLinks() {
    const next = getQueryParam("next");
    if (!next) return;
    const encoded = encodeURIComponent(next);
    const toSignup = $("link-to-signup");
    if (toSignup) {
      toSignup.href = `/signup?next=${encoded}`;
    }
    const toLogin = $("link-to-login");
    if (toLogin) {
      toLogin.href = `/login?next=${encoded}`;
    }
  }

  /**
   * Show the page-level banner.
   * `source` optionally carries the i18n key + params so the banner can be
   * re-rendered in place when the visitor switches language.
   */
  function showAlert(message, type = "error", source = null) {
    const alertEl = $("auth-alert");
    const msgEl = $("auth-alert-msg");
    if (!alertEl || !msgEl) return;

    lastAlert = { type, message, source };
    msgEl.textContent = message;
    alertEl.className = `auth-alert is-${type}`;
    alertEl.hidden = false;

    // On a long form (signup on mobile) the banner is often out of view by the
    // time the user submits, so bring it back on screen.
    if (typeof alertEl.scrollIntoView === "function") {
      alertEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  function showAlertKey(key, fallback, type = "error", params = {}) {
    showAlert(t(key, fallback, params), type, { key, fallback, params });
  }

  function clearAlert() {
    const alertEl = $("auth-alert");
    if (alertEl) {
      alertEl.hidden = true;
    }
    lastAlert = null;
  }

  function refreshAlertLanguage() {
    const alertEl = $("auth-alert");
    if (!alertEl || alertEl.hidden || !lastAlert || !lastAlert.source) return;
    const { key, fallback, params } = lastAlert.source;
    const msgEl = $("auth-alert-msg");
    if (msgEl) msgEl.textContent = t(key, fallback, params);
  }

  function setFieldError(input, message) {
    if (!input) return;
    input.classList.add("has-error");
    input.classList.remove("has-success");
    input.setAttribute("aria-invalid", "true");
    const errorId = FIELD_ERROR_IDS[input.id];
    const errorEl = errorId ? $(errorId) : null;
    if (errorEl) {
      errorEl.textContent = message || "";
      errorEl.hidden = !message;
    }
  }

  function clearFieldError(input) {
    if (!input) return;
    input.classList.remove("has-error");
    input.removeAttribute("aria-invalid");
    const errorId = FIELD_ERROR_IDS[input.id];
    const errorEl = errorId ? $(errorId) : null;
    if (errorEl) {
      errorEl.textContent = "";
      errorEl.hidden = true;
    }
  }

  function clearAllFieldErrors() {
    document.querySelectorAll(".auth-input").forEach(clearFieldError);
  }

  function focusFirstError() {
    const first = document.querySelector(".auth-input.has-error");
    if (!first) return;
    first.focus();
    if (typeof first.scrollIntoView === "function") {
      first.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  async function readApiPayload(response) {
    const text = await response.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch {
      return {};
    }
  }

  function apiErrorMessage(data, fallback) {
    const detail = data && data.detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (Array.isArray(detail) && detail.length) {
      const first = detail[0];
      if (typeof first === "string") return first;
      if (first && (first.msg || first.message)) return first.msg || first.message;
    }
    return fallback;
  }

  /**
   * Attach a server rejection to the field that caused it, so "username taken"
   * lands next to the username box instead of only in the page-level banner.
   * Returns true when the message was handled at field level.
   */
  function routeServerError(rawMessage) {
    if (!rawMessage) return false;
    for (const rule of SERVER_FIELD_ERRORS) {
      if (!rule.test.test(rawMessage)) continue;
      const input = $(rule.inputId);
      if (!input) continue;
      setFieldError(input, t(rule.key, rule.fallback));
      input.focus();
      input.select?.();
      return true;
    }
    return false;
  }

  function setupInputClearListeners() {
    document.querySelectorAll(".auth-input").forEach((input) => {
      input.addEventListener("input", () => {
        clearFieldError(input);
        if (!$("auth-alert")?.classList.contains("is-info")) {
          clearAlert();
        }
      });
    });
  }

  function updatePasswordToggle(btn, visible) {
    const eyeOpen = btn.querySelector(".icon-eye-open");
    const eyeClosed = btn.querySelector(".icon-eye-closed");
    if (eyeOpen) {
      if (visible) {
        eyeOpen.setAttribute("hidden", "");
        eyeOpen.style.display = "none";
      } else {
        eyeOpen.removeAttribute("hidden");
        eyeOpen.style.display = "";
      }
    }
    if (eyeClosed) {
      if (visible) {
        eyeClosed.removeAttribute("hidden");
        eyeClosed.style.display = "";
      } else {
        eyeClosed.setAttribute("hidden", "");
        eyeClosed.style.display = "none";
      }
    }
    btn.classList.toggle("is-active", visible);
    btn.setAttribute("aria-pressed", visible ? "true" : "false");
    btn.setAttribute(
      "aria-label",
      visible ? t("auth_hide_pw", "Hide password") : t("auth_show_pw", "Show password")
    );
    btn.setAttribute(
      "data-i18n-aria-label",
      visible ? "auth_hide_pw" : "auth_show_pw"
    );
  }

  function setupPasswordToggles() {
    document.querySelectorAll(".pw-toggle-btn").forEach((btn) => {
      const targetId = btn.getAttribute("data-target");
      const input = $(targetId);
      if (input) {
        updatePasswordToggle(btn, input.type === "text");
      }

      btn.addEventListener("click", () => {
        const targetId = btn.getAttribute("data-target");
        const input = $(targetId);
        if (!input) return;

        const show = input.type === "password";
        input.type = show ? "text" : "password";
        updatePasswordToggle(btn, show);
      });
    });
  }

  function refreshPasswordToggleLabels() {
    document.querySelectorAll(".pw-toggle-btn").forEach((btn) => {
      const targetId = btn.getAttribute("data-target");
      const input = $(targetId);
      updatePasswordToggle(btn, input ? input.type === "text" : false);
    });
  }

  function setupCapsLockWarning() {
    // Each password field owns its own hint, so the warning appears beside the
    // box being typed into rather than always under the first one.
    const pairs = [
      ["field-password", "caps-lock-hint"],
      ["field-confirm-password", "caps-lock-hint-confirm"],
    ];

    pairs.forEach(([inputId, hintId]) => {
      const input = $(inputId);
      const hint = $(hintId);
      if (!input || !hint) return;

      const setCaps = (on) => {
        hint.hidden = !on;
      };
      const syncFromEvent = (evt) => {
        if (typeof evt.getModifierState === "function") {
          setCaps(evt.getModifierState("CapsLock"));
        }
      };

      input.addEventListener("keydown", syncFromEvent);
      input.addEventListener("keyup", syncFromEvent);
      input.addEventListener("blur", () => setCaps(false));
    });
  }

  function calculatePasswordStrength(password) {
    let score = 0;
    const checks = {
      length: password.length >= MIN_PASSWORD_LENGTH,
      lower: /[a-z]/.test(password),
      upper: /[A-Z]/.test(password),
      number: /[0-9]/.test(password) || /[^a-zA-Z0-9]/.test(password),
    };

    if (checks.length) score++;
    if (checks.lower && checks.upper) score++;
    if (checks.number) score++;
    if (password.length >= 12 && checks.lower && checks.upper && checks.number) score++;

    // Length is the single strongest factor: a long passphrase should not be
    // rated "Weak" merely because it skips character classes.
    if (password.length >= 16) score = Math.max(score, 3);
    if (password.length >= 24) score = 4;

    return { score: Math.min(score, 4), checks };
  }

  function getStrengthLabel(score) {
    switch (score) {
      case 0:
      case 1:
        return t("auth_strength_weak", "Weak");
      case 2:
        return t("auth_strength_fair", "Fair");
      case 3:
        return t("auth_strength_good", "Good");
      case 4:
        return t("auth_strength_strong", "Strong");
      default:
        return t("auth_strength_weak", "Weak");
    }
  }

  let strengthAnnounceTimer = null;

  /**
   * The meter itself is aria-hidden and repaints on every keystroke; screen
   * readers get a single debounced summary instead of the whole rule list.
   */
  function announceStrength(label) {
    const liveEl = $("strength-live");
    if (!liveEl) return;
    window.clearTimeout(strengthAnnounceTimer);
    strengthAnnounceTimer = window.setTimeout(() => {
      liveEl.textContent = label
        ? t("auth_pw_strength_status", "Password strength: {label}", { label })
        : "";
    }, 600);
  }

  function updateStrengthMeter(password) {
    const meterEl = $("strength-meter");
    const scoreLabel = $("strength-score-label");
    if (!meterEl || !scoreLabel) return;

    meterEl.hidden = !password;

    if (!password) {
      meterEl.setAttribute("data-score", "0");
      scoreLabel.setAttribute("data-score", "0");
      scoreLabel.textContent = "—";
      ["rule-len", "rule-upper", "rule-lower", "rule-num"].forEach((id) => {
        const el = $(id);
        if (el) el.classList.remove("is-valid");
      });
      announceStrength("");
      return;
    }

    const { score, checks } = calculatePasswordStrength(password);
    const label = getStrengthLabel(score);
    meterEl.setAttribute("data-score", score.toString());
    scoreLabel.setAttribute("data-score", score.toString());
    scoreLabel.textContent = label;
    announceStrength(label);

    const ruleMap = {
      "rule-len": checks.length,
      "rule-upper": checks.upper,
      "rule-lower": checks.lower,
      "rule-num": checks.number,
    };

    Object.entries(ruleMap).forEach(([id, isValid]) => {
      const el = $(id);
      if (el) {
        el.classList.toggle("is-valid", isValid);
      }
    });
  }

  function setBusy(submitBtn, submitText, submitSpinner, idleKey, idleFallback, busyKey, busyFallback) {
    formBusy = true;
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.setAttribute("aria-busy", "true");
    }
    if (submitText) submitText.textContent = t(busyKey, busyFallback);
    if (submitSpinner) submitSpinner.hidden = false;
    return function restore() {
      formBusy = false;
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.removeAttribute("aria-busy");
      }
      if (submitText) submitText.textContent = t(idleKey, idleFallback);
      if (submitSpinner) submitSpinner.hidden = true;
    };
  }

  function setupForgotPassword() {
    const btn = $("btn-forgot");
    const modal = $("modal-forgot-pw");
    const closeBtn = $("btn-close-forgot-modal");
    const cancelBtn = $("btn-forgot-cancel");
    const backdrop = $("modal-forgot-backdrop");
    const form = $("forgot-form");
    const idInput = $("field-forgot-identifier");
    const submitBtn = $("btn-forgot-submit");
    const submitSpinner = $("forgot-submit-spinner");
    const submitText = $("forgot-submit-text");
    const alertBox = $("forgot-alert");
    const alertMsg = $("forgot-alert-msg");
    const errField = $("err-forgot-identifier");

    if (!btn || !modal) return;

    const dialog = modal.querySelector(".auth-modal-dialog");
    const FOCUSABLE =
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    // Where focus came from, so closing the dialog puts it back instead of
    // dumping the keyboard user at the top of the document.
    let lastFocused = null;

    function focusableInDialog() {
      if (!dialog) return [];
      return Array.from(dialog.querySelectorAll(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement
      );
    }

    function openModal() {
      lastFocused = document.activeElement;
      modal.hidden = false;
      // The page behind a full-screen dialog must not scroll with it.
      document.body.style.overflow = "hidden";
      if (alertBox) alertBox.hidden = true;
      if (errField) errField.hidden = true;
      if (idInput) {
        idInput.value = $("field-identifier")?.value || "";
        setTimeout(() => idInput.focus(), 50);
      }
    }

    function closeModal() {
      if (modal.hidden) return;
      modal.hidden = true;
      document.body.style.overflow = "";
      if (lastFocused && typeof lastFocused.focus === "function") {
        lastFocused.focus();
      }
      lastFocused = null;
    }

    btn.addEventListener("click", openModal);
    closeBtn?.addEventListener("click", closeModal);
    cancelBtn?.addEventListener("click", closeModal);
    backdrop?.addEventListener("click", closeModal);

    document.addEventListener("keydown", (e) => {
      if (modal.hidden) return;

      if (e.key === "Escape") {
        closeModal();
        return;
      }

      // Keep Tab inside the dialog: aria-modal alone does not stop a keyboard
      // user from tabbing into the sign-in form behind the backdrop.
      if (e.key !== "Tab") return;
      const items = focusableInDialog();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      } else if (!dialog.contains(document.activeElement)) {
        e.preventDefault();
        first.focus();
      }
    });

    form?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const identifier = (idInput?.value || "").trim();
      if (!identifier) {
        if (errField) {
          errField.textContent = t("auth_err_missing_identifier", "Username or email is required.");
          errField.hidden = false;
        }
        return;
      }
      if (errField) errField.hidden = true;

      submitBtn.disabled = true;
      if (submitSpinner) submitSpinner.hidden = false;
      if (submitText) submitText.textContent = t("auth_sending", "Sending…");

      try {
        const response = await fetch("/api/auth/forgot-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ identifier }),
        });
        const data = await readApiPayload(response);
        if (!response.ok) {
          throw new Error(apiErrorMessage(data, t("auth_err_send_reset", "Failed to send reset link.")));
        }

        if (alertBox && alertMsg) {
          alertBox.className = "auth-alert is-success";
          alertMsg.textContent = t(
            "auth_reset_sent_msg",
            "If an account matches that username or email, a password reset link has been sent. Please check your inbox."
          );
          alertBox.hidden = false;
        }
        form.reset();
      } catch (err) {
        if (alertBox && alertMsg) {
          const localized = localizeServerMessage(err && err.message);
          alertBox.className = "auth-alert is-error";
          alertMsg.textContent =
            (localized && localized.text) ||
            (err && err.name === "TypeError"
              ? t("auth_network_error", "Network error. Check your connection and try again.")
              : (err && err.message) || t("auth_err_send_reset", "Failed to send reset link."));
          alertBox.hidden = false;
        }
      } finally {
        submitBtn.disabled = false;
        if (submitSpinner) submitSpinner.hidden = true;
        if (submitText) submitText.textContent = t("auth_send_reset_link", "Send Reset Link");
      }
    });
  }

  function setupResetPasswordForm() {
    const form = $("reset-password-form");
    if (!form) return;

    const tokenInput = $("field-reset-token");
    const passwordInput = $("field-password");
    const confirmInput = $("field-confirm-password");
    const submitBtn = $("reset-submit-btn");
    const submitSpinner = $("reset-submit-spinner");
    const submitText = $("reset-submit-text");

    const confirmHint = $("confirm-hint");
    const params = new URLSearchParams(window.location.search);
    const token = params.get("token") || "";
    if (tokenInput) tokenInput.value = token;

    if (!token) {
      showAlertKey("auth_err_invalid_reset_token", "Invalid or missing password reset link. Please request a new one.");
      if (submitBtn) submitBtn.disabled = true;
    }

    /** Live match feedback, mirroring the sign-up form. */
    function validateResetMatch() {
      if (!confirmInput || !passwordInput) return true;
      const p2 = confirmInput.value;
      if (!p2) {
        confirmInput.classList.remove("has-error", "has-success");
        confirmInput.removeAttribute("aria-invalid");
        if (confirmHint) {
          confirmHint.hidden = true;
          confirmHint.textContent = "";
          confirmHint.classList.remove("is-success", "is-error");
        }
        return true;
      }
      const matches = passwordInput.value === p2;
      confirmInput.classList.toggle("has-error", !matches);
      confirmInput.classList.toggle("has-success", matches);
      if (matches) {
        confirmInput.removeAttribute("aria-invalid");
      } else {
        confirmInput.setAttribute("aria-invalid", "true");
      }
      if (confirmHint) {
        confirmHint.hidden = false;
        confirmHint.classList.toggle("is-success", matches);
        confirmHint.classList.toggle("is-error", !matches);
        confirmHint.textContent = matches
          ? t("auth_pw_match", "Passwords match")
          : t("auth_pw_mismatch_inline", "Passwords do not match");
      }
      return matches;
    }

    // The meter markup exists on this page too, but nothing was driving it.
    passwordInput?.addEventListener("input", () => {
      updateStrengthMeter(passwordInput.value);
      validateResetMatch();
    });
    confirmInput?.addEventListener("input", validateResetMatch);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!token) {
        showAlertKey("auth_err_invalid_reset_token", "Invalid or missing password reset link. Please request a new one.");
        return;
      }

      const password = passwordInput?.value || "";
      const confirm = confirmInput?.value || "";

      clearAlert();
      clearAllFieldErrors();

      // These take the input element, not a field name — passing a string used
      // to throw and silently abort the submit.
      if (password.length < MIN_PASSWORD_LENGTH) {
        setFieldError(passwordInput, t("auth_err_pw_min_length", "Password must be at least 8 characters."));
        focusFirstError();
        return;
      }
      if (password.length > MAX_PASSWORD_LENGTH) {
        setFieldError(passwordInput, t("auth_err_pw_too_long", "Password must be 128 characters or fewer."));
        focusFirstError();
        return;
      }
      if (password !== confirm) {
        setFieldError(confirmInput, t("auth_err_pw_mismatch", "Passwords do not match."));
        validateResetMatch();
        focusFirstError();
        return;
      }

      submitBtn.disabled = true;
      if (submitSpinner) submitSpinner.hidden = false;
      if (submitText) submitText.textContent = t("auth_updating_pw", "Updating password…");

      try {
        const response = await fetch("/api/auth/reset-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token, password }),
        });
        const data = await readApiPayload(response);
        if (!response.ok || !data.ok) {
          throw new Error(apiErrorMessage(data, t("auth_err_reset_failed", "Password reset failed.")));
        }

        showAlertKey("auth_reset_success", "Password updated successfully! Redirecting to sign in…", "success");
        setTimeout(() => {
          window.location.href = "/login?reset=success";
        }, 1200);
      } catch (err) {
        if (err && err.name === "TypeError") {
          showAlertKey("auth_network_error", "Network error. Check your connection and try again.");
        } else {
          showServerAlert(err && err.message, "auth_err_reset_failed", "Password reset failed.");
        }
        submitBtn.disabled = false;
        if (submitSpinner) submitSpinner.hidden = true;
        if (submitText) submitText.textContent = t("auth_reset_submit_btn", "Update Password");
      }
    });
  }

  function setupLoginForm() {
    const form = $("login-form");
    if (!form) return;

    const identifierInput = $("field-identifier");
    const passwordInput = $("field-password");
    const submitBtn = $("login-submit-btn");
    const submitText = $("login-submit-text");
    const submitSpinner = $("login-submit-spinner");

    function validateLoginFields(showErrors) {
      const identifier = (identifierInput?.value || "").trim();
      const password = passwordInput?.value || "";
      let valid = true;

      if (!identifier) {
        if (showErrors) {
          setFieldError(identifierInput, t("auth_err_enter_identifier", "Enter your username or email."));
        }
        valid = false;
      }
      if (!password) {
        if (showErrors) {
          setFieldError(passwordInput, t("auth_err_enter_password", "Enter your password."));
        }
        valid = false;
      }
      return valid;
    }

    async function handleLogin(identifier, password, rememberMe = false) {
      clearAlert();
      clearAllFieldErrors();
      if (!validateLoginFields(true)) {
        focusFirstError();
        return;
      }

      const restore = setBusy(
        submitBtn,
        submitText,
        submitSpinner,
        "auth_sign_in_btn",
        "Sign In",
        "auth_signing_in",
        "Signing in…"
      );

      try {
        const response = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            identifier: identifier.trim(),
            password,
            remember_me: rememberMe,
          }),
        });

        const data = await readApiPayload(response);
        if (!response.ok || !data.ok) {
          if (response.status === 429) {
            const seconds = Number(response.headers.get("Retry-After")) || 60;
            throw Object.assign(
              new Error(t("auth_err_too_many", "Too many sign-in attempts. Try again in {seconds}s.", { seconds })),
              { i18n: { key: "auth_err_too_many", fallback: "Too many sign-in attempts. Try again in {seconds}s.", params: { seconds } } }
            );
          }
          throw new Error(
            apiErrorMessage(data, t("auth_err_generic_login", "Invalid username/email or password."))
          );
        }

        showAlertKey("auth_success_login", "Signed in. Opening the trading desk…", "success");
        setTimeout(() => {
          window.location.href = getRedirectUrl();
        }, 350);
      } catch (err) {
        if (err && err.name === "TypeError") {
          showAlertKey("auth_network_error", "Network error. Check your connection and try again.");
        } else if (err && err.i18n) {
          showAlertKey(err.i18n.key, err.i18n.fallback, "error", err.i18n.params);
        } else {
          showServerAlert(err && err.message, "auth_err_generic_login", "Invalid username/email or password.");
        }
        restore();
        passwordInput?.focus();
        passwordInput?.select();
      }
    }

    form.addEventListener("submit", (evt) => {
      evt.preventDefault();
      if (formBusy) return;
      const identifier = identifierInput?.value || "";
      const password = passwordInput?.value || "";
      const rememberMe = $("field-remember")?.checked || false;
      handleLogin(identifier, password, rememberMe);
    });
  }

  function setupSignupForm() {
    const form = $("signup-form");
    if (!form) return;

    const usernameInput = $("field-username");
    const emailInput = $("field-email");
    const passwordInput = $("field-password");
    const confirmInput = $("field-confirm-password");
    const submitBtn = $("signup-submit-btn");
    const submitText = $("signup-submit-text");
    const submitSpinner = $("signup-submit-spinner");
    const confirmHint = $("confirm-hint");

    function validatePasswordMatch(showHint) {
      if (!confirmInput || !passwordInput) return true;
      const p1 = passwordInput.value;
      const p2 = confirmInput.value;
      if (!p2) {
        confirmInput.classList.remove("has-error", "has-success");
        if (confirmHint) {
          confirmHint.hidden = true;
          confirmHint.textContent = "";
          confirmHint.classList.remove("is-success", "is-error");
        }
        return true;
      }
      const matches = p1 === p2;
      confirmInput.classList.toggle("has-error", !matches);
      confirmInput.classList.toggle("has-success", matches);
      // Note: toggleAttribute would set aria-invalid="", which ARIA reads as
      // "false" — the value has to be spelled out.
      if (matches) {
        confirmInput.removeAttribute("aria-invalid");
      } else {
        confirmInput.setAttribute("aria-invalid", "true");
      }
      if (confirmHint && showHint) {
        confirmHint.hidden = false;
        confirmHint.classList.toggle("is-success", matches);
        confirmHint.classList.toggle("is-error", !matches);
        confirmHint.textContent = matches
          ? t("auth_pw_match", "Passwords match")
          : t("auth_pw_mismatch_inline", "Passwords do not match");
      }
      return matches;
    }

    function validateUsername(showErrors) {
      const username = (usernameInput?.value || "").trim();
      if (!username) {
        if (showErrors) setFieldError(usernameInput, t("auth_err_choose_username", "Choose a username."));
        return false;
      }
      if (!USERNAME_REGEX.test(username)) {
        if (showErrors) {
          setFieldError(
            usernameInput,
            t("auth_err_username_format", "Use 3–30 characters: letters, numbers, underscores, dots, or hyphens.")
          );
        }
        return false;
      }
      clearFieldError(usernameInput);
      return true;
    }

    function validateEmail(showErrors) {
      const email = (emailInput?.value || "").trim().toLowerCase();
      if (!email) {
        if (showErrors) setFieldError(emailInput, t("auth_err_enter_email", "Enter your email address."));
        return false;
      }
      if (!EMAIL_REGEX.test(email)) {
        if (showErrors) setFieldError(emailInput, t("auth_err_invalid_email", "Enter a valid email address."));
        return false;
      }
      clearFieldError(emailInput);
      return true;
    }

    if (passwordInput) {
      passwordInput.addEventListener("input", () => {
        updateStrengthMeter(passwordInput.value);
        validatePasswordMatch(true);
      });
    }

    if (confirmInput) {
      confirmInput.addEventListener("input", () => validatePasswordMatch(true));
    }

    usernameInput?.addEventListener("blur", () => {
      if (usernameInput.value.trim()) validateUsername(true);
    });
    emailInput?.addEventListener("blur", () => {
      if (emailInput.value.trim()) validateEmail(true);
    });
    confirmInput?.addEventListener("blur", () => {
      if (confirmInput.value) validatePasswordMatch(true);
    });

    form.addEventListener("submit", async (evt) => {
      evt.preventDefault();
      if (formBusy) return;
      clearAlert();
      clearAllFieldErrors();

      const username = (usernameInput?.value || "").trim();
      const email = (emailInput?.value || "").trim().toLowerCase();
      const password = passwordInput?.value || "";
      const confirm = confirmInput?.value || "";
      let valid = true;

      if (!validateUsername(true)) valid = false;
      if (!validateEmail(true)) valid = false;

      if (!password) {
        setFieldError(passwordInput, t("auth_err_create_password", "Create a password."));
        valid = false;
      } else if (password.length < MIN_PASSWORD_LENGTH) {
        setFieldError(passwordInput, t("auth_err_pw_min_length", "Password must be at least 8 characters."));
        valid = false;
      } else if (password.length > MAX_PASSWORD_LENGTH) {
        setFieldError(passwordInput, t("auth_err_pw_too_long", "Password must be 128 characters or fewer."));
        valid = false;
      }

      if (!confirm) {
        setFieldError(confirmInput, t("auth_err_pw_mismatch", "Confirm your password."));
        valid = false;
      } else if (password !== confirm) {
        setFieldError(confirmInput, t("auth_err_pw_mismatch", "Passwords do not match."));
        validatePasswordMatch(true);
        valid = false;
      }

      if (!valid) {
        focusFirstError();
        return;
      }

      const restore = setBusy(
        submitBtn,
        submitText,
        submitSpinner,
        "auth_create_account_btn",
        "Create Account",
        "auth_creating_account",
        "Creating account…"
      );

      try {
        const response = await fetch("/api/auth/signup", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username,
            email,
            password,
            display_name: username,
          }),
        });

        const data = await readApiPayload(response);
        if (!response.ok || !data.ok) {
          throw new Error(apiErrorMessage(data, t("auth_err_generic_signup", "Account creation failed.")));
        }

        showAlertKey("auth_success_signup", "Account created. Opening the desk…", "success");
        setTimeout(() => {
          window.location.href = getRedirectUrl();
        }, 400);
      } catch (err) {
        restore();
        if (err && err.name === "TypeError") {
          showAlertKey("auth_network_error", "Network error. Check your connection and try again.");
          return;
        }
        // "Username taken" / "email in use" belong on the field, not only in
        // the banner — otherwise the user has to guess which one to change.
        if (routeServerError(err && err.message)) return;
        showServerAlert(
          err && err.message,
          "auth_err_generic_signup",
          "Account creation failed. Check your details and try again."
        );
      }
    });
  }

  /**
   * Point the "not you?" escape hatch at the sign-in form with force=1 (which
   * suppresses the auto-redirect), keeping any `next` target intact.
   */
  function prepareSwitchAccountLink() {
    const link = $("link-switch-account");
    if (!link) return;
    const params = new URLSearchParams();
    params.set("force", "1");
    const next = getQueryParam("next");
    if (next) params.set("next", next);
    link.href = `/login?${params.toString()}`;
  }

  async function checkActiveSession() {
    // Someone following a reset link may well still have a live session — that
    // is exactly the "I forgot my password on my other device" case. Bouncing
    // them to the desk would make the link impossible to use.
    if (document.body.dataset.page === "reset-password") return;

    const force = getQueryParam("force");
    // force=1 means "let me sign in as somebody else" — skip the auto-redirect
    // but leave the form fully usable.
    if (force === "1" || force === "true") return;

    try {
      const res = await fetch("/api/auth/me");
      if (!res.ok) return;
      const data = await readApiPayload(res);
      if (data.ok && data.authenticated && data.user) {
        const name = data.user.display_name || data.user.username || "Trader";
        showAlertKey(
          "auth_already_logged_in",
          "Already signed in as {name}. Opening the desk…",
          "success",
          { name }
        );

        // Without this, a signed-in visitor is bounced away from /login with no
        // way to reach the form and switch accounts.
        prepareSwitchAccountLink();
        const escapeEl = $("auth-switch-account");
        const escapeLink = $("link-switch-account");
        if (escapeEl) escapeEl.hidden = false;

        formBusy = true;
        document.querySelectorAll(".auth-form button, .auth-form input, .auth-form select").forEach((el) => {
          el.disabled = true;
        });

        const redirectTimer = window.setTimeout(() => {
          window.location.href = getRedirectUrl();
        }, 1400);

        // Reaching for the escape hatch cancels the pending redirect, so the
        // link is actually clickable instead of vanishing mid-gesture.
        ["pointerenter", "focus", "click"].forEach((evtName) => {
          escapeLink?.addEventListener(evtName, () => window.clearTimeout(redirectTimer));
        });
      }
    } catch (e) {
      // Ignore network failures on initial check
    }
  }

  function maybeAutofocus() {
    const first = document.querySelector(".auth-form .auth-input");
    if (!first) return;
    const coarse = window.matchMedia("(pointer: coarse)").matches;
    if (!coarse) first.focus();
  }

  function initAuthNiceSelects() {
    if (typeof NiceSelect === "undefined") return;
    document.querySelectorAll(".lang-select, #lang-select").forEach((el) => {
      if (el._niceSelect) return;
      NiceSelect.bind(el, { searchable: false });
      if (el._niceSelect && typeof el._niceSelect.update === "function") {
        el._niceSelect.update();
      }
      el.addEventListener("focus", () => {
        if (document.activeElement === el && el._niceSelect?.dropdown) {
          if (!el._niceSelect.dropdown.classList.contains("open")) {
            el._niceSelect.focus("focus_event");
          }
        }
      });
    });
  }

  function initEngineShowcaseTabs() {
    const tabs = Array.from(document.querySelectorAll(".engine-tab-btn"));
    if (!tabs.length) return;

    const moduleContainer = document.querySelector(".showcase-engines-module");
    const panels = Array.from(document.querySelectorAll(".showcase-engine-panel"));
    const AUTO_ROTATE_DELAY = 4500; // 4.5 seconds per slide
    let timer = null;
    let isPaused = false;
    let currentIndex = tabs.findIndex((tab) => tab.classList.contains("is-active"));
    if (currentIndex === -1) currentIndex = 0;

    // Ensure all tab buttons have a progress bar element
    tabs.forEach((tab) => {
      if (!tab.querySelector(".engine-tab-progress")) {
        const prog = document.createElement("span");
        prog.className = "engine-tab-progress";
        prog.setAttribute("aria-hidden", "true");
        tab.appendChild(prog);
      }
    });

    function resetTabProgressAnimation(tab) {
      const progress = tab.querySelector(".engine-tab-progress");
      if (progress) {
        progress.style.animation = "none";
        void progress.offsetWidth; // Force DOM reflow to restart CSS animation
        progress.style.animation = "";
      }
    }

    function switchEngineTab(targetTab, shouldScroll = false) {
      const targetEngine = targetTab.getAttribute("data-engine");
      if (!targetEngine) return;

      currentIndex = tabs.indexOf(targetTab);

      tabs.forEach((tab) => {
        const isMatch = tab === targetTab;
        tab.classList.toggle("is-active", isMatch);
        tab.setAttribute("aria-selected", isMatch ? "true" : "false");
        tab.tabIndex = isMatch ? 0 : -1;
        if (isMatch) {
          resetTabProgressAnimation(tab);
        }
      });

      panels.forEach((panel) => {
        const isMatch = panel.id === `panel-engine-${targetEngine}`;
        panel.classList.toggle("is-active", isMatch);
        panel.hidden = !isMatch;
      });

      if (shouldScroll) {
        const container = targetTab.closest ? targetTab.closest(".showcase-engine-tabs") : targetTab.parentElement;
        if (container && container.scrollWidth > container.clientWidth) {
          const containerRect = container.getBoundingClientRect();
          const tabRect = targetTab.getBoundingClientRect();

          const tabRelativeLeft = tabRect.left - containerRect.left + container.scrollLeft;
          const tabRelativeRight = tabRelativeLeft + tabRect.width;

          if (tabRelativeLeft < container.scrollLeft) {
            container.scrollTo({ left: Math.max(0, tabRelativeLeft - 12), behavior: "smooth" });
          } else if (tabRelativeRight > container.scrollLeft + container.clientWidth) {
            container.scrollTo({
              left: tabRelativeRight - container.clientWidth + 12,
              behavior: "smooth"
            });
          }
        }
      }
    }

    function nextSlide() {
      const nextIndex = (currentIndex + 1) % tabs.length;
      switchEngineTab(tabs[nextIndex], true);
    }

    function startAutoRotate() {
      stopAutoRotate();
      if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        return;
      }
      if (isPaused) return;

      timer = setInterval(() => {
        if (!isPaused && document.visibilityState === "visible") {
          nextSlide();
        }
      }, AUTO_ROTATE_DELAY);
    }

    function stopAutoRotate() {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    }

    function restartAutoRotate() {
      stopAutoRotate();
      startAutoRotate();
    }

    // Initialize tab accessibility
    tabs.forEach((tab) => {
      tab.tabIndex = tab.classList.contains("is-active") ? 0 : -1;
      if (tab.classList.contains("is-active")) {
        resetTabProgressAnimation(tab);
      }
    });

    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => {
        switchEngineTab(tab, true);
        restartAutoRotate();
      });

      tab.addEventListener("keydown", (e) => {
        let targetIndex = null;
        if (e.key === "ArrowRight") {
          targetIndex = (index + 1) % tabs.length;
        } else if (e.key === "ArrowLeft") {
          targetIndex = (index - 1 + tabs.length) % tabs.length;
        } else if (e.key === "Home") {
          targetIndex = 0;
        } else if (e.key === "End") {
          targetIndex = tabs.length - 1;
        }

        if (targetIndex !== null) {
          e.preventDefault();
          tabs[targetIndex].focus();
          switchEngineTab(tabs[targetIndex], true);
          restartAutoRotate();
        }
      });
    });

    const tabsContainer = document.querySelector(".showcase-engine-tabs");
    if (tabsContainer) {
      tabsContainer.addEventListener("wheel", (e) => {
        if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && tabsContainer.scrollWidth > tabsContainer.clientWidth) {
          e.preventDefault();
          tabsContainer.scrollLeft += e.deltaY;
        }
      }, { passive: false });
    }

    // Pause on hover / touch / focus for usability & accessibility
    if (moduleContainer) {
      moduleContainer.addEventListener("mouseenter", () => {
        isPaused = true;
        moduleContainer.classList.add("is-paused");
        stopAutoRotate();
      });

      moduleContainer.addEventListener("mouseleave", () => {
        isPaused = false;
        moduleContainer.classList.remove("is-paused");
        startAutoRotate();
      });

      moduleContainer.addEventListener("focusin", () => {
        isPaused = true;
        moduleContainer.classList.add("is-paused");
        stopAutoRotate();
      });

      moduleContainer.addEventListener("focusout", (e) => {
        if (!moduleContainer.contains(e.relatedTarget)) {
          isPaused = false;
          moduleContainer.classList.remove("is-paused");
          startAutoRotate();
        }
      });

      moduleContainer.addEventListener("touchstart", () => {
        isPaused = true;
        moduleContainer.classList.add("is-paused");
        stopAutoRotate();
      }, { passive: true });

      moduleContainer.addEventListener("touchend", () => {
        setTimeout(() => {
          isPaused = false;
          moduleContainer.classList.remove("is-paused");
          startAutoRotate();
        }, 2000);
      }, { passive: true });
    }

    // Pause when tab is inactive/hidden, resume when visible
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        if (!isPaused) {
          moduleContainer?.classList.remove("is-paused");
          startAutoRotate();
        }
      } else {
        moduleContainer?.classList.add("is-paused");
        stopAutoRotate();
      }
    });

    // Start carousel auto-rotation
    startAutoRotate();
  }

  function init() {
    initAuthNiceSelects();
    initEngineShowcaseTabs();
    syncSwitchLinks();
    prepareSwitchAccountLink();
    setupInputClearListeners();
    setupPasswordToggles();
    setupCapsLockWarning();
    setupForgotPassword();
    setupResetPasswordForm();
    setupLoginForm();
    setupSignupForm();
    checkActiveSession();
    maybeAutofocus();

    const params = new URLSearchParams(window.location.search);
    if (params.get("reset") === "success") {
      showAlertKey("auth_reset_success_login", "Password updated successfully. Please sign in with your new password.", "success");
    }

    window.addEventListener("languageChange", () => {
      const pwInput = $("field-password");
      if (pwInput && pwInput.value) {
        updateStrengthMeter(pwInput.value);
      }
      refreshAlertLanguage();
      refreshPasswordToggleLabels();
      const confirmInput = $("field-confirm-password");
      if (confirmInput && confirmInput.value && pwInput) {
        confirmInput.dispatchEvent(new Event("input"));
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
