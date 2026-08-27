/**
 * AlgoPaca - Desktop Shell & Navigation Integration (>= 769px)
 * Hover dropdowns, viewport boundary alignment, ARIA states, and desktop calendar popover.
 * Loaded and executed exclusively on desktop viewports.
 */

(function () {
  if (window.__deskShellLoaded) return;
  window.__deskShellLoaded = true;

  let deskNavBound = false;

  function initDeskNav() {
    if (deskNavBound) return;
    deskNavBound = true;

    const groups = Array.from(document.querySelectorAll(".desk-nav-group"))
      .map((root) => ({
        root,
        trigger: root.querySelector(".desk-nav-trigger"),
        menu: root.querySelector(".desk-menu"),
      }))
      .filter((g) => g.trigger && g.menu);
    if (!groups.length) return;

    const canHover = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
    let closeTimer = null;

    const items = (g) => Array.from(g.menu.querySelectorAll(".desk-menu-link"));

    function close(g) {
      g.menu.hidden = true;
      g.menu.classList.remove("is-flipped");
      g.menu.style.left = "";
      g.menu.style.right = "";
      g.trigger.setAttribute("aria-expanded", "false");
    }

    /**
     * Keep the panel inside the viewport: right-align it under its trigger if
     * it would run off the right edge, then nudge it back if it is still
     * clipped.
     */
    function place(g) {
      const pad = 8;
      const vw = document.documentElement.clientWidth;
      g.menu.classList.remove("is-flipped");
      g.menu.style.left = "";
      g.menu.style.right = "";

      let box = g.menu.getBoundingClientRect();
      if (box.right > vw - pad) {
        g.menu.classList.add("is-flipped");
        box = g.menu.getBoundingClientRect();
      }

      const flipped = g.menu.classList.contains("is-flipped");
      let shift = 0;
      if (box.left < pad) shift = pad - box.left;
      else if (box.right > vw - pad) shift = vw - pad - box.right;
      if (!shift) return;
      if (flipped) g.menu.style.right = `${Math.round(-shift)}px`;
      else g.menu.style.left = `${Math.round(shift)}px`;
    }

    function closeAll(except) {
      groups.forEach((g) => {
        if (g !== except) close(g);
      });
    }

    function open(g) {
      clearTimeout(closeTimer);
      closeAll(g);
      g.menu.hidden = false;
      g.trigger.setAttribute("aria-expanded", "true");
      place(g);
    }

    function isOpen(g) {
      return !g.menu.hidden;
    }

    function focusItem(g, index) {
      const list = items(g);
      if (!list.length) return;
      const i = (index + list.length) % list.length;
      list[i].focus();
    }

    groups.forEach((g) => {
      g.trigger.addEventListener("click", (e) => {
        e.preventDefault();
        if (isOpen(g)) close(g);
        else open(g);
      });

      g.trigger.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          open(g);
          focusItem(g, 0);
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          open(g);
          focusItem(g, -1);
        } else if (e.key === "Escape" && isOpen(g)) {
          close(g);
        }
      });

      g.menu.addEventListener("keydown", (e) => {
        const list = items(g);
        const at = list.indexOf(document.activeElement);
        if (e.key === "ArrowDown") {
          e.preventDefault();
          focusItem(g, at + 1);
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          focusItem(g, at - 1);
        } else if (e.key === "Escape") {
          e.preventDefault();
          close(g);
          g.trigger.focus();
        } else if (e.key === "Tab") {
          close(g);
        }
      });

      if (canHover) {
        g.root.addEventListener("mouseenter", () => open(g));
        g.root.addEventListener("mouseleave", () => {
          clearTimeout(closeTimer);
          closeTimer = setTimeout(() => close(g), 140);
        });
      }
    });

    // Close on click outside or focus loss
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".desk-nav-group")) closeAll();
    });
    document.addEventListener("focusin", (e) => {
      if (!e.target.closest(".desk-nav-group")) closeAll();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeAll();
    });
  }

  // Export globally
  window.initDeskNav = initDeskNav;

  // Auto-init on load if ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDeskNav);
  } else {
    initDeskNav();
  }
})();
