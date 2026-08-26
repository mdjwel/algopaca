/**
 * Admin Desk Controller for AlgoPaca
 *
 * Analytics KPIs and the Chart.js growth curve, the user table with search /
 * filter / sort / pagination, the manage + invite dialogs, SMTP settings and
 * live diagnostics, the administrative activity log, and database maintenance.
 *
 * Every user-visible string goes through t() so the page reads in the language
 * the rest of the desk is set to. escapeHtml, $ and showToast come from common.js.
 */

(function () {
  "use strict";

  /* ==========================================================================
     State
     ========================================================================== */
  let activeTab = "analytics";
  let currentUser = null;
  let growthChartInstance = null;
  let selectedUser = null;
  let searchDebounceTimer = null;

  const usersQuery = {
    search: "",
    role: "",
    status: "",
    sort: "last_login_at",
    direction: "desc",
    limit: 25,
    offset: 0,
  };
  let usersTotal = 0;
  let usersRows = [];

  const auditQuery = { limit: 25, offset: 0 };
  let auditTotal = 0;

  // Focus to restore when a dialog closes, so keyboard users land back where
  // they were rather than at the top of the document.
  let focusBeforeDialog = null;

  /* ==========================================================================
     Elements
     ========================================================================== */
  const tabButtons = Array.from(document.querySelectorAll(".admin-tab-btn"));
  const tabPanels = Array.from(document.querySelectorAll(".admin-panel"));
  const mainRegion = $("main");

  const kpiTotalUsers = $("kpi-total-users");
  const kpiSignups24h = $("kpi-signups-24h");
  const kpiActiveSessions = $("kpi-active-sessions");
  const kpiOnlineUsers = $("kpi-online-users");
  const kpiPaperUsers = $("kpi-paper-users");
  const kpiLiveUsers = $("kpi-live-users");
  const kpiLivePct = $("kpi-live-pct");
  const kpiActiveLoops = $("kpi-active-loops");
  const canvasGrowth = $("canvas-growth-chart");
  const chartGrowthState = $("chart-growth-state");
  const chartGrowthStateText = $("chart-growth-state-text");
  const btnChartRetry = $("btn-chart-retry");
  const btnRefreshAnalytics = $("btn-refresh-analytics");

  const integCountPaper = $("integ-count-paper");
  const integCountLive = $("integ-count-live");
  const integCountOpenai = $("integ-count-openai");
  const integCountGemini = $("integ-count-gemini");
  const progPaper = $("prog-paper");
  const progLive = $("prog-live");
  const progOpenai = $("prog-openai");
  const progGemini = $("prog-gemini");

  const inputUsersSearch = $("input-users-search");
  const btnClearUsersSearch = $("btn-clear-users-search");
  const selectRoleFilter = $("select-role-filter");
  const selectStatusFilter = $("select-status-filter");
  const selectPageSize = $("select-page-size");
  const btnRefreshUsers = $("btn-refresh-users");
  const btnExportUsers = $("btn-export-users");
  const btnOpenInvite = $("btn-open-invite");
  const usersTbody = $("users-tbody");
  const usersBusy = $("users-busy");
  const usersTotalBadge = $("users-total-badge");
  const usersPaginationInfo = $("users-pagination-info");
  const usersPageIndicator = $("users-page-indicator");
  const btnUsersFirst = $("btn-users-first");
  const btnUsersPrev = $("btn-users-prev");
  const btnUsersNext = $("btn-users-next");
  const btnUsersLast = $("btn-users-last");
  const sortHeaders = Array.from(document.querySelectorAll(".admin-table th.is-sortable"));

  const modalUserManage = $("modal-user-manage");
  const btnModalClose = $("btn-modal-close");
  const modalUserAvatar = $("modal-user-avatar");
  const modalUserName = $("modal-user-name");
  const modalUserEmail = $("modal-user-email");
  const modalDetailLastLogin = $("modal-detail-last-login");
  const modalDetailJoined = $("modal-detail-joined");
  const modalDetailMode = $("modal-detail-mode");
  const modalDetailKeys = $("modal-detail-keys");
  const modalSessionList = $("modal-session-list");
  const modalSelectRole = $("modal-select-role");
  const btnModalSaveRole = $("btn-modal-save-role");
  const btnModalSendReset = $("btn-modal-send-reset");
  const btnModalRevokeSessions = $("btn-modal-revoke-sessions");
  const btnModalSuspend = $("btn-modal-suspend");
  const btnModalSuspendText = $("btn-modal-suspend-text");
  const btnModalDeleteUser = $("btn-modal-delete-user");
  const modalDeleteConfirm = $("modal-delete-confirm");
  const modalDeleteWarning = $("modal-delete-warning");
  const modalDeleteInput = $("modal-delete-input");
  const btnDeleteCancel = $("btn-delete-cancel");
  const btnDeleteFinal = $("btn-delete-final");
  const modalResetLinkBox = $("modal-reset-link-box");
  const modalResetLinkInput = $("modal-reset-link-input");
  const btnCopyResetLink = $("btn-copy-reset-link");
  const btnCopyResetLinkText = $("btn-copy-reset-link-text");

  const modalInvite = $("modal-invite");
  const btnInviteClose = $("btn-invite-close");
  const formInvite = $("form-invite");
  const inviteUsername = $("invite-username");
  const inviteEmail = $("invite-email");
  const inviteRole = $("invite-role");
  const inviteDisplayName = $("invite-display-name");
  const btnInviteSubmit = $("btn-invite-submit");
  const inviteLinkBox = $("invite-link-box");
  const inviteLinkInput = $("invite-link-input");
  const btnCopyInviteLink = $("btn-copy-invite-link");
  const btnCopyInviteLinkText = $("btn-copy-invite-link-text");

  const formSmtpConfig = $("form-smtp-config");
  const smtpHost = $("smtp-host");
  const smtpPort = $("smtp-port");
  const smtpUsername = $("smtp-username");
  const smtpPassword = $("smtp-password");
  const smtpPasswordHint = $("smtp-password-hint");
  const smtpFromEmail = $("smtp-from-email");
  const smtpSenderName = $("smtp-sender-name");
  const smtpUseSsl = $("smtp-use-ssl");
  const btnToggleSmtpPw = $("btn-toggle-smtp-pw");
  const smtpOrb = $("smtp-orb");
  const smtpStatusTitle = $("smtp-status-title");
  const smtpStatusDesc = $("smtp-status-desc");
  const smtpConfiguredBadge = $("smtp-configured-badge");
  const presetButtons = Array.from(document.querySelectorAll(".btn-preset"));

  const formSmtpTest = $("form-smtp-test");
  const smtpTestRecipient = $("smtp-test-recipient");
  const btnRunSmtpTest = $("btn-run-smtp-test");
  const smtpTerminalOutput = $("smtp-terminal-output");
  const btnClearDiagLogs = $("btn-clear-diag-logs");
  const btnCopyDiagLogs = $("btn-copy-diag-logs");
  const emailLogList = $("email-log-list");
  const btnRefreshEmailLog = $("btn-refresh-email-log");

  const auditList = $("audit-list");
  const auditPaginationInfo = $("audit-pagination-info");
  const btnAuditPrev = $("btn-audit-prev");
  const btnAuditNext = $("btn-audit-next");
  const btnRefreshAudit = $("btn-refresh-audit");

  const sysPythonVal = $("sys-python-val");
  const sysDbPath = $("sys-db-path");
  const sysDbSize = $("sys-db-size");
  const sysIntegrity = $("sys-integrity");
  const sysClock = $("sys-clock");
  const btnRefreshSystem = $("btn-refresh-system");
  const btnMaintPurge = $("btn-maint-purge");
  const btnMaintVacuum = $("btn-maint-vacuum");
  const maintOutputBox = $("maint-output-box");

  /* ==========================================================================
     Small helpers
     ========================================================================== */
  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  function formatDate(isoString) {
    if (!isoString) return "--";
    const d = new Date(isoString);
    if (Number.isNaN(d.getTime())) return "--";
    return d.toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }

  /** "3 days ago" style label — what an admin actually wants from a timestamp. */
  function formatRelative(isoString) {
    if (!isoString) return t("never", "Never");
    const then = new Date(isoString).getTime();
    if (Number.isNaN(then)) return "--";
    const secs = Math.round((then - Date.now()) / 1000);
    const units = [
      ["year", 31536000], ["month", 2592000], ["day", 86400],
      ["hour", 3600], ["minute", 60], ["second", 1],
    ];
    for (const [unit, size] of units) {
      if (Math.abs(secs) >= size || unit === "second") {
        try {
          return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
            .format(Math.round(secs / size), unit);
        } catch (_) {
          return formatDate(isoString);
        }
      }
    }
    return formatDate(isoString);
  }

  /** Read a CSS custom property so Chart.js follows the active desk theme. */
  function themeColor(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function setBusy(el, busy) {
    if (el) el.hidden = !busy;
  }

  async function readError(res, fallbackKey) {
    try {
      const data = await res.json();
      return data.detail || data.error || t(fallbackKey, "Request failed.");
    } catch (_) {
      return t(fallbackKey, "Request failed.");
    }
  }

  /* ==========================================================================
     Dialog plumbing: focus move-in, focus trap, focus restore, scroll lock
     ========================================================================== */
  const FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function openDialog(dialog) {
    focusBeforeDialog = document.activeElement;
    dialog.hidden = false;
    document.body.classList.add("modal-open");
    // Anything behind the dialog must leave the tab order entirely.
    if (mainRegion) mainRegion.setAttribute("inert", "");
    const first = dialog.querySelector(FOCUSABLE);
    if (first) first.focus();
  }

  function closeDialog(dialog) {
    dialog.hidden = true;
    document.body.classList.remove("modal-open");
    if (mainRegion) mainRegion.removeAttribute("inert");
    if (focusBeforeDialog && document.contains(focusBeforeDialog)) {
      focusBeforeDialog.focus();
    }
    focusBeforeDialog = null;
  }

  function openDialogs() {
    return [modalUserManage, modalInvite].filter((d) => d && !d.hidden);
  }

  document.addEventListener("keydown", (e) => {
    const dialog = openDialogs()[0];
    if (!dialog) return;

    if (e.key === "Escape") {
      e.preventDefault();
      dialog === modalInvite ? closeInviteModal() : closeUserModal();
      return;
    }

    if (e.key !== "Tab") return;
    const items = Array.from(dialog.querySelectorAll(FOCUSABLE))
      .filter((el) => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  /* ==========================================================================
     Tab navigation — click, arrow keys, and a shareable URL hash
     ========================================================================== */
  const VALID_ADMIN_TABS = ["analytics", "users", "smtp", "activity", "system"];
  const STORAGE_KEY_ADMIN_TAB = "algopaca_admin_active_tab";

  function resolveInitialTab() {
    const hash = window.location.hash.replace(/^#/, "").replace(/^tab=/, "").trim();
    if (VALID_ADMIN_TABS.includes(hash)) return hash;
    try {
      const queryTab = new URLSearchParams(window.location.search).get("tab");
      if (queryTab && VALID_ADMIN_TABS.includes(queryTab)) return queryTab;
    } catch (_) {}
    try {
      const saved = localStorage.getItem(STORAGE_KEY_ADMIN_TAB);
      if (saved && VALID_ADMIN_TABS.includes(saved)) return saved;
    } catch (_) {}
    return "analytics";
  }

  function switchTab(tabId, updateUrl = true) {
    if (!VALID_ADMIN_TABS.includes(tabId)) tabId = "analytics";
    activeTab = tabId;

    try {
      localStorage.setItem(STORAGE_KEY_ADMIN_TAB, tabId);
    } catch (_) {}
    if (updateUrl && history.replaceState) {
      history.replaceState(null, "", `#${tabId}`);
    }

    tabButtons.forEach((btn) => {
      const isMatch = btn.getAttribute("data-tab") === tabId;
      btn.classList.toggle("is-active", isMatch);
      btn.setAttribute("aria-selected", String(isMatch));
      // Roving tabindex: only the selected tab is a tab stop, as the ARIA
      // tablist pattern requires — arrows move between the rest.
      btn.tabIndex = isMatch ? 0 : -1;
    });

    tabPanels.forEach((panel) => {
      const isMatch = panel.id === `panel-${tabId}`;
      panel.classList.toggle("is-active", isMatch);
      panel.hidden = !isMatch;
    });

    if (tabId === "analytics") fetchAnalytics();
    else if (tabId === "users") fetchUsers();
    else if (tabId === "smtp") { fetchSmtpConfig(); fetchEmailLog(); }
    else if (tabId === "activity") fetchAuditLog();
    else if (tabId === "system") fetchSystemStats();
  }

  tabButtons.forEach((btn, index) => {
    btn.addEventListener("click", () => {
      const tabId = btn.getAttribute("data-tab");
      if (tabId) switchTab(tabId, true);
    });

    btn.addEventListener("keydown", (e) => {
      const keys = { ArrowRight: 1, ArrowLeft: -1 };
      let next = null;
      if (e.key in keys) {
        next = (index + keys[e.key] + tabButtons.length) % tabButtons.length;
      } else if (e.key === "Home") {
        next = 0;
      } else if (e.key === "End") {
        next = tabButtons.length - 1;
      }
      if (next === null) return;
      e.preventDefault();
      const target = tabButtons[next];
      switchTab(target.getAttribute("data-tab"), true);
      target.focus();
    });
  });

  window.addEventListener("hashchange", () => {
    const hash = window.location.hash.replace(/^#/, "").replace(/^tab=/, "").trim();
    if (VALID_ADMIN_TABS.includes(hash) && hash !== activeTab) switchTab(hash, false);
  });

  /* ==========================================================================
     Analytics
     ========================================================================== */
  async function fetchAnalytics() {
    try {
      const res = await fetch("/api/admin/stats");
      if (res.status === 403) {
        showToast(t("admin_required", "Administrator access is required."), "error");
        window.location.href = "/auto-trade";
        return;
      }
      if (!res.ok) throw new Error(await readError(res, "stats_load_failed"));
      const data = await res.json();
      if (!data.ok) return;
      renderAnalyticsOverview(data.analytics, data.system);
    } catch (err) {
      console.error("Fetch analytics error:", err);
      showToast(t("stats_load_failed", "Could not load analytics."), "error");
      showChartState(t("stats_load_failed", "Could not load analytics."), true);
    }
  }

  function renderAnalyticsOverview(analytics, system) {
    if (!analytics) return;
    const ov = analytics.overview || {};
    const tm = analytics.trading_modes || {};
    const intg = analytics.integrations || {};

    const totalUsers = ov.total_users || 0;

    if (kpiTotalUsers) kpiTotalUsers.textContent = totalUsers.toLocaleString();
    if (kpiSignups24h) {
      kpiSignups24h.innerHTML = t("signups_24h", "<b>+{n}</b> in the last 24h", {
        n: (ov.signups_24h || 0).toLocaleString(),
      });
    }

    // The headline number is people, not tokens: session count exceeding user
    // count reads as a bug, and "who is online" is the useful figure anyway.
    if (kpiActiveSessions) kpiActiveSessions.textContent = (ov.online_users || 0).toLocaleString();
    if (kpiOnlineUsers) {
      kpiOnlineUsers.textContent = t("sessions_sub", "across {n} session(s)", {
        n: (ov.active_sessions || 0).toLocaleString(),
      });
    }

    // Paper / Live / Not-set-up now sum to total_users, so this card and the
    // integration bars below divide by the same denominator.
    const paperCnt = tm.paper || 0;
    const liveCnt = tm.live || 0;
    const unconfigured = tm.unconfigured || 0;
    const modeTotal = tm.total || totalUsers || 1;

    if (kpiPaperUsers) {
      kpiPaperUsers.innerHTML = `<strong class="num">${paperCnt.toLocaleString()}</strong> ${escapeHtml(t("paper", "Paper"))}`;
    }
    if (kpiLiveUsers) {
      kpiLiveUsers.innerHTML = `<strong class="num highlight-gold">${liveCnt.toLocaleString()}</strong> ${escapeHtml(t("live", "Live"))}`;
    }
    if (kpiLivePct) {
      kpiLivePct.textContent = unconfigured
        ? `${unconfigured.toLocaleString()} ${t("kpi_unconfigured", "Not set up")} · ${t("of_total_users", "of {n} registered traders", { n: modeTotal.toLocaleString() })}`
        : t("of_total_users", "of {n} registered traders", { n: modeTotal.toLocaleString() });
    }

    if (kpiActiveLoops && system) kpiActiveLoops.textContent = system.active_loops || 0;

    const denom = totalUsers || 1;
    const bars = [
      [integCountPaper, progPaper, intg.paper_keys],
      [integCountLive, progLive, intg.live_keys],
      [integCountOpenai, progOpenai, intg.openai],
      [integCountGemini, progGemini, intg.gemini],
    ];
    bars.forEach(([countEl, barEl, value]) => {
      const n = value || 0;
      if (countEl) countEl.textContent = n.toLocaleString();
      if (barEl) barEl.style.width = `${Math.min(100, Math.round((n / denom) * 100))}%`;
    });

    renderGrowthChart(analytics.daily_signups || []);
  }

  function showChartState(message, canRetry = false) {
    if (!chartGrowthState) return;
    if (chartGrowthStateText) chartGrowthStateText.textContent = message;
    if (btnChartRetry) btnChartRetry.hidden = !canRetry;
    chartGrowthState.hidden = false;
    if (canvasGrowth) canvasGrowth.style.visibility = "hidden";
  }

  function hideChartState() {
    if (chartGrowthState) chartGrowthState.hidden = true;
    if (canvasGrowth) canvasGrowth.style.visibility = "";
  }

  function renderGrowthChart(dailySignups) {
    if (!canvasGrowth || typeof Chart === "undefined") return;

    if (!dailySignups.length) {
      showChartState(t("chart_no_data", "No registrations in this period yet."));
      return;
    }
    // A flat all-zero series is real data, not an error — say so rather than
    // drawing a bare axis the reader has to interpret.
    if (dailySignups.every((d) => !d.count)) {
      showChartState(t("chart_no_data", "No registrations in this period yet."));
      return;
    }
    hideChartState();

    const labels = dailySignups.map((d) => d.label);
    const dataPoints = dailySignups.map((d) => d.count);

    if (growthChartInstance) {
      growthChartInstance.data.labels = labels;
      growthChartInstance.data.datasets[0].data = dataPoints;
      growthChartInstance.update("none");
      return;
    }

    const ctx = canvasGrowth.getContext("2d");
    if (!ctx) return;

    const accent = themeColor("--copper", "#38bdf8");
    const gridColor = themeColor("--line", "#2a384c");
    const mutedColor = themeColor("--muted", "#8f949b");
    const panelColor = themeColor("--panel", "#1a2433");
    const textColor = themeColor("--text", "#f2ebe1");

    const gradient = ctx.createLinearGradient(0, 0, 0, 240);
    gradient.addColorStop(0, `color-mix(in srgb, ${accent} 35%, transparent)`);
    gradient.addColorStop(1, `color-mix(in srgb, ${accent} 0%, transparent)`);

    growthChartInstance = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: t("chart_series", "New traders"),
          data: dataPoints,
          borderColor: accent,
          borderWidth: 2.5,
          backgroundColor: gradient,
          fill: true,
          tension: 0.36,
          pointBackgroundColor: panelColor,
          pointBorderColor: accent,
          pointBorderWidth: 2,
          pointRadius: 4,
          pointHoverRadius: 6,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 650, easing: "easeOutQuart" },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: panelColor,
            titleColor: mutedColor,
            bodyColor: textColor,
            borderColor: accent,
            borderWidth: 1,
            padding: 10,
            cornerRadius: 6,
            displayColors: false,
            titleFont: { family: "IBM Plex Mono, monospace", size: 11 },
            bodyFont: { family: "IBM Plex Mono, monospace", size: 13, weight: "600" },
          },
        },
        scales: {
          x: {
            grid: { color: gridColor, drawBorder: false },
            ticks: {
              color: mutedColor,
              font: { family: "IBM Plex Mono, monospace", size: 10 },
              maxRotation: 0, autoSkip: true, maxTicksLimit: 8,
            },
          },
          y: {
            beginAtZero: true,
            suggestedMax: 5,
            grid: { color: gridColor, drawBorder: false },
            ticks: {
              color: mutedColor,
              font: { family: "IBM Plex Mono, monospace", size: 10 },
              precision: 0, padding: 6,
            },
          },
        },
      },
    });
  }

  btnChartRetry?.addEventListener("click", fetchAnalytics);

  btnRefreshAnalytics?.addEventListener("click", () => {
    fetchAnalytics();
    showToast(t("analytics_refreshed", "Analytics refreshed."), "ok");
  });

  /* ==========================================================================
     User table
     ========================================================================== */
  async function fetchUsers() {
    setBusy(usersBusy, true);
    try {
      const params = new URLSearchParams({
        search: usersQuery.search,
        role: usersQuery.role,
        status: usersQuery.status,
        sort: usersQuery.sort,
        direction: usersQuery.direction,
        limit: String(usersQuery.limit),
        offset: String(usersQuery.offset),
      });
      const res = await fetch(`/api/admin/users?${params}`);
      if (!res.ok) throw new Error(await readError(res, "users_load_failed"));
      const data = await res.json();
      if (!data.ok) return;

      usersRows = data.users || [];
      usersTotal = data.total || 0;
      renderUsersTable();
    } catch (err) {
      console.error("Fetch users error:", err);
      renderUsersError();
      showToast(t("users_load_failed", "Could not load trader accounts."), "error");
    } finally {
      setBusy(usersBusy, false);
    }
  }

  function renderUsersError() {
    usersTbody.innerHTML = `
      <tr><td colspan="8" class="table-state">
        <strong>${escapeHtml(t("users_load_failed", "Could not load trader accounts."))}</strong>
        <button type="button" class="btn btn-ghost btn-sm" id="btn-users-retry">${escapeHtml(t("retry", "Retry"))}</button>
      </td></tr>`;
    $("btn-users-retry")?.addEventListener("click", fetchUsers);
    updatePagination();
  }

  function roleLabel(role) {
    if (role === "owner") return t("role_owner", "Owner");
    if (role === "admin") return t("role_admin", "Admin");
    return t("role_trader", "Trader");
  }

  function renderUsersTable() {
    if (usersTotalBadge) {
      usersTotalBadge.textContent = t("users_count", "{n} traders", { n: usersTotal.toLocaleString() });
    }

    if (!usersRows.length) {
      usersTbody.innerHTML = `
        <tr><td colspan="8" class="table-state">
          <strong>${escapeHtml(t("no_users_found", "No trader accounts match these filters."))}</strong>
          ${escapeHtml(t("no_users_hint", "Try clearing the search or the role filter."))}
        </td></tr>`;
      updatePagination();
      return;
    }

    usersTbody.innerHTML = usersRows.map((u) => {
      const initials = (u.display_name || u.username || "T").slice(0, 2).toUpperCase();
      const role = (u.role || "trader").toLowerCase();
      const roleClass = role === "owner" ? "is-owner" : role === "admin" ? "is-admin" : "is-trader";
      const suspended = (u.status || "active").toLowerCase() === "suspended";

      const badges = [];
      if (u.has_paper_key) {
        badges.push(`<span class="mini-badge is-active">${escapeHtml(t("paper", "Paper"))}</span>`);
      }
      if (u.has_live_key) {
        badges.push(`<span class="mini-badge is-live">${escapeHtml(t("live", "Live"))}</span>`);
      }
      if (u.has_openai_key || u.has_gemini_key) {
        badges.push('<span class="mini-badge is-active">AI</span>');
      }
      if (!badges.length) {
        badges.push(`<span class="mini-badge">${escapeHtml(t("kpi_unconfigured", "Not set up"))}</span>`);
      }

      const sessionBadge = u.active_sessions > 0
        ? `<span class="mini-badge is-active">${escapeHtml(t("n_active", "{n} active", { n: u.active_sessions }))}</span>`
        : `<span class="mini-badge">${escapeHtml(t("idle", "idle"))}</span>`;

      const statusPill = suspended
        ? `<span class="status-pill">${escapeHtml(t("status_suspended", "Suspended"))}</span>`
        : "";

      return `
        <tr data-user-id="${u.id}"${suspended ? ' class="is-suspended"' : ""}>
          <td>
            <div class="user-cell-wrap">
              <div class="user-avatar-badge ${roleClass}">${escapeHtml(initials)}</div>
              <div class="user-names">
                <span class="user-display-name">${escapeHtml(u.display_name || u.username)} ${statusPill}</span>
                <span class="user-username-sub">@${escapeHtml(u.username)}</span>
              </div>
            </div>
          </td>
          <td class="mono">${escapeHtml(u.email)}</td>
          <td><span class="role-pill ${roleClass}">${escapeHtml(roleLabel(role))}</span></td>
          <td><div class="key-badges-row">${badges.join("")}</div></td>
          <td>${sessionBadge}</td>
          <td class="mono text-muted" title="${escapeHtml(formatDate(u.last_login_at))}">${escapeHtml(formatRelative(u.last_login_at))}</td>
          <td class="mono text-muted">${escapeHtml(formatDate(u.created_at))}</td>
          <td style="text-align: right;">
            <button type="button" class="btn btn-ghost btn-sm btn-manage-user" data-user-id="${u.id}">
              ${escapeHtml(t("manage", "Manage"))}
            </button>
          </td>
        </tr>`;
    }).join("");

    usersTbody.querySelectorAll(".btn-manage-user").forEach((btn) => {
      btn.addEventListener("click", () => {
        const uid = parseInt(btn.getAttribute("data-user-id") || "0", 10);
        const userObj = usersRows.find((u) => u.id === uid);
        if (userObj) openUserModal(userObj);
      });
    });

    updatePagination();
  }

  function updatePagination() {
    const { offset, limit } = usersQuery;
    const start = usersTotal === 0 ? 0 : offset + 1;
    const end = Math.min(offset + limit, usersTotal);
    const page = Math.floor(offset / limit) + 1;
    const pages = Math.max(1, Math.ceil(usersTotal / limit));

    if (usersPaginationInfo) {
      usersPaginationInfo.textContent = t("showing_range", "Showing {a}–{b} of {n}", {
        a: start.toLocaleString(), b: end.toLocaleString(), n: usersTotal.toLocaleString(),
      });
    }
    if (usersPageIndicator) usersPageIndicator.textContent = `${page} / ${pages}`;

    const atStart = offset <= 0;
    const atEnd = offset + limit >= usersTotal;
    if (btnUsersFirst) btnUsersFirst.disabled = atStart;
    if (btnUsersPrev) btnUsersPrev.disabled = atStart;
    if (btnUsersNext) btnUsersNext.disabled = atEnd;
    if (btnUsersLast) btnUsersLast.disabled = atEnd;
  }

  function goToOffset(offset) {
    usersQuery.offset = Math.max(0, offset);
    fetchUsers();
  }

  btnUsersFirst?.addEventListener("click", () => goToOffset(0));
  btnUsersPrev?.addEventListener("click", () => goToOffset(usersQuery.offset - usersQuery.limit));
  btnUsersNext?.addEventListener("click", () => goToOffset(usersQuery.offset + usersQuery.limit));
  btnUsersLast?.addEventListener("click", () => {
    const pages = Math.max(1, Math.ceil(usersTotal / usersQuery.limit));
    goToOffset((pages - 1) * usersQuery.limit);
  });

  selectPageSize?.addEventListener("change", (e) => {
    usersQuery.limit = parseInt(e.target.value, 10) || 25;
    usersQuery.offset = 0;
    fetchUsers();
  });

  // Sorting. Clicking the active column flips direction; a new column starts
  // descending, which is what you want for dates and counts alike.
  function applySortIndicators() {
    sortHeaders.forEach((th) => {
      const key = th.getAttribute("data-sort");
      const isActive = key === usersQuery.sort;
      th.setAttribute("aria-sort", isActive ? (usersQuery.direction === "asc" ? "ascending" : "descending") : "none");
      const arrow = th.querySelector(".sort-arrow");
      if (arrow) arrow.textContent = isActive && usersQuery.direction === "asc" ? "↑" : "↓";
    });
  }

  function toggleSort(key) {
    if (usersQuery.sort === key) {
      usersQuery.direction = usersQuery.direction === "asc" ? "desc" : "asc";
    } else {
      usersQuery.sort = key;
      usersQuery.direction = "desc";
    }
    usersQuery.offset = 0;
    applySortIndicators();
    fetchUsers();
  }

  sortHeaders.forEach((th) => {
    const key = th.getAttribute("data-sort");
    if (!key) return;
    th.addEventListener("click", () => toggleSort(key));
    th.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggleSort(key);
      }
    });
  });

  inputUsersSearch?.addEventListener("input", (e) => {
    const val = e.target.value;
    if (btnClearUsersSearch) btnClearUsersSearch.hidden = !val.trim();
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(() => {
      usersQuery.search = val.trim();
      usersQuery.offset = 0;
      fetchUsers();
    }, 280);
  });

  btnClearUsersSearch?.addEventListener("click", () => {
    if (inputUsersSearch) inputUsersSearch.value = "";
    btnClearUsersSearch.hidden = true;
    usersQuery.search = "";
    usersQuery.offset = 0;
    fetchUsers();
  });

  selectRoleFilter?.addEventListener("change", (e) => {
    usersQuery.role = e.target.value;
    usersQuery.offset = 0;
    fetchUsers();
  });

  selectStatusFilter?.addEventListener("change", (e) => {
    usersQuery.status = e.target.value;
    usersQuery.offset = 0;
    fetchUsers();
  });

  btnRefreshUsers?.addEventListener("click", () => {
    fetchUsers();
    showToast(t("users_refreshed", "Trader accounts refreshed."), "ok");
  });

  /* Export the page currently in view, matching what the admin can see. */
  btnExportUsers?.addEventListener("click", () => {
    if (!usersRows.length) {
      showToast(t("export_no_rows", "Nothing to export."), "error");
      return;
    }
    const header = ["username", "email", "display_name", "role", "status", "trading_mode", "active_sessions", "last_login_at", "created_at"];
    const csvCell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const csv = [header.join(",")]
      .concat(usersRows.map((u) => header.map((k) => csvCell(u[k])).join(",")))
      .join("\r\n");

    const blob = new Blob([`﻿${csv}`], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `algopaca-traders-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast(t("export_ok", "Exported {n} row(s).", { n: usersRows.length }), "ok");
  });

  /* ==========================================================================
     Manage dialog
     ========================================================================== */
  function canActOn(target) {
    const myRole = (currentUser?.role || "").toLowerCase();
    const theirRole = (target.role || "trader").toLowerCase();
    const isSelf = currentUser?.id === target.id;
    return {
      isSelf,
      // Mirrors the ladder the API enforces, so a forbidden action is greyed
      // out up front rather than failing after the admin has committed to it.
      canGrantOwner: myRole === "owner",
      canModify: !(theirRole === "owner" && myRole !== "owner"),
      canDelete: !isSelf && !(theirRole === "owner" && myRole !== "owner"),
      canSuspend: !isSelf && !(theirRole === "owner" && myRole !== "owner"),
    };
  }

  function openUserModal(user) {
    selectedUser = user;
    const perms = canActOn(user);
    const suspended = (user.status || "active").toLowerCase() === "suspended";

    if (modalUserName) modalUserName.textContent = user.display_name || user.username;
    if (modalUserEmail) modalUserEmail.textContent = user.email;
    if (modalUserAvatar) {
      modalUserAvatar.textContent = (user.display_name || user.username || "T").slice(0, 2).toUpperCase();
    }

    if (modalDetailLastLogin) modalDetailLastLogin.textContent = formatRelative(user.last_login_at);
    if (modalDetailJoined) modalDetailJoined.textContent = formatDate(user.created_at);
    if (modalDetailMode) {
      modalDetailMode.textContent = user.has_credentials
        ? (user.trading_mode === "live" ? t("live", "Live") : t("paper", "Paper"))
        : t("kpi_unconfigured", "Not set up");
    }
    if (modalDetailKeys) {
      const keys = [];
      if (user.has_paper_key) keys.push(t("paper", "Paper"));
      if (user.has_live_key) keys.push(t("live", "Live"));
      if (user.has_openai_key) keys.push("OpenAI");
      if (user.has_gemini_key) keys.push("Gemini");
      modalDetailKeys.textContent = keys.length ? keys.join(", ") : "—";
    }

    if (modalSelectRole) {
      modalSelectRole.value = (user.role || "trader").toLowerCase();
      Array.from(modalSelectRole.options).forEach((opt) => {
        opt.disabled = opt.value === "owner" && !perms.canGrantOwner;
      });
      modalSelectRole.disabled = !perms.canModify;
      refreshNiceSelect(modalSelectRole);
    }
    if (btnModalSaveRole) {
      btnModalSaveRole.disabled = !perms.canModify;
      btnModalSaveRole.title = perms.canModify ? "" : t("owner_only", "Only an Owner can change this.");
    }

    if (btnModalSuspend) {
      btnModalSuspend.hidden = !perms.canSuspend;
      btnModalSuspend.classList.toggle("is-warn", !suspended);
      if (btnModalSuspendText) {
        btnModalSuspendText.textContent = suspended
          ? t("btn_reinstate", "Reinstate Account")
          : t("btn_suspend", "Suspend Account");
      }
    }

    if (btnModalDeleteUser) {
      btnModalDeleteUser.hidden = !perms.canDelete;
      btnModalDeleteUser.title = perms.isSelf ? t("own_account", "This is your own account.") : "";
    }

    if (modalResetLinkBox) modalResetLinkBox.hidden = true;
    if (modalResetLinkInput) modalResetLinkInput.value = "";
    hideDeleteConfirm();

    openDialog(modalUserManage);
    fetchUserSessions(user.id);
  }

  function closeUserModal() {
    hideDeleteConfirm();
    selectedUser = null;
    closeDialog(modalUserManage);
  }

  btnModalClose?.addEventListener("click", closeUserModal);
  modalUserManage?.addEventListener("click", (e) => {
    if (e.target === modalUserManage) closeUserModal();
  });

  async function fetchUserSessions(userId) {
    if (!modalSessionList) return;
    modalSessionList.innerHTML = `<p class="session-empty">${escapeHtml(t("loading", "Loading…"))}</p>`;
    try {
      const res = await fetch(`/api/admin/users/${userId}/sessions`);
      if (!res.ok) throw new Error("failed");
      const data = await res.json();
      renderSessions(data.sessions || []);
    } catch (_) {
      modalSessionList.innerHTML = `<p class="session-empty">${escapeHtml(t("sessions_failed", "Could not load sessions."))}</p>`;
    }
  }

  function renderSessions(sessions) {
    if (!sessions.length) {
      modalSessionList.innerHTML = `<p class="session-empty">${escapeHtml(t("sessions_empty", "No active sessions."))}</p>`;
      return;
    }
    modalSessionList.innerHTML = sessions.map((s) => `
      <div class="session-row">
        <div class="session-meta">
          <span class="session-device" title="${escapeHtml(s.user_agent)}">${escapeHtml(shortDevice(s.user_agent))}</span>
          <span class="session-when">${escapeHtml(t("signed_in", "Signed in {when}", { when: formatRelative(s.created_at) }))}</span>
        </div>
        <button type="button" class="btn-revoke-one" data-session-id="${escapeHtml(s.id)}">${escapeHtml(t("revoke", "Revoke"))}</button>
      </div>`).join("");

    modalSessionList.querySelectorAll(".btn-revoke-one").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!selectedUser) return;
        btn.disabled = true;
        try {
          const sid = btn.getAttribute("data-session-id");
          const res = await fetch(`/api/admin/users/${selectedUser.id}/sessions/${sid}`, { method: "DELETE" });
          if (!res.ok) throw new Error(await readError(res, "sessions_failed"));
          showToast(t("session_revoked", "Session revoked."), "ok");
          fetchUserSessions(selectedUser.id);
          fetchUsers();
        } catch (err) {
          showToast(err.message, "error");
          btn.disabled = false;
        }
      });
    });
  }

  /** Turn a user-agent string into something a human can recognise. */
  function shortDevice(ua) {
    if (!ua) return t("unknown_device", "Unknown device");
    const browser = /Edg\//.test(ua) ? "Edge"
      : /OPR\//.test(ua) ? "Opera"
      : /Firefox\//.test(ua) ? "Firefox"
      : /Chrome\//.test(ua) ? "Chrome"
      : /Safari\//.test(ua) ? "Safari" : null;
    const os = /Windows/.test(ua) ? "Windows"
      : /Android/.test(ua) ? "Android"
      : /(iPhone|iPad|iOS)/.test(ua) ? "iOS"
      : /Mac OS X|Macintosh/.test(ua) ? "macOS"
      : /Linux/.test(ua) ? "Linux" : null;
    if (browser && os) return `${browser} · ${os}`;
    return browser || os || ua.slice(0, 40);
  }

  btnModalSaveRole?.addEventListener("click", async () => {
    if (!selectedUser) return;
    const newRole = modalSelectRole ? modalSelectRole.value : "trader";
    btnModalSaveRole.disabled = true;
    try {
      const res = await fetch(`/api/admin/users/${selectedUser.id}/role`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: newRole }),
      });
      if (!res.ok) throw new Error(await readError(res, "users_load_failed"));
      showToast(t("role_updated", "Role updated to {role}.", { role: roleLabel(newRole) }), "ok");
      closeUserModal();
      fetchUsers();
    } catch (err) {
      showToast(err.message, "error");
      btnModalSaveRole.disabled = false;
    }
  });

  btnModalSendReset?.addEventListener("click", async () => {
    if (!selectedUser) return;
    btnModalSendReset.disabled = true;
    try {
      const res = await fetch(`/api/admin/users/${selectedUser.id}/send-reset`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || t("users_load_failed", "Request failed."));
      if (data.reset_url && modalResetLinkInput && modalResetLinkBox) {
        modalResetLinkInput.value = data.reset_url;
        modalResetLinkBox.hidden = false;
      }
      showToast(data.message, "ok");
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btnModalSendReset.disabled = false;
    }
  });

  function copyToClipboard(value, labelEl, okKey) {
    if (!value) return;
    navigator.clipboard.writeText(value).then(
      () => {
        if (labelEl) {
          labelEl.textContent = t("copied", "Copied");
          setTimeout(() => { labelEl.textContent = t("copy", "Copy"); }, 2000);
        }
        showToast(t(okKey, "Copied."), "ok");
      },
      () => showToast(t("copy_failed", "Could not copy to clipboard."), "error")
    );
  }

  btnCopyResetLink?.addEventListener("click", () =>
    copyToClipboard(modalResetLinkInput?.value, btnCopyResetLinkText, "reset_link_copied"));

  btnModalRevokeSessions?.addEventListener("click", async () => {
    if (!selectedUser) return;
    if (!confirm(t("confirm_revoke_all", "Sign @{name} out of every device?", { name: selectedUser.username }))) return;
    btnModalRevokeSessions.disabled = true;
    try {
      const res = await fetch(`/api/admin/users/${selectedUser.id}/revoke-sessions`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || t("sessions_failed", "Request failed."));
      showToast(data.message, "ok");
      fetchUserSessions(selectedUser.id);
      fetchUsers();
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btnModalRevokeSessions.disabled = false;
    }
  });

  btnModalSuspend?.addEventListener("click", async () => {
    if (!selectedUser) return;
    const suspended = (selectedUser.status || "active").toLowerCase() === "suspended";
    const next = suspended ? "active" : "suspended";
    if (!suspended && !confirm(t("suspend_confirm", "Suspend @{name}?", { name: selectedUser.username }))) return;

    btnModalSuspend.disabled = true;
    try {
      const res = await fetch(`/api/admin/users/${selectedUser.id}/status`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: next }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || t("users_load_failed", "Request failed."));
      showToast(data.message, "ok");
      closeUserModal();
      fetchUsers();
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btnModalSuspend.disabled = false;
    }
  });

  /* Typed confirmation in place of the browser prompt(): styled, translatable,
     and it cannot be silently suppressed the way prompt() can. */
  function showDeleteConfirm() {
    if (!selectedUser || !modalDeleteConfirm) return;
    modalDeleteWarning.innerHTML = t(
      "delete_warning",
      "This erases @{name}'s account. Type <b>{name}</b> to confirm.",
      { name: escapeHtml(selectedUser.username) }
    );
    modalDeleteInput.value = "";
    modalDeleteInput.placeholder = selectedUser.username;
    btnDeleteFinal.disabled = true;
    modalDeleteConfirm.hidden = false;
    modalDeleteInput.focus();
  }

  function hideDeleteConfirm() {
    if (modalDeleteConfirm) modalDeleteConfirm.hidden = true;
  }

  btnModalDeleteUser?.addEventListener("click", showDeleteConfirm);
  btnDeleteCancel?.addEventListener("click", hideDeleteConfirm);

  modalDeleteInput?.addEventListener("input", () => {
    if (!selectedUser || !btnDeleteFinal) return;
    btnDeleteFinal.disabled = modalDeleteInput.value.trim() !== selectedUser.username;
  });

  btnDeleteFinal?.addEventListener("click", async () => {
    if (!selectedUser) return;
    btnDeleteFinal.disabled = true;
    try {
      const res = await fetch(`/api/admin/users/${selectedUser.id}`, { method: "DELETE" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || t("users_load_failed", "Request failed."));
      showToast(t("user_deleted", "Account permanently deleted."), "ok");
      closeUserModal();
      fetchUsers();
      fetchAnalytics();
    } catch (err) {
      showToast(err.message, "error");
      btnDeleteFinal.disabled = false;
    }
  });

  /* ==========================================================================
     Invite dialog
     ========================================================================== */
  btnOpenInvite?.addEventListener("click", () => {
    formInvite?.reset();
    if (inviteLinkBox) inviteLinkBox.hidden = true;
    if (inviteRole) {
      const isOwner = (currentUser?.role || "").toLowerCase() === "owner";
      Array.from(inviteRole.options).forEach((opt) => {
        opt.disabled = opt.value === "owner" && !isOwner;
      });
      refreshNiceSelect(inviteRole);
    }
    openDialog(modalInvite);
  });

  function closeInviteModal() {
    closeDialog(modalInvite);
  }

  btnInviteClose?.addEventListener("click", closeInviteModal);
  modalInvite?.addEventListener("click", (e) => {
    if (e.target === modalInvite) closeInviteModal();
  });

  formInvite?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = (inviteUsername?.value || "").trim();
    const email = (inviteEmail?.value || "").trim();

    if (!username || !email) {
      showToast(t("invite_fields_required", "Username and email are both required."), "error");
      return;
    }
    if (!EMAIL_RE.test(email)) {
      showToast(t("email_invalid", "Enter a valid email address."), "error");
      inviteEmail.focus();
      return;
    }

    btnInviteSubmit.disabled = true;
    try {
      const res = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username,
          email,
          role: inviteRole?.value || "trader",
          display_name: (inviteDisplayName?.value || "").trim() || null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || t("users_load_failed", "Request failed."));

      showToast(data.message || t("invite_created", "Account created for @{name}.", { name: username }), "ok");
      // Always surface the link: if SMTP is down it is the only way in.
      if (data.setup_url && inviteLinkInput && inviteLinkBox) {
        inviteLinkInput.value = data.setup_url;
        inviteLinkBox.hidden = false;
      }
      formInvite.reset();
      fetchUsers();
      fetchAnalytics();
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btnInviteSubmit.disabled = false;
    }
  });

  btnCopyInviteLink?.addEventListener("click", () =>
    copyToClipboard(inviteLinkInput?.value, btnCopyInviteLinkText, "reset_link_copied"));

  /* ==========================================================================
     SMTP configuration
     ========================================================================== */
  const PRESET_CONFIGS = {
    gmail: { host: "smtp.gmail.com", port: 465, use_ssl: true },
    sendgrid: { host: "smtp.sendgrid.net", port: 587, use_ssl: false, username: "apikey" },
    mailgun: { host: "smtp.mailgun.org", port: 587, use_ssl: false },
    ses: { host: "email-smtp.us-east-1.amazonaws.com", port: 587, use_ssl: false },
    outlook: { host: "smtp.office365.com", port: 587, use_ssl: false },
    custom: {},
  };

  async function fetchSmtpConfig() {
    try {
      const res = await fetch("/api/admin/smtp");
      if (!res.ok) throw new Error(await readError(res, "smtp_load_failed"));
      const data = await res.json();
      if (data.ok) renderSmtpForm(data.smtp);
    } catch (err) {
      console.error("Fetch SMTP config error:", err);
      showToast(t("smtp_load_failed", "Could not load SMTP configuration."), "error");
    }
  }

  function renderSmtpForm(cfg) {
    if (!cfg) return;

    if (smtpHost) smtpHost.value = cfg.host || "";
    if (smtpPort) smtpPort.value = cfg.port || 587;
    if (smtpUsername) smtpUsername.value = cfg.username || "";
    if (smtpFromEmail) smtpFromEmail.value = cfg.from_email || "";
    if (smtpSenderName) smtpSenderName.value = cfg.sender_name || "AlgoPaca";
    if (smtpUseSsl) smtpUseSsl.checked = !!cfg.use_ssl;

    // Leave the field empty and say plainly that a secret is on file, instead
    // of pre-filling a mask that looks identical to a freshly typed password.
    if (smtpPassword) {
      smtpPassword.value = "";
      smtpPassword.placeholder = cfg.has_password ? "••••••••" : "";
    }
    if (smtpPasswordHint) {
      smtpPasswordHint.textContent = cfg.has_password
        ? t("password_stored_hint", "A password is stored. Leave blank to keep it.")
        : "";
      smtpPasswordHint.hidden = !cfg.has_password;
    }

    // Reflect which preset the saved host actually matches.
    const matched = Object.keys(PRESET_CONFIGS).find(
      (id) => PRESET_CONFIGS[id].host && PRESET_CONFIGS[id].host === cfg.host
    ) || "custom";
    presetButtons.forEach((b) => b.classList.toggle("is-active", b.getAttribute("data-preset") === matched));

    const isConfigured = !!cfg.configured;
    if (smtpOrb) smtpOrb.className = `status-indicator-orb ${isConfigured ? "is-connected" : ""}`;
    if (smtpStatusTitle) {
      smtpStatusTitle.textContent = isConfigured
        ? t("smtp_active", "SMTP Service Active")
        : t("smtp_unconfigured", "SMTP Not Configured");
    }
    if (smtpStatusDesc) {
      smtpStatusDesc.textContent = isConfigured
        ? t("smtp_ready_desc", "Dispatching through {host}:{port} as {from}", {
            host: cfg.host, port: cfg.port, from: cfg.from_email || cfg.username,
          })
        : t("smtp_unconfigured_desc", "Password reset and invite emails will not be delivered until this is set up.");
    }
    if (smtpConfiguredBadge) {
      smtpConfiguredBadge.textContent = isConfigured
        ? t("badge_ready", "Ready")
        : t("badge_unconfigured", "Unconfigured");
      smtpConfiguredBadge.className = `badge ${isConfigured ? "is-emerald" : "is-amber"}`;
    }

    if (smtpTestRecipient && !smtpTestRecipient.value) {
      smtpTestRecipient.value = currentUser?.email || cfg.from_email || "";
    }
  }

  presetButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const preset = PRESET_CONFIGS[btn.getAttribute("data-preset")];
      if (!preset) return;
      presetButtons.forEach((b) => b.classList.toggle("is-active", b === btn));
      if (preset.host && smtpHost) smtpHost.value = preset.host;
      if (preset.port && smtpPort) smtpPort.value = preset.port;
      if (preset.use_ssl !== undefined && smtpUseSsl) smtpUseSsl.checked = preset.use_ssl;
      if (preset.username !== undefined && smtpUsername && !smtpUsername.value) {
        smtpUsername.value = preset.username;
      }
    });
  });

  btnToggleSmtpPw?.addEventListener("click", () => {
    if (!smtpPassword) return;
    const showing = smtpPassword.type === "text";
    smtpPassword.type = showing ? "password" : "text";
    btnToggleSmtpPw.setAttribute("aria-pressed", String(!showing));
  });

  /** Shared validation: the forms carry novalidate, so this is the only gate. */
  function readSmtpForm() {
    const host = (smtpHost?.value || "").trim();
    if (!host) {
      showToast(t("smtp_host_required", "Enter an SMTP host before saving."), "error");
      smtpHost?.focus();
      return null;
    }
    const port = parseInt(smtpPort?.value || "587", 10);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      showToast(t("smtp_port_invalid", "Port must be between 1 and 65535."), "error");
      smtpPort?.focus();
      return null;
    }
    return {
      host,
      port,
      username: (smtpUsername?.value || "").trim(),
      password: smtpPassword?.value || "",
      from_email: (smtpFromEmail?.value || "").trim(),
      sender_name: (smtpSenderName?.value || "").trim() || "AlgoPaca",
      use_ssl: !!smtpUseSsl?.checked,
    };
  }

  formSmtpConfig?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = readSmtpForm();
    if (!payload) return;

    const saveBtn = $("btn-save-smtp");
    if (saveBtn) saveBtn.disabled = true;
    try {
      const res = await fetch("/api/admin/smtp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || t("smtp_load_failed", "Request failed."));
      showToast(data.message, data.smtp?.configured ? "ok" : "error");
      renderSmtpForm(data.smtp);
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  });

  /* ==========================================================================
     SMTP diagnostics console
     ========================================================================== */
  function appendDiagLog(step, status, detail) {
    if (!smtpTerminalOutput) return;
    const line = document.createElement("p");
    line.className = `term-line is-${status}`;
    line.innerHTML = `
      <span class="term-time">${new Date().toLocaleTimeString()}</span>
      <strong class="term-tag">[${escapeHtml(String(step || "log").toUpperCase())}]</strong>
      <span class="term-text">${escapeHtml(detail)}</span>`;
    smtpTerminalOutput.appendChild(line);
    smtpTerminalOutput.scrollTop = smtpTerminalOutput.scrollHeight;
  }

  formSmtpTest?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const recipient = (smtpTestRecipient?.value || "").trim();
    if (!EMAIL_RE.test(recipient)) {
      showToast(t("email_invalid", "Enter a valid email address."), "error");
      smtpTestRecipient?.focus();
      return;
    }
    const cfg = readSmtpForm();
    if (!cfg) return;

    btnRunSmtpTest.disabled = true;
    smtpTerminalOutput.innerHTML = "";
    appendDiagLog("init", "info", t("diag_starting", "Verifying SMTP delivery to {to}…", { to: recipient }));

    try {
      const res = await fetch("/api/admin/smtp/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to_email: recipient, ...cfg }),
      });
      const data = await res.json();
      (data.logs || []).forEach((item) => appendDiagLog(item.step, item.status, item.detail));

      // The banner and the console must not disagree about the same test.
      if (smtpOrb) smtpOrb.className = `status-indicator-orb ${data.ok ? "is-connected" : "is-error"}`;
      if (smtpConfiguredBadge && !data.ok) {
        smtpConfiguredBadge.textContent = t("status_failed", "Failed");
        smtpConfiguredBadge.className = "badge is-rose";
      }
      showToast(data.ok ? data.message : data.error, data.ok ? "ok" : "error");
      fetchEmailLog();
    } catch (err) {
      appendDiagLog("error", "error", err.message);
      if (smtpOrb) smtpOrb.className = "status-indicator-orb is-error";
      showToast(err.message, "error");
    } finally {
      btnRunSmtpTest.disabled = false;
    }
  });

  btnClearDiagLogs?.addEventListener("click", () => {
    if (smtpTerminalOutput) {
      smtpTerminalOutput.innerHTML =
        `<p class="term-line is-muted">${escapeHtml(t("diag_cleared", "Console cleared."))}</p>`;
    }
  });

  btnCopyDiagLogs?.addEventListener("click", () => {
    const lines = smtpTerminalOutput?.querySelectorAll(".term-line:not(.is-muted)") || [];
    if (!lines.length) {
      showToast(t("diag_no_logs", "No diagnostic output to copy."), "error");
      return;
    }
    const text = Array.from(lines).map((l) => l.innerText.trim()).join("\n");
    navigator.clipboard.writeText(text).then(
      () => showToast(t("diag_copied", "Diagnostic output copied."), "ok"),
      () => showToast(t("copy_failed", "Could not copy to clipboard."), "error")
    );
  });

  /* ==========================================================================
     Email delivery log
     ========================================================================== */
  async function fetchEmailLog() {
    if (!emailLogList) return;
    try {
      const res = await fetch("/api/admin/email-log?limit=25");
      if (!res.ok) return;
      const data = await res.json();
      const entries = data.entries || [];
      if (!entries.length) {
        emailLogList.innerHTML = `<p class="session-empty">${escapeHtml(t("email_log_empty", "No messages sent yet."))}</p>`;
        return;
      }
      emailLogList.innerHTML = entries.map((e) => `
        <div class="email-log-row">
          <span class="email-log-dot ${e.success ? "" : "is-fail"}"></span>
          <span class="email-log-to" title="${escapeHtml(e.error || e.kind)}">${escapeHtml(e.recipient)}</span>
          <span class="email-log-when">${escapeHtml(formatRelative(e.created_at))}</span>
        </div>`).join("");
    } catch (err) {
      console.error("Fetch email log error:", err);
    }
  }

  btnRefreshEmailLog?.addEventListener("click", fetchEmailLog);

  /* ==========================================================================
     Activity (audit) log
     ========================================================================== */
  const AUDIT_TONE = {
    "user.delete": "is-danger",
    "user.suspended": "is-danger",
    "user.role_change": "is-warn",
    "session.revoke_all": "is-warn",
    "session.revoke_one": "is-warn",
  };

  async function fetchAuditLog() {
    if (!auditList) return;
    try {
      const res = await fetch(`/api/admin/audit?limit=${auditQuery.limit}&offset=${auditQuery.offset}`);
      if (!res.ok) throw new Error(await readError(res, "audit_load_failed"));
      const data = await res.json();
      auditTotal = data.total || 0;
      renderAuditLog(data.entries || []);
    } catch (err) {
      console.error("Fetch audit error:", err);
      auditList.innerHTML = `<p class="session-empty">${escapeHtml(t("audit_load_failed", "Could not load the activity log."))}</p>`;
    }
  }

  function renderAuditLog(entries) {
    if (!entries.length) {
      auditList.innerHTML = `<p class="session-empty">${escapeHtml(t("audit_empty", "No administrative actions recorded yet."))}</p>`;
    } else {
      auditList.innerHTML = entries.map((e) => {
        const tone = AUDIT_TONE[e.action] || "";
        const target = e.target_username ? ` → <b>@${escapeHtml(e.target_username)}</b>` : "";
        const detail = e.detail ? ` · ${escapeHtml(e.detail)}` : "";
        return `
          <div class="audit-row">
            <span class="audit-when" title="${escapeHtml(formatDate(e.created_at))}">${escapeHtml(formatRelative(e.created_at))}</span>
            <span class="audit-text">
              <span class="audit-action ${tone}">${escapeHtml(e.action)}</span>
              <b>@${escapeHtml(e.actor_username || "system")}</b>${target}${detail}
            </span>
          </div>`;
      }).join("");
    }

    const start = auditTotal === 0 ? 0 : auditQuery.offset + 1;
    const end = Math.min(auditQuery.offset + auditQuery.limit, auditTotal);
    if (auditPaginationInfo) {
      auditPaginationInfo.textContent = t("showing_range", "Showing {a}–{b} of {n}", {
        a: start, b: end, n: auditTotal.toLocaleString(),
      });
    }
    if (btnAuditPrev) btnAuditPrev.disabled = auditQuery.offset <= 0;
    if (btnAuditNext) btnAuditNext.disabled = auditQuery.offset + auditQuery.limit >= auditTotal;
  }

  btnAuditPrev?.addEventListener("click", () => {
    auditQuery.offset = Math.max(0, auditQuery.offset - auditQuery.limit);
    fetchAuditLog();
  });
  btnAuditNext?.addEventListener("click", () => {
    auditQuery.offset += auditQuery.limit;
    fetchAuditLog();
  });
  btnRefreshAudit?.addEventListener("click", () => {
    auditQuery.offset = 0;
    fetchAuditLog();
  });

  /* ==========================================================================
     System status & maintenance
     ========================================================================== */
  async function fetchSystemStats() {
    try {
      const res = await fetch("/api/admin/stats");
      if (!res.ok) throw new Error(await readError(res, "system_load_failed"));
      const data = await res.json();
      if (!data.ok) return;

      const sys = data.system || {};
      if (sysPythonVal) sysPythonVal.textContent = sys.python_version || "--";
      if (sysDbPath) sysDbPath.textContent = sys.db_path || "--";
      if (sysDbSize) sysDbSize.textContent = `${sys.db_size_mb || 0} MB`;
      if (sysClock) sysClock.textContent = sys.server_time ? formatDate(sys.server_time) : "--";
    } catch (err) {
      console.error("Fetch system stats error:", err);
      showToast(t("system_load_failed", "Could not load system metrics."), "error");
    }
  }

  btnRefreshSystem?.addEventListener("click", () => {
    fetchSystemStats();
    showToast(t("system_refreshed", "System metrics refreshed."), "ok");
  });

  function setMaintOutput(html) {
    if (maintOutputBox) maintOutputBox.innerHTML = html;
  }

  btnMaintPurge?.addEventListener("click", async () => {
    btnMaintPurge.disabled = true;
    setMaintOutput(`<span class="highlight-cyan">${escapeHtml(t("purge_running", "Purging expired records…"))}</span>`);
    try {
      const res = await fetch("/api/admin/maintenance/purge-expired", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || t("system_load_failed", "Request failed."));
      setMaintOutput(`<span class="highlight-emerald">✔ ${escapeHtml(
        t("purge_done", "Purged {s} session(s) and {t} reset token(s).", {
          s: data.purged_sessions || 0, t: data.purged_tokens || 0,
        })
      )}</span>`);
      showToast(data.message, "ok");
      fetchSystemStats();
    } catch (err) {
      setMaintOutput(`<span style="color: var(--sell);">${escapeHtml(err.message)}</span>`);
      showToast(err.message, "error");
    } finally {
      btnMaintPurge.disabled = false;
    }
  });

  btnMaintVacuum?.addEventListener("click", async () => {
    btnMaintVacuum.disabled = true;
    setMaintOutput(`<span class="highlight-cyan">${escapeHtml(t("vacuum_running", "Running VACUUM and integrity check…"))}</span>`);
    try {
      const res = await fetch("/api/admin/maintenance/vacuum", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || t("system_load_failed", "Request failed."));

      const integrity = data.integrity || "ok";
      const healthy = integrity === "ok";
      setMaintOutput(`<span class="${healthy ? "highlight-emerald" : ""}" ${healthy ? "" : 'style="color: var(--sell);"'}>${
        healthy ? "✔ " : "✖ "
      }${escapeHtml(t("vacuum_done", "VACUUM complete. Integrity: {i}. Size now {mb} MB.", {
        i: integrity, mb: data.file_size_mb || 0,
      }))}</span>`);

      // The integrity row is only truthful right after a check, and its colour
      // must follow the result rather than being permanently green.
      if (sysIntegrity) {
        sysIntegrity.textContent = integrity;
        sysIntegrity.classList.toggle("highlight-emerald", healthy);
        sysIntegrity.style.color = healthy ? "" : "var(--sell)";
      }
      showToast(healthy ? data.message : t("integrity_failed", "Integrity check reported a problem."), healthy ? "ok" : "error");
      fetchSystemStats();
    } catch (err) {
      setMaintOutput(`<span style="color: var(--sell);">${escapeHtml(err.message)}</span>`);
      showToast(err.message, "error");
    } finally {
      btnMaintVacuum.disabled = false;
    }
  });

  /* ==========================================================================
     Boot
     ========================================================================== */
  document.addEventListener("DOMContentLoaded", async () => {
    if (typeof checkAuthStatus === "function") {
      currentUser = await checkAuthStatus();
    }

    // Only hide the native selects once NiceSelect has actually replaced them,
    // so a blocked vendor script leaves working controls behind.
    [selectRoleFilter, selectStatusFilter, modalSelectRole, inviteRole].forEach((el) => {
      if (!el) return;
      ensureNiceSelect(el);
      if (el._niceSelect) el.parentElement?.classList.add("nice-bound");
    });

    applySortIndicators();
    switchTab(resolveInitialTab(), true);
  });

  window.addEventListener("resize", () => {
    if (activeTab === "analytics" && growthChartInstance) growthChartInstance.resize();
  });
})();
