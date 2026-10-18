// Small behaviours every page shares. No inline scripts (CSP), no dependencies.
(() => {
  "use strict";

  // 1. Images that cannot be fetched (offline, removed upstream) must not leave a broken-image
  //    icon or an empty grey box: cards fall back to the site initial, article figures disappear.
  document.addEventListener("error", event => {
    const img = event.target;
    if (!(img instanceof HTMLImageElement)) return;
    const card = img.closest(".card-thumb");
    if (card) {
      const fallback = document.createElement("span");
      fallback.className = "thumb-fallback";
      fallback.textContent = card.dataset.initial || "N";
      img.replaceWith(fallback);
      return;
    }
    const figure = img.closest(".article-images figure");
    if (figure) {
      const gallery = figure.parentElement;
      figure.remove();
      if (gallery && !gallery.querySelector("figure")) gallery.remove();
    }
  }, true);

  // 2. Theme: automatic (follow the OS) -> light -> dark -> automatic. The choice lives in
  //    localStorage("theme"); the inline boot script in <head> applies it before first paint.
  const THEMES = { auto: "自動（端末の設定に合わせる）", light: "ライト", dark: "ダーク" };
  const toggle = document.getElementById("theme-toggle");
  const root = document.documentElement;
  const readTheme = () => { try { return localStorage.getItem("theme") || "auto"; } catch (_) { return root.dataset.theme || "auto"; } };
  const applyTheme = theme => {
    if (theme === "light" || theme === "dark") root.dataset.theme = theme; else delete root.dataset.theme;
    // Phone browsers colour their toolbar from theme-color; follow a forced choice there too.
    document.querySelectorAll('meta[name="theme-color"]').forEach(meta => {
      const forDark = theme === "auto" ? meta.media.includes("dark") : theme === "dark";
      meta.content = forDark ? "#1b1d22" : "#ffffff";
    });
    if (toggle) {
      const label = THEMES[theme] || THEMES.auto;  // the icon follows data-theme via CSS
      toggle.setAttribute("aria-label", `テーマ: ${label}`);
      toggle.title = `テーマ: ${label}`;
    }
  };
  applyTheme(readTheme());
  if (toggle) {
    toggle.addEventListener("click", () => {
      const order = ["auto", "light", "dark"];
      const next = order[(order.indexOf(readTheme()) + 1) % order.length];
      try { if (next === "auto") localStorage.removeItem("theme"); else localStorage.setItem("theme", next); } catch (_) { /* private mode */ }
      applyTheme(next);
    });
  }

  // 3. Prefetch fallback for browsers without Speculation Rules (e.g. Safari on iPhone):
  //    warm the HTTP cache for an article / comparison page when a pointer rests on its link
  //    or a finger touches it. Those pages are "Cache-Control: private, max-age=60", so the
  //    navigation that follows is served from that cache.
  if (window.HTMLScriptElement && HTMLScriptElement.supports && HTMLScriptElement.supports("speculationrules")) return;
  const warmed = new Set();
  const linkOf = target => (target && target.closest ? target.closest("a[href]") : null);
  const wanted = a => a && a.origin === location.origin && /^\/(a|stories)\//.test(a.pathname) && !warmed.has(a.href);
  const warm = a => {
    warmed.add(a.href);
    fetch(a.href, { credentials: "same-origin" }).catch(() => warmed.delete(a.href));
  };
  let timer = 0;
  document.addEventListener("pointerover", event => {
    const a = linkOf(event.target);
    if (!wanted(a)) return;
    clearTimeout(timer);
    timer = setTimeout(() => warm(a), 80);
  }, { passive: true });
  document.addEventListener("pointerout", () => clearTimeout(timer), { passive: true });
  document.addEventListener("touchstart", event => {
    const a = linkOf(event.target);
    if (wanted(a)) warm(a);
  }, { passive: true });
})();
