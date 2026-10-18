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
    const item = img.closest(".story-item");
    if (item) { item.classList.remove("with-thumb"); img.remove(); return; }
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

  // 3. "今すぐ更新" on /stories: start a regrouping, poll its status, reload when done.
  const refresh = document.querySelector('[data-action="stories-refresh"]');
  if (refresh) {
    const state = refresh.parentElement.querySelector(".refresh-state");
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
    const say = text => { if (state) state.textContent = text; };
    const poll = (tries) => {
      fetch("/api/stories/status", { credentials: "same-origin", cache: "no-store" })
        .then(r => r.json())
        .then(info => {
          if (info.running && tries < 90) { setTimeout(() => poll(tries + 1), 2000); return; }
          if (info.error) { say("見出しの英訳に失敗しました（英語の見出しだけで集計）"); refresh.disabled = false; return; }
          location.reload();
        })
        .catch(() => { say("状態を取得できませんでした"); refresh.disabled = false; });
    };
    refresh.addEventListener("click", () => {
      refresh.disabled = true;
      say("集計中…");
      fetch("/api/stories/refresh", { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf } })
        .then(r => { if (r.status !== 202) throw new Error(String(r.status)); setTimeout(() => poll(0), 1200); })
        .catch(() => { say("開始できませんでした"); refresh.disabled = false; });
    });
  }

  // 3a. Status page: undo every "別のニュース" correction.
  const clearButton = document.querySelector('[data-action="stories-clear-exclusions"]');
  if (clearButton) {
    clearButton.addEventListener("click", () => {
      const state = clearButton.parentElement.querySelector(".refresh-state");
      clearButton.disabled = true;
      if (state) state.textContent = "取り消しています…";
      fetch("/api/stories/exclusions/clear", { method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content || "" } })
        .then(r => { if (!r.ok) throw new Error(String(r.status)); location.reload(); })
        .catch(() => { clearButton.disabled = false; if (state) state.textContent = "取り消せませんでした"; });
    });
  }

  // 3b. "見出しを訳す" on the article list: a translated line under each card's headline.
  //     On/off is remembered per device; the language follows the article page's 翻訳先.
  const glossToggle = document.getElementById("gloss-toggle");
  if (glossToggle) {
    const GLOSS_KEY = "news-html-reader-list-gloss";
    const readLocal = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
    const writeLocal = (key, value) => { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } };
    const target = () => {
      const saved = readLocal("news-html-reader-translation-target");
      return ["ja", "en-gb", "ko", "zh-hant"].includes(saved) ? saved : "ja";
    };
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
    const cards = () => [...document.querySelectorAll("a.card[data-id]")];
    const clear = () => document.querySelectorAll(".card-gloss, .gloss-error").forEach(node => node.remove());
    const show = async () => {
      clear();
      const ids = cards().map(card => Number(card.dataset.id)).filter(Boolean);
      if (!ids.length) return;
      glossToggle.disabled = true;
      try {
        const response = await fetch("/api/translate-titles", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
          body: JSON.stringify({ ids, target: target() })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || "見出しを翻訳できませんでした。");
        if (glossToggle.getAttribute("aria-pressed") !== "true") return; // switched off meanwhile
        for (const card of cards()) {
          const text = payload.translations?.[card.dataset.id];
          const heading = card.querySelector("h2");
          if (!text || !heading || text === heading.textContent.trim()) continue;
          const line = document.createElement("p");
          line.className = "card-gloss";
          line.lang = target() === "zh-hant" ? "zh-Hant" : target() === "en-gb" ? "en-GB" : target();
          line.textContent = text;
          heading.after(line);
        }
      } catch (error) {
        const note = document.createElement("p");
        note.className = "gloss-error hint";
        note.setAttribute("role", "status");
        note.textContent = error.message || "見出しを翻訳できませんでした。";
        glossToggle.closest(".list-head")?.after(note);
      } finally {
        glossToggle.disabled = false;
      }
    };
    const set = on => {
      glossToggle.setAttribute("aria-pressed", String(on));
      glossToggle.textContent = on ? "訳を隠す" : "見出しを訳す";
      writeLocal(GLOSS_KEY, on ? "1" : "0");
      if (on) show(); else clear();
    };
    glossToggle.addEventListener("click", () => set(glossToggle.getAttribute("aria-pressed") !== "true"));
    if (readLocal(GLOSS_KEY) === "1") set(true);
  }

  // 4. Prefetch fallback for browsers without Speculation Rules (e.g. Safari on iPhone):
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
