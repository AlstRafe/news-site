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

  // 2. Prefetch fallback for browsers without Speculation Rules (e.g. Safari on iPhone):
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
