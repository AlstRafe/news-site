// Same-news pages (and the related block on an article): translated headlines, the two views of a
// group, translated openings side by side, and "別のニュース" corrections. No inline scripts (CSP).
(() => {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const readLocal = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
  const writeLocal = (key, value) => { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } };
  const targetLang = () => {
    const saved = readLocal("news-html-reader-translation-target");
    return ["ja", "en-gb", "ko", "zh-hant"].includes(saved) ? saved : "ja";
  };
  const langTag = t => (t === "zh-hant" ? "zh-Hant" : t === "en-gb" ? "en-GB" : t);
  const post = async (url, body) => {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "処理できませんでした。");
    return payload;
  };
  const errorBox = document.getElementById("story-gloss-error");
  const say = text => { if (errorBox) { errorBox.textContent = text; errorBox.hidden = !text; } };

  // 1. Headlines in the reader's language under each original (on by default; remembered).
  const GLOSS_KEY = "news-html-reader-story-gloss";
  const toggle = document.getElementById("story-gloss-toggle");
  const glossTargets = () => [...document.querySelectorAll("[data-gloss-id]")];
  const clearGloss = () => document.querySelectorAll(".story-gloss").forEach(node => node.remove());
  async function showGloss() {
    clearGloss();
    const ids = [...new Set(glossTargets().map(el => Number(el.dataset.glossId)).filter(Boolean))];
    const translations = {};
    try {
      for (let i = 0; i < ids.length; i += 40) {
        const payload = await post("/api/translate-titles", { ids: ids.slice(i, i + 40), target: targetLang() });
        Object.assign(translations, payload.translations || {});
      }
      say("");
    } catch (error) {
      say(`見出しを翻訳できませんでした: ${error.message}`);
    }
    if (toggle && toggle.getAttribute("aria-pressed") !== "true") return;
    for (const el of glossTargets()) {
      const text = translations[el.dataset.glossId];
      if (!text || text === el.textContent.trim()) continue;
      const line = document.createElement("span");
      line.className = "story-gloss";
      line.lang = langTag(targetLang());
      line.textContent = text;
      el.after(line);
    }
  }
  const setGloss = on => {
    if (toggle) {
      toggle.setAttribute("aria-pressed", String(on));
      toggle.textContent = on ? "訳を隠す" : "見出しを訳す";
    }
    writeLocal(GLOSS_KEY, on ? "1" : "0");
    if (on) showGloss(); else { clearGloss(); say(""); }
  };
  if (glossTargets().length) {
    toggle?.addEventListener("click", () => setGloss(toggle.getAttribute("aria-pressed") !== "true"));
    setGloss(readLocal(GLOSS_KEY) !== "0");
  }

  // 2. A group by outlet or as one timeline (remembered).
  const VIEW_KEY = "news-html-reader-story-view";
  const segs = [...document.querySelectorAll(".segmented .seg")];
  const setView = view => {
    for (const seg of segs) seg.setAttribute("aria-pressed", String(seg.dataset.view === view));
    document.querySelectorAll("[data-view-panel]").forEach(panel => { panel.hidden = panel.dataset.viewPanel !== view; });
    writeLocal(VIEW_KEY, view);
  };
  if (segs.length) {
    segs.forEach(seg => seg.addEventListener("click", () => setView(seg.dataset.view)));
    setView(readLocal(VIEW_KEY) === "timeline" ? "timeline" : "outlets");
  }

  // 3. The opening of every article, in full and translated, to compare how each outlet tells it.
  const leadsButton = document.getElementById("translate-leads");
  leadsButton?.addEventListener("click", async () => {
    const ids = [...new Set([...document.querySelectorAll("[data-article-id]")].map(el => Number(el.dataset.articleId)))];
    leadsButton.disabled = true;
    leadsButton.textContent = "翻訳しています…";
    try {
      const payload = await post("/api/translate-leads", { ids: ids.slice(0, 12), target: targetLang() });
      for (const [id, lead] of Object.entries(payload.leads || {})) {
        document.querySelectorAll(`[data-lead-for="${id}"]`).forEach(el => { el.textContent = lead.original; el.classList.add("full"); });
        document.querySelectorAll(`[data-lead-tr-for="${id}"]`).forEach(el => {
          el.textContent = lead.translation;
          el.lang = langTag(targetLang());
          el.hidden = !lead.translation;
        });
      }
      leadsButton.textContent = "書き出しを訳しました";
      say("");
    } catch (error) {
      leadsButton.disabled = false;
      leadsButton.textContent = "書き出しを訳して比べる";
      say(`書き出しを翻訳できませんでした: ${error.message}`);
    }
  });

  // 4. "別のニュース": two taps (the first one asks), then the group is corrected and re-grouped.
  const key = document.querySelector('meta[name="story-key"]')?.content || "";
  document.addEventListener("click", async event => {
    const button = event.target.closest?.(".exclude-button");
    if (!button || !key) return;
    event.preventDefault();
    if (!button.classList.contains("confirm")) {
      document.querySelectorAll(".exclude-button.confirm").forEach(other => { other.classList.remove("confirm"); other.textContent = "別のニュース"; });
      button.classList.add("confirm");
      button.textContent = "このグループから外す（もう一度押す）";
      window.setTimeout(() => { if (button.isConnected && button.classList.contains("confirm") && !button.disabled) { button.classList.remove("confirm"); button.textContent = "別のニュース"; } }, 4000);
      return;
    }
    button.disabled = true;
    button.textContent = "外しています…";
    try {
      const payload = await post("/api/stories/exclude", { id: Number(button.dataset.excludeId), key });
      location.replace(payload.redirect || "/stories");
    } catch (error) {
      button.disabled = false;
      button.classList.remove("confirm");
      button.textContent = "別のニュース";
      say(error.message);
    }
  });
})();
