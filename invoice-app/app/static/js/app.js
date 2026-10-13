/* Shared UI behaviour: navigation drawer, menus, dialogs, toasts, tabs, copy, submit guards.
   Motion (window.Motion) animates transitions; everything works without it. */
(function () {
  "use strict";
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const M = window.Motion;
  // Motion takes keyframes as { property: [from, to] }. Callers pass frame lists for readability.
  const toTracks = frames => frames.reduce((tracks, frame) => {
    Object.entries(frame).forEach(([key, value]) => { (tracks[key] = tracks[key] || []).push(value); });
    return tracks;
  }, {});
  const animate = (el, frames, options) => {
    if (reduce || !M || !M.animate || !el) return Promise.resolve();
    const control = M.animate(el, toTracks(frames), { duration: 0.18, ease: [0.2, 0.7, 0.2, 1], ...options });
    return control.finished || Promise.resolve();
  };
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* ---------- mobile navigation ---------- */
  const nav = document.getElementById("mobile-nav");
  function openNav() {
    if (!nav) return;
    nav.hidden = false;
    animate(nav.querySelector("aside"), [{ transform: "translateX(-100%)" }, { transform: "translateX(0)" }], { duration: 0.22 });
    nav.querySelector("a, button")?.focus();
  }
  function closeNav() {
    if (!nav || nav.hidden) return;
    nav.hidden = true;
    document.querySelector("[data-open-nav]")?.focus();
  }
  $$("[data-open-nav]").forEach(b => b.addEventListener("click", openNav));
  $$("[data-close-nav]").forEach(b => b.addEventListener("click", closeNav));

  /* ---------- dropdown menus ---------- */
  function closeMenus(except) {
    $$("[data-menu]").forEach(menu => {
      if (menu === except) return;
      const panel = menu.querySelector("[data-menu-panel]");
      const trigger = menu.querySelector("[data-menu-trigger]");
      if (panel && !panel.hidden) {
        panel.hidden = true;
        trigger?.setAttribute("aria-expanded", "false");
      }
    });
  }
  function openMenu(menu, focusFirst) {
    closeMenus(menu);
    const panel = menu.querySelector("[data-menu-panel]");
    const trigger = menu.querySelector("[data-menu-trigger]");
    panel.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    animate(panel, [{ opacity: 0, transform: "translateY(-4px)" }, { opacity: 1, transform: "translateY(0)" }]);
    if (focusFirst) panel.querySelector('[role="menuitem"]:not([disabled])')?.focus();
  }
  document.addEventListener("click", e => {
    const trigger = e.target.closest("[data-menu-trigger]");
    if (trigger) {
      const menu = trigger.closest("[data-menu]");
      const panel = menu.querySelector("[data-menu-panel]");
      panel.hidden ? openMenu(menu, false) : closeMenus();
      return;
    }
    if (!e.target.closest("[data-menu-panel]")) closeMenus();
  });
  document.addEventListener("keydown", e => {
    const trigger = e.target.closest?.("[data-menu-trigger]");
    if (trigger && (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ")) {
      if (e.key === "ArrowDown") { e.preventDefault(); openMenu(trigger.closest("[data-menu]"), true); }
    }
    const item = e.target.closest?.('[role="menuitem"]');
    if (item) {
      const items = $$('[role="menuitem"]:not([disabled])', item.closest('[role="menu"]'));
      const i = items.indexOf(item);
      if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
      if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
      if (e.key === "Escape") {
        const menu = item.closest("[data-menu]");
        closeMenus();
        menu?.querySelector("[data-menu-trigger]")?.focus();
      }
      if (e.key === "Tab") closeMenus();
    }
    if (e.key === "Escape") {
      closeMenus();
      if (nav && !nav.hidden) closeNav();
    }
  });

  /* ---------- dialogs ---------- */
  function openDialog(dialog) {
    if (!dialog || dialog.open) return;
    dialog.showModal();
    animate(dialog, [{ opacity: 0, transform: "translateY(6px) scale(0.98)" }, { opacity: 1, transform: "none" }], { duration: 0.2 });
    dialog.querySelector("input, textarea, select, button[type=submit]")?.focus();
  }
  function closeDialog(dialog) {
    if (!dialog || !dialog.open) return;
    dialog.close();
  }
  $$("[data-dialog-open]").forEach(btn => btn.addEventListener("click", () => {
    const target = document.getElementById(btn.dataset.dialogOpen);
    if (target) openDialog(target);
  }));
  $$("dialog").forEach(dialog => {
    dialog.addEventListener("click", e => {
      if (e.target === dialog) closeDialog(dialog); // backdrop click
    });
    dialog.querySelectorAll("[data-dialog-close]").forEach(b => b.addEventListener("click", () => closeDialog(dialog)));
  });

  /* ---------- toasts ---------- */
  function dismissToast(toast) {
    if (!toast || toast.dataset.leaving) return;
    toast.dataset.leaving = "1";
    animate(toast, [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(6px)" }], { duration: 0.16 })
      .then(() => toast.remove());
    if (reduce) toast.remove();
  }
  $$("[data-toast]").forEach((toast, i) => {
    animate(toast, [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "none" }], { duration: 0.25, delay: i * 0.05 });
    setTimeout(() => dismissToast(toast), 5200);
  });
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-dismiss-toast]");
    if (b) dismissToast(b.closest("[data-toast]"));
  });

  /* ---------- tabs (settings) ---------- */
  $$("[data-tabs]").forEach(root => {
    const tabs = $$("[data-tab-target]", root);
    const select = name => {
      tabs.forEach(t => {
        const on = t.dataset.tabTarget === name;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        t.classList.toggle("text-foreground", on);
        t.classList.toggle("border-primary", on);
        t.classList.toggle("text-muted-foreground", !on);
        t.classList.toggle("border-transparent", !on);
        const panel = document.getElementById(t.dataset.tabTarget);
        if (panel) panel.hidden = !on;
      });
    };
    tabs.forEach(t => t.addEventListener("click", () => {
      select(t.dataset.tabTarget);
      history.replaceState(null, "", "#" + t.dataset.tabTarget);
    }));
    tabs.forEach((t, i) => t.addEventListener("keydown", e => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
      next.focus();
      select(next.dataset.tabTarget);
    }));
    const fromHash = location.hash.slice(1);
    const start = tabs.find(t => t.dataset.tabTarget === fromHash) || tabs[0];
    if (start) select(start.dataset.tabTarget);
  });

  /* ---------- copy buttons ---------- */
  $$("[data-copy]").forEach(btn => btn.addEventListener("click", async () => {
    const text = btn.dataset.copy;
    try {
      await navigator.clipboard.writeText(text);
      const label = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = label), 1400);
    } catch (_) {
      btn.dataset.copyFallback = "1";
      btn.title = text;
    }
  }));

  /* ---------- submit guards: one submit per click ---------- */
  document.addEventListener("submit", e => {
    const form = e.target;
    if (form.dataset.submitting === "1") { e.preventDefault(); return; }
    if (form.hasAttribute("data-once")) {
      form.dataset.submitting = "1";
      $$("button[type=submit], input[type=submit]", form).forEach(b => { b.disabled = true; b.setAttribute("aria-busy", "true"); });
    }
  });
  document.addEventListener("click", e => {
    const a = e.target.closest("a[data-once]");
    if (a) a.setAttribute("aria-disabled", "true");
  });
  window.addEventListener("pageshow", () => {
    $$("form[data-submitting]").forEach(f => { f.dataset.submitting = ""; });
    $$("button[aria-busy]").forEach(b => { b.disabled = false; b.removeAttribute("aria-busy"); });
  });

  /* ---------- entrance motion for page content ---------- */
  const main = document.getElementById("main");
  if (main && !reduce && M && M.animate) {
    animate(main.firstElementChild, [{ opacity: 0, transform: "translateY(6px)" }, { opacity: 1, transform: "none" }],
      { duration: 0.32 });
  }
})();
