// Small behaviours every page shares. No inline scripts (CSP), no dependencies.
(() => {
  "use strict";
  // the path the site is served under ("/news" behind the front page; "" on its own)
  const base = document.querySelector('meta[name="app-base"]')?.content || "";

  // 1. Images that cannot be fetched (offline, removed upstream) must not leave a broken-image
  //    icon or an empty grey box: cards fall back to the site initial, article figures disappear.
  const dropBrokenImage = img => {
    const card = img.closest(".card-thumb");
    if (card) {
      const fallback = document.createElement("span");
      fallback.className = "thumb-fallback";
      fallback.textContent = card.dataset.initial || "N";
      img.replaceWith(fallback);
      return;
    }
    const thumb = img.closest(".story-thumb");
    if (thumb) { thumb.remove(); return; }
    const item = img.closest(".story-item");
    if (item) { item.classList.remove("with-thumb"); img.remove(); return; }
    const figure = img.closest(".article-images figure");
    if (figure) {
      const gallery = figure.parentElement;
      figure.remove();
      if (gallery && !gallery.querySelector("figure")) gallery.remove();
    }
  };
  document.addEventListener("error", event => {
    if (event.target instanceof HTMLImageElement) dropBrokenImage(event.target);
  }, true);
  // this script is deferred: an image that failed while the page was still parsing fired its error
  // before the listener existed
  document.querySelectorAll("img").forEach(img => {
    if (img.complete && img.naturalWidth === 0 && img.getAttribute("src")) dropBrokenImage(img);
  });

  // 1b. A long caption under the lead image is cut to two lines on phones; a tap shows it all.
  document.addEventListener("click", event => {
    const caption = event.target.closest?.(".lead-image figcaption");
    if (caption) caption.classList.toggle("full");
  });

  // 1c. A page left open on the phone while a new version was deployed: offer a reload when the
  //     page is looked at again (one small request, only when the tab becomes visible).
  const pageVersion = document.querySelector('meta[name="app-version"]')?.content || "";
  let versionNotice = null;
  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState !== "visible" || !pageVersion || versionNotice) return;
    try {
      const response = await fetch(base + "/api/version", { cache: "no-store", credentials: "same-origin" });
      const { version } = await response.json();
      if (!version || version === pageVersion) return;
      versionNotice = document.createElement("button");
      versionNotice.type = "button";
      versionNotice.className = "version-notice";
      versionNotice.textContent = "新しい版になりました。タップで再読み込み";
      versionNotice.addEventListener("click", () => location.reload());
      document.body.appendChild(versionNotice);
    } catch (_) { /* offline or restarting: try again next time */ }
  });

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
      const label = THEMES[theme] || THEMES.auto;
      toggle.textContent = "配色 " + ({ light: "ライト", dark: "ダーク" }[theme] || "自動");
      toggle.setAttribute("aria-label", `表示の明るさ: ${label}（押すと切り替え）`);
      toggle.title = `表示の明るさ: ${label}`;
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

  // 2b. An article's "…の一覧へ戻る" goes back to the very list the reader came from (its kind of
  //     news, outlet, search and page), not to the outlet's first page.
  document.querySelectorAll("a[data-list-back]").forEach(link => {
    try {
      const from = new URL(document.referrer);
      if (from.origin === location.origin && from.pathname === `${base}/` && from.search) {
        link.href = from.href;
        link.textContent = "一覧へ戻る";
      }
    } catch (_) { /* no referrer */ }
  });

  // 3. "今すぐ更新" on /stories: start a regrouping, poll its status, reload when done.
  const refresh = document.querySelector('[data-action="stories-refresh"]');
  if (refresh) {
    const state = refresh.parentElement.querySelector(".refresh-state");
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
    const say = text => { if (state) state.textContent = text; };
    const poll = (tries) => {
      fetch(base + "/api/stories/status", { credentials: "same-origin", cache: "no-store" })
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
      fetch(base + "/api/stories/refresh", { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf } })
        .then(r => { if (r.status !== 202) throw new Error(String(r.status)); setTimeout(() => poll(0), 1200); })
        .catch(() => { say("開始できませんでした"); refresh.disabled = false; });
    });
  }

  // 3a. Status page: undo every "別のニュース" / "同じニュース" correction.
  document.querySelectorAll('[data-action="stories-clear-exclusions"]').forEach(clearButton => {
    clearButton.addEventListener("click", () => {
      const state = clearButton.parentElement.querySelector(".refresh-state");
      clearButton.disabled = true;
      if (state) state.textContent = "取り消しています…";
      fetch(clearButton.dataset.clearUrl || base + "/api/stories/exclusions/clear", { method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content || "" } })
        .then(r => { if (!r.ok) throw new Error(String(r.status)); location.reload(); })
        .catch(() => { clearButton.disabled = false; if (state) state.textContent = "取り消せませんでした"; });
    });
  });

  // 3b. Headlines in the reader's language on the article list, printed above each original
  //     (on unless turned off on this device). The page arrives with the Japanese headlines
  //     already translated once; only the rest — or all of them for another 翻訳先 — are fetched.
  const glossToggle = document.getElementById("gloss-toggle");
  if (glossToggle) {
    const GLOSS_KEY = "news-html-reader-list-gloss";
    const readLocal = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
    const writeLocal = (key, value) => { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } };
    const target = () => {
      const saved = readLocal("news-html-reader-translation-target");
      let menu = {};
      try { menu = JSON.parse(document.querySelector('meta[name="translation-targets"]')?.content || "{}"); } catch (_) { menu = {}; }
      return menu[saved]?.available ? saved : "ja";
    };
    const langTag = code => code === "zh-hant" ? "zh-Hant" : code; // BCP 47
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
    const originals = () => [...document.querySelectorAll("a.card[data-id] h2, [data-gloss-id]")];
    const idOf = el => Number(el.closest("a.card[data-id]")?.dataset.id || el.dataset.glossId);
    const glossOf = el => [el.previousElementSibling, el.nextElementSibling].find(n => n?.classList.contains("card-gloss")) || null;
    const clear = () => document.querySelectorAll(".card-gloss, .gloss-error").forEach(node => node.remove());
    const show = async () => {
      document.querySelectorAll(".gloss-error").forEach(node => node.remove());
      const lang = target();
      if (lang !== "ja") clear();
      const ids = [...new Set(originals().filter(el => !glossOf(el)).map(idOf).filter(Boolean))].slice(0, 40);
      if (!ids.length) return;
      glossToggle.disabled = true;
      try {
        const response = await fetch(base + "/api/translate-titles", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
          body: JSON.stringify({ ids, target: lang, auto: true })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || "見出しを翻訳できませんでした。");
        if (glossToggle.getAttribute("aria-pressed") !== "true") return; // switched off meanwhile
        for (const el of originals()) {
          const text = payload.translations?.[idOf(el)];
          if (!text || glossOf(el) || text === el.textContent.trim()) continue;
          const line = document.createElement(el.matches("h2, strong") ? "p" : "span");
          line.className = "card-gloss";
          line.lang = langTag(lang);
          line.textContent = text;
          // A headline reads in the reader's language first; in the small lists (first reports,
          // earlier reports) the original keeps its place and the translation follows it.
          if (el.closest(".digest-firsts, .digest-earlier, .outlet-scoops")) el.after(line); else el.before(line);
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
      glossToggle.textContent = on ? "原文の見出しだけにする" : "見出しを訳す";
      document.documentElement.classList.toggle("gloss-off", !on);
      writeLocal(GLOSS_KEY, on ? "1" : "0");
      if (on) show(); else clear();
    };
    glossToggle.addEventListener("click", () => set(glossToggle.getAttribute("aria-pressed") !== "true"));
    set(readLocal(GLOSS_KEY) !== "0");
  }

  // 話題を追う (D-61): follow a search from its results; stop following from the home page.
  document.addEventListener("click", async event => {
    const button = event.target.closest?.("[data-topic-follow]");
    if (!button) return;
    const follow = button.getAttribute("aria-pressed") !== "true";
    button.disabled = true;
    try {
      const response = await fetch(base + "/api/topics", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content || "" },
        body: JSON.stringify({ topic: button.dataset.topicFollow, follow })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "保存できませんでした。");
      const card = button.closest("[data-topic-card]");
      if (card && !follow) {
        const section = card.closest(".topics");
        card.remove();
        if (section && !section.querySelector("[data-topic-card]")) section.remove();
        return;
      }
      button.setAttribute("aria-pressed", String(follow));
      button.textContent = follow ? "追跡中（やめる）" : "この話題を追う";
    } catch (error) {
      button.title = error.message;
      button.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  // 同じニュース (D-72): put a similar article of another outlet into this article's group.
  //   Two taps (the first one asks), then the groups are recomputed and the group page opens.
  document.addEventListener("click", async event => {
    const button = event.target.closest?.(".join-button");
    if (!button) return;
    event.preventDefault();
    const label = "同じニュース";
    if (!button.classList.contains("confirm")) {
      document.querySelectorAll(".join-button.confirm").forEach(other => { other.classList.remove("confirm"); other.textContent = label; });
      button.classList.add("confirm");
      button.textContent = "まとめる（もう一度押す）";
      window.setTimeout(() => { if (button.isConnected && button.classList.contains("confirm") && !button.disabled) { button.classList.remove("confirm"); button.textContent = label; } }, 4000);
      return;
    }
    button.disabled = true;
    button.textContent = "まとめています…";
    try {
      const response = await fetch(base + "/api/stories/join", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content || "" },
        body: JSON.stringify({ id: Number(button.dataset.joinId), other: Number(button.dataset.joinOther) })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "まとめられませんでした。");
      location.assign(payload.redirect || base + "/stories");
    } catch (error) {
      button.disabled = false;
      button.classList.remove("confirm");
      button.textContent = error.message;
    }
  });

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
