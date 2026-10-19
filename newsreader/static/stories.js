// Same-news pages (and the related block on an article): translated headlines, the two views of a
// group, translated openings side by side, and "別のニュース" corrections. No inline scripts (CSP).
(() => {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const readLocal = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
  const writeLocal = (key, value) => { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } };
  const targetMenu = (() => { try { return JSON.parse(document.querySelector('meta[name="translation-targets"]')?.content || "{}"); } catch (_) { return {}; } })();
  const targetLang = () => {
    const saved = readLocal("news-html-reader-translation-target");
    return targetMenu[saved]?.available ? saved : "ja";
  };
  const langTag = t => targetMenu[t]?.tag || t;
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

  // 3b. "言語をそろえる": the other articles' headline and opening in the language of one outlet
  //     (e.g. CNA in English beside BBC), hidden until asked for; the reader's own translations
  //     (訳 / 書き出しを訳して比べる) stay as they are, so both can be shown at once.
  const alignButtons = [...document.querySelectorAll("[data-align]")];
  const aligned = {}; // target -> {id: {title, lead}}
  const hideAligned = () => document.querySelectorAll(".cmp-tr").forEach(box => { box.hidden = true; box.replaceChildren(); });
  async function alignTo(target, button) {
    if (button.getAttribute("aria-pressed") === "true") {
      button.setAttribute("aria-pressed", "false");
      hideAligned();
      return;
    }
    alignButtons.forEach(other => other.setAttribute("aria-pressed", "false"));
    button.setAttribute("aria-pressed", "true");
    hideAligned();
    const ids = [...new Set([...document.querySelectorAll("[data-article-id]")]
      .filter(el => el.dataset.langTarget !== target).map(el => Number(el.dataset.articleId)))];
    if (!aligned[target]) {
      button.disabled = true;
      try {
        aligned[target] = {};
        for (let i = 0; i < ids.length; i += 12) {
          const payload = await post("/api/compare-translate", { ids: ids.slice(i, i + 12), target });
          Object.assign(aligned[target], payload.items || {});
        }
        say("");
      } catch (error) {
        delete aligned[target];
        button.setAttribute("aria-pressed", "false");
        say(`翻訳できませんでした: ${error.message}`);
        return;
      } finally {
        button.disabled = false;
      }
    }
    if (button.getAttribute("aria-pressed") !== "true") return;
    const label = `${targetMenu[target]?.label || target}に翻訳（${button.querySelector("small")?.textContent || ""}と同じ言語）`;
    for (const [id, item] of Object.entries(aligned[target])) {
      document.querySelectorAll(`[data-cmp-for="${id}"]`).forEach(box => {
        const head = document.createElement("span");
        head.className = "cmp-label";
        head.textContent = label;
        const title = document.createElement("strong");
        title.className = "cmp-title";
        title.textContent = item.title || "";
        box.replaceChildren(head, title);
        if (item.lead) {
          const lead = document.createElement("p");
          lead.className = "cmp-lead";
          lead.textContent = item.lead;
          box.appendChild(lead);
        }
        const full = document.createElement("a");
        full.className = "cmp-full";
        full.href = `/a/${id}?tr=${encodeURIComponent(target)}`;
        full.textContent = `全文を${targetMenu[target]?.label || target}の対訳で読む →`;
        box.appendChild(full);
        box.lang = langTag(target);
        box.hidden = false;
      });
    }
  }
  alignButtons.forEach(button => button.addEventListener("click", () => alignTo(button.dataset.align, button)));
  // "?align=en" (from an article's "…にそろえて比べる"): start in that language.
  const askedAlign = new URLSearchParams(location.search).get("align");
  const askedButton = alignButtons.find(button => button.dataset.align === askedAlign);
  if (askedButton) alignTo(askedAlign, askedButton);

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

// 6. 違いを見る (D-56): the outlets' figures side by side (a count they disagree on is flagged),
//    and the words of the headlines only one outlet uses. Fetched when first opened.
(() => {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const key = document.querySelector('meta[name="story-key"]')?.content || "";
  const targetMenu = (() => { try { return JSON.parse(document.querySelector('meta[name="translation-targets"]')?.content || "{}"); } catch (_) { return {}; } })();
  const targetLang = () => {
    let saved = null;
    try { saved = localStorage.getItem("news-html-reader-translation-target"); } catch (_) { saved = null; }
    return targetMenu[saved]?.available ? saved : "ja";
  };
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const badge = (source, lang) => { const b = el("span", "badge", source || "未分類"); b.dataset.lang = lang || ""; return b; };
  const when = iso => {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? "" :
      date.toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };
  const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // The sentence with the figure marked: the English digits ("1,200"), without commas, or the
  // digits of the figure as shown ("少なくとも 53" -> 53).
  function sentenceWithFigure(text, fact, lang) {
    const p = el("p", "fact-sentence");
    if (lang) p.lang = lang;
    const candidates = [fact.number, String(fact.number || "").replace(/,/g, ""), ...(String(fact.text).match(/[\d.,]*\d/g) || [])]
      .filter(c => c && /\d/.test(c));
    for (const candidate of candidates) {
      const found = new RegExp(`(?<![\\d.,])${escape(candidate)}(?![\\d])`).exec(text);
      if (found) {
        p.append(text.slice(0, found.index), el("mark", "fact-num", found[0]), text.slice(found.index + found[0].length));
        return p;
      }
    }
    p.textContent = text;
    return p;
  }
  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify(body)
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "処理できませんでした。");
    return payload;
  }

  function renderFacts(panel, payload, target) {
    panel.replaceChildren();
    const tag = targetMenu[target]?.tag || target;
    if (!payload.categories?.length) {
      panel.append(el("p", "hint", "2つ以上の媒体が報じた同じ種類の数字（死者・負傷者・金額・割合など）は見つかりませんでした。"));
      return;
    }
    panel.append(el("p", "hint", `${payload.compared} 本の見出しと書き出しから数字を拾いました（英語以外の記事は英訳して照合し、文は元の言語から訳しています）。`));
    for (const category of payload.categories) {
      const box = el("section", "fact-kind" + (category.differs ? " differs" : ""));
      const head = el("h3", "", category.label);
      if (category.differs) head.append(el("span", "diff-flag", "媒体で数字が違います"));
      box.append(head);
      if (category.compared) {
        // At a glance: each report's figure in the order they were published.
        const trail = el("ol", "fact-trail");
        trail.setAttribute("aria-label", `${category.label}の数字（報じた順）`);
        for (const row of category.rows) {
          const top = row.facts.reduce((best, f) => (f.value ?? -1) > (best?.value ?? -1) ? f : best, row.facts[0]);
          const step = el("li");
          step.append(badge(row.source, row.lang), el("strong", "", top?.text || ""), el("time", "", when(row.published)));
          trail.append(step);
        }
        box.append(trail);
      }
      if (category.differs) box.append(el("p", "hint", "報じた時刻の違い（その後の更新）による場合もあります。古い順に並べています。"));
      const list = el("ul", "fact-rows");
      for (const row of category.rows) {
        const item = el("li", "fact-row");
        const meta = el("div", "fact-meta");
        meta.append(badge(row.source, row.lang), el("time", "", when(row.published)));
        const values = el("strong", "fact-values", row.facts.map(f => f.text).join(" / "));
        meta.append(values);
        const link = el("a", "fact-link", "記事");
        link.href = `/a/${row.id}`;
        meta.append(link);
        item.append(meta);
        for (const fact of row.facts) {
          if (fact.shown) item.append(sentenceWithFigure(fact.shown, fact, tag));
          if (fact.original && fact.original !== fact.shown) {
            const own = el("p", "fact-original", fact.original);
            if (row.tag) own.lang = row.tag;
            item.append(own);
          }
        }
        list.append(item);
      }
      box.append(list);
      panel.append(box);
    }
  }

  function renderWords(panel, payload) {
    panel.replaceChildren();
    const chips = (words, kind) => {
      const list = el("ul", `word-chips ${kind}`);
      for (const w of words) {
        const chip = el("li", "word-chip");
        const word = el("span", "word", w.word);
        word.lang = "en";
        chip.append(word);
        if (w.gloss) chip.append(el("small", "", w.gloss));
        list.append(chip);
      }
      return list;
    };
    if (!payload.only?.length) {
      panel.append(el("p", "hint", "比べられる見出しがありません。"));
      return;
    }
    panel.append(el("p", "hint", "見出し（英語以外は英訳）の言葉を媒体ごとに比べています。"));
    if (payload.shared?.length) {
      const shared = el("div", "word-group");
      shared.append(el("h3", "", "どの媒体も使う言葉"), chips(payload.shared, "shared"));
      panel.append(shared);
    }
    for (const entry of payload.only) {
      const group = el("div", "word-group");
      const head = el("h3", "");
      head.append(badge(entry.source, entry.lang), " だけが使う言葉");
      group.append(head);
      group.append(entry.words.length ? chips(entry.words, "only") : el("p", "hint", "（ほかの媒体と同じ言葉だけ）"));
      panel.append(group);
    }
  }

  function wire(buttonId, panelId, url, render, labels) {
    const button = document.getElementById(buttonId);
    const panel = document.getElementById(panelId);
    if (!button || !panel || !key) return;
    let loadedFor = "";
    button.addEventListener("click", async () => {
      const open = button.getAttribute("aria-expanded") === "true";
      if (open) {
        panel.hidden = true;
        button.setAttribute("aria-expanded", "false");
        button.textContent = labels.show;
        return;
      }
      const target = targetLang();
      panel.hidden = false;
      button.setAttribute("aria-expanded", "true");
      button.textContent = labels.hide;
      if (loadedFor === target) return;
      panel.replaceChildren(el("p", "hint", labels.loading));
      button.disabled = true;
      try {
        render(panel, await post(url, { key, target }), target);
        loadedFor = target;
      } catch (error) {
        panel.replaceChildren(el("p", "gloss-error hint", `${labels.failed}: ${error.message}`));
      } finally {
        button.disabled = false;
      }
    });
  }
  wire("compare-facts", "facts-panel", "/api/story-facts", renderFacts,
       { show: "数字を比べる", hide: "数字を隠す", loading: "記事の数字を集めています…", failed: "数字を比べられませんでした" });
  wire("compare-words", "words-panel", "/api/story-words", renderWords,
       { show: "言葉の違い", hide: "言葉の違いを隠す", loading: "見出しの言葉を比べています…", failed: "言葉を比べられませんでした" });
})();
