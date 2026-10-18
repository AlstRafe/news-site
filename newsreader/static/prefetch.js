// Prefetch fallback for browsers without Speculation Rules (e.g. Safari):
// warm the HTTP cache for an article / comparison page when a pointer rests on its
// link or a finger touches it. The pages are sent with "Cache-Control: private,
// max-age=60", so the navigation that follows is served from that cache.
(() => {
  "use strict";
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
