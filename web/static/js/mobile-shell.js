/**
 * AlgoPaca - Mobile App Experience & Shell Integration (<= 768px)
 * Bottom navigation bar, slide-up "More" drawer, touch gestures, active route sync, theme & lang controls.
 * Loaded and executed exclusively on mobile viewports.
 */

(function () {
  if (window.__mobileShellLoaded) return;
  window.__mobileShellLoaded = true;

  function initMobileAppShell() {
    const path = window.location.pathname;
    if (path.startsWith("/login") || path.startsWith("/signup") || path.startsWith("/reset-password")) {
      return;
    }

    let tabBar = document.getElementById("mobile-tab-bar");
    let backdrop = document.getElementById("mobile-sheet-backdrop");
    let sheet = document.getElementById("mobile-more-sheet");

    if (!tabBar) {
      tabBar = document.createElement("nav");
      tabBar.id = "mobile-tab-bar";
      tabBar.className = "mobile-tab-bar";
      tabBar.setAttribute("aria-label", "Mobile App Navigation");
      tabBar.innerHTML = `
        <a href="/auto-trade" class="mobile-tab-item" data-page="auto-trade" aria-label="Auto Trade">
          <div class="mobile-tab-icon-wrap">
            <svg viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
          </div>
          <span class="mobile-tab-label" data-i18n="nav_auto_trade">Auto Trade</span>
        </a>
        <a href="/manual-order" class="mobile-tab-item" data-page="manual-order" aria-label="Advanced Order">
          <div class="mobile-tab-icon-wrap">
            <svg viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
          </div>
          <span class="mobile-tab-label" data-i18n="tab_order">Order</span>
        </a>
        <a href="/positions" class="mobile-tab-item" data-page="positions" aria-label="Positions">
          <div class="mobile-tab-icon-wrap">
            <svg viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="14" rx="2" ry="2" stroke-linecap="round" stroke-linejoin="round"/><path stroke-linecap="round" stroke-linejoin="round" d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>
          </div>
          <span class="mobile-tab-label" data-i18n="nav_positions">Positions</span>
        </a>
        <a href="/orders" class="mobile-tab-item" data-page="orders" aria-label="Orders">
          <div class="mobile-tab-icon-wrap">
            <svg viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path stroke-linecap="round" stroke-linejoin="round" d="M9 14l2 2 4-4"/></svg>
            <span class="mobile-tab-badge" id="mobile-orders-badge" hidden>0</span>
          </div>
          <span class="mobile-tab-label" data-i18n="nav_orders">Orders</span>
        </a>
        <button type="button" class="mobile-tab-item" id="mobile-more-trigger" aria-haspopup="dialog" aria-expanded="false" aria-controls="mobile-more-sheet" aria-label="Menu & More">
          <div class="mobile-tab-icon-wrap">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="1.75"/><circle cx="19" cy="12" r="1.75"/><circle cx="5" cy="12" r="1.75"/></svg>
          </div>
          <span class="mobile-tab-label" data-i18n="nav_more">More</span>
        </button>
      `;
      document.body.appendChild(tabBar);
    }

    if (!backdrop) {
      backdrop = document.createElement("div");
      backdrop.id = "mobile-sheet-backdrop";
      backdrop.className = "mobile-sheet-backdrop";
      backdrop.setAttribute("aria-hidden", "true");
      document.body.appendChild(backdrop);
    }

    if (!sheet) {
      sheet = document.createElement("div");
      sheet.id = "mobile-more-sheet";
      sheet.className = "mobile-sheet";
      sheet.setAttribute("role", "dialog");
      sheet.setAttribute("aria-modal", "true");
      sheet.setAttribute("aria-label", "Navigation & Settings Menu");
      sheet.innerHTML = `
        <div class="mobile-sheet-handle-wrap" id="mobile-sheet-handle">
          <div class="mobile-sheet-handle"></div>
        </div>
        <div class="mobile-sheet-head">
          <h2 class="mobile-sheet-title" data-i18n="app_menu">Menu & Settings</h2>
          <button type="button" class="mobile-sheet-close" id="mobile-sheet-close" aria-label="Close menu">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M18 6L6 18M6 6l12 12"></path>
            </svg>
          </button>
        </div>
        <div class="mobile-sheet-body">
          <div class="mobile-profile-card" id="mobile-sheet-profile">
            <div class="mobile-profile-avatar" id="mobile-profile-avatar">AP</div>
            <div class="mobile-profile-info">
              <div class="mobile-profile-name" id="mobile-profile-name">Trader</div>
              <div class="mobile-profile-meta">
                <span class="mobile-profile-role" id="mobile-profile-role">Trader</span>
                <span class="mobile-profile-badge armed" id="mobile-profile-mode">Paper</span>
              </div>
            </div>
          </div>

          <div class="mobile-nav-group">
            <h3 class="mobile-nav-group-title" data-i18n="nav_group_trade">Trading & Portfolio</h3>
            <div class="mobile-nav-card">
              <a href="/auto-trade" data-page="auto-trade" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="nav_auto_trade">Auto Trade</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
              <a href="/manual-order" data-page="manual-order" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="nav_manual_order">Advanced Order</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
              <a href="/positions" data-page="positions" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="14" rx="2" ry="2" stroke-linecap="round" stroke-linejoin="round"/><path stroke-linecap="round" stroke-linejoin="round" d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="nav_positions">Positions</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
              <a href="/orders" data-page="orders" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path stroke-linecap="round" stroke-linejoin="round" d="M9 14l2 2 4-4"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="nav_orders">Orders</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
              <a href="/history" data-page="history" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="nav_history">History & Fills</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
            </div>
          </div>

          <div class="mobile-nav-group">
            <h3 class="mobile-nav-group-title" data-i18n="nav_backtest">Backtest Lab</h3>
            <div class="mobile-nav-card">
              <a href="/backtest" data-page="backtest" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="bt_subnav_run">Run Simulation</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
              <a href="/backtest/history" data-page="backtest-history" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M3 3v5h5M21 21v-5h-5"/><path stroke-linecap="round" stroke-linejoin="round" d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="bt_subnav_history">Backtest History</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
              <a href="/backtest/compare" data-page="backtest-compare" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="bt_subnav_compare">Compare Strategies</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
            </div>
          </div>

          <div class="mobile-nav-group">
            <h3 class="mobile-nav-group-title" data-i18n="nav_group_settings">Settings & Tools</h3>
            <div class="mobile-nav-card">
              <a href="/setup-wizard" data-page="setup-wizard" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="nav_setup_wizard">Setup Wizard</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
              <a href="/api-keys" data-page="api-keys" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4"/><path stroke-linecap="round" stroke-linejoin="round" d="m21 2-9.6 9.6"/><circle cx="7.5" cy="15.5" r="5.5"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="nav_api_keys">API Keys</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
              <a href="/settings" data-page="settings" class="mobile-nav-row">
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path stroke-linecap="round" stroke-linejoin="round" d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="nav_settings">Settings</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
              <a href="/admin" data-page="admin" class="mobile-nav-row" id="mobile-sheet-admin-link" hidden>
                <span class="mobile-nav-row-icon">
                  <svg viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                </span>
                <span class="mobile-nav-row-label" data-i18n="nav_admin">Admin Panel</span>
                <span class="mobile-nav-row-chevron"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
              </a>
            </div>
          </div>

          <div class="mobile-nav-group">
            <h3 class="mobile-nav-group-title" data-i18n="settings_theme_title">Terminal Theme</h3>
            <div class="mobile-theme-picker" id="mobile-theme-picker" role="radiogroup" aria-label="Terminal Themes" data-i18n-aria-label="terminal_themes">
              <button type="button" class="mobile-theme-btn" data-theme-val="obsidian" role="radio" aria-label="Obsidian Night" data-i18n-aria-label="theme_obsidian">
                <span class="mobile-theme-swatch obsidian"></span>
                <span class="mobile-theme-label" data-i18n="theme_short_obsidian">Obsidian</span>
              </button>
              <button type="button" class="mobile-theme-btn" data-theme-val="midnight" role="radio" aria-label="Midnight Slate" data-i18n-aria-label="theme_midnight">
                <span class="mobile-theme-swatch midnight"></span>
                <span class="mobile-theme-label" data-i18n="theme_short_midnight">Midnight</span>
              </button>
              <button type="button" class="mobile-theme-btn" data-theme-val="emerald" role="radio" aria-label="Emerald Forest" data-i18n-aria-label="theme_emerald">
                <span class="mobile-theme-swatch emerald"></span>
                <span class="mobile-theme-label" data-i18n="theme_short_emerald">Emerald</span>
              </button>
              <button type="button" class="mobile-theme-btn" data-theme-val="daylight" role="radio" aria-label="Daylight Desk" data-i18n-aria-label="theme_daylight">
                <span class="mobile-theme-swatch daylight"></span>
                <span class="mobile-theme-label" data-i18n="theme_short_daylight">Daylight</span>
              </button>
            </div>
          </div>

          <div class="mobile-nav-group">
            <h3 class="mobile-nav-group-title" data-i18n="language">Language</h3>
            <div class="mobile-lang-wrap">
              <select class="mobile-lang-select lang-select" id="mobile-lang-select" data-native-select="true" aria-label="Language">
                <option value="en">English (US)</option>
                <option value="es">Español</option>
                <option value="fr">Français</option>
                <option value="hi">हिन्दी</option>
                <option value="bn">বাংলা</option>
              </select>
            </div>
          </div>

          <div class="mobile-sheet-actions" id="mobile-sheet-actions">
            <button type="button" class="mobile-sheet-btn is-logout" id="btn-mobile-logout">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg>
              <span data-i18n="nav_sign_out">Sign Out</span>
            </button>
          </div>
        </div>
      `;
      document.body.appendChild(sheet);
    }

    // Active route sync for mobile tab bar
    const curPath = window.location.pathname.replace(/\/$/, "") || "/auto-trade";
    tabBar.querySelectorAll(".mobile-tab-item[data-page]").forEach((tab) => {
      const page = tab.getAttribute("data-page");
      const href = tab.getAttribute("href");
      const isActive = curPath === href || curPath === `/${page}`;
      tab.classList.toggle("is-active", isActive);
      if (isActive) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    });

    sheet.querySelectorAll(".mobile-nav-row[data-page]").forEach((row) => {
      const page = row.getAttribute("data-page");
      const href = row.getAttribute("href");
      const isActive = curPath === href || curPath === `/${page}`;
      row.classList.toggle("is-active", isActive);
    });

    // Bind Open / Close Sheet handlers
    const openTrigger = document.getElementById("mobile-more-trigger");
    const closeBtn = document.getElementById("mobile-sheet-close");
    const sheetHandle = document.getElementById("mobile-sheet-handle");

    function openSheet() {
      backdrop.classList.add("is-open");
      sheet.classList.add("is-open");
      document.body.classList.add("sheet-open");
      openTrigger?.setAttribute("aria-expanded", "true");
      openTrigger?.classList.add("is-active");
      syncMobileThemeButtons();
      syncMobileLangSelect();
    }

    function closeSheet() {
      backdrop.classList.remove("is-open");
      sheet.classList.remove("is-open");
      document.body.classList.remove("sheet-open");
      openTrigger?.setAttribute("aria-expanded", "false");
      openTrigger?.classList.remove("is-active");
      if (typeof window.initRouting === "function") {
        window.initRouting();
      }
    }

    openTrigger?.addEventListener("click", (e) => {
      e.preventDefault();
      if (sheet.classList.contains("is-open")) closeSheet();
      else openSheet();
    });

    closeBtn?.addEventListener("click", closeSheet);
    backdrop?.addEventListener("click", closeSheet);

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && sheet.classList.contains("is-open")) {
        closeSheet();
      }
    });

    // Touch swipe-down to dismiss gesture
    let startY = 0;
    let currentY = 0;
    sheetHandle?.addEventListener("touchstart", (e) => {
      startY = e.touches[0].clientY;
    }, { passive: true });

    sheetHandle?.addEventListener("touchmove", (e) => {
      currentY = e.touches[0].clientY;
      const diff = currentY - startY;
      if (diff > 0) {
        sheet.style.transform = `translateY(${diff}px)`;
      }
    }, { passive: true });

    sheetHandle?.addEventListener("touchend", () => {
      const diff = currentY - startY;
      sheet.style.transform = "";
      if (diff > 60) {
        closeSheet();
      }
      startY = 0;
      currentY = 0;
    });

    // Theme switcher handler in sheet
    function syncMobileThemeButtons() {
      const activeTheme = document.documentElement.getAttribute("data-theme") || localStorage.getItem("algopaca_theme") || "obsidian";
      document.querySelectorAll(".mobile-theme-btn").forEach((btn) => {
        btn.classList.toggle("is-active", btn.getAttribute("data-theme-val") === activeTheme);
      });
    }

    document.querySelectorAll(".mobile-theme-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const themeVal = btn.getAttribute("data-theme-val");
        if (themeVal) {
          if (typeof setDeskTheme === "function") {
            setDeskTheme(themeVal);
          } else {
            document.documentElement.setAttribute("data-theme", themeVal);
            try {
              localStorage.setItem("algopaca_theme", themeVal);
              document.cookie = `algopaca_theme=${encodeURIComponent(themeVal)}; path=/; max-age=31536000; SameSite=Lax`;
            } catch (e) {}
            window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: themeVal } }));
          }
          syncMobileThemeButtons();
        }
      });
    });

    window.addEventListener("themechange", syncMobileThemeButtons);

    // Language selector in sheet
    function syncMobileLangSelect() {
      const langSelect = document.getElementById("mobile-lang-select");
      if (!langSelect) return;
      const currentLang = typeof window.i18n !== "undefined" && window.i18n.currentLanguage
        ? window.i18n.currentLanguage
        : localStorage.getItem("algopaca_lang") || "en";
      langSelect.value = currentLang;
    }

    const mobileLangSelect = document.getElementById("mobile-lang-select");
    mobileLangSelect?.addEventListener("change", (e) => {
      const chosen = e.target.value;
      if (typeof window.i18n !== "undefined" && typeof window.i18n.setLanguage === "function") {
        window.i18n.setLanguage(chosen);
      }
    });

    // Wire up logout button in sheet
    const mobileLogoutBtn = document.getElementById("btn-mobile-logout");
    if (mobileLogoutBtn && typeof window.handleUserLogout === "function") {
      mobileLogoutBtn.addEventListener("click", window.handleUserLogout);
    }

    // Close sheet when navigation link is clicked
    sheet.querySelectorAll(".mobile-nav-row").forEach((link) => {
      link.addEventListener("click", () => {
        closeSheet();
      });
    });

    // Synchronize current profile state if auth was already checked
    if (typeof window.syncMobileProfileDrawer === "function" && window.currentUser !== undefined) {
      window.syncMobileProfileDrawer(window.currentUser);
    }

    // Translate mobile elements
    if (typeof window.i18n !== "undefined" && window.i18n.translateDOM) {
      window.i18n.translateDOM(tabBar);
      window.i18n.translateDOM(sheet);
    }
  }

  // Export globally
  window.initMobileAppShell = initMobileAppShell;

  // Auto-init on load if in mobile view or responsive mode
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initMobileAppShell);
  } else {
    initMobileAppShell();
  }
})();
